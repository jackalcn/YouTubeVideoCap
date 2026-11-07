from __future__ import annotations

import heapq
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from videocap.tooling import get_ffprobe_status


VIDEO_EXTENSIONS = frozenset({".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".ts"})
AUDIO_EXTENSIONS = frozenset({".m4a", ".mp3", ".opus", ".flac", ".wav", ".ogg", ".aac"})
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
SUBTITLE_EXTENSIONS = frozenset({".srt", ".vtt", ".ass", ".ttml", ".json3", ".srv1", ".srv2", ".srv3"})
THUMBNAIL_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
SIDECAR_EXTENSIONS = SUBTITLE_EXTENSIONS | THUMBNAIL_EXTENSIONS | {".txt", ".description", ".json", ".md", ".csv", ".nfo"}
# The downloader's own files name their kind before the extension: "clip.comments.csv", "clip.live_chat.json".
MARKED_SIDECAR_KINDS = frozenset({"comments", "live_chat"})
# Only those: a plain "clip.csv" is someone's spreadsheet, not ours to delete with the video.
MARKED_ONLY_EXTENSIONS = frozenset({".csv"})
# Kodi and Jellyfin read "clip-thumb.jpg" as the cover of "clip.mp4".
EPISODE_THUMB_SUFFIX = "-thumb"
# Browsers can only play these, so offering a preview of anything else would just show an error.
BROWSER_PLAYABLE_EXTENSIONS = frozenset({".mp4", ".webm", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".flac", ".mov", ".m4v"})
# What yt-dlp leaves while a download runs: "title.mp4.part", "title.f137.mp4.part-Frag12" and "title.mp4.ytdl".
PARTIAL_SUFFIX_RE = re.compile(r"\.(?:part(?:-Frag\d+)?|ytdl)$", re.IGNORECASE)
# yt-dlp's and the tools' own temporary results, such as "title.temp.mp4".
TEMP_OUTPUT_MARK = ".temp"
# What the tools write besides media and pictures, which the library does not list.
TOOL_OUTPUT_EXTENSIONS = frozenset({".gif"})
# yt-dlp names a stream waiting to be merged like "title.f137.mp4" or "title.f251-drc.webm".
FORMAT_PART_RE = re.compile(r"f\d{2,3}(?:-[A-Za-z0-9]+)*")
DOWNLOADED_EXTENSIONS = MEDIA_EXTENSIONS | SUBTITLE_EXTENSIONS | THUMBNAIL_EXTENSIONS
# The language part of a subtitle name, such as "en", "zh-TW", or "es-419".
LANGUAGE_SEGMENT_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
# Sidecars whose names carry a language: "clip.en.srt", and the transcript "clip.en.txt" written from it.
LANGUAGE_NAMED_EXTENSIONS = SUBTITLE_EXTENSIONS | {".txt"}
SCAN_LIMIT = 5000
# Windows refuses these in a file or folder name.
WINDOWS_RESERVED_CHARACTERS = frozenset('<>:"|?*')
# Recycle bins that can sit inside a library: Windows' at a drive's root, the Linux desktop's and macOS's on other
# drives, and Synology's and QNAP's on network shares. What was deleted must not show up in the library again.
RECYCLE_BIN_FOLDER_RE = re.compile(r"\$recycle\.bin|\.trash(?:es|-\d+)?|#recycle|@recycle", re.IGNORECASE)
# Public visitors' files and the tools' scratch copies live in the temp folder, where a recycle bin would only keep
# strangers' files on the host. Read once at start.
TEMP_FOLDER = Path(tempfile.gettempdir())
# GetDriveTypeW's answer for a local hard drive, the only kind Windows keeps a recycle bin for.
WINDOWS_FIXED_DRIVE = 3
LINUX_DESKTOP_VARIABLES = ("XDG_CURRENT_DESKTOP", "DISPLAY", "WAYLAND_DISPLAY")
RECYCLED_TEXT = "已移到回收桶"
REMOVED_TEXT = "已永久刪除（這個位置沒有回收桶）"


def load_send_to_trash() -> Optional[Callable[[str], None]]:
    """send2trash's function, or None in an install from before it was required, which then deletes as it always did."""
    try:
        from send2trash import send2trash
    except ImportError:
        return None
    return send2trash


send_to_trash = load_send_to_trash()
# Drives where the recycle bin failed and the file was deleted for good instead, so the next delete there says so first.
_devices_without_recycle_bin: set[int] = set()


@dataclass
class LibraryEntry:
    path: Path
    size: int
    modified_at: float
    kind: str
    sidecars: list[Path] = field(default_factory=list)

    @property
    def subtitles(self) -> list[Path]:
        return [path for path in self.sidecars if path.suffix.lower() in SUBTITLE_EXTENSIONS]


@dataclass
class LibraryScan:
    entries: list[LibraryEntry] = field(default_factory=list)
    # Media files found, which is more than the entries when the list is cut at the limit.
    media_count: int = 0
    # Leftovers of interrupted downloads and tools, with their sizes.
    partial_files: dict[Path, int] = field(default_factory=dict)
    # Streams like "title.f137.mp4" with no finished "title.*" beside them. A download killed mid-run saves nothing that
    # would let a cancel find them, but a running or paused one may still need them.
    orphan_streams: dict[Path, int] = field(default_factory=dict)


def scan_folder(root: Path, limit: int = SCAN_LIMIT) -> LibraryScan:
    """Finds media files under a folder, newest first, with the subtitle and info files that share each name, and the
    partial files that interrupted downloads left. One pass, and only names that matter are looked at further, because
    a library can hold tens of thousands of files."""
    scan = LibraryScan()
    if not root.is_dir():
        return scan

    media: list[LibraryEntry] = []
    sidecars_by_folder: dict[Path, list[Path]] = {}
    streams: dict[Path, int] = {}
    folders = [root]
    while folders:
        folder = folders.pop()
        try:
            with os.scandir(folder) as listing:
                items = list(listing)
        except OSError:
            continue
        for item in items:
            try:
                if item.is_dir(follow_symlinks=False):
                    if not RECYCLE_BIN_FOLDER_RE.fullmatch(item.name):
                        folders.append(Path(item.path))
                    continue
                if not item.is_file():
                    continue
                name = item.name
                suffix = os.path.splitext(name)[1].lower()
                if is_partial_file(name):
                    # On Windows the size comes with the folder listing, so this costs nothing extra.
                    found = streams if is_intermediate_stream(Path(name)) else scan.partial_files
                    found[Path(item.path)] = item.stat().st_size
                elif suffix in MEDIA_EXTENSIONS:
                    stat = item.stat()
                    media.append(
                        LibraryEntry(
                            path=Path(item.path),
                            size=stat.st_size,
                            modified_at=stat.st_mtime,
                            kind="video" if suffix in VIDEO_EXTENSIONS else "audio",
                        )
                    )
                elif suffix in SIDECAR_EXTENSIONS:
                    sidecars_by_folder.setdefault(folder, []).append(Path(item.path))
            except OSError:
                continue

    scan.media_count = len(media)
    finished = {(entry.path.parent, entry.path.stem) for entry in media}
    scan.orphan_streams = {path: size for path, size in streams.items() if (path.parent, stream_owner_stem(path.name)) not in finished}
    # Keep the newest rather than whatever the folder order reached first, so a new download is never the one left out.
    scan.entries = heapq.nlargest(limit, media, key=lambda entry: entry.modified_at)
    entries_by_stem = {(entry.path.parent, entry.path.stem): entry for entry in scan.entries}
    for folder, candidates in sidecars_by_folder.items():
        for candidate in candidates:
            owner = next(
                (entries_by_stem[(folder, stem)] for stem in sidecar_owner_stems(candidate.name) if (folder, stem) in entries_by_stem),
                None,
            )
            if owner:
                owner.sidecars.append(candidate)
    for entry in scan.entries:
        entry.sidecars.sort(key=lambda path: path.name)
    return scan


def scan_library(root: Path, limit: int = SCAN_LIMIT) -> list[LibraryEntry]:
    return scan_folder(root, limit).entries


def sidecar_owner_stems(name: str) -> list[str]:
    """The names of the video a sidecar may belong to, most likely first: "clip.en.srt" and "clip.webp" belong to
    "clip", but "Learn Node.js.webp" belongs to "Learn Node.js".

    Matching by prefix would also hand "Lesson 1. Intro.en.srt" to "Lesson 1", and deleting one video would take the
    other's subtitles with it."""
    if name.endswith(".info.json"):
        return [name.removesuffix(".info.json")]
    stem, extension = os.path.splitext(name)
    base, dot, last = stem.rpartition(".")
    if dot and last in MARKED_SIDECAR_KINDS:
        return [base, stem]
    if extension.lower() in MARKED_ONLY_EXTENSIONS:
        return []
    if extension.lower() in THUMBNAIL_EXTENSIONS and stem.endswith(EPISODE_THUMB_SUFFIX):
        # A video may itself be called "Poster-thumb", and then this is its own cover.
        return [stem, stem.removesuffix(EPISODE_THUMB_SUFFIX)]
    if not (dot and LANGUAGE_SEGMENT_RE.match(last)):
        return [stem]
    # yt-dlp puts a language in subtitle names but never in thumbnail or description names.
    language_named = extension.lower() in LANGUAGE_NAMED_EXTENSIONS
    return [base, stem] if language_named else [stem, base]


def sidecar_owner_stem(name: str) -> str:
    return next(iter(sidecar_owner_stems(name)), "")


def media_entry(path: Path) -> LibraryEntry:
    """A media file with the subtitles, thumbnail and info files beside it, paired the way scan_folder pairs them but
    without walking the whole library: a sidecar belongs to the first of its possible owners that exists, so a
    similar title keeps its own files. Beside "clip.m4a", "clip.mp4" has none: their subtitles and cover are shared,
    and deleting one must leave them to the other."""
    try:
        with os.scandir(path.parent) as listing:
            names = [item.name for item in listing if item.is_file()]
    except OSError:
        names = []
    media_names = [name for name in names if os.path.splitext(name)[1].lower() in MEDIA_EXTENSIONS]
    media_stems = {os.path.splitext(name)[0] for name in media_names}
    shared = sum(os.path.splitext(name)[0] == path.stem for name in media_names) > 1
    sidecars = [] if shared else [
        path.parent / name
        for name in names
        if os.path.splitext(name)[1].lower() in SIDECAR_EXTENSIONS
        and next((stem for stem in sidecar_owner_stems(name) if stem in media_stems), None) == path.stem
    ]
    stat = path.stat()
    kind = "video" if path.suffix.lower() in VIDEO_EXTENSIONS else "audio"
    return LibraryEntry(path=path, size=stat.st_size, modified_at=stat.st_mtime, kind=kind, sidecars=sorted(sidecars))


def is_intermediate_stream(path: Path) -> bool:
    """yt-dlp names a stream waiting to be merged like "title.f137.mp4"."""
    parts = path.name.split(".")
    return len(parts) >= 3 and FORMAT_PART_RE.fullmatch(parts[-2]) is not None


def stream_owner_stem(name: str) -> str:
    """The finished file's name without its extension: "title" for "title.f137.mp4"."""
    return name.rsplit(".", 2)[0]


def partial_output_path(path: Path) -> Path:
    """Where a result is written until it is complete, named the way yt-dlp names its own temporary files."""
    return path.with_name(f"{path.stem}{TEMP_OUTPUT_MARK}{path.suffix}")


def is_partial_file(name: str) -> bool:
    """Only names this app and yt-dlp make. A shared Downloads folder also holds other programs' ".part" and ".tmp"
    files, such as a browser's download in progress, which are not ours to delete."""
    match = PARTIAL_SUFFIX_RE.search(name)
    if match:
        return os.path.splitext(name[: match.start()])[1].lower() in DOWNLOADED_EXTENSIONS
    stem, suffix = os.path.splitext(name)
    if suffix.lower() not in MEDIA_EXTENSIONS | THUMBNAIL_EXTENSIONS | TOOL_OUTPUT_EXTENSIONS:
        return False
    return stem.lower().endswith(TEMP_OUTPUT_MARK) or is_intermediate_stream(Path(name))


def find_partial_files(root: Path) -> list[Path]:
    scan = scan_folder(root)
    return [*scan.partial_files, *scan.orphan_streams]


def folder_creation_problem(folder: Path) -> str:
    """Why a folder could never be created, or "" when it already exists or the first download can create it."""
    if not folder.is_absolute() or not Path(folder.anchor).exists():
        return f"找不到磁碟「{folder.anchor or folder}」，請確認資料夾路徑。"
    if sys.platform == "win32" and any(WINDOWS_RESERVED_CHARACTERS & set(part) for part in folder.parts[1:]):
        return "資料夾名稱不能含有 < > : \" | ? * 這些符號，請確認資料夾路徑。"
    existing = folder
    while not existing.exists():
        existing = existing.parent
    if not existing.is_dir():
        return f"「{existing.name}」是檔案，不是資料夾，請確認資料夾路徑。"
    return ""


def is_inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


class DeleteResult(list[str]):
    """The problems, one for each file that could not be deleted, as delete_entry has always returned them. It also
    counts the files that went to the recycle bin and those deleted for good, so a page can say which happened."""

    def __init__(self) -> None:
        super().__init__()
        self.trashed = 0
        self.removed = 0

    @property
    def outcome(self) -> str:
        if self.trashed and self.removed:
            return f"{self.trashed} 個檔案已移到回收桶，{self.removed} 個已永久刪除（那裡沒有回收桶）"
        if self.removed:
            return REMOVED_TEXT
        # Nothing was there any more, such as a file deleted by hand while the page was open.
        return RECYCLED_TEXT if self.trashed else "已刪除"


def delete_entry(entry: LibraryEntry, root: Path, include_sidecars: bool = True) -> DeleteResult:
    """Deletes a media file and optionally its sidecars, into the recycle bin where the folder has one. Refuses anything
    outside the library folder. Returns the problems, which also tell where the files went."""
    targets = [entry.path, *(entry.sidecars if include_sidecars else [])]
    result = DeleteResult()
    for target in targets:
        if not is_inside(target, root):
            result.append(f"{target.name} 不在下載資料夾內，沒有刪除。")
            continue
        if not os.path.lexists(target):
            continue
        try:
            if remove_file(target):
                result.trashed += 1
            else:
                result.removed += 1
        except OSError as exc:
            result.append(f"{target.name} 無法刪除：{exc}")
    return result


def remove_file(path: Path) -> bool:
    """Deletes a file, into the recycle bin where its folder has one. True when it went there; raises OSError when it
    could not be deleted at all."""
    if send_to_trash is None or not recycle_bin_available(path.parent):
        path.unlink(missing_ok=True)
        return False
    try:
        send_to_trash(str(path))
        return True
    except OSError:
        # TrashPermissionError on a NAS share or a Docker volume. A file another program has open fails again below,
        # which says nothing about the recycle bin.
        path.unlink(missing_ok=True)
    device = folder_device(path.parent)
    if device is not None:
        _devices_without_recycle_bin.add(device)
    return False


def recycle_bin_available(folder: Path) -> bool:
    """Whether deleting in this folder goes to the recycle bin, for a page to say before anything is deleted. Nothing is
    moved to find out, so this is cheap enough to ask on every run of a page."""
    if send_to_trash is None or is_inside(folder, TEMP_FOLDER):
        return False
    if folder_device(folder) in _devices_without_recycle_bin:
        return False
    return folder_has_recycle_bin(folder)


def folder_has_recycle_bin(folder: Path, platform: str = sys.platform) -> bool:
    if platform == "win32":
        # On a network drive, a USB stick or a RAM disk, Windows deletes for good and still reports the file recycled.
        return windows_drive_type(folder) == WINDOWS_FIXED_DRIVE
    if platform == "darwin":
        return True
    # Without a desktop, as in Docker or on a server, nobody would ever empty a trash folder and it would fill the disk.
    return any(os.environ.get(name) for name in LINUX_DESKTOP_VARIABLES)


def windows_drive_type(folder: Path) -> int:
    import ctypes

    try:
        # Resolved, so a folder linked to a network share counts as that share. Mapped drive letters and
        # "\\server\share\" roots both answer as remote.
        anchor = folder.resolve().anchor
    except OSError:
        return 0
    return int(ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(anchor)))  # type: ignore[attr-defined]


def folder_device(folder: Path) -> Optional[int]:
    try:
        return os.stat(folder).st_dev
    except OSError:
        return None


def probe_media(path: Path) -> Optional[dict[str, Any]]:
    available, ffprobe = get_ffprobe_status()
    if not available:
        return None
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_format", "-show_streams", "-show_chapters", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
            # Without this, every probe from the windowless app flashes a console window on Windows.
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def get_media_duration(path: Path) -> Optional[float]:
    probe = probe_media(path)
    try:
        return float(probe["format"]["duration"]) if probe else None
    except (KeyError, TypeError, ValueError):
        return None


def describe_probe(probe: dict[str, Any]) -> list[str]:
    details: list[str] = []
    for stream in probe.get("streams") or []:
        kind = stream.get("codec_type")
        codec = stream.get("codec_name") or ""
        if kind == "video" and stream.get("disposition", {}).get("attached_pic"):
            details.append(f"封面 {codec}")
        elif kind == "video":
            width, height = display_size(stream)
            details.append(f"影像 {codec} {width}x{height}")
        elif kind == "audio":
            language = stream.get("tags", {}).get("language")
            details.append(f"音訊 {codec}" + (f"（{language}）" if language else ""))
        elif kind == "subtitle":
            language = stream.get("tags", {}).get("language")
            details.append(f"字幕 {language or codec}")
    chapters = probe.get("chapters") or []
    if chapters:
        details.append(f"{len(chapters)} 個章節")
    return details


def display_size(stream: dict[str, Any]) -> tuple[Any, Any]:
    """Phone videos are often stored sideways with a rotation flag, and play upright."""
    width, height = stream.get("width"), stream.get("height")
    rotation: Any = stream.get("tags", {}).get("rotate", 0)
    for side_data in stream.get("side_data_list") or []:
        rotation = side_data.get("rotation", rotation)
    try:
        sideways = round(abs(float(rotation))) % 180 == 90
    except (TypeError, ValueError):
        sideways = False
    return (height, width) if sideways else (width, height)


def open_in_file_manager(path: Path) -> Optional[str]:
    """Opens a folder or file on the machine running the app. Returns a problem, or None."""
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as exc:
        return f"無法開啟：{exc}"
    return None

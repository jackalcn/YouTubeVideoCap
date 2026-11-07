from __future__ import annotations

import math
import os
import re
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence

from videocap.library import LANGUAGE_SEGMENT_RE, display_size, partial_output_path, probe_media
from videocap.processes import end_process_tree, remove_when_released, start_tied_process
from videocap.subtitles import format_timestamp_short
from videocap.textfiles import read_text_file
from videocap.tooling import format_bytes, get_ffmpeg_status


TOOL_LABELS = {
    "trim": "剪輯片段",
    "to_h264": "轉成 H.264 MP4",
    "compress": "壓縮影片",
    "extract_audio": "抽出音訊",
    "embed_subtitles": "嵌入字幕（可開關）",
    "burn_subtitles": "燒錄字幕（永久顯示）",
    "frame": "擷取畫面",
    "loudnorm": "音量標準化",
    "join": "合併影片",
    "gif": "轉成 GIF",
    "transcribe": "語音辨識字幕",
}
AUDIO_CODECS = {"mp3": "libmp3lame", "m4a": "aac", "opus": "libopus", "flac": "flac", "wav": "pcm_s16le"}
# A precise trim of an audio file re-encodes it, and each container only takes its own codec.
AUDIO_ENCODERS = {
    ".mp3": ["-c:a", "libmp3lame", "-b:a", "192k"],
    ".m4a": ["-c:a", "aac", "-b:a", "192k"],
    ".aac": ["-c:a", "aac", "-b:a", "192k"],
    ".opus": ["-c:a", "libopus", "-b:a", "160k"],
    ".ogg": ["-c:a", "libvorbis", "-q:a", "6"],
    ".flac": ["-c:a", "flac"],
    ".wav": ["-c:a", "pcm_s16le"],
}
# Loudness normalization keeps each file's container, so the sound is encoded again in what that container takes.
LOUDNORM_ENCODERS = {**AUDIO_ENCODERS, ".webm": ["-c:a", "libopus", "-b:a", "160k"], ".avi": ["-c:a", "libmp3lame", "-b:a", "192k"]}
# EBU R128 loudness in one pass. -16 suits phones and streaming; -23 is what TV broadcasts use.
LOUDNESS_TARGETS = {"標準（-16 LUFS）": -16, "較安靜（-23 LUFS，廣播標準）": -23}
LOUDNORM_TRUE_PEAK = -1.5
LOUDNORM_RANGE = 11
GIF_WIDTHS = (240, 320, 480, 640)
GIF_FRAME_RATES = (10, 15, 20)
# Longer GIFs grow to tens of megabytes, more than chat apps and web pages take.
GIF_MAX_SECONDS = 60
# The first GIF pass writes its colours here, in the scratch folder ffmpeg runs in.
GIF_PALETTE_NAME = "palette.png"
# concat needs every part's sound in one format, and a part without sound gets silence in it.
JOIN_AUDIO_FORMAT = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
JOIN_SILENCE = "anullsrc=r=48000:cl=stereo"
# ffprobe's frame rate, such as "30000/1001"; "0/0" means unknown.
FRAME_RATE_RE = re.compile(r"[1-9]\d{0,5}(?:/[1-9]\d{0,5})?")
# Lower is better quality. Roughly: 18 near-lossless, 23 default, 28 clearly smaller.
COMPRESS_LEVELS = {"輕度（畫質優先）": 23, "中度": 28, "重度（檔案優先）": 32}
COMPRESS_HEIGHTS = {"維持解析度": None, "1080p": 1080, "720p": 720, "480p": 480}
# Room for the container's own overhead, so the result lands under the size asked for.
TARGET_SIZE_MARGIN = 0.97
# Below this H.264 is a smear, whatever the resolution.
MIN_TARGET_VIDEO_KBPS = 150
PROGRESS_TIME_RE = re.compile(r"^out_time_ms=(\d+)")
PROGRESS_LINE_RE = re.compile(r"^[a-z_0-9]+=\S*\s*$")
# ffmpeg 7 names the cause first and then several generic lines about shutting down.
FFMPEG_NOISE_RE = re.compile(
    r"Terminating thread|Task finished|Error sending frames|Could not open encoder before EOF|Conversion failed|"
    r"Error while filtering|Nothing was written|Error closing file|Error while processing the decoded data|Exiting normally"
)
FFMPEG_LINE_PREFIX_RE = re.compile(r"^\[[^\]]*@ [0-9a-fA-Fx]+\]\s*")
# With "-loglevel level+warning" each line names its level after the optional "[filter @ 0x...]" part.
FFMPEG_LEVEL_TAG_RE = re.compile(r"^((?:\[[^\]]*@ [0-9a-fA-Fx]+\]\s*)?)\[(panic|fatal|error|warning)\]\s*")
# libass prints this when no installed font has a character, which then shows as a box burned into every frame.
# Its "Glyph ... not found" line is not a sign of this: it also comes before a fallback font that does have it.
MISSING_GLYPH_MARKER = "failed to find any fallback"
MISSING_FONT_WARNING = "字幕可能有些字顯示成方框：這台電腦缺少能顯示這些字的字型（例如中文字型）。安裝字型後再燒錄一次即可。"
FFMPEG_HINTS = (
    ("No space left on device", "硬碟空間不足。"),
    ("Permission denied", "無法寫入，可能沒有權限，或檔案正被其他程式使用。"),
    ("Invalid data found when processing input", "檔案可能已損壞，或不是影音檔。"),
    ("does not contain any stream", "這段時間內沒有影像或聲音。"),
    ("matches no streams", "來源檔案裡沒有這項工具需要的影像或聲音。"),
    ("Invalid UTF-8", "字幕檔的文字編碼無法辨識。"),
    ("not divisible by 2", "影片的寬或高是奇數。"),
    ("Could not find tag for codec", "這種格式放不進目前的檔案類型。"),
    ("supported for WebM", "這種格式放不進 WebM 檔。"),
)
# ffmpeg's subtitles filter parses its argument twice, and titles can hold any character. A fixed name in a
# scratch folder, with ffmpeg running from that folder, avoids having to escape the real path.
BURN_SUBTITLE_NAME = "subtitle"
SAFE_SUBTITLE_SUFFIX_RE = re.compile(r"\.[0-9a-z]{1,10}")
# libass sizes SRT and WebVTT text against a 288-line-high frame, whatever the video's own height; 16 is its default.
BURN_FONT_SIZES = {"小": 12, "標準": 16, "大": 22, "特大": 30}
BURN_FONT_SIZE_RANGE = (6, 120)
# libass reads force_style's Alignment in the old SSA numbering (top centre is 6), not the keypad layout of ASS
# files (where it is 8). Checked with libass 0.17.3.
BURN_POSITIONS = {"bottom": 2, "top": 6}
# A half-transparent black box behind the text, which stays readable on any picture.
BURN_BOX_COLOUR = "&H80000000"
BURN_BORDERS = {
    "outline": ("BorderStyle=1",),
    "box": ("BorderStyle=3", f"OutlineColour={BURN_BOX_COLOUR}", f"BackColour={BURN_BOX_COLOUR}", "Outline=1", "Shadow=0"),
}
# Only these get the chosen style; an .ass file carries its author's own styles, which win.
STYLED_SUBTITLE_SUFFIXES = frozenset({".srt", ".vtt"})
# libass splits force_style at commas and equals signs and cannot escape them, so a font name may not hold them.
FONT_NAME_UNSAFE_RE = re.compile(r"[,=\\'\":;\[\]{}\x00-\x1f]")
FONT_NAME_MAX_CHARS = 64
# Fonts that show Traditional Chinese, as (file, family name), best first.
WINDOWS_CJK_FONTS = (
    ("msjh.ttc", "Microsoft JhengHei"),
    ("mingliu.ttc", "MingLiU"),
    ("msyh.ttc", "Microsoft YaHei"),
    ("simsun.ttc", "SimSun"),
)
MAC_CJK_FONTS = (
    ("/System/Library/Fonts/PingFang.ttc", "PingFang TC"),
    ("/System/Library/Fonts/STHeiti Medium.ttc", "Heiti TC"),
)
SUBTITLE_CODECS = {".mp4": "mov_text", ".m4v": "mov_text", ".mov": "mov_text", ".webm": "webvtt", ".mkv": "copy"}
TEXT_SUBTITLE_SUFFIXES = frozenset({".srt", ".vtt", ".ass", ".ssa"})
# Subtitle formats an MP4 can carry after conversion to mov_text; picture-based ones such as PGS cannot.
TEXT_SUBTITLE_CODECS = frozenset({"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"})
HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67"})
# HDR video converted to 8-bit without this looks washed out. Needs ffmpeg built with zimg, as the common builds are.
TONEMAP_FILTER = "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv"
SDR_COLOR_TAGS = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
# libx264 needs even sizes, and screen recordings or cropped videos are often odd.
EVEN_SIZE_SCALE = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
# Containers that keep a precise trim's H.264 and AAC; any other video is written as MP4.
H264_CONTAINERS = frozenset({".mp4", ".m4v", ".mov", ".mkv"})
# Hardware H.264 encoders, best first, with the options that turn a CRF-like quality number into their own scale.
# They make bigger files than libx264 at the same number, which the 「檔案稍大」 in the option's label owns up to.
HARDWARE_H264_ENCODERS: dict[str, Callable[[int], list[str]]] = {
    "h264_nvenc": lambda quality: ["-rc", "vbr", "-cq", str(quality), "-b:v", "0", "-preset", "p5"],
    "h264_qsv": lambda quality: ["-global_quality", str(quality), "-preset", "medium"],
    "h264_amf": lambda quality: ["-rc", "cqp", "-qp_i", str(quality), "-qp_p", str(quality), "-qp_b", str(quality), "-quality", "balanced"],
}
# ffmpeg lists the encoders it was built with, whether or not this machine has the hardware, so each one is tried on
# a single frame. NVENC refuses frames smaller than about 145x49.
ENCODER_TEST_INPUT = ["-f", "lavfi", "-i", "color=c=black:size=256x256:rate=1"]
# The tools page waits for detection, so a stuck driver may only hold it this long. A working NVENC takes about 1.5 s.
ENCODER_TEST_SECONDS = 8
# The tools that re-encode with a quality number and so can use the GPU. Target-size compression needs libx264's two
# passes, and a precise trim is short enough not to need it.
GPU_TOOLS = frozenset({"to_h264", "compress", "burn_subtitles", "join"})
GPU_FALLBACK_TEXT = "顯示卡加速失敗，改用處理器重新處理..."
GPU_FALLBACK_WARNING = "顯示卡加速無法使用，這次改用處理器完成。若每次都這樣，請取消勾選「使用顯示卡加速」。"
VIDEO_ONLY_TOOLS = frozenset({"to_h264", "compress", "embed_subtitles", "burn_subtitles", "frame", "gif"})
SOUND_TOOLS = frozenset({"loudnorm", "transcribe"})
SUBTITLE_TOOLS = frozenset({"embed_subtitles", "burn_subtitles"})
# ISO 639-2 codes that MP4 and MKV players understand, for the language part of names like "clip.zh-TW.srt".
LANGUAGE_CODES = {
    "zh": "chi", "en": "eng", "ja": "jpn", "ko": "kor", "es": "spa", "fr": "fre", "de": "ger", "it": "ita",
    "pt": "por", "ru": "rus", "vi": "vie", "th": "tha", "id": "ind", "ms": "may", "ar": "ara", "hi": "hin",
    "tr": "tur", "nl": "dut", "pl": "pol", "uk": "ukr", "sv": "swe", "fi": "fin", "da": "dan", "no": "nor",
    "nb": "nob", "he": "heb", "el": "gre", "cs": "cze", "hu": "hun", "ro": "rum", "fil": "fil", "tl": "tgl",
}


class ToolProgress(Protocol):
    def progress(self, value: float, text: str = "") -> None: ...


@dataclass(frozen=True)
class ToolRequest:
    tool: str
    input_path: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    output_path: Optional[Path] = None
    error: str = ""
    stopped: bool = False
    # Finished, but with a problem worth telling, such as subtitles drawn as boxes.
    warning: str = ""


@dataclass(frozen=True)
class MediaInfo:
    duration: Optional[float] = None
    has_video: bool = True
    audio_streams: int = 0
    # The highest bitrate among the audio tracks, when the file says.
    audio_kbps: Optional[int] = None
    subtitle_streams: int = 0
    text_subtitle_indexes: tuple[int, ...] = ()
    hdr: bool = False
    # The first audio track's sample rate, and the picture's upright size and frame rate.
    audio_rate: Optional[int] = None
    size: Optional[tuple[int, int]] = None
    frame_rate: str = ""


def read_media_info(path: Path) -> Optional[MediaInfo]:
    """What a tool needs to know about its source. None when ffprobe is missing, and the tools then trust ffmpeg's
    own choices."""
    probe = probe_media(path)
    if not probe:
        return None
    streams = probe.get("streams") or []
    # Cover art in an audio file shows up as a video stream too.
    videos = [stream for stream in streams if stream.get("codec_type") == "video" and not stream.get("disposition", {}).get("attached_pic")]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    subtitles = [stream for stream in streams if stream.get("codec_type") == "subtitle"]
    try:
        duration: Optional[float] = float(probe["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        duration = None
    audio_rates = [int(stream["bit_rate"]) for stream in audios if str(stream.get("bit_rate", "")).isdigit()]
    sample_rate = str(audios[0].get("sample_rate", "")) if audios else ""
    width, height = display_size(videos[0]) if videos else (None, None)
    frame_rate = str(videos[0].get("avg_frame_rate", "")) if videos else ""
    return MediaInfo(
        duration=duration,
        has_video=bool(videos),
        audio_streams=len(audios),
        audio_kbps=math.ceil(max(audio_rates) / 1000) if audio_rates else None,
        subtitle_streams=len(subtitles),
        text_subtitle_indexes=tuple(int(stream["index"]) for stream in subtitles if stream.get("codec_name") in TEXT_SUBTITLE_CODECS),
        hdr=bool(videos) and videos[0].get("color_transfer") in HDR_TRANSFERS,
        audio_rate=int(sample_rate) if sample_rate.isdigit() else None,
        size=(width, height) if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0 else None,
        frame_rate=frame_rate if FRAME_RATE_RE.fullmatch(frame_rate) else "",
    )


def reserve_output_path(path: Path) -> Path:
    """Claims a free name by creating the file. A tool must never overwrite its source or an earlier result, and two
    jobs starting at the same moment must not pick the same name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for number in range(1000):
        candidate = path if number == 0 else path.with_name(f"{path.stem} ({number}){path.suffix}")
        try:
            candidate.open("x").close()
        except FileExistsError:
            continue
        return candidate
    raise OSError(f"{path.name} 已有太多同名檔案。")


def reserve_tool_output(path: Path) -> Path:
    """Claims a free result name by creating its temporary file, which ffmpeg then writes. The library lists only
    finished files, and a crash leaves something the partial-file cleanup recognizes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for number in range(1000):
        candidate = path if number == 0 else path.with_name(f"{path.stem} ({number}){path.suffix}")
        if candidate.exists():
            continue
        try:
            partial_output_path(candidate).open("x").close()
        except FileExistsError:
            continue
        return candidate
    raise OSError(f"{path.name} 已有太多同名檔案。")


def finish_tool_output(output_path: Path) -> Path:
    final_path = output_path
    if final_path.exists():
        # Something else took the name while the tool ran.
        final_path = reserve_output_path(output_path)
    os.replace(partial_output_path(output_path), final_path)
    return final_path


@lru_cache(maxsize=4)
def has_ffmpeg_filter(ffmpeg: str, name: str) -> bool:
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return any(line.split()[1:2] == [name] for line in result.stdout.splitlines())


def tone_maps(info: Optional[MediaInfo], ffmpeg: str) -> bool:
    return bool(info and info.hdr) and has_ffmpeg_filter(ffmpeg, "zscale")


def run_quietly(command: list[str], timeout: Optional[float] = None) -> tuple[int, str]:
    """Runs a short ffmpeg check and returns its exit code and output; -1 when it could not run or took too long."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout or ENCODER_TEST_SECONDS,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""
    return result.returncode, result.stdout


def detect_hardware_encoder(ffmpeg: str, runner: Callable[[list[str]], tuple[int, str]] = run_quietly) -> str:
    """The first hardware H.264 encoder that really encodes a frame on this machine, or "" when none does."""
    code, listing = runner([ffmpeg, "-hide_banner", "-encoders"])
    if code != 0:
        return ""
    listed = {line.split()[1] for line in listing.splitlines() if len(line.split()) > 1}
    for encoder in HARDWARE_H264_ENCODERS:
        if encoder not in listed:
            continue
        test = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", *ENCODER_TEST_INPUT, "-frames:v", "1", "-pix_fmt", "yuv420p", "-c:v", encoder, "-f", "null", "-"]
        code = runner(test)[0]
        if code == 0:
            return encoder
        if code == -1:
            # A check that hung points at a stuck driver, which would only hold the page up again for the next one.
            return ""
    return ""


_hardware_encoders: dict[str, str] = {}
_hardware_detection_lock = threading.Lock()


def hardware_h264_encoder(ffmpeg: str) -> str:
    """Detected once per ffmpeg, also when nothing works, because the test encodes take a moment and the hardware does
    not change. The lock keeps pages opened together from running the checks side by side."""
    with _hardware_detection_lock:
        if ffmpeg not in _hardware_encoders:
            _hardware_encoders[ffmpeg] = detect_hardware_encoder(ffmpeg)
        return _hardware_encoders[ffmpeg]


def video_encoder(request: ToolRequest, ffmpeg: str) -> str:
    if request.params.get("gpu") and request.tool in GPU_TOOLS and not request.params.get("target_mb"):
        return hardware_h264_encoder(ffmpeg) or "libx264"
    return "libx264"


def quality_arguments(encoder: str, quality: int) -> list[str]:
    hardware = HARDWARE_H264_ENCODERS.get(encoder)
    return hardware(quality) if hardware else ["-crf", str(quality), "-preset", "medium"]


def h264_video_arguments(
    info: Optional[MediaInfo],
    ffmpeg: str,
    rate: list[str],
    extra_filters: tuple[str, ...] = (),
    scale: str = EVEN_SIZE_SCALE,
    encoder: str = "libx264",
) -> list[str]:
    """8-bit 4:2:0 H.264 with even sizes, the only kind every browser, phone and TV plays. rate holds the encoder's own
    quality or bitrate options."""
    tone_map = tone_maps(info, ffmpeg)
    filters = [*([TONEMAP_FILTER] if tone_map else []), *extra_filters, scale, "format=yuv420p"]
    # Hardware encoders otherwise choose a profile themselves; High is what libx264 makes and every player takes.
    profile = ["-profile:v", "high"] if encoder in HARDWARE_H264_ENCODERS else []
    arguments = ["-vf", ",".join(filters), "-c:v", encoder, *profile, *rate]
    return arguments + (SDR_COLOR_TAGS if tone_map else [])


def mp4_stream_maps(info: Optional[MediaInfo]) -> list[str]:
    """Keeps every audio track and the text subtitles, which ffmpeg would otherwise drop for all but one track."""
    if info is None:
        return []
    maps = ["-map", "0:V:0", "-map", "0:a?"]
    if info.text_subtitle_indexes:
        for index in info.text_subtitle_indexes:
            maps += ["-map", f"0:{index}"]
        maps += ["-c:s", "mov_text"]
    return maps


def compress_scale(height: Optional[int]) -> str:
    if not height:
        return EVEN_SIZE_SCALE
    # The quality names mean the shorter side, so a 1080p phone video stays 1080 wide. Never enlarges.
    return (
        f"scale='if(gte(iw,ih),-2,2*trunc(min({height},iw)/2))'"
        f":'if(gte(iw,ih),2*trunc(min({height},ih)/2),-2)'"
    )


def target_video_kbps(target_mb: float, info: Optional[MediaInfo]) -> tuple[int, int]:
    """The video and per-track audio bitrates that fill the target size."""
    duration = info.duration if info and info.duration else 0
    if duration <= 0:
        return 0, 0
    total_kbps = target_mb * 1024 * 1024 * 8 * TARGET_SIZE_MARGIN / duration / 1000
    audio_kbps = 128 if total_kbps > 1000 else 96 if total_kbps > 400 else 64
    tracks = info.audio_streams if info else 1
    return int(total_kbps - audio_kbps * tracks), audio_kbps


def minimum_target_mb(info: MediaInfo) -> float:
    duration = info.duration or 0
    kbps = MIN_TARGET_VIDEO_KBPS + 64 * info.audio_streams
    # Rounded up to a tenth, so the size named is always enough.
    return math.ceil(kbps * 1000 * duration / 8 / TARGET_SIZE_MARGIN / 1024 / 1024 * 10) / 10


def subtitle_language(subtitle_path: str) -> tuple[str, str]:
    """The language part of "clip.zh-TW.srt", as a display title and an ISO 639-2 code."""
    stem = Path(subtitle_path).stem
    _, dot, segment = stem.rpartition(".")
    if not dot or not LANGUAGE_SEGMENT_RE.match(segment):
        return "", ""
    base = segment.split("-")[0].lower()
    return segment, LANGUAGE_CODES.get(base, base if len(base) == 3 else "")


def working_subtitle_name(request: ToolRequest) -> str:
    # The extension is the one part of the real name that reaches the filter graph, where "clip.s'rt" would end the
    # value early. Any other extension is dropped, and ffmpeg then recognizes the subtitles by their content.
    suffix = Path(str(request.params["subtitle_path"])).suffix.lower()
    return f"{BURN_SUBTITLE_NAME}{suffix if SAFE_SUBTITLE_SUFFIX_RE.fullmatch(suffix) else ''}"


@lru_cache(maxsize=1)
def default_cjk_font() -> str:
    """The family name of an installed font that shows Traditional Chinese, or "" when none is found, and libass then
    picks one itself."""
    if sys.platform == "win32":
        folders = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"]
        if os.environ.get("LOCALAPPDATA"):
            folders.append(Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "Windows" / "Fonts")
        return next((family for file, family in WINDOWS_CJK_FONTS if any((folder / file).is_file() for folder in folders)), "")
    if sys.platform == "darwin":
        return next((family for file, family in MAC_CJK_FONTS if Path(file).is_file()), "")
    try:
        result = subprocess.run(
            ["fc-list", ":lang=zh-tw", "family"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    # Each line lists one font's names, such as "Noto Sans CJK TC,Noto Sans CJK TC Regular".
    families = sorted({line.split(",")[0].strip() for line in result.stdout.splitlines() if line.strip()})
    return next((family for family in families if family.endswith((" TC", " TW"))), families[0] if families else "")


def clean_font_name(name: Any) -> str:
    return FONT_NAME_UNSAFE_RE.sub("", str(name or "")).strip()[:FONT_NAME_MAX_CHARS]


def burn_force_style(params: dict[str, Any]) -> str:
    """The ASS style overrides for the chosen look, or "" when nothing was chosen, which keeps libass's defaults."""
    entries: list[str] = []
    font = clean_font_name(params.get("font_name"))
    if font:
        entries.append(f"FontName={font}")
    if params.get("font_size"):
        low, high = BURN_FONT_SIZE_RANGE
        entries.append(f"FontSize={min(max(int(params['font_size']), low), high)}")
    if params.get("position") in BURN_POSITIONS:
        entries.append(f"Alignment={BURN_POSITIONS[params['position']]}")
    entries += BURN_BORDERS.get(str(params.get("border")), ())
    return ",".join(entries)


def escape_filter_value(value: str) -> str:
    """ffmpeg reads a filter option twice: as the filter's own options, where ":" ends a value, and before that as part
    of the filter graph, where "," and ";" end the filter. Each reading takes away one level of backslashes."""
    option_level = re.sub(r"([\\':])", r"\\\1", value)
    return re.sub(r"([\\'\[\],;])", r"\\\1", option_level)


def burn_subtitle_filter(request: ToolRequest) -> str:
    name = working_subtitle_name(request)
    style = burn_force_style(request.params) if Path(name).suffix in STYLED_SUBTITLE_SUFFIXES else ""
    return f"subtitles={name}:force_style={escape_filter_value(style)}" if style else f"subtitles={name}"


def build_ffmpeg_command(
    request: ToolRequest, output_path: Path, ffmpeg: str, info: Optional[MediaInfo] = None, inputs: Sequence[MediaInfo] = ()
) -> list[str]:
    """Builds the command as an argument list. File names come from YouTube titles, so they must never reach a shell.
    inputs holds what is known about each file a join takes."""
    params = request.params
    source = request.input_path
    base = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-progress", "pipe:1"]
    # ffmpeg writes to a temporary name next to the result, which keeps the extension that picks the format.
    output = [str(partial_output_path(output_path))]

    if request.tool == "join":
        return build_join_command(request, output_path, ffmpeg, inputs)

    if request.tool == "loudnorm":
        encoder = LOUDNORM_ENCODERS.get(output_path.suffix.lower(), AUDIO_ENCODERS[".m4a"])
        loudness = f"loudnorm=I={int(params.get('loudness', -16))}:TP={LOUDNORM_TRUE_PEAK}:LRA={LOUDNORM_RANGE}"
        # loudnorm works at 192 kHz, so the sound goes back to the source's rate. Opus only takes 48 kHz.
        rate = info.audio_rate if info and info.audio_rate and "libopus" not in encoder else 48000
        # Everything else, the picture, a song's cover and the subtitles, is copied as it is.
        return [*base, "-i", source, "-map", "0", "-map", "-0:d", "-c", "copy", "-af", loudness, *encoder, "-ar", str(rate), *output]

    if request.tool == "gif":
        filters = f"[0:V:0]{gif_filters(request, info, ffmpeg)}[frames];[frames][1:v]paletteuse"
        return [*base, *gif_window(request), "-i", source, "-i", GIF_PALETTE_NAME, "-filter_complex", filters, "-loop", "0", *output]

    if request.tool == "trim":
        start = params.get("start")
        end = params.get("end")
        if params.get("precise"):
            # Seeking before -i is exact when re-encoding. The output-side -t limits every stream to the clip.
            window = ["-ss", str(start)] if start else []
            length = ["-t", str(float(end) - float(start or 0))] if end is not None else []
            if info is not None and not info.has_video:
                encoder = AUDIO_ENCODERS.get(output_path.suffix.lower(), AUDIO_ENCODERS[".m4a"])
                return [*base, *window, "-i", source, *length, "-map", "0:a:0", *encoder, *output]
            # Subtitle tracks ignore the seek and would run on to the end of the source, so a precise clip has none.
            maps = [] if info is None else ["-map", "0:V:0", "-map", "0:a?"]
            video = h264_video_arguments(info, ffmpeg, quality_arguments("libx264", 18))
            return [*base, *window, "-i", source, *length, *maps, *video, "-c:a", "aac", "-b:a", "192k", "-sn", *output]
        # Before -i, because seeking in the input keeps the keyframe ahead of the cut. Seeking in the output
        # drops all video when the clip holds no keyframe of its own.
        window = ["-ss", str(start)] if start else []
        if end is not None:
            window += ["-to", str(end)]
        # Stream copy is instant but can only start at a keyframe. The -map keeps subtitle tracks with the clip
        # and drops the chapter data track, which would keep the source's length.
        return [*base, *window, "-i", source, "-map", "0", "-map", "-0:d", "-c", "copy", *output]

    encoder = video_encoder(request, ffmpeg)
    if request.tool == "to_h264":
        video = h264_video_arguments(info, ffmpeg, quality_arguments(encoder, 20), encoder=encoder)
        return [*base, "-i", source, *mp4_stream_maps(info), *video, "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", *output]

    if request.tool == "compress":
        scale = compress_scale(params.get("height"))
        # Re-encoding small audio at a higher rate only makes the file bigger.
        audio_kbps = max(min(128, info.audio_kbps or 128), 32) if info else 128
        if params.get("target_mb"):
            video_kbps, audio_kbps = target_video_kbps(float(params["target_mb"]), info)
            rate = [*two_pass_rate(video_kbps), "-pass", "2", "-preset", "medium"]
        else:
            rate = quality_arguments(encoder, int(params.get("crf", 28)))
        video = h264_video_arguments(info, ffmpeg, rate, scale=scale, encoder=encoder)
        return [*base, "-i", source, *mp4_stream_maps(info), *video, "-c:a", "aac", "-b:a", f"{audio_kbps}k", "-movflags", "+faststart", *output]

    if request.tool == "extract_audio":
        audio_format = str(params.get("format", "mp3"))
        arguments = [*base, "-i", source, "-vn", "-c:a", AUDIO_CODECS[audio_format]]
        if audio_format in {"mp3", "m4a", "opus"}:
            arguments += ["-b:a", f"{int(params.get('bitrate', 192))}k"]
        return [*arguments, *output]

    if request.tool == "embed_subtitles":
        codec = SUBTITLE_CODECS.get(output_path.suffix.lower(), "copy")
        # The source's own subtitle tracks are copied as they are; only the new one, after them, is converted.
        # Without ffprobe the count is unknown, so every track is converted as before.
        new_track = info.subtitle_streams if info else 0
        codec_option = f"-c:s:{new_track}" if info else "-c:s"
        title, language = subtitle_language(str(params["subtitle_path"]))
        metadata = [f"-metadata:s:s:{new_track}", f"title={title}"] if title else []
        metadata += [f"-metadata:s:s:{new_track}", f"language={language}"] if language else []
        return [
            *base, "-i", source, "-i", working_subtitle_name(request), "-map", "0", "-map", "-0:d", "-map", "1:0",
            "-c", "copy", codec_option, codec, *metadata, *output,
        ]

    if request.tool == "burn_subtitles":
        video = h264_video_arguments(info, ffmpeg, quality_arguments(encoder, 20), extra_filters=(burn_subtitle_filter(request),), encoder=encoder)
        # libass reports missing fonts as warnings, which "-loglevel error" would hide.
        base[base.index("-loglevel") + 1] = "level+warning"
        return [*base, "-i", source, *mp4_stream_maps(info), *video, "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", *output]

    if request.tool == "frame":
        tone_map = ["-vf", TONEMAP_FILTER] if tone_maps(info, ffmpeg) else []
        maps = ["-map", "0:V:0"] if info is not None else []
        # -update 1 writes one image, so a "%d" in a title is not read as a numbered sequence.
        return [*base, "-ss", str(params.get("at", 0)), "-i", source, *maps, *tone_map, "-frames:v", "1", "-q:v", "2", "-update", "1", *output]

    raise ValueError(f"未知的工具：{request.tool}")


def gif_window(request: ToolRequest) -> list[str]:
    start = float(request.params.get("start") or 0)
    # Before -i, so only the chosen part is read, and decoding from the keyframe before it keeps the start exact.
    return ["-ss", str(start), "-t", str(float(request.params["end"]) - start)]


def gif_filters(request: ToolRequest, info: Optional[MediaInfo], ffmpeg: str) -> str:
    width = int(request.params.get("width") or GIF_WIDTHS[1])
    fps = int(request.params.get("fps") or GIF_FRAME_RATES[1])
    tone_map = [TONEMAP_FILTER] if tone_maps(info, ffmpeg) else []
    # Never wider than the video itself: enlarging only makes the file bigger.
    return ",".join([*tone_map, f"fps={fps}", f"scale='min({width},iw)':-1:flags=lanczos"])


def build_palette_command(request: ToolRequest, ffmpeg: str, info: Optional[MediaInfo]) -> list[str]:
    """A GIF holds 256 colours. The first pass picks the ones these frames need, which the second then uses."""
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-progress", "pipe:1", *gif_window(request),
        "-i", request.input_path, "-map", "0:V:0", "-vf", f"{gif_filters(request, info, ffmpeg)},palettegen", "-update", "1", GIF_PALETTE_NAME,
    ]


def join_paths(request: ToolRequest) -> list[str]:
    return [str(path) for path in request.params.get("paths") or []]


def build_join_command(request: ToolRequest, output_path: Path, ffmpeg: str, inputs: Sequence[MediaInfo]) -> list[str]:
    """Joins videos one after another. Each part is decoded and fitted into the first one's frame, with black bars
    where its shape differs, so any mix of codecs and sizes joins. The graph names the files only by number."""
    width, height = inputs[0].size or (1280, 720)
    # libx264 needs even sizes.
    width, height = max(width // 2 * 2, 2), max(height // 2 * 2, 2)
    frame_rate = f",fps={inputs[0].frame_rate}" if inputs[0].frame_rate else ""
    with_sound = any(info.audio_streams for info in inputs)
    chains: list[str] = []
    links: list[str] = []
    for index, info in enumerate(inputs):
        tone_map = f"{TONEMAP_FILTER}," if tone_maps(info, ffmpeg) else ""
        chains.append(
            f"[{index}:V:0]{tone_map}scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1{frame_rate},format=yuv420p[v{index}]"
        )
        links.append(f"[v{index}]")
        if with_sound:
            # Silence as long as a part without sound, so the sound of the parts after it stays in step.
            sound = f"[{index}:a:0]{JOIN_AUDIO_FORMAT}" if info.audio_streams else f"{JOIN_SILENCE},atrim=duration={info.duration}"
            chains.append(f"{sound}[a{index}]")
            links.append(f"[a{index}]")
    chains.append(f"{''.join(links)}concat=n={len(inputs)}:v=1:a={int(with_sound)}[v]{'[a]' if with_sound else ''}")
    encoder = video_encoder(request, ffmpeg)
    profile = ["-profile:v", "high"] if encoder in HARDWARE_H264_ENCODERS else []
    sound_output = ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"] if with_sound else []
    colors = SDR_COLOR_TAGS if any(tone_maps(info, ffmpeg) for info in inputs) else []
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-progress", "pipe:1",
        *(part for path in join_paths(request) for part in ("-i", path)),
        "-filter_complex", ";".join(chains), "-map", "[v]", "-c:v", encoder, *profile, *quality_arguments(encoder, 20), *colors,
        *sound_output, "-movflags", "+faststart", str(partial_output_path(output_path)),
    ]


def two_pass_rate(video_kbps: int) -> list[str]:
    return ["-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps * 2}k", "-bufsize", f"{video_kbps * 2}k"]


def build_first_pass_command(request: ToolRequest, ffmpeg: str, info: Optional[MediaInfo], video_kbps: int) -> list[str]:
    """Target-size compression first measures the whole video, so the second pass spends bits where they are needed."""
    rate = [*two_pass_rate(video_kbps), "-pass", "1", "-preset", "medium"]
    video = h264_video_arguments(info, ffmpeg, rate, scale=compress_scale(request.params.get("height")))
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-progress", "pipe:1",
        "-i", request.input_path, "-map", "0:V:0", *video, "-an", "-sn", "-f", "null", "-",
    ]


def output_name(request: ToolRequest, info: Optional[MediaInfo] = None) -> Path:
    source = Path(request.input_path)
    params = request.params
    trim_suffix = source.suffix
    if request.tool == "trim" and params.get("precise") and (info is None or info.has_video) and trim_suffix.lower() not in H264_CONTAINERS:
        trim_suffix = ".mp4"
    # AVI, FLV and TS cannot hold a subtitle track that players switch on and off.
    embed_suffix = source.suffix if source.suffix.lower() in SUBTITLE_CODECS else ".mkv"
    names = {
        "trim": f"{source.stem}_片段{trim_suffix}",
        "to_h264": f"{source.stem}_h264.mp4",
        "compress": f"{source.stem}_壓縮.mp4",
        "extract_audio": f"{source.stem}.{params.get('format', 'mp3')}",
        "embed_subtitles": f"{source.stem}_含字幕{embed_suffix}",
        "burn_subtitles": f"{source.stem}_燒錄字幕.mp4",
        "frame": f"{source.stem}_畫面.jpg",
        "loudnorm": f"{source.stem}_音量標準化{source.suffix}",
        "join": f"{source.stem}_合併.mp4",
        "gif": f"{source.stem}.gif",
    }
    return source.with_name(names[request.tool])


def default_output_path(request: ToolRequest, info: Optional[MediaInfo] = None) -> Path:
    return reserve_tool_output(output_name(request, info))


def expected_duration(request: ToolRequest, info: Optional[MediaInfo], inputs: Sequence[MediaInfo] = ()) -> Optional[float]:
    if request.tool == "frame":
        return None
    if request.tool == "join":
        durations = [part.duration or 0 for part in inputs]
        return sum(durations) if all(durations) else None
    duration = info.duration if info else None
    if duration is None or request.tool not in ("trim", "gif"):
        return duration
    start = float(request.params.get("start") or 0)
    end = request.params.get("end")
    return max(min(float(end), duration) - start, 0.1) if end is not None else max(duration - start, 0.1)


def check_join(request: ToolRequest, inputs: Sequence[Optional[MediaInfo]]) -> str:
    paths = [Path(path) for path in join_paths(request)]
    if len(paths) < 2:
        return "請至少選兩個檔案來合併。"
    for path, info in zip(paths, inputs):
        if not path.is_file():
            return f"找不到「{path.name}」，可能已被移動或刪除。"
        if info is None:
            return f"無法讀取「{path.name}」的影片資訊，請確認 ffprobe 已安裝，或檔案沒有損壞。"
        if not info.has_video:
            return f"「{path.name}」沒有影像，{TOOL_LABELS['join']}只能用在影片上。"
    # A part without sound is filled with silence of its own length, which must be known.
    if any(info and info.audio_streams for info in inputs) and any(info and not info.audio_streams and not info.duration for info in inputs):
        return "無法得知沒有聲音的那段影片有多長，請先用「轉成 H.264 MP4」轉檔後再合併。"
    return ""


def check_request(request: ToolRequest, info: Optional[MediaInfo], inputs: Sequence[Optional[MediaInfo]] = ()) -> str:
    """Problems that ffmpeg would only answer with an empty file or a cryptic message."""
    params = request.params
    if request.tool == "join":
        return check_join(request, inputs)
    if request.tool == "gif":
        start, end = float(params.get("start") or 0), params.get("end")
        if end is None:
            return "請填入結束時間。"
        if float(end) <= start:
            return "結束時間要晚於開始時間。"
        if float(end) - start > GIF_MAX_SECONDS:
            return f"GIF 最長 {GIF_MAX_SECONDS} 秒，請縮短時間範圍。"
    if request.tool in SUBTITLE_TOOLS and not Path(str(params.get("subtitle_path", ""))).is_file():
        return "找不到字幕檔，可能已被移動或刪除。"
    target_mb = float(params.get("target_mb") or 0) if request.tool == "compress" else 0
    if target_mb and not (info and info.duration):
        return "無法得知影片長度，請改用壓縮程度。"
    if info is None:
        return ""
    if request.tool in VIDEO_ONLY_TOOLS and not info.has_video:
        return f"「{Path(request.input_path).name}」沒有影像，{TOOL_LABELS[request.tool]}只能用在影片上。"
    if request.tool in SOUND_TOOLS and not info.audio_streams:
        return f"「{Path(request.input_path).name}」沒有聲音，{TOOL_LABELS[request.tool]}只能用在有聲音的檔案上。"
    moment_key = {"trim": "start", "gif": "start", "frame": "at"}.get(request.tool)
    if info.duration and moment_key and float(params.get(moment_key) or 0) >= info.duration:
        label = "時間點" if request.tool == "frame" else "開始時間"
        return f"{label}超過了影片長度（{format_timestamp_short(info.duration)}）。"
    if target_mb and not already_small_enough(request) and target_video_kbps(target_mb, info)[0] < MIN_TARGET_VIDEO_KBPS:
        return f"目標大小太小，這部影片至少要 {minimum_target_mb(info):g} MB 才看得清楚。可以只剪出需要的片段再壓縮。"
    return ""


def already_small_enough(request: ToolRequest) -> str:
    """Why a compression to a target size has nothing to do, or ""."""
    target_mb = float(request.params.get("target_mb") or 0) if request.tool == "compress" else 0
    if target_mb and target_mb * 1024 * 1024 >= Path(request.input_path).stat().st_size:
        return f"原檔已經小於 {target_mb:g} MB，不需要壓縮。"
    return ""


def resolve_request(request: ToolRequest) -> ToolRequest:
    # ffmpeg runs in a scratch folder, so a relative path would point somewhere else.
    params = dict(request.params)
    if params.get("subtitle_path"):
        params["subtitle_path"] = str(Path(str(params["subtitle_path"])).expanduser().resolve())
    if params.get("paths"):
        params["paths"] = [str(Path(str(path)).expanduser().resolve()) for path in params["paths"]]
    return replace(request, input_path=str(Path(request.input_path).expanduser().resolve()), params=params)


def run_tool(
    request: ToolRequest,
    progress_ui: ToolProgress,
    should_stop: Optional[Callable[[], bool]] = None,
) -> ToolResult:
    available, ffmpeg = get_ffmpeg_status()
    if not available:
        return ToolResult(error="這項工具需要 ffmpeg，請先安裝。")
    if not Path(request.input_path).expanduser().is_file():
        return ToolResult(error="找不到來源檔案。")
    request = resolve_request(request)
    info = read_media_info(Path(request.input_path))
    probed = [read_media_info(Path(path)) for path in join_paths(request)] if request.tool == "join" else []
    problem = check_request(request, info, probed)
    if problem:
        return ToolResult(error=problem)
    if request.tool == "transcribe":
        # Imported here because speech builds on this module. Whisper's text is held in memory and written at the end.
        from videocap.speech import transcribe

        return transcribe(request, ffmpeg, progress_ui, should_stop)
    # Checked above: every part of a join is a video ffprobe could read.
    inputs = [part for part in probed if part is not None]
    note = already_small_enough(request)
    if note:
        # Not a failure, whose retry could only fail again: the original already is what was asked for.
        return ToolResult(output_path=Path(request.input_path), warning=note)

    try:
        output_path = default_output_path(request, info)
    except OSError as exc:
        return ToolResult(error=f"無法建立結果檔：{exc}")
    work_path = partial_output_path(output_path)
    label = TOOL_LABELS.get(request.tool, request.tool)
    progress_ui.progress(0.0, text=f"{label}...")
    try:
        # A killed ffmpeg may still hold a work file for a moment, so the folder is removed again below.
        with tempfile.TemporaryDirectory(prefix="youtube-downloader-tool-", ignore_cleanup_errors=True) as workdir:
            if request.tool in SUBTITLE_TOOLS:
                copy_subtitle_as_utf8(Path(str(request.params["subtitle_path"])), Path(workdir) / working_subtitle_name(request))
            result = run_tool_commands(request, output_path, ffmpeg, info, workdir, progress_ui, should_stop, inputs)
        remove_when_released(Path(workdir))
        if result.output_path:
            result.output_path = finish_tool_output(result.output_path)
        else:
            remove_when_released(work_path)
        return result
    except OSError as exc:
        remove_when_released(work_path)
        return ToolResult(error=f"{label}失敗：{exc}")


def copy_subtitle_as_utf8(source: Path, target: Path) -> None:
    """ffmpeg only reads UTF-8 subtitles, and Traditional Chinese ones are often Big5."""
    if source.suffix.lower() in TEXT_SUBTITLE_SUFFIXES:
        target.write_text(read_text_file(source), encoding="utf-8")
    else:
        target.write_bytes(source.read_bytes())


def run_tool_commands(
    request: ToolRequest,
    output_path: Path,
    ffmpeg: str,
    info: Optional[MediaInfo],
    workdir: str,
    progress_ui: ToolProgress,
    should_stop: Optional[Callable[[], bool]],
    inputs: Sequence[MediaInfo] = (),
) -> ToolResult:
    label = TOOL_LABELS.get(request.tool, request.tool)
    duration = expected_duration(request, info, inputs)
    work_path = partial_output_path(output_path)
    target_mb = float(request.params.get("target_mb") or 0) if request.tool == "compress" else 0

    if target_mb:
        video_kbps = target_video_kbps(target_mb, info)[0]
        first_pass = build_first_pass_command(request, ffmpeg, info, video_kbps)
        result = _run_ffmpeg(first_pass, duration, label, workdir, progress_ui, should_stop, (0.0, 0.5))
        if result.error or result.stopped:
            return result
        command = build_ffmpeg_command(request, output_path, ffmpeg, info)
        result = _run_ffmpeg(command, duration, label, workdir, progress_ui, should_stop, (0.5, 1.0))
        target_bytes = target_mb * 1024 * 1024
        if not result.error and not result.stopped and work_path.stat().st_size > target_bytes:
            # Two-pass usually lands within a few percent; one more go at a proportionally lower rate fixes the rest.
            lower = max(int(video_kbps * target_bytes / work_path.stat().st_size * 0.95), MIN_TARGET_VIDEO_KBPS)
            command = [*command]
            command[command.index("-b:v") + 1] = f"{lower}k"
            result = _run_ffmpeg(command, duration, label, workdir, progress_ui, should_stop, (0.5, 1.0))
            if not result.error and not result.stopped and work_path.stat().st_size > target_bytes:
                return ToolResult(error=f"壓縮後仍有 {format_bytes(work_path.stat().st_size)}，超過目標大小。請把目標調大一些，或降低解析度。")
    elif request.tool == "gif":
        result = _run_ffmpeg(build_palette_command(request, ffmpeg, info), duration, label, workdir, progress_ui, should_stop, (0.0, 0.5))
        if result.error or result.stopped:
            return result
        # The palette comes out as one picture at the very end, so ffmpeg's own progress never got this far.
        progress_ui.progress(0.5, text=f"{label} 50%")
        command = build_ffmpeg_command(request, output_path, ffmpeg, info)
        result = _run_ffmpeg(command, duration, label, workdir, progress_ui, should_stop, (0.5, 1.0))
    else:
        command = build_ffmpeg_command(request, output_path, ffmpeg, info, inputs)
        result = _run_ffmpeg(command, duration, label, workdir, progress_ui, should_stop, (0.0, 1.0))
        if result.error and video_encoder(request, ffmpeg) != "libx264":
            # An old driver, a size the chip cannot take or a GPU in use elsewhere only shows once encoding starts.
            progress_ui.progress(0.0, text=GPU_FALLBACK_TEXT)
            request = replace(request, params={**request.params, "gpu": False})
            command = build_ffmpeg_command(request, output_path, ffmpeg, info, inputs)
            result = _run_ffmpeg(command, duration, label, workdir, progress_ui, should_stop, (0.0, 1.0))
            if not result.error and not result.stopped:
                result.warning = " ".join(filter(None, [GPU_FALLBACK_WARNING, result.warning]))
    if result.error or result.stopped:
        return result

    problem = check_output(request, work_path)
    if problem:
        return ToolResult(error=problem)
    progress_ui.progress(1.0, text=f"{label}完成，但{result.warning}" if result.warning else f"{label}完成")
    return ToolResult(output_path=output_path, warning=result.warning)


def check_output(request: ToolRequest, work_path: Path) -> str:
    """ffmpeg reports success even when it wrote nothing, such as a frame past the end of the video."""
    size = work_path.stat().st_size
    probe = probe_media(work_path) if size else None
    if not size or (probe is not None and not probe.get("streams")):
        return "沒有產生任何內容：指定的時間可能超過了影片長度。"
    if request.tool == "compress" and not request.params.get("target_mb"):
        source_size = Path(request.input_path).stat().st_size
        if size >= source_size:
            return (
                f"壓縮後（{format_bytes(size)}）沒有比原檔（{format_bytes(source_size)}）小，所以沒有保留。"
                "原檔可能已經很精簡，可以改選「重度」、降低解析度，或指定目標大小。"
            )
    return ""


def summarize_ffmpeg_error(lines: list[str]) -> str:
    causes = [FFMPEG_LINE_PREFIX_RE.sub("", line) for line in lines if not FFMPEG_NOISE_RE.search(line)]
    detail = " ".join(causes[:3] or lines[-3:])[:300]
    hint = next((hint for marker, hint in FFMPEG_HINTS if marker in " ".join(lines)), "")
    return f"ffmpeg 處理失敗：{hint}（{detail}）" if hint else f"ffmpeg 處理失敗：{detail}"


def _run_ffmpeg(
    command: list[str],
    duration: Optional[float],
    label: str,
    workdir: str,
    progress_ui: ToolProgress,
    should_stop: Optional[Callable[[], bool]],
    span: tuple[float, float],
) -> ToolResult:
    # Tied to the app, so ffmpeg never keeps encoding after the app is killed.
    process = start_tied_process(
        command,
        cwd=workdir,
        stdout=subprocess.PIPE,
        # One pipe for both, because a full stderr pipe nobody reads would make ffmpeg hang.
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    assert process.stdout is not None

    stopped = False
    problems: list[str] = []
    missing_glyphs = False
    for line in process.stdout:
        if should_stop and should_stop():
            stopped = True
            end_process_tree(process)
            break
        match = PROGRESS_TIME_RE.match(line.strip())
        if match:
            if duration:
                # ffmpeg reports out_time_ms in microseconds, despite the name.
                fraction = span[0] + (span[1] - span[0]) * min(int(match.group(1)) / 1_000_000 / duration, 1.0)
                progress_ui.progress(fraction, text=f"{label} {fraction * 100:.0f}%")
        elif line.strip() and not PROGRESS_LINE_RE.match(line.strip()):
            level = FFMPEG_LEVEL_TAG_RE.match(line.strip())
            if level and level.group(2) == "warning":
                missing_glyphs = missing_glyphs or MISSING_GLYPH_MARKER in line
            elif len(problems) < 40:
                problems.append(FFMPEG_LEVEL_TAG_RE.sub(r"\1", line.strip(), count=1))

    process.wait()
    if stopped:
        return ToolResult(stopped=True)
    if process.returncode != 0:
        return ToolResult(error=summarize_ffmpeg_error(problems))
    return ToolResult(warning=MISSING_FONT_WARNING if missing_glyphs else "")

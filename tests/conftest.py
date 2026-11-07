from __future__ import annotations

import shutil
import os
import functools
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterator

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

has_ffmpeg = shutil.which("ffmpeg") is not None
needs_ffmpeg = pytest.mark.skipif(not has_ffmpeg, reason="ffmpeg is not installed")


@pytest.fixture(autouse=True)
def isolated_engine(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The engine purges old download and cookie folders in the temp folder, so no test may use the real one. The
    engine is also shared by the whole process, so each test starts with a new one."""
    import videocap.services as services_module

    temp_dir = tmp_path_factory.mktemp("temp")
    monkeypatch.setattr(tempfile, "tempdir", str(temp_dir))
    monkeypatch.setattr(services_module, "CLOUD_OUTPUT_ROOT", temp_dir / "youtube-downloader-files")
    yield
    services_module.reset_shared_services()


@pytest.fixture(autouse=True)
def no_real_recycle_bin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests delete only their own temp files, which the library already removes for good; this keeps the real
    recycle bin out of it even when pytest's temp folder is moved somewhere else."""
    import videocap.library as library

    monkeypatch.setattr(library, "send_to_trash", None, raising=False)


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real 6-second video with sound, generated once for the whole test run."""
    if not has_ffmpeg:
        pytest.skip("ffmpeg is not installed")
    path = tmp_path_factory.mktemp("media") / "sample video's [test].mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=6:size=320x240:rate=15",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
    )
    return path


@pytest.fixture(scope="session")
def sample_subtitle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("subs") / "sample's sub, [zh].srt"
    path.write_text(
        "1\n00:00:00,500 --> 00:00:02,000\n第一行字幕\n\n2\n00:00:02,500 --> 00:00:05,000\nsecond line\n",
        encoding="utf-8",
    )
    return path


@functools.lru_cache(maxsize=1)
def shortcuts_can_be_saved() -> bool:
    """Whether this Windows session can save a .lnk through WScript.Shell. GitHub's Windows runners answer "Value does
    not fall within the expected range" to every Save, so the autostart tests are skipped there."""
    if sys.platform != "win32":
        return False
    with tempfile.TemporaryDirectory(prefix="vc-lnk-") as folder:
        target = Path(folder) / "probe.lnk"
        script = (
            "$ErrorActionPreference = 'Stop'; $s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:VIDEOCAP_PROBE); "
            "$s.TargetPath = $env:SystemRoot + '\\notepad.exe'; $s.Save()"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                env={**os.environ, "VIDEOCAP_PROBE": str(target)},
                capture_output=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and target.exists()


needs_shortcuts = pytest.mark.skipif(not shortcuts_can_be_saved(), reason="this Windows session cannot save shortcuts")

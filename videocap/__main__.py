"""Starts the app with its download engine already running: python -m videocap [Streamlit options].

`streamlit run main.py` only builds the engine when the first page is opened, so after a restart nobody's
subscriptions are checked and unfinished jobs are not restored until someone visits. Building it here first,
in the same process Streamlit then runs the pages in, makes them start with the app.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Optional

MAIN_SCRIPT = Path(__file__).resolve().parents[1] / "main.py"


def streamlit_arguments(extra: list[str]) -> list[str]:
    return ["streamlit", "run", str(MAIN_SCRIPT), *extra]


def secrets_files() -> list[Path]:
    from streamlit import config

    return [Path(name) for name in config.get_option("secrets.files")]


def unreadable_secrets_file() -> Optional[Path]:
    """Streamlit reads its secrets files as UTF-8 while the server starts, and dies with a traceback on any other
    encoding, before the app could explain. Windows editors also save UTF-16 or Big5."""
    for path in secrets_files():
        if not path.is_file():
            continue
        try:
            path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            return path
    return None


def detect_gpu() -> None:
    from videocap.media_tools import hardware_h264_encoder
    from videocap.tooling import get_ffmpeg_status

    available, ffmpeg = get_ffmpeg_status()
    if available:
        hardware_h264_encoder(ffmpeg)


def warm_up_gpu_detection() -> None:
    # The 工具 page asks whether the GPU encodes, which takes a test encode or two; asked now, its first visit need not wait.
    threading.Thread(target=detect_gpu, name="videocap-gpu-check", daemon=True).start()


def main() -> None:
    from streamlit.web import cli

    from videocap.environment import is_streamlit_cloud_environment
    from videocap.services import get_shared_services

    unreadable = unreadable_secrets_file()
    if unreadable:
        print(
            f"無法啟動：{unreadable} 不是 UTF-8 編碼的檔案。Streamlit 只讀得懂 UTF-8，"
            "請用記事本開啟它，「另存新檔」時把編碼選成「UTF-8」，再重新執行。",
            file=sys.stderr,
        )
        sys.exit(1)
    is_public = is_streamlit_cloud_environment()
    get_shared_services(is_public)
    if not is_public:
        warm_up_gpu_detection()
    sys.argv = streamlit_arguments(sys.argv[1:])
    sys.exit(cli.main())


if __name__ == "__main__":
    main()

"""YouTube影片下載平台：下載引擎、背景佇列與 Streamlit 介面。"""

from __future__ import annotations

import sys
from pathlib import Path

from videocap.config import get_data_dir


# In the data folder while 模擬瀏覽器 is switched off, by this machine's owner or by the app's own check.
IMPERSONATION_SWITCH_NAME = "impersonation-off"


def impersonation_switch() -> Path:
    return get_data_dir() / IMPERSONATION_SWITCH_NAME


def keep_curl_cffi_out() -> None:
    """While 模擬瀏覽器 is switched off, yt-dlp must not find the curl-cffi package at all. yt-dlp counts a package it
    cannot import as not installed, and Python refuses to import a module whose entry here is None. This runs before
    anything imports yt-dlp and leans on nothing inside yt-dlp, so it holds even when videocap.impersonation's own way
    of keeping the package from YouTube no longer does."""
    try:
        switched_off = impersonation_switch().exists()
    except OSError:
        switched_off = False
    if switched_off and "curl_cffi" not in sys.modules:
        sys.modules["curl_cffi"] = None  # type: ignore[assignment]


keep_curl_cffi_out()

"""模擬瀏覽器 without the curl-cffi package: a handler that takes curl-cffi's place in yt-dlp, makes an ordinary
connection, and says it is a browser with the header a real Chrome sends. Where the package is installed, the same
tests run a second time with yt-dlp's real handler."""

from __future__ import annotations

import os
from typing import Any

import pytest
from yt_dlp.networking._urllib import UrllibRH
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.networking.impersonate import ImpersonateRequestHandler, ImpersonateTarget

import videocap.impersonation as impersonation
from offline_web import BROWSER_HEADER


# Set where curl-cffi was installed for the run, as in the CI step for it: its tests then fail instead of being skipped.
EXPECT_CURL_CFFI = bool(os.environ.get("VIDEOCAP_EXPECT_CURL_CFFI"))
STAND_IN = "stand-in"
CURL_CFFI = "curl-cffi"
# What is really installed. conftest.py hides it from every test that does not ask for the package.
INSTALLED_VERSION = impersonation.installed_version


def stand_in_handler() -> Any:
    class StandInBrowserRH(ImpersonateRequestHandler, UrllibRH):
        _SUPPORTED_IMPERSONATE_TARGET_MAP = {ImpersonateTarget("chrome", "999", "windows", "10"): "chrome999"}

        def _check_extensions(self, extensions: dict[str, Any]) -> None:
            super()._check_extensions(extensions)
            extensions.pop("impersonate", None)

        def send(self, request: Any) -> Any:
            # As yt-dlp's curl-cffi handler does: the answer says which browser the request passed for.
            target = self._get_request_target(request)
            try:
                response = super().send(request)
            except HTTPError as error:
                error.response.extensions["impersonate"] = target
                raise
            response.extensions["impersonate"] = target
            return response

        def _send(self, request: Any) -> Any:
            if self._get_request_target(request) is not None:
                request = request.copy()
                request.headers[BROWSER_HEADER] = '"Chromium";v="999"'
            return super()._send(request)

    return StandInBrowserRH


def recording(handler: Any) -> Any:
    """The handler, keeping count of how often it was built and of every address it was sent to."""

    class RecordingRH(handler):  # type: ignore[misc, valid-type]
        RH_KEY = handler.RH_KEY
        RH_NAME = handler.RH_NAME
        built = 0
        sent: list[str] = []

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            RecordingRH.built += 1

        def _send(self, request: Any) -> Any:
            RecordingRH.sent.append(request.url)
            return super()._send(request)

    RecordingRH.sent = []
    return RecordingRH


def real_handler(monkeypatch: pytest.MonkeyPatch) -> Any:
    """yt-dlp's own handler for curl-cffi, which videocap.impersonation took out of yt-dlp's list, with the package
    no longer hidden from the test. Skips the test where the package is not installed."""
    taken = list(impersonation._taken.values())
    if not taken:
        if EXPECT_CURL_CFFI:
            pytest.fail("curl-cffi was to be installed for this run, and yt-dlp has no handler for it")
        pytest.skip("curl-cffi is not installed")
    monkeypatch.setattr(impersonation, "installed_version", INSTALLED_VERSION)
    return taken[0]


@pytest.fixture(name="browser", params=[STAND_IN, CURL_CFFI])
def browser_fixture(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Any:
    """模擬瀏覽器 installed and ready. Returns the handler, which says what it was sent."""
    handler = recording(stand_in_handler() if request.param == STAND_IN else real_handler(monkeypatch))
    monkeypatch.setattr(impersonation, "_kept", (impersonation.leaving_youtube_alone(handler),))
    return handler

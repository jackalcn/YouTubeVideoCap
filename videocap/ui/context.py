from __future__ import annotations

import ipaddress
import os
import re
import time
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

import streamlit as st

from videocap.config import (
    AUDIO_FORMAT_OPTIONS,
    CLOUD_RETENTION_SECONDS,
    NAMING_CUSTOM,
    NAMING_DATED,
    NAMING_ORIGINAL,
    NAMING_TEMPLATE,
    get_default_output_dir,
)
from videocap.environment import is_streamlit_cloud_environment, remote_visitors_are_trusted
from videocap.options import AccessLimits
from videocap.services import LOCAL_OWNER, AppServices, get_shared_services
from videocap.settings import SETTING_KEY_PREFIX, DownloadSettings, settings_from_dict
from videocap.storage import LAST_SETTINGS_KEY, OUTPUT_DIR_KEY
from videocap.tooling import get_ffmpeg_status, get_js_runtime_status


# Streamlit forgets a widget's value when a page does not draw it, and these must survive moving between pages.
PERSISTENT_KEY_PREFIXES = (SETTING_KEY_PREFIX, "keep_")
OUTPUT_DIR_WIDGET_KEY = "keep_output_dir"
VISITOR_COOKIE = "videocap_visitor"
# A session cookie: the choice lasts until the browser closes, through refreshes that start a new session.
NOTIFY_COOKIE = "videocap_notify"
# Kept on every page, so a job finishing while the visitor is on another page still notifies them.
NOTIFY_KEY = "keep_browser_notify"
# Set by 清除我的資料: the next run tells the browser to drop this app's cookies.
FORGET_COOKIES_KEY = "forget_visitor_cookies"
# What 清除我的資料 keeps: the password already given, and the pause between requests to YouTube, which clearing must
# not skip.
KEPT_WHEN_FORGETTING = ("access_granted", "last_public_request")
PROXY_HEADERS = ("X-Forwarded-For", "X-Real-Ip", "Forwarded")
VISITOR_ID_RE = re.compile(r"[0-9a-f]{32}")
VISITOR_COOKIE_RENEW_SECONDS = 10 * 60
LOCAL_HOST_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
# Comma-separated extra names a VIDEOCAP_TRUST_REMOTE install is reached by, such as a NAS's name or LAN address.
ALLOWED_HOSTS_ENV = "VIDEOCAP_ALLOWED_HOSTS"


@dataclass
class AppContext:
    services: AppServices
    is_cloud: bool
    # Allowed to reach this machine's files and settings: the public host's visitors never are.
    trusted: bool
    browser_login_allowed: bool
    owner: str
    limits: AccessLimits
    ffmpeg_available: bool
    ffmpeg_status: str
    js_runtime_available: bool
    js_runtime_status: str
    # The visitor's network address when it tells people apart; see visitor_address.
    client: str = ""
    pages: dict[str, Any] = field(default_factory=dict)

    @property
    def can_open_files(self) -> bool:
        # Opening a file happens on the machine running the app, which only helps someone sitting at it.
        return self.browser_login_allowed

    @property
    def naming_rules(self) -> list[str]:
        rules = [NAMING_ORIGINAL, NAMING_DATED, NAMING_CUSTOM]
        return [*rules, NAMING_TEMPLATE] if self.trusted else rules

    @property
    def audio_formats(self) -> list[str]:
        # Without ffmpeg nothing can be converted, so what YouTube serves is all there is.
        return list(AUDIO_FORMAT_OPTIONS) if self.ffmpeg_available else ["m4a"]


def request_host_name(headers: Mapping[str, str]) -> Optional[str]:
    """The site name the browser asked for, which it always sends and a web page cannot change."""
    try:
        return urlparse(f"//{headers.get('Host', '')}").hostname
    except ValueError:
        return None


def origin_is_allowed(headers: Mapping[str, str], host_names: set[str] | frozenset[str]) -> bool:
    """Whether the page that opened this session belongs to the app. Streamlit refuses other sites' pages itself, but
    not when its CORS check is turned off, as some dev setups do."""
    origin = headers.get("Origin")
    if origin is None:
        return True
    try:
        return urlparse(origin).hostname in host_names
    except ValueError:
        return False


def is_local_request(headers: Mapping[str, str], ip_address: Optional[str]) -> bool:
    # Reading browser cookies acts as whoever is logged in on this machine, so only offer it to this machine.
    # A reverse proxy on this machine makes every visitor look local; proxies announce the real visitor in these.
    proxied = any(headers.get(name) for name in PROXY_HEADERS)
    local_host = request_host_name(headers) in LOCAL_HOST_NAMES
    return ip_address is None and local_host and not proxied and origin_is_allowed(headers, LOCAL_HOST_NAMES)


def allowed_host_names() -> set[str]:
    extra = os.getenv(ALLOWED_HOSTS_ENV, "")
    return {*LOCAL_HOST_NAMES, *(name.strip().strip("[]").lower() for name in extra.split(",") if name.strip())}


def remote_request_is_trusted(headers: Mapping[str, str]) -> bool:
    """For VIDEOCAP_TRUST_REMOTE. Another site can point its own name at this machine (DNS rebinding) and drive the app
    from a browser here, so only the names this app is meant to be reached by count."""
    if not remote_visitors_are_trusted():
        return False
    if not origin_is_allowed(headers, allowed_host_names()):
        return False
    # Browsers always send the name; only a script talking to the port directly leaves it out, and such a script can
    # reach the port anyway, which is what VIDEOCAP_TRUST_REMOTE accepts.
    if not headers.get("Host"):
        return True
    return request_host_name(headers) in allowed_host_names()


def get_services(is_public: bool) -> AppServices:
    return get_shared_services(is_public)


def get_session_owner() -> str:
    # A public host is shared by every visitor, so each visitor only sees their own jobs. A refresh starts a new
    # session, so the ID also lives in a cookie: otherwise a refresh would lose sight of a running download.
    if not st.session_state.get("session_owner"):
        saved = st.context.cookies.get(VISITOR_COOKIE)
        is_valid = isinstance(saved, str) and VISITOR_ID_RE.fullmatch(saved)
        st.session_state["session_owner"] = saved if is_valid else uuid.uuid4().hex
    return str(st.session_state["session_owner"])


def start_as_new_visitor() -> None:
    """For 清除我的資料: forgets everything this session holds and carries on under a fresh visitor ID, which the
    cookie still naming the old one cannot bring back."""
    kept = {key: st.session_state[key] for key in KEPT_WHEN_FORGETTING if key in st.session_state}
    st.session_state.clear()
    st.session_state.update(kept)
    st.session_state["session_owner"] = uuid.uuid4().hex
    # Otherwise the cookie this run's browser sent would turn notifications back on.
    st.session_state[NOTIFY_KEY] = False
    st.session_state[FORGET_COOKIES_KEY] = True


def forget_cookies_script() -> str:
    return (
        f'<script>document.cookie = "{VISITOR_COOKIE}=; max-age=0; path=/";'
        f'document.cookie = "{NOTIFY_COOKIE}=; max-age=0; path=/";</script>'
    )


def remember_visitor(ctx: AppContext) -> None:
    if st.session_state.pop(FORGET_COOKIES_KEY, False):
        # The new ID is written on a later run, so this cannot run after it and wipe it.
        st.html(forget_cookies_script(), unsafe_allow_javascript=True)
        return
    # Written again on each visit, and now and then during a long one, so the ID lasts as long as the files of the
    # visitor's latest jobs rather than two hours from the first visit.
    now = time.time()
    if ctx.trusted or now - st.session_state.get("visitor_cookie_written_at", 0.0) < VISITOR_COOKIE_RENEW_SECONDS:
        return
    st.session_state["visitor_cookie_written_at"] = now
    # The ID is hexadecimal, as checked or generated above, so it cannot break out of this script.
    st.html(
        f'<script>document.cookie = "{VISITOR_COOKIE}={ctx.owner}; path=/; max-age={CLOUD_RETENTION_SECONDS}; SameSite=Lax";</script>',
        unsafe_allow_javascript=True,
    )


def visitor_access(is_cloud: bool) -> tuple[bool, bool]:
    """Whether this session sits at this machine, and whether it may reach the machine's files and settings."""
    headers = st.context.headers
    local_client = not is_cloud and is_local_request(headers, st.context.ip_address)
    return local_client, local_client or (not is_cloud and remote_request_is_trusted(headers))


def visitor_address(ip_address: Any, is_cloud: bool) -> str:
    """The visitor's network address, for limits that opening another session does not reset, or "" when it cannot
    tell people apart. Streamlit Community Cloud's proxy may give every visitor the same private address, which would
    merge them all into one, so there only a public address counts."""
    try:
        address = ipaddress.ip_address(str(ip_address)) if isinstance(ip_address, str) and ip_address else None
    except ValueError:
        return ""
    if address is None or address.is_loopback or address.is_unspecified or (is_cloud and not address.is_global):
        return ""
    return str(address)


def build_context() -> AppContext:
    is_cloud = is_streamlit_cloud_environment()
    local_client, trusted = visitor_access(is_cloud)
    ffmpeg_available, ffmpeg_status = get_ffmpeg_status()
    js_runtime_available, js_runtime_status = get_js_runtime_status()
    return AppContext(
        services=get_services(is_cloud),
        is_cloud=is_cloud,
        trusted=trusted,
        browser_login_allowed=local_client,
        owner=LOCAL_OWNER if trusted else get_session_owner(),
        limits=AccessLimits() if trusted else AccessLimits.for_public_host(),
        ffmpeg_available=ffmpeg_available,
        ffmpeg_status=ffmpeg_status,
        js_runtime_available=js_runtime_available,
        js_runtime_status=js_runtime_status,
        client="" if trusted else visitor_address(st.context.ip_address, is_cloud),
    )


def init_session_state(ctx: AppContext) -> None:
    st.session_state.setdefault("last_reports", [])
    st.session_state.setdefault("keep_url_input", "")
    st.session_state.setdefault("last_batch_id", "")
    st.session_state.setdefault("search_results", [])
    st.session_state.setdefault("known_finished_jobs", None)
    st.session_state.setdefault("seen_active_jobs", set())
    st.session_state.setdefault(NOTIFY_KEY, st.context.cookies.get(NOTIFY_COOKIE) == "1")

    if "settings_loaded" not in st.session_state:
        st.session_state["settings_loaded"] = True
        apply_settings_to_state(load_starting_settings(ctx))

    if OUTPUT_DIR_WIDGET_KEY not in st.session_state:
        st.session_state[OUTPUT_DIR_WIDGET_KEY] = str(default_output_dir(ctx))
    st.session_state.setdefault("keep_login_method", "none")

    for key in list(st.session_state):
        if str(key).startswith(PERSISTENT_KEY_PREFIXES):
            st.session_state[key] = st.session_state[key]


def load_starting_settings(ctx: AppContext) -> DownloadSettings:
    storage = ctx.services.storage
    if ctx.trusted and storage:
        saved = storage.get_value(LAST_SETTINGS_KEY)
        if isinstance(saved, dict):
            return settings_from_dict(saved)
    return DownloadSettings()


def default_output_dir(ctx: AppContext) -> Path:
    storage = ctx.services.storage
    if ctx.trusted and storage:
        saved = storage.get_value(OUTPUT_DIR_KEY)
        if isinstance(saved, str) and saved.strip():
            return Path(saved)
    return get_default_output_dir()


def apply_settings_to_state(settings: DownloadSettings) -> None:
    for spec in fields(DownloadSettings):
        st.session_state[f"{SETTING_KEY_PREFIX}{spec.name}"] = getattr(settings, spec.name)


def collect_settings() -> DownloadSettings:
    values = {spec.name: st.session_state.get(f"{SETTING_KEY_PREFIX}{spec.name}") for spec in fields(DownloadSettings)}
    return settings_from_dict({key: value for key, value in values.items() if value is not None})


def current_output_dir(ctx: AppContext) -> Path:
    # Windows' 「複製路徑」 wraps the folder in quotes, which would otherwise become part of a folder name.
    raw = str(st.session_state.get(OUTPUT_DIR_WIDGET_KEY) or "").strip().strip('"').strip()
    return Path(raw or get_default_output_dir()).expanduser().resolve()


def current_browser() -> Optional[str]:
    return st.session_state.get("keep_login_browser") if st.session_state.get("keep_login_method") == "browser" else None


def current_cookie_bytes() -> Optional[bytes]:
    return st.session_state.get("cookies_bytes") if st.session_state.get("keep_login_method") == "cookies" else None

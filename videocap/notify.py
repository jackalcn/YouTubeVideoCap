"""Tells the owner's phone when downloads finish: a Discord webhook, a Telegram bot, or an ntfy topic.

Plain urllib, so nothing new to install. The targets hold bot tokens, so they stay out of settings exports and
backups, and pages show only their last few characters."""

from __future__ import annotations

import json
import logging
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, fields
from typing import Any, Callable, Mapping


LOGGER = logging.getLogger("videocap.notify")
TELEGRAM_API = "https://api.telegram.org"
SEND_TIMEOUT_SECONDS = 10
USER_AGENT = "youtube-videocap"
DISCORD_MAX_CHARACTERS = 2000
TELEGRAM_MAX_CHARACTERS = 4096
DISCORD_WEBHOOK_RE = re.compile(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+/?")
TELEGRAM_TOKEN_RE = re.compile(r"\d+:[\w-]{30,}")
# A numeric chat (groups and channels are negative), or a public channel's @name.
TELEGRAM_CHAT_RE = re.compile(r"-?\d+|@\w{5,}")


@dataclass(frozen=True)
class NotifyTargets:
    discord_webhook: str = ""
    telegram_token: str = ""
    telegram_chat_id: str = ""
    ntfy_url: str = ""

    @property
    def is_set(self) -> bool:
        return bool(self.discord_webhook or (self.telegram_token and self.telegram_chat_id) or self.ntfy_url)

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def targets_from_dict(data: Any) -> NotifyTargets:
    if not isinstance(data, Mapping):
        return NotifyTargets()
    return NotifyTargets(**{spec.name: str(data[spec.name]).strip() for spec in fields(NotifyTargets) if isinstance(data.get(spec.name), str)})


def target_problems(targets: NotifyTargets) -> list[str]:
    """What is wrong with each target, in words for the person, so a typo is caught on saving rather than never heard of."""
    problems: list[str] = []
    if targets.discord_webhook and not DISCORD_WEBHOOK_RE.fullmatch(targets.discord_webhook):
        problems.append("Discord Webhook 網址不對：請在頻道的「整合」→「Webhook」按「複製 Webhook 網址」後整段貼上。")
    if bool(targets.telegram_token) != bool(targets.telegram_chat_id):
        problems.append("Telegram 需要同時填 Bot Token 和 Chat ID。")
    if targets.telegram_token and not TELEGRAM_TOKEN_RE.fullmatch(targets.telegram_token):
        problems.append("Telegram Bot Token 不對：應該像 123456789:ABC... 這樣，可在 @BotFather 查到。")
    if targets.telegram_chat_id and not TELEGRAM_CHAT_RE.fullmatch(targets.telegram_chat_id):
        problems.append("Telegram Chat ID 不對：應該是一串數字（群組以 - 開頭），或公開頻道的 @名稱。")
    if targets.ntfy_url:
        parsed = urllib.parse.urlparse(targets.ntfy_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or not parsed.path.strip("/"):
            problems.append("ntfy 主題網址不對：應該像 https://ntfy.sh/你的主題名稱。")
    return problems


def masked(secret: str) -> str:
    """Enough to tell which one is saved, never enough to use it."""
    return f"已設定（…{secret[-4:]}）" if len(secret) > 12 else "已設定"


def _post(url: str, body: bytes, content_type: str, proxy: str, timeout: float) -> None:
    request = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": content_type, "User-Agent": USER_AGENT})
    # Without a proxy setting, build_opener still uses the system's proxy, as urlopen does.
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    with urllib.request.build_opener(*handlers).open(request, timeout=timeout) as response:
        response.read(1024)


def send_discord(webhook: str, title: str, message: str, proxy: str = "", timeout: float = SEND_TIMEOUT_SECONDS) -> None:
    content = f"**{title}**\n{message}"[:DISCORD_MAX_CHARACTERS]
    # Titles come from uploaders; "@everyone" in one must not ping the owner's whole server.
    body = json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode("utf-8")
    _post(webhook, body, "application/json", proxy, timeout)


def send_telegram(token: str, chat_id: str, title: str, message: str, proxy: str = "", timeout: float = SEND_TIMEOUT_SECONDS) -> None:
    # Plain text: without parse_mode, nothing in a title can turn into a link or formatting.
    body = json.dumps({"chat_id": chat_id, "text": f"{title}\n{message}"[:TELEGRAM_MAX_CHARACTERS]}).encode("utf-8")
    _post(f"{TELEGRAM_API}/bot{token}/sendMessage", body, "application/json", proxy, timeout)


def send_ntfy(topic_url: str, title: str, message: str, proxy: str = "", timeout: float = SEND_TIMEOUT_SECONDS) -> None:
    # HTTP headers cannot carry Chinese, so the title goes in ntfy's query parameter instead of its Title header.
    separator = "&" if urllib.parse.urlparse(topic_url).query else "?"
    url = f"{topic_url}{separator}{urllib.parse.urlencode({'title': title})}"
    _post(url, message.encode("utf-8"), "text/plain; charset=utf-8", proxy, timeout)


def describe_failure(exc: Exception) -> str:
    """Never the address: Telegram's holds the bot token."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"伺服器回應 HTTP {exc.code}，請檢查設定是否正確。"
    if isinstance(exc, urllib.error.URLError):
        return f"連不上：{exc.reason}"
    if isinstance(exc, TimeoutError):
        return "等候回應逾時。"
    return f"傳送失敗：{type(exc).__name__}"


def send_all(targets: NotifyTargets, title: str, message: str, proxy: str = "", timeout: float = SEND_TIMEOUT_SECONDS) -> list[str]:
    """Sends to every target that is set. Returns what failed, one line per target."""
    senders: list[tuple[str, Callable[[], None]]] = []
    if targets.discord_webhook:
        senders.append(("Discord", lambda: send_discord(targets.discord_webhook, title, message, proxy, timeout)))
    if targets.telegram_token and targets.telegram_chat_id:
        senders.append(
            ("Telegram", lambda: send_telegram(targets.telegram_token, targets.telegram_chat_id, title, message, proxy, timeout))
        )
    if targets.ntfy_url:
        senders.append(("ntfy", lambda: send_ntfy(targets.ntfy_url, title, message, proxy, timeout)))
    problems: list[str] = []
    for name, send in senders:
        try:
            send()
        except Exception as exc:
            problems.append(f"{name}：{describe_failure(exc)}")
    return problems


def send_in_background(targets: NotifyTargets, title: str, message: str, proxy: str = "") -> threading.Thread:
    """For events nobody waits for: a slow or failing service must hold up no job, and its failure only reaches the log."""

    def run() -> None:
        for problem in send_all(targets, title, message, proxy):
            LOGGER.warning("通知沒有送出：%s", problem)

    thread = threading.Thread(target=run, name="videocap-notify", daemon=True)
    thread.start()
    return thread

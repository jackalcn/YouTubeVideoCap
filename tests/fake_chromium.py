"""A program that stands in for a Chromium-based browser: it opens no window, listens on the debugging connection a
browser opens in its profile, and answers the few commands that videocap.verification asks of one.

What it says is set by the FAKE_CHROMIUM environment variable, a JSON object:
  cookies        cookies it holds from the start, as the browser's own list gives them
  clearance      {"after": seconds, "cookie": {...}}: a cookie that appears that long after it started, as the one a
                 check leaves when it is passed
  user_agent, hints, title, url   what it says of itself and of its page
  exit_after     seconds after which its window is closed: its connection drops at once, and it ends `linger` seconds
                 later (none by default), as a browser does once it has shut down
  exit_at_once   true: ends before it listens, as a browser that cannot start
  argv_log       a file to write the arguments it was started with in, as a list
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from websockets.sync.server import serve


scenario: dict[str, Any] = json.loads(os.environ.get("FAKE_CHROMIUM") or "{}")
started = time.monotonic()
# The connections made to it, which closing its window drops.
connections: list[Any] = []
DEFAULT_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"


def cookies_now() -> list[dict[str, Any]]:
    cookies = list(scenario.get("cookies", []))
    clearance = scenario.get("clearance")
    if clearance and time.monotonic() - started >= clearance["after"]:
        cookies.append(clearance["cookie"])
    return cookies


def answer(method: str) -> dict[str, Any]:
    if method == "Browser.getVersion":
        return {"product": "Chrome/157.0.0.0", "userAgent": scenario.get("user_agent", DEFAULT_AGENT)}
    if method == "Storage.getCookies":
        return {"cookies": cookies_now()}
    if method == "Target.getTargets":
        page = {"targetId": "page-1", "type": "page", "title": scenario.get("title", "Just a moment..."), "url": scenario.get("url", "https://site.example/")}
        return {"targetInfos": [page]}
    if method == "Target.attachToTarget":
        return {"sessionId": "session-1"}
    if method == "Runtime.evaluate":
        return {"result": {"value": json.dumps(scenario.get("hints"))}}
    if method in ("Target.detachFromTarget", "Browser.close"):
        return {}
    raise ValueError(method)


def close_window() -> None:
    # Dropped with no closing word, as a browser that goes away drops it.
    for connection in list(connections):
        try:
            connection.socket.close()
        except OSError:
            pass
    time.sleep(float(scenario.get("linger") or 0))
    os._exit(0)


def handle(connection: Any) -> None:
    connections.append(connection)
    for raw in connection:
        message = json.loads(raw)
        try:
            reply: dict[str, Any] = {"id": message["id"], "result": answer(message["method"])}
        except ValueError:
            reply = {"id": message["id"], "error": {"message": "unknown command"}}
        connection.send(json.dumps(reply))
        if message["method"] == "Browser.close":
            os._exit(0)


def main() -> None:
    if scenario.get("argv_log"):
        Path(scenario["argv_log"]).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
    if scenario.get("exit_at_once"):
        sys.exit(3)
    profile = Path(next(argument.split("=", 1)[1] for argument in sys.argv if argument.startswith("--user-data-dir=")))
    with serve(handle, "127.0.0.1", 0) as server:
        port = server.socket.getsockname()[1]
        (profile / "DevToolsActivePort").write_text(f"{port}\n/devtools/browser/fake\n", encoding="utf-8")
        if scenario.get("exit_after") is not None:
            threading.Timer(float(scenario["exit_after"]), close_window).start()
        server.serve_forever()


if __name__ == "__main__":
    main()

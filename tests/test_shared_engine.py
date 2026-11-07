from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
import streamlit as st

import videocap
import videocap.__main__ as launcher
import videocap.services as services_module
from videocap.services import get_shared_services, reset_shared_services
from videocap.storage import PENDING_JOBS_KEY


@pytest.fixture(autouse=True)
def private_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEOCAP_DATA_DIR", str(tmp_path / "data"))


def test_a_cleared_streamlit_cache_does_not_start_a_second_engine() -> None:
    first = get_shared_services(False)
    # Any visitor can make Streamlit clear its caches; the engine must not live there.
    st.cache_resource.clear()
    assert get_shared_services(False) is first


def test_the_engine_survives_streamlit_reimporting_its_module(monkeypatch: pytest.MonkeyPatch) -> None:
    first = get_shared_services(False)
    # Streamlit drops an app module from sys.modules when its file changes, and imports it again on the next run.
    # Both entries are put back afterwards, so later tests see the original module.
    monkeypatch.setattr(videocap, "services", services_module)
    monkeypatch.delitem(sys.modules, "videocap.services")
    reimported = importlib.import_module("videocap.services")

    assert reimported is not services_module
    assert reimported.get_shared_services(False) is first


def test_one_engine_restores_unfinished_jobs_once(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = get_shared_services(False)
    assert engine.storage
    engine.storage.set_value(PENDING_JOBS_KEY, [])
    restored: list[object] = []
    monkeypatch.setattr(services_module.AppServices, "_restore_pending_jobs", lambda self: restored.append(self))
    for _ in range(3):
        get_shared_services(False)
    assert restored == []


def test_resetting_builds_a_new_engine() -> None:
    first = get_shared_services(False)
    reset_shared_services()
    assert get_shared_services(False) is not first


def test_the_launcher_starts_the_engine_before_streamlit(monkeypatch: pytest.MonkeyPatch) -> None:
    from streamlit.web import cli

    seen: dict[str, object] = {}

    def fake_streamlit() -> int:
        holder = sys.modules[services_module.SHARED_SERVICES_MODULE]
        seen["engine_ready"] = bool(holder.instances)
        seen["argv"] = list(sys.argv)
        return 0

    monkeypatch.setattr(cli, "main", fake_streamlit)
    monkeypatch.setattr(launcher, "warm_up_gpu_detection", lambda: None)
    monkeypatch.setattr(sys, "argv", ["videocap", "--server.port", "8765"])
    with pytest.raises(SystemExit) as stopped:
        launcher.main()
    assert stopped.value.code == 0
    assert seen["engine_ready"] is True
    assert seen["argv"] == ["streamlit", "run", str(launcher.MAIN_SCRIPT), "--server.port", "8765"]
    assert launcher.MAIN_SCRIPT.name == "main.py" and launcher.MAIN_SCRIPT.exists()


def run_launcher(monkeypatch: pytest.MonkeyPatch, mode: str) -> list[str]:
    """Runs the launcher with a stand-in for Streamlit, the engine and the GPU check. Returns the threads that checked."""
    import threading

    from streamlit.web import cli

    import videocap.media_tools as media_tools
    import videocap.tooling as tooling

    checked: list[str] = []
    done = threading.Event()

    def fake_check(ffmpeg: str) -> str:
        current = threading.current_thread()
        checked.append(f"{current.name} daemon={current.daemon}")
        done.set()
        return ""

    monkeypatch.setenv("VIDEOCAP_MODE", mode)
    monkeypatch.setattr(services_module, "get_shared_services", lambda is_public: None)
    monkeypatch.setattr(tooling, "get_ffmpeg_status", lambda: (True, "ffmpeg"))
    monkeypatch.setattr(media_tools, "hardware_h264_encoder", fake_check)
    monkeypatch.setattr(cli, "main", lambda: 0)
    monkeypatch.setattr(sys, "argv", ["videocap"])
    with pytest.raises(SystemExit):
        launcher.main()
    done.wait(5 if mode == "local" else 0.5)
    return checked


def test_the_launcher_checks_the_gpu_in_the_background_on_this_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    # The 工具 page's first visit would otherwise wait for the test encodes.
    checked = run_launcher(monkeypatch, "local")
    assert len(checked) == 1 and checked[0].endswith("daemon=True") and not checked[0].startswith("MainThread")


def test_the_launcher_leaves_the_gpu_alone_on_a_public_host(monkeypatch: pytest.MonkeyPatch) -> None:
    assert run_launcher(monkeypatch, "cloud") == []


def test_a_secrets_file_streamlit_cannot_read_stops_the_launcher_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from streamlit.web import cli

    # Streamlit itself dies with a UnicodeDecodeError traceback on such a file, before the app could say anything.
    secrets = tmp_path / "secrets.toml"
    secrets.write_bytes('app_password = "開門"\n'.encode("utf-16"))
    monkeypatch.setattr(launcher, "secrets_files", lambda: [secrets])
    monkeypatch.setattr(cli, "main", lambda: pytest.fail("Streamlit must not start"))
    monkeypatch.setattr(sys, "argv", ["videocap"])
    with pytest.raises(SystemExit) as stopped:
        launcher.main()
    assert stopped.value.code == 1
    message = capsys.readouterr().err
    assert str(secrets) in message and "UTF-8" in message


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_a_utf8_secrets_file_lets_the_launcher_start(encoding: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from streamlit.web import cli

    secrets = tmp_path / "secrets.toml"
    secrets.write_text('app_password = "開門"\n', encoding=encoding)
    monkeypatch.setattr(launcher, "secrets_files", lambda: [secrets, tmp_path / "missing.toml"])
    monkeypatch.setattr(launcher, "warm_up_gpu_detection", lambda: None)
    monkeypatch.setattr(cli, "main", lambda: 0)
    monkeypatch.setattr(sys, "argv", ["videocap"])
    with pytest.raises(SystemExit) as stopped:
        launcher.main()
    assert stopped.value.code == 0

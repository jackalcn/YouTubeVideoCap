"""The pages with links to sites other than YouTube: what this machine's owner can do with them, what a visitor is
told instead, and the texts that no longer speak of YouTube alone."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest
from streamlit.testing.v1 import AppTest

import videocap.services as services_module
from conftest import needs_ffmpeg
from offline_web import LocalSite, new_site_fixture, web_media_fixture  # noqa: F401
from offline_youtube import duration_of
from test_ui_download import (  # noqa: F401
    URL,
    button,
    download,
    isolated_app,
    new_page,
    public_mode,
    run_page,
    services,
    texts,
    wait_until,
)
from videocap.config import LOCAL_RUN_GUIDE_URL
from videocap.jobs import ACTIVE_STATES, DONE
from videocap.options import OTHER_SITES_OWNER_ONLY_MESSAGE
from videocap.services import SUBSCRIPTION_YOUTUBE_ONLY_MESSAGE
from videocap.settings import DownloadSettings


VIMEO = "https://vimeo.com/76979871"
DIRECT = "https://example.com/files/clip.mp4"
PAGE = "https://example.com/article"
NETFLIX = "https://www.netflix.com/watch/80100172"
PLAYLIST = "https://www.youtube.com/playlist?list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"
NewSite = Callable[[], LocalSite]


def queued_urls(public: bool = False) -> list[str]:
    return [job.payload.spec.url for job in services(public).manager.list_jobs()]


def plain(text: str) -> str:
    # Links and names arrive escaped for Markdown.
    return text.replace("\\", "")


def paste(app: AppTest, text: str) -> AppTest:
    app.text_area(key="keep_url_input").set_value(text).run()
    assert not app.exception, app.exception
    return app


# The owner: other sites are downloaded like YouTube


def test_the_owner_sees_other_sites_named_and_queues_them() -> None:
    app = paste(run_page("download"), f"{VIMEO}\n{DIRECT}")
    detected = texts(app.caption)
    assert "Vimeo 1 個連結" in detected and "example.com（直接連結） 1 個連結" in detected
    assert "已略過" not in texts(app.warning)
    download(app, VIMEO, DIRECT)
    assert queued_urls() == [VIMEO, DIRECT]
    assert all(job.state == DONE for job in services().manager.list_jobs())


def test_the_owner_is_shown_how_to_paste_another_sites_link() -> None:
    app = run_page("download")
    box = app.text_area(key="keep_url_input")
    assert box.label == "影片網址"
    assert "vimeo.com" in box.placeholder and ".mp4" in box.placeholder
    assert "直接連結" in texts(app.caption)


def test_the_owner_is_told_that_other_sites_links_need_their_scheme() -> None:
    app = paste(run_page("download"), "https://www.youtube.com/")
    assert "不是可下載的 YouTube 影片、播放清單或頻道連結" in texts(app.warning)
    assert "其他網站的連結請以 http:// 或 https:// 開頭" in texts(app.warning)


def test_the_rights_reminder_names_every_sites_terms() -> None:
    shown = texts(run_page("download").caption)
    assert "各網站的服務條款" in shown and "擁有權利或已獲授權" in shown
    assert "YouTube 服務條款" not in shown


# Visitors: only YouTube, and told why


def test_a_visitor_is_told_why_other_sites_are_left_out_and_youtube_still_downloads(monkeypatch: pytest.MonkeyPatch) -> None:
    public_mode(monkeypatch)
    app = paste(run_page("download"), f"{VIMEO}\n{DIRECT}")
    notice = texts(app.warning)
    assert OTHER_SITES_OWNER_ONLY_MESSAGE in notice and "已略過 2 個其他網站的連結" in notice
    assert LOCAL_RUN_GUIDE_URL in notice
    button(app, "開始下載").click().run()
    assert not app.exception, app.exception
    assert "請輸入至少一個 YouTube 網址" in texts(app.error)
    assert queued_urls(public=True) == []

    download(app, VIMEO, DIRECT, URL, public=True)
    assert "已略過 2 個其他網站的連結" in texts(app.warning)
    assert queued_urls(public=True) == [URL]


def test_a_visitor_keeps_the_youtube_wording(monkeypatch: pytest.MonkeyPatch) -> None:
    public_mode(monkeypatch)
    app = run_page("download")
    assert "vimeo" not in app.text_area(key="keep_url_input").placeholder
    assert "直接連結" not in texts(app.caption)
    paste(app, "https://www.youtube.com/")
    assert "不是可下載的 YouTube" in texts(app.warning) and "其他網站的連結請以" not in texts(app.warning)


# Ordinary pages and encrypted services


def test_pages_among_video_links_wait_until_ticked() -> None:
    app = paste(run_page("download"), f"{URL}\n{PAGE}")
    box = app.checkbox(key="keep_include_pages")
    assert box.label == "也嘗試這 1 個一般網頁" and not box.value
    said = plain(texts(app.caption))
    assert "可能沒有影片" in said and PAGE in said
    assert "偵測到 1 部影片。" in texts(app.caption)

    box.check().run()
    assert "example.com（一般網頁） 1 個連結" in texts(app.caption)
    button(app, "開始下載").click().run()
    assert not app.exception, app.exception
    wait_until(lambda: not [job for job in services().manager.list_jobs() if job.state in ACTIVE_STATES])
    assert queued_urls() == [URL, PAGE]


def test_a_tick_for_some_pages_does_not_carry_over_to_others() -> None:
    app = paste(run_page("download"), f"{URL}\n{PAGE}")
    app.checkbox(key="keep_include_pages").check().run()
    # Another link beside the same page keeps the tick.
    paste(app, f"{URL}\n{PAGE}\nhttps://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert app.checkbox(key="keep_include_pages").value
    # Other pages start left out again.
    paste(app, f"{URL}\nhttps://example.org/another-article")
    assert not app.checkbox(key="keep_include_pages").value
    paste(app, f"{URL}\n{PAGE}")
    assert not app.checkbox(key="keep_include_pages").value


def test_pages_alone_are_taken_without_asking() -> None:
    app = paste(run_page("download"), PAGE)
    assert not [box for box in app.checkbox if box.key == "keep_include_pages"]
    assert "example.com（一般網頁） 1 個連結" in texts(app.caption)


def test_an_encrypted_services_link_is_named_and_not_tried() -> None:
    app = paste(run_page("download"), NETFLIX)
    notice = plain(texts(app.warning))
    assert "DRM" in notice and "無法下載" in notice and NETFLIX in notice
    button(app, "開始下載").click().run()
    assert not app.exception, app.exception
    assert "請輸入至少一個影片網址" in texts(app.error)
    assert queued_urls() == []


def test_cookies_without_a_youtube_login_hold_back_only_youtube_links() -> None:
    from videocap.config import NO_LOGIN_COOKIES_MESSAGE

    app = run_page("download")
    app.session_state["keep_login_method"] = "cookies"
    # Another site's login, which cannot be read the way a YouTube login can.
    app.session_state["cookies_bytes"] = b"# Netscape HTTP Cookie File\n.vimeo.com\tTRUE\t/\tTRUE\t2147483647\tvimeo\tabc\n"
    paste(app, URL)
    button(app, "開始下載").click().run()
    assert not app.exception, app.exception
    assert NO_LOGIN_COOKIES_MESSAGE in plain(texts(app.error))
    assert queued_urls() == []

    download(app, VIMEO)
    assert not app.error, texts(app.error)
    assert queued_urls() == [VIMEO]


# Reports of other sites


def stored_report(url: str, **fields: Any) -> dict[str, Any]:
    return {"url": url, "error": "", "title": "t", "video_id": "76979871", "is_playlist": False, **fields}


def test_a_report_names_its_site_and_has_comments_only_on_youtube() -> None:
    app = new_page("download")
    app.session_state["keep_url_input"] = f"{URL}\n{VIMEO}"
    app.session_state["last_reports"] = [
        stored_report(URL, video_id="jNQXAC9IVRw", site="YouTube", extractor="youtube"),
        stored_report(VIMEO, site="Vimeo", extractor="vimeo"),
    ]
    app.run()
    assert not app.exception, app.exception
    shown = texts(app.markdown)
    assert "網站：YouTube" in shown and "網站：Vimeo" in shown
    assert [tab.label for tab in app.tabs].count("留言") == 1
    assert len([item for item in app.button if item.label.startswith("取得留言")]) == 1


def test_a_format_without_codec_details_is_offered_by_what_is_known() -> None:
    # A direct link's file, or a site that lists only its files: the table says what is not known, the picker does not.
    row = {"格式ID": "2", "類型": "影像", "解析度": "400x304", "FPS": "", "動態範圍": "", "容器": "ogv", "影像編碼": "未知", "音訊編碼": "未知"}
    row |= {"語言": "", "大小": "3.87 MB", "位元率": ""}
    app = new_page("download")
    app.session_state["keep_url_input"] = VIMEO
    app.session_state["last_reports"] = [stored_report(VIMEO, site="Vimeo", extractor="vimeo", format_rows=[row])]
    app.run()
    assert not app.exception, app.exception
    picker = next(box for box in app.selectbox if box.label == "影像格式")
    assert list(picker.options) == ["不含影像", "2 · 400x304 · ogv · 3.87 MB"]


def test_another_sites_live_stream_is_not_offered_for_recording() -> None:
    app = new_page("download")
    app.session_state["keep_url_input"] = VIMEO
    app.session_state["last_reports"] = [stored_report(VIMEO, site="Vimeo", extractor="vimeo", is_live=True)]
    app.run()
    assert not app.exception, app.exception
    assert "只支援錄製 YouTube" in texts(app.warning)
    assert "播放清單與頻道" not in texts(app.warning)


def list_report(url: str, ids: tuple[str, ...], **fields: Any) -> dict[str, Any]:
    entries = [
        {"index": index, "id": video_id, "title": f"第 {index} 部", "url": "", "duration": "", "channel": "", "views": None}
        for index, video_id in enumerate(ids, start=1)
    ]
    listed = {"is_playlist": True, "is_nested": False, "entry_count": len(ids), "entries": entries}
    return {"url": url, "error": "", "title": "清單", **listed, **fields}


def editor_id(app: AppTest) -> str:
    return str(next(frame for frame in app.dataframe if frame.proto.editing_mode).proto.id)


def run_with_edits(app: AppTest, frame_id: str, edits: dict[str, dict[str, Any]]) -> AppTest:
    """Runs the app as the browser would after ticking cells: AppTest itself cannot edit a table."""
    states = app._tree.get_widget_states()
    state = states.widgets.add()
    state.id = frame_id
    state.string_value = json.dumps({"edited_rows": edits, "added_rows": [], "deleted_rows": []})
    app._run(states)
    assert not app.exception, app.exception
    return app


def pick_second_of_three(report: dict[str, Any]) -> AppTest:
    app = new_page("download")
    app.session_state["keep_url_input"] = report["url"]
    app.session_state["last_reports"] = [report]
    app.run()
    assert not app.exception, app.exception
    frame_id = editor_id(app)
    edits = {"0": {"下載": False}, "1": {"下載": True}, "2": {"下載": False}}
    run_with_edits(app, frame_id, edits)
    button(app, "下載勾選的 1 部").click()
    # The browser keeps sending the cells it last had edited.
    return run_with_edits(app, frame_id, edits)


@pytest.mark.parametrize(
    "report",
    [
        list_report("https://example.com/videos.html", ("a-1", "a-2", "a-3"), site="example.com（一般網頁）", extractor="generic"),
        # A YouTube list whose entries came without IDs used to download the first videos instead.
        list_report(PLAYLIST, ("", "", ""), entries_have_ids=False),
    ],
    ids=["another-site", "youtube-without-ids"],
)
def test_a_list_without_youtube_ids_is_picked_by_position(report: dict[str, Any]) -> None:
    app = pick_second_of_three(report)
    assert "依清單中的編號" in texts(app.caption)
    jobs = services().manager.list_jobs()
    assert len(jobs) == 1
    spec = jobs[0].payload.spec
    assert (spec.url, spec.playlist_items, spec.playlist_limit, spec.video_ids) == (report["url"], "2", 1, [])


def test_a_youtube_list_is_still_picked_by_id() -> None:
    report = list_report(PLAYLIST, ("video000001", "video000002", "video000003"))
    app = pick_second_of_three(report)
    assert "依清單中的編號" not in texts(app.caption)
    spec = services().manager.list_jobs()[0].payload.spec
    assert (spec.playlist_items, spec.playlist_limit, spec.video_ids) == ("", 1, ["video000002"])


# The real yt-dlp against web sites on this machine


def real_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    from videocap.downloader import run_download

    monkeypatch.setattr(services_module, "run_download", run_download)


def wait_for_jobs(app: AppTest) -> list[Any]:
    manager = services().manager
    wait_until(lambda: bool(manager.list_jobs()) and not [job for job in manager.list_jobs() if job.state in ACTIVE_STATES], seconds=90)
    app.run()
    assert not app.exception, app.exception
    return manager.list_jobs()


@needs_ffmpeg
def test_a_direct_link_is_inspected_downloaded_and_kept_with_its_site(new_site: NewSite, monkeypatch: pytest.MonkeyPatch) -> None:
    real_engine(monkeypatch)
    site = new_site()
    url = site.add_clip("clip.mp4")
    app = paste(run_page("download"), url)
    assert "127.0.0.1（直接連結） 1 個連結" in texts(app.caption)

    button(app, "解析連結（不下載）").click().run()
    assert not app.exception, app.exception
    assert not app.error, texts(app.error)
    assert "網站：127.0.0.1（直接連結）" in texts(app.markdown)
    labels = [tab.label for tab in app.tabs]
    assert "格式" in labels and "留言" not in labels

    button(app, "開始下載").click().run()
    assert not app.exception, app.exception
    jobs = wait_for_jobs(app)
    assert [job.state for job in jobs] == [DONE], [job.error for job in jobs]
    assert "下載完成" in texts(app.success)

    history = run_page("history")
    assert list(history.dataframe[0].value["網站"]) == ["127.0.0.1（直接連結）"]
    library = run_page("library")
    assert list(library.dataframe[0].value["網站"]) == ["127.0.0.1（直接連結）"]


@needs_ffmpeg
def test_a_page_with_two_videos_downloads_the_one_ticked(new_site: NewSite, monkeypatch: pytest.MonkeyPatch) -> None:
    real_engine(monkeypatch)
    site = new_site()
    site.add_clip("one.mp4", "a")
    site.add_clip("two.mp4", "b")
    url = site.add_page("both.html", sources=("one.mp4", "two.mp4"), title="Both")
    app = paste(run_page("download"), url)
    button(app, "解析連結（不下載）").click().run()
    assert not app.exception, app.exception
    assert not app.error, texts(app.error)
    assert "依清單中的編號" in texts(app.caption)

    frame_id = editor_id(app)
    edits = {"0": {"下載": False}, "1": {"下載": True}}
    run_with_edits(app, frame_id, edits)
    button(app, "下載勾選的 1 部").click()
    run_with_edits(app, frame_id, edits)
    jobs = wait_for_jobs(app)
    assert [job.state for job in jobs] == [DONE], [job.error for job in jobs]
    items = jobs[0].outcome["items"]
    # The second clip is the three-second one.
    assert len(items) == 1 and duration_of(Path(items[0]["path"])) == pytest.approx(3, abs=0.3)


def test_history_and_library_name_each_files_site(tmp_path: Path) -> None:
    from videocap.ui import page_history

    folder = (tmp_path / "out").resolve()
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("talk.mp4", "mine.mp4"):
        (folder / name).write_bytes(b"video")
    storage = services().storage
    talk = {"video_id": "76979871", "title": "Talk", "url": VIMEO, "path": str(folder / "talk.mp4"), "extractor": "vimeo"}
    storage.add_history(
        [
            {**talk, "finished_at": 2.0},
            # Written before other sites existed, so it says nothing about its site.
            {"video_id": "jNQXAC9IVRw", "title": "Old", "url": URL, "finished_at": 1.0},
        ]
    )
    assert list(run_page("history").dataframe[0].value["網站"]) == ["Vimeo", "YouTube"]
    exported = page_history.history_csv(storage, "").decode("utf-8-sig").splitlines()
    assert exported[0].startswith("標題,頻道,網站,") and exported[1].startswith("Talk,,Vimeo,")

    rows = run_page("library").dataframe[0].value
    # A file put there by hand has no download behind it.
    assert dict(zip(rows["檔名"], rows["網站"])) == {"talk.mp4": "Vimeo", "mine.mp4": ""}


# Texts elsewhere that spoke of YouTube alone


def test_the_login_section_speaks_of_the_site_being_downloaded(monkeypatch: pytest.MonkeyPatch) -> None:
    from videocap.ui import context

    monkeypatch.setattr(context, "is_local_request", lambda *args: True)
    app = run_page("download")
    login = app.radio(key="keep_login_method")
    assert "正在下載的網站" in str(login.help) and "cookies.txt" in str(login.help)
    login.set_value("browser").run()
    assert "交給 yt-dlp" in texts(app.caption) and "正在下載的網站" in texts(app.caption)
    assert "只檢查 YouTube" in str(button(app, "確認登入狀態").help)


@pytest.mark.parametrize("public", [False, True])
def test_options_that_only_work_on_youtube_say_so_to_the_owner(monkeypatch: pytest.MonkeyPatch, public: bool) -> None:
    from videocap.ui import context

    monkeypatch.setattr(context, "get_ffmpeg_status", lambda: (True, "ffmpeg"))
    if public:
        public_mode(monkeypatch)
    app = run_page("download")
    helps = [
        app.selectbox(key="opt_sponsorblock_mode").help,
        app.selectbox(key="opt_player_client").help,
        app.selectbox(key="opt_comments_mode").help,
        app.checkbox(key="opt_prefer_traditional_metadata").help,
    ]
    if not public:
        helps += [app.checkbox(key="opt_wait_for_live").help, app.checkbox(key="opt_save_live_chat").help]
    # A visitor can only download from YouTube anyway.
    assert all(("只對 YouTube 有效" in str(help_text)) is not public for help_text in helps), helps


@pytest.mark.parametrize("public", [False, True])
def test_the_bookmarklet_says_which_pages_it_helps_with(monkeypatch: pytest.MonkeyPatch, public: bool) -> None:
    if public:
        public_mode(monkeypatch)
    app = run_page("tools")
    headings = texts(app.markdown)
    if public:
        assert "從 YouTube 頁面一鍵送到這裡" in headings
        assert "只能下載 YouTube" in texts(app.caption)
    else:
        assert "從影片網頁一鍵送到這裡" in headings
        assert "其他網站" in texts(app.caption)


@pytest.mark.parametrize("public", [False, True])
def test_an_empty_queue_says_which_links_can_be_pasted(monkeypatch: pytest.MonkeyPatch, public: bool) -> None:
    if public:
        public_mode(monkeypatch)
    said = texts(run_page("queue").info)
    assert ("貼上 YouTube 連結" if public else "貼上影片連結") in said


def test_a_subscription_to_another_site_is_refused_with_the_reason() -> None:
    app = run_page("subscriptions")
    next(item for item in app.text_input if item.label == "頻道或播放清單連結").set_value(VIMEO)
    button(app, "加入訂閱").click().run()
    assert not app.exception, app.exception
    assert SUBSCRIPTION_YOUTUBE_ONLY_MESSAGE in texts(app.error)
    assert services().storage.list_subscriptions() == []


def test_editing_a_subscription_to_another_site_says_why_it_cannot_be_saved(tmp_path: Path) -> None:
    # As a backup made with a later version, or by hand, might bring in.
    subscription_id = services().storage.add_subscription(VIMEO, "Vimeo channel", DownloadSettings(), str(tmp_path / "subs"), 5, 24)
    app = run_page("subscriptions")
    app.text_input(key=f"subscription_edit_title_{subscription_id}").set_value("新名字")
    button(app, "儲存").click().run()
    assert not app.exception, app.exception
    assert SUBSCRIPTION_YOUTUBE_ONLY_MESSAGE in texts(app.error)
    assert services().storage.get_subscription(subscription_id).title == "Vimeo channel"

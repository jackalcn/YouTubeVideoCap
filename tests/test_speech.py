"""語音辨識字幕: faster-whisper behind a fake, so nothing here needs the package, a model or the network. The one real
run is marked speech and needs VIDEOCAP_SPEECH_TESTS=1."""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

import pytest

import videocap.speech as speech
from conftest import needs_ffmpeg
from videocap.library import media_entry
from videocap.media_tools import ToolRequest, ToolResult
from videocap.subtitles import read_subtitle_file


class Progress:
    def __init__(self) -> None:
        self.values: list[float] = []
        self.texts: list[str] = []

    def progress(self, value: float, text: str = "") -> None:
        self.values.append(value)
        self.texts.append(text)


class FakeWhisper:
    """Stands in for the faster_whisper module: what each model was asked, and what it hears."""

    def __init__(self) -> None:
        self.heard = "zh"
        self.duration = 10.0
        self.segments: list[tuple[float, float, str]] = [(0.0, 2.5, " 今天天气很好"), (3.0, 5.0, " 我们一起去公园散步")]
        self.broken_devices: set[str] = set()
        # Where a broken device fails: as loading or as the first encode, which is how a missing cuBLAS shows.
        self.fails_when = "loading"
        self.models: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.on_segment: Callable[[int], None] = lambda index: None
        whisper = self

        class WhisperModel:
            def __init__(self, size: str, **options: Any) -> None:
                whisper.models.append({"size": size, **options})
                self.device = options.get("device")
                if self.device in whisper.broken_devices and whisper.fails_when == "loading":
                    raise ValueError("Requested float16 compute type, but the target device does not support it")

            def detect_language(self, audio: Any, **options: Any) -> tuple[str, float, list[Any]]:
                if self.device in whisper.broken_devices:
                    raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
                whisper.calls.append({"detect": len(audio), **options})
                return whisper.heard, 0.97, []

            def transcribe(self, audio: Any, **options: Any) -> tuple[Any, Any]:
                whisper.calls.append(options)
                return whisper.segment_stream(), SimpleNamespace(language=options["language"], duration=whisper.duration)

        self.WhisperModel = WhisperModel

    def segment_stream(self) -> Any:
        for index, (start, end, text) in enumerate(self.segments):
            self.on_segment(index)
            yield SimpleNamespace(start=start, end=end, text=text)

    @property
    def transcribe_options(self) -> dict[str, Any]:
        return next(call for call in reversed(self.calls) if "detect" not in call)


@pytest.fixture
def whisper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeWhisper:
    fake = FakeWhisper()
    monkeypatch.setenv("VIDEOCAP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(speech, "import_whisper", lambda: fake)
    monkeypatch.setattr(speech, "decode_audio", lambda path, ffmpeg, should_stop: [0.0] * 16000 * 10)
    monkeypatch.setattr(speech, "cuda_device_count", lambda: 0)
    monkeypatch.setattr(speech, "cuda_compute_type", lambda: "float16")
    monkeypatch.setattr(speech, "cuda_libraries_ready", lambda: True)
    return fake


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    path = tmp_path / "Lesson 1. 開場.mp4"
    path.write_bytes(b"video")
    return path


def transcribe(
    source: Path, should_stop: Optional[Callable[[], bool]] = None, progress: Optional[Progress] = None, **params: Any
) -> ToolResult:
    return speech.transcribe(ToolRequest("transcribe", str(source), params), "ffmpeg", progress or Progress(), should_stop)


def downloaded_model(data_dir: Path, model: str, model_bytes: int = 1000) -> Path:
    """A model as huggingface_hub leaves a finished download: model.bin and 24 bytes of other files."""
    snapshot = data_dir / "models" / "whisper" / speech.model_folder(model).name / "snapshots" / "d90ca5fe"
    snapshot.mkdir(parents=True)
    (snapshot / "model.bin").write_bytes(b"x" * model_bytes)
    for name, size in (("config.json", 4), ("tokenizer.json", 10), ("vocabulary.txt", 10)):
        (snapshot / name).write_bytes(b"x" * size)
    return snapshot


def cue_texts(path: Path) -> list[str]:
    return [cue.text for cue in read_subtitle_file(path)]


def test_speech_becomes_taiwan_traditional_subtitles_beside_the_video(whisper: FakeWhisper, clip: Path) -> None:
    result = transcribe(clip, language="zh", traditional=True)
    assert not result.error and not result.warning
    assert result.output_path == clip.with_name("Lesson 1. 開場.zh-TW.srt")
    content = result.output_path.read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:02,500" in content and "00:00:03,000 --> 00:00:05,000" in content
    assert cue_texts(result.output_path) == ["今天天氣很好", "我們一起去公園散步"]
    # Paired with its video, so the 媒體庫 lists it there.
    assert result.output_path in media_entry(clip).subtitles


def test_without_conversion_chinese_is_kept_as_whisper_wrote_it(whisper: FakeWhisper, clip: Path) -> None:
    result = transcribe(clip, language="zh", traditional=False)
    assert result.output_path == clip.with_name("Lesson 1. 開場.zh.srt")
    assert cue_texts(result.output_path) == ["今天天气很好", "我们一起去公园散步"]


def test_conversion_is_on_unless_turned_off(whisper: FakeWhisper, clip: Path) -> None:
    assert transcribe(clip, language="auto").output_path == clip.with_name("Lesson 1. 開場.zh-TW.srt")


def test_automatic_detection_names_the_file_after_the_language_heard(whisper: FakeWhisper, clip: Path) -> None:
    whisper.heard = "en"
    whisper.segments = [(0.0, 3.76, " The quick brown fox jumps over the lazy dog."), (3.76, 7.0, " Subtitles are generated from speech.")]
    result = transcribe(clip, language="auto", traditional=True)
    assert result.output_path == clip.with_name("Lesson 1. 開場.en.srt")
    assert cue_texts(result.output_path) == ["The quick brown fox jumps over the lazy dog.", "Subtitles are generated from speech."]
    options = whisper.transcribe_options
    # A Chinese prompt would only push English towards Chinese.
    assert options["language"] == "en" and options["initial_prompt"] is None
    assert options["vad_filter"] is True and options["beam_size"] == 5


@pytest.mark.parametrize("language", ["zh", "auto"])
def test_chinese_gets_a_traditional_prompt_that_never_shows_up_as_a_subtitle(whisper: FakeWhisper, clip: Path, language: str) -> None:
    whisper.segments = [(0.0, 1.0, " 以下是繁體中文的字幕。"), (1.0, 2.0, " 以下是繁体中文的字幕"), (2.0, 4.0, " 大家好")]
    result = transcribe(clip, language=language)
    assert whisper.transcribe_options["initial_prompt"] == speech.TRADITIONAL_PROMPT
    assert whisper.transcribe_options["language"] == "zh"
    assert cue_texts(result.output_path) == ["大家好"]


def test_a_chosen_language_is_used_even_when_another_is_heard_with_a_warning(whisper: FakeWhisper, clip: Path) -> None:
    whisper.heard = "en"
    whisper.segments = [(0.0, 2.0, " クイック")]
    result = transcribe(clip, language="ja")
    assert whisper.transcribe_options["language"] == "ja" and whisper.transcribe_options["initial_prompt"] is None
    assert result.output_path == clip.with_name("Lesson 1. 開場.ja.srt")
    assert "日文" in result.warning and "英文" in result.warning and "自動偵測" in result.warning


def test_a_chosen_language_that_is_heard_gives_no_warning(whisper: FakeWhisper, clip: Path) -> None:
    whisper.heard = "ko"
    assert transcribe(clip, language="ko").warning == ""


def test_existing_subtitles_are_never_overwritten(whisper: FakeWhisper, clip: Path) -> None:
    youtube = clip.with_name("Lesson 1. 開場.zh-TW.srt")
    youtube.write_text("YouTube's own", encoding="utf-8")
    first = transcribe(clip, language="zh")
    second = transcribe(clip, language="zh")
    assert youtube.read_text(encoding="utf-8") == "YouTube's own"
    assert first.output_path == clip.with_name("Lesson 1. 開場.zh-TW-ai.srt")
    assert second.output_path == clip.with_name("Lesson 1. 開場.zh-TW-ai2.srt")
    # Every one of them still belongs to the video.
    assert {youtube, first.output_path, second.output_path} <= set(media_entry(clip).subtitles)


def test_progress_follows_the_segments_and_names_the_language(whisper: FakeWhisper, clip: Path) -> None:
    progress = Progress()
    transcribe(clip, progress=progress, language="auto")
    assert "辨識中 25%（語言：中文）" in progress.texts and "辨識中 50%（語言：中文）" in progress.texts
    assert progress.values == sorted(progress.values)
    assert (progress.values[-1], progress.texts[-1]) == (1.0, "語音辨識字幕完成")


def test_stopping_between_segments_writes_nothing(whisper: FakeWhisper, clip: Path) -> None:
    stop = threading.Event()
    whisper.segments = [(float(second), second + 1.0, f" 第{second}句") for second in range(5)]
    whisper.on_segment = lambda index: stop.set() if index == 2 else None
    result = transcribe(clip, should_stop=stop.is_set, language="zh")
    assert result.stopped and not result.error
    assert sorted(path.name for path in clip.parent.iterdir()) == [clip.name]


def test_without_faster_whisper_the_owner_is_sent_to_the_system_page(
    whisper: FakeWhisper, clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing() -> Any:
        raise ImportError("No module named 'faster_whisper'")

    monkeypatch.setattr(speech, "import_whisper", missing)
    result = transcribe(clip, language="zh")
    assert "系統" in result.error and speech.INSTALL_BUTTON_LABEL in result.error
    assert speech.INSTALL_BUTTON_LABEL == "安裝語音辨識（下載約 125 MB）"


def test_the_cpu_runs_eight_threads_at_most_with_int8(whisper: FakeWhisper, clip: Path, tmp_path: Path) -> None:
    transcribe(clip, language="zh", gpu=True)
    download_root = str(tmp_path / "data" / "models" / "whisper")
    threads = min(8, os.cpu_count() or 1)
    assert whisper.models == [
        {"size": "small", "device": "cpu", "compute_type": "int8", "cpu_threads": threads, "download_root": download_root, "local_files_only": False}
    ]


@pytest.mark.parametrize("fails_when", ["loading", "recognizing"])
def test_a_gpu_that_fails_hands_over_to_the_cpu_with_a_warning(
    whisper: FakeWhisper, clip: Path, monkeypatch: pytest.MonkeyPatch, fails_when: str
) -> None:
    monkeypatch.setattr(speech, "cuda_device_count", lambda: 1)
    whisper.broken_devices = {"cuda"}
    whisper.fails_when = fails_when
    progress = Progress()
    result = transcribe(clip, progress=progress, language="zh", gpu=True, model="medium")
    assert [(model["device"], model["compute_type"]) for model in whisper.models] == [("cuda", "float16"), ("cpu", "int8")]
    assert result.output_path == clip.with_name("Lesson 1. 開場.zh-TW.srt")
    assert "顯示卡" in result.warning and "使用顯示卡" in result.warning
    assert "改用處理器" in " ".join(progress.texts)


def test_without_cublas_the_gpu_is_skipped_and_the_system_page_named(whisper: FakeWhisper, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(speech, "cuda_device_count", lambda: 1)
    monkeypatch.setattr(speech, "cuda_libraries_ready", lambda: False)
    progress = Progress()
    result = transcribe(clip, progress=progress, language="zh", gpu=True)
    # Straight to the processor: the GPU could only fail.
    assert [model["device"] for model in whisper.models] == ["cpu"]
    assert result.output_path == clip.with_name("Lesson 1. 開場.zh-TW.srt")
    assert "系統" in result.warning and speech.GPU_INSTALL_BUTTON_LABEL in result.warning
    assert not any("顯示卡加速失敗" in text for text in progress.texts)


@pytest.fixture
def nvidia_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """pip's nvidia-cublas-cu12 in a folder of its own, with the loading recorded instead of done."""
    import importlib.util

    package = tmp_path / "site-packages" / "nvidia" / "cublas"
    folder = package / ("bin" if sys.platform == "win32" else "lib")
    machine: dict[str, Any] = {"folder": folder, "loaded": [], "dll_folders": [], "system": False}
    real_find_spec = importlib.util.find_spec

    def find_spec(name: str, *args: Any) -> Any:
        if name == "nvidia.cublas":
            return SimpleNamespace(submodule_search_locations=[str(package)]) if package.is_dir() else None
        return real_find_spec(name, *args)

    def load_library(name: str) -> None:
        # A bare name is the system's own search, which finds a CUDA installed with the driver or toolkit.
        if Path(name).is_absolute() and not Path(name).is_file() or not Path(name).is_absolute() and not machine["system"]:
            raise OSError(f"could not load {name}")
        machine["loaded"].append(name)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    monkeypatch.setattr(speech, "load_library", load_library)
    monkeypatch.setattr(os, "add_dll_directory", lambda path: machine["dll_folders"].append(path), raising=False)
    speech.cuda_libraries_ready.cache_clear()
    yield machine
    speech.cuda_libraries_ready.cache_clear()


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS has no CUDA")
def test_cublas_from_pip_is_loaded_before_ctranslate2_asks_for_it(nvidia_package: dict[str, Any]) -> None:
    assert not speech.cuda_libraries_ready()
    folder = nvidia_package["folder"]
    folder.mkdir(parents=True)
    names = speech.CUBLAS_LIBRARIES["win32" if sys.platform == "win32" else "linux"]
    for name in names:
        (folder / name).write_bytes(b"x")
    speech.cuda_libraries_ready.cache_clear()
    assert speech.cuda_libraries_ready()
    # cuBLASLt first, which cuBLAS itself needs.
    assert nvidia_package["loaded"] == [str(folder / name) for name in names]
    assert nvidia_package["dll_folders"] == ([str(folder)] if sys.platform == "win32" else [])


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS has no CUDA")
def test_cublas_installed_with_cuda_itself_also_counts(nvidia_package: dict[str, Any]) -> None:
    nvidia_package["system"] = True
    assert speech.cuda_libraries_ready()
    assert nvidia_package["loaded"] == [speech.CUBLAS_LIBRARIES["win32" if sys.platform == "win32" else "linux"][-1]]


def test_a_working_gpu_needs_no_warning(whisper: FakeWhisper, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(speech, "cuda_device_count", lambda: 1)
    result = transcribe(clip, language="zh", gpu=True)
    assert [model["device"] for model in whisper.models] == ["cuda"] and "cpu_threads" not in whisper.models[0]
    assert result.warning == ""
    # Without the box ticked the GPU stays out of it.
    whisper.models.clear()
    transcribe(clip, language="zh")
    assert [model["device"] for model in whisper.models] == ["cpu"]


def test_the_first_use_of_a_model_says_it_downloads_and_how_much(whisper: FakeWhisper, clip: Path, tmp_path: Path) -> None:
    progress = Progress()
    transcribe(clip, progress=progress, language="zh")
    assert "下載語音模型 small（約 480 MB）..." in progress.texts
    assert not speech.is_model_downloaded("small")

    assert whisper.models[-1]["local_files_only"] is False

    downloaded_model(tmp_path / "data", "small", model_bytes=1000)
    progress = Progress()
    transcribe(clip, progress=progress, language="zh")
    assert "載入語音模型 small..." in progress.texts
    assert not any(text.startswith("下載語音模型") for text in progress.texts)
    # Loaded as it is, without asking Hugging Face, so it also works offline.
    assert whisper.models[-1]["local_files_only"] is True
    assert speech.downloaded_models() == {"small": 1024}


def test_a_failed_download_says_to_check_the_network(whisper: FakeWhisper, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def offline(size: str, **options: Any) -> Any:
        raise OSError("Cannot reach https://huggingface.co")

    monkeypatch.setattr(whisper, "WhisperModel", offline)
    result = transcribe(clip, language="zh")
    assert "下載語音模型 small 失敗" in result.error and "網路" in result.error
    assert sorted(path.name for path in clip.parent.iterdir()) == [clip.name]


def test_no_speech_makes_no_file(whisper: FakeWhisper, clip: Path) -> None:
    whisper.segments = [(0.0, 1.0, "  ")]
    result = transcribe(clip, language="auto")
    assert "沒有聽到" in result.error
    assert sorted(path.name for path in clip.parent.iterdir()) == [clip.name]


def test_sound_ffmpeg_cannot_read_is_named(whisper: FakeWhisper, clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unreadable(path: Path, ffmpeg: str, should_stop: Any) -> Any:
        raise speech.SpeechError(f"讀不到「{path.name}」的聲音：Invalid data found when processing input")

    monkeypatch.setattr(speech, "decode_audio", unreadable)
    result = transcribe(clip, language="auto")
    assert "讀不到「Lesson 1. 開場.mp4」的聲音" in result.error
    assert whisper.models == []


def test_one_transcription_at_a_time(whisper: FakeWhisper, clip: Path) -> None:
    """Each loads its own model, up to several gigabytes, and one already keeps the processor busy."""
    progress = Progress()
    with speech._transcription_lock:
        result = transcribe(clip, should_stop=lambda: "等待其他語音辨識完成..." in progress.texts, progress=progress, language="zh")
    assert result.stopped and whisper.models == []


def test_unknown_choices_fall_back_to_the_defaults(whisper: FakeWhisper, clip: Path) -> None:
    transcribe(clip, model="huge", language="xx")
    assert whisper.models[0]["size"] == speech.DEFAULT_MODEL == "small"
    assert whisper.transcribe_options["language"] == "zh"


def test_every_model_offered_says_how_big_it_is() -> None:
    assert list(speech.WHISPER_MODELS) == ["tiny", "base", "small", "medium", "large-v3-turbo", "large-v3"]
    sizes = [choice.size for choice in speech.WHISPER_MODELS.values()]
    assert sizes == ["約 75 MB", "約 145 MB", "約 480 MB", "約 1.5 GB", "約 1.6 GB", "約 3 GB"]
    assert all(choice.caption for choice in speech.WHISPER_MODELS.values())
    assert list(speech.SPEECH_LANGUAGES) == ["auto", "zh", "en", "ja", "ko"]


def test_turbo_is_recommended_only_with_a_usable_gpu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEOCAP_DATA_DIR", str(tmp_path / "data"))
    assert speech.recommended_model(gpu=True) == "large-v3-turbo"
    assert speech.recommended_model(gpu=False) == speech.DEFAULT_MODEL == "small"
    # Not one of Systran's conversions, so it has its own folder.
    assert speech.model_folder("large-v3-turbo") == speech.models_dir() / "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"
    downloaded_model(tmp_path / "data", "large-v3-turbo")
    assert speech.is_model_downloaded("large-v3-turbo") and not speech.is_model_downloaded("large-v3")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("今天天氣很好,我們一起去公園散步.", "今天天氣很好，我們一起去公園散步。"),
        ("你好嗎?我很好!", "你好嗎？我很好！"),
        ("你好,Hello, world.再見", "你好,Hello, world.再見"),
        ("他用Python 3.12寫的,很快", "他用Python 3.12寫的，很快"),
        ("版本3.5版", "版本3.5版"),
        ("e.g. 例如", "e.g. 例如"),
        ("好,", "好，"),
        (",開頭", ",開頭"),
    ],
)
def test_ascii_punctuation_between_chinese_characters_becomes_full_width(text: str, expected: str) -> None:
    assert speech.full_width_punctuation(text) == expected


def test_chinese_subtitles_get_full_width_punctuation_and_other_languages_keep_theirs(whisper: FakeWhisper, clip: Path) -> None:
    whisper.segments = [(0.0, 2.0, " 今天天气很好,我们去公园散步."), (2.0, 4.0, " 用的是iPhone 15, 对吧?")]
    result = transcribe(clip, language="zh")
    assert cue_texts(result.output_path) == ["今天天氣很好，我們去公園散步。", "用的是iPhone 15, 對吧？"]

    whisper.heard = "en"
    whisper.segments = [(0.0, 2.0, " Hello, world. It costs 3.50 dollars!")]
    result = transcribe(clip, language="auto")
    assert cue_texts(result.output_path) == ["Hello, world. It costs 3.50 dollars!"]


def test_models_live_in_the_data_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEOCAP_DATA_DIR", str(tmp_path / "data"))
    assert speech.models_dir() == tmp_path / "data" / "models" / "whisper"
    assert speech.downloaded_models() == {}
    # A download cut short lacks some of its files.
    snapshot = downloaded_model(tmp_path / "data", "tiny")
    (snapshot / "vocabulary.txt").unlink()
    assert not speech.is_model_downloaded("tiny") and speech.downloaded_models() == {}
    (snapshot / "vocabulary.json").write_text("[]", encoding="utf-8")
    assert speech.is_model_downloaded("tiny")


def test_installed_is_checked_without_importing_it(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    seen: list[str] = []
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: seen.append(name))
    assert not speech.is_installed()
    assert seen == ["faster_whisper"]


@needs_ffmpeg
def test_the_sound_is_decoded_to_16_khz_mono(tmp_path: Path) -> None:
    import subprocess

    song = tmp_path / "stereo song.m4a"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-ac", "2", str(song)],
        check=True,
    )
    audio = speech.decode_audio(song, "ffmpeg", lambda: False)
    assert audio is not None and audio.dtype.name == "float32"
    assert abs(len(audio) - 3 * 16000) < 1600
    assert speech.decode_audio(song, "ffmpeg", lambda: True) is None
    with pytest.raises(speech.SpeechError, match="讀不到「broken.mp3」的聲音"):
        speech.decode_audio(song.with_name("broken.mp3"), "ffmpeg", lambda: False)


# The real thing, once: pytest -m speech with VIDEOCAP_SPEECH_TESTS=1, faster-whisper installed, and
# VIDEOCAP_SPEECH_SAMPLES naming a folder with sample_en.wav ("The quick brown fox jumps over the lazy dog. Subtitles are
# generated from speech.") and sample_zh.wav (「今天天氣很好，我們一起去公園散步。」), as Windows' text-to-speech makes
# them. The tiny model, about 75 MB, is downloaded into VIDEOCAP_DATA_DIR on the first run.

SPEECH_SAMPLES = Path(os.environ.get("VIDEOCAP_SPEECH_SAMPLES", "")) if os.environ.get("VIDEOCAP_SPEECH_SAMPLES") else None


def real_speech_reason() -> str:
    if os.environ.get("VIDEOCAP_SPEECH_TESTS") != "1":
        return "set VIDEOCAP_SPEECH_TESTS=1 to run the real model"
    if not speech.is_installed():
        return "faster-whisper is not installed (pip install -r requirements-speech.txt)"
    if SPEECH_SAMPLES is None or not all((SPEECH_SAMPLES / name).is_file() for name in ("sample_en.wav", "sample_zh.wav")):
        return "set VIDEOCAP_SPEECH_SAMPLES to the folder with sample_en.wav and sample_zh.wav"
    if not shutil.which("ffmpeg"):
        return "ffmpeg is not installed"
    return ""


@pytest.mark.speech
@pytest.mark.skipif(bool(real_speech_reason()), reason=real_speech_reason() or "runs")
@pytest.mark.parametrize("gpu", [False, True], ids=["cpu", "gpu"])
@pytest.mark.parametrize("name", ["sample_en.wav", "sample_zh.wav"])
def test_the_tiny_model_writes_real_subtitles(name: str, gpu: bool, tmp_path: Path) -> None:
    from videocap.media_tools import run_tool

    assert SPEECH_SAMPLES is not None
    if gpu and not (speech.cuda_device_count() and speech.cuda_libraries_ready()):
        pytest.skip("no NVIDIA GPU with cuBLAS for CUDA 12 here")
    source = tmp_path / name
    shutil.copy(SPEECH_SAMPLES / name, source)
    started = time.perf_counter()
    result = run_tool(ToolRequest("transcribe", str(source), {"model": "tiny", "language": "auto", "gpu": gpu}), Progress())
    seconds = time.perf_counter() - started
    assert not result.error, result.error
    # Any fallback to the processor says so, so no warning means CUDA did the work.
    assert result.warning == "", result.warning
    assert result.output_path is not None
    text = " ".join(cue_texts(result.output_path))
    print(f"\n{name} on {'cuda' if gpu else 'cpu'}: {seconds:.1f} s, {result.output_path.name}: {text}")
    if name == "sample_en.wav":
        assert result.output_path.name == "sample_en.en.srt"
        assert "fox" in text.lower() or "lazy dog" in text.lower()
    else:
        assert result.output_path.name == "sample_zh.zh-TW.srt"
        assert any("一" <= char <= "鿿" for char in text)

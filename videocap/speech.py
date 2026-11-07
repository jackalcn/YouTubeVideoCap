"""語音辨識字幕: subtitles for what is said in a video or audio file, with faster-whisper (Whisper on CTranslate2,
without torch).

faster-whisper is an optional install of about 125 MB (requirements-speech.txt, or the 系統 page's button), so it is
imported only when a transcription runs, and the pages ask is_installed() first. Models come from Hugging Face on
first use and stay in the data folder, so Docker keeps them in /data.
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional

import numpy

from videocap.chinese import TRADITIONAL_LANGUAGE, ChineseConversionUnavailable, to_traditional
from videocap.config import get_data_dir
from videocap.media_tools import GPU_FALLBACK_TEXT, TOOL_LABELS, ToolProgress, ToolRequest, ToolResult, summarize_ffmpeg_error
from videocap.processes import end_process_tree, start_tied_process
from videocap.subtitles import Cue, cues_to_srt, is_cjk
from videocap.tooling import NO_WINDOW


@dataclass(frozen=True)
class WhisperChoice:
    size: str
    caption: str
    # The Hugging Face repository faster-whisper downloads it from, when it is not one of Systran's conversions.
    repo: str = ""


# Roughly how much each one downloads, and what it is like. tiny was measured at about 9 times real time on a 2018
# laptop processor (i7-8750H, 8 threads); the others are scaled from that by faster-whisper's own benchmarks.
WHISPER_MODELS = {
    "tiny": WhisperChoice("約 75 MB", "最快：只用處理器時，1 小時的影片約 5～10 分鐘。但錯字最多，中文常聽錯，適合先試試看。"),
    "base": WhisperChoice("約 145 MB", "很快：只用處理器時，1 小時的影片約 10～15 分鐘。比 tiny 準一些，中文仍常有錯字。"),
    "small": WhisperChoice("約 480 MB", "速度和準確度最平衡，中文大致可用。只用處理器時，1 小時的影片約 20～40 分鐘。"),
    "medium": WhisperChoice("約 1.5 GB", "比 small 準確，但慢上兩、三倍：只用處理器時，可能和影片本身一樣長。"),
    # large-v3 with 4 decoder layers instead of 32: the same listening, much less writing.
    "large-v3-turbo": WhisperChoice(
        "約 1.6 GB",
        "準確度接近 large-v3，速度卻快好幾倍，有顯示卡時建議使用。只用處理器時仍然慢，可能和影片本身一樣長。",
        repo="mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    ),
    "large-v3": WhisperChoice("約 3 GB", "最準確，也最慢：只用處理器時，可能要影片長度的兩、三倍，建議搭配顯示卡。"),
}
DEFAULT_MODEL = "small"
GPU_MODEL = "large-v3-turbo"
SPEECH_LANGUAGES = {"auto": "自動偵測", "zh": "中文", "en": "英文", "ja": "日文", "ko": "韓文"}
# What Whisper may also hear when left to detect the language, for the status line and warnings.
LANGUAGE_NAMES = {
    **SPEECH_LANGUAGES,
    "yue": "粵語", "es": "西班牙文", "fr": "法文", "de": "德文", "ru": "俄文", "th": "泰文", "vi": "越南文", "id": "印尼文",
}
# Whisper tends to write Chinese in Simplified characters; a prompt in Traditional ones nudges it, and OpenCC fixes the rest.
TRADITIONAL_PROMPT = "以下是繁體中文的字幕。"
# Whisper now and then writes its prompt back as if it had been said, mostly over silence or music.
PROMPT_ECHOES = frozenset({"以下是繁體中文的字幕", "以下是繁体中文的字幕"})
INSTALL_BUTTON_LABEL = "安裝語音辨識（下載約 125 MB）"
NOT_INSTALLED_MESSAGE = f"還沒有安裝語音辨識。請到「系統」頁按「{INSTALL_BUTTON_LABEL}」，裝好後不必重新啟動。"
NO_SPEECH_MESSAGE = "沒有聽到說話的聲音，所以沒有產生字幕。"
GPU_FALLBACK_WARNING = "顯示卡無法用來辨識語音，這次改用處理器完成。若每次都這樣，請取消勾選「使用顯示卡」。（原因：{reason}）"
# pip's nvidia-cublas-cu12 with its nvrtc: about 630 MB on Windows and 670 MB on Linux.
GPU_INSTALL_BUTTON_LABEL = "安裝顯示卡支援（下載約 650 MB）"
GPU_MISSING_TEXT = f"這台電腦有 NVIDIA 顯示卡，但還沒有顯示卡支援。到「系統」頁按「{GPU_INSTALL_BUTTON_LABEL}」後，就能用顯示卡辨識，快很多。"
GPU_MISSING_WARNING = f"這台電腦還沒有顯示卡支援，這次改用處理器完成。到「系統」頁按「{GPU_INSTALL_BUTTON_LABEL}」後，就能用顯示卡辨識。"
# cuBLAS for CUDA 12, the one library CTranslate2 loads on the GPU besides the driver; cuBLASLt first, which cuBLAS needs.
CUBLAS_LIBRARIES = {"win32": ("cublasLt64_12.dll", "cublas64_12.dll"), "linux": ("libcublasLt.so.12", "libcublas.so.12")}
# Whisper writes Chinese with ASCII punctuation now and then, as in "今天天氣很好,我們".
FULL_WIDTH_PUNCTUATION = {",": "，", ".": "。", "?": "？", "!": "！"}
# Whisper hears 16 kHz mono.
SAMPLE_RATE = 16000
# The language is told from the first speech in this much of the file; voice detection over a long file takes a while.
LANGUAGE_DETECTION_SECONDS = 600
# Beam search as Whisper's own tool does it; a processor runs CTranslate2's int8 on at most this many threads.
BEAM_SIZE = 5
MAX_CPU_THREADS = 8
# Besides vocabulary.txt or vocabulary.json, depending on the model.
MODEL_FILES = ("config.json", "model.bin", "tokenizer.json")

# Each transcription loads its own model, up to several gigabytes, and one already keeps the processor busy.
_transcription_lock = threading.Lock()


class SpeechError(Exception):
    """A step that failed, said in words for the person."""


@dataclass
class Transcript:
    # The language Whisper wrote in, and the one it heard, which differ when the person chose one.
    language: str
    detected: str
    cues: list[Cue]


def is_installed() -> bool:
    """Whether faster-whisper can be imported, without importing it: that takes a second and loads CTranslate2."""
    return importlib.util.find_spec("faster_whisper") is not None


def import_whisper() -> Any:
    # On Windows without symlinks huggingface_hub warns that its cache takes more space; it works all the same.
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    import faster_whisper

    return faster_whisper


def cuda_device_count() -> int:
    """How many NVIDIA GPUs CTranslate2 sees. Needs only the driver; running a model also needs CUDA's libraries."""
    try:
        return _cuda_device_count()
    except ImportError:
        return 0


@lru_cache(maxsize=1)
def _cuda_device_count() -> int:
    import ctranslate2

    return int(ctranslate2.get_cuda_device_count())


def nvidia_library_folders() -> list[Path]:
    """Where pip's nvidia-cublas-cu12 keeps its libraries: nvidia/cublas/bin on Windows, nvidia/cublas/lib on Linux."""
    try:
        spec = importlib.util.find_spec("nvidia.cublas")
    except ImportError:
        return []
    folder = "bin" if sys.platform == "win32" else "lib"
    locations = spec.submodule_search_locations if spec else None
    return [Path(location) / folder for location in locations or [] if (Path(location) / folder).is_dir()]


def load_library(name: str) -> None:
    """Loads a library into this process, from a full path or by name through the system's own search."""
    if sys.platform != "win32":
        # Global, as CTranslate2 would have loaded it.
        ctypes.CDLL(name, mode=ctypes.RTLD_GLOBAL)
    elif os.path.isabs(name):
        ctypes.CDLL(name)
    else:
        # LoadLibrary's standard search, PATH included, as CTranslate2 itself searches.
        ctypes.CDLL(name, winmode=0)


@lru_cache(maxsize=1)
def cuda_libraries_ready() -> bool:
    """Whether cuBLAS can be loaded, and loads it ahead of CTranslate2, which asks for it by name ("cublas64_12.dll",
    "libcublas.so.12"): the system then hands it the copy already loaded. Neither Windows' nor Linux's loader looks in
    site-packages, where pip's nvidia-cublas-cu12 puts it; a CUDA installed on the system is found as usual.
    Remembered, so call cuda_libraries_ready.cache_clear() after installing it."""
    names = CUBLAS_LIBRARIES.get("win32" if sys.platform == "win32" else "linux" if sys.platform.startswith("linux") else "")
    if not names:
        return False
    for folder in nvidia_library_folders():
        if sys.platform == "win32":
            os.add_dll_directory(str(folder))
        try:
            for name in names:
                load_library(str(folder / name))
        except OSError:
            continue
        return True
    try:
        load_library(names[-1])
    except OSError:
        return False
    return True


def recommended_model(gpu: bool) -> str:
    return GPU_MODEL if gpu else DEFAULT_MODEL


def cuda_compute_type() -> str:
    """float16 where the GPU computes it well; older cards, such as the GTX 10 series, only take int8 or float32."""
    import ctranslate2

    supported = ctranslate2.get_supported_compute_types("cuda")
    return next((kind for kind in ("float16", "int8_float16", "int8") if kind in supported), "float32")


def models_dir() -> Path:
    return get_data_dir() / "models" / "whisper"


def model_folder(model: str) -> Path:
    # huggingface_hub's cache layout, which faster-whisper downloads into.
    repo = WHISPER_MODELS[model].repo or f"Systran/faster-whisper-{model}"
    return models_dir() / f"models--{repo.replace('/', '--')}"


def is_model_downloaded(model: str) -> bool:
    """Whether a version of the model, in snapshots/<commit>, has every file faster-whisper loads. A download cut
    short lacks some, and a model found here is loaded without asking Hugging Face."""
    return any(
        all((snapshot / name).is_file() for name in MODEL_FILES) and any(snapshot.glob("vocabulary.*"))
        for snapshot in model_folder(model).glob("snapshots/*")
    )


def downloaded_models() -> dict[str, int]:
    """The offered models on disk, with the bytes each takes."""
    return {model: folder_size(model_folder(model)) for model in WHISPER_MODELS if is_model_downloaded(model)}


def folder_size(folder: Path) -> int:
    # lstat, so a snapshot's symlink is not counted on top of the file it points to.
    return sum(os.lstat(os.path.join(root, name)).st_size for root, _, names in os.walk(folder) for name in names)


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get(code, code)


def bare_text(text: str) -> str:
    return re.sub(r"[\W_]+", "", text)


def full_width_punctuation(text: str) -> str:
    """ASCII , . ? ! right after a Chinese character, and before another one or at the end, as ，。？！. Beside Latin
    letters or digits, as in "Python 3.12, iPhone", it stays as it is."""
    return "".join(
        FULL_WIDTH_PUNCTUATION[char]
        if char in FULL_WIDTH_PUNCTUATION and index and is_cjk(text[index - 1]) and (index + 1 == len(text) or is_cjk(text[index + 1]))
        else char
        for index, char in enumerate(text)
    )


def decode_audio(path: Path, ffmpeg: str, should_stop: Callable[[], bool]) -> Optional[numpy.ndarray]:
    """The first audio track as 16 kHz mono floats, which is what Whisper takes, or None when stopped. Decoded by the
    app's own ffmpeg: faster-whisper's decoder broke with PyAV 19, and ffmpeg reads every file the other tools do."""
    command = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(path),
        "-map", "0:a:0", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-",
    ]
    # ffmpeg's messages go to a file: a full pipe nobody reads would make it hang.
    with tempfile.TemporaryFile() as messages:
        try:
            process = start_tied_process(
                command, stdout=subprocess.PIPE, stderr=messages, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW
            )
        except OSError as exc:
            raise SpeechError(f"讀不到「{path.name}」的聲音：{exc}") from exc
        assert process.stdout is not None
        samples = bytearray()
        while chunk := process.stdout.read(1 << 20):
            if should_stop():
                end_process_tree(process)
                process.wait()
                return None
            samples += chunk
        process.wait()
        if process.returncode != 0:
            messages.seek(0)
            lines = messages.read().decode("utf-8", "replace").splitlines()
            raise SpeechError(f"讀不到「{path.name}」的聲音。{summarize_ffmpeg_error(lines)}")
    return numpy.frombuffer(samples, dtype=numpy.float32)


def load_model(whisper: Any, model: str, gpu: bool, progress_ui: ToolProgress) -> Any:
    downloaded = is_model_downloaded(model)
    progress_ui.progress(0.0, text=f"載入語音模型 {model}..." if downloaded else f"下載語音模型 {model}（{WHISPER_MODELS[model].size}）...")
    try:
        if gpu:
            device: dict[str, Any] = {"device": "cuda", "compute_type": cuda_compute_type()}
        else:
            device = {"device": "cpu", "compute_type": "int8", "cpu_threads": min(MAX_CPU_THREADS, os.cpu_count() or 1)}
        # A model on disk is loaded as it is: asking Hugging Face for a newer copy takes a second or two, and fails offline.
        return whisper.WhisperModel(model, download_root=str(models_dir()), local_files_only=downloaded, **device)
    except Exception as exc:
        if downloaded:
            raise SpeechError(f"載入語音模型 {model} 失敗：{exc}") from exc
        raise SpeechError(f"下載語音模型 {model} 失敗，請確認網路連線後再試一次。（{exc}）") from exc


def run_whisper(
    whisper: Any, audio: Any, model_name: str, language: str, gpu: bool, progress_ui: ToolProgress, should_stop: Callable[[], bool]
) -> Optional[Transcript]:
    """One go on one device; None when stopped."""
    model = load_model(whisper, model_name, gpu, progress_ui)
    if should_stop():
        return None
    progress_ui.progress(0.0, text="偵測語言...")
    try:
        # Told apart here even when a language was chosen, so a wrong choice can be named afterwards.
        detected = str(model.detect_language(audio[: LANGUAGE_DETECTION_SECONDS * SAMPLE_RATE], vad_filter=True)[0])
        spoken = detected if language == "auto" else language
        segments, info = model.transcribe(
            audio,
            language=spoken,
            beam_size=BEAM_SIZE,
            # Skips silence and music, where Whisper otherwise makes up words.
            vad_filter=True,
            initial_prompt=TRADITIONAL_PROMPT if spoken == "zh" else None,
        )
        cues: list[Cue] = []
        for segment in segments:
            if should_stop():
                return None
            text = segment.text.strip()
            if spoken == "zh":
                text = full_width_punctuation(text)
            if text and bare_text(text) not in PROMPT_ECHOES:
                cues.append(Cue(float(segment.start), float(segment.end), (text,)))
            fraction = min(float(segment.end) / info.duration, 1.0) if info.duration else 0.0
            progress_ui.progress(fraction, text=f"辨識中 {fraction:.0%}（語言：{language_name(spoken)}）")
    except Exception as exc:
        # Also a GPU that loaded the model but cannot run it: a missing cuBLAS only shows at the first encode.
        raise SpeechError(f"語音辨識失敗：{exc}") from exc
    return Transcript(spoken, detected, cues)


def recognize(
    whisper: Any, audio: Any, model: str, language: str, gpu: bool, progress_ui: ToolProgress, should_stop: Callable[[], bool]
) -> tuple[Optional[Transcript], list[str]]:
    """The transcript, on the GPU when asked and on the processor when the GPU fails, with the warnings to tell."""
    if not gpu:
        return run_whisper(whisper, audio, model, language, False, progress_ui, should_stop), []
    if not cuda_libraries_ready():
        # Without cuBLAS the GPU can only fail, and only after the whole model went onto the card.
        return run_whisper(whisper, audio, model, language, False, progress_ui, should_stop), [GPU_MISSING_WARNING]
    try:
        return run_whisper(whisper, audio, model, language, True, progress_ui, should_stop), []
    except SpeechError as exc:
        # Missing cuDNN or cuBLAS libraries are common on Windows, and an older card cannot compute float16.
        reason = str(exc.__cause__ or exc)[:200]
    progress_ui.progress(0.0, text=GPU_FALLBACK_TEXT)
    transcript = run_whisper(whisper, audio, model, language, False, progress_ui, should_stop)
    return transcript, [GPU_FALLBACK_WARNING.format(reason=reason)]


def wait_for_turn(progress_ui: ToolProgress, should_stop: Callable[[], bool]) -> bool:
    """Takes the transcription lock, waiting for another transcription to finish; False when stopped meanwhile."""
    if _transcription_lock.acquire(blocking=False):
        return True
    progress_ui.progress(0.0, text="等待其他語音辨識完成...")
    while not _transcription_lock.acquire(timeout=0.5):
        if should_stop():
            return False
    return True


def transcribe(
    request: ToolRequest, ffmpeg: str, progress_ui: ToolProgress, should_stop: Optional[Callable[[], bool]] = None
) -> ToolResult:
    """Writes subtitles for what is said in request.input_path, a full path, as an SRT file beside it. Runs in a job's
    thread, and stops between Whisper's segments when asked."""
    stop = should_stop or (lambda: False)
    try:
        whisper = import_whisper()
    except ImportError:
        return ToolResult(error=NOT_INSTALLED_MESSAGE)
    params = request.params
    model = str(params.get("model") or DEFAULT_MODEL)
    model = model if model in WHISPER_MODELS else DEFAULT_MODEL
    language = str(params.get("language") or "auto")
    language = language if language in SPEECH_LANGUAGES else "auto"
    gpu = bool(params.get("gpu")) and cuda_device_count() > 0
    source = Path(request.input_path)

    if not wait_for_turn(progress_ui, stop):
        return ToolResult(stopped=True)
    try:
        progress_ui.progress(0.0, text="讀取聲音...")
        audio = decode_audio(source, ffmpeg, stop)
        if audio is None:
            return ToolResult(stopped=True)
        transcript, warnings = recognize(whisper, audio, model, language, gpu, progress_ui, stop)
    except SpeechError as exc:
        return ToolResult(error=str(exc))
    finally:
        _transcription_lock.release()
    if transcript is None:
        return ToolResult(stopped=True)
    return save_transcript(source, transcript, language, bool(params.get("traditional", True)), warnings, progress_ui)


def save_transcript(
    source: Path, transcript: Transcript, chosen: str, traditional: bool, warnings: list[str], progress_ui: ToolProgress
) -> ToolResult:
    if not transcript.cues:
        return ToolResult(error=NO_SPEECH_MESSAGE)
    if chosen != "auto" and transcript.detected != chosen:
        warnings.append(
            f"你選的是{language_name(chosen)}，但聽起來比較像{language_name(transcript.detected)}。如果字幕不對，請改選「自動偵測」再辨識一次。"
        )
    content = cues_to_srt(transcript.cues)
    tag = transcript.language
    if tag == "zh" and traditional:
        try:
            content, tag = to_traditional(content), TRADITIONAL_LANGUAGE
        except ChineseConversionUnavailable as exc:
            warnings.append(str(exc))
    try:
        output_path = write_new_subtitles(source, tag, content)
    except OSError as exc:
        return ToolResult(error=f"無法寫入字幕檔：{exc}")
    warning = " ".join(warnings)
    label = TOOL_LABELS["transcribe"]
    progress_ui.progress(1.0, text=f"{label}完成，但{warning}" if warning else f"{label}完成")
    return ToolResult(output_path=output_path, warning=warning)


def write_new_subtitles(source: Path, language: str, content: str) -> Path:
    """Saves "X.zh-TW.srt" beside "X.mp4", or "X.zh-TW-ai.srt", "X.zh-TW-ai2.srt" and so on when that is taken, as by
    the video's own subtitles from YouTube: never over an existing file. The language part stays last before ".srt",
    where players and the library look for it, so the library pairs each of them with the video."""
    for number in range(1, 100):
        tag = language if number == 1 else f"{language}-ai" if number == 2 else f"{language}-ai{number - 1}"
        path = source.with_name(f"{source.stem}.{tag}.srt")
        try:
            # Claims the name, so two transcriptions of one file finishing together never share it.
            file = path.open("x", encoding="utf-8")
        except FileExistsError:
            continue
        try:
            with file:
                file.write(content)
        except OSError:
            path.unlink(missing_ok=True)
            raise
        return path
    raise OSError(f"{source.stem} 已有太多同名字幕檔。")

"""即時字幕引擎（Phase 2c）。

擷取 Windows 系統聲音（WASAPI loopback）→ 能量式 VAD 靜音過濾 →
累積 10–15 秒段落 → Whisper 轉文字 → 現有翻譯管線 → 字幕資料。
"""
import array
import io
import logging
import queue
import threading
import time
import wave
from collections import deque

from agent.config import settings
from agent.llm_client import LLMError
from agent.stt import STTError

logger = logging.getLogger("agent.live")

try:  # Windows-only package; graceful fallback elsewhere.
    import pyaudiowpatch as pyaudio
except ImportError:
    pyaudio = None


class LiveCaptureError(Exception):
    pass


def encode_wav(pcm_bytes: bytes, channels: int, sample_rate: int, sampwidth: int = 2) -> bytes:
    """Wrap raw int16 PCM bytes in a WAV container (in memory)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sampwidth)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm_bytes)
    return buf.getvalue()


def rms_of(chunk: bytes) -> float:
    """Root-mean-square level of an int16 little-endian PCM chunk."""
    samples = array.array("h")
    usable = chunk[: len(chunk) // 2 * 2]
    if not usable:
        return 0.0
    samples.frombytes(usable)
    if not samples:
        return 0.0
    return (sum(s * s for s in samples) / len(samples)) ** 0.5


def detect_lang(text: str) -> str:
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return "zh" if cjk > len(text) * 0.1 else "en"


# Split sentences while keeping the punctuation at the end of each sentence.
_SENTENCE_SPLIT = __import__("re").compile(r"(?<=[。！？；!?;])|(?<=[.!?])\s+")


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(text) if p and p.strip()]
    return parts or ([text.strip()] if text.strip() else [])


class CaptionStore:
    """Thread-safe ring buffer of recent subtitle lines."""

    def __init__(self, maxlen: int = 50):
        self._items: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0

    def add(self, transcript: str, translated_text: str, model: str, source: str, elapsed_ms: int):
        with self._lock:
            self._seq += 1
            self._items.append({
                "seq": self._seq,
                "time": time.strftime("%H:%M:%S"),
                "transcript": transcript,
                "translated_text": translated_text,
                "model": model,
                "source": source,
                "elapsed_ms": elapsed_ms,
            })

    def latest(self, n: int = 8) -> list[dict]:
        with self._lock:
            return list(self._items)[-n:]

    def clear(self):
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


class LoopbackSource:
    """WASAPI loopback capture of the default speakers (Windows only).

    Reads small 20ms blocks: large frames_per_buffer values make the
    Realtek/WASAPI loopback deliver data far slower than realtime.
    """

    READ_SECONDS = 0.02

    def __init__(self, chunk_seconds: float = 0.1):
        self.chunk_seconds = chunk_seconds
        self.channels = 2
        self.sample_rate = 48000
        self._pyaudio = None
        self._stream = None

    def __enter__(self):
        if pyaudio is None:
            raise LiveCaptureError("PyAudioWPatch 未安裝（僅支援 Windows）")
        try:
            self._pyaudio = pyaudio.PyAudio()
            device = self._pyaudio.get_default_wasapi_loopback()
            self.channels = max(1, int(device.get("maxInputChannels", 2)))
            self.sample_rate = int(device.get("defaultSampleRate", 48000))
            frames = max(64, int(self.sample_rate * self.READ_SECONDS))
            self._stream = self._pyaudio.open(
                format=pyaudio.paInt16,
                channels=self.channels,
                rate=self.sample_rate,
                frames_per_buffer=frames,
                input=True,
                input_device_index=int(device["index"]),
            )
            logger.info(
                "Loopback capture started: %s (%d Hz, %d ch)",
                device.get("name", "?"), self.sample_rate, self.channels,
            )
            return self
        except Exception as exc:
            self._cleanup()
            raise LiveCaptureError(f"無法開啟系統聲音擷取：{exc}") from exc

    def read(self) -> bytes:
        return self._stream.read(max(64, int(self.sample_rate * self.READ_SECONDS)), exception_on_overflow=False)

    def _cleanup(self):
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._pyaudio is not None:
            try:
                self._pyaudio.terminate()
            except Exception:
                pass
            self._pyaudio = None

    def __exit__(self, *exc):
        self._cleanup()
        return False


class FakeSource:
    """Deterministic audio source for tests (sine/quiet chunks)."""

    def __init__(self, rate: int = 16000, channels: int = 1, chunk_seconds: float = 0.1,
                 script: list[bytes] | None = None, tail: bytes = b""):
        self.sample_rate = rate
        self.channels = channels
        self.chunk_seconds = chunk_seconds
        self._script = list(script or [])
        self._tail = tail

    def __enter__(self):
        return self

    def read(self) -> bytes:
        if self._script:
            return self._script.pop(0)
        return self._tail

    def __exit__(self, *exc):
        return False


class LiveCaptionEngine:
    """Captures system audio, segments speech, transcribes and translates."""

    def __init__(
        self,
        stt_client,
        service,
        source_factory=None,
        store: CaptionStore | None = None,
        chunk_seconds: float = 0.1,
        min_segment: float | None = None,
        max_segment: float | None = None,
        hangover: float = 1.5,
        min_speech: float = 2.0,
        target_lang: str | None = None,
    ):
        self.stt_client = stt_client
        self.service = service
        self.source_factory = source_factory or (lambda: LoopbackSource(chunk_seconds))
        self.store = store if store is not None else CaptionStore()
        self.chunk_seconds = chunk_seconds
        self.min_segment = min_segment if min_segment is not None else settings.live_min_segment
        self.max_segment = max_segment if max_segment is not None else settings.live_max_segment
        self.hangover_frames = max(1, int(hangover / chunk_seconds))
        self.min_speech_frames = max(1, int(min_speech / chunk_seconds))
        self.target_lang = target_lang or settings.live_target_lang
        self._queue: queue.Queue = queue.Queue(maxsize=4)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._state_lock = threading.Lock()
        self._running = False
        self._error: str | None = None

    # -- lifecycle ---------------------------------------------------------
    @property
    def running(self) -> bool:
        with self._state_lock:
            return self._running

    @property
    def last_error(self) -> str | None:
        return self._error

    def start(self):
        with self._state_lock:
            if self._running:
                return
            self._stop.clear()
            self._error = None
            self._running = True
        self.store.clear()
        capture = threading.Thread(target=self._capture_loop, daemon=True, name="live-capture")
        worker = threading.Thread(target=self._worker_loop, daemon=True, name="live-worker")
        self._threads = [capture, worker]
        capture.start()
        worker.start()

    def stop(self):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=10)
        self._threads = []
        with self._state_lock:
            self._running = False

    # -- capture -----------------------------------------------------------
    def _flush(self, buffer: list[bytes], channels: int, sample_rate: int):
        duration = len(buffer) * self.chunk_seconds
        pcm = b"".join(buffer)
        buffer.clear()
        if len(pcm) < self.min_speech_frames * int(sample_rate * self.chunk_seconds) * channels * 2:
            return  # too short to be real speech
        wav_bytes = encode_wav(pcm, channels, sample_rate)
        try:
            self._queue.put_nowait((wav_bytes, duration))
        except queue.Full:
            # Stay realtime: drop the oldest pending segment.
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait((wav_bytes, duration))
            except queue.Full:
                logger.warning("Live segment dropped (worker too slow)")

    def _capture_loop(self):
        hangover_silence = 0
        try:
            with self.source_factory() as source:
                channels, rate = source.channels, source.sample_rate
                # Small reads (20ms) aggregated into ~100ms VAD frames.
                frame_bytes = int(rate * self.chunk_seconds) * channels * 2
                pending = b""
                buffer: list[bytes] = []
                speech_seen = False
                noise_floor = 0.0
                calibration = []
                threshold = None
                window_start = time.perf_counter()
                window_audio = 0.0

                while not self._stop.is_set():
                    try:
                        chunk = source.read()
                    except Exception as exc:
                        self._error = f"擷取系統聲音失敗：{exc}"
                        logger.error(self._error)
                        break

                    pending += chunk
                    while len(pending) >= frame_bytes:
                        frame, pending = pending[:frame_bytes], pending[frame_bytes:]

                        # Watchdog: verify capture keeps up with realtime.
                        window_audio += self.chunk_seconds
                        wall = time.perf_counter()
                        if wall - window_start >= 10.0:
                            elapsed = wall - window_start
                            ratio = window_audio / elapsed
                            if ratio < 0.8:
                                logger.warning(
                                    "Capture running behind: %.2fx realtime (%.1fs audio in %.1fs wall)",
                                    ratio, window_audio, elapsed,
                                )
                            else:
                                logger.info("Capture rate: %.2fx realtime", ratio)
                            window_start = wall
                            window_audio = 0.0

                        level = rms_of(frame)

                        if threshold is None:
                            calibration.append(level)
                            if len(calibration) >= 10:  # ~1s ambient calibration
                                ordered = sorted(calibration)
                                noise_floor = ordered[len(ordered) // 2]
                                threshold = max(noise_floor * 3.0, noise_floor + 200.0)
                                logger.info("VAD calibrated: noise floor %.0f, threshold %.0f", noise_floor, threshold)
                            continue

                        if level < threshold:
                            noise_floor = noise_floor * 0.95 + level * 0.05  # adaptive floor
                            threshold = max(noise_floor * 3.0, noise_floor + 200.0)

                        if level >= threshold:
                            speech_seen = True
                            hangover_silence = 0
                        else:
                            hangover_silence += 1

                        buffer.append(frame)
                        duration = len(buffer) * self.chunk_seconds
                        if duration >= self.max_segment:
                            # Hard cap: flush if the segment contains speech, otherwise drop it.
                            if speech_seen:
                                self._flush(buffer, channels, rate)
                            else:
                                buffer.clear()
                            speech_seen = False
                            hangover_silence = 0
                        elif (
                            speech_seen and duration >= self.min_segment
                            and hangover_silence >= self.hangover_frames
                        ):
                            self._flush(buffer, channels, rate)
                            speech_seen = False
                            hangover_silence = 0
        except LiveCaptureError as exc:
            self._error = str(exc)
            logger.error(self._error)
        except Exception as exc:
            self._error = f"即時字幕擷取執行緒異常：{exc}"
            logger.exception(self._error)
        finally:
            with self._state_lock:
                self._running = False

    # -- worker ------------------------------------------------------------
    def _worker_loop(self):
        while not self._stop.is_set():
            try:
                wav_bytes, duration = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._process_segment(wav_bytes, duration)
            except Exception:
                logger.exception("Live segment processing failed")

    def _process_segment(self, wav_bytes: bytes, duration: float):
        try:
            transcript = self.stt_client.transcribe(wav_bytes, filename="segment.wav").strip()
        except STTError as exc:
            logger.warning("Live STT failed (%.1fs segment): %s", duration, exc)
            return
        if not transcript:
            return

        source_lang = detect_lang(transcript)
        if source_lang == self.target_lang:
            self.store.add(transcript, transcript, "passthrough", "passthrough", 0)
            return

        try:
            result = self.service.translate(transcript, source_lang, self.target_lang)
        except LLMError as exc:
            logger.warning("Live translation failed: %s", exc)
            self.store.add(transcript, f"（翻譯失敗：{exc}）", "error", "error", 0)
            return

        # Show the translation sentence by sentence (subtitle rhythm).
        translation_parts = split_sentences(result["translated_text"])
        transcript_parts = split_sentences(transcript)
        if len(translation_parts) > 1:
            for index, part in enumerate(translation_parts):
                src = (
                    transcript_parts[index]
                    if len(transcript_parts) == len(translation_parts)
                    else (transcript if index == 0 else "")
                )
                self.store.add(src, part, result["model"], result["source"], result["elapsed_ms"])
        else:
            self.store.add(
                transcript, result["translated_text"], result["model"], result["source"], result["elapsed_ms"],
            )
        logger.info(
            "Live caption ready (%.1fs audio, %d lines): %s...",
            duration, len(translation_parts), result["translated_text"][:40],
        )

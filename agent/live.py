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
            try:
                logger.info("PortAudio version: %s", self._pyaudio.get_portaudio_version_text())
            except Exception:
                pass
            # 自行掃描裝置清單，不依賴 get_default_wasapi_loopback()：
            # 打包後的 exe 曾選到沒有輸入通道的裝置（-9996 Invalid device）。
            device = self._find_loopback_device()
            self._log_device_details(device)
            # exe 環境下 Pa_OpenStream 曾在裝置正確時回報 -9996（底層 WASAPI
            # 錯誤碼被官方 wheel 關閉的除錯輸出吃掉），故用多組參數
            # 依序回退：哪組成功就用哪組，並記錄每一次回退的結果。
            last_exc = None
            for label, kwargs in self._build_open_attempts(device):
                try:
                    self._stream = self._pyaudio.open(**kwargs)
                    self.channels = int(kwargs["channels"])
                    self.sample_rate = int(kwargs["rate"])
                    logger.info(
                        "Loopback capture started (%s): %s (index %s, %d Hz, %d ch)",
                        label, device.get("name", "?"), int(kwargs["input_device_index"]),
                        self.sample_rate, self.channels,
                    )
                    return self
                except Exception as exc:
                    last_exc = exc
                    logger.error("Loopback open failed [%s]: %s", label, exc)
            if last_exc is None:  # 沒有任何可嘗試的參數組合
                raise LiveCaptureError("沒有可嘗試的音訊開啟參數")
            raise last_exc
        except Exception as exc:
            self._log_audio_devices()
            self._cleanup()
            raise LiveCaptureError(f"無法開啟系統聲音擷取：{exc}") from exc

    def _build_open_attempts(self, device: dict):
        """Return labelled open() parameter sets, best guess first."""
        base = {
            "format": pyaudio.paInt16,
            "input": True,
            "input_device_index": int(device["index"]),
        }

        def params(index, channels, rate, frames):
            kwargs = dict(base)
            kwargs["input_device_index"] = int(index)
            kwargs["channels"] = channels
            kwargs["rate"] = rate
            if frames is not None:
                kwargs["frames_per_buffer"] = frames
            label = "index %s, %d Hz, %d ch, fpb %s" % (index, rate, channels, frames)
            return (label, kwargs)

        index = int(device["index"])
        channels = max(1, int(device.get("maxInputChannels", 2)))
        rate = int(float(device.get("defaultSampleRate", 48000)))
        attempts = [
            params(index, channels, rate, max(64, int(rate * self.READ_SECONDS))),
            # 官方 README 範例的寫法（frames_per_buffer = 樣組大小）
            params(index, channels, rate, pyaudio.get_sample_size(pyaudio.paInt16)),
            # 交給 PortAudio 自行決定緩衝區大小
            params(index, channels, rate, None),
        ]
        # 函式庫自己的預設 loopback（官方建議路徑）
        try:
            lib_device = self._pyaudio.get_default_wasapi_loopback()
            lib_index = int(lib_device["index"])
            if lib_index != index:
                attempts.append(params(
                    lib_index,
                    max(1, int(lib_device.get("maxInputChannels", 2))),
                    int(float(lib_device.get("defaultSampleRate", 48000))),
                    None,
                ))
        except Exception as exc:
            logger.error("get_default_wasapi_loopback failed: %s", exc)
        # 其餘 loopback 候選裝置
        for info in self._loopback_candidates():
            cand_index = int(info["index"])
            if cand_index == index:
                continue
            attempts.append(params(
                cand_index,
                max(1, int(info.get("maxInputChannels", 2))),
                int(float(info.get("defaultSampleRate", 48000))),
                None,
            ))
        return attempts

    def _log_device_details(self, device: dict):
        """Log every field of the chosen device for dev-vs-exe comparison."""
        try:
            logger.error("Chosen device full info: %r", dict(device))
        except Exception:
            pass

    def _find_loopback_device(self) -> dict:
        """Find a usable '[Loopback]' input device, preferring WASAPI + 48kHz."""
        try:
            wasapi_index = int(
                self._pyaudio.get_host_api_info_by_type(pyaudio.paWASAPI).get("index", -1)
            )
        except Exception:
            wasapi_index = -1
        candidates = self._loopback_candidates()
        if not candidates:
            raise LiveCaptureError("找不到系統聲音迴路裝置（[Loopback]）")
        candidates.sort(
            key=lambda info: (
                int(info.get("hostApi", -1)) == wasapi_index,
                float(info.get("defaultSampleRate", 0)),
            ),
            reverse=True,
        )
        chosen = candidates[0]
        logger.info(
            "Loopback device chosen: %s (index %s, WASAPI %s)",
            chosen.get("name"), chosen.get("index"),
            int(chosen.get("hostApi", -1)) == wasapi_index,
        )
        return chosen

    def _loopback_candidates(self):
        """All devices whose name contains '[Loopback]' with input channels."""
        candidates = []
        for i in range(self._pyaudio.get_device_count()):
            try:
                info = dict(self._pyaudio.get_device_info_by_index(i))
            except Exception:
                continue
            if "[Loopback]" not in str(info.get("name", "")):
                continue
            if int(info.get("maxInputChannels", 0)) <= 0:
                continue  # 沒有輸入通道的裝置開啟必失敗
            candidates.append(info)
        return candidates

    def _log_audio_devices(self):
        """Dump every audio device when opening fails, for diagnosis."""
        if self._pyaudio is None:
            return
        try:
            logger.error("Audio devices found (%d):", self._pyaudio.get_device_count())
            for i in range(self._pyaudio.get_device_count()):
                info = self._pyaudio.get_device_info_by_index(i)
                logger.error(
                    "  device %d: %s (in %s, out %s, %s Hz)",
                    i, info.get("name"), info.get("maxInputChannels"),
                    info.get("maxOutputChannels"), info.get("defaultSampleRate"),
                )
        except Exception:
            pass

    def read(self) -> bytes:
        return self._stream.read(max(64, int(self.sample_rate * self.READ_SECONDS)), exception_on_overflow=False)

    def read_available(self) -> int:
        """Frames waiting unread in the audio buffer (0 if unknown)."""
        try:
            return self._stream.get_read_available()
        except Exception:
            return 0

    def drain(self, frames_to_drop: int):
        """Read and discard stale frames so latency never accumulates."""
        while frames_to_drop > 0 and self._stream is not None:
            n = min(frames_to_drop, int(self.sample_rate * 0.5))
            try:
                self._stream.read(n, exception_on_overflow=False)
            except Exception:
                return
            frames_to_drop -= n

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

    def read_available(self) -> int:
        return 0

    def drain(self, frames_to_drop: int):
        pass

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
        catchup_interval: float = 2.0,
        stall_timeout: float | None = None,
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
        self.catchup_interval = catchup_interval
        self.vad_enabled = settings.live_vad_enabled
        self.vad_multiplier = settings.live_vad_multiplier
        # Failsafe: if no segment is flushed for this long, start halving the
        # VAD threshold so a miscalibrated floor can never black out captions.
        self.stall_timeout = (
            stall_timeout if stall_timeout is not None else settings.live_stall_timeout
        )
        # Backlog beyond this is dropped to keep end-to-end latency bounded.
        self.backlog_max_seconds = 0.4
        self.backlog_keep_seconds = 0.2
        self._queue: queue.Queue = queue.Queue(maxsize=2)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._state_lock = threading.Lock()
        self._running = False
        self._error: str | None = None
        # Diagnostics written by the capture thread (read via /live/status or tests).
        self.last_noise_floor: float | None = None
        self.last_threshold: float | None = None

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
                window_peak = 0.0
                last_catchup = window_start
                audio_since_flush = 0.0
                failsafe_factor = 1.0

                def speech_threshold() -> float:
                    # Multiplicative threshold scales with system volume;
                    # the small absolute floors guard against digital silence.
                    return max(
                        noise_floor * self.vad_multiplier,
                        noise_floor + 100.0,
                        100.0,
                    )

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
                                logger.info(
                                    "Capture rate: %.2fx realtime, queue %d, level %.0f / threshold %s",
                                    ratio, self._queue.qsize(), window_peak,
                                    f"{threshold * failsafe_factor:.0f}"
                                    if threshold is not None
                                    else "calibrating",
                                )
                            window_start = wall
                            window_audio = 0.0
                            window_peak = 0.0

                        # Catch-up: drop stale buffered audio so latency stays bounded.
                        if self.catchup_interval and wall - last_catchup >= self.catchup_interval:
                            last_catchup = wall
                            backlog_frames = source.read_available()
                            backlog_seconds = backlog_frames / rate if rate else 0.0
                            if backlog_seconds > self.backlog_max_seconds:
                                excess = backlog_frames - int(rate * self.backlog_keep_seconds)
                                source.drain(excess)
                                logger.info(
                                    "Dropped %.2fs of stale audio (backlog was %.2fs)",
                                    excess / rate if rate else 0.0, backlog_seconds,
                                )

                        level = rms_of(frame)
                        window_peak = max(window_peak, level)

                        if not self.vad_enabled:
                            speech_seen = True  # send every segment regardless of level
                        else:
                            if threshold is None:
                                calibration.append(level)
                                if len(calibration) >= 10:  # ~1s ambient calibration
                                    ordered = sorted(calibration)
                                    noise_floor = ordered[len(ordered) // 2]
                                    threshold = speech_threshold()
                                    audio_since_flush = 0.0
                                    self.last_noise_floor = noise_floor
                                    self.last_threshold = threshold
                                    logger.info(
                                        "VAD calibrated: noise floor %.0f, threshold %.0f",
                                        noise_floor, threshold,
                                    )
                                continue

                            if level < threshold:
                                # Adaptive floor: rise SLOWLY (a loud start must not
                                # absorb speech into the floor) but fall FAST so the
                                # threshold recovers quickly when noise stops.
                                if level > noise_floor:
                                    noise_floor = noise_floor * 0.9975 + level * 0.0025
                                else:
                                    noise_floor = noise_floor * 0.95 + level * 0.05
                                threshold = speech_threshold()
                                self.last_noise_floor = noise_floor
                                self.last_threshold = threshold

                            # Failsafe: when no segment has been flushed for a
                            # while (measured in audio time), halve the effective
                            # threshold every stall_timeout seconds until speech
                            # gets through again. A miscalibrated floor can then
                            # never black out captions for more than a few stalls.
                            audio_since_flush += self.chunk_seconds
                            if self.stall_timeout and audio_since_flush > self.stall_timeout:
                                new_factor = max(
                                    0.5 ** int(audio_since_flush / self.stall_timeout),
                                    0.0625,
                                )
                                if new_factor < failsafe_factor:
                                    failsafe_factor = new_factor
                                    logger.warning(
                                        "No captions for %.1fs of audio, VAD failsafe: "
                                        "threshold lowered to %.0f",
                                        audio_since_flush, threshold * failsafe_factor,
                                    )
                            effective_threshold = threshold * failsafe_factor

                            if level >= effective_threshold:
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
                                audio_since_flush = 0.0
                                failsafe_factor = 1.0
                            else:
                                buffer.clear()
                            speech_seen = False
                            hangover_silence = 0
                        elif (
                            speech_seen and duration >= self.min_segment
                            and hangover_silence >= self.hangover_frames
                        ):
                            self._flush(buffer, channels, rate)
                            audio_since_flush = 0.0
                            failsafe_factor = 1.0
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

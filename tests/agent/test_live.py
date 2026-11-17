import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import array
import math

import pytest

import agent.live as live_module
from agent.live import (
    CaptionStore,
    FakeSource,
    LiveCaptionEngine,
    detect_lang,
    encode_wav,
    rms_of,
)


def quiet_chunk(rate=16000, chunk_seconds=0.05):
    return b"\x00\x00" * int(rate * chunk_seconds)


def loud_chunk(rate=16000, chunk_seconds=0.05, amplitude=8000):
    n = int(rate * chunk_seconds)
    samples = array.array("h", (int(amplitude * math.sin(2 * math.pi * 440 * i / rate)) for i in range(n)))
    return samples.tobytes()


class FakeSTT:
    model = "whisper-large-v3-turbo"

    def __init__(self, text="Hello there."):
        self.text = text
        self.calls = []

    def transcribe(self, audio_bytes, filename="segment.wav", language=None, timeout=None):
        self.calls.append({"size": len(audio_bytes)})
        return self.text

    def close(self):
        pass


class FakeService:
    def translate(self, text, source_lang, target_lang):
        return {
            "translated_text": f"譯文：{text}",
            "source": "cloud",
            "model": "test-model",
            "source_lang": source_lang,
            "target_lang": target_lang,
            "elapsed_ms": 5,
        }


def test_encode_wav_roundtrip():
    pcm = loud_chunk()
    wav = encode_wav(pcm, channels=1, sample_rate=16000)
    import io
    import wave

    with wave.open(io.BytesIO(wav)) as reader:
        assert reader.getnchannels() == 1
        assert reader.getframerate() == 16000
        assert reader.getsampwidth() == 2
        assert reader.getnframes() == 800


def test_rms_of_silence_and_speech():
    assert rms_of(quiet_chunk()) == 0.0
    assert rms_of(loud_chunk()) > 1000.0
    assert rms_of(b"") == 0.0


def test_detect_lang():
    assert detect_lang("Hello world") == "en"
    assert detect_lang("這是中文") == "zh"


def test_caption_store():
    store = CaptionStore()
    store.add("a", "甲", "m", "cloud", 1)
    store.add("b", "乙", "m", "cloud", 2)
    items = store.latest(1)
    assert len(items) == 1
    assert items[0]["transcript"] == "b"
    assert items[0]["seq"] == 2
    store.clear()
    assert len(store) == 0


def make_engine(store, chunk_seconds=0.05, **kwargs):
    script = (
        [quiet_chunk() for _ in range(10)]          # ~0.5s ambient calibration
        + [loud_chunk() for _ in range(14)]          # 0.7s of speech
        + [quiet_chunk() for _ in range(4)]          # pause triggers flush
    )
    source = FakeSource(script=script, tail=quiet_chunk())
    engine = LiveCaptionEngine(
        FakeSTT(),
        FakeService(),
        source_factory=lambda: source,
        store=store,
        chunk_seconds=chunk_seconds,
        min_segment=0.5,
        max_segment=1.0,
        hangover=0.1,
        min_speech=0.1,
        **kwargs,
    )
    return engine


def test_engine_produces_caption_from_speech():
    store = CaptionStore()
    engine = make_engine(store)
    engine.start()
    try:
        deadline = time.time() + 10
        while len(store) == 0 and time.time() < deadline:
            time.sleep(0.05)
        assert len(store) == 1
        caption = store.latest(1)[0]
        assert caption["transcript"] == "Hello there."
        assert caption["translated_text"] == "譯文：Hello there."
        assert caption["source"] == "cloud"
    finally:
        engine.stop()
    assert engine.running is False


def test_engine_stop_is_idempotent():
    store = CaptionStore()
    engine = make_engine(store)
    engine.start()
    engine.stop()
    engine.stop()
    assert engine.running is False


def test_engine_records_capture_error():
    class BrokenSource:
        channels = 1
        sample_rate = 16000

        def __enter__(self):
            raise live_module.LiveCaptureError("no device")

        def __exit__(self, *exc):
            return False

    store = CaptionStore()
    engine = LiveCaptionEngine(
        FakeSTT(), FakeService(), source_factory=lambda: BrokenSource(), store=store,
    )
    engine.start()
    try:
        deadline = time.time() + 5
        while engine.last_error is None and time.time() < deadline:
            time.sleep(0.05)
        assert "no device" in engine.last_error
        assert engine.running is False
    finally:
        engine.stop()


def test_passthrough_when_transcript_already_target():
    class EnSTT:
        model = "whisper-large-v3-turbo"

        def transcribe(self, audio_bytes, filename="segment.wav", language=None, timeout=None):
            return "Hello there"

        def close(self):
            pass

    service = FakeService()
    store = CaptionStore()
    engine = LiveCaptionEngine(
        EnSTT(), service, source_factory=lambda: FakeSource(), store=store,
        chunk_seconds=0.05, min_segment=0.5, max_segment=1.0, target_lang="en",
    )
    engine._process_segment(b"wav-bytes", 1.0)
    caption = store.latest(1)[0]
    assert caption["source"] == "passthrough"
    assert caption["translated_text"] == "Hello there"
    # FakeService never called


def test_stt_failure_is_swallowed():
    class FailingSTT:
        model = "whisper-large-v3-turbo"

        def transcribe(self, audio_bytes, filename="segment.wav", language=None, timeout=None):
            from agent.stt import STTError

            raise STTError("quota exhausted")

        def close(self):
            pass

    store = CaptionStore()
    engine = LiveCaptionEngine(
        FailingSTT(), FakeService(), source_factory=lambda: FakeSource(), store=store,
        chunk_seconds=0.05, min_segment=0.5, max_segment=1.0,
    )
    engine._process_segment(b"wav-bytes", 1.0)
    assert len(store) == 0


def test_split_sentences_zh():
    from agent.live import split_sentences

    parts = split_sentences("NVIDIA 對 AI 很有興趣。而 Jensen，他的想法是什麼？沒有人知道。")
    assert parts == [
        "NVIDIA 對 AI 很有興趣。",
        "而 Jensen，他的想法是什麼？",
        "沒有人知道。",
    ]


def test_split_sentences_en():
    from agent.live import split_sentences

    parts = split_sentences("Hello there. This is a test! And one more")
    assert parts == ["Hello there.", "This is a test!", "And one more"]


def test_split_sentences_single():
    from agent.live import split_sentences

    assert split_sentences("一句話沒有標點") == ["一句話沒有標點"]
    assert split_sentences("") == []


def test_process_segment_splits_translation_into_lines():
    class MultiService:
        def translate(self, text, source_lang, target_lang):
            return {
                "translated_text": "第一句。第二句！第三句？",
                "source": "cloud",
                "model": "m",
                "source_lang": source_lang,
                "target_lang": target_lang,
                "elapsed_ms": 5,
            }

    store = CaptionStore()
    engine = LiveCaptionEngine(
        FakeSTT("Sentence one. Sentence two. Sentence three."),
        MultiService(),
        source_factory=lambda: FakeSource(),
        store=store,
        chunk_seconds=0.05, min_segment=0.5, max_segment=1.0,
    )
    engine._process_segment(b"wav", 1.0)
    items = store.latest(10)
    assert [i["translated_text"] for i in items] == ["第一句。", "第二句！", "第三句？"]
    assert [i["transcript"] for i in items] == [
        "Sentence one.", "Sentence two.", "Sentence three.",
    ]


class BacklogSource(FakeSource):
    """FakeSource that reports a configurable unread backlog."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.available = 0
        self.dropped = 0

    def read_available(self):
        return self.available

    def drain(self, frames_to_drop):
        self.dropped += frames_to_drop
        self.available = max(0, self.available - frames_to_drop)


def test_engine_drains_stale_audio_backlog():
    store = CaptionStore()
    source = BacklogSource(
        script=[quiet_chunk() for _ in range(10)] + [loud_chunk() for _ in range(30)],
        tail=quiet_chunk(),
    )
    engine = LiveCaptionEngine(
        FakeSTT(),
        FakeService(),
        source_factory=lambda: source,
        store=store,
        chunk_seconds=0.05,
        min_segment=0.2,
        max_segment=0.4,
        hangover=0.05,
        min_speech=0.05,
        catchup_interval=0.01,
    )
    source.available = 16000  # 1.0s of unread audio at 16 kHz
    engine.start()
    try:
        deadline = time.time() + 10
        while source.dropped == 0 and time.time() < deadline:
            time.sleep(0.02)
        # Keeps 0.2s, drops the remaining 0.8s = 12800 frames.
        assert source.dropped == 12800
        assert source.available == 3200
    finally:
        engine.stop()


def test_engine_no_drain_when_no_backlog():
    store = CaptionStore()
    source = BacklogSource(script=[quiet_chunk() for _ in range(10)], tail=quiet_chunk())
    engine = LiveCaptionEngine(
        FakeSTT(),
        FakeService(),
        source_factory=lambda: source,
        store=store,
        chunk_seconds=0.05, min_segment=0.5, max_segment=1.0,
        catchup_interval=0.01,
    )
    engine.start()
    try:
        deadline = time.time() + 3
        while len(source._script) > 0 and time.time() < deadline:
            time.sleep(0.02)
        time.sleep(0.2)
        assert source.dropped == 0
    finally:
        engine.stop()


def test_low_volume_speech_is_detected():
    """Speech barely above ambient must be caught (old 3x threshold missed it)."""
    store = CaptionStore()
    ambient = quiet_chunk()  # RMS 0
    faint_ambient = loud_chunk(amplitude=300)   # RMS ~212
    faint_speech = loud_chunk(amplitude=800)    # RMS ~565
    script = (
        [faint_ambient for _ in range(20)]   # ~1s calibration: floor ~212
        + [faint_speech for _ in range(16)]  # 0.8s of quiet speech
        + [ambient for _ in range(6)]        # pause triggers flush
    )
    engine = LiveCaptionEngine(
        FakeSTT(),
        FakeService(),
        source_factory=lambda: FakeSource(script=script, tail=ambient),
        store=store,
        chunk_seconds=0.05, min_segment=0.3, max_segment=0.8,
        hangover=0.1, min_speech=0.1,
    )
    engine.start()
    try:
        deadline = time.time() + 10
        while len(store) == 0 and time.time() < deadline:
            time.sleep(0.02)
        assert len(store) >= 1, "quiet speech below 3x floor must still be detected"
    finally:
        engine.stop()


def test_vad_disabled_sends_all_segments(monkeypatch):
    monkeypatch.setattr("agent.live.settings.live_vad_enabled", False)
    store = CaptionStore()
    script = [quiet_chunk() for _ in range(10)] + [quiet_chunk() for _ in range(60)]
    engine = LiveCaptionEngine(
        FakeSTT(),
        FakeService(),
        source_factory=lambda: FakeSource(script=script, tail=quiet_chunk()),
        store=store,
        chunk_seconds=0.05, min_segment=0.5, max_segment=2.5, min_speech=0.5,
    )
    engine.start()
    try:
        deadline = time.time() + 10
        while len(store) == 0 and time.time() < deadline:
            time.sleep(0.02)
        # Silence-only segments are still sent when VAD is off.
        assert len(store) >= 1
        assert engine.stt_client.calls, "STT must be called even for quiet audio"
    finally:
        engine.stop()


def test_noise_floor_does_not_absorb_speech():
    """B fix: a floor calibrated during loud audio must not chase sustained
    speech upward (the old 0.05 EMA pushed the threshold above the speech
    level within seconds, blacking out captions)."""
    store = CaptionStore()
    loud_ambient = loud_chunk(amplitude=3000)   # RMS ~2121 -> threshold ~4242
    speech = loud_chunk(amplitude=6600)         # RMS ~4667, just above threshold
    engine = LiveCaptionEngine(
        FakeSTT(),
        FakeService(),
        source_factory=lambda: FakeSource(
            script=[loud_ambient for _ in range(20)] + [speech for _ in range(100)],
            tail=quiet_chunk(),
        ),
        store=store,
        chunk_seconds=0.05, min_segment=0.3, max_segment=1000.0,
        hangover=100.0, min_speech=0.1,
        stall_timeout=0,  # failsafe off: isolate the floor behaviour
    )
    engine.start()
    try:
        deadline = time.time() + 5
        while engine.last_threshold is None and time.time() < deadline:
            time.sleep(0.01)
        assert engine.last_threshold is not None, "VAD must calibrate"
        time.sleep(0.3)  # let the speech frames flow through the VAD
        # Old code: floor absorbs speech -> ~4650. New code: slow rise -> ~2700.
        assert engine.last_noise_floor < 3200, (
            f"noise floor absorbed speech: {engine.last_noise_floor:.0f}"
        )
    finally:
        engine.stop()


def test_stall_failsafe_recovers_captures():
    """C fix: speech trapped below a miscalibrated threshold must still be
    flushed once the failsafe halves the threshold (audio-time based)."""
    store = CaptionStore()
    loud_ambient = loud_chunk(amplitude=3000)   # RMS ~2121 -> threshold ~4242
    faint_speech = loud_chunk(amplitude=3500)   # RMS ~2475, below threshold
    script = (
        [loud_ambient for _ in range(20)]    # calibration: floor 2121
        + [faint_speech for _ in range(60)]  # 3s of speech the VAD rejects
        + [quiet_chunk() for _ in range(6)]  # pause
    )
    engine = LiveCaptionEngine(
        FakeSTT(),
        FakeService(),
        source_factory=lambda: FakeSource(script=script, tail=quiet_chunk()),
        store=store,
        chunk_seconds=0.05, min_segment=0.5, max_segment=2.0,
        hangover=0.1, min_speech=0.1,
        stall_timeout=1.0,  # audio seconds without a flush
    )
    engine.start()
    try:
        deadline = time.time() + 10
        while len(store) == 0 and time.time() < deadline:
            time.sleep(0.02)
        assert len(store) >= 1, "failsafe must recover speech below a bad threshold"
    finally:
        engine.stop()


def test_device_logging_never_raises():
    """The device dump on open-failure must not mask the original error."""
    from agent.live import LoopbackSource

    class BrokenPyAudio:
        def get_device_count(self):
            raise RuntimeError("boom")

    src = LoopbackSource()
    src._pyaudio = BrokenPyAudio()
    src._log_audio_devices()  # must not raise


class FakePyAudioDevices:
    """Mimics a PyAudio instance with a WASAPI loopback device present."""
    paWASAPI = 99

    def __init__(self, devices, wasapi_index=1):
        self._devices = devices
        self._wasapi = wasapi_index

    def get_device_count(self):
        return len(self._devices)

    def get_device_info_by_index(self, i):
        return self._devices[i]

    def get_host_api_info_by_type(self, _type):
        return {"index": self._wasapi}


def test_find_loopback_device_skips_output_only_and_prefers_wasapi():
    from agent.live import LoopbackSource

    devices = [
        {"index": 0, "name": "喇叭 (Realtek) ", "maxInputChannels": 0,
         "maxOutputChannels": 2, "defaultSampleRate": 44100.0, "hostApi": 0},
        {"index": 1, "name": "喇叭 (Realtek) [Loopback]", "maxInputChannels": 2,
         "maxOutputChannels": 0, "defaultSampleRate": 44100.0, "hostApi": 0},
        {"index": 2, "name": "喇叭 (Realtek) [Loopback]", "maxInputChannels": 2,
         "maxOutputChannels": 0, "defaultSampleRate": 48000.0, "hostApi": 1},
    ]
    src = LoopbackSource()
    src._pyaudio = FakePyAudioDevices(devices)
    chosen = src._find_loopback_device()
    # WASAPI (hostApi 1) + 48000 Hz wins; output-only device must be ignored.
    assert chosen["index"] == 2


def test_find_loopback_device_raises_without_candidates():
    from agent.live import LiveCaptureError, LoopbackSource

    devices = [
        {"index": 0, "name": "麥克風", "maxInputChannels": 2,
         "maxOutputChannels": 0, "defaultSampleRate": 44100.0, "hostApi": 0},
    ]
    src = LoopbackSource()
    src._pyaudio = FakePyAudioDevices(devices)
    with pytest.raises(LiveCaptureError):
        src._find_loopback_device()

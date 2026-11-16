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

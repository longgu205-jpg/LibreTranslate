# -*- coding: utf-8 -*-
"""Tests for the direct ctypes WASAPI loopback capture (方案 C)."""
import array
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pytest

import agent.live as live_mod
from agent.live import LoopbackSource


class FakeWasapiCapture:
    """Stand-in for wasapi_capture.WasapiLoopbackCapture."""

    def __init__(self, rate=48000, channels=2, exc=None):
        self.sample_rate = rate
        self.channels = channels
        self._exc = exc
        self.entered = False
        self.exited = False
        self.reads = []

    def __enter__(self):
        if self._exc is not None:
            raise self._exc
        self.entered = True
        return self

    def __exit__(self, *exc):
        self.exited = True
        return False

    def read(self, frames):
        self.reads.append(frames)
        return b"\x00\x01" * frames * self.channels

    def available(self):
        return 123


# ---------------------------------------------------------------- unit tests
def test_hresult_name_formats_known_code():
    from agent.wasapi_capture import hresult_name
    text = hresult_name(0x88890008)
    assert "0x88890008" in text
    assert "AUDCLNT_E_UNSUPPORTED_FORMAT" in text
    assert hresult_name(0xDEADBEEF) == "0xDEADBEEF"


def test_wasapi_error_carries_stage_and_hresult():
    from agent.wasapi_capture import WasapiError
    exc = WasapiError("Initialize(loopback shared)", 0x88890008)
    assert exc.stage == "Initialize(loopback shared)"
    assert exc.hresult == 0x88890008
    assert "0x88890008" in str(exc)


def test_is_supported_false_outside_windows():
    from agent.wasapi_capture import is_supported
    if sys.platform == "win32":
        pytest.skip("running on Windows")
    assert is_supported() is False


def test_to_int16_float32_clips():
    from agent.wasapi_capture import WasapiLoopbackCapture
    cap = WasapiLoopbackCapture.__new__(WasapiLoopbackCapture)
    cap._mix_kind = "float32"

    values = np.array([0.0, 0.5, -0.5, 1.5, -2.0], dtype=np.float32)
    ints = array.array("h")
    ints.frombytes(cap._to_int16(values.tobytes()))
    assert list(ints) == [0, 16383, -16383, 32767, -32767]


def test_to_int16_int16_passthrough():
    from agent.wasapi_capture import WasapiLoopbackCapture
    cap = WasapiLoopbackCapture.__new__(WasapiLoopbackCapture)
    cap._mix_kind = "int16"
    raw = b"\x00\x01\x00\x01"
    assert cap._to_int16(raw) is raw


def test_to_int16_unsupported_format_raises():
    from agent.wasapi_capture import WasapiError, WasapiLoopbackCapture
    cap = WasapiLoopbackCapture.__new__(WasapiLoopbackCapture)
    cap._mix_kind = "int24"
    with pytest.raises(WasapiError):
        cap._to_int16(b"\x00\x00\x00")


def test_read_pads_zeros_when_silent():
    from agent.wasapi_capture import WasapiLoopbackCapture
    cap = WasapiLoopbackCapture.__new__(WasapiLoopbackCapture)
    cap._mix_kind = "int16"
    cap._block_align = 4
    cap._capture = None  # no capture client -> no packets -> silence
    cap._pending = b""
    out = cap.read(10)
    assert out == b"\x00" * 40


def test_read_stops_when_no_more_packets():
    from agent.wasapi_capture import WasapiLoopbackCapture
    cap = WasapiLoopbackCapture.__new__(WasapiLoopbackCapture)
    cap._mix_kind = "int16"
    cap._block_align = 4
    cap._capture = object()
    cap._pending = b""
    pulls = []

    def fake_pull():
        pulls.append(1)
        cap._pending += b"\x00\x00\x00\x00"  # 1 frame per packet
        return len(pulls) < 2  # two packets only

    cap._pull_one_packet = fake_pull
    out = cap.read(3)  # needs 12 bytes, only 8 available -> zero pad
    assert pulls == [1, 1]
    assert len(out) == 12


# ------------------------------------------------------- integration (live.py)
DEVICES = [
    {"index": 1, "name": "喇叭 [Loopback]", "maxInputChannels": 2,
     "maxOutputChannels": 0, "defaultSampleRate": 48000.0, "hostApi": 1},
]


class FakePyAudioModule:
    """Fake pyaudiowpatch module; the direct-WASAPI path must never touch it."""

    paInt16 = 8

    def __init__(self, devices):
        self.devices = devices
        self.open_calls = []

    def PyAudio(self):
        return self

    @staticmethod
    def get_portaudio_version_text():
        return "PortAudio V19.7.0-devel (fake)"

    @staticmethod
    def get_sample_size(_fmt):
        return 2

    def get_host_api_info_by_type(self, _t):
        return {"index": 1}

    def get_device_count(self):
        return len(self.devices)

    def get_device_info_by_index(self, i):
        return self.devices[i]

    def get_default_wasapi_loopback(self):
        raise RuntimeError("no default loopback")

    def open(self, **kwargs):
        self.open_calls.append(kwargs)
        return object()


class FakeWasapiModule:
    WasapiError = type("WasapiError", (Exception,), {})
    IsSupported = True
    _capture_factory = None

    @classmethod
    def is_supported(cls):
        return cls.IsSupported

    @classmethod
    def WasapiLoopbackCapture(cls):
        return cls._capture_factory()


def test_loopback_source_prefers_direct_wasapi(monkeypatch):
    fake_pa = FakePyAudioModule(DEVICES)
    cap = FakeWasapiCapture()
    FakeWasapiModule._capture_factory = lambda: cap
    monkeypatch.setattr(live_mod, "wasapi_capture", FakeWasapiModule)
    monkeypatch.setattr(live_mod, "pyaudio", fake_pa)

    src = LoopbackSource()
    with src:
        assert src._wasapi is cap
        assert src.sample_rate == 48000
        assert src.channels == 2
        assert src.read() == b"\x00\x01" * 960 * 2  # 20ms @ 48kHz
        assert src.read_available() == 123
        src.drain(100)  # must not touch the PyAudio path
    assert cap.entered and cap.exited
    assert fake_pa.open_calls == []  # ladder never entered


def test_loopback_source_falls_back_on_wasapi_failure(monkeypatch):
    fake_pa = FakePyAudioModule(DEVICES)
    failing = FakeWasapiCapture(exc=FakeWasapiModule.WasapiError("Initialize", 0x88890008))
    FakeWasapiModule._capture_factory = lambda: failing
    monkeypatch.setattr(live_mod, "wasapi_capture", FakeWasapiModule)
    monkeypatch.setattr(live_mod, "pyaudio", fake_pa)

    src = LoopbackSource()
    with src:
        assert src._wasapi is None  # direct path failed -> ladder used
        assert src.sample_rate == 48000
    assert len(fake_pa.open_calls) == 1
    assert failing.exited  # failing capture was cleaned up


def test_loopback_source_skips_wasapi_when_unsupported(monkeypatch):
    fake_pa = FakePyAudioModule(DEVICES)
    FakeWasapiModule.IsSupported = False
    FakeWasapiModule._capture_factory = lambda: FakeWasapiCapture()
    monkeypatch.setattr(live_mod, "wasapi_capture", FakeWasapiModule)
    monkeypatch.setattr(live_mod, "pyaudio", fake_pa)

    src = LoopbackSource()
    with src:
        assert src._wasapi is None
    assert len(fake_pa.open_calls) == 1
    FakeWasapiModule.IsSupported = True


# --------------------------------------------------- stage diagnostics (new) 
def test_invoke_wraps_unexpected_exceptions(monkeypatch):
    import agent.wasapi_capture as wc
    from agent.wasapi_capture import WasapiError

    def broken(*args, **kwargs):
        raise OSError(22, "The parameter is incorrect.", None, -2147024809, None)

    monkeypatch.setattr(wc, "_vtbl_method", broken)
    with pytest.raises(WasapiError) as info:
        wc._invoke("IMMDevice.Activate(IAudioClient)", None, 3, None)
    assert info.value.stage == "IMMDevice.Activate(IAudioClient)"
    assert "OSError(22" in str(info.value)


def test_invoke_passes_through_wasapi_error(monkeypatch):
    import agent.wasapi_capture as wc
    from agent.wasapi_capture import WasapiError

    def broken(*args, **kwargs):
        raise WasapiError("Internal", 0x88890008)

    monkeypatch.setattr(wc, "_vtbl_method", broken)
    with pytest.raises(WasapiError) as info:
        wc._invoke("Outer", None, 3, None)
    assert info.value.stage == "Internal"


def test_hresult_name_maps_generic_hresult():
    from agent.wasapi_capture import hresult_name
    assert "E_INVALIDARG" in hresult_name(0x80070057)
    assert "E_ACCESSDENIED" in hresult_name(0x80070005)


def test_read_mix_format_detects_extensible_float32():
    import ctypes
    import struct

    from agent.wasapi_capture import (_FLOAT_SUBFORMAT_GUID,
                                      WasapiLoopbackCapture)

    # Raw WAVEFORMATEXTENSIBLE bytes exactly as Windows layout:
    # WAVEFORMATEX(18) + wValidBits(2) + dwChannelMask(4) + SubFormat GUID(16)
    buf = ctypes.create_string_buffer(40)
    struct.pack_into("<HHIIHHH", buf, 0, 0xFFFE, 2, 48000, 96000, 8, 32, 22)
    struct.pack_into("<H", buf, 18, 32)           # wValidBitsPerSample
    struct.pack_into("<I", buf, 20, 3)            # dwChannelMask = front L/R
    buf[24:40] = bytes(_FLOAT_SUBFORMAT_GUID)     # SubFormat = IEEE float

    cap = WasapiLoopbackCapture.__new__(WasapiLoopbackCapture)
    cap._format_ptr = ctypes.c_void_p(ctypes.addressof(buf))
    cap._read_mix_format()
    assert cap._mix_kind == "float32"
    assert cap.sample_rate == 48000
    assert cap.channels == 2
    assert cap._block_align == 4


def test_no_split_invoke_call_sites():
    """Guard: every _invoke() call must pass its args in the same call.

    A regression once split columns into `_invoke(...)(args)`, which made the
    inner invocation run with zero arguments (TypeError 4 vs 0). Scan the
    source statically so this can never sneak back in.
    """
    import re

    from pathlib import Path
    source = Path("agent/wasapi_capture.py").read_text(encoding="utf-8")
    # any `_invoke(` ... `)(` on one call chain -> broken split call
    assert re.search(r"_invoke\([^()]*(?:\([^()]*\)[^()]*)*\)\(", source) is None

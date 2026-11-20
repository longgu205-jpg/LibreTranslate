# -*- coding: utf-8 -*-
"""Direct WASAPI loopback capture via ctypes (no third-party audio package).

Why this exists: the prebuilt PyAudioWPatch binary swallows the native
HRESULT when Pa_OpenStream fails, which left us diagnosing an opaque
"[Errno -9996] Invalid device". Talking to WASAPI directly tells us the
real error and removes the frozen-app dependency on that package entirely.

Flow (all on ONE thread, the capture thread):
    CoInitializeEx -> CoCreateInstance(MMDeviceEnumerator)
    -> GetDefaultAudioEndpoint(eRender, eConsole)
    -> IMMDevice.Activate(IAudioClient)
    -> GetMixFormat -> Initialize(SHARED | AUDCLNT_STREAMFLAGS_LOOPBACK)
    -> GetService(IAudioCaptureClient) -> Start()
Polling read model: the caller asks for `frames` int16 interleaved frames
every ~20ms; we drain available packets, convert float32/any PCM to int16
and pad with zeros when no audio is flowing (keeps VAD timing stable).
"""
import ctypes
import logging
import sys
from ctypes import (POINTER, c_long, c_uint32, c_uint64,
                    c_ulong, c_ubyte, c_ushort, c_void_p, cast, sizeof)

# HRESULT / WINFUNCTYPE only exist in Windows ctypes; keep the module
# importable everywhere so live.py can reference it on any platform.
try:
    from ctypes import HRESULT, WINFUNCTYPE
except ImportError:  # non-Windows
    HRESULT = c_long
    WINFUNCTYPE = None

logger = logging.getLogger("agent.live.wasapi")

try:  # numpy ships with argos/ctranslate2; used for float32 -> int16.
    import numpy as np
except ImportError:  # pragma: no cover - argos guarantees numpy in practice
    np = None

# ---------------------------------------------------------------- constants
CLSCTX_ALL = 0x17
E_RENDER = 0          # eRender
E_CONSOLE = 0         # eConsole
SHARE_SHARED = 0      # AUDCLNT_SHAREMODE_SHARED
FLAG_LOOPBACK = 0x00020000
FLAG_NOPERSIST = 0x00080000
FLAG_SILENT = 0x1
RPC_E_CHANGED_MODE = 0x80010106

WAVE_FORMAT_PCM = 1
WAVE_FORMAT_IEEE_FLOAT = 3
WAVE_FORMAT_EXTENSIBLE = 0xFFFE

_AUDCLNT_ERRORS = {
    0x88890001: "AUDCLNT_E_NOT_INITIALIZED",
    0x88890002: "AUDCLNT_E_ALREADY_INITIALIZED",
    0x88890003: "AUDCLNT_E_WRONG_ENDPOINT_TYPE",
    0x88890004: "AUDCLNT_E_DEVICE_INVALIDATED",
    0x88890005: "AUDCLNT_E_POLL_NOT_SUPPORTED",
    0x88890006: "AUDCLNT_E_BUFFER_ERROR",
    0x88890007: "AUDCLNT_E_BUFFER_SIZE_NOT_VALID",
    0x88890008: "AUDCLNT_E_UNSUPPORTED_FORMAT",
    0x88890009: "AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED",
    0x8889000A: "AUDCLNT_E_SAMPLERATE_MISMATCH",
    0x8889000B: "AUDCLNT_E_INVALID_DEVICE_PERIOD",
    0x8889000D: "AUDCLNT_E_EVENTHANDLE_NOT_EXPECTED",
    0x8889000E: "AUDCLNT_E_SERVICE_NOT_RUNNING",
    0x88890019: "AUDCLNT_E_BUFFER_SIZE_NOT_ALIGNED",
}


_GENERIC_HRESULTS = {
    0x80004005: "E_FAIL",
    0x80070057: "E_INVALIDARG",
    0x8007000E: "E_OUTOFMEMORY",
    0x80040154: "REGDB_E_CLASSNOTREG",
    0x80010106: "RPC_E_CHANGED_MODE",
    0x800401F0: "CO_E_NOTINITIALIZED",
    0x80070005: "E_ACCESSDENIED",
}


def hresult_name(hr: int) -> str:
    hr &= 0xFFFFFFFF
    name = _AUDCLNT_ERRORS.get(hr) or _GENERIC_HRESULTS.get(hr)
    return "0x%08X%s" % (hr, " (%s)" % name if name else "")


class WasapiError(Exception):
    """WASAPI failure with the real HRESULT attached."""

    def __init__(self, stage: str, hresult: int = 0, detail: str = ""):
        self.stage = stage
        self.hresult = int(hresult) & 0xFFFFFFFF
        text = "WASAPI %s failed: %s%s" % (
            stage, hresult_name(self.hresult),
            (": %s" % detail) if detail else "",
        )
        super().__init__(text)


# -------------------------------------------------------------------- GUIDs
class _GUID(ctypes.Structure):
    # c_uint32 instead of c_ulong: c_ulong is 8 bytes on LP64 Linux but the
    # Windows GUID Data1 is DWORD (4 bytes) -- fixed.size keeps the layout
    # identical on every platform (also makes the unit tests meaningful).
    _fields_ = [("Data1", c_uint32), ("Data2", c_ushort), ("Data3", c_ushort),
                ("Data4", c_ubyte * 8)]


def _guid(l, w1, w2, *b):
    return _GUID(l, w1, w2, (c_ubyte * 8)(*b))


_CLSID_MMDeviceEnumerator = _guid(0xBCDE0395, 0xE52F, 0x467C, 0x8E, 0x3D, 0xC4, 0x57, 0x92, 0x91, 0x69, 0x2E)
_IID_IMMDeviceEnumerator = _guid(0xA95664D2, 0x9614, 0x4F35, 0xA7, 0x46, 0xDE, 0x8D, 0xB6, 0x36, 0x17, 0xE6)
_IID_IAudioClient = _guid(0x1CB9AD4C, 0xDBFA, 0x4C32, 0xB1, 0x78, 0xC2, 0xF5, 0x68, 0xA7, 0x03, 0xB2)
_IID_IAudioCaptureClient = _guid(0xC8ADBD64, 0xE71E, 0x48A0, 0xA4, 0xDE, 0x18, 0x5C, 0x39, 0x5C, 0xD3, 0x17)
_FLOAT_SUBFORMAT_GUID = _guid(0x00000003, 0x0000, 0x0010, 0x80, 0x00, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71)


class _WAVEFORMATEX(ctypes.Structure):
    # DWORD fields use c_uint32 (c_ulong would be 8 bytes on LP64 Linux).
    _fields_ = [("wFormatTag", c_ushort), ("nChannels", c_ushort),
                ("nSamplesPerSec", c_uint32), ("nAvgBytesPerSec", c_uint32),
                ("nBlockAlign", c_ushort), ("wBitsPerSample", c_ushort),
                ("cbSize", c_ushort)]


# ------------------------------------------------------------- vtable access
def _vtbl_method(iface, index, prototype):
    vtbl = cast(iface, POINTER(c_void_p)).contents.value
    addr = cast(vtbl + index * sizeof(c_void_p), POINTER(c_void_p)).contents.value
    return prototype(addr)


def _invoke(stage, iface, index, prototype, *args):
    """Call a vtable method, converting unexpected exceptions into WasapiError.

    ctypes raises raw OSError/ArgumentError when something is wrong at the
    ABI level; attaching the stage name here is what tells us exactly which
    native call blew up in the frozen exe.
    """
    try:
        return _vtbl_method(iface, index, prototype)(*args)
    except WasapiError:
        raise
    except OSError as exc:
        # ctypes auto-raises OSError(winerror=hr) for failed HRESULT results;
        # recover the unsigned HRESULT so the ladder can react per-code.
        winerr = getattr(exc, "winerror", None)
        if winerr is not None:
            raise WasapiError(stage, int(winerr) & 0xFFFFFFFF, repr(exc)) from exc
        raise WasapiError(stage, 0, repr(exc)) from exc
    except Exception as exc:
        raise WasapiError(stage, 0, repr(exc)) from exc


if WINFUNCTYPE is not None:
    _Prototype_QueryInterface = WINFUNCTYPE(HRESULT, c_void_p, POINTER(_GUID), POINTER(c_void_p))
    _Prototype_Release = WINFUNCTYPE(c_ulong, c_void_p)
    _Prototype_GetDefaultAudioEndpoint = WINFUNCTYPE(HRESULT, c_void_p, c_ulong, c_ulong, POINTER(c_void_p))
    _Prototype_IMMDevice_Activate = WINFUNCTYPE(HRESULT, c_void_p, POINTER(_GUID), c_ulong, c_void_p, POINTER(c_void_p))
    _Prototype_Initialize = WINFUNCTYPE(HRESULT, c_void_p, c_ulong, c_ulong, ctypes.c_longlong, ctypes.c_longlong, c_void_p, c_void_p)
    _Prototype_GetBufferSize = WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_uint32))
    _Prototype_GetMixFormat = WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_void_p))
    _Prototype_GetDevicePeriod = WINFUNCTYPE(HRESULT, c_void_p, POINTER(ctypes.c_longlong), POINTER(ctypes.c_longlong))
    _Prototype_StartStop = WINFUNCTYPE(HRESULT, c_void_p)
    _Prototype_GetService = WINFUNCTYPE(HRESULT, c_void_p, POINTER(_GUID), POINTER(c_void_p))
    _Prototype_GetBuffer = WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_void_p), POINTER(c_uint32),
                                       POINTER(c_ulong), POINTER(c_uint64), POINTER(c_uint64))
    _Prototype_ReleaseBuffer = WINFUNCTYPE(HRESULT, c_void_p, c_uint32)
    _Prototype_GetNextPacketSize = WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_uint32))
    # vtable index 7: HRESULT IsFormatSupported(BOOL sharemode, WAVEFORMATEX*, WAVEFORMATEX**)
    _Prototype_IsFormatSupported = WINFUNCTYPE(HRESULT, c_void_p, c_long, c_void_p, POINTER(c_void_p))
else:  # non-Windows: keep the names defined so reference sites cannot NameError
    _Prototype_QueryInterface = None
    _Prototype_Release = None
    _Prototype_GetDefaultAudioEndpoint = None
    _Prototype_IMMDevice_Activate = None
    _Prototype_Initialize = None
    _Prototype_GetBufferSize = None
    _Prototype_GetMixFormat = None
    _Prototype_GetDevicePeriod = None
    _Prototype_StartStop = None
    _Prototype_GetService = None
    _Prototype_GetBuffer = None
    _Prototype_ReleaseBuffer = None
    _Prototype_GetNextPacketSize = None
    _Prototype_IsFormatSupported = None


def is_supported() -> bool:
    """True only on Windows with ole32 present."""
    return sys.platform == "win32" and hasattr(ctypes, "windll") and hasattr(
        ctypes.windll, "ole32")


class WasapiLoopbackCapture:
    """Capture the default speakers via WASAPI loopback. Owns its thread."""

    #: requested WASAPI buffer duration, in 100-ns units (0.5 s)
    BUFFER_HNS = 5_000_000
    SILENCE_CHUNK_MS = 20

    def __init__(self):
        self.sample_rate = 48000
        self.channels = 2
        self._com_init_done = False
        self._enumerator = None
        self._device = None
        self._client = None
        self._capture = None
        self._format_ptr = None
        self._started = False
        self._pending = b""      # leftover int16 bytes from previous pulls
        self._block_align = 4    # channels * 2 for int16

    # ---------------------------------------------------------------- open
    def __enter__(self):
        if not is_supported():
            raise WasapiError("platform check", 0, "not Windows")
        ole32 = ctypes.windll.ole32
        try:
            hr = ole32.CoInitializeEx(None, 0x0)  # MTA; capture thread is private
        except Exception as exc:
            raise WasapiError("CoInitializeEx", 0, repr(exc)) from exc
        if hr in (0, 1):  # S_OK / S_FALSE -> we own the uninitialize
            self._com_init_done = True
        elif (hr & 0xFFFFFFFF) == RPC_E_CHANGED_MODE:
            logger.info("WASAPI: thread already COM-initialized in another mode, continuing")
        else:
            raise WasapiError("CoInitializeEx", hr)
        try:
            self._open_stream()
        except Exception:
            self._release_all()
            if self._com_init_done:
                ole32.CoUninitialize()
                self._com_init_done = False
            raise
        return self

    def _open_stream(self):
        ole32 = ctypes.windll.ole32
        self._enumerator = c_void_p()
        try:
            hr = ole32.CoCreateInstance(
                ctypes.byref(_CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                ctypes.byref(_IID_IMMDeviceEnumerator), ctypes.byref(self._enumerator))
        except Exception as exc:
            raise WasapiError("CoCreateInstance(MMDeviceEnumerator)", 0, repr(exc)) from exc
        if hr != 0:
            raise WasapiError("CoCreateInstance(MMDeviceEnumerator)", hr)

        self._device = c_void_p()
        hr = _invoke("GetDefaultAudioEndpoint(eRender/eConsole)", self._enumerator, 4,
                     _Prototype_GetDefaultAudioEndpoint, self._enumerator,
                     E_RENDER, E_CONSOLE, ctypes.byref(self._device))
        if hr != 0:
            raise WasapiError("GetDefaultAudioEndpoint(eRender/eConsole)", hr)

        self._client = c_void_p()
        hr = _invoke("IMMDevice.Activate(IAudioClient)", self._device, 3,
                     _Prototype_IMMDevice_Activate, self._device,
                     ctypes.byref(_IID_IAudioClient), CLSCTX_ALL, None,
                     ctypes.byref(self._client))
        if hr != 0:
            raise WasapiError("IMMDevice.Activate(IAudioClient)", hr)

        self._format_ptr = c_void_p()
        hr = _invoke("GetMixFormat", self._client, 8, _Prototype_GetMixFormat,
                     self._client, ctypes.byref(self._format_ptr))
        if hr != 0 or not self._format_ptr.value:
            raise WasapiError("GetMixFormat", hr)
        self._read_mix_format()

        self._initialize_loopback(self.BUFFER_HNS)
        buf = ctypes.c_uint32(0)
        hr = _invoke("GetBufferSize", self._client, 4, _Prototype_GetBufferSize,
                     self._client, ctypes.byref(buf))
        if hr == 0:
            logger.info("WASAPI buffer frames: %d", buf.value)

        self._capture = c_void_p()
        hr = _invoke("GetService(IAudioCaptureClient)", self._client, 14,
                     _Prototype_GetService, self._client,
                     ctypes.byref(_IID_IAudioCaptureClient),
                     ctypes.byref(self._capture))
        if hr != 0:
            raise WasapiError("GetService(IAudioCaptureClient)", hr)

        hr = _invoke("IAudioClient.Start", self._client, 10, _Prototype_StartStop, self._client)
        if hr != 0:
            raise WasapiError("IAudioClient.Start", hr)
        self._started = True
        logger.info(
            "WASAPI loopback opened (polling): %d Hz, %d ch, block %d bytes, mix bits %d",
            self.sample_rate, self.channels, self._block_align, self._mix_bits)

    def _read_mix_format(self):
        fmt = cast(self._format_ptr, POINTER(_WAVEFORMATEX)).contents
        self._mix_bits = fmt.wBitsPerSample
        self.channels = int(fmt.nChannels) or 2
        self.sample_rate = int(fmt.nSamplesPerSec) or 48000
        try:
            raw = bytes(cast(self._format_ptr.value, POINTER(c_ubyte *
                        (18 + int(fmt.cbSize or 0)))).contents)
            logger.info("WASAPI mix format raw: tag=0x%04X ch=%d rate=%d bits=%d "
                        "block=%d cb=%d bytes=%s", fmt.wFormatTag, fmt.nChannels,
                        fmt.nSamplesPerSec, fmt.wBitsPerSample, fmt.nBlockAlign,
                        fmt.cbSize, raw.hex())
        except Exception:
            pass
        if fmt.wFormatTag == WAVE_FORMAT_IEEE_FLOAT or (
                fmt.wFormatTag == WAVE_FORMAT_EXTENSIBLE
                and fmt.wBitsPerSample == 32
                and bytes(cast(self._format_ptr.value + 24, POINTER(_GUID)).contents)
                == bytes(_FLOAT_SUBFORMAT_GUID)):
            self._mix_kind = "float32"
        elif fmt.wBitsPerSample == 16:
            self._mix_kind = "int16"
        else:
            self._mix_kind = "int%d" % fmt.wBitsPerSample
        self._block_align = self.channels * 2

    def _period_hns(self) -> int:
        period = ctypes.c_longlong(0)
        _invoke("GetDevicePeriod", self._client, 9, _Prototype_GetDevicePeriod,
                self._client, ctypes.byref(period), None)
        return max(1, int(period.value))

    def _initialize_loopback(self, buffer_hns):
        """Initialize the loopback stream, trying a ladder of flag/buffer combos.

        MSDN: a FAILED Initialize may legally be retried on the same client
        (only a successful one makes the next call return E_ALREADY_INITIALIZED).
        The ladder includes AUDCLNT_STREAMFLAGS_NOPERSIST variants: if volume/
        session persistence is what trips the engine, NOPERSIST clears it.
        """
        period = self._period_hns()
        aligned = ((buffer_hns + period - 1) // period) * period
        attempts = [
            ("loopback buffer=%d" % buffer_hns, FLAG_LOOPBACK, buffer_hns),
            ("loopback nopersist buffer=%d" % buffer_hns,
             FLAG_LOOPBACK | FLAG_NOPERSIST, buffer_hns),
            ("loopback engine-default", FLAG_LOOPBACK, 0),
            ("loopback nopersist engine-default",
             FLAG_LOOPBACK | FLAG_NOPERSIST, 0),
            ("loopback aligned(%d)" % aligned, FLAG_LOOPBACK, aligned),
        ]
        last = None
        for label, flags, dur in attempts:
            try:
                hr = _invoke("Initialize(%s)" % label, self._client, 3,
                             _Prototype_Initialize,
                             self._client, SHARE_SHARED, flags, dur, 0,
                             self._format_ptr.value, None)
                if hr == 0:
                    logger.info("WASAPI Initialize ok (%s)", label)
                    return
            except WasapiError as exc:
                last = exc
                logger.error("WASAPI Initialize failed (%s): %s", label, exc)
        self._diagnose()
        raise last or WasapiError("Initialize(loopback)", 0, "empty ladder")

    # ---------------------------------------------------------------- probes
    def _diagnose(self):
        """Log-only probes that pinpoint WHY every Initialize attempt failed.

        Runs only after the whole ladder failed. Every probe swallows its own
        errors so diagnostics can never mask the real loopback failure.
        """
        logger.info("WASAPI diag: all Initialize attempts failed, running probes")
        self._diag_is_format_supported()
        self._diag_render_canary()

    def _diag_is_format_supported(self):
        """Probe 1: does the engine accept the exact mix format at all?"""
        closest = c_void_p()
        try:
            _invoke("diag IsFormatSupported", self._client, 7,
                    _Prototype_IsFormatSupported, self._client,
                    c_long(SHARE_SHARED), self._format_ptr.value,
                    ctypes.byref(closest))
        except Exception as exc:
            logger.info("WASAPI diag: IsFormatSupported rejected the mix "
                        "format too: %s", exc)
            return
        logger.info("WASAPI diag: IsFormatSupported(shared, mix format) = S_OK "
                    "(engine accepts the format)")
        if closest.value:
            try:
                cf = cast(closest, POINTER(_WAVEFORMATEX)).contents
                logger.info("WASAPI diag: closest-match format: tag=0x%04X "
                            "ch=%d rate=%d bits=%d", cf.wFormatTag,
                            cf.nChannels, cf.nSamplesPerSec, cf.wBitsPerSample)
            except Exception:
                pass
            try:
                ctypes.windll.ole32.CoTaskMemFree(closest)
            except Exception:
                pass

    def _diag_render_canary(self):
        """Probe 2: canary -- plain render stream (NO loopback flag) in the
        same process on the same endpoint.

        If even this gets E_INVALIDARG, the engine rejects our PROCESS image
        regardless of stream type -- the signature of process-level
        interception (antivirus/EDR), not a format or flag problem.
        """
        client = c_void_p()
        try:
            _invoke("diag Activate(canary)", self._device, 3,
                    _Prototype_IMMDevice_Activate, self._device,
                    ctypes.byref(_IID_IAudioClient), CLSCTX_ALL, None,
                    ctypes.byref(client))
        except Exception as exc:
            logger.info("WASAPI diag: canary Activate failed: %s", exc)
            return
        try:
            _invoke("diag Initialize(render, no loopback)", client, 3,
                    _Prototype_Initialize, client, SHARE_SHARED, 0,
                    self.BUFFER_HNS, 0, self._format_ptr.value, None)
            logger.info("WASAPI diag: render (no loopback) Initialize OK -> "
                        "the engine accepts this process; the rejection is "
                        "specific to the loopback flag")
        except Exception as exc:
            logger.info("WASAPI diag: render (no loopback) Initialize also "
                        "failed -> the engine rejects this PROCESS regardless "
                        "of stream type (process-level interception, e.g. "
                        "antivirus/EDR): %s", exc)
        finally:
            try:
                _vtbl_method(client, 2, _Prototype_Release)(client)
            except Exception:
                pass

    # ---------------------------------------------------------------- read
    def available(self) -> int:
        """Frames of int16 data not yet consumed (internal + device-side)."""
        frames = len(self._pending) // self._block_align
        # Peek at the device-side backlog non-destructively via GetNextPacketSize,
        # so the executor's catch-up loop sees audio it has not pulled yet.
        if self._capture is not None and self._capture.value:
            try:
                packet = c_uint32(0)
                hr = _invoke("GetNextPacketSize(peek)", self._capture, 5, _Prototype_GetNextPacketSize,
                             self._capture, ctypes.byref(packet))
                if hr == 0 and packet.value:
                    frames += int(packet.value)
            except Exception:
                pass
        return frames

    def read(self, frames: int) -> bytes:
        """Return exactly `frames` int16 interleaved frames (zeros if silent)."""
        need = max(0, int(frames)) * self._block_align
        while len(self._pending) < need:
            if not self._pull_one_packet():
                break
        out = self._pending[:need]
        self._pending = self._pending[need:]
        if len(out) < need:
            out = out + b"\x00" * (need - len(out))
        return out

    def read_seconds(self, seconds: float) -> bytes:
        frames = max(64, int(self.sample_rate * seconds))
        return self.read(frames)

    def _pull_one_packet(self) -> bool:
        """Drain one WASAPI packet into the pending buffer. False = empty."""
        if self._capture is None or not self._capture.value:
            return False
        packet = c_uint32(0)
        hr = _invoke("GetNextPacketSize", self._capture, 5, _Prototype_GetNextPacketSize,
                     self._capture, ctypes.byref(packet))
        if hr != 0:
            raise WasapiError("GetNextPacketSize", hr)
        if packet.value == 0:
            return False
        data = c_void_p()
        frames = c_uint32(0)
        flags = c_ulong(0)
        hr = _invoke("GetBuffer", self._capture, 3, _Prototype_GetBuffer,
                     self._capture, ctypes.byref(data), ctypes.byref(frames),
                     ctypes.byref(flags), None, None)
        if hr != 0:
            raise WasapiError("GetBuffer", hr)
        n = frames.value
        try:
            if flags.value & FLAG_SILENT or not data.value:
                raw = b"\x00" * (n * int(self._mix_bits // 8) * self.channels)
            else:
                size = n * int(self._mix_bits // 8) * self.channels
                raw = ctypes.string_at(data.value, size)
        finally:
            _invoke("ReleaseBuffer", self._capture, 4, _Prototype_ReleaseBuffer,
                    self._capture, n)
        converted = self._to_int16(raw)
        self._pending += converted
        return True

    def _to_int16(self, raw: bytes) -> bytes:
        kind = getattr(self, "_mix_kind", "float32")
        if kind == "int16":
            return raw
        if kind == "float32":
            if np is not None:
                x = np.frombuffer(raw, dtype=np.float32)
                return (np.clip(x, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
            # pure-Python fallback (slow but serviceable)
            import struct
            vals = struct.unpack("<%df" % (len(raw) // 4), raw)
            return struct.pack(
                "<%dh" % len(vals),
                *(max(-32767, min(32767, int(v * 32767.0))) for v in vals))
        raise WasapiError("format", 0, "unsupported mix format %s" % kind)

    # --------------------------------------------------------------- close
    def stop(self):
        if self._client is not None and self._client.value and self._started:
            try:
                _invoke("IAudioClient.Stop", self._client, 11, _Prototype_StartStop,
                        self._client)
            except Exception:
                pass
            self._started = False

    def __exit__(self, *exc):
        self.stop()
        self._release_all()
        if self._com_init_done:
            try:
                ctypes.windll.ole32.CoUninitialize()
            except Exception:
                pass
            self._com_init_done = False
        return False

    def _release_all(self):
        def _rel(iface):
            if iface is not None and iface.value:
                try:
                    _vtbl_method(iface, 2, _Prototype_Release)(iface)
                except Exception:
                    pass
        _rel(self._capture)
        self._capture = None
        _rel(self._client)
        self._client = None
        _rel(self._device)
        self._device = None
        _rel(self._enumerator)
        self._enumerator = None
        if self._format_ptr is not None and self._format_ptr.value:
            try:
                ctypes.windll.ole32.CoTaskMemFree(self._format_ptr)
            except Exception:
                pass
            self._format_ptr = None

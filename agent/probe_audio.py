# -*- coding: utf-8 -*-
"""Standalone probe: which open() parameter sets work here?

Delta from v1: v1 hand-coded frames_per_buffer=2 (README style) and drew
false conclusions on machines where tiny buffers are rejected by WASAPI.
This version tries a parameter matrix labelled per attempt and prints a
PASS/FAIL summary, runnable as plain script (dev) and via PyInstaller exe.
"""
import ctypes
import sys
import threading
import traceback

COINIT_MULTITHREADED = 0x0
COINIT_APARTMENTTHREADED = 0x2


def log(msg):
    print(msg, flush=True)


def try_open(pa, info, fpb, label, output=False):
    import pyaudiowpatch as pyaudio

    kwargs = {
        "format": pyaudio.paInt16,
        "channels": max(1, int(info["maxInputChannels"] if not output
                              else info["maxOutputChannels"])),
        "rate": int(float(info["defaultSampleRate"])),
        "input_device_index": int(info["index"]),
        "output_device_index": int(info["index"]),
    }
    if output:
        kwargs["output"] = True
    else:
        kwargs["input"] = True
    if fpb is not None:
        kwargs["frames_per_buffer"] = fpb
    log("  %-28s open(%s index %s, rate %s, fpb=%r)"
        % (label, "output" if output else "input", int(info["index"]),
           int(float(info["defaultSampleRate"])), fpb))
    stream = pa.open(**kwargs)
    data = stream.read(480, exception_on_overflow=False) if not output \
        else b"\x00\x00\x00\x00" * 480 * 2
    stream.stop_stream()
    stream.close()
    log("    -> PASS" if output else "    -> PASS (read %d bytes)" % len(data))
    return True


def full_mirror(info, fpb):
    """Exact mirror of the app's capture path: PyAudio created AND opened
    AND terminated entirely on one fresh thread."""
    import pyaudiowpatch as pyaudio

    log("  0-mirror  own PyAudio() on this thread")
    log("  0-mirror  PortAudio %s" % pyaudio.get_portaudio_version_text())
    pa = pyaudio.PyAudio()
    try:
        found = None
        for i in range(pa.get_device_count()):
            di = pa.get_device_info_by_index(i)
            if "[Loopback]" in str(di.get("name", "")) and di["maxInputChannels"] > 0:
                found = di
                break
        if found is None:
            log("    -> FAIL: no loopback device in this instance's enumeration")
            return False
        log("  0-mirror  chosen: %s (index %s)" % (found["name"], found["index"]))
        try_open(pa, found, fpb, "0-mirror")
        return True
    finally:
        try:
            pa.terminate()
        except Exception:
            pass


def attempt(pa, info, fpb, label, com_mode=None):
    """Run one attempt on a fresh thread; return True/False."""
    result = {}

    def worker():
        try:
            if com_mode is not None:
                hr = ctypes.windll.ole32.CoInitializeEx(None, com_mode)
                log("    CoInitializeEx(0x%X) -> 0x%08X" % (com_mode, hr & 0xFFFFFFFF))
            try_open(pa, info, fpb, label)
            result["ok"] = True
        except Exception as exc:
            traceback.print_exc()
            log("    -> FAIL: %r" % (exc,))

    t = threading.Thread(target=worker, name="probe-" + label.split()[0], daemon=True)
    t.start()
    t.join()
    return bool(result.get("ok"))


def main():
    log("[sys] frozen=%r" % (getattr(sys, "frozen", False),))
    log("[sys] executable=%s" % sys.executable)
    log("[sys] python=%s" % sys.version.replace("\n", " "))

    import pyaudiowpatch as pyaudio

    try:
        log("[sys] portaudio: %s" % pyaudio.get_portaudio_version_text())
    except Exception as exc:
        log("[sys] portaudio version unavailable: %r" % (exc,))
    log("[sys] pyaudiowpatch version: %s" % getattr(pyaudio, "__version__", "?"))

    pa = pyaudio.PyAudio()
    try:
        count = pa.get_device_count()
        log("[enum] device count: %d" % count)
        loopbacks = []
        mics = []
        for i in range(count):
            try:
                info = pa.get_device_info_by_index(i)
            except Exception as exc:
                log("  device %d: query error %r" % (i, exc))
                continue
            log(
                "  device %d: %s (in %s, out %s, %s Hz)"
                % (i, info["name"], info["maxInputChannels"],
                   info["maxOutputChannels"], info["defaultSampleRate"])
            )
            name = str(info.get("name", ""))
            if "[Loopback]" in name and info["maxInputChannels"] > 0:
                loopbacks.append(info)
            elif info["maxInputChannels"] > 0 and "音效對應表" not in name and \
                    "Sound Mapper" not in name:
                mics.append(info)
        if not loopbacks:
            log("[result] FAIL: no [Loopback] device found")
            return
        target = loopbacks[0]
        mic = mics[0] if mics else None
        log("[enum] loopback target: %s (index %s)" % (target["name"], target["index"]))
        try:
            default_loopback = pa.get_default_wasapi_loopback()
            log("[enum] get_default_wasapi_loopback: index %s" % default_loopback["index"])
        except Exception as exc:
            log("[enum] get_default_wasapi_loopback failed: %r" % (exc,))

        rate = int(float(target["defaultSampleRate"]))
        fpb_20ms = max(64, int(rate * 0.02))
        results = []

        log("[test 0] FULL MIRROR of app capture thread (own PyAudio + open + terminate)")
        result = {}
        def mirror_worker():
            try:
                result["ok"] = full_mirror(target, fpb_20ms)
            except Exception:
                traceback.print_exc()
                result["ok"] = False
        t = threading.Thread(target=mirror_worker, name="probe-0-mirror", daemon=True)
        t.start()
        t.join()
        results.append(("mirror", bool(result.get("ok"))))

        log("[test 1] fpb=%d (app's 20ms), plain thread" % fpb_20ms)
        results.append(("loop fpb%dfresh" % fpb_20ms, attempt(pa, target, fpb_20ms, "1-fresh")))
        log("[test 2] fpb=%d, MAIN thread" % fpb_20ms)
        try:
            try_open(pa, target, fpb_20ms, "2-main")
            results.append(("loop fpb%d main" % fpb_20ms, True))
        except Exception:
            traceback.print_exc()
            results.append(("loop fpb%d main" % fpb_20ms, False))
        log("[test 3] fpb=256")
        results.append(("loop fpb256", attempt(pa, target, 256, "3-fpb256")))
        log("[test 4] fpb=None (PortAudio default)")
        results.append(("loop fpbNone", attempt(pa, target, None, "4-fpbNone")))
        log("[test 5] fpb=2 (README style)")
        results.append(("loop fpb2", attempt(pa, target, 2, "5-fpb2")))
        log("[test 6] fpb=%d + CoInitializeEx(MTA)" % fpb_20ms)
        results.append(("loop MTA", attempt(pa, target, fpb_20ms, "6-mta", COINIT_MULTITHREADED)))
        log("[test 7] fpb=%d + CoInitializeEx(STA)" % fpb_20ms)
        results.append(("loop STA", attempt(pa, target, fpb_20ms, "7-sta", COINIT_APARTMENTTHREADED)))
        if mic is not None:
            log("[test 8] mic %s (index %s), fpb=%d" % (mic["name"], mic["index"], fpb_20ms))
            results.append(("mic", attempt(pa, mic, fpb_20ms, "8-mic")))

        speakers = [info for info in (pa.get_device_info_by_index(i) for i in range(count))
                    if info["maxOutputChannels"] > 0 and "Loopback" not in str(info.get("name", ""))
                    and int(info.get("hostApi", -1)) == int(target.get("hostApi", 2))]
        if speakers:
            spk = speakers[0]
            log("[test 9] OUTPUT device %s (index %s), fpb=%d -- same host API as loopback"
                % (spk["name"], spk["index"], fpb_20ms))
            result = {}
            def out_worker():
                try:
                    try_open(pa, spk, fpb_20ms, "9-out", output=True)
                    result["ok"] = True
                except Exception as exc:
                    traceback.print_exc()
                    log("    -> FAIL: %r" % (exc,))
            t = threading.Thread(target=out_worker, name="probe-9-out", daemon=True)
            t.start()
            t.join()
            results.append(("output", bool(result.get("ok"))))

        log("[summary] " + ", ".join("%s=%s" % (k, "PASS" if v else "FAIL")
                                     for k, v in results))
    finally:
        try:
            pa.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    main()
    input("\n按 Enter 結束...")

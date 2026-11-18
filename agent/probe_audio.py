# -*- coding: utf-8 -*-
"""Standalone probe: does WASAPI loopback open work in this environment?

Runs the same sequence of open() attempts used by agent.live, one variant at
a time, and prints a PASS/FAIL summary. Built both as a plain script (dev)
and as a tiny PyInstaller exe to compare dev vs frozen environments.
"""
import ctypes
import sys
import threading
import traceback


def log(msg):
    print(msg, flush=True)


COINIT_MULTITHREADED = 0x0
COINIT_APARTMENTTHREADED = 0x2


def open_loopback(pa, info):
    import pyaudiowpatch as pyaudio

    kwargs = {
        "format": pyaudio.paInt16,
        "channels": max(1, int(info["maxInputChannels"])),
        "rate": int(float(info["defaultSampleRate"])),
        "input": True,
        "input_device_index": int(info["index"]),
        "frames_per_buffer": pyaudio.get_sample_size(pyaudio.paInt16),
    }
    log("    open kwargs: %s" % kwargs)
    stream = pa.open(**kwargs)
    data = stream.read(480, exception_on_overflow=False)
    stream.stop_stream()
    stream.close()
    log("    read %d bytes OK" % len(data))


def run_in_thread(name, setup, pa, info):
    """Run one open() attempt on a fresh thread; return True/False."""
    result = {}

    def worker():
        try:
            if setup is not None:
                hr = ctypes.windll.ole32.CoInitializeEx(None, setup)
                log("    CoInitializeEx(0x%X) -> HRESULT 0x%08X" % (setup, hr & 0xFFFFFFFF))
            open_loopback(pa, info)
            result["ok"] = True
        except Exception as exc:
            result["err"] = exc
            traceback.print_exc()
            log("    FAILED: %r" % (exc,))

    t = threading.Thread(target=worker, name=name, daemon=True)
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
        plain_inputs = []
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
            if "[Loopback]" in str(info.get("name", "")) and info["maxInputChannels"] > 0:
                loopbacks.append(info)
            elif info["maxInputChannels"] > 0:
                plain_inputs.append(info)
        if not loopbacks:
            log("[result] FAIL: no [Loopback] device found")
            return
        target = loopbacks[0]
        log("[enum] target: %s (index %s)" % (target["name"], target["index"]))

        results = {}
        log("[test A] fresh thread, no explicit COM init")
        results["A plain thread"] = run_in_thread("probe-A", None, pa, target)
        log("[test B] fresh thread + CoInitializeEx(MTA)")
        results["B MTA"] = run_in_thread("probe-B", COINIT_MULTITHREADED, pa, target)
        log("[test C] fresh thread + CoInitializeEx(STA)")
        results["C STA"] = run_in_thread("probe-C", COINIT_APARTMENTTHREADED, pa, target)
        if plain_inputs:
            mic = plain_inputs[0]
            log("[test D] plain input device %s (index %s), no COM init"
                % (mic["name"], mic["index"]))
            results["D mic"] = run_in_thread("probe-D-mic", None, pa, mic)

        log("[summary] " + ", ".join("%s=%s" % (k, "PASS" if v else "FAIL")
                                     for k, v in results.items()))
    finally:
        try:
            pa.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    main()
    input("\n按 Enter 結束...")

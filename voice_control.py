"""
voice_control.py - say a command, the game gets the input.

Setup (Windows):
    pip install vosk sounddevice pyyaml pydirectinput
    pip install vgamepad      (optional: Xbox controller actions; also installs the ViGEmBus driver)
    Download vosk-model-small-en-us-0.15 from https://alphacephei.com/vosk/models
    and unzip it next to this script, so that vosk-model-small-en-us-0.15\\am exists
    (not vosk-model-small-en-us-0.15\\vosk-model-small-en-us-0.15\\am).

Run:
    python voice_control.py [commands.yaml] [--port 8765] [--no-browser]

This opens the editor at http://127.0.0.1:8765, where you start/stop listening,
edit the mappings, and set which window each profile applies to.

Run the terminal as Administrator if the game runs elevated, otherwise Windows
silently drops the injected input.
"""
import argparse
import ctypes
import json
import queue
import re
import sys
import threading
import time
import webbrowser
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pydirectinput as pdi
import sounddevice as sd
import yaml
from vosk import KaldiRecognizer, Model, SetLogLevel

pdi.PAUSE = 0  # pydirectinput sleeps 100 ms after every call by default

HERE = Path(__file__).resolve().parent
KEYS = set(pdi.KEYBOARD_MAPPING)
MATCH_TYPES = ("title_contains", "process", "title_regex")

# Virtual Xbox 360 controller (vgamepad + ViGEmBus). Name -> XUSB_GAMEPAD_* suffix.
PAD_BUTTONS = {
    "a": "A", "b": "B", "x": "X", "y": "Y", "lb": "LEFT_SHOULDER", "rb": "RIGHT_SHOULDER",
    "back": "BACK", "start": "START", "guide": "GUIDE", "ls": "LEFT_THUMB", "rs": "RIGHT_THUMB",
    "dpad_up": "DPAD_UP", "dpad_down": "DPAD_DOWN", "dpad_left": "DPAD_LEFT", "dpad_right": "DPAD_RIGHT",
}
PAD_NAMES = set(PAD_BUTTONS) | {"lt", "rt"}  # triggers act as buttons: fully pulled or released
D = 0.7071
STICK_DIRS = {"up": (0, 1), "down": (0, -1), "left": (-1, 0), "right": (1, 0),
              "up_left": (-D, D), "up_right": (D, D), "down_left": (-D, -D), "down_right": (D, -D),
              "center": (0, 0)}
PAD_INSTALL_HINT = ("Controller actions need vgamepad and the ViGEmBus driver. "
                    "Run  pip install vgamepad  and accept the driver installer, then restart.")


def pad_available() -> bool:
    import importlib.util
    return importlib.util.find_spec("vgamepad") is not None


# --------------------------------------------------------------------------- input

class Executor:
    """Runs actions on a worker thread so sequences never stall the audio loop."""

    def __init__(self):
        self.jobs: queue.Queue = queue.Queue()
        self.held: set[str] = set()
        self.lock = threading.Lock()
        self.listening = True
        self.gen = 0  # bumped by release_all; running sequences stop when it changes
        self.pad = None  # virtual Xbox controller, created on first use
        self._vg = None
        self.on_error = print
        threading.Thread(target=self._loop, daemon=True).start()

    def ensure_pad(self):
        """Plug in the virtual controller. Kept for the whole session: games dislike
        controllers that come and go."""
        with self.lock:
            if self.pad is None:
                try:
                    import vgamepad as vg
                except ImportError:
                    raise RuntimeError(PAD_INSTALL_HINT) from None
                try:
                    self.pad = vg.VX360Gamepad()
                except Exception as e:
                    raise RuntimeError(f"Couldn't create the virtual controller ({e}). "
                                       "Is the ViGEmBus driver installed?") from None
                self._vg = vg
                print("  [virtual Xbox controller connected]")
        return self.pad

    def _pad_button(self, name: str, down: bool):
        pad = self.ensure_pad()
        with self.lock:
            if name in ("lt", "rt"):
                (pad.left_trigger if name == "lt" else pad.right_trigger)(value=255 if down else 0)
            else:
                b = getattr(self._vg.XUSB_BUTTON, "XUSB_GAMEPAD_" + PAD_BUTTONS[name])
                (pad.press_button if down else pad.release_button)(button=b)
            pad.update()

    def _stick(self, side: str, x: float, y: float):
        pad = self.ensure_pad()
        with self.lock:
            (pad.left_joystick_float if side == "left" else pad.right_joystick_float)(
                x_value_float=x, y_value_float=y)
            pad.update()

    def submit(self, phrase: str, action: dict, on_done=None):
        if "listen" in action:  # control commands bypass the queue and pause state
            self.listening = bool(action["listen"])
            if not self.listening:
                self.release_all()
            print(f"  [voice {'ON' if self.listening else 'OFF'}]")
            return
        if self.listening:
            self.jobs.put((phrase, action, time.perf_counter(), self.gen, on_done))

    def _loop(self):
        while True:
            phrase, action, t0, gen, on_done = self.jobs.get()
            if gen != self.gen:
                continue  # queued before a stop / window switch
            try:
                self._run(action, gen)
            except Exception as e:
                self.on_error(str(e))
                continue
            ms = (time.perf_counter() - t0) * 1000
            print(f"  -> {phrase}  ({ms:.0f} ms to execute)")
            if on_done:
                on_done(phrase, ms)

    def _run(self, a: dict, gen: int):
        if gen != self.gen:
            return
        if "sequence" in a:
            for step in a["sequence"]:
                if "wait" in step:
                    time.sleep(step["wait"])
                else:
                    self._run(step, gen)
        elif "tap" in a:
            key = str(a["tap"])
            pdi.keyDown(key)
            time.sleep(a.get("duration", 0.03))  # many games miss sub-frame taps
            pdi.keyUp(key)
        elif "hold" in a:
            key = str(a["hold"])
            with self.lock:
                if key not in self.held:
                    pdi.keyDown(key)
                    self.held.add(key)
        elif "release" in a:
            key = str(a["release"])
            with self.lock:
                pdi.keyUp(key)
                self.held.discard(key)
        elif a.get("release_all"):
            self.release_all()
        elif "click" in a:
            pdi.click(button=a["click"])
        elif "pad_tap" in a:
            name = a["pad_tap"]
            self._pad_button(name, True)
            time.sleep(a.get("duration", 0.05))  # games read pads once per frame
            self._pad_button(name, False)
        elif "pad_hold" in a:
            self._pad_button(a["pad_hold"], True)
            self.held.add("pad:" + a["pad_hold"])
        elif "pad_release" in a:
            self._pad_button(a["pad_release"], False)
            self.held.discard("pad:" + a["pad_release"])
        elif "stick" in a:
            side, direction = a["stick"], a.get("dir", "center")
            amount = a.get("amount", 1.0)
            x, y = STICK_DIRS[direction]
            self._stick(side, x * amount, y * amount)
            if "duration" in a and direction != "center":
                time.sleep(a["duration"])
                self._stick(side, 0, 0)
            elif direction == "center":
                self.held.discard("stick:" + side)
            else:
                self.held.add("stick:" + side)

    def release_all(self):
        self.gen += 1
        with self.lock:
            for key in list(self.held):
                if ":" not in key:
                    pdi.keyUp(key)
            self.held.clear()
            if self.pad is not None:
                self.pad.reset()  # all buttons up, triggers released, sticks centered
                self.pad.update()


class Matcher:
    """Greedy longest-match of recognized words onto command phrases."""

    def __init__(self, phrases):
        self.phrases = {tuple(p.split()) for p in phrases}
        self.max_len = max(len(p) for p in self.phrases)
        # A phrase that starts a longer phrase ("quick" in "quick save") can't be
        # fired from a partial result - the speaker may still be mid-command.
        self.prefixes = {p for p in self.phrases
                         if any(len(q) > len(p) and q[:len(p)] == p for q in self.phrases)}

    def parse(self, text: str) -> list[tuple[str, ...]]:
        words = [w for w in text.split() if w != "[unk]"]
        out, i = [], 0
        while i < len(words):
            for n in range(min(self.max_len, len(words) - i), 0, -1):
                cand = tuple(words[i:i + n])
                if cand in self.phrases:
                    out.append(cand)
                    i += n
                    break
            else:
                i += 1
        return out


# --------------------------------------------------------------------------- windows

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def window_info(hwnd) -> dict:
    if not hwnd:
        return {"title": "", "process": ""}
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    proc = ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if h:
        size = wintypes.DWORD(1024)
        pbuf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, pbuf, ctypes.byref(size)):
            proc = Path(pbuf.value).name
        kernel32.CloseHandle(h)
    return {"title": buf.value, "process": proc}


def foreground_window() -> dict:
    return window_info(user32.GetForegroundWindow())


def list_windows() -> list[dict]:
    out = []

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            out.append(window_info(hwnd))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    seen, uniq = set(), []
    for w in out:
        k = (w["title"], w["process"])
        if k not in seen and w["title"] not in ("Program Manager",):
            seen.add(k)
            uniq.append(w)
    return uniq


def matches(match: dict, win: dict) -> bool:
    kind, value = match.get("type"), match.get("value", "")
    if not value:
        return False  # a profile without a window rule is never active
    if kind == "title_contains":
        return value.lower() in win["title"].lower()
    if kind == "process":
        return value.lower() == win["process"].lower()
    if kind == "title_regex":
        return re.search(value, win["title"], re.IGNORECASE) is not None
    return False


# --------------------------------------------------------------------------- config

HEADER = """\
# Voice -> input mappings. Edit here or in the web UI (python voice_control.py).
#
# global:    commands that work in every window
# profiles:  commands that only work while a matching window is in front.
#            match is one of  {title_contains: "..."}  {process: game.exe}  {title_regex: "..."}
#            The first matching profile wins; on a phrase clash it beats global.
#
# input_mode: keyboard | controller   which input each command sends (toggle in the UI)
#   A command is either one action, used in both modes, or one input per mode
#   (leave a mode out and the command does nothing in it):
#     jump: {keyboard: {tap: space}, controller: {pad_tap: a}}
#
# Actions:
#   tap: <key>                 press + release (optional  duration: seconds to hold)
#   hold: <key>                press and keep held until released
#   release: <key>             release a held key
#   release_all: true          release everything currently held
#   click: left|right|middle   mouse click
#   sequence: [ ...steps ]     run steps in order; a step can be any action or {wait: seconds}
#   listen: true|false         pause / resume voice control (always honored)
#
# Xbox controller actions (virtual pad; needs  pip install vgamepad  + the ViGEmBus driver):
#   pad_tap: <button>          press + release (optional  duration: seconds, default 0.05)
#   pad_hold: <button>         press and keep held
#   pad_release: <button>      release a held button
#   stick: left|right, dir: <direction>   push a stick (optional  amount: 0-1,
#                              duration: seconds; without duration it stays until dir: center)
#   Buttons: a b x y lb rb lt rt back start guide ls rs dpad_up dpad_down dpad_left dpad_right
#   Directions: up down left right up_left up_right down_left down_right center
#
# Key names follow pydirectinput: a-z, 0-9, space, shift, ctrl, alt, tab, enter,
# esc, f1-f12, up, down, left, right, etc.

"""


def normalize_phrase(p) -> str:
    return " ".join(str(p).lower().split())


MODES = ("keyboard", "controller")


def split_binding(v) -> dict:
    """A command's YAML value -> {'keyboard': action|None, 'controller': action|None}.

    Either  {keyboard: <action>, controller: <action>}  (one may be left out: that mode
    does nothing), or a single action, which is used in both modes."""
    if isinstance(v, dict) and v and set(v) <= set(MODES):
        return {m: v.get(m) for m in MODES}
    return {"keyboard": v, "controller": v}


def join_binding(b: dict):
    """Inverse of split_binding: one action when both modes match, else the split form."""
    k, c = b.get("keyboard"), b.get("controller")
    if k is not None and k == c:
        return k
    return {m: b[m] for m in MODES if b.get(m) is not None}


def commands_in(d) -> dict:
    return {normalize_phrase(k): split_binding(v) for k, v in (d or {}).items()}


def load_config(path: Path) -> dict:
    """Read YAML into the internal form. Old flat `commands:` files are migrated."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = {
        "model": raw.get("model", "vosk-model-small-en-us-0.15"),
        "fire_on_partial": raw.get("fire_on_partial", True),
        "block_ms": raw.get("block_ms", 50),
        "input_mode": raw.get("input_mode") if raw.get("input_mode") in MODES else "keyboard",
        "global": {},
        "profiles": [],
        "migrated": False,
    }
    if "profiles" in raw or "global" in raw:
        cfg["global"] = commands_in(raw.get("global"))
        for p in raw.get("profiles") or []:
            m = p.get("match") or {}
            kind = next((k for k in MATCH_TYPES if k in m), "title_contains")
            cfg["profiles"].append({
                "name": str(p.get("name", "Profile")),
                "match": {"type": kind, "value": str(m.get(kind, "") or "")},
                "commands": commands_in(p.get("commands")),
            })
    else:  # old single-list format: voice on/off become global, the rest a profile
        old = raw.get("commands") or {}
        listen = {k: v for k, v in old.items() if isinstance(v, dict) and "listen" in v}
        cfg["global"] = commands_in(listen)
        cfg["profiles"].append({
            "name": "My game",
            "match": {"type": "title_contains", "value": ""},
            "commands": commands_in({k: v for k, v in old.items() if k not in listen}),
        })
        cfg["migrated"] = True
    return cfg


def validate_action(a, where: str, errors: list, in_sequence=False):
    if not isinstance(a, dict) or not a:
        errors.append(f"{where}: action is empty")
        return
    if in_sequence and "wait" in a:
        if not isinstance(a["wait"], (int, float)) or a["wait"] < 0:
            errors.append(f"{where}: wait must be a number of seconds")
        return
    for kind in ("pad_tap", "pad_hold", "pad_release"):
        if kind in a:
            if a[kind] not in PAD_NAMES:
                errors.append(f"{where}: unknown controller button '{a[kind]}'")
            if "duration" in a and (not isinstance(a["duration"], (int, float)) or a["duration"] < 0):
                errors.append(f"{where}: duration must be a number of seconds")
            return
    if "stick" in a:
        if a["stick"] not in ("left", "right"):
            errors.append(f"{where}: stick must be left or right")
        if a.get("dir", "center") not in STICK_DIRS:
            errors.append(f"{where}: unknown stick direction '{a.get('dir')}'")
        amt = a.get("amount", 1.0)
        if not isinstance(amt, (int, float)) or not 0 <= amt <= 1:
            errors.append(f"{where}: stick amount must be between 0 and 1")
        if "duration" in a and (not isinstance(a["duration"], (int, float)) or a["duration"] < 0):
            errors.append(f"{where}: duration must be a number of seconds")
        return
    for kind in ("tap", "hold", "release"):
        if kind in a:
            if str(a[kind]) not in KEYS:
                errors.append(f"{where}: unknown key '{a[kind]}'")
            if "duration" in a and (not isinstance(a["duration"], (int, float)) or a["duration"] < 0):
                errors.append(f"{where}: duration must be a number of seconds")
            return
    if "click" in a:
        if a["click"] not in ("left", "right", "middle"):
            errors.append(f"{where}: click must be left, right or middle")
        return
    if "release_all" in a or "listen" in a:
        return
    if "sequence" in a and not in_sequence:
        steps = a["sequence"]
        if not isinstance(steps, list) or not steps:
            errors.append(f"{where}: sequence has no steps")
            return
        for i, s in enumerate(steps, 1):
            validate_action(s, f"{where} step {i}", errors, in_sequence=True)
        return
    errors.append(f"{where}: unknown action {json.dumps(a)}")


def validate_commands(cmds, where: str, errors: list) -> dict:
    out = {}
    if not isinstance(cmds, dict):
        errors.append(f"{where}: commands must be a mapping")
        return out
    for phrase, value in cmds.items():
        p = normalize_phrase(phrase)
        if not p:
            errors.append(f"{where}: a command has no phrase")
            continue
        if not re.fullmatch(r"[a-z' ]+", p):
            errors.append(f"{where}: '{p}' - phrases can only use letters, spaces and apostrophes")
        if p in out:
            errors.append(f"{where}: '{p}' is listed twice")
        b = split_binding(value)
        if b["keyboard"] is None and b["controller"] is None:
            errors.append(f"{where} / '{p}': set an input for keyboard, controller or both")
        for m in MODES:
            if b[m] is not None:
                validate_action(b[m], f"{where} / '{p}' ({m})", errors)
        out[p] = b
    return out


def validate_config(data: dict) -> tuple[dict, list]:
    errors: list[str] = []
    cfg = {
        "model": data.get("model", "vosk-model-small-en-us-0.15"),
        "fire_on_partial": bool(data.get("fire_on_partial", True)),
        "block_ms": data.get("block_ms", 50),
        "input_mode": data.get("input_mode") if data.get("input_mode") in MODES else "keyboard",
        "global": validate_commands(data.get("global") or {}, "Global", errors),
        "profiles": [],
    }
    names = set()
    for p in data.get("profiles") or []:
        name = str(p.get("name", "")).strip()
        if not name:
            errors.append("A profile has no name")
            name = "Unnamed profile"
        elif name in names:
            errors.append(f"Two profiles are named '{name}'")
        names.add(name)
        m = p.get("match") or {}
        kind, value = m.get("type"), str(m.get("value", ""))
        if kind not in MATCH_TYPES:
            errors.append(f"{name}: unknown window rule '{kind}'")
        if kind == "title_regex" and value:
            try:
                re.compile(value)
            except re.error as e:
                errors.append(f"{name}: window regex is invalid ({e})")
        cfg["profiles"].append({
            "name": name,
            "match": {"type": kind, "value": value},
            "commands": validate_commands(p.get("commands") or {}, name or "Profile", errors),
        })
    return cfg, errors


def save_config(path: Path, cfg: dict):
    out = {
        "model": cfg["model"],
        "fire_on_partial": cfg["fire_on_partial"],
        "block_ms": cfg["block_ms"],
        "input_mode": cfg["input_mode"],
        "global": {k: join_binding(b) for k, b in cfg["global"].items()},
        "profiles": [{"name": p["name"],
                      "match": {p["match"]["type"]: p["match"]["value"]},
                      "commands": {k: join_binding(b) for k, b in p["commands"].items()}}
                     for p in cfg["profiles"]],
    }
    body = yaml.safe_dump(out, sort_keys=False, default_flow_style=None,
                          allow_unicode=True, width=100)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(HEADER + body, encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- engine

class Engine:
    def __init__(self, cfg_path: Path):
        self.cfg_path = cfg_path
        self.lock = threading.Lock()
        self.executor = Executor()
        self.executor.on_error = self._action_error
        self.running = threading.Event()
        self.version = 0
        self.window = {"title": "", "process": ""}
        self.profile: str | None = None
        self.last_heard = ""
        self.last_fired = ""
        self.last_ms = None
        self.error = ""
        self._recs: dict = {}
        self.pad_ok = pad_available()

        self.cfg = load_config(cfg_path)
        if self.cfg["migrated"]:
            print("commands.yaml uses the old single-list format. It will be converted when "
                  "you save from the UI (the original is kept as commands.yaml.bak).")
        SetLogLevel(-1)
        model_dir = Path(self.cfg["model"])
        if not model_dir.is_absolute():
            model_dir = cfg_path.parent / model_dir
        if not (model_dir / "am").is_dir():
            sys.exit(f"Vosk model not found at {model_dir}\n(expected an 'am' folder directly "
                     f"inside it - if you see {model_dir.name}\\{model_dir.name}, move the inner "
                     f"folder's contents up one level)")
        print("Loading speech model...")
        self.model = Model(str(model_dir))

        threading.Thread(target=self._watch_windows, daemon=True).start()
        threading.Thread(target=self._audio_loop, daemon=True).start()

    # -- config

    def set_config(self, cfg: dict):
        if self.cfg.get("migrated"):
            bak = self.cfg_path.with_suffix(".yaml.bak")
            if not bak.exists():
                bak.write_text(self.cfg_path.read_text(encoding="utf-8"), encoding="utf-8")
        save_config(self.cfg_path, cfg)
        with self.lock:
            self.cfg = {**cfg, "migrated": False}
            self._recs.clear()
            self.version += 1
            self.profile = self._match_profile(self.window)

    def _match_profile(self, win) -> str | None:
        for p in self.cfg["profiles"]:
            if matches(p["match"], win):
                return p["name"]
        return None

    def active_commands(self) -> dict:
        """Phrase -> action for the matched profile + global, in the current input mode.
        Phrases with nothing set for this mode are left out of the vocabulary."""
        merged = dict(self.cfg["global"])
        for p in self.cfg["profiles"]:
            if p["name"] == self.profile:
                merged.update(p["commands"])
        mode = self.cfg["input_mode"]
        return {p: b[mode] for p, b in merged.items() if b.get(mode) is not None}

    def set_mode(self, mode: str):
        with self.lock:
            self.cfg["input_mode"] = mode
            self._recs.clear()
            self.version += 1  # makes the audio loop switch vocabulary
            if not self.cfg.get("migrated"):  # old-format files are only rewritten by Save
                save_config(self.cfg_path, self.cfg)
        self.executor.release_all()  # nothing stays held from the other mode
        print(f"  [input mode: {mode}]")
        if mode == "controller" and self.running.is_set() and self.uses_pad():
            try:
                self.executor.ensure_pad()
            except RuntimeError as e:
                self.error = str(e)

    def _recognizer(self, rate: int):
        """Recognizer limited to the active commands' words, cached per profile."""
        key = (self.profile, rate, self.cfg["input_mode"])
        if key not in self._recs:
            cmds = self.active_commands()
            if not cmds:
                self._recs[key] = (cmds, None, None)
            else:
                # Restricting the vocabulary to our phrases is what makes this fast and accurate.
                # "[unk]" soaks up everything else (game audio, chatter) instead of forcing a match.
                grammar = sorted({w for p in cmds for w in p.split()}) + list(cmds) + ["[unk]"]
                rec = KaldiRecognizer(self.model, rate, json.dumps(grammar))
                self._recs[key] = (cmds, Matcher(cmds), rec)
        return self._recs[key]

    # -- control

    def _action_error(self, msg: str):
        print(f"  !! {msg}")
        self.error = msg

    def uses_pad(self) -> bool:
        def pad_action(a) -> bool:
            if not isinstance(a, dict):
                return False
            if any(k in a for k in ("pad_tap", "pad_hold", "pad_release", "stick")):
                return True
            return any(pad_action(s) for s in a.get("sequence") or [])
        bindings = list(self.cfg["global"].values())
        for p in self.cfg["profiles"]:
            bindings += p["commands"].values()
        return any(pad_action(b.get("controller")) for b in bindings)

    def start(self):
        self.error = ""
        if self.cfg["input_mode"] == "controller" and self.uses_pad():
            # Plug the controller in now, so the game has seen it before the first command.
            try:
                self.executor.ensure_pad()
            except RuntimeError as e:
                self.error = str(e)  # keyboard commands still work
        self.executor.listening = True
        self.running.set()

    def stop(self):
        self.running.clear()
        self.executor.release_all()

    def status(self) -> dict:
        return {
            "running": self.running.is_set(),
            "listening": self.executor.listening,
            "window": self.window,
            "profile": self.profile,
            "last_heard": self.last_heard,
            "last_fired": self.last_fired,
            "last_ms": None if self.last_ms is None else round(self.last_ms),
            "error": self.error,
            "version": self.version,
            "input_mode": self.cfg["input_mode"],
            "pad_available": self.pad_ok,
            "pad_connected": self.executor.pad is not None,
        }

    # -- threads

    def _watch_windows(self):
        while True:
            try:
                win = foreground_window()
            except OSError:
                win = {"title": "", "process": ""}
            with self.lock:
                self.window = win
                prof = self._match_profile(win)
                if prof != self.profile:
                    self.profile = prof
                    self.executor.release_all()  # never leave keys held in another window
            time.sleep(0.2)

    def _on_done(self, phrase, ms):
        self.last_fired, self.last_ms = phrase, ms

    def _audio_loop(self):
        while True:
            self.running.wait()
            try:
                self._listen()
            except Exception as e:  # mic unplugged, device busy, ...
                self.error = f"Audio error: {e}"
                print(self.error)
                self.running.clear()
            self.executor.release_all()

    def _listen(self):
        rate = int(sd.query_devices(kind="input")["default_samplerate"])
        audio: queue.Queue = queue.Queue()
        block = int(rate * self.cfg.get("block_ms", 50) / 1000)
        state_key, cmds, matcher, rec, fired = None, {}, None, None, 0

        def fire(found):
            for c in found:
                phrase = " ".join(c)
                self.executor.submit(phrase, cmds[phrase], self._on_done)

        with sd.RawInputStream(samplerate=rate, blocksize=block, dtype="int16", channels=1,
                               callback=lambda data, *_: audio.put(bytes(data))):
            print("Listening.")
            while self.running.is_set():
                try:
                    data = audio.get(timeout=0.2)
                except queue.Empty:
                    continue
                with self.lock:
                    key = (self.profile, self.version)
                    if key != state_key:  # window or config changed: switch vocabulary
                        state_key = key
                        cmds, matcher, rec = self._recognizer(rate)
                        if rec:
                            rec.Reset()
                        fired = 0
                if rec is None:
                    continue
                if rec.AcceptWaveform(data):
                    text = json.loads(rec.Result()).get("text", "")
                    found = matcher.parse(text)
                    if text and text != "[unk]":
                        print(f"heard: {text}")
                        self.last_heard = text
                    fire(found[fired:])
                    fired = 0
                elif self.cfg.get("fire_on_partial", True):
                    found = matcher.parse(json.loads(rec.PartialResult()).get("partial", ""))
                    if found and found[-1] in matcher.prefixes:
                        found = found[:-1]  # might still become a longer command
                    if len(found) > fired:
                        fire(found[fired:])
                        fired = len(found)
        print("Stopped.")


# --------------------------------------------------------------------------- web UI

def make_handler(engine: Engine, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _trusted(self) -> bool:
            # Only this page may talk to the API: blocks other websites (and DNS
            # rebinding) from rewriting the mappings or toggling the mic.
            if self.headers.get("Host") not in allowed_hosts:
                return False
            origin = self.headers.get("Origin")
            return origin is None or origin.split("//", 1)[-1] in allowed_hosts

        def do_GET(self):
            if not self._trusted():
                return self._send(403, {"error": "forbidden"})
            if self.path in ("/", "/index.html"):
                return self._send(200, (HERE / "ui.html").read_bytes(), "text/html; charset=utf-8")
            if self.path == "/api/config":
                with engine.lock:
                    return self._send(200, {**engine.cfg, "keys": sorted(KEYS)})
            if self.path == "/api/status":
                return self._send(200, engine.status())
            if self.path == "/api/windows":
                return self._send(200, list_windows())
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._trusted() or self.headers.get("Content-Type") != "application/json":
                return self._send(403, {"error": "forbidden"})
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0)
            if self.path == "/api/start":
                engine.start()
                return self._send(200, engine.status())
            if self.path == "/api/stop":
                engine.stop()
                return self._send(200, engine.status())
            if self.path == "/api/mode":
                try:
                    mode = json.loads(body).get("mode")
                except (ValueError, AttributeError):
                    mode = None
                if mode not in MODES:
                    return self._send(400, {"errors": ["mode must be keyboard or controller"]})
                engine.set_mode(mode)
                return self._send(200, engine.status())
            if self.path == "/api/config":
                try:
                    data = json.loads(body)
                except ValueError:
                    return self._send(400, {"errors": ["Request was not valid JSON"]})
                cfg, errors = validate_config(data)
                if errors:
                    return self._send(400, {"errors": errors})
                engine.set_config(cfg)
                return self._send(200, {"ok": True, "version": engine.version})
            self._send(404, {"error": "not found"})

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Voice commands to game input, with a web editor.")
    ap.add_argument("config", nargs="?", default=str(HERE / "commands.yaml"))
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="don't open the editor on start")
    ap.add_argument("--start", action="store_true", help="start listening right away")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # show log lines immediately even when piped

    engine = Engine(Path(args.config).resolve())
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(engine, args.port))
    url = f"http://127.0.0.1:{args.port}"
    print(f"Editor: {url}   (Ctrl+C to quit)")
    if args.start:
        engine.start()
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()


if __name__ == "__main__":
    main()

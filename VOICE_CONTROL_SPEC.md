# Voice Control for Games: Handoff Spec

## Goal

Build a Windows desktop program that listens to the microphone, recognizes short spoken commands, and sends the matching keyboard/mouse input to the game in focus. It adds to normal controls; it doesn't replace them.

Example: the user says **"jump"** → the program taps **Space** → the character jumps.

## Target environment

- **OS:** Windows 10/11 x64. Games run in the foreground; this program runs in the background (terminal or tray).
- **Language:** Python 3.10+. A starter implementation is included below.
- **Mic:** headset mic assumed. Game audio may leak into the mic.

## Core requirements

1. **Low latency is the top priority.** The time from the end of a spoken word to the input should be **≤ ~200 ms**. Anything slower feels broken in a game.
2. **Fully offline.** No cloud speech-to-text calls.
3. **Configurable mappings** in a YAML file: phrase → action. Editing the file and restarting should be enough (hot-reload is a nice-to-have).
4. **Input must work in games.** Many games use DirectInput/Raw Input and ignore virtual-key `SendInput` events. Use **scan-code** injection (`KEYEVENTF_SCANCODE`). The `pydirectinput` library does this.
5. **Few false triggers.** Game audio, music, and chatter must not trigger commands.
6. **Safety:** releasing everything (the "stop" command, Ctrl+C on exit, or pausing voice control) must release every held key so nothing stays stuck.

## Key design decisions (already made; keep them unless there's a strong reason)

| Concern | Decision | Why |
|---|---|---|
| Speech recognition | **Vosk** (Kaldi) with a **restricted grammar** made of only the configured phrases plus `[unk]` | Free-form STT (Whisper etc.) waits for end of utterance → 0.5–1 s+ latency. A grammar-constrained streaming recognizer is fast and accurate, and `[unk]` absorbs noise. |
| Firing time | Act on **partial results** (mid-utterance), not only final results | Saves 100–300 ms. "jump" fires while it's still being said. |
| Prefix ambiguity | Don't fire a partial match if that phrase is a word-prefix of a longer phrase (e.g. "quick" vs "quick save"); wait for more words/final | Prevents firing the wrong command. |
| Multi-command utterances | Greedy longest-match over recognized words; track how many commands were already fired for the current utterance, and fire only the new ones on later partial/final results | Handles "sprint forward stop" in one breath without double-firing. |
| Input injection | `pydirectinput` with `PAUSE = 0` | Scan codes work in DirectX games; the default 100 ms pause per call kills latency. |
| Tap duration | Hold key ~30 ms by default (configurable per command) | Games polling at low FPS miss instant down/up taps. |
| Execution | Actions run on a **worker thread** fed by a queue | Sequences with waits must not block the audio loop. |
| Audio | `sounddevice.RawInputStream`, mono int16, device's default sample rate, ~50 ms blocks | Small blocks = snappier partials. Vosk accepts the device rate directly. |

## Action types (config schema)

```yaml
model: vosk-model-small-en-us-0.15   # unzipped Vosk model folder (relative to config)
fire_on_partial: true
block_ms: 50

commands:
  <phrase>: <action>
```

| Action | Example | Behavior |
|---|---|---|
| `tap` | `{tap: space}` / `{tap: s, duration: 0.6}` | key down, sleep `duration` (default 0.03 s), key up |
| `hold` | `{hold: shift}` | key down, stays held (tracked in a `held` set) |
| `release` | `{release: shift}` | key up, remove from `held` |
| `release_all` | `{release_all: true}` | release every held key |
| `click` | `{click: left}` | mouse click (left/right/middle) |
| `sequence` | `{sequence: [{tap: space}, {wait: 0.15}, {tap: space}]}` | steps in order; `{wait: s}` sleeps |
| `listen` | `{listen: false}` / `{listen: true}` | pause/resume voice control. Always honored even while paused; pausing also releases all held keys |

Key names follow pydirectinput: `a`–`z`, `0`–`9`, `space`, `shift`, `ctrl`, `alt`, `tab`, `enter`, `esc`, `f1`–`f12`, arrows, etc.

## Setup

```
pip install vosk sounddevice pyyaml pydirectinput
```

Download `vosk-model-small-en-us-0.15` from https://alphacephei.com/vosk/models and unzip it next to the script.
Run: `python voice_control.py [path/to/commands.yaml]`

## Known gotchas

- **UIPI:** if the game runs elevated (as Administrator), the script must too, or Windows silently drops the injected input.
- **Anti-cheat:** competitive online games (EAC, BattlEye, Vanguard) may flag or block injected input. Target single-player/offline games.
- **Phrase choice:** short words that sound alike collide ("jump"/"dump", "left"/"lift"). Prefer distinct, 1–2 word phrases.
- **Partial-result revision:** Vosk can occasionally revise earlier words in a partial result. The current "fired count" approach accepts this small risk.
- **Original `pydirectinput`** is lightly maintained; `pydirectinput-rgx` is a drop-in fork with more keys and fixes if needed.

## Starter implementation status

- ✅ Written: full pipeline (mic → Vosk grammar → matcher → worker-thread executor → pydirectinput).
- ✅ Tested: `Matcher` parsing (single, multi-word, multi-command, `[unk]` filtering, prefix detection) with stubbed dependencies.
- ❌ Not yet tested: live mic capture and input injection on real Windows hardware. **This is the first thing to verify.**

## Suggested next steps (priority order)

1. **Verify on Windows**: run against Notepad first, then a real game. Measure latency (the script prints execute time; add a timestamp from the first partial match too).
2. **Push-to-talk / hotkey toggle** (e.g. hold a mouse side button to listen), as an alternative to always-listening, to cut false triggers further.
3. **Per-game profiles**: pick the YAML based on the foreground window's process name (`win32gui.GetForegroundWindow` + `psutil`).
4. **Repeat-while-speaking** for movement (e.g. keep `w` held while the user keeps saying "forward").
5. **Small overlay or tray icon** showing listening state and last heard command.
6. **Hot-reload** of commands.yaml on file change.
7. Optional: a confidence threshold using Vosk's word-level `conf` (`rec.SetWords(True)`) to reject weak matches.

---

## File: `commands.yaml`

```yaml
model: vosk-model-small-en-us-0.15
fire_on_partial: true
block_ms: 50

commands:
  jump:          {tap: space}
  crouch:        {tap: ctrl}
  reload:        {tap: r}
  interact:      {tap: e}
  inventory:     {tap: tab}
  map:           {tap: m}
  quick save:    {tap: f5}
  quick load:    {tap: f9}

  sprint:        {hold: shift}
  walk:          {release: shift}
  forward:       {hold: w}
  back up:       {tap: s, duration: 0.6}
  stop:          {release_all: true}

  attack:        {click: left}
  block:         {click: right}
  double jump:   {sequence: [{tap: space}, {wait: 0.15}, {tap: space}]}
  heal:          {sequence: [{tap: "4"}, {wait: 0.05}, {click: left}]}

  voice off:     {listen: false}
  voice on:      {listen: true}
```

## File: `voice_control.py`

```python
"""
voice_control.py - say a command, the game gets the input.

Setup (Windows):
    pip install vosk sounddevice pyyaml pydirectinput
    Download vosk-model-small-en-us-0.15 from https://alphacephei.com/vosk/models
    and unzip it next to this script. Then:  python voice_control.py [commands.yaml]

Run the terminal as Administrator if the game runs elevated, otherwise Windows
silently drops the injected input.
"""
import json
import queue
import sys
import threading
import time
from pathlib import Path

import pydirectinput as pdi
import sounddevice as sd
import yaml
from vosk import KaldiRecognizer, Model, SetLogLevel

pdi.PAUSE = 0  # pydirectinput sleeps 100 ms after every call by default


class Executor:
    """Runs actions on a worker thread so sequences never stall the audio loop."""

    def __init__(self):
        self.jobs: queue.Queue = queue.Queue()
        self.held: set[str] = set()
        self.listening = True
        threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, phrase: str, action: dict):
        if "listen" in action:  # control commands bypass the queue and pause state
            self.listening = bool(action["listen"])
            if not self.listening:
                self._release_all()
            print(f"  [voice {'ON' if self.listening else 'OFF'}]")
            return
        if self.listening:
            self.jobs.put((phrase, action, time.perf_counter()))

    def _loop(self):
        while True:
            phrase, action, t0 = self.jobs.get()
            self._run(action)
            print(f"  -> {phrase}  ({(time.perf_counter() - t0) * 1000:.0f} ms to execute)")

    def _run(self, a: dict):
        if "sequence" in a:
            for step in a["sequence"]:
                if "wait" in step:
                    time.sleep(step["wait"])
                else:
                    self._run(step)
        elif "tap" in a:
            key = str(a["tap"])
            pdi.keyDown(key)
            time.sleep(a.get("duration", 0.03))  # many games miss sub-frame taps
            pdi.keyUp(key)
        elif "hold" in a:
            key = str(a["hold"])
            if key not in self.held:
                pdi.keyDown(key)
                self.held.add(key)
        elif "release" in a:
            key = str(a["release"])
            pdi.keyUp(key)
            self.held.discard(key)
        elif a.get("release_all"):
            self._release_all()
        elif "click" in a:
            pdi.click(button=a["click"])

    def _release_all(self):
        for key in list(self.held):
            pdi.keyUp(key)
        self.held.clear()


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


def main():
    cfg_path = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name("commands.yaml"))
    cfg = yaml.safe_load(cfg_path.read_text())
    commands = {k.lower(): v for k, v in cfg["commands"].items()}
    matcher = Matcher(commands)
    executor = Executor()

    SetLogLevel(-1)
    model_dir = Path(cfg.get("model", "vosk-model-small-en-us-0.15"))
    if not model_dir.is_absolute():
        model_dir = cfg_path.parent / model_dir
    model = Model(str(model_dir))

    rate = int(sd.query_devices(kind="input")["default_samplerate"])
    # Restricting the vocabulary to our phrases is what makes this fast and accurate.
    # "[unk]" soaks up everything else (game audio, chatter) instead of forcing a match.
    grammar = sorted({w for p in commands for w in p.split()}) + list(commands) + ["[unk]"]
    rec = KaldiRecognizer(model, rate, json.dumps(grammar))

    audio: queue.Queue = queue.Queue()
    block = int(rate * cfg.get("block_ms", 50) / 1000)
    on_partial = cfg.get("fire_on_partial", True)
    fired = 0  # commands already executed for the current utterance

    def fire(cmds):
        for c in cmds:
            phrase = " ".join(c)
            executor.submit(phrase, commands[phrase])

    with sd.RawInputStream(samplerate=rate, blocksize=block, dtype="int16", channels=1,
                           callback=lambda data, *_: audio.put(bytes(data))):
        print(f"Listening ({len(commands)} commands). Ctrl+C to quit.")
        try:
            while True:
                data = audio.get()
                if rec.AcceptWaveform(data):
                    text = json.loads(rec.Result()).get("text", "")
                    cmds = matcher.parse(text)
                    if text and text != "[unk]":
                        print(f"heard: {text}")
                    fire(cmds[fired:])
                    fired = 0
                elif on_partial:
                    cmds = matcher.parse(json.loads(rec.PartialResult()).get("partial", ""))
                    if cmds and cmds[-1] in matcher.prefixes:
                        cmds = cmds[:-1]  # might still become a longer command
                    if len(cmds) > fired:
                        fire(cmds[fired:])
                        fired = len(cmds)
        except KeyboardInterrupt:
            executor._release_all()


if __name__ == "__main__":
    main()
```

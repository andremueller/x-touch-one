# X-Touch One → macOS system bridge

One resident Python process turns a Behringer **X-Touch One** (Mackie Control mode) into a macOS
system controller: motorized fader = output volume, jog wheel = scroll, transport buttons = media
keys, plus mute / mic-mute / zoom / tab / scroll-plane buttons with LED and LCD feedback.

Python drives the Mac directly — no TouchOSC document, no Hammerspoon, no OSC hop, no virtual
Audio-MIDI bus. MIDI goes through `mido` over `python-rtmidi` (CoreMIDI here, ALSA/JACK and WinMM
elsewhere); every system action sits behind the `ActionBackend` seam (`xtouch_one/backend.py`).

## Requirements

* macOS, X-Touch One connected by USB, switched to **MC Std** or **MC Cub** (hold `STOP` + press
  the encoder knob; `MC User` does not match this note map).
* Python **3.12** — `python-rtmidi` publishes wheels only up to cp312.
* **Accessibility** permission for the interpreter that runs the bridge (keyboard, scroll and
  media-key synthesis). Check with `xtouch-one --check`, then enable the binary under System
  Settings → Privacy & Security → Accessibility.
* For the Zoom button: System Settings → Accessibility → Zoom → "Use keyboard shortcuts to zoom".
* Do not run Cubase's Mackie Control device and this bridge at the same time — both write the same
  LCD and LEDs.

## Setup

```sh
brew install python@3.12
/opt/homebrew/opt/python@3.12/bin/python3.12 -m venv .venv
.venv/bin/pip install --only-binary=:all: mido python-rtmidi pyobjc-framework-Cocoa pyobjc-framework-Quartz
.venv/bin/pip install -e .
```

`--only-binary=:all:` is deliberate: it fails loudly if a dependency would need compilation.

## Makefile shortcuts

`make help` lists every target. The everyday ones:

|Target|Effect|
|---|---|
|`make venv`|create/update `.venv` (stamp re-runs pip only when `pyproject.toml` changes)|
|`make run`|bridge in the foreground (Ctrl-C stops it)|
|`make start` / `make stop` / `make restart`|bridge in the background via PID file, SIGTERM then SIGKILL|
|`make status` / `make log`|PID + last log lines / follow the log|
|`make ports` / `make check-env`|MIDI ports / permission, audio backend, matched ports|
|`make test` / `test-protocol` / `test-engine` / `test-macos`|full suite / `mcu.py` / `engine.py` / macOS scroll axes|
|`make check`|integrity check without hardware: tests + environment|
|`make selftest`|smoke test on the real device with `--backend dummy` (no system actions)|
|`make check-all`|`check` plus `selftest`|
|`make agent-install` / `agent-uninstall`|write/remove the LaunchAgent plist (prints the `launchctl` commands)|
|`make clean` / `clean-venv`|remove caches, PID file and logs / remove `.venv`|

Variables are overridable: `make start PORT="X-Touch" AUDIO=osascript`, `make run ARGS="--log-level DEBUG"`,
`make selftest SELFTEST_SECONDS=10`.

## Usage

```sh
.venv/bin/xtouch-one --list          # MIDI ports, matched ones marked with *
.venv/bin/xtouch-one --check         # permission, audio control, backend, matched ports
.venv/bin/xtouch-one                 # run
```

|Flag|Effect|
|---|---|
|`--port NAME`|substring of the MIDI port name (default `X-Touch`)|
|`--invert-jog`|flip jog polarity|
|`--invert-scroll`|flip scroll direction|
|`--audio {auto,coreaudio,osascript}`|system volume control; `auto` probes CoreAudio and falls back to `osascript`|
|`--backend {auto,dummy}`|`dummy` logs each action instead of performing it|
|`--log-level {DEBUG,INFO,WARNING}`|stderr verbosity|
|`--install-agent` / `--uninstall-agent`|write / remove the LaunchAgent plist (prints the `launchctl` commands to run)|

### Controls

|Control (MIDI)|Action|Feedback|
|---|---|---|
|Fader (pitch bend ch 1/9)|output volume, 0–100 %|LCD row 2 `VOL  NN`; motor follows external changes|
|Jog (CC 60)|scroll, ×4 while Shift is lit|—|
|Mute (note 16)|toggle output mute|LED 16, LCD `SYSTEM`/`MUTED`\|`UNMUTE`, backlight yellow|
|Rec (note 0)|toggle input (mic) mute|LED 0, LCD `MIC`/`MUTED`\|`LIVE`, backlight red|
|Scrub (note 101)|toggle scroll plane vertical/horizontal|LED 101, LCD `SCROLL`/`VERT`\|`HORIZ`|
|Shift (note 57)|quadruples jog scroll|LED 57|
|Play (94) / Stop (93)|play/pause|LED flash ~120 ms|
|Rewind (91) / FF (92)|previous / next track|LED flash|
|Channel ◀ (48) / ▶ (49)|previous / next tab (⌃⇧⇥ / ⌃⇥)|LED flash + LCD notice `TAB`/`< PREV` \| `NEXT >` for 1.2 s, then the previous display returns|
|Zoom (100)|⌥⌘8 accessibility zoom|LED flash|

Stop sends play/pause: macOS has no `NX_KEYTYPE_STOP`. Unassigned by design: transport Record
(note 95), the encoder (CC 16 / note 32), the foot switch, the 8-LED meter, the 7-segment display.

### Autostart

```sh
.venv/bin/xtouch-one --install-agent
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.xtouch-one.bridge.plist
launchctl kickstart -k gui/$(id -u)/com.xtouch-one.bridge
```

The bridge never calls `launchctl` itself; it only writes the plist and prints these commands.
`KeepAlive` restarts it after a crash; logs land in `~/Library/Logs/xtouch-one.log`.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Protocol tests are byte-exact and need no hardware or audio device.

## Implementation notes

* `xtouch_one/mcu.py` — protocol constants and `mido.Message` builders (no I/O): SysEx id
  `00 00 66 14`, LCD `0x12` with row offsets `0x00`/`0x38` (7 ASCII chars, space padded),
  colour `0x72` (8 slots), jog CC 60 signed-bit deltas, pitch-wheel bias 8192.
* `xtouch_one/midi.py` — port matching by substring, callback → queue, `rescan()` name check.
* `xtouch_one/engine.py` — mapping and state; the 60 Hz tick expires LED flashes and transient LCD
  notices, flushes coalesced volume writes and reflects audio state changes without calling back
  into the backend. The LCD has one persistent state (volume, mute, scroll plane) plus a notice
  layer: `_notice_lcd()` shows something for `LCD_NOTICE_MS` and any state change cancels it.
* `xtouch_one/macos.py` — Quartz keyboard/scroll events, `NSEvent` system-defined events for the
  media keys, and CoreAudio default-device volume/mute.
  CoreAudio property I/O is bound with `ctypes` because pyobjc's CoreAudio bridge cannot express
  `AudioObjectGetPropertyData`: the length of its out-data argument is itself an `inout` argument,
  so pyobjc passes a NULL buffer and CoreAudio answers `kAudioHardwareIllegalOperationError`. All
  other macOS APIs use pyobjc. Events created by `CGEventCreate*` are owned by pyobjc and released
  by GC — calling `CFRelease` on them crashes the process.
  Scroll events carry one axis vertically and two horizontally: the wheel-count argument of
  `CGEventCreateScrollWheelEvent2` decides how many axes are read, so horizontal needs
  `wheelCount=2` plus `wheelCount + 2` values (pyobjc's variadic bridge). Measured against a real
  scroll view: positive jog detents move the content down/right, and `--invert-scroll` flips both
  axes.
* Audio writes are coalesced: `set_volume` only records the newest value, `tick()` writes it (at
  most 20 Hz through `osascript`, which spawns a process per write).

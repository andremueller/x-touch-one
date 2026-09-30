# X-Touch One → macOS System Bridge (TouchOSC root script + Hammerspoon helper)

## Context

Goal: when Cubase is not in use, the Behringer X-Touch One (MC mode) becomes a macOS system controller — motorized
fader = output volume, jog wheel = scroll, transport = media keys, plus mute / mic / zoom / tab / scroll-plane
buttons, with LED + LCD feedback. Two processes cooperate: a **TouchOSC document-root Lua script** owns the
X-Touch One MIDI port and all LCD/LED/cursor feedback, and a **Hammerspoon helper** performs the macOS actions.

`PLAN.md` (the spec under review) is not implementable as written; every item below is verified against a primary
source:

| Claim in PLAN.md | Reality |
| --- | --- |
| `os.execute("osascript …")` for every action | TouchOSC's sandbox is **Lua 5.1** and `os` exposes only `clock`, `date`, `difftime`, `time` — there is no `os.execute` and no `io` (hexler.net/touchosc/manual/script-functions-lua). All action execution must move to an external process. |
| `statusByte & 0xF0`, `data2 << 7`, `\|`, `>>` | Lua 5.1 has no bitwise operators; TouchOSC back-ports Lua 5.2's `bit32` table. The script as written is a **syntax error**. |
| `osascript … key code 16/18/19/47 using {command down}` = media keys | System Events cannot emit `NX_SYSDEFINED` events; those codes are kVK_ANSI_Y / 1 / 2 / Period (⌘Y, ⌘1, ⌘2, ⌘.). Media keys require `hs.eventtap.event.newSystemKeyEvent`. |
| Zoom button = `key code 24 using {option down, command down}` | 24 = `kVK_ANSI_Equal` → zoom **in**. Toggle is ⌥⌘8 (`key code 28`). |
| "Enable Fader Touch Sensitivity: send Note On 104" | Note 104 is only ever **sent by** the device when the fader is touched; the host cannot trigger it. |
| "Send MCU Host Wakeup SysEx F0 00 00 66 14 00 F7" | Byte-correct (Device Query, host→device), but no source documents any handshake requirement for the X-Touch One; no challenge/response may be attempted. |
| Jog decode `data2 > 64 and data2 - 128 or data2` | MCU relative encoders use **signed-bit** (0x01 = +1, 0x41 = −1). The two's-complement formula turns a −1 detent into −63. |
| LCD backlight "Yellow / Red / Green" | Plausible but cosmetic-only; the One has one **colour** LCD scribble strip (Behringer QSG "Display: Channel display — Color LCD scribble strip x 1"), driven in MC mode by Mackie colour SysEx `0x72`. |

Everything else in the old mapping matrix is **correct** and is carried over: Rec 0, Mute 16, Channel < / > 48/49,
F4 57, Rewind 91, FF 92, Stop 93, Play 94, Zoom 100, Scrub 101, fader touch 104, jog CC 60, LCD instruction `0x12`
with row offsets `0x00` / `0x38`.

Approved decisions: Hammerspoon helper · manual Cubase/system toggle · Stop sends the Play/Pause key (macOS has no
`NX_KEYTYPE_STOP`) · stateful LEDs for mute/mic/scrub/shift + 1 Hz volume/mute sync from the helper.

## Architecture

```
X-Touch One (MC Std / MC Cub)
   │ USB MIDI: notes, CC 60, pitch bend, SysEx
   ▼
TouchOSC (control-surface mode) ── root script ──► LCD/LED/pitch-bend back to X-Touch One (MIDI conn 1)
   │ OSC/UDP 127.0.0.1:9000  (int32 args)
   ▼
Hammerspoon helper ──► hs.eventtap (media keys, scroll, keystrokes), hs.audiodevice (volume/mute/input)
   │ OSC/UDP 127.0.0.1:9001  /xt/state at 1 Hz on change
   └──────────────────────────► TouchOSC (OSC conn 1 receive port)
```

**Mode switching (manual, approved):** the One's LCD/LEDs are host-owned; Cubase and TouchOSC must not both drive
them. While Cubase's Mackie Control device is active, leave TouchOSC in **editor** mode (or quit it). TouchOSC must
be in control-surface mode for `init` / `update` / `onReceiveMIDI` to run at all (hexler manual, Script editor).

### Bridge wire contract (fixed; both sides implement exactly this)

Every message carries **exactly one int32 argument** (never zero — avoids the empty-argument-list case in
TouchOSC's complex OSC form). OSC strings are padded to a multiple of 4 bytes **including** the terminating NUL:
`L = (floor(#s / 4) + 1) * 4`.

| Dir | Path | arg | Helper behaviour |
| --- | --- | --- | --- |
| X→H | `/xt/volume` | 0–100 | `hs.audiodevice.defaultOutputDevice():setOutputVolume(v)` |
| X→H | `/xt/mute` | 0/1 | `:setOutputMuted(v == 1)` (absolute) |
| X→H | `/xt/mic` | 0/1 | input mute absolute (see fallback below) |
| X→H | `/xt/play` | 1 | `newSystemKeyEvent('PLAY')` down+up |
| X→H | `/xt/stop` | 1 | `newSystemKeyEvent('PLAY')` down+up (no STOP key exists on macOS) |
| X→H | `/xt/next` | 1 | `newSystemKeyEvent('NEXT')` down+up |
| X→H | `/xt/prev` | 1 | `newSystemKeyEvent('PREVIOUS')` down+up |
| X→H | `/xt/zoom` | 1 | `hs.eventtap.keyStroke({'alt','cmd'}, '8')` |
| X→H | `/xt/tabnext` | 1 | `hs.eventtap.keyStroke({'ctrl'}, 'tab')` |
| X→H | `/xt/tabprev` | 1 | `hs.eventtap.keyStroke({'ctrl','shift'}, 'tab')` |
| X→H | `/xt/scrollv` | ±n pixels | n × `newScrollEvent({0, -10 * sign}, {}, SCROLL_UNIT)` (positive arg = scroll down) |
| X→H | `/xt/scrollh` | ±n pixels | n × `newScrollEvent({-10 * sign, 0}, {}, SCROLL_UNIT)` (positive arg = scroll right) |
| X→H | `/xt/hello` | 1 | push `/xt/state` immediately |
| H→X | `/xt/state` | vol, muted, mic | TouchOSC updates LCD + fader + mute/mic LEDs |

## Step 1 — Hammerspoon helper

Create `hammerspoon/xtouch_system.lua`; create/append one line to `~/.hammerspoon/init.lua`:
`require("xtouch_system")`. Install by symlink (keeps repo authoritative):
`ln -sf "$PWD/hammerspoon/xtouch_system.lua" ~/.hammerspoon/xtouch_system.lua`.

Constants and state (top of file, exact names):

```lua
local IN_PORT, OUT_HOST, OUT_PORT = 9000, "127.0.0.1", 9001
local POLL_INTERVAL_S = 1
local SCROLL_UNIT   = "pixel"   -- contingency: "line" (see Assumptions)
local SCROLL_PX     = 10
local last = { volume = nil, muted = nil, mic = nil }
local savedInputVolume = nil    -- input level captured when the mic is muted
```

Functions to implement (Hammerspoon API names are exact, all documented on www.hammerspoon.org/docs):

1. `beInt32(s, pos)` → signed 32-bit integer from `s:byte(pos, pos+3)` by pure arithmetic (`((b1*256+b2)*256+b3)*256+b4`,
   minus 2^32 when ≥ 2^31). Do **not** use `string.unpack` — avoid depending on the bundled Lua version.
2. `pad4(s)` → `s .. string.rep("\0", (math.floor(#s / 4) + 1) * 4 - #s)`.
3. `oscDecode(data)` → `path, args`:
   * path terminator `z = data:find("\0", 1, true)`; if absent return `nil, {}`; `path = data:sub(1, z - 1)`.
   * `p = (math.floor((z - 1) / 4) + 1) * 4 + 1` (start of the type-tag string);
     `tz = data:find("\0", p, true)`; `tags = data:sub(p, tz - 1)`.
   * `q = (math.floor((tz - 1) / 4) + 1) * 4 + 1` (start of the first argument); walk `tags` from index 2:
     `'i'` → append `beInt32(data, q)`, `q = q + 4`; `'f'` → `q = q + 4` (skipped; this bridge never sends floats).
   * A missing/truncated argument must yield `args[i] == nil`, never an error — a malformed packet must not kill the
     socket callback.
4. `oscEncode(path, ints)` → `pad4(path) .. pad4("," .. string.rep("i", #ints)) ..` big-endian 4-byte value per int.
5. `mediaKey(name)` → `hs.eventtap.event.newSystemKeyEvent(name, true):post()` then `(name, false):post()`, wrapped in
   `pcall`, logging a warning if `hs.accessibilityState()` is false.
6. `setVolume(v)`, `setMuted(b)`, `setMicMuted(b)`:
   * `local dev = hs.audiodevice.defaultOutputDevice()`; nil → log `[xtouch] no default output device` and return.
   * `dev:setOutputVolume(math.max(0, math.min(100, v)))`, `dev:setOutputMuted(b)`.
   * mic: `local ind = hs.audiodevice.defaultInputDevice()`; on mute, store `savedInputVolume = ind:inputVolume()` then
     `ind:setInputMuted(true)`; on unmute `ind:setInputMuted(false)`. If `setInputMuted` returns `nil`/`false`
     (unsupported by the device), fall back to `ind:setInputVolume(0)` / `ind:setInputVolume(savedInputVolume or 100)`.
7. `scroll(steps, horizontal)` → `for i = 1, math.abs(steps) do` post
   `hs.eventtap.event.newScrollEvent(horizontal and {-SCROLL_PX * sign, 0} or {0, -SCROLL_PX * sign}, {}, SCROLL_UNIT):post()`
   with `sign = steps > 0 and 1 or -1`. Zero steps → return immediately. Cap `math.abs(steps)` at 20 per call.
8. `tabNext()`, `tabPrev()`, `zoomToggle()` — the `keyStroke` calls from the table above.
9. `pushState()` → read `dev:outputVolume()`, `dev:outputMuted()`, `ind:inputMuted()`; if any differs from `last`,
   update `last` and `sock:send(oscEncode("/xt/state", {vol, muted and 1 or 0, mic and 1 or 0}), OUT_HOST, OUT_PORT)`.
   Values that are `nil` (unsupported property) are sent as `0`.
10. `dispatch[path]` table mapping the thirteen X→H paths to handlers; unknown path → `print("[xtouch] unknown " .. path)`.
11. `onPacket(data, sockaddr)` → `local path, args = oscDecode(data)`; log `[xtouch] <path> <arg>` for every accepted
    message (this log is the verification surface); call the handler with `args[1]`.
12. Startup block (runs on `require`):
    * `hs.accessibilityState(true)` (prompts on first run), console line `[xtouch] bridge loaded`, `hs.alert.show("X-Touch One bridge ready")`.
    * `sock = hs.socket.udp.new()` + `hs.socket.udp.server(IN_PORT, onPacket):receive()` (server object passed to
      `:receive()`; keep it in a local so it is not collected).
    * `hs.timer.doEvery(POLL_INTERVAL_S, pushState)`.
    * Log both the inbound port and the outbound target so a misconfigured TouchOSC is diagnosable.

## Step 2 — TouchOSC document-root script

Create `touchosc/xtouch_one_system.lua`. Attach to the **document root** (editor panel with no control selected →
Script section), never to a control: SysEx is only processed at root level (hexler manual, editor-messages-midi).

Constants / state:

```lua
local CONN_XTOUCH = {true}   -- MIDI connection 1 = X-Touch One
local CONN_HELPER = {true}   -- OSC connection 1  = helper
local HDR = {0xF0, 0x00, 0x00, 0x66, 0x14}   -- Mackie Designs / Mackie Control device id
local LED_FLASH_MS, FADER_ECHO_SUPPRESS_MS = 120, 300
local JOG_INVERT = false     -- set true if clockwise scrolls the wrong way
local COLOR = {red = 1, green = 2, yellow = 3, blue = 4, white = 7}
local isMuted, isMicMuted, isScrubHorizontal, isShift = false, false, false, false
local volume = nil
local lastLine1, lastLine2 = nil, nil
local ledValue, flashes, flashOrder = {}, {}, {}
local lastFaderMs = 0
```

Helpers (exact names/behaviour):

* `sendHelper(path, value)` → `sendOSC({path, {{tag = "i", value = value}}}, CONN_HELPER)`.
* `sysex(body)` → table `{table.unpack(HDR), table.unpack(body), 0xF7}`; `sendMIDI(msg, CONN_XTOUCH)`.
* `setLcd(line1, line2)` → `string.format("%-7s", string.sub(s ~= nil and s or "", 1, 7))`; skip a row whose padded text
  equals the cached value; write row 1 with offset `0x00` and row 2 with offset `0x38` via `sysex({0x12, offset, bytes…})`.
* `setLed(note, on)` → skip if `ledValue[note]` already equals the new value; else
  `sendMIDI({0x90, note, on and 127 or 0}, CONN_XTOUCH)`.
* `flashLed(note)` → `setLed(note, true)`; `flashes[note] = getMillis() + LED_FLASH_MS`.
* `setColor(code)` → `sysex({0x72, code, 0, 0, 0, 0, 0, 0, 0})` (8 colour slots; slot 1 = channel 1's strip).
* `setFader(vol)` → `pb = math.floor(vol * 16383 / 100 + 0.5)`;
  `sendMIDI({0xE0, bit32.band(pb, 0x7F), bit32.rshift(pb, 7)}, CONN_XTOUCH)`.
* `showVolume()` → `setLcd("MASTER", string.format("VOL%4d", volume))` (`"VOL  42"` / `"VOL 100"`, exactly 7 chars).

`init()` (idempotent — TouchOSC may re-enter control-surface mode):

1. `sendMIDI(allLedsOff, CONN_XTOUCH)` where `allLedsOff` is a module-level prebuilt list of `{0x90, n, 0}` for
   `n = 0 … 117` (one `sendMIDI` call; TouchOSC processes consecutive messages in a single byte list).
   This clears every LED the One owns, including ones Cubase left lit.
2. `sendMIDI({0xF0,0x00,0x00,0x66,0x14,0x00,0xF7}, CONN_XTOUCH)` — MCU Device Query; log any reply. Do **not**
   implement the Host Connection challenge/response: no source requires it for this device.
3. `sendMIDI({0xF0,0x00,0x00,0x66,0x14,0x0B,0x7F,0xF7}, CONN_XTOUCH)` — LCD backlight saver set to maximum timeout so
   the display does not blank after 15 minutes. Cosmetic; drop if the One ignores it.
4. `setColor(COLOR.white)`; `setLcd("DESKTOP", "READY")`; `sendHelper("/xt/hello", 1)`.
5. Reset `isMuted, isMicMuted, isScrubHorizontal, isShift`, `ledValue`, `flashes`, `lastLine1/2`, `volume = nil`.

`onReceiveMIDI(message, connections)`:

```lua
if not connections[1] then return false end      -- message came from the helper's port, not the One
local status, d1, d2 = message[1], message[2], message[3]
local t = bit32.band(status, 0xF0)
```

* **Fader** — `t == 0xE0` (channel 1) or `t == 0xE8` (channel 9 = "MASTER" engaged). `pb = d2 * 128 + d1`;
  `v = math.floor(pb / 16383 * 100 + 0.5)`; if `v ~= volume` then set `volume = v`, `lastFaderMs = getMillis()`,
  `sendHelper("/xt/volume", v)`, `showVolume()`. Because the rounded value is compared, a full fader sweep emits ≤ 101
  OSC packets.
* **Jog** — `t == 0xB0 and d1 == 60`: `local v = bit32.band(d2, 0x3F)`; if `bit32.btest(d2, 0x40)` then `v = -v` end
  (signed-bit); if `JOG_INVERT` then `v = -v` end; if `v ~= 0` then `sendHelper(isScrubHorizontal and "/xt/scrollh" or "/xt/scrollv", v * (isShift and 4 or 1))`.
* **Notes** — `t == 0x90 and d2 > 0` dispatches on `d1`:

| Note | Action | Feedback |
| --- | --- | --- |
| 16 | toggle `isMuted`; `sendHelper("/xt/mute", …)` | `setLed(16, isMuted)`; `setLcd("SYSTEM", isMuted and "MUTED" or "UNMUTE")`; `setColor(isMuted and COLOR.yellow or COLOR.white)` |
| 0 | toggle `isMicMuted`; `sendHelper("/xt/mic", …)` | `setLed(0, isMicMuted)`; `setLcd("MIC", isMicMuted and "MUTED" or "LIVE")`; `setColor(isMicMuted and COLOR.red or COLOR.white)` |
| 101 | toggle `isScrubHorizontal` | `setLed(101, …)`; `setLcd("SCROLL", isScrubHorizontal and "HORIZ" or "VERT")` |
| 57 | toggle `isShift` (modifier only) | `setLed(57, isShift)` |
| 94 | `sendHelper("/xt/play", 1)` | `flashLed(94)` |
| 93 | `sendHelper("/xt/stop", 1)` | `flashLed(93)` |
| 91 | `sendHelper("/xt/prev", 1)` | `flashLed(91)` |
| 92 | `sendHelper("/xt/next", 1)` | `flashLed(92)` |
| 48 | `sendHelper("/xt/tabprev", 1)` | `flashLed(48)` |
| 49 | `sendHelper("/xt/tabnext", 1)` | `flashLed(49)` |
| 100 | `sendHelper("/xt/zoom", 1)` | `flashLed(100)` |
| other | ignored | – |

  End with `return true` so the message is not routed to any control.

`onReceiveOSC(message, connections)`: only act on `message[1] == "/xt/state"`; read
`vol, muted, mic = message[2][1].value, message[2][2].value, message[2][3].value`; update `isMuted` / `isMicMuted`,
their LEDs and LCD text exactly as the note-16 / note-0 branches do; if `vol ~= volume` then set `volume = vol` and
`showVolume()`, and if `getMillis() - lastFaderMs > FADER_ECHO_SUPPRESS_MS` then `setFader(vol)` (motor follows an
externally changed volume; suppressed for 300 ms after a local fader move). **Never** echo an OSC message back —
`/xt/state` only updates local state (otherwise the helper's 1 Hz poll ping-pongs with TouchOSC).

`update()`: for every note in `flashOrder` whose `flashes[note] < getMillis()`, `setLed(note, false)` and remove it.
Nothing else — keep the handler trivial (TouchOSC kills any script whose single invocation exceeds ~200 ms).

## Step 3 — Rewrite `PLAN.md` as the corrected spec

Replace the whole file (its code block is dead code and its §3 handshake narrative is wrong):

* §1 Setup: One in **MC Std or MC Cub** mode (mode select: hold STOP + press encoder knob); TouchOSC MIDI
  connection 1 = Receive Port **and** Send Port `X-Touch One`; OSC connection 1 = Type UDP, Host `127.0.0.1`,
  Send Port `9000`, Receive Port `9001`; Hammerspoon installed, `require("xtouch_system")`, Accessibility permission
  granted, Launch-at-login enabled (Hammerspoon Preferences → General); TouchOSC must be in control-surface mode.
* §2 Mapping matrix: the verified table from this plan (control, exact MIDI message, action, feedback), including the
  corrections and the explicit note that transport RECORD (95), the encoder (CC 16 / Note 32), the foot switch, the
  8-LED meter and the 7-segment time/assignment display are **unassigned by design**.
* §3 Bridge protocol: the direction/table above, plus the OSC padding rule.
* §4 Operating notes: manual mode toggle (never run Cubase's Mackie Control and TouchOSC in control-surface mode
  simultaneously); `MC User` mode reassigns buttons and is unsupported; the scribble-strip colour is cosmetic.
* §5 Troubleshooting: nothing arrives → TouchOSC MIDI connection disabled or Receive Port empty, or the Log view is
  not open (script `print` output appears only there); buttons do nothing → Accessibility permission missing;
  wrong scroll direction → `JOG_INVERT`; stale volume → helper not running (console line absent).
* No Lua listings — the two script files are the code.

## Critical files & anchors

| File | What matters |
| --- | --- |
| `hammerspoon/xtouch_system.lua` | Entire helper: `oscDecode` padding/argument math (step 1.3), the 13-path dispatch table, `pushState` state diffing, the scroll sign convention (`-SCROLL_PX * sign` because positive Hammerspoon offsets scroll up/left). |
| `touchosc/xtouch_one_system.lua` | Root script: `init()` LED-sweep + Device Query, `onReceiveMIDI` decode (`bit32`, signed-bit jog, 0xE0/0xE8), `setLcd` row offsets `0x00`/`0x38`, `setColor` SysEx `0x72`. |
| `~/.hammerspoon/init.lua` | Only `require("xtouch_system")` must be added; preserve any existing content. |
| `PLAN.md` | Full rewrite (step 3); it is the only spec document for the project. |

## Verification

Prerequisites: X-Touch One connected and in MC Std/MC Cub; Hammerspoon running with Accessibility granted; TouchOSC
document open with both connections configured and control-surface mode active; Hammerspoon console visible;
TouchOSC Log view open (Script page).

1. **Helper in isolation** — from Terminal (octal escapes; `.i` tag, one int32):
   `printf '/xt/next\000\000\000\000,i\000\000\000\000\000\001' | nc -u -w 1 127.0.0.1 9000`
   Expect console `[xtouch] /xt/next 1` and the frontmost media app advancing one track.
   `printf '/xt/volume\000\000,i\000\000\000\000\000\062' | nc -u -w 1 127.0.0.1 9000` → console `[xtouch] /xt/volume 50`
   and `osascript -e 'output volume of (get volume settings)'` printing `50`.
   `printf '/xt/play\000\000\000\000,i\000\000\000\000\000\001' | nc -u -w 1 127.0.0.1 9000` → playback toggles.
2. **Note numbers** — press each mapped button once with the Log view open; the logged `message` must be
   `240` = Note On, note = 0 / 16 / 48 / 49 / 57 / 91 / 92 / 93 / 94 / 100 / 101 as per the table. A mismatch means the
   One is in the wrong mode (fix: hold STOP + press the encoder, select `MC Std` or `MC Cub`) — not a script bug.
3. **Fader** — move the fader: macOS output volume follows 0–100 and the LCD row 2 tracks `VOL  nn`; then press the
   Mac's volume-up key: within ~1 s the fader moves to the new value and the LCD matches (helper poll + `/xt/state`).
4. **Jog polarity** — clockwise over TextEdit must scroll towards the end of the document. Wrong direction → set
   `JOG_INVERT = true` in the TouchOSC script (no other change).
5. **Scroll plane** — press SCRUB (LCD `SCROLL / HORIZ`), then jog over a horizontally scrollable view (wide page in
   Safari): content pans horizontally; press SCRUB again → `VERT` and vertical scroll returns.
6. **Media buttons** — with a track loaded in Music or Spotify: Play toggles playback; Rewind/FF change track;
   Stop halts playback (it sends PLAY, so it resumes if already paused — expected, per the approved decision); each
   button's LED flashes for ~120 ms.
7. **Mute / Mic** — Mute: LED lights, LCD `SYSTEM / MUTED`, backlight turns yellow, output is silent, LCD reverts on
   the second press. Rec: LED lights, LCD `MIC / MUTED`, backlight red, and the input level in System Settings →
   Sound shows no signal while speaking.
8. **Zoom / Channel** — Zoom button toggles Accessibility zoom (requires System Settings → Accessibility → Zoom →
   "Use keyboard shortcuts to zoom"); Channel < / > switch browser tabs.
9. **Shift** — hold-free toggle via F4: LED lights, and one jog detent scrolls ≈4× further than with the LED off.
10. **Idle sanity** — leave the bridge running 2 minutes with no input: console quiet, no motor drift, LCD still lit
    (backlight-saver command effective), and no repeated `/xt/volume` traffic (dedupe working).

## Assumptions & contingencies

* **Runtime** — Hammerspoon (approved). `hs.eventtap` posting needs Accessibility; `hs.midi` is **not** used at all
  (Hammerspoon cannot create a virtual MIDI port — `hs.midi.createDevice` does not exist — and the bridge uses OSC).
* **Transport** — OSC/UDP on the loopback rather than an IAC MIDI bus: no extra Audio MIDI Setup step, and int32
  arguments avoid 7-bit quantisation. Replies go to a fixed `127.0.0.1:9001`, so TouchOSC's OSC connection must keep
  Receive Port = 9001.
* **Jog polarity** is the one device behaviour the sources disagree on (MCU says 0x01 = clockwise; the One's MIDI-mode
  documentation says 65 = clockwise). Verification step 4 decides it; `JOG_INVERT` is the only change needed.
* **Scroll smoothing** — `SCROLL_UNIT = "pixel"`. If an app ignores pixel-based synthetic scrolling, set
  `SCROLL_UNIT = "line"` and `SCROLL_PX = 1` in the helper; nothing else changes.
* **Colour feedback** — the Mackie `0x72` colour command is what the shipped X-Touch One Cubase script uses, but
  Behringer documents the colour byte only for its MIDI-mode SysEx. If no colour change is observed, delete the three
  `setColor` calls (cosmetic only).
* **Mic mute** — `setInputMuted` is documented but device-dependent; the input-volume fallback in step 1.6 is the
  pre-decided path when it is unsupported.
* **Stop** — macOS has no `NX_KEYTYPE_STOP`, so Stop sends PLAY (approved decision; behaviour documented in
  `PLAN.md`).
* **Mode switching** — manual (approved). No Cubase detection code is written. The consequence: while Cubase's MCU
  device is enabled, TouchOSC must stay out of control-surface mode, otherwise both hosts write the LCD/LEDs.
* **Fader authority** — the TouchOSC script is the source of truth for mute/mic/scrub/shift; the helper pushes only
  volume/mute/mic changes observed from CoreAudio. `/xt/state` never triggers an outgoing OSC message.
* **TouchOSC 200 ms script timeout** — no action may be performed inline; every handler only mutates locals and calls
  `sendMIDI`/`sendOSC`. If a timeout error appears in the Log view, the offending handler is doing too much work.

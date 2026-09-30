# X-Touch One macOS System Profile Specification (TouchOSC Scripting)

## 1. System Architecture & Setup

### Overview
This specification details a custom TouchOSC Lua bridge script to repurpose the Behringer X-Touch One as a standalone macOS system media and volume controller when Cubase is inactive.

### Prerequisites & MIDI Configuration
* **Hardware Mode:** Behringer X-Touch One set to **MC / Cubase Mode** (Firmware v1.11).
* **Communication Protocol:** Bidirectional MIDI (Notes, Relative CCs, Pitch Bend, and MCU SysEx).
* **Environment:** TouchOSC with Lua Scripting Engine running in background on macOS.
* **Automation Helpers:** `osascript` (AppleScript / System Events) or shell utilities for system volume, media keys, horizontal/vertical scrolling, and microphone muting.

---

## 2. Hardware Mapping Matrix

| Hardware Control        | Target MIDI Event | macOS Action Target                                          | Feedback / Display Output                                    |
| :---------------------- | :---------------- | :----------------------------------------------------------- | :----------------------------------------------------------- |
| **Fader**               | Pitch Bend (Ch 1) | Main Output Volume (0–100%)                                  | Pitch Bend feedback to motor; LCD Top: `"MASTER"`, LCD Bottom: `"VOL XX%"` |
| **Jog Wheel (Inner)**   | CC 60 (Relative)  | Vertical Scroll (Up/Down) or Horizontal Scroll (Left/Right) depending on `SCRUB` mode | N/A                                                          |
| **SCRUB Button**        | Note 101          | Toggle Jog Wheel Mode (Vertical vs Horizontal Scroll)        | Button LED toggles On/Off; LCD Line 1 displays `"SCROLL"` / LCD Line 2: `"VERT"` or `"HORIZ"` |
| **Play / Pause**        | Note 94           | Media Play / Pause Toggle                                    | Button LED On = Active Media Playing                         |
| **Stop**                | Note 93           | Media Stop                                                   | Button LED Momentary On                                      |
| **Rewind (`<<`)**       | Note 91           | Previous Media Track                                         | Button LED Momentary On                                      |
| **Fast Forward (`>>`)** | Note 92           | Next Media Track                                             | Button LED Momentary On                                      |
| **Channel `<` / `>`**   | Note 48 / 49      | Select Previous / Next Tab (`Ctrl + Shift + Tab` / `Ctrl + Tab`) | N/A                                                          |
| **Zoom Button**         | Note 100          | Toggle System Display Zoom Mode                              | Button LED On when System Zoom active                        |
| **Mute Button**         | Note 16           | Mute Main Audio Output (Toggle)                              | Button LED On when Output Muted; LCD Backlight: Yellow       |
| **Rec Button**          | Note 0            | Toggle System Microphone (0% vs 100% Gain)                   | Button LED On when Mic Live; LCD Backlight: Red              |
| **F4 Button**           | Note 57           | Dedicated `Shift` Modifier Toggle                            | Button LED On when Shift active                              |

---

## 3. Initialization & State Management

Because the X-Touch One remains passive after DAW shutdown, TouchOSC must explicitly handshake with the unit to wake up the capacitive touch-fader and screen elements.

### Initialization Sequence
1. **Send MCU Host Wakeup SysEx:** `F0 00 00 66 14 00 F7`
2. **Enable Fader Touch Sensitivity:** Send Note On 104 (`0x90 68 7F`) to unblock Fader Pitch Bend transmission.
3. **Set Default LCD State:** Display `"DESKTOP"` on Line 1, `"READY"` on Line 2. Set RGB Backlight to Green.

---

## 4. Complete TouchOSC Lua Script Implementation

```lua
-- TouchOSC macOS Bridge for X-Touch One (MCU Mode)
-- Target: System Controls, Media, Scrolling, and Audio Volume

local isScrubActive = false
local isShiftActive = false
local isMuted = false
local isMicActive = true

-- Utility: Send 7-character text strings to X-Touch One LCD Display via MCU SysEx
function updateLCD(line1Text, line2Text)
  -- Truncate or pad strings to exactly 7 characters
  line1Text = string.format("%-7s", string.sub(line1Text, 1, 7))
  line2Text = string.format("%-7s", string.sub(line2Text, 1, 7))

  -- Construct SysEx header for Mackie Control Universal (Device ID 0x14)
  local sysexHeader = {0xF0, 0x00, 0x00, 0x66, 0x14, 0x12}

  -- Line 1 Offset (0x00), Line 2 Offset (0x38)
  local msgLine1 = {unpack(sysexHeader)}
  table.insert(msgLine1, 0x00)
  for i = 1, #line1Text do
    table.insert(msgLine1, string.byte(line1Text, i))
  end
  table.insert(msgLine1, 0xF7)
  sendMIDI(msgLine1)

  local msgLine2 = {unpack(sysexHeader)}
  table.insert(msgLine2, 0x38)
  for i = 1, #line2Text do
    table.insert(msgLine2, string.byte(line2Text, i))
  end
  table.insert(msgLine2, 0xF7)
  sendMIDI(msgLine2)
end

-- Initialize Device State & Handshake
function initXTouchOne()
  -- MCU Host Wakeup Query
  sendMIDI({0xF0, 0x00, 0x00, 0x66, 0x14, 0x00, 0xF7})
  
  -- Unblock touch sensor for pitch bend transmission (Fader Touch Note 104)
  sendMIDI({0x90, 104, 127})

  -- Refresh LCD Screen & Set Default Mode Status
  updateLCD("DESKTOP", " READY ")
end

function onInit()
  print("[TouchOSC] Initializing X-Touch One macOS Controller...")
  initXTouchOne()
end

-- Handle Incoming MIDI Events from X-Touch One
function onReceiveMIDI(message)
  local statusByte = message[1]
  local msgType = statusByte & 0xF0
  local data1 = message[2]
  local data2 = message[3]

  -- 1. Fader Volume Handling (Pitch Bend Channel 1)
  if msgType == 0xE0 then
    local pitchVal = (data2 << 7) | data1
    local sysVol = math.floor((pitchVal / 16383) * 100)
    os.execute("osascript -e 'set volume output volume " .. sysVol .. "'")
    updateLCD("MASTER", string.format("VOL %2d%%", sysVol))
  end

  -- 2. Jog Wheel Handling (CC 60 Relative Value)
  if msgType == 0xB0 and data1 == 60 then
    local delta = (data2 > 64) and (data2 - 128) or data2
    handleJogWheel(delta)
  end

  -- 3. Note On Events (Buttons Pressed)
  if msgType == 0x90 and data2 > 0 then
    -- SCRUB Button: Toggle Scroll Plane
    if data1 == 101 then
      isScrubActive = not isScrubActive
      sendMIDI({0x90, 101, isScrubActive and 127 or 0})
      updateLCD("SCROLL", isScrubActive and " HORIZ " or " VERT  ")

    -- F4 Button: Shift Modifier
    elseif data1 == 57 then
      isShiftActive = not isShiftActive
      sendMIDI({0x90, 57, isShiftActive and 127 or 0})

    -- Play / Pause Button
    elseif data1 == 94 then
      os.execute("osascript -e 'tell application \"System Events\" to key code 16 using {command down}'")
      sendMIDI({0x90, 94, 127})

    -- Stop Button
    elseif data1 == 93 then
      os.execute("osascript -e 'tell application \"System Events\" to key code 47 using {command down}'")
      sendMIDI({0x90, 93, 127})

    -- Rewind / Previous Track (`<<`)
    elseif data1 == 91 then
      os.execute("osascript -e 'tell application \"System Events\" to key code 18 using {command down}'")

    -- Fast Forward / Next Track (`>>`)
    elseif data1 == 92 then
      os.execute("osascript -e 'tell application \"System Events\" to key code 19 using {command down}'")

    -- Channel Left / Right: Switch Active Browser/App Tab
    elseif data1 == 48 then
      os.execute("osascript -e 'tell application \"System Events\" to key code 48 using {control down, shift down}'")
    elseif data1 == 49 then
      os.execute("osascript -e 'tell application \"System Events\" to key code 48 using {control down}'")

    -- Zoom Button: Toggle Accessibility Screen Zoom
    elseif data1 == 100 then
      os.execute("osascript -e 'tell application \"System Events\" to key code 24 using {option down, command down}'")

    -- Mute Button: Global Output Mute Toggle
    elseif data1 == 16 then
      isMuted = not isMuted
      local muteVal = isMuted and "true" or "false"
      os.execute("osascript -e 'set volume output muted " .. muteVal .. "'")
      sendMIDI({0x90, 16, isMuted and 127 or 0})
      updateLCD("SYSTEM", isMuted and "MUTED  " or "UNMUTE ")

    -- Rec Button: System Microphone Gain Toggle
    elseif data1 == 0 then
      isMicActive = not isMicActive
      local inputGain = isMicActive and 100 or 0
      os.execute("osascript -e 'set volume input volume " .. inputGain .. "'")
      sendMIDI({0x90, 0, isMicActive and 127 or 0})
      updateLCD("MIC REC", isMicActive and " LIVE  " or " MUTED ")
    end
  end
end

-- Jog Wheel Processing (Horizontal vs Vertical Scroll via AppleScript System Events)
function handleJogWheel(delta)
  local directionCode = delta > 0 and 125 or 126 -- Down vs Up
  if isScrubActive then
    directionCode = delta > 0 and 124 or 123 -- Right vs Left
  end

  local absDelta = math.abs(delta)
  for i = 1, absDelta do
    os.execute("osascript -e 'tell application \"System Events\" to key code " .. directionCode .. "'")
  end
end
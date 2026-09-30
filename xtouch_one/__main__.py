"""CLI: port listing, environment check, run loop, launchd agent install."""

from __future__ import annotations

import argparse
import logging
import shutil
import signal
import sys
import threading
import time
from pathlib import Path

import mido

from . import __version__, midi
from .backend import get_backend
from .engine import Engine

log = logging.getLogger("xtouch")

AGENT_LABEL = "com.xtouch-one.bridge"
AGENT_TEMPLATE = Path(__file__).resolve().parent.parent / "launchd" / f"{AGENT_LABEL}.plist"
AGENT_TARGET = Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"
NO_ACCESS_HINT = ("[bridge] no Accessibility permission — System Settings → Privacy & "
                  "Security → Accessibility → enable the python binary")
RESCAN_INTERVAL = 2.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="xtouch-one", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--port", default="X-Touch",
                        help="substring of the MIDI port name (default: %(default)s)")
    parser.add_argument("--list", action="store_true", help="list MIDI ports and exit")
    parser.add_argument("--check", action="store_true",
                        help="print permissions, audio control, backend and matched ports; exit")
    parser.add_argument("--invert-jog", action="store_true", help="flip jog wheel polarity")
    parser.add_argument("--invert-scroll", action="store_true", help="flip scroll direction")
    parser.add_argument("--audio", choices=("auto", "coreaudio", "osascript"), default="auto",
                        help="system volume control (default: %(default)s)")
    parser.add_argument("--backend", choices=("auto", "dummy"), default="auto",
                        help="action backend; dummy logs instead of acting (default: %(default)s)")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING"), default="INFO")
    parser.add_argument("--install-agent", action="store_true", help="write the LaunchAgent plist")
    parser.add_argument("--uninstall-agent", action="store_true", help="remove the LaunchAgent plist")
    return parser.parse_args(argv)


def _marker(name: str, substring: str) -> str:
    return "*" if midi._match([name], substring) is not None else " "


def list_ports(substring: str) -> int:
    io = midi.MidiIO(substring)
    print(f"mido backend: {mido.backend}")
    print("inputs:")
    for name in io.sources():
        print(f"  {_marker(name, substring)} {name}")
    print("outputs:")
    for name in io.destinations():
        print(f"  {_marker(name, substring)} {name}")
    return 0


def check(args: argparse.Namespace) -> int:
    backend = get_backend(args.audio, args.backend, invert_scroll=args.invert_scroll)
    io = midi.MidiIO(args.port)
    inputs, outputs = io.sources(), io.destinations()
    print(f"mido backend: {mido.backend}")
    print(f"CGPreflightPostEventAccess(): {backend.preflight()}")
    print(f"audio: {getattr(backend, 'audio_control', 'dummy')}")
    print(f"port substring: {args.port!r}")
    print(f"matched input:  {midi._match(inputs, args.port)!r}")
    print(f"matched output: {midi._match(outputs, args.port)!r}")
    backend.close()
    return 0


def run(args: argparse.Namespace) -> int:
    stop = threading.Event()

    def _stop(_signum, _frame):
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _stop)

    backend = get_backend(args.audio, args.backend, invert_scroll=args.invert_scroll)
    if not backend.preflight():
        log.warning(NO_ACCESS_HINT)
    ticks = 0
    try:
        while not stop.is_set():
            io = midi.MidiIO(args.port)
            try:
                io.open()
            except midi.DeviceNotFound as exc:
                log.warning("[bridge] no MIDI port matching %r — retrying (%s)", args.port, exc)
                if stop.wait(RESCAN_INTERVAL):
                    break
                continue
            engine = Engine(io, backend, invert_jog=args.invert_jog)
            engine.start()
            last_rescan = time.monotonic()
            try:
                while not stop.is_set():
                    for msg in io.drain():
                        engine.handle(msg)
                    engine.tick()
                    ticks += 1
                    now = time.monotonic()
                    if now - last_rescan >= RESCAN_INTERVAL:
                        last_rescan = now
                        if not io.rescan():
                            log.warning("[bridge] MIDI port disappeared — reconnecting")
                            break
                    stop.wait(1.0 / Engine.TICK_HZ)
            finally:
                io.close()
    finally:
        backend.close()
    log.info("[bridge] stopped after %d ticks", ticks)
    return 0


def install_agent() -> int:
    if not AGENT_TEMPLATE.is_file():
        print(f"plist template not found: {AGENT_TEMPLATE}", file=sys.stderr)
        return 1
    exe = shutil.which("xtouch-one")
    if exe:
        program = f"<string>{exe}</string>"
    else:
        program = (f"<string>{sys.executable}</string><string>-m</string>"
                   f"<string>xtouch_one</string>")
    text = AGENT_TEMPLATE.read_text()
    text = text.replace("<string>__VENV_BIN__</string>", program)
    text = text.replace("__HOME__", str(Path.home()))
    (Path.home() / "Library" / "Logs").mkdir(parents=True, exist_ok=True)
    AGENT_TARGET.parent.mkdir(parents=True, exist_ok=True)
    AGENT_TARGET.write_text(text)
    print(f"wrote {AGENT_TARGET}")
    print(f"launchctl bootstrap gui/$(id -u) {AGENT_TARGET}")
    print(f"launchctl kickstart -k gui/$(id -u)/{AGENT_LABEL}")
    return 0


def uninstall_agent() -> int:
    print(f"launchctl bootout gui/$(id -u)/{AGENT_LABEL}")
    if AGENT_TARGET.exists():
        AGENT_TARGET.unlink()
        print(f"removed {AGENT_TARGET}")
    else:
        print(f"nothing to remove at {AGENT_TARGET}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    try:
        if args.list:
            return list_ports(args.port)
        if args.check:
            return check(args)
        if args.install_agent:
            return install_agent()
        if args.uninstall_agent:
            return uninstall_agent()
        return run(args)
    except Exception:
        log.exception("[bridge] fatal")
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""CLI: port listing, environment check, run loop, launchd agent install."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import subprocess
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


def default_lock_file() -> Path:
    """Stable, per-user lock path shared by CLI, Makefile and LaunchAgent.

    Not `tempfile.gettempdir()`: on macOS that is $TMPDIR (/var/folders/…/T), which is
    per-user and per-boot, so a foreground run and the agent would hold different locks.
    """
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return Path(state_home) / "xtouch-one" / "bridge.lock"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "xtouch-one" / "bridge.lock"
    return Path.home() / ".local" / "state" / "xtouch-one" / "bridge.lock"


LOCK_FILE = default_lock_file()
NO_ACCESS_HINT = ("[bridge] no Accessibility permission — System Settings → Privacy & "
                  "Security → Accessibility → enable the python binary")
RESCAN_INTERVAL = 2.0
EXIT_ALREADY_RUNNING = 3


class AlreadyRunning(RuntimeError):
    """Another bridge instance holds the single-instance lock."""

    def __init__(self, path: Path, pid: int | None) -> None:
        held = f" (PID {pid})" if pid is not None else ""
        super().__init__(f"another instance holds {path}{held}")
        self.path = path
        self.pid = pid


def _lock_holder(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def acquire_lock(path: Path) -> int:
    """Take the exclusive single-instance lock; returns an fd held for the process lifetime.

    The kernel drops a flock when the holder dies, so a stale PID inside the file never
    blocks a start — the number is read only to name the holder in the error.
    """
    import fcntl  # POSIX only; imported lazily so --list/--check work everywhere

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        pid = _lock_holder(path)
        os.close(fd)
        raise AlreadyRunning(path, pid) from None
    os.ftruncate(fd, 0)
    os.write(fd, b"%d\n" % os.getpid())
    os.fsync(fd)
    return fd


def lock_report(path: Path) -> str:
    """Lock state as a line, for --check/--lock-status and `make status`. Creates nothing."""
    if not path.is_file():
        return f"lock {path}: free (not created yet)"
    if not _is_locked(path):
        return f"lock {path}: free"
    pid = _lock_holder(path)
    return (f"lock {path}: held by PID {pid}" if pid is not None
            else f"lock {path}: held by an unknown PID")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="xtouch-one", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--port", default="X-Touch",
                        help="substring of the MIDI port name (default: %(default)s)")
    parser.add_argument("--list", action="store_true", help="list MIDI ports and exit")
    parser.add_argument("--check", action="store_true",
                        help="print permissions, audio control, backend and matched ports; exit")
    parser.add_argument("--lock-status", action="store_true",
                        help="print whether another instance holds the lock; exit")
    parser.add_argument("--stop", action="store_true",
                        help="SIGTERM the instance holding the lock (then SIGKILL); exit")
    parser.add_argument("--invert-jog", action="store_true", help="flip jog wheel polarity")
    parser.add_argument("--invert-scroll", action="store_true", help="flip scroll direction")
    parser.add_argument("--audio", choices=("auto", "coreaudio", "osascript"), default="auto",
                        help="system volume control (default: %(default)s)")
    parser.add_argument("--backend", choices=("auto", "dummy"), default="auto",
                        help="action backend; dummy logs instead of acting (default: %(default)s)")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING"), default="INFO")
    parser.add_argument("--lock-file", default=None,
                        help=f"single-instance lock (default: {LOCK_FILE})")
    parser.add_argument("--lcd-keepalive", type=float, default=1.0, metavar="SECONDS",
                        help="re-send unchanged LCD rows this often so the display does not "
                             "blank; 0 disables (default: %(default)s)")
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
    print(lock_report(Path(args.lock_file)))
    print(f"matched input:  {midi._match(inputs, args.port)!r}")
    print(f"matched output: {midi._match(outputs, args.port)!r}")
    backend.close()
    return 0


def _is_locked(path: Path) -> bool:
    """True while some process holds the lock (the file itself is never trusted)."""
    import fcntl

    if not path.is_file():
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return True  # unreadable: assume someone else owns it
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)
    return False


def _process_command(pid: int) -> str:
    try:
        done = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True,
                              text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip()


def stop_instance(path: Path, timeout: float = 5.0) -> int:
    """SIGTERM (then SIGKILL) whichever process holds the lock."""
    if not _is_locked(path):
        print(f"no instance running ({lock_report(path)})")
        return 0
    pid = _lock_holder(path)
    if pid is None:
        print(f"{path} is held but contains no PID — cannot stop it", file=sys.stderr)
        return 1
    command = _process_command(pid)
    if "xtouch" not in command:
        print(f"refusing to signal PID {pid}: {command!r} is not the bridge", file=sys.stderr)
        return 1
    print(f"stopping PID {pid}")
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_locked(path):
            print(f"stopped PID {pid}")
            return 0
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)
    time.sleep(0.3)
    if _is_locked(path):
        print(f"PID {pid} still holds {path} after SIGKILL", file=sys.stderr)
        return 1
    print(f"killed PID {pid} after {timeout:.0f}s")
    return 0


def run(args: argparse.Namespace) -> int:
    lock_path = Path(args.lock_file)
    try:
        lock_fd = acquire_lock(lock_path)
    except AlreadyRunning as exc:
        log.error("[bridge] %s — refusing to start; stop that instance or pass another "
                  "--lock-file", exc)
        return EXIT_ALREADY_RUNNING
    log.debug("[bridge] lock %s held (PID %d)", lock_path, os.getpid())

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
            engine = Engine(io, backend, invert_jog=args.invert_jog,
                            lcd_keepalive=args.lcd_keepalive)
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
        os.close(lock_fd)
        log.debug("[bridge] lock %s released", lock_path)
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
    if args.lock_file is None:
        args.lock_file = str(LOCK_FILE)
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    try:
        if args.list:
            return list_ports(args.port)
        if args.lock_status:
            print(lock_report(Path(args.lock_file)))
            return 0
        if args.stop:
            return stop_instance(Path(args.lock_file))
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

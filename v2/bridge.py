"""
LEGO SPIKE Prime V2 controller -> virtual Xbox controller -> Chrome -> Xbox Remote Play.

    python bridge.py --calibrate     # first time: find the levers and the tilt directions
    python bridge.py                 # play: Chrome opens on Xbox Remote Play
    python bridge.py --tester        # Chrome opens the dashboard: LEGO controller + virtual Xbox controller
    python bridge.py --no-chrome     # only print what the controller does
    python bridge.py --record        # also record the game (or the dashboard) for make_demo.py

Before running: turn the hub on, press its Bluetooth button, close the SPIKE app, and
leave both motorised buttons and the shoulder triggers at rest (that position is 0°).
All tuning values and Xbox mappings are in config.py.
"""

import argparse
import asyncio
import contextlib
import json
import math
import os
import sys
import time
from pathlib import Path

import config as cfg
from controller import ControllerLogic, check_config
from gamepad import TESTER_URL, ChromeGamepad
from spike import SpikeHub
from spike import protocol as p

HERE = Path(__file__).parent
HUB_PROGRAM = HERE / "hub_program.py"
CALIBRATION = HERE / "calibration.json"
LEVERS = ("left_button", "right_button", "trigger")


def _profile_dir() -> Path:
    """Chrome profile for Remote Play (shared with V1), outside the project: it holds your sign-in."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "lego-spike-game-controller" / "chrome-profile"


class LineReader:
    """Collects the hub's print() output, splits it into lines and wakes the main loop."""

    def __init__(self):
        self._buffer = ""
        self.lines: list[str] = []
        self.loop = asyncio.get_running_loop()
        self.arrived = asyncio.Event()

    def feed(self, text: str) -> None:
        self._buffer += text
        *done, self._buffer = self._buffer.split("\n")
        self.lines += [line.strip() for line in done if line.strip()]
        self.loop.call_soon_threadsafe(self.arrived.set)

    def take(self) -> list[str]:
        self.arrived.clear()
        lines, self.lines = self.lines, []
        return lines

    async def wait(self, timeout: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.arrived.wait(), timeout)


# ---- hub program -----------------------------------------------------------------------

async def detect_ports(hub: SpikeHub) -> dict:
    """Use the hub's sensor stream to find the force sensor, the two button motors and the large motor."""
    found: dict[str, object] = {}
    seen = asyncio.Event()

    def on_sensors(readings):
        for r in readings:
            if isinstance(r, (p.Motor, p.ForceSensor)):
                found[r.port] = r
        seen.set()

    await hub.start_sensor_stream(on_sensors, interval_ms=50)
    await asyncio.wait_for(seen.wait(), timeout=3)
    await asyncio.sleep(0.2)
    await hub.stop_sensor_stream()

    force = [port for port, r in found.items() if isinstance(r, p.ForceSensor)]
    # The motorised buttons may report as small or medium angular motors.
    buttons = sorted(port for port, r in found.items() if isinstance(r, p.Motor) and r.type in ("small", "medium"))
    large = [port for port, r in found.items() if isinstance(r, p.Motor) and r.type == "large"]
    problems = []
    if len(force) != 1:
        problems.append(f"expected 1 force sensor, found {len(force)}")
    if len(buttons) != 2:
        problems.append(f"expected 2 small/medium angular motors (motorised buttons), found {len(buttons)}")
    if len(large) != 1:
        problems.append(f"expected 1 large angular motor (shoulder triggers), found {len(large)}")
    if problems:
        seen_list = ", ".join(f"{port}: {getattr(r, 'type', 'force sensor')}" for port, r in sorted(found.items()))
        raise RuntimeError("Check the cables: " + "; ".join(problems) + f" (seen {seen_list or 'nothing'})")
    print(f"Found force sensor on {force[0]}, button motors on {' and '.join(buttons)}, large motor on {large[0]}.")
    return {"force": force[0], "buttons": buttons, "large": large[0]}


def hub_source(ports: dict, calibration: dict | None) -> str:
    """hub_program.py with its CONFIG block filled in from config.py (and the calibration)."""
    levers = []
    if calibration:
        for name in LEVERS:
            lever = calibration["levers"][name]
            if name == "trigger":
                thresholds = (cfg.TRIGGER_PULL_DEG, cfg.TRIGGER_PULL_DEG, cfg.TRIGGER_RELEASE_DEG,
                              cfg.TRIGGER_AUTO_RETURN)
            else:
                thresholds = (cfg.BUTTON_PRESS_DEG, cfg.BUTTON_BACK_DEG, cfg.BUTTON_RELEASE_DEG,
                              cfg.BUTTON_AUTO_RETURN)
            levers.append((name, lever["port"], lever["sign"], *thresholds))
    values = {
        "FORCE_PORT": ports["force"],
        "MOTOR_PORTS": [*ports["buttons"], ports["large"]],
        "LEVERS": levers,
        "REARM_DEG": cfg.REARM_DEG,
        "RETURN_SPEED": cfg.RETURN_SPEED,
        "RETURN_TIMEOUT_MS": cfg.RETURN_TIMEOUT_MS,
        "FORCE_PRESS": cfg.FORCE_PRESS,
        "FORCE_RELEASE": cfg.FORCE_RELEASE,
        "GYRO_REPORT_MS": cfg.GYRO_REPORT_MS,
        "HUB_LOOP_MS": cfg.HUB_LOOP_MS,
        "HEARTBEAT_MS": cfg.HEARTBEAT_MS,
        "INVENTORY_CLOSE_HOLD_MS": round(cfg.INVENTORY_CLOSE_HOLD_S * 1000),
        "TRIGGER_SPRING": cfg.TRIGGER_SPRING,
        "TRIGGER_SPRING_STRENGTH": cfg.TRIGGER_SPRING_STRENGTH,
        "TRIGGER_SPRING_MAX": cfg.TRIGGER_SPRING_MAX,
        "TRIGGER_SPRING_DEADBAND_DEG": cfg.TRIGGER_SPRING_DEADBAND_DEG,
        "TRIGGER_SPRING_LIMIT_DEG": cfg.TRIGGER_SPRING_LIMIT_DEG,
    }
    block = "\n".join(f"{key} = {value!r}" for key, value in values.items())
    source = HUB_PROGRAM.read_text()
    start, end = source.index("# <config>"), source.index("# </config>")
    head = source[start:].split("\n", 1)[0]
    return source[:start] + head + "\n" + block + "\n" + source[end:]


async def start_hub_program(hub: SpikeHub, ports: dict, calibration: dict | None, slot: int) -> None:
    print(f"Uploading hub_program.py to slot {slot}...")
    await hub.run_program(hub_source(ports, calibration), slot=slot)


# ---- calibration -----------------------------------------------------------------------

def tilt_degrees(tilt: dict, pitch: int, roll: int) -> tuple[float, float]:
    """
    (forward, right) tilt in degrees along the calibrated directions.

    Solves  reading - level = right * R + forward * F  for right and forward (R and F are
    the calibrated tilts, each holding both pitch and roll, so a skewed hub mount still
    gives clean directions), then scales each by the size of its calibrated tilt.
    """
    (rp, rr), (fp, fr) = tilt["right"], tilt["forward"]
    dp, dr = pitch - tilt["level"][0], roll - tilt["level"][1]
    det = rp * fr - fp * rr
    right, forward = (dp * fr - fp * dr) / det, (rp * dr - dp * rr) / det
    return forward * math.hypot(fp, fr), right * math.hypot(rp, rr)


async def next_lines(hub: SpikeHub, reader: LineReader):
    """Yield the hub's lines as they arrive (for the calibration steps)."""
    while True:
        if not hub.connected:
            raise RuntimeError("Hub disconnected during calibration")
        for line in reader.take():
            yield line.split()
        await reader.wait(0.05)


async def calibrate_levers(hub: SpikeHub, reader: LineReader, ports: dict) -> dict:
    print("\nFinding the motorised buttons and the shoulder triggers.")
    levers = {}
    for name, question, candidates in (
        ("left_button", "Press the LEFT motorised button normally (not backward)", ports["buttons"]),
        ("right_button", "Press the RIGHT motorised button normally (not backward)", ports["buttons"]),
        ("trigger", "Pull the RIGHT shoulder trigger", [ports["large"]]),
    ):
        candidates = [c for c in candidates if c not in [lever["port"] for lever in levers.values()]]
        print(f"  {question}...")
        async for kind, *values in next_lines(hub, reader):
            if kind == "A" and values[0] in candidates and abs(int(values[1])) >= 15:
                levers[name] = {"port": values[0], "sign": 1 if int(values[1]) > 0 else -1}
                print(f"    port {values[0]}, {'+' if int(values[1]) > 0 else '-'} direction")
                break
        print("  Now put it back to rest.")
        async for kind, *values in next_lines(hub, reader):
            if kind == "A" and values[0] == levers[name]["port"] and abs(int(values[1])) <= 8:
                break
    return levers


async def calibrate_tilt(hub: SpikeHub, reader: LineReader) -> dict:
    print("\nCalibrating the tilt. Hold the controller the way you play.")
    steps, latest = {}, None
    for step, question in (("level", "Hold it LEVEL"),
                           ("forward", "Tilt it FORWARD (away from you) as far as a normal gesture"),
                           ("right", "Tilt it to the RIGHT as far as a normal gesture")):
        print(f"  {question}, then press the RIGHT hub button.")
        reader.take()  # ignore presses from before the question
        async for kind, *values in next_lines(hub, reader):
            if kind == "T":
                latest = (int(values[0]), int(values[1]))
            elif kind == "B" and values == ["R", "1"] and latest is not None:
                break
        steps[step] = latest
        print(f"    pitch {latest[0]}°, roll {latest[1]}°")

    def change(step):
        return [steps[step][0] - steps["level"][0], steps[step][1] - steps["level"][1]]

    tilt = {"level": list(steps["level"]), "forward": change("forward"), "right": change("right")}
    (rp, rr), (fp, fr) = tilt["right"], tilt["forward"]
    right_size, forward_size = math.hypot(rp, rr), math.hypot(fp, fr)
    # |det| = sizes * sin(angle between the two tilts); they must be >= 45° apart.
    if min(right_size, forward_size) < 10 or abs(rp * fr - fp * rr) < right_size * forward_size * math.sin(math.pi / 4):
        raise RuntimeError("Couldn't tell forward and right apart. Run --calibrate again and tilt further.")
    print(f"    forward {forward_size:.0f}°, right {right_size:.0f}° "
          f"(gestures fire at {cfg.GYRO_THRESHOLD_DEG['forward']}° / {cfg.GYRO_THRESHOLD_DEG['right']}°).")
    return tilt


async def calibrate(hub: SpikeHub, reader: LineReader, ports: dict, slot: int) -> dict:
    await start_hub_program(hub, ports, None, slot)  # no lever logic: only raw angles
    calibration = {"ports": ports, "levers": await calibrate_levers(hub, reader, ports)}
    calibration["tilt"] = await calibrate_tilt(hub, reader)
    CALIBRATION.write_text(json.dumps(calibration, indent=2) + "\n")
    print(f"Saved to {CALIBRATION.name}.")
    await hub.stop_program(slot)
    return calibration


# ---- playing ---------------------------------------------------------------------------

class HubEvents:
    """Turns lines printed by hub_program.py into ControllerLogic calls."""

    def __init__(self, logic: ControllerLogic, calibration: dict):
        self.logic = logic
        self.tilt = calibration["tilt"]
        self.lever_of_port = {lever["port"]: name for name, lever in calibration["levers"].items()}
        self.sign = {lever["port"]: lever["sign"] for lever in calibration["levers"].values()}

    def handle(self, line: str, now: float) -> bool:
        """Returns False when the line is not from a healthy hub program (an error)."""
        m = self.logic
        kind, *values = line.split()
        if kind == "T":
            m.on_tilt(*tilt_degrees(self.tilt, int(values[0]), int(values[1])), now)
        elif kind == "F":
            m.on_force(values[0] == "1", now)
        elif kind == "B":
            m.on_hub_button("left" if values[0] == "L" else "right", values[1] == "1", now)
        elif kind == "M":
            m.on_lever(self.lever_of_port[values[0]], values[1], now)
        elif kind == "A":
            m.angles[self.lever_of_port[values[0]]] = self.sign[values[0]] * int(values[1])
        elif kind == "W":
            print("hub warning:", line[2:])
        elif kind == "READY":
            print("Hub program running.")
        elif kind != "H":
            print("hub:", line)  # errors from the hub program end up here
            return False
        return True


async def play(hub: SpikeHub, reader: LineReader, calibration: dict, chrome, dashboard: bool = False,
               recorder=None) -> None:
    def log(text: str) -> None:
        print(f"  {text}")
        if recorder:
            recorder.event(text)  # for the demo captions

    logic = ControllerLogic(log=log)
    events = HubEvents(logic, calibration)
    last_heard = time.monotonic()
    silent = False
    last_status = None
    print("Close Chrome or press Ctrl+C to quit. The centre hub button also stops it.")
    try:
        while hub.connected and not hub.program_stopped and not (chrome and chrome.closed):
            await reader.wait(0.01)  # wakes as soon as a line arrives
            now = time.monotonic()
            lines = reader.take()
            if lines:
                last_heard, silent = now, False
            for line in lines:
                if not events.handle(line, now):
                    logic.release_all()
            if now - last_heard > cfg.WATCHDOG_S and not silent:
                silent = True
                logic.release_all()
                print(f"  Nothing from the hub for {cfg.WATCHDOG_S} s: released everything.")
            logic.update(now)
            if chrome:
                await chrome.push(logic.pad.snapshot(now))
                if dashboard:
                    await chrome.push_status(logic.dashboard())
            status = (logic.mode, logic.moving, tuple(logic.pad.pressed(now)))
            if status != last_status:
                print(logic.status(now))
                last_status = status
    finally:
        logic.release_all()
        if chrome and not chrome.closed:
            await chrome.push(logic.pad.snapshot(time.monotonic()))
        if chrome and chrome.closed:
            print("Chrome closed.")
        elif hub.program_stopped:
            print("The hub program stopped.")
        elif not hub.connected:
            print("Lost the connection to the hub.")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--calibrate", action="store_true", help="find the levers and the tilt directions again")
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--tester", action="store_true",
                       help="open the dashboard (LEGO controller + virtual Xbox controller) instead of Remote Play")
    where.add_argument("--no-chrome", action="store_true", help="don't open Chrome, only print what happens")
    parser.add_argument("--record", action="store_true",
                        help="record the Remote Play game picture (or the dashboard with --tester) for make_demo.py")
    parser.add_argument("--slot", type=int, default=0, help="hub program slot 0-19 (default 0)")
    parser.add_argument("--name", help="only connect to the hub with this Bluetooth name")
    args = parser.parse_args()
    if args.record and args.no_chrome:
        parser.error("--record needs Chrome")
    check_config()

    reader = LineReader()
    async with SpikeHub(name=args.name, on_console=reader.feed) as hub:
        ports = await detect_ports(hub)
        try:
            calibration = None if args.calibrate or not CALIBRATION.exists() else json.loads(CALIBRATION.read_text())
            if calibration and calibration.get("ports") != ports:
                print("The sensor or motor ports changed since the calibration. Calibrating again.")
                calibration = None
            if calibration is None:
                calibration = await calibrate(hub, reader, ports, args.slot)
                print("\nPut the motorised buttons and the shoulder triggers at rest.")
                await asyncio.sleep(2)
            await start_hub_program(hub, ports, calibration, args.slot)

            async with contextlib.AsyncExitStack() as stack:
                chrome = None
                if not args.no_chrome:
                    url = TESTER_URL if args.tester else cfg.REMOTE_PLAY_URL
                    print(f"Opening {'the dashboard' if args.tester else 'Xbox Remote Play'} in Chrome...")
                    chrome = await stack.enter_async_context(ChromeGamepad(url, _profile_dir()))
                    if not args.tester:
                        print("Sign in if asked, pick your Xbox and press Remote play.\n"
                              "The SPIKE controller is the page's Xbox controller.")
                recorder = None
                if args.record:
                    from recorder import Recorder

                    recorder = Recorder(chrome.page, "#stage" if args.tester else None)
                    await recorder.start()
                    print("Recording" + (" the game picture once it shows up" if not args.tester else " the dashboard")
                          + "...")
                try:
                    await play(hub, reader, calibration, chrome, dashboard=args.tester, recorder=recorder)
                finally:
                    if recorder:
                        folder = await recorder.stop()
                        print(f"Recording saved in {folder.relative_to(HERE)} ({len(recorder.frames)} frames). "
                              f"Make the demo with: ../.venv/bin/python make_demo.py"
                              + (" --no-captions" if args.tester else ""))
        finally:
            if hub.connected and not hub.program_stopped:
                await hub.stop_program(args.slot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError as e:
        sys.exit(f"Error: {e}")
    except KeyboardInterrupt:
        print("\nBye!")

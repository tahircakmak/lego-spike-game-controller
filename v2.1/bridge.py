"""
LEGO SPIKE Prime V2.1 controller -> virtual Xbox controller -> Chrome -> Xbox Remote Play.

    python bridge.py --calibrate     # first time: find the buttons, the tilt and the distance sensor
    python bridge.py                 # play: Chrome opens on Xbox Remote Play
    python bridge.py --tester        # Chrome opens the dashboard: LEGO controller + virtual Xbox controller
    python bridge.py --no-chrome     # only print what the controller does

Before running: turn the hub on, press its Bluetooth button, close the SPIKE app, and
leave both motorised buttons and both back triggers at rest (that position is 0°).
Ports, tuning values and Xbox mappings are in config.py.
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
from controller import LEVERS, ControllerLogic, check_config
from gamepad import TESTER_URL, ChromeGamepad
from spike import SpikeHub
from spike import protocol as p

HERE = Path(__file__).parent
HUB_PROGRAM = HERE / "hub_program.py"
CALIBRATION = HERE / "calibration.json"
MOTOR_PORTS = [*cfg.BUTTON_PORTS, cfg.RIGHT_TRIGGER_PORT, cfg.LEFT_TRIGGER_PORT]


def _profile_dir() -> Path:
    """Chrome profile for Remote Play (shared with V1 and V2), outside the project: it holds your sign-in."""
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

def describe(reading) -> str:
    if isinstance(reading, p.Motor):
        return f"{reading.type} motor"
    return {p.ForceSensor: "force sensor", p.DistanceSensor: "distance sensor",
            p.ColorSensor: "color sensor"}.get(type(reading), type(reading).__name__)


def check_ports(found: dict) -> None:
    """found: port -> sensor reading. Raises with a clear message when a cable is in the wrong port."""
    expected = {port: ("small or medium motor (motorised button)", lambda r: isinstance(r, p.Motor)
                       and r.type in ("small", "medium")) for port in cfg.BUTTON_PORTS}
    for port, what in ((cfg.RIGHT_TRIGGER_PORT, "large motor (right trigger)"),
                       (cfg.LEFT_TRIGGER_PORT, "large motor (left trigger)")):
        expected[port] = (what, lambda r: isinstance(r, p.Motor) and r.type == "large")
    expected[cfg.FORCE_PORT] = ("force sensor", lambda r: isinstance(r, p.ForceSensor))
    expected[cfg.DISTANCE_PORT] = ("distance sensor", lambda r: isinstance(r, p.DistanceSensor))
    problems = []
    for port, (what, ok) in sorted(expected.items()):
        reading = found.get(port)
        if reading is None or not ok(reading):
            problems.append(f"port {port} should have a {what}, "
                            f"found {describe(reading) if reading is not None else 'nothing'}")
    if problems:
        raise RuntimeError("Check the cables (or the ports in config.py):\n  " + "\n  ".join(problems))


async def detect_ports(hub: SpikeHub) -> None:
    """Use the hub's sensor stream to check that every part is on the port config.py says."""
    found: dict[str, object] = {}
    seen = asyncio.Event()

    def on_sensors(readings):
        for r in readings:
            if hasattr(r, "port"):
                found[r.port] = r
        seen.set()

    await hub.start_sensor_stream(on_sensors, interval_ms=50)
    await asyncio.wait_for(seen.wait(), timeout=3)
    await asyncio.sleep(0.2)
    await hub.stop_sensor_stream()
    check_ports(found)
    print(f"Ports OK: buttons on {' and '.join(cfg.BUTTON_PORTS)}, right trigger on {cfg.RIGHT_TRIGGER_PORT}, "
          f"left trigger on {cfg.LEFT_TRIGGER_PORT}, force sensor on {cfg.FORCE_PORT}, "
          f"distance sensor on {cfg.DISTANCE_PORT}.")


def ports_now() -> dict:
    """The ports from config.py, as saved in the calibration (a change means calibrating again)."""
    return {"buttons": sorted(cfg.BUTTON_PORTS), "right_trigger": cfg.RIGHT_TRIGGER_PORT,
            "left_trigger": cfg.LEFT_TRIGGER_PORT, "force": cfg.FORCE_PORT, "distance": cfg.DISTANCE_PORT}


def hub_source(calibration: dict | None) -> str:
    """hub_program.py with its CONFIG block filled in from config.py (and the calibration)."""
    levers = []
    if calibration:
        for name in LEVERS:
            lever = calibration["levers"][name]
            if "trigger" in name:
                thresholds = (cfg.TRIGGER_PULL_DEG, cfg.TRIGGER_RELEASE_DEG)
            else:
                thresholds = (cfg.BUTTON_PRESS_DEG, cfg.BUTTON_RELEASE_DEG)
            spring = cfg.BUTTON_SPRING and "button" in name
            levers.append((name, lever["port"], lever["sign"], *thresholds, cfg.AUTO_RETURN, spring))
    values = {
        "FORCE_PORT": cfg.FORCE_PORT,
        "DISTANCE_PORT": cfg.DISTANCE_PORT,
        "MOTOR_PORTS": MOTOR_PORTS,
        "LEVERS": levers,
        "REARM_DEG": cfg.REARM_DEG,
        "RETURN_SPEED": cfg.RETURN_SPEED,
        "RETURN_TIMEOUT_MS": cfg.RETURN_TIMEOUT_MS,
        "FORCE_PRESS": cfg.FORCE_PRESS,
        "FORCE_RELEASE": cfg.FORCE_RELEASE,
        "GYRO_REPORT_MS": cfg.GYRO_REPORT_MS,
        "DISTANCE_REPORT_MS": cfg.DISTANCE_REPORT_MS,
        "DISTANCE_REPORT_STEP_MM": cfg.DISTANCE_REPORT_STEP_MM,
        "HUB_LOOP_MS": cfg.HUB_LOOP_MS,
        "HEARTBEAT_MS": cfg.HEARTBEAT_MS,
        "SPRING_STRENGTH": cfg.SPRING_STRENGTH,
        "SPRING_MAX": cfg.SPRING_MAX,
        "SPRING_DEADBAND_DEG": cfg.SPRING_DEADBAND_DEG,
        "SPRING_LIMIT_DEG": cfg.SPRING_LIMIT_DEG,
    }
    block = "\n".join(f"{key} = {value!r}" for key, value in values.items())
    source = HUB_PROGRAM.read_text()
    start, end = source.index("# <config>"), source.index("# </config>")
    head = source[start:].split("\n", 1)[0]
    return source[:start] + head + "\n" + block + "\n" + source[end:]


async def start_hub_program(hub: SpikeHub, calibration: dict | None, slot: int) -> None:
    print(f"Uploading hub_program.py to slot {slot}...")
    await hub.run_program(hub_source(calibration), slot=slot)


# ---- calibration -----------------------------------------------------------------------

def tilt_right(tilt: dict, pitch: int, roll: int) -> float:
    """
    How far the controller is tilted to the right, in degrees (negative = left).

    The reading's change from level is projected onto the calibrated right tilt (which
    holds both pitch and roll), so it works however the hub sits in the build.
    """
    rp, rr = tilt["right"]
    dp, dr = pitch - tilt["level"][0], roll - tilt["level"][1]
    return (dp * rp + dr * rr) / math.hypot(rp, rr)


def distance_settings(hand: int, away: int) -> dict:
    """Artifact settings from two readings: the hand over the sensor, and the hand gone."""
    if hand >= 0:
        near_mm = hand + max(20, hand // 2)
        if 0 <= away <= near_mm + cfg.ARTIFACT_FAR_EXTRA_MM:
            raise RuntimeError(f"The distance sensor read {hand} mm with your hand and {away} mm without it: too "
                               "close to tell apart. Point it at open space and run --calibrate again.")
        return {"near_mm": near_mm, "covered": False}
    if away < 0:
        raise RuntimeError("The distance sensor saw nothing, with or without your hand. "
                           "Hold your hand a little higher over it and run --calibrate again.")
    # Right on the sensor it reads nothing (-1), and something further away without the hand:
    # "nothing" is the hand. Readings just above the sensor's 40 mm minimum count as well.
    return {"near_mm": 50, "covered": True}


async def next_lines(hub: SpikeHub, reader: LineReader):
    """Yield the hub's lines as they arrive (for the calibration steps)."""
    while True:
        if not hub.connected:
            raise RuntimeError("Hub disconnected during calibration")
        for line in reader.take():
            yield line.split()
        await reader.wait(0.05)


async def calibrate_levers(hub: SpikeHub, reader: LineReader) -> dict:
    print("\nFinding the motorised buttons and the back triggers.")
    levers = {}
    for name, question, candidates in (
        ("right_button", "Press the RIGHT motorised button (melee)", cfg.BUTTON_PORTS),
        ("left_button", "Press the LEFT motorised button (jump)", cfg.BUTTON_PORTS),
        ("right_trigger", "Pull the RIGHT back trigger", [cfg.RIGHT_TRIGGER_PORT]),
        ("left_trigger", "Pull the LEFT back trigger", [cfg.LEFT_TRIGGER_PORT]),
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


async def wait_for_right_hub_button(hub: SpikeHub, reader: LineReader, latest: dict) -> None:
    """Keep the newest T and D readings in latest until the right hub button is pressed."""
    reader.take()  # ignore presses from before the question
    async for kind, *values in next_lines(hub, reader):
        if kind == "T":
            latest["T"] = (int(values[0]), int(values[1]))
        elif kind == "D":
            latest["D"] = int(values[0])
        elif kind == "B" and values == ["R", "1"] and "T" in latest and "D" in latest:
            return


async def calibrate_tilt(hub: SpikeHub, reader: LineReader, latest: dict) -> dict:
    print("\nCalibrating the steering tilt. Hold the controller the way you play.")
    steps = {}
    for step, question in (("level", "Hold it LEVEL"),
                           ("right", "Tilt it to the RIGHT, like turning a steering wheel")):
        print(f"  {question}, then press the RIGHT hub button.")
        await wait_for_right_hub_button(hub, reader, latest)
        steps[step] = latest["T"]
        print(f"    pitch {steps[step][0]}°, roll {steps[step][1]}°")
    right = [steps["right"][0] - steps["level"][0], steps["right"][1] - steps["level"][1]]
    size = math.hypot(*right)
    if size < 10:
        raise RuntimeError("That tilt was too small to measure. Run --calibrate again and tilt further.")
    print(f"    right tilt {size:.0f}° (full steering at {cfg.STEER_FULL_DEG}°).")
    return {"level": list(steps["level"]), "right": right}


async def calibrate_distance(hub: SpikeHub, reader: LineReader, latest: dict) -> dict:
    print("\nCalibrating the distance sensor (artifacts).")
    readings = {}
    for step, question in (("hand", "Hold your hand over the distance sensor, as close as you will in the game"),
                           ("away", "Take your hand away")):
        print(f"  {question}, then press the RIGHT hub button.")
        await wait_for_right_hub_button(hub, reader, latest)
        readings[step] = latest["D"]
        print(f"    {readings[step]} mm" if readings[step] >= 0 else "    sees nothing")
    settings = distance_settings(readings["hand"], readings["away"])
    print("    artifacts when the sensor sees nothing (your hand covers it) or less than 50 mm."
          if settings["covered"] else f"    artifacts when your hand is closer than {settings['near_mm']} mm.")
    return settings


async def calibrate(hub: SpikeHub, reader: LineReader, slot: int) -> dict:
    await start_hub_program(hub, None, slot)  # no lever logic: only raw angles
    latest: dict = {}
    calibration = {"ports": ports_now(), "levers": await calibrate_levers(hub, reader)}
    calibration["tilt"] = await calibrate_tilt(hub, reader, latest)
    calibration["distance"] = await calibrate_distance(hub, reader, latest)
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
            m.on_tilt(tilt_right(self.tilt, int(values[0]), int(values[1])), now)
        elif kind == "F":
            m.on_force(values[0] == "1", now)
        elif kind == "B":
            m.on_hub_button("left" if values[0] == "L" else "right", values[1] == "1", now)
        elif kind == "D":
            m.on_distance(int(values[0]), now)
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


async def play(hub: SpikeHub, reader: LineReader, calibration: dict, chrome, dashboard: bool = False) -> None:
    logic = ControllerLogic(calibration["distance"], log=lambda text: print(f"  {text}"))
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
                print(f"  Nothing from the hub for {cfg.WATCHDOG_S} s: released everything and stopped moving.")
            logic.update(now)
            if chrome:
                await chrome.push(logic.pad.snapshot(now))
                if dashboard:
                    await chrome.push_status(logic.dashboard())
            status = (logic.moving, tuple(logic.pad.pressed(now)))
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
    parser.add_argument("--calibrate", action="store_true",
                        help="find the buttons, the tilt and the distance sensor again")
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--tester", action="store_true",
                       help="open the dashboard (LEGO controller + virtual Xbox controller) instead of Remote Play")
    where.add_argument("--no-chrome", action="store_true", help="don't open Chrome, only print what happens")
    parser.add_argument("--slot", type=int, default=0, help="hub program slot 0-19 (default 0)")
    parser.add_argument("--name", help="only connect to the hub with this Bluetooth name")
    args = parser.parse_args()
    check_config()

    reader = LineReader()
    async with SpikeHub(name=args.name, on_console=reader.feed) as hub:
        await detect_ports(hub)
        try:
            calibration = None if args.calibrate or not CALIBRATION.exists() else json.loads(CALIBRATION.read_text())
            if calibration and calibration.get("ports") != ports_now():
                print("The ports in config.py changed since the calibration. Calibrating again.")
                calibration = None
            if calibration is None:
                calibration = await calibrate(hub, reader, args.slot)
                print("\nPut the motorised buttons and the back triggers at rest.")
                await asyncio.sleep(2)
            await start_hub_program(hub, calibration, args.slot)

            async with contextlib.AsyncExitStack() as stack:
                chrome = None
                if not args.no_chrome:
                    url = TESTER_URL if args.tester else cfg.REMOTE_PLAY_URL
                    print(f"Opening {'the dashboard' if args.tester else 'Xbox Remote Play'} in Chrome...")
                    chrome = await stack.enter_async_context(ChromeGamepad(url, _profile_dir()))
                    if not args.tester:
                        print("Sign in if asked, pick your Xbox and press Remote play.\n"
                              "The SPIKE controller is the page's Xbox controller.")
                await play(hub, reader, calibration, chrome, dashboard=args.tester)
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

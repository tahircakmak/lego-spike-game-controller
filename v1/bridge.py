"""
Use the real SPIKE controller over Bluetooth.

Uploads hub_program.py to the hub, starts it, and turns what it prints into
game actions. The simulator window shows everything live.

    python bridge.py --calibrate     # first time: teach it the tilt directions and dial positions
    python bridge.py                 # watch only, no keys are sent
    python bridge.py --keys          # also press real keys / mouse for the game
    python bridge.py --xbox          # play on your Xbox through Remote Play in Chrome

Before running: turn the hub on, press its Bluetooth button, close the SPIKE
app, hold the controller level and leave the trigger at its rest angle.
"""

import argparse
import asyncio
import contextlib
import json
import math
import re
import sys
import tkinter as tk
from pathlib import Path

import controller_simulator as sim
from spike import SpikeHub
from spike import protocol as p

HERE = Path(__file__).parent
HUB_PROGRAM = HERE / "hub_program.py"
CALIBRATION = HERE / "calibration.json"


class LineReader:
    """Collects the hub's print() output and splits it into lines."""

    def __init__(self):
        self._buffer = ""
        self.lines: list[str] = []

    def feed(self, text: str) -> None:
        self._buffer += text
        *done, self._buffer = self._buffer.split("\n")
        self.lines += [line.strip() for line in done if line.strip()]

    def take(self) -> list[str]:
        lines, self.lines = self.lines, []
        return lines


class HubEvents:
    """Turns lines printed by hub_program.py into ControllerLogic calls."""

    def __init__(self, model: sim.ControllerLogic, calibration: dict):
        self.model = model
        self.dial_angles = calibration["dial"]
        # {"level": [pitch, roll], "forward": [pitch, roll], "right": [pitch, roll]}
        # forward / right are how much pitch and roll changed for a full-speed tilt that way.
        self.tilt = calibration["tilt"]

    def handle(self, line: str) -> None:
        m = self.model
        kind, *values = line.split()
        if kind == "T":
            self._tilt(int(values[0]), int(values[1]))
        elif kind == "F":
            m.on_force(values[0] == "1")
        elif kind == "P":
            m.on_potion()
        elif kind == "B":
            m.on_button("left" if values[0] == "L" else "right")
        elif kind == "D":
            m.on_dial(nearest_dial(int(values[0]), self.dial_angles))
        elif kind == "A":
            m.trigger_angle = int(values[0])
        elif kind == "TP":
            m.trigger_pull()
        elif kind == "TR":
            m.trigger_release()
        elif kind == "TZ":
            m.trigger_returning = False
        elif kind == "READY":
            print("Hub program running.")
        else:
            print("hub:", line)  # errors from the hub program end up here

    def _tilt(self, pitch: int, roll: int) -> None:
        right, forward = tilt_amounts(self.tilt, pitch, roll)
        self.model.on_tilt(right * sim.TILT_MAX, forward * sim.TILT_MAX)


def tilt_amounts(tilt: dict, pitch: int, roll: int) -> tuple[float, float]:
    """
    How much of a full-speed right / forward tilt the reading is (1.0 = full speed).

    Solves  reading - level = right * R + forward * F  for right and forward, where R and F
    are the calibrated tilts. Because R and F each hold both pitch and roll, a hub mounted
    at a slight angle doesn't turn a pure right tilt into a diagonal.
    """
    (rp, rr), (fp, fr) = tilt["right"], tilt["forward"]
    p, r = pitch - tilt["level"][0], roll - tilt["level"][1]
    det = rp * fr - fp * rr
    return (p * fr - fp * r) / det, (rp * r - p * rr) / det


def nearest_dial(angle: int, dial_angles: list[int]) -> int:
    return min(range(len(dial_angles)), key=lambda i: abs((dial_angles[i] - angle + 180) % 360 - 180))


class KeyboardMouseOutput:
    """Presses real keys and mouse buttons with pynput."""

    def __init__(self):
        from pynput.keyboard import Controller as Keyboard, Key
        from pynput.mouse import Button, Controller as Mouse

        if sys.platform == "darwin":
            from ApplicationServices import AXIsProcessTrusted

            # Without Accessibility permission macOS silently drops every key press.
            if not AXIsProcessTrusted():
                print("WARNING: this terminal has no Accessibility permission, so key presses won't arrive.\n"
                      "  Allow it in System Settings > Privacy & Security > Accessibility, then restart it.")
        self.keyboard, self.mouse = Keyboard(), Mouse()
        self.special = {"space": Key.space, "esc": Key.esc}
        self.buttons = {"mouse_left": Button.left, "mouse_right": Button.right}
        self.held: set[str] = set()
        self.moving: set[str] = set()
        self._mouse_rest = [0.0, 0.0]

    def hold(self, key):
        if key in self.buttons:
            self.mouse.press(self.buttons[key])
        else:
            self.keyboard.press(self.special.get(key, key))
        self.held.add(key)

    def release(self, key):
        if key in self.buttons:
            self.mouse.release(self.buttons[key])
        else:
            self.keyboard.release(self.special.get(key, key))
        self.held.discard(key)

    def tap(self, key):
        self.hold(key)
        self.release(key)

    def set_move(self, keys):
        for key in self.moving - keys:
            self.release(key)
        for key in keys - self.moving:
            self.hold(key)
        self.moving = set(keys)

    def stick(self, x, y):
        pass  # the keyboard moves with W A S D (set_move)

    def mouse_move(self, dx, dy):
        # Keep the fractions so slow tilts still add up to whole pixels.
        self._mouse_rest[0] += dx
        self._mouse_rest[1] += dy
        whole = [int(v) for v in self._mouse_rest]
        if any(whole):
            self.mouse.move(*whole)
            self._mouse_rest = [v - w for v, w in zip(self._mouse_rest, whole)]

    def release_all(self):
        for key in list(self.held):
            self.release(key)
        self.moving = set()


async def check_ports(hub: SpikeHub) -> str:
    """Look at the sensor stream to find the force sensor and check both motors."""
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

    problems = [f"no motor on port {port} ({job})"
                for port, job in (("A", "trigger"), ("F", "artifact dial"))
                if not isinstance(found.get(port), p.Motor)]
    force_ports = [port for port, r in found.items() if isinstance(r, p.ForceSensor)]
    if not force_ports:
        problems.append("no force sensor found")
    if problems:
        raise RuntimeError("Check the cables: " + ", ".join(problems))
    print(f"Found trigger motor on A, dial motor on F, force sensor on {force_ports[0]}.")
    return force_ports[0]


async def start_hub_program(hub: SpikeHub, slot: int) -> None:
    force_port = await check_ports(hub)
    source = HUB_PROGRAM.read_text()
    source = re.sub(r"^FORCE_PORT = port\.\w", f"FORCE_PORT = port.{force_port}", source, flags=re.M)
    print(f"Uploading hub_program.py to slot {slot}...")
    await hub.run_program(source, slot=slot)


async def wait_for_right_button(hub: SpikeHub, reader: LineReader, latest: dict) -> None:
    """Keep the latest tilt and dial readings in `latest` until the right hub button is pressed."""
    reader.take()  # ignore presses from before the question
    while True:
        for line in reader.take():
            kind, *values = line.split()
            if kind == "T":
                latest["tilt"] = (int(values[0]), int(values[1]))
            elif kind == "D":
                latest["dial"] = int(values[0])
            elif kind == "B" and values == ["R"] and "tilt" in latest and "dial" in latest:
                return
        if not hub.connected:
            raise RuntimeError("Hub disconnected during calibration")
        await asyncio.sleep(0.02)


async def calibrate_tilt(hub: SpikeHub, reader: LineReader, latest: dict) -> dict:
    print("\nCalibrating the tilt. Hold the controller the way you play.")
    steps = {}
    for step, question in (("level", "Hold it LEVEL"),
                           ("forward", "Tilt it FORWARD (away from you) as far as you want for full speed"),
                           ("right", "Tilt it to the RIGHT as far as you want for full speed")):
        print(f"  {question}, then press the RIGHT hub button.")
        await wait_for_right_button(hub, reader, latest)
        steps[step] = latest["tilt"]
        print(f"    pitch {steps[step][0]}°, roll {steps[step][1]}°")

    def change(step):
        return [steps[step][0] - steps["level"][0], steps[step][1] - steps["level"][1]]

    tilt = {"level": list(steps["level"]), "forward": change("forward"), "right": change("right")}
    (rp, rr), (fp, fr) = tilt["right"], tilt["forward"]
    right_size, forward_size = math.hypot(rp, rr), math.hypot(fp, fr)
    # |det| = sizes * sin(angle between the two tilts); they must be >= 45° apart.
    if min(right_size, forward_size) < 5 or abs(rp * fr - fp * rr) < right_size * forward_size * math.sin(math.pi / 4):
        raise RuntimeError("Couldn't tell forward and right apart. Run --calibrate tilt again and tilt further.")
    print(f"    full speed at {forward_size:.0f}° forward and {right_size:.0f}° right.")
    return tilt


async def calibrate_dial(hub: SpikeHub, reader: LineReader, latest: dict) -> list[int]:
    print("\nCalibrating the artifact dial.")
    angles = []
    for name, *_ in sim.DIAL:
        print(f"  Turn the dial to {name.upper()}, then press the RIGHT hub button.")
        await wait_for_right_button(hub, reader, latest)
        angles.append(latest["dial"])
        print(f"    {name}: {latest['dial']}°")
    return angles


async def calibrate(hub: SpikeHub, reader: LineReader, parts: list[str]) -> None:
    calibration = json.loads(CALIBRATION.read_text()) if CALIBRATION.exists() else {}
    latest = {}
    if "tilt" in parts:
        calibration["tilt"] = await calibrate_tilt(hub, reader, latest)
    if "dial" in parts:
        calibration["dial"] = await calibrate_dial(hub, reader, latest)
    CALIBRATION.write_text(json.dumps(calibration, indent=2) + "\n")
    print(f"Saved to {CALIBRATION.name}.")


async def run_window(hub: SpikeHub, reader: LineReader, calibration: dict, args) -> None:
    async with contextlib.AsyncExitStack() as stack:
        remote = None
        if args.xbox:
            from xbox_remote_play import GamepadOutput, RemotePlay

            print("Opening Xbox Remote Play in Chrome...")
            remote = await stack.enter_async_context(RemotePlay())
            output = GamepadOutput()
        else:
            output = KeyboardMouseOutput() if args.keys else None
        await _run_window(hub, reader, calibration, args, output, remote)


async def _run_window(hub, reader, calibration, args, output, remote) -> None:
    root = tk.Tk()
    app = sim.App(root, output=output, hardware=True)
    events = HubEvents(app.model, calibration)
    closed = False

    def on_close():
        nonlocal closed
        closed = True

    root.protocol("WM_DELETE_WINDOW", on_close)

    recorder = None
    toggle_recording = False
    if remote:
        from recorder import Recorder

        recorder = Recorder(remote.page)
        show_in_window = app.model.log

        def log(source, action, binding=None):
            show_in_window(source, action, binding)
            recorder.event(source, action)  # for the demo captions

        app.model.log = log

        def on_r(_event):
            nonlocal toggle_recording
            toggle_recording = True

        root.bind("<KeyPress-r>", on_r)
        title = root.title()
    if remote:
        print("Sign in if asked, pick your Xbox and press Remote play.\n"
              "The SPIKE controller shows up there as an Xbox controller.\n"
              "To record a demo, press R in the controller window (again to stop).")
    elif args.keys:
        print("Sending keys to the game.")
    else:
        print("Watch-only mode: nothing is sent to the game (add --keys or --xbox).")
    print("Close the window or press Ctrl+C to quit.")
    try:
        while hub.connected and not closed and not (remote and remote.closed):
            for line in reader.take():
                events.handle(line)
            root.update()
            if remote:
                await remote.push(output.state())
            if toggle_recording:
                toggle_recording = False
                if recorder.recording:
                    folder = await recorder.stop()
                    root.title(title)
                    print(f"Recording saved in {folder.relative_to(HERE)} "
                          f"({len(recorder.frames)} frames). Make the demo with: python make_demo.py")
                else:
                    await recorder.start()
                    root.title(title + "   ● REC")
                    print("Recording the game picture... press R again to stop.")
            await asyncio.sleep(0.01)
    finally:
        if recorder and recorder.recording:
            folder = await recorder.stop()
            print(f"Recording saved in {folder.relative_to(HERE)}.")
        if output:
            output.release_all()
            if remote:
                await remote.push(output.state())
        root.destroy()
        if remote and remote.closed:
            print("Chrome closed.")
        else:
            print("Window closed." if closed else "Lost the connection to the hub.")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--calibrate", nargs="?", const="all", choices=["all", "tilt", "dial"],
                        help="record the tilt directions and/or the dial positions")
    outputs = parser.add_mutually_exclusive_group()
    outputs.add_argument("--keys", action="store_true", help="press real keys and mouse buttons for the game")
    outputs.add_argument("--xbox", action="store_true",
                         help="play on your Xbox via Remote Play in Chrome, as a virtual Xbox controller")
    parser.add_argument("--slot", type=int, default=0, help="program slot 0-19 (default 0)")
    parser.add_argument("--name", help="only connect to the hub with this Bluetooth name")
    args = parser.parse_args()

    if args.calibrate:
        parts = ["tilt", "dial"] if args.calibrate == "all" else [args.calibrate]
    else:
        saved = json.loads(CALIBRATION.read_text()) if CALIBRATION.exists() else {}
        parts = [part for part in ("tilt", "dial") if part not in saved]
        if "tilt" in saved and not isinstance(saved["tilt"].get("right", [None])[0], (int, float)):
            parts.insert(0, "tilt")  # saved by an older version in a different format
        if parts:
            print(f"Not calibrated yet: {' and '.join(parts)}. Calibrating first.")

    reader = LineReader()
    async with SpikeHub(name=args.name, on_console=reader.feed) as hub:
        await start_hub_program(hub, args.slot)
        try:
            if parts:
                await calibrate(hub, reader, parts)
                if args.calibrate:
                    return
            await run_window(hub, reader, json.loads(CALIBRATION.read_text()), args)
        finally:
            if hub.connected:
                await hub.stop_program(args.slot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError as e:
        sys.exit(f"Error: {e}")
    except KeyboardInterrupt:
        print("\nBye!")

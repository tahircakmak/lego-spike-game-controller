"""
Custom software V1: one program for every LEGO SPIKE controller design.

It finds what is plugged into the hub, calibrates it, and turns it into a virtual Xbox
controller in Chrome (Xbox Remote Play), following a loadout you make in the control panel.

    python app.py                    # connect, open Xbox Remote Play + the control panel
    python app.py --panel-only       # Chrome opens only the control panel (try a loadout first)
    python app.py --no-chrome        # no Chrome: open the printed address in any browser
    python app.py --no-hub           # no hub: edit loadouts and test them with sliders
    python app.py --calibrate        # run the calibration wizard again
    python app.py --loadout example-everything   # start with this loadout

Before running: turn the hub on, press its Bluetooth button and close the SPIKE app.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import sys
import time
from collections import deque
from pathlib import Path

from calibration import Wizard
from chrome import REMOTE_PLAY_URL, ChromeGamepad
from devices import (DEMO_PORTS, Devices, complete_calibration, detect_ports, hub_source, needs_calibration,
                     ports_summary)
from engine import (ACTION_TYPES, AXIS_OUTPUTS, BUTTONS, DIRECTION_OUTPUTS, REMAP_NAMES, ZONE_MODES, Engine,
                    empty_loadout, normalize_loadout)
from panel_server import PanelServer

HERE = Path(__file__).parent
LOADOUTS = HERE / "loadouts"
CALIBRATION = HERE / "calibration.json"
SETTINGS = HERE / "settings.json"
WATCHDOG_S = 2.0  # nothing from the hub for this long -> release everything
HUB_FIELDS = ("mode", "zero", "spring", "spring_strength", "spring_max")  # the hub program needs these


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "loadout"


def read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


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


class App:
    def __init__(self, args):
        self.args = args
        self.notes: deque[str] = deque(maxlen=6)  # messages for the control panel
        calibration = read_json(CALIBRATION)
        ports = DEMO_PORTS if args.no_hub else {}
        if args.no_hub and calibration and calibration.get("ports"):
            ports = calibration["ports"]
        self.devices = Devices(ports, calibration)
        self.calibration_saved = calibration is not None
        self.engine = Engine(log=lambda text: print(f"  {text}"))
        self.file: str | None = None
        self.dirty = False
        self.open_first_loadout()
        self.hub = None
        self.reader: LineReader | None = None
        self.hub_status = "simulation" if args.no_hub else "not connected"
        self.restarting = False
        self.wizard: Wizard | None = None
        self.lock = asyncio.Lock()
        self.last_heard = time.monotonic()
        self.silent = False

    def note(self, text: str) -> None:
        print(text)
        self.notes.append(text)

    # ---- loadouts --------------------------------------------------------------

    def open_first_loadout(self) -> None:
        wanted = self.args.loadout or (read_json(SETTINGS) or {}).get("loadout")
        if wanted:
            wanted = wanted if wanted.endswith(".json") else wanted + ".json"
            if (LOADOUTS / wanted).exists():
                self.open_loadout(wanted)
                return
            print(f"No loadout {wanted} in loadouts/, starting with an empty one.")
        self.engine.load(empty_loadout("My loadout"))
        self.file, self.dirty = None, True
        self.engine.ranges = self.devices.ranges()

    def open_loadout(self, file: str) -> None:
        path = LOADOUTS / Path(file).name
        data = read_json(path)
        if data is None:
            raise ValueError(f"can't read {path.name}")
        self.engine.load(data)
        self.engine.ranges = self.devices.ranges()
        self.file, self.dirty = path.name, False
        SETTINGS.write_text(json.dumps({"loadout": path.name}, indent=2) + "\n")
        self.note(f"Loadout: {self.engine.loadout['name']} ({path.name})")

    def save_loadout(self, name: str | None = None, as_new: bool = False) -> None:
        loadout = self.engine.copy_loadout()
        if name:
            loadout["name"] = name
            self.engine.loadout["name"] = name
        if as_new or self.file is None:
            file = slug(loadout["name"]) + ".json"
        else:
            file = self.file
        loadout["devices"] = {port: info for port, info in self.devices.ports.items()}  # for reference only
        LOADOUTS.mkdir(exist_ok=True)
        (LOADOUTS / file).write_text(json.dumps(loadout, indent=2) + "\n")
        self.file, self.dirty = file, False
        SETTINGS.write_text(json.dumps({"loadout": file}, indent=2) + "\n")
        self.note(f"Saved {loadout['name']} to loadouts/{file}")

    def list_loadouts(self) -> list[dict]:
        out = []
        for path in sorted(LOADOUTS.glob("*.json")):
            data = read_json(path) or {}
            out.append({"file": path.name, "name": data.get("name", path.stem),
                        "description": data.get("description", ""), "bindings": len(data.get("bindings", []))})
        return out

    # ---- calibration -----------------------------------------------------------

    def save_calibration(self, calibration: dict) -> bool:
        """Use and save a calibration. Returns True when the hub program has to be sent again."""
        old = self.devices.calibration["motors"]
        self.devices.set_calibration(calibration)
        self.engine.ranges = self.devices.ranges()
        CALIBRATION.write_text(json.dumps(self.devices.calibration, indent=2) + "\n")
        self.calibration_saved = True
        new = self.devices.calibration["motors"]
        return any(old.get(p, {}).get(k) != new[p].get(k) for p in new for k in HUB_FIELDS)

    def start_wizard(self) -> None:
        if self.hub_status != "connected":
            raise RuntimeError("Connect to the hub first: calibration needs live readings.")
        self.engine.release_all()
        self.wizard = Wizard(self.devices.ports, self.devices.calibration, self.devices.raw)
        self.note("Calibration started. Capture each step with the panel's button or the RIGHT hub button.")
        print(f"  Step 1: {self.wizard.step['text']}")

    async def wizard_step(self, skip: bool = False) -> None:
        w = self.wizard
        if not w or w.done:
            return
        if skip:
            w.skip()
        else:
            print(f"    {w.capture()}")
        if w.done:
            needs_upload = self.save_calibration(w.cal)
            self.note("Calibration saved to calibration.json.")
            if needs_upload:
                await self.restart_program()
        else:
            print(f"  Step {w.index + 1}: {w.step['text']}")

    # ---- the hub ---------------------------------------------------------------

    async def connect(self) -> None:
        async with self.lock:
            if self.args.no_hub:
                raise RuntimeError("Started with --no-hub.")
            if self.hub and self.hub.connected:
                await self.hub.disconnect()
            self.hub_status = "connecting"
            self.restarting = True
            self.reader = LineReader()
            self.hub = SpikeHubClass(name=self.args.name, on_console=self.reader.feed)
            try:
                await self.hub.connect()
            except Exception as e:
                self.hub_status = "not connected"
                self.note(f"Could not connect: {e}")
                raise RuntimeError(str(e)) from None
            finally:
                self.restarting = False
            await self._upload(rescan=True)
        forced, self.args.calibrate = self.args.calibrate, False
        if forced or needs_calibration(self.devices.calibration if self.calibration_saved else None,
                                       self.devices.ports):
            if not forced:
                self.note("These devices haven't been calibrated yet.")
            self.start_wizard()

    async def restart_program(self, rescan: bool = False) -> None:
        async with self.lock:
            if not self.hub or not self.hub.connected:
                raise RuntimeError("The hub is not connected.")
            await self._upload(rescan)

    async def _upload(self, rescan: bool) -> None:
        self.restarting = True
        self.engine.release_all()
        try:
            with contextlib.suppress(asyncio.TimeoutError):
                await self.hub.stop_program(self.args.slot)
            if rescan:
                ports = await detect_ports(self.hub)
                self.devices.set_ports(ports)
                self.engine.ranges = self.devices.ranges()
                self.note(f"Found {ports_summary(ports)}.")
                for port, info in ports.items():
                    if info["kind"] == "unsupported":
                        self.note(f"Port {port}: {info['type']} is not supported, it will be ignored.")
            print(f"Uploading the hub program to slot {self.args.slot}...")
            await self.hub.run_program(hub_source(self.devices.ports, self.devices.calibration), slot=self.args.slot)
            self.hub_status = "connected"
            self.last_heard, self.silent = time.monotonic(), False
        except Exception as e:
            self.hub_status = "program stopped" if self.hub.connected else "disconnected"
            message = str(e) or type(e).__name__
            self.note(f"Hub error: {message}")
            raise RuntimeError(message) from None  # the panel stays open and shows it
        finally:
            self.restarting = False

    # ---- the main loop ---------------------------------------------------------

    def handle_line(self, line: str) -> None:
        kind = self.devices.handle_line(line)
        if kind is None:
            print("hub:", line)  # errors from the hub program end up here
            self.engine.release_all()
        elif kind == "W":
            self.note("Hub warning: " + line[2:])
        elif kind == "READY":
            print("Hub program running.")
        elif kind == "B" and line == "B R 1" and self.wizard and not self.wizard.done:
            asyncio.ensure_future(self.wizard_step())

    async def run(self, chrome) -> None:
        last_pressed = None
        print("Close Chrome or press Ctrl+C to quit.")
        while not (chrome and chrome.closed):
            if self.reader:
                await self.reader.wait(0.01)  # wakes as soon as a line arrives
            else:
                await asyncio.sleep(0.01)
            now = time.monotonic()
            if self.reader:
                lines = self.reader.take()
                if lines:
                    self.last_heard, self.silent = now, False
                for line in lines:
                    self.handle_line(line)
            if self.hub and not self.restarting:
                if not self.hub.connected and self.hub_status in ("connected", "program stopped"):
                    self.hub_status = "disconnected"
                    self.note("Lost the connection to the hub. Press Connect in the control panel to try again.")
                elif self.hub.connected and self.hub.program_stopped and self.hub_status == "connected":
                    self.hub_status = "program stopped"
                    self.note("The hub program stopped (centre button?). Press Restart in the control panel.")
                elif self.hub_status == "connected" and now - self.last_heard > WATCHDOG_S and not self.silent:
                    self.silent = True
                    self.note(f"Nothing from the hub for {WATCHDOG_S:.0f} s: released everything.")
            if self.wizard:
                self.wizard.feed()
                if self.wizard.done:
                    self.wizard = None
            for source, value in self.devices.values().items():
                self.engine.set_source(source, value)
            live = self.hub_status == "simulation" or (self.hub_status == "connected" and not self.silent)
            if self.wizard or not live:
                self.engine.release_all()
            else:
                self.engine.tick(now)
            if chrome:
                await chrome.push(self.engine.snapshot())
            pressed = (tuple(self.engine.pressed()), tuple(round(v, 1) for v in self.engine.out_axes))
            if pressed[0] != (last_pressed or ((),))[0]:
                print(f"    buttons: {' '.join(pressed[0]) or '-'}")
            last_pressed = pressed

    # ---- the control panel's API ----------------------------------------------

    def state(self) -> dict:
        return {
            "hub": {"status": self.hub_status, "name": self.hub.device_name if self.hub else None},
            "ports": self.devices.ports, "catalog": self.devices.catalog(), "values": self.devices.values(),
            "engine": self.engine.status(), "wizard": self.wizard.state() if self.wizard else None,
            "loadout": {"name": self.engine.loadout["name"], "file": self.file, "dirty": self.dirty},
            "calibrated": self.calibration_saved and not needs_calibration(self.devices.calibration,
                                                                          self.devices.ports),
            "notes": list(self.notes), "simulation": self.args.no_hub,
        }

    async def api(self, method: str, path: str, body: dict):
        if method == "GET":
            if path == "state":
                return 200, self.state()
            if path == "loadout":
                return 200, {"loadout": self.engine.loadout, "file": self.file, "dirty": self.dirty}
            if path == "loadouts":
                return 200, self.list_loadouts()
            if path == "calibration":
                return 200, self.devices.calibration
            if path == "meta":
                return 200, {"buttons": BUTTONS, "directions": DIRECTION_OUTPUTS, "axes": AXIS_OUTPUTS,
                             "remap": REMAP_NAMES, "actions": list(ACTION_TYPES), "modes": list(ZONE_MODES)}
            return 404, {"error": "unknown"}
        if path == "loadout":
            loadout = normalize_loadout(body["loadout"])
            self.engine.update_loadout(loadout)
            self.dirty = True
            return 200, {"loadout": self.engine.loadout}
        if path == "loadout/save":
            self.save_loadout(body.get("name"), bool(body.get("as_new")))
        elif path == "loadout/open":
            self.open_loadout(body["file"])
        elif path == "loadout/new":
            self.engine.load(empty_loadout(body.get("name") or "New loadout"))
            self.file, self.dirty = None, True
        elif path == "loadout/delete":
            target = LOADOUTS / Path(body["file"]).name
            if target.exists():
                target.unlink()
                self.note(f"Deleted loadouts/{target.name}")
                if self.file == target.name:
                    self.file, self.dirty = None, True
        elif path == "calibration":
            if self.save_calibration(complete_calibration(body["calibration"], self.devices.ports)) \
                    and self.hub_status == "connected":
                await self.restart_program()
            self.note("Calibration saved.")
        elif path == "calibrate/start":
            self.start_wizard()
        elif path == "calibrate/capture":
            await self.wizard_step()
        elif path == "calibrate/skip":
            await self.wizard_step(skip=True)
        elif path == "calibrate/cancel":
            self.wizard = None
            self.note("Calibration cancelled: nothing was changed.")
        elif path == "hub/connect":
            await self.connect()
        elif path == "hub/restart":
            await self.restart_program()
        elif path == "hub/rescan":
            await self.restart_program(rescan=True)
        elif path == "var":
            if body.get("reset"):
                self.engine.reset_var(body["name"])
            else:
                self.engine.set_var(body["name"], float(body["value"]))
        elif path == "system":
            self.engine.system_on[body["id"]] = bool(body["on"])
        elif path == "simulate":
            if not self.args.no_hub:
                raise ValueError("sliders only work with --no-hub")
            if body.get("value") is None:
                self.devices.simulated.pop(body["source"], None)
            else:
                self.devices.simulated[body["source"]] = float(body["value"])
        elif path == "release":
            self.engine.release_all()
            self.note("Released every button.")
        else:
            return 404, {"error": "unknown"}
        return 200, {"ok": True}


SpikeHubClass = None  # set in main(): importing bleak is skipped with --no-hub


async def main() -> None:
    global SpikeHubClass
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--panel-only", action="store_true", help="Chrome opens only the control panel")
    where.add_argument("--no-chrome", action="store_true", help="don't open Chrome (use the printed address)")
    parser.add_argument("--no-hub", action="store_true", help="no hub: edit loadouts, test them with sliders")
    parser.add_argument("--calibrate", action="store_true", help="run the calibration wizard again")
    parser.add_argument("--loadout", help="loadout file in loadouts/ to start with")
    parser.add_argument("--name", help="only connect to the hub with this Bluetooth name")
    parser.add_argument("--slot", type=int, default=0, help="hub program slot 0-19 (default 0)")
    parser.add_argument("--panel-port", type=int, default=8765, help="control panel port (default 8765)")
    args = parser.parse_args()
    if not args.no_hub:
        from spike import SpikeHub
        SpikeHubClass = SpikeHub

    app = App(args)
    server = PanelServer(app.api, args.panel_port)
    await server.start()
    print(f"Control panel: {server.url}")
    try:
        if not args.no_hub:
            with contextlib.suppress(RuntimeError):  # the panel shows the error and a Connect button
                await app.connect()
        async with contextlib.AsyncExitStack() as stack:
            chrome = None
            if not args.no_chrome:
                urls = [server.url] if args.panel_only else [REMOTE_PLAY_URL, server.url]
                print("Opening Chrome...")
                chrome = await stack.enter_async_context(ChromeGamepad(urls, server.url))
                if not args.panel_only:
                    print("Xbox Remote Play is the first tab, the control panel the second.\n"
                          "Sign in if asked, pick your Xbox and press Remote play.")
            await app.run(chrome)
            print("Chrome closed.")
    finally:
        app.engine.release_all()
        if app.hub and app.hub.connected:
            with contextlib.suppress(Exception):
                if not app.hub.program_stopped:
                    await app.hub.stop_program(args.slot)
                await app.hub.disconnect()
        await server.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError as e:
        sys.exit(f"Error: {e}")
    except KeyboardInterrupt:
        print("\nBye!")

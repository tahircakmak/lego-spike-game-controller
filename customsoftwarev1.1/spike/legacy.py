"""
Connect to a hub running HubOS 2: the MINDSTORMS Robot Inventor (51515) firmware, or the
older SPIKE Prime firmware (SPIKE App 2).

HubOS 2 does not speak the SPIKE Prime Bluetooth Low Energy protocol in protocol.py (that is
HubOS 3). It talks over a serial line instead: the USB cable (pyserial), or Bluetooth Classic once
the hub is paired with the computer (on macOS through IOBluetooth, see macbt.py: the /dev/cu.<hub>
port macOS makes for it can stay silent). Every message is one line of JSON ending in a carriage
return:

    computer -> hub   {"m": "program_execute", "p": {"slotid": 0}, "i": "a1B2"}
    hub -> computer   {"i": "a1B2", "r": ...}                     the answer (or "e": error)
                      {"m": 0, "p": [...]}                        every port and the gyro, all the time
                      {"m": "userProgram.print", "p": {"value": "<base64>"}, "i": ...}
                      {"m": 12, "p": [program id, running]}       a program started or stopped

A running program's print() output arrives as userProgram.print messages on some hubs, and
as plain text lines between the JSON messages on others (a MINDSTORMS hub over Bluetooth
does this), so both are passed on.

This is what the MINDSTORMS app used. It is not documented by LEGO; it was worked out by the
community (spike-tools, lego-hub-tk). LegacyHub offers the same methods as SpikeHub, so app.py
can use either.
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import string
import threading
import time
from typing import Callable

from . import protocol as p
from .macbt import bluetooth_address

LEGO_USB_VENDOR = 0x0694
HUBOS3_USB_PRODUCT = 0x0009  # a hub on HubOS 3 over USB speaks protocol.py, not JSON
OPEN_TIMEOUT_S = 8           # opening a paired Bluetooth hub's port can hang while the hub is off
MOTORS = {48: "medium", 49: "large", 65: "small", 75: "medium", 76: "large"}  # 75 / 76: Robot Inventor
COLOR, DISTANCE, FORCE, LIGHT_MATRIX = 61, 62, 63, 64


def serial_ports(remembered: str | None = None) -> list[str]:
    """
    Serial ports that may be a LEGO hub on HubOS 2, in the order to try them: the USB cable
    (LEGO's USB id), then `remembered` (the port that worked last time), then paired Bluetooth
    hubs (a hub keeps the name "LEGOHub..." unless it was renamed, so its port has it too).
    """
    from serial.tools import list_ports

    usb, bluetooth, present = [], [], set()
    for port in list_ports.comports():
        present.add(port.device)
        text = f"{port.device} {port.description} {port.name}".lower()
        if port.vid == LEGO_USB_VENDOR and port.pid != HUBOS3_USB_PRODUCT:
            usb.append(port.device)
        elif port.vid is None and "lego" in text:
            bluetooth.append(port.device)
    found = usb + ([remembered] if remembered in present else []) + bluetooth
    return list(dict.fromkeys(found))  # without repeats, in order


def all_serial_ports() -> list[str]:
    from serial.tools import list_ports

    return [port.device for port in list_ports.comports()]


def readings_from_telemetry(params: list) -> list:
    """The m=0 message's six ports, as the same reading objects protocol.py makes (for port detection)."""
    readings = []
    for index, entry in enumerate(params[:6]):
        if not isinstance(entry, list) or len(entry) < 2 or not entry[0]:
            continue
        kind, values = entry[0], entry[1] or []
        port = p.PORTS[index]
        if kind in MOTORS:
            speed, position, absolute, power = (list(values) + [0, 0, 0, 0])[:4]
            readings.append(p.Motor(port, MOTORS[kind], absolute or 0, power or 0, speed or 0, position or 0))
        elif kind == FORCE:
            readings.append(p.ForceSensor(port, 0, False))
        elif kind == DISTANCE:
            readings.append(p.DistanceSensor(port, -1))
        elif kind == COLOR:
            readings.append(p.ColorSensor(port, "none", 0, 0, 0, 0))
        elif kind == LIGHT_MATRIX:
            readings.append(p.ColorMatrix(port, [0] * 9))
    return readings


class LegacyHub:
    """
    Usage (the same as SpikeHub):
        async with LegacyHub("/dev/cu.usbmodem1101") as hub:
            await hub.run_program(source_code)
    """

    hubos = 2
    detect_timeout_s = 12  # after a program is stopped the hub soft-reboots: its readings stop for a few seconds

    def __init__(self, port: str | None = None, on_console: Callable[[str], None] = print,
                 remembered: str | None = None):
        """port: a serial port, or "auto" to try every port that looks like a hub (remembered first after USB)."""
        self.port = port
        self.remembered = remembered
        self.on_console = on_console
        self.device_name: str | None = None
        self.firmware: str | None = None
        self._serial = None
        self._buffer = b""
        self._pending: dict[str, asyncio.Future] = {}
        self._on_sensors: Callable[[list], None] | None = None
        self._program_stopped = asyncio.Event()
        self._disconnected = asyncio.Event()
        self._json_seen = asyncio.Event()
        self._hubos3_seen = False
        self._running_id = None
        self._program_started = False  # plain text lines are program output once a program runs
        self._loop: asyncio.AbstractEventLoop | None = None

    # --- connection --------------------------------------------------------

    async def connect(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self.port not in (None, "auto"):
            await self._connect(self.port)
            return
        candidates = serial_ports(self.remembered)
        if not candidates:
            others = ", ".join(all_serial_ports()) or "none"
            raise RuntimeError("No LEGO hub found on a serial port. Plug in the USB cable, or pair the hub "
                               f"over Bluetooth and pass its port with --serial (serial ports now: {others}).")
        errors = []
        for port in candidates:
            try:
                await self._connect(port)
                return
            except RuntimeError as e:
                errors.append(str(e))
        raise RuntimeError(" / ".join(errors))

    async def _open(self, port: str):
        """
        Open the connection to the hub. A paired Bluetooth hub on macOS: its serial channel through
        IOBluetooth. Anything else (the USB cable): pyserial, without waiting forever.
        """
        address = bluetooth_address(port)
        if address:
            from .macbt import MacBluetoothLink

            try:
                return await MacBluetoothLink.open(address)
            except OSError as e:
                raise RuntimeError(f"Can't reach the hub on {port} over Bluetooth: {e}. Is it on, and connected "
                                   "in the Bluetooth settings?") from None
        import serial

        opening = self._loop.run_in_executor(None, lambda: serial.Serial(port, 115200, timeout=0.1, write_timeout=2))
        try:
            return await asyncio.wait_for(asyncio.shield(opening), OPEN_TIMEOUT_S)
        except asyncio.TimeoutError:
            # If it opens after all, close it again: nobody is waiting for it any more.
            opening.add_done_callback(lambda f: f.exception() is None and f.result().close())
            raise RuntimeError(f"{port} didn't open within {OPEN_TIMEOUT_S} s. Is the hub on (and paired, "
                               "for Bluetooth)?") from None
        except (serial.SerialException, OSError) as e:
            raise RuntimeError(f"Can't open {port}: {e}") from None

    async def _connect(self, port: str) -> None:
        how = "over Bluetooth" if bluetooth_address(port) else "on the serial port"
        print(f"Opening {port} {how} (HubOS 2: MINDSTORMS / SPIKE 2 firmware)...")
        self._buffer = b""
        self._json_seen.clear()
        self._disconnected.clear()
        self._hubos3_seen = False
        self._serial = await self._open(port)
        self.device_name = port
        threading.Thread(target=self._read_loop, args=(self._serial,), name="LegacyHubRead", daemon=True).start()
        try:
            await asyncio.wait_for(self._json_seen.wait(), timeout=5)
        except asyncio.TimeoutError:
            await self.disconnect()
            if self._hubos3_seen:
                raise RuntimeError(f"The hub on {port} runs HubOS 3: connect over Bluetooth instead "
                                   "(start without --serial).") from None
            raise RuntimeError(f"No answer from a HubOS 2 hub on {port}. Is the hub on, and is the "
                               "MINDSTORMS app closed?") from None
        try:
            info = await self._request("get_firmware_info")
            self.firmware = ".".join(str(x) for x in info.get("version", []))
        except (RuntimeError, AttributeError):  # not on every HubOS 2 version: keep the start-up banner's
            self.firmware = self.firmware or "?"
        print(f"Connected. Firmware {self.firmware} (HubOS 2)")

    async def disconnect(self) -> None:
        if self._serial is not None:
            serial_port, self._serial = self._serial, None
            try:
                serial_port.close()
            except OSError:
                pass
        self._disconnected.set()

    @property
    def connected(self) -> bool:
        return self._serial is not None and not self._disconnected.is_set()

    async def __aenter__(self) -> LegacyHub:
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    # --- programs ----------------------------------------------------------

    async def upload_program(self, source: str, slot: int = 0) -> None:
        data = source.encode("utf8")
        now = int(time.time() * 1000)
        name = base64.b64encode(b"Custom software").decode()  # the hub keeps slot names in base64
        meta = {"created": now, "modified": now, "name": name, "type": "python",
                "project_id": "".join(random.choice(string.ascii_letters) for _ in range(12))}
        start = await self._request("start_write_program", {"slotid": slot, "size": len(data), "meta": meta},
                                    timeout=15)  # the hub erases the slot first: this took 4.3 s on a real hub
        block, transfer = start["blocksize"], start["transferid"]
        for i in range(0, len(data), block):
            chunk = base64.b64encode(data[i:i + block]).decode()
            await self._request("write_package", {"data": chunk, "transferid": transfer})

    async def start_program(self, slot: int = 0) -> None:
        self._program_stopped.clear()
        self._running_id = None
        self._program_started = True
        # After stopping a program the hub soft-reboots its runtime first: this took over 5 s on a real hub
        await self._request("program_execute", {"slotid": slot}, timeout=15)

    async def stop_program(self, slot: int = 0) -> None:
        self._program_started = False  # the stopped program's KeyboardInterrupt traceback is not news
        try:
            await self._request("program_terminate")
        except RuntimeError:
            pass  # nothing was running

    async def run_program(self, source: str, slot: int = 0) -> None:
        await self.stop_program(slot)
        await self.upload_program(source, slot)
        await self.start_program(slot)

    @property
    def program_stopped(self) -> bool:
        return self._program_stopped.is_set()

    # --- sensors -----------------------------------------------------------

    async def start_sensor_stream(self, on_sensors: Callable[[list], None], interval_ms: int = 100) -> None:
        """HubOS 2 sends every port's reading all the time; this only starts passing them on."""
        self._on_sensors = on_sensors

    async def stop_sensor_stream(self) -> None:
        self._on_sensors = None

    # --- low level ---------------------------------------------------------

    def _send(self, message: dict) -> None:
        if not self.connected:
            raise RuntimeError("The hub is not connected.")
        self._serial.write(json.dumps(message).encode() + b"\r")

    async def _request(self, method: str, params: dict | None = None, timeout: float = 5.0):
        msg_id = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(4))
        future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        try:
            self._send({"m": method, "p": params or {}, "i": msg_id})
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise RuntimeError(f"The hub didn't answer {method}.") from None
        finally:
            self._pending.pop(msg_id, None)

    def _read_loop(self, port) -> None:
        """Runs in its own thread (pyserial reads block), for one opened port."""
        try:
            while self._serial is port:
                data = port.read(port.in_waiting or 1)
                if data:
                    self._loop.call_soon_threadsafe(self.feed, data)
        except Exception:
            pass
        try:
            self._loop.call_soon_threadsafe(self._on_disconnect, port)
        except RuntimeError:
            pass  # the program is already shutting down

    def _on_disconnect(self, port) -> None:
        if self._serial is not port:
            return  # an earlier port (a failed attempt, or a closed connection): not this connection
        print("\nHub disconnected.")
        self._serial = None
        self._disconnected.set()

    def feed(self, data: bytes) -> None:
        """Bytes from the hub: split into lines, handle every JSON message, pass program text on."""
        self._buffer += data
        *lines, self._buffer = self._buffer.split(b"\r")
        for line in lines:
            start = line.find(b"{")
            if start >= 0:
                try:
                    message = json.loads(line[start:].decode("utf8", errors="replace"))
                except ValueError:
                    message = None
                if isinstance(message, dict):
                    self._json_seen.set()
                    self.handle(message)
                    continue
            if p.DELIMITER in line:
                self._hubos3_seen = True  # binary SPIKE Prime frames: HubOS 3
                continue
            self.text_line(line.decode("utf8", errors="replace").strip("\n\r"))
        if len(self._buffer) > 4096 and b"{" not in self._buffer:
            self._hubos3_seen = self._hubos3_seen or p.DELIMITER in self._buffer
            self._buffer = b""

    def text_line(self, text: str) -> None:
        """A line that isn't JSON: the start-up banner, or (once a program runs) the program's print()."""
        if not text.strip():
            return
        if text.startswith("MicroPython "):
            self.firmware = "MicroPython " + text.split()[1].lstrip("v").split("-")[0]
        elif self._program_started and not text.startswith('Type "help()"'):
            self.on_console(text + "\n")

    def handle(self, message: dict) -> None:
        kind = message.get("m")
        if kind is None and "i" in message:
            future = self._pending.get(message["i"])
            if future and not future.done():
                if "e" in message:
                    future.set_exception(RuntimeError(_decode_error(message["e"])))
                else:
                    future.set_result(message.get("r"))
        elif kind == 0:
            if self._on_sensors:
                self._on_sensors(readings_from_telemetry(message.get("p") or []))
        elif kind == "userProgram.print":
            self.on_console(base64.b64decode(message["p"]["value"]).decode("utf8", errors="replace"))
            if "i" in message and self.connected:
                self._send({"i": message["i"], "r": None})
        elif kind in ("user_program_error", "runtime_error"):
            params = message.get("p") or []
            text = _decode_error(params[3]) if len(params) > 3 else str(params)
            self.on_console("\nhub-error " + " | ".join(text.strip().splitlines()) + "\n")
        elif kind == 12:
            program_id, running = (list(message.get("p") or []) + [None, None])[:2]
            if running:
                self._running_id = program_id
            elif self._running_id is not None and program_id == self._running_id:
                self._program_stopped.set()


def _decode_error(value) -> str:
    try:
        decoded = base64.b64decode(value).decode("utf8", errors="replace")
        try:
            return str(json.loads(decoded))
        except ValueError:
            return decoded
    except (ValueError, TypeError):
        return str(value)

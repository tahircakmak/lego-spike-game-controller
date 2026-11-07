"""Tests for what V1.1 adds: return to rest, and HubOS 2 (MINDSTORMS) hubs. No hardware needed.

    python -m unittest discover -s tests
"""

import asyncio
import base64
import contextlib
import io
import json
import sys
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from devices import detect_ports, hub_source, kind_of, return_dps  # noqa: E402
from spike import protocol as p  # noqa: E402
from spike.legacy import LegacyHub, readings_from_telemetry  # noqa: E402


def load(file: str) -> dict:
    """Run a hub program's top level on the Mac with its main loop stubbed out."""
    runloop = types.ModuleType("runloop")
    runloop.run = lambda coroutine: coroutine.close()
    sys.modules["runloop"] = runloop
    source = (HERE / file).read_text().replace("\nmain()\n", "\n")  # the HubOS 2 program calls main() itself
    namespace = {"__name__": "hub_program"}
    exec(compile(source, file, "exec"), namespace)
    return namespace


class MotorModel:
    """A crude motor: power above its friction sets the speed, with some lag. As stiff as a real
    MINDSTORMS medium motor seemed: it didn't move at power 900 (of 10000)."""

    def __init__(self, angle):
        self.angle = float(angle)
        self.speed = 0.0
        self.measured = 0.0

    def step(self, duty, dt_ms, held=False):
        drive = max(0.0, abs(duty) - 1500) * 0.13 * (1 if duty > 0 else -1)
        self.speed += (drive - self.speed) * min(1.0, dt_ms / 50)
        if held:
            self.speed = 0.0
        self.angle += self.speed * dt_ms / 1000
        self.measured = 0.7 * self.measured + 0.3 * self.speed  # like the hub program's smoothing


class ReturnToRest(unittest.TestCase):
    def run_motor(self, start, speed, ms=5000, program="hub_program.py"):
        hp = load(program)
        returner, motor = hp["ReturnToRest"](speed), MotorModel(start)
        trace = []
        for t in range(0, ms, 10):
            duty = returner.update(motor.angle, motor.measured, 10)
            motor.step(duty, 10)
            trace.append((t, motor.angle, motor.speed, duty))
        return hp, trace

    def test_comes_back_slowly_and_stops_at_rest(self):
        hp, trace = self.run_motor(60, return_dps(0.5))
        start = next(t for t, _, _, duty in trace if duty)
        self.assertGreaterEqual(start, 240)  # waits until it has been still for 250 ms
        t_end, angle, _, duty = trace[-1]
        self.assertLessEqual(abs(angle), 6)
        self.assertEqual(duty, 0)  # lets go once it is back
        self.assertLess(max(abs(s) for _, _, s, _ in trace), 100 * 1.4)  # about 0.5x = 100 deg/s

    def test_brakes_on_arriving_so_it_does_not_overshoot(self):
        hp = load("hub_program.py")
        r = hp["ReturnToRest"](100)
        for _ in range(30):
            r.update(20, 0, 10)  # left still 20 degrees away: starts returning
        self.assertTrue(r.returning)
        self.assertEqual(r.update(2, -40, 10), 0)  # arrives at rest
        self.assertEqual(r.brake_ms, hp["BRAKE_MS"])  # asks for the brake...
        for _ in range(20):
            r.update(1, 0, 10)
        self.assertEqual(r.brake_ms, 0)  # ...for a moment, then lets go

    def test_unsticks_a_stiff_motor(self):
        hp = load("hub_program.py")
        r = hp["ReturnToRest"](100)
        duties = [r.update(7, 0, 10) for _ in range(80)]  # 7 degrees away and not moving, like the real motor
        self.assertGreater(max(abs(d) for d in duties), 2500)  # it pushes harder until it would turn

    def test_both_directions(self):
        _, trace = self.run_motor(-90, return_dps(0.5))
        self.assertLessEqual(abs(trace[-1][1]), 6)

    def test_speed_setting(self):
        def time_back(speed):
            _, trace = self.run_motor(120, return_dps(speed))
            return next(t for t, angle, _, _ in trace if abs(angle) < 20)
        self.assertLess(time_back(1.0), time_back(0.5))
        self.assertLess(time_back(0.5), time_back(0.25))

    def test_does_nothing_at_rest_or_while_moving(self):
        hp = load("hub_program.py")
        r = hp["ReturnToRest"](180)
        self.assertEqual([r.update(2, 0, 10) for _ in range(100)], [0] * 100)       # inside the deadband
        self.assertEqual([r.update(40, 100, 10) for _ in range(100)], [0] * 100)    # still being turned

    def test_lets_go_when_pushed_away(self):
        hp = load("hub_program.py")
        r = hp["ReturnToRest"](180)
        for _ in range(30):
            r.update(40, 0, 10)
        self.assertTrue(r.returning)
        self.assertEqual(r.update(45, 120, 10), 0)  # a hand turns it further away
        self.assertFalse(r.returning)

    def test_held_still_lets_go(self):
        """A gas lever held down: a short nudge, then it leaves it alone (no constant push)."""
        hp = load("hub_program.py")
        r, motor = hp["ReturnToRest"](180), MotorModel(40)
        duties = []
        for _ in range(300):  # held for 3 seconds
            duties.append(r.update(motor.angle, motor.measured, 10))
            motor.step(duties[-1], 10, held=True)
        pushing = sum(1 for d in duties if d)
        self.assertLess(pushing * 10, 3000 * 0.35)  # pushes well under a third of the time
        self.assertTrue(all(d <= 0 for d in duties))  # only ever towards rest
        first_rest = duties.index(next(d for d in duties if d)) + hp["HELD_MS"] // 10
        self.assertEqual(duties[first_rest:first_rest + 90], [0] * 90)  # then leaves it alone a while

    def test_comes_back_after_being_held_and_let_go(self):
        hp = load("hub_program.py")
        r, motor = hp["ReturnToRest"](180), MotorModel(40)
        for t in range(0, 6000, 10):
            duty = r.update(motor.angle, motor.measured, 10)
            motor.step(duty, 10, held=t < 2000)  # held for 2 s, then released
        self.assertLessEqual(abs(motor.angle), 6)

    def test_no_input_while_the_hub_moves_a_motor_back(self):
        from devices import Devices
        ports = {"D": {"kind": "motor", "type": "medium"}}
        d = Devices(ports, {"motors": {"D": {"zero": 0}}})
        d.handle_line("A D 40")
        self.assertEqual(d.values()["motor:D"], 40)  # the lever left at 40 degrees: full gas
        self.assertEqual(d.handle_line("R D 1"), "R")  # the hub starts moving it back...
        d.handle_line("A D 25")
        self.assertEqual(d.values()["motor:D"], 0)    # ...so it reads as at rest: no RT / LT on the way
        d.handle_line("R D 0")
        d.handle_line("A D 2")
        self.assertEqual(d.values()["motor:D"], 2)    # back at rest: a hand counts again
        d.handle_line("R D 1")
        d.handle_line("READY")                        # a new hub program isn't moving anything
        self.assertEqual(d.values()["motor:D"], 2)

    def test_hub_programs_report_returning(self):
        for program in ("hub_program.py", "hub_program_legacy.py"):
            with self.subTest(program):
                self.assertIn('print("R", name, 1 if returner.returning else 0)', (HERE / program).read_text())

    def test_both_hub_programs_have_the_same_controller(self):
        def part(file):
            text = (HERE / file).read_text()
            return text[text.index("STILL_DPS"):text.index("def changed")]
        self.assertEqual(part("hub_program.py"), part("hub_program_legacy.py"))

    def test_config(self):
        ports = {"A": {"kind": "motor", "type": "large"}}
        cal = {"motors": {"A": {"zero": 0, "return_to_rest": True, "return_speed": 1.5}}}
        from devices import Devices
        source = hub_source(ports, Devices(ports, cal).calibration)
        self.assertIn("MOTORS = [('A', True, 0, False, 100, 3000, True, 300)]", source)
        self.assertEqual(return_dps(0.5), 100)
        self.assertEqual(return_dps(0), 10)  # never 0 (it would never come back)


# ---- HubOS 2 -------------------------------------------------------------------------------

class FakeHubModule:
    """Just enough of HubOS 2's `hub` module for hub_program_legacy.py."""

    def __init__(self):
        self.pwm = []
        test = self

        class Motor:
            def __init__(self):
                self.values = [0, 0, 100, 0]

            def mode(self, modes): pass
            def float(self): test.pwm.append(0)
            def pwm(self, value): test.pwm.append(value)
            def preset(self, position): pass
            def get(self, fmt=None): return list(self.values)

        class Device:
            def __init__(self, raw):
                self.raw = raw

            def mode(self, modes): self.modes = modes
            def get(self, fmt=None): return list(self.raw)

        def port(motor=None, device=None, modes=()):
            return types.SimpleNamespace(motor=motor, device=device or motor,
                                         info=lambda: {"type": 1, "modes": list(modes)})

        mode = lambda name, raw, pct, si: {"name": name, "raw": raw, "pct": pct, "si": si}  # noqa: E731
        self.motor = Motor()
        self.port = types.SimpleNamespace(
            A=port(motor=self.motor),
            C=port(device=Device([512]), modes=[mode("FORCE", [0, 1024], [0, 100], [0, 10])]),
            D=port(device=Device([950]), modes=[mode("DISTL", [0, 2500], [0, 100], [0, 250])]),
            E=port(device=Device([3, 512]), modes=[mode("COLOR", [-1, 10], [0, 100], [-1, 10]),
                                                    mode("REFLT", [0, 1024], [0, 100], [0, 100])]),
        )
        pressed = lambda value: types.SimpleNamespace(is_pressed=lambda: value)  # noqa: E731
        self.button = types.SimpleNamespace(left=pressed(True), right=pressed(False))
        self.motion = types.SimpleNamespace(yaw_pitch_roll=lambda: (5, -3, 12))
        self.display = types.SimpleNamespace(show=lambda image: None)
        self.Image = types.SimpleNamespace(HAPPY=None)


class Stop(Exception):
    pass


class LegacyHubProgram(unittest.TestCase):
    def run_program(self, loops=30):
        ports = {"A": {"kind": "motor", "type": "medium"}, "C": {"kind": "force"}, "D": {"kind": "distance"},
                 "E": {"kind": "color"}}
        from devices import Devices
        cal = Devices(ports, {"motors": {"A": {"zero": 100, "return_to_rest": True}}}).calibration
        source = hub_source(ports, cal, hubos=2)
        fake = FakeHubModule()
        clock = {"ms": 0, "loops": 0}

        def sleep_ms(ms):
            clock["ms"] += ms
            clock["loops"] += 1
            if clock["loops"] == 3:
                fake.motor.values = [0, 0, 140, 0]  # moved 40 degrees from rest, then left there
            if clock["loops"] >= loops:
                raise Stop

        utime = types.SimpleNamespace(ticks_ms=lambda: clock["ms"], ticks_diff=lambda a, b: a - b, sleep_ms=sleep_ms)
        sys.modules["hub"] = fake
        sys.modules["utime"] = utime
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.suppress(Stop):
            exec(compile(source, "hub_program_legacy.py", "exec"), {"__name__": "__main__"})
        return out.getvalue().splitlines(), fake

    def test_prints_the_same_lines_as_hubos3(self):
        lines, _ = self.run_program()
        self.assertEqual(lines[0], "READY")
        for expected in ("A A 100", "A A 140", "F C 50", "D D 950", "C E 3 50", "B L 1", "T 5 -3 12", "H"):
            if expected == "H":
                continue  # heartbeat only every 500 ms
            self.assertIn(expected, lines)
        self.assertFalse([line for line in lines if line.startswith("W")], lines)

    def test_return_to_rest_uses_hubos2_power_range(self):
        _, fake = self.run_program(loops=60)
        pushes = [v for v in fake.pwm if v]
        self.assertTrue(pushes)
        self.assertTrue(all(-100 <= v < 0 for v in pushes), pushes)  # pushes back (rest is below)


class FakeSerial:
    """Answers LegacyHub's requests like a HubOS 2 hub would."""

    def __init__(self, hub):
        self.hub = hub
        self.sent = []
        self.uploaded = b""

    def write(self, data):
        message = json.loads(data.decode().strip())
        self.sent.append(message)
        answers = {"start_write_program": {"blocksize": 10, "transferid": "T1"}, "get_firmware_info": {"version": [1, 0, 6]}}
        method = message.get("m")
        if method == "write_package":
            self.uploaded += base64.b64decode(message["p"]["data"])
        if method == "program_terminate":
            reply = {"i": message["i"], "e": base64.b64encode(b'"nothing running"').decode()}
        elif method:
            reply = {"i": message["i"], "r": answers.get(method)}
        else:
            return
        asyncio.get_running_loop().call_soon(self.hub.feed, json.dumps(reply).encode() + b"\r")

    def close(self):
        pass


class LegacyConnection(unittest.IsolatedAsyncioTestCase):
    def make(self):
        console = []
        hub = LegacyHub("test", on_console=console.append)
        hub._serial = FakeSerial(hub)
        hub._loop = asyncio.get_running_loop()
        return hub, console

    TELEMETRY = {"m": 0, "p": [[75, [0, 10, -30, 0]], [76, [0, 0, 0, 0]], [63, [0, 0]], [62, [None]],
                                [61, [-1, 0]], [0, []], [0, 0, 981], [0, 0, 0], [0, 0, 0], "", 0]}

    def test_telemetry_ports(self):
        readings = readings_from_telemetry(self.TELEMETRY["p"])
        self.assertEqual({r.port: kind_of(r)["kind"] for r in readings},
                         {"A": "motor", "B": "motor", "C": "force", "D": "distance", "E": "color"})
        self.assertEqual(readings[0].type, "medium")  # the Robot Inventor medium motor
        self.assertEqual(readings[0].absolute_position, -30)

    async def test_detect_ports(self):
        hub, _ = self.make()
        asyncio.get_running_loop().call_later(0.05, hub.feed, json.dumps(self.TELEMETRY).encode() + b"\r")
        ports = await detect_ports(hub)
        self.assertEqual(ports["A"], {"kind": "motor", "type": "medium"})
        self.assertEqual(ports["B"], {"kind": "motor", "type": "large"})
        self.assertNotIn("F", ports)

    async def test_print_output_reaches_the_console(self):
        hub, console = self.make()
        text = base64.b64encode(b"A A 5\nF C 40\n").decode()
        hub.feed(b'{"m": "userProgram.print", "p": {"value": "' + text.encode() + b'"}, "i": "x1"}\r')
        self.assertEqual(console, ["A A 5\nF C 40\n"])
        self.assertEqual(hub._serial.sent[-1], {"i": "x1", "r": None})  # the hub waits for this answer

    async def test_plain_text_program_output(self):
        # A MINDSTORMS hub over Bluetooth: print() arrives as plain text lines between the JSON
        hub, console = self.make()
        hub.feed(b'MicroPython v1.14-36-g377ea11 on 2022-11-01; LEGO Technic Large Hub\r\nType "help()" for more.\r')
        self.assertEqual(console, [])  # the start-up banner is not program output
        self.assertEqual(hub.firmware, "MicroPython 1.14")
        await hub.start_program(0)
        hub.feed(b'{"m": 12, "p": ["P1", true]}\rREADY\r\nA B 22\r\n{"m": 0, "p": []}\rH\r\n')
        self.assertEqual(console, ["READY\n", "A B 22\n", "H\n"])

    async def test_split_lines_and_errors(self):
        hub, console = self.make()
        error = base64.b64encode(b"Traceback\nNameError: x").decode()
        line = json.dumps({"m": "runtime_error", "p": [0, 0, 0, error]}).encode() + b"\r"
        hub.feed(line[:10])
        hub.feed(line[10:])
        self.assertEqual(console, ["\nhub-error Traceback | NameError: x\n"])

    async def test_upload_and_start(self):
        hub, _ = self.make()
        source = "print('READY')\n" * 5
        await hub.run_program(source, slot=3)
        methods = [m.get("m") for m in hub._serial.sent]
        self.assertEqual(methods[0], "program_terminate")
        self.assertEqual(methods[1], "start_write_program")
        self.assertEqual(methods[-1], "program_execute")
        self.assertEqual(hub._serial.sent[1]["p"]["meta"]["type"], "python")
        self.assertEqual(hub._serial.uploaded.decode(), source)
        self.assertEqual(hub._serial.sent[-1]["p"], {"slotid": 3})

    async def test_program_stop(self):
        hub, _ = self.make()
        hub.feed(b'{"m": 12, "p": ["old", false]}\r')  # the previous program ending
        self.assertFalse(hub.program_stopped)
        hub.feed(b'{"m": 12, "p": ["abc", true]}\r{"m": 12, "p": ["abc", false]}\r')
        self.assertTrue(hub.program_stopped)

    async def test_recognises_hubos3(self):
        hub, _ = self.make()
        hub.feed(bytes([0x01, 0x23, 0x45, p.DELIMITER]) + b"\r")
        self.assertTrue(hub._hubos3_seen)


class MindstormsMotorsOnHubOS3(unittest.TestCase):
    def test_motor_types(self):
        self.assertEqual(p.MOTOR_TYPES[0x4B], "medium")
        self.assertEqual(p.MOTOR_TYPES[0x4C], "large")



class ConnectionOrder(unittest.TestCase):
    def kinds(self, serial, ports, settings=None):
        import tempfile
        import app as A
        args = types.SimpleNamespace(serial=serial, name=None, no_hub=False, loadout=None, calibrate=False)
        A.SpikeHubClass = lambda name, on_console: "ble"
        A.LegacyHubClass = lambda port, on_console, remembered=None: f"serial:{port}:{remembered}"
        old_ports, old_settings = A.serial_ports, A.SETTINGS
        with tempfile.TemporaryDirectory() as tmp:
            A.SETTINGS = Path(tmp) / "settings.json"
            if settings:
                A.SETTINGS.write_text(json.dumps(settings))
            A.serial_ports = lambda remembered=None: ports
            try:
                return [make(print) for make in A.App.hub_kinds(types.SimpleNamespace(args=args))]
            finally:
                A.serial_ports, A.SETTINGS = old_ports, old_settings

    def test_bluetooth_first_then_serial(self):
        self.assertEqual(self.kinds(None, []), ["ble"])
        self.assertEqual(self.kinds(None, ["/dev/cu.usbmodem1"]), ["ble", "serial:auto:None"])

    def test_last_hub_first(self):
        port = "/dev/cu.LEGOHub2CAB3356B372"
        settings = {"hub": "serial", "serial_port": port, "loadout": "x.json"}
        self.assertEqual(self.kinds(None, [port], settings), [f"serial:auto:{port}", "ble"])
        self.assertEqual(self.kinds(None, [port], {"hub": "ble"}), ["ble", "serial:auto:None"])

    def test_serial_only(self):
        self.assertEqual(self.kinds("auto", []), ["serial:auto:None"])
        self.assertEqual(self.kinds("/dev/cu.MyHub", []), ["serial:/dev/cu.MyHub:None"])

    def test_settings_keep_each_other(self):
        import tempfile
        import app as A
        old = A.SETTINGS
        with tempfile.TemporaryDirectory() as tmp:
            A.SETTINGS = Path(tmp) / "settings.json"
            try:
                A.save_settings(loadout="mine.json")
                A.save_settings(hub="serial", serial_port="/dev/cu.LEGOHub1")
                self.assertEqual(A.read_settings(), {"loadout": "mine.json", "hub": "serial",
                                                     "serial_port": "/dev/cu.LEGOHub1"})
            finally:
                A.SETTINGS = old


class SerialPorts(unittest.TestCase):
    def ports(self, remembered=None):
        from unittest import mock
        from spike import legacy
        port = lambda device, vid=None, pid=None, description="n/a": types.SimpleNamespace(  # noqa: E731
            device=device, vid=vid, pid=pid, description=description, name=device.rsplit("/", 1)[-1])
        listed = [port("/dev/cu.Bluetooth-Incoming-Port"), port("/dev/cu.LEGOHub2CAB3356B372"),
                  port("/dev/cu.usbmodem1101", 0x0694, 0x0010), port("/dev/cu.usbmodem2201", 0x0694, 0x0009),
                  port("/dev/cu.RenamedHub")]
        with mock.patch("serial.tools.list_ports.comports", return_value=listed):
            return legacy.serial_ports(remembered)

    def test_usb_then_bluetooth_lego_hubs(self):
        # the HubOS 3 hub on USB (product 0x0009) and other devices are left out
        self.assertEqual(self.ports(), ["/dev/cu.usbmodem1101", "/dev/cu.LEGOHub2CAB3356B372"])

    def test_remembered_port_after_usb(self):
        self.assertEqual(self.ports("/dev/cu.RenamedHub"),
                         ["/dev/cu.usbmodem1101", "/dev/cu.RenamedHub", "/dev/cu.LEGOHub2CAB3356B372"])
        self.assertEqual(self.ports("/dev/cu.Gone"), ["/dev/cu.usbmodem1101", "/dev/cu.LEGOHub2CAB3356B372"])


class SerialConnecting(unittest.IsolatedAsyncioTestCase):
    async def test_tries_every_port(self):
        from unittest import mock
        from spike import legacy
        hub = LegacyHub("auto", on_console=print)
        tried = []

        async def connect(port):
            tried.append(port)
            if port != "/dev/cu.LEGOHub1":
                raise RuntimeError(f"no answer on {port}")
        with mock.patch.object(legacy, "serial_ports", return_value=["/dev/cu.usbmodem1", "/dev/cu.LEGOHub1"]), \
                mock.patch.object(hub, "_connect", connect):
            await hub.connect()
        self.assertEqual(tried, ["/dev/cu.usbmodem1", "/dev/cu.LEGOHub1"])

    async def test_a_port_that_hangs_times_out(self):
        import threading as th
        from unittest import mock
        from spike import legacy
        release = th.Event()
        closed = th.Event()

        class SlowSerial:
            def __init__(self, *a, **k):
                release.wait(5)

            def close(self):
                closed.set()
        hub = LegacyHub("/dev/cu.usbmodem1", on_console=print)
        hub._loop = asyncio.get_running_loop()
        with mock.patch.object(legacy, "OPEN_TIMEOUT_S", 0.2), mock.patch("serial.Serial", SlowSerial), \
                mock.patch.object(legacy, "bluetooth_address", lambda port: None):
            with self.assertRaises(RuntimeError) as caught:
                await hub._open("/dev/cu.usbmodem1")
            self.assertIn("didn't open", str(caught.exception))
            release.set()  # it opens after all...
            await asyncio.get_running_loop().run_in_executor(None, closed.wait, 2)
        self.assertTrue(closed.is_set())  # ...and is closed again

class MacBluetooth(unittest.TestCase):
    """The macOS Bluetooth link, without real Bluetooth (a stand-in IOBluetooth module)."""

    def address(self, port, platform="darwin"):
        from unittest import mock
        from spike import macbt
        device = types.SimpleNamespace(name=lambda: "LEGO Hub 2C:AB:33:56:B3:72", addressString=lambda: "2c-ab-33-56-b3-72")
        renamed = types.SimpleNamespace(name=lambda: "Steering Rig", addressString=lambda: "aa-bb-cc-dd-ee-ff")
        fake = types.SimpleNamespace(IOBluetoothDevice=types.SimpleNamespace(pairedDevices=lambda: [device, renamed]))
        with mock.patch.dict(sys.modules, {"IOBluetooth": fake}), mock.patch.object(macbt.sys, "platform", platform):
            return macbt.bluetooth_address(port)

    def test_finds_the_paired_hub_behind_a_port(self):
        self.assertEqual(self.address("/dev/cu.LEGOHub2CAB3356B372"), "2c-ab-33-56-b3-72")
        self.assertEqual(self.address("/dev/tty.LEGOHub2CAB3356B372"), "2c-ab-33-56-b3-72")
        self.assertEqual(self.address("/dev/cu.SteeringRig"), "aa-bb-cc-dd-ee-ff")  # a renamed hub
        self.assertEqual(self.address("2C:AB:33:56:B3:72"), "2C-AB-33-56-B3-72")

    def test_leaves_other_ports_to_pyserial(self):
        self.assertIsNone(self.address("/dev/cu.usbmodem1101"))
        self.assertIsNone(self.address("/dev/cu.Bluetooth-Incoming-Port"))
        self.assertIsNone(self.address("/dev/cu.LEGOHub2CAB3356B372", platform="linux"))

    def test_link_reads_and_writes_like_pyserial(self):
        from spike.macbt import MacBluetoothLink
        link = MacBluetoothLink("2c-ab-33-56-b3-72")  # not opened: only the buffering is tested
        self.assertEqual(link.read(10), b"")  # nothing yet: waits 0.1 s, like pyserial's timeout
        link._received(b'{"m": 0}\r{"m"')
        self.assertEqual(link.in_waiting, 13)
        self.assertEqual(link.read(9), b'{"m": 0}\r')
        written = []
        link._channel = types.SimpleNamespace(writeSync_length_=lambda data, n: written.append(data) or 0,
                                              closeChannel=lambda: written.append("closed"))
        link._mtu = 4
        self.assertEqual(link.write(b"hello\r"), 6)
        self.assertEqual(written, [b"hell", b"o\r"])  # split into the channel's packet size
        link._hub_closed()
        self.assertEqual(written[-1], "closed")
        self.assertEqual(link.read(10), b'{"m"')  # what was already received is still read
        with self.assertRaises(OSError):
            link.read(10)  # then the closed channel ends LegacyHub's read loop
        with self.assertRaises(OSError):
            link.write(b"x")


if __name__ == "__main__":
    unittest.main()

"""Tests for the binding engine, the device readings, calibration and the hub program config. No hardware needed.

    python -m unittest discover -s tests
"""

import json
import sys
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from calibration import Wizard, distance_settings  # noqa: E402
from devices import NOTHING_FAR_CM, Devices, hub_source, needs_calibration, ports_from_readings  # noqa: E402
from spike import protocol as p  # noqa: E402
from engine import BUTTON_INDEX, Engine, axis_value, empty_loadout, in_zone, normalize_loadout, stick_value  # noqa: E402

LATER = 0.11  # just past the 100 ms shortest press


def load_hub_program():
    """Run hub_program.py's top level on the Mac (runloop stubbed, main() never runs)."""
    runloop = types.ModuleType("runloop")
    runloop.run = lambda coroutine: coroutine.close()
    sys.modules["runloop"] = runloop
    namespace = {"__name__": "hub_program"}
    exec(compile((HERE / "hub_program.py").read_text(), "hub_program.py", "exec"), namespace)
    return namespace


def zone(bid, source, mode, a, actions, b=None, hysteresis=0, **extra):
    return {"id": bid, "name": bid, "kind": "zone", "source": source,
            "zone": {"mode": mode, "a": a, "b": b, "hysteresis": hysteresis}, "actions": actions, **extra}


def hold(*outputs):
    return {"type": "hold", "outputs": list(outputs)}


def tap(*outputs, **extra):
    return {"type": "tap", "outputs": list(outputs), **extra}


class Base(unittest.TestCase):
    def make(self, bindings, variables=(), systems=None):
        loadout = empty_loadout()
        loadout["bindings"] = bindings
        loadout["variables"] = list(variables)
        if systems:
            loadout["systems"] = systems
        self.e = Engine(loadout, log=lambda text: None)
        self.t = 100.0
        return self.e

    def step(self, dt=0.01, **sources):
        for name, value in sources.items():
            self.e.set_source(name.replace("_", ":", 1), value)
        self.t += dt
        self.e.tick(self.t)
        return self.e.pressed()

    def axes(self):
        return [round(v, 3) for v in self.e.out_axes]


class Zones(unittest.TestCase):
    def test_modes(self):
        self.assertTrue(in_zone({"mode": "above", "a": 10}, 10))
        self.assertFalse(in_zone({"mode": "above", "a": 10}, 9.9))
        self.assertTrue(in_zone({"mode": "below", "a": -30}, -31))
        self.assertTrue(in_zone({"mode": "between", "a": 20, "b": 10}, 15))
        self.assertFalse(in_zone({"mode": "outside", "a": 10, "b": 20}, 15))
        self.assertTrue(in_zone({"mode": "outside", "a": 10, "b": 20}, 25))
        self.assertTrue(in_zone({"mode": "at", "a": 3}, 3))
        self.assertFalse(in_zone({"mode": "at", "a": 3}, 4))
        self.assertFalse(in_zone({"mode": "above", "a": 0}, None))

    def test_hysteresis_only_while_active(self):
        z = {"mode": "above", "a": 25, "hysteresis": 8}
        self.assertFalse(in_zone(z, 20, was_active=False))
        self.assertTrue(in_zone(z, 20, was_active=True))
        self.assertFalse(in_zone(z, 16, was_active=True))
        out = {"mode": "outside", "a": 10, "b": 20, "hysteresis": 2}
        self.assertTrue(in_zone(out, 11, was_active=True))
        self.assertFalse(in_zone(out, 15, was_active=True))


class HoldAndTap(Base):
    def test_motor_angle_holds_dpad_up(self):
        self.make([zone("m", "motor:E", "below", -30, [hold("dpad_up")])])
        self.assertEqual(self.step(motor_E=0), [])
        self.assertEqual(self.step(motor_E=-31), ["dpad_up"])
        self.assertEqual(self.step(5.0, motor_E=-40), ["dpad_up"])
        self.assertEqual(self.step(motor_E=0), [])

    def test_several_outputs_in_one_activation(self):
        self.make([zone("d", "distance:D", "below", 10, [hold("y", "b", "rb")])])
        self.assertEqual(sorted(self.step(distance_D=5)), ["b", "rb", "y"])

    def test_quick_press_lasts_min_press(self):
        self.make([zone("f", "force:C", "above", 10, [hold("lb")])])
        self.step(force_C=50)
        self.assertEqual(self.step(0.02, force_C=0), ["lb"])
        self.assertEqual(self.step(LATER, force_C=0), [])

    def test_tap_once_and_repeat(self):
        self.make([zone("t", "hub:left", "above", 1, [tap("a")]),
                   zone("r", "hub:right", "above", 1, [tap("b", repeat_ms=300)])])
        self.assertEqual(self.step(hub_left=1), ["a"])
        self.assertEqual(self.step(LATER), [])
        self.assertEqual(self.step(1.0), [])  # held, but a tap is once
        self.assertEqual(self.step(hub_right=1), ["b"])
        self.assertEqual(self.step(LATER), [])
        self.assertEqual(self.step(0.2), ["b"])  # repeated after 300 ms

    def test_tap_on_deactivate(self):
        self.make([zone("t", "hub:left", "above", 1, [tap("x", on="deactivate")])])
        self.assertEqual(self.step(hub_left=1), [])
        self.assertEqual(self.step(hub_left=0), ["x"])

    def test_toggle(self):
        self.make([zone("t", "hub:left", "above", 1, [{"type": "toggle", "outputs": ["lt"]}])])
        self.step(hub_left=1)
        self.assertEqual(self.step(hub_left=0), ["lt"])
        self.assertEqual(self.step(5.0), ["lt"])
        self.step(hub_left=1)
        self.assertEqual(self.step(LATER, hub_left=0), [])

    def test_stick_direction_output(self):
        self.make([zone("n", "hub:left", "above", 1, [hold("ls:ne")])])
        self.step(hub_left=1)
        self.assertEqual(self.axes(), [0.707, 0.707, 0, 0])
        self.assertEqual(self.e.snapshot()["axes"], [0.707, -0.707, 0.0, 0.0])  # Gamepad API: up is -1


class Variables(Base):
    VARS = [{"name": "ammo", "initial": 3, "min": 0, "max": 5}]

    def test_add_with_limits_and_reset(self):
        self.make([zone("up", "hub:left", "above", 1, [{"type": "var_add", "var": "ammo", "amount": 2}]),
                   zone("reset", "hub:right", "above", 1, [{"type": "var_reset", "var": "ammo"}])], self.VARS)
        for _ in range(3):
            self.step(hub_left=1)
            self.step(hub_left=0)
        self.assertEqual(self.e.var_values["ammo"], 5)  # max
        self.step(hub_right=1)
        self.assertEqual(self.e.var_values["ammo"], 3)

    def test_add_repeats_while_held(self):
        self.make([zone("up", "hub:left", "above", 1, [{"type": "var_add", "var": "ammo", "amount": -1,
                                                         "repeat_ms": 100}])], self.VARS)
        self.step(hub_left=1)
        self.step(0.1)
        self.step(0.1)
        self.assertEqual(self.e.var_values["ammo"], 0)
        self.step(0.1)
        self.assertEqual(self.e.var_values["ammo"], 0)  # min

    def test_wrap(self):
        self.make([zone("m", "hub:left", "above", 1, [{"type": "var_add", "var": "mode", "amount": 1}])],
                  [{"name": "mode", "initial": 0, "min": 0, "max": 2, "wrap": True}])
        seen = []
        for _ in range(4):
            self.step(hub_left=1)
            self.step(hub_left=0)
            seen.append(self.e.var_values["mode"])
        self.assertEqual(seen, [1, 2, 0, 1])

    def test_input_when_variable_reaches_a_number(self):
        self.make([zone("down", "hub:left", "above", 1, [{"type": "var_add", "var": "ammo", "amount": -1}]),
                   zone("empty", "var:ammo", "at", 0, [tap("x")]),
                   zone("set", "hub:right", "above", 1, [{"type": "var_set", "var": "ammo", "value": 4}])], self.VARS)
        for _ in range(3):
            self.step(hub_left=1)
            self.step(hub_left=0)
        self.assertEqual(self.e.var_values["ammo"], 0)
        self.assertEqual(self.step(), ["x"])  # var bindings react on the next tick
        self.step(hub_right=1)
        self.assertEqual(self.e.var_values["ammo"], 4)

    def test_conditions(self):
        self.make([zone("shoot", "hub:left", "above", 1, [hold("rt")],
                        conditions=[{"source": "var:ammo", "mode": "above", "a": 1}])], self.VARS)
        self.assertEqual(self.step(hub_left=1), ["rt"])
        self.e.set_var("ammo", 0)
        self.assertEqual(self.step(LATER), [])


class Remaps(Base):
    def test_force_sensor_turns_dpad_up_into_y(self):
        self.make([zone("dpad", "motor:E", "below", -30, [hold("dpad_up")]),
                   zone("swap", "force:C", "above", 10, [{"type": "remap", "from": "dpad_up", "to": "y"}])])
        self.assertEqual(self.step(motor_E=-40, force_C=0), ["dpad_up"])
        self.assertEqual(self.step(LATER, force_C=50), ["y"])
        self.assertEqual(self.step(LATER, force_C=0), ["dpad_up"])

    def test_remap_only_targets(self):
        self.make([zone("one", "hub:left", "above", 1, [hold("a")]),
                   zone("two", "hub:right", "above", 1, [hold("a")]),
                   zone("swap", "force:C", "above", 10, [{"type": "remap", "from": "a", "to": "b", "targets": ["one"]}])])
        self.assertEqual(self.step(hub_left=1, hub_right=0, force_C=50), ["b"])
        self.assertEqual(sorted(self.step(LATER, hub_left=0, hub_right=1)), ["a"])

    def test_distance_sensor_moves_left_stick_to_right_stick(self):
        self.make([{"id": "ax", "kind": "axis", "source": "motor:A", "output": "ls:y", "deadzone": [-5, 5],
                    "range": [-90, 90]},
                   zone("swap", "distance:D", "below", 10, [{"type": "remap", "from": "ls", "to": "rs"}])])
        self.step(motor_A=90, distance_D=50)
        self.assertEqual(self.axes(), [0, 1, 0, 0])
        self.step(distance_D=8)
        self.assertEqual(self.axes(), [0, 0, 0, 1])


class Axes(Base):
    def test_deadzone_example(self):
        dz, rng = [-10, 24], [-90, 90]
        self.assertEqual(axis_value(0, dz, rng), 0)
        self.assertEqual(axis_value(24, dz, rng), 0)
        self.assertEqual(axis_value(-10, dz, rng), 0)
        self.assertAlmostEqual(axis_value(57, dz, rng), 0.5)
        self.assertAlmostEqual(axis_value(-50, dz, rng), -0.5)
        self.assertEqual(axis_value(200, dz, rng), 1)
        self.assertEqual(axis_value(90, dz, rng, invert=True), -1)

    def test_axis_binding_on_right_stick_and_trigger(self):
        self.make([{"id": "x", "kind": "axis", "source": "motor:F", "output": "rs:x", "deadzone": [-10, 24],
                    "range": [-90, 90]},
                   {"id": "t", "kind": "axis", "source": "force:C", "output": "rt", "deadzone": [0, 5],
                    "range": [0, 105]}])
        self.step(motor_F=-90, force_C=55)
        self.assertEqual(self.axes(), [0, 0, -1, 0])
        self.assertAlmostEqual(self.e.out_buttons[BUTTON_INDEX["rt"]], 0.5)

    def test_axis_uses_calibrated_range(self):
        self.make([{"id": "x", "kind": "axis", "source": "motor:F", "output": "ls:x", "deadzone": [0, 0]}])
        self.e.ranges = {"motor:F": (-40, 40)}
        self.step(motor_F=20)
        self.assertEqual(self.axes(), [0.5, 0, 0, 0])

    def test_stick_deadzone_and_8_directions(self):
        self.assertEqual(stick_value(3, 3, 6, 30), (0, 0))
        x, y = stick_value(30, 0, 6, 30)
        self.assertAlmostEqual(x, 1)
        x, y = stick_value(20, 14, 6, 30, directions=8)  # about 35°: snaps to north-east
        self.assertAlmostEqual(x, y)
        x, y = stick_value(20, 5, 6, 30, directions=4)
        self.assertAlmostEqual(y, 0)

    def test_combined_sticks_stay_in_the_circle(self):
        self.make([zone("n", "hub:left", "above", 1, [hold("ls:n")]),
                   {"id": "x", "kind": "axis", "source": "gyro:right", "output": "ls:x", "deadzone": [0, 0],
                    "range": [-30, 30]}])
        self.step(hub_left=1, gyro_right=30)
        x, y = self.e.out_axes[:2]
        self.assertAlmostEqual(x * x + y * y, 1)


class Systems(Base):
    def test_system_conditions_and_toggle_action(self):
        systems = [{"id": "main", "name": "Main"},
                   {"id": "walk", "name": "Walk", "conditions": [{"source": "var:mode", "mode": "at", "a": 1}]}]
        self.make([zone("w", "hub:left", "above", 1, [hold("a")], system="walk"),
                   zone("off", "hub:right", "above", 1, [{"type": "system", "system": "walk", "mode": "toggle"}])],
                  [{"name": "mode", "initial": 0}], systems)
        self.assertEqual(self.step(hub_left=1), [])
        self.e.set_var("mode", 1)
        self.assertEqual(self.step(), ["a"])
        self.step(hub_right=1)
        self.assertEqual(self.step(LATER), [])

    def test_editing_keeps_variables(self):
        e = self.make([], [{"name": "ammo", "initial": 3}])
        e.set_var("ammo", 1)
        loadout = e.copy_loadout()
        loadout["bindings"].append(zone("new", "hub:left", "above", 1, [hold("a")]))
        e.update_loadout(loadout)
        self.assertEqual(e.var_values["ammo"], 1)

    def test_editing_does_not_fire_held_bindings_again(self):
        e = self.make([zone("m", "hub:right", "above", 1, [{"type": "var_add", "var": "mode", "amount": 1}])],
                      [{"name": "mode", "initial": 0, "min": 0, "max": 1, "wrap": True}])
        self.step(hub_right=1)
        e.update_loadout(e.copy_loadout())
        self.step()
        self.assertEqual(e.var_values["mode"], 1)


class Validation(unittest.TestCase):
    def bad(self, change):
        loadout = empty_loadout()
        loadout["variables"] = [{"name": "ammo"}]
        loadout["bindings"] = [zone("z", "hub:left", "above", 1, [hold("a")])]
        change(loadout)
        with self.assertRaises(ValueError):
            normalize_loadout(loadout)

    def test_errors(self):
        self.bad(lambda lo: lo["bindings"][0]["actions"].append(hold("not_a_button")))
        self.bad(lambda lo: lo["bindings"][0]["actions"].append({"type": "var_add", "var": "nope"}))
        self.bad(lambda lo: lo["bindings"][0]["zone"].update(mode="between", b=None))
        self.bad(lambda lo: lo["bindings"][0]["actions"].append({"type": "remap", "from": "ls", "to": "a"}))
        self.bad(lambda lo: lo["variables"].append({"name": "2bad"}))
        self.bad(lambda lo: lo["bindings"].append({"kind": "stick", "deadzone": 10, "full": 5}))

    def test_example_loadouts_load(self):
        for path in sorted((HERE / "loadouts").glob("*.json")):
            with self.subTest(path.name):
                Engine(json.loads(path.read_text()), log=lambda text: None).tick(1.0)


class DeviceReadings(unittest.TestCase):
    PORTS = {"A": {"kind": "motor", "type": "medium"}, "C": {"kind": "force"}, "D": {"kind": "distance"},
             "E": {"kind": "color"}}

    def test_values(self):
        cal = {"motors": {"A": {"zero": 170, "invert": True}}, "deadzones": {"gyro:pitch": [-3, 3]},
               "gyro": {"level": [0, 10, 0], "right": [0, 20], "forward": None}}
        d = Devices(self.PORTS, cal)
        for line in ("A A -170", "F C 40", "D D 95", "C E 3 55", "T 5 12 15", "B L 1"):
            self.assertIsNotNone(d.handle_line(line))
        v = d.values()
        self.assertEqual(v["motor:A"], -20)  # -170 - 170 wraps to 20, inverted
        self.assertEqual(v["force:C"], 40)
        self.assertEqual(v["distance:D"], 9.5)
        self.assertEqual((v["color:E"], v["color:E:reflection"]), (3, 55))
        self.assertEqual(v["gyro:pitch"], 0)  # 2° is inside the deadzone
        self.assertEqual(v["gyro:right"], 15)
        self.assertEqual(v["hub:left"], 1)
        self.assertIsNone(d.handle_line("Traceback (most recent call last):"))

    def test_port_detection_ignores_hub_readings(self):
        readings = [p.Battery(90), p.Imu("top", "front", 0, 0, 0, 0, 0, 1000, 0, 0, 0), p.Display([0] * 25),
                    p.Motor("A", "medium", 0, 0, 0, 0), p.ForceSensor("C", 0, False), p.DistanceSensor("D", -1),
                    p.ColorSensor("E", "none", 0, 0, 0, 0), p.ColorMatrix("F", [0] * 9)]
        self.assertEqual(ports_from_readings(readings),
                         {"A": {"kind": "motor", "type": "medium"}, "C": {"kind": "force"}, "D": {"kind": "distance"},
                          "E": {"kind": "color"}, "F": {"kind": "unsupported", "type": "3x3 colour light matrix"}})

    def test_distance_nothing(self):
        d = Devices(self.PORTS, None)
        d.handle_line("D D -1")
        self.assertEqual(d.values()["distance:D"], NOTHING_FAR_CM)
        d.calibration["distance"]["D"]["nothing_means"] = "near"
        self.assertEqual(d.values()["distance:D"], 0)

    def test_motor_without_calibration_starts_at_zero(self):
        d = Devices(self.PORTS, None)
        d.handle_line("A A 77")
        d.handle_line("A A 87")
        self.assertEqual(d.values()["motor:A"], 10)

    def test_hub_source(self):
        d = Devices(self.PORTS, {"motors": {"A": {"zero": 5, "spring": True}}})
        source = hub_source(self.PORTS, d.calibration)
        self.assertIn("MOTORS = [('A', True, 5, True, 100, 3000)]", source)
        self.assertIn("COLOR_PORTS = ['E']", source)
        compile(source, "hub_program.py", "exec")

    def test_needs_calibration(self):
        self.assertTrue(needs_calibration(None, self.PORTS))
        self.assertFalse(needs_calibration({"ports": self.PORTS}, self.PORTS))
        self.assertTrue(needs_calibration({"ports": {"A": {"kind": "force"}}}, self.PORTS))


class Calibrating(unittest.TestCase):
    def test_wizard(self):
        ports = {"A": {"kind": "motor", "type": "medium"}, "C": {"kind": "force"}, "D": {"kind": "distance"}}
        raw = {"A": 100, "C": 2, "D": -1, "tilt": (0, 1, 2)}
        w = Wizard(ports, None, raw)
        self.assertEqual([s["id"] for s in w.steps], ["rest", "motor", "force", "distance", "gyro_right", "gyro_forward"])
        w.capture()
        self.assertEqual(w.cal["motors"]["A"]["zero"], 100)
        for angle in (100, 60, 150, 100):  # -40° .. +50°
            raw["A"] = angle
            w.feed()
        w.capture()
        self.assertEqual((w.cal["motors"]["A"]["min"], w.cal["motors"]["A"]["max"]), (-40, 50))
        raw["C"] = 82
        w.feed()
        w.capture()
        self.assertEqual(w.cal["force"]["C"]["press"], 26)
        raw["D"] = 45
        w.capture()
        self.assertEqual(w.cal["distance"]["D"]["near_cm"], 6.7)  # 45 mm + 22 mm
        raw["tilt"] = (0, 1, 30)
        w.capture()
        self.assertEqual(w.cal["gyro"]["right"], [0, 28])
        w.skip()
        self.assertTrue(w.done)
        self.assertEqual(w.cal["ports"], ports)

    def test_distance_covered(self):
        self.assertEqual(distance_settings(-1, 300)["nothing_means"], "near")
        self.assertEqual(distance_settings(50, -1)["nothing_means"], "far")


class HubProgram(unittest.TestCase):
    def test_spring_and_reporting(self):
        hp = load_hub_program()
        self.assertEqual(hp["spring_duty"](1, 100, 3000), 0)       # inside the deadband
        self.assertEqual(hp["spring_duty"](20, 100, 3000), -2000)  # pushes back
        self.assertEqual(hp["spring_duty"](-80, 100, 3000), 3000)  # capped
        self.assertEqual(hp["wrap180"](190), -170)
        self.assertTrue(hp["changed"](-1, 300, 4))
        self.assertFalse(hp["changed"](302, 300, 4))


if __name__ == "__main__":
    unittest.main()

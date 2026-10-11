"""Tests for the V2.1 rules, the hub's lever logic and the hub config. No hardware needed.

    python -m unittest discover -s tests
"""

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg  # noqa: E402
from bridge import check_ports, distance_settings, hub_source, tilt_right  # noqa: E402
from controller import ControllerLogic, steer_amount  # noqa: E402
from spike import protocol as p  # noqa: E402

LATER = cfg.MIN_PRESS_S + 0.01


def load_hub_program():
    """Run hub_program.py's top level on the Mac (runloop stubbed, main() never runs)."""
    runloop = types.ModuleType("runloop")
    runloop.run = lambda coroutine: coroutine.close()
    sys.modules["runloop"] = runloop
    namespace = {"__name__": "hub_program"}
    exec(compile((Path(__file__).parent.parent / "hub_program.py").read_text(), "hub_program.py", "exec"), namespace)
    return namespace


class Base(unittest.TestCase):
    def setUp(self):
        self.c = ControllerLogic({"near_mm": 80, "covered": False}, log=lambda text: None)
        self.t = 100.0
        self.c.update(self.t)

    def at(self, dt=0.0):
        self.t += dt
        self.c.update(self.t)
        return self.t

    def down(self, dt=0.0):
        return self.c.pad.pressed(self.at(dt))


class HeldButtons(Base):
    def check_held(self, press, release, button):
        press()
        self.assertEqual(self.down(), [button])
        self.assertEqual(self.down(5.0), [button])  # still held, not repeated
        release()
        self.assertEqual(self.down(), [])

    def test_right_button_is_melee(self):
        self.check_held(lambda: self.c.on_lever("right_button", "+", self.t),
                        lambda: self.c.on_lever("right_button", "0", self.t), "x")

    def test_left_button_is_jump(self):
        self.check_held(lambda: self.c.on_lever("left_button", "+", self.t),
                        lambda: self.c.on_lever("left_button", "0", self.t), "a")

    def test_right_trigger_is_ranged(self):
        self.check_held(lambda: self.c.on_lever("right_trigger", "+", self.t),
                        lambda: self.c.on_lever("right_trigger", "0", self.t), "rt")

    def test_left_trigger_is_lt(self):
        self.check_held(lambda: self.c.on_lever("left_trigger", "+", self.t),
                        lambda: self.c.on_lever("left_trigger", "0", self.t), "lt")

    def test_force_sensor_is_lb(self):
        self.check_held(lambda: self.c.on_force(True, self.t), lambda: self.c.on_force(False, self.t), "lb")

    def test_left_hub_button_is_dpad_up(self):
        self.check_held(lambda: self.c.on_hub_button("left", True, self.t),
                        lambda: self.c.on_hub_button("left", False, self.t), "dpad_up")

    def test_quick_tap_still_lasts_min_press(self):
        self.c.on_force(True, self.t)
        self.c.on_force(False, self.t + 0.001)
        self.assertEqual(self.down(0.05), ["lb"])
        self.assertEqual(self.down(LATER), [])

    def test_rest_event_changes_nothing(self):
        self.c.on_lever("right_trigger", "+", self.t)
        self.c.on_lever("right_trigger", "0", self.t)
        self.c.on_lever("right_trigger", "Z", self.t)
        self.assertEqual(self.down(LATER), [])

    def test_everything_at_once(self):
        for lever in ("right_button", "left_button", "right_trigger", "left_trigger"):
            self.c.on_lever(lever, "+", self.t)
        self.c.on_force(True, self.t)
        self.assertEqual(sorted(self.down()), ["a", "lb", "lt", "rt", "x"])


class Artifacts(Base):
    def test_hand_uses_all_artifacts_once(self):
        self.c.on_distance(400, self.t)
        self.assertEqual(self.down(), [])
        self.c.on_distance(60, self.t)
        self.assertEqual(sorted(self.down()), sorted(cfg.ARTIFACTS))
        self.c.on_distance(50, self.t)
        self.assertEqual(self.down(LATER), [])  # a short press, not held, not repeated
        self.c.on_distance(70, self.t)
        self.assertEqual(self.down(1.0), [])

    def test_needs_to_go_away_before_next_use(self):
        self.c.on_distance(60, self.t)
        self.at(2.0)
        self.c.on_distance(80 + cfg.ARTIFACT_FAR_EXTRA_MM - 1, self.t)  # inside the hysteresis
        self.c.on_distance(60, self.t)
        self.assertEqual(self.down(), [])
        self.c.on_distance(400, self.t)
        self.c.on_distance(60, self.t)
        self.assertEqual(sorted(self.down()), sorted(cfg.ARTIFACTS))

    def test_cooldown(self):
        self.c.on_distance(60, self.t)
        self.c.on_distance(400, self.at(0.2))
        self.c.on_distance(60, self.at(0.1))
        self.assertEqual(self.down(LATER), [])

    def test_sensor_losing_the_hand(self):
        self.c.on_distance(60, self.t)
        self.c.on_distance(-1, self.at(0.1))
        self.at(cfg.DISTANCE_LOST_S + 0.01)
        self.assertFalse(self.c.artifacts.near)
        self.c.on_distance(60, self.at(1.0))
        self.assertEqual(sorted(self.down()), sorted(cfg.ARTIFACTS))

    def test_short_blank_keeps_the_hand(self):
        self.c.on_distance(60, self.t)
        self.c.on_distance(-1, self.at(0.1))
        self.c.on_distance(60, self.at(0.1))
        self.assertTrue(self.c.artifacts.near)

    def test_covered_sensor(self):
        c = ControllerLogic({"near_mm": 50, "covered": True}, log=lambda text: None)
        c.on_distance(800, self.t)
        self.assertEqual(c.pad.pressed(self.t), [])
        c.on_distance(-1, self.t)
        self.assertEqual(sorted(c.pad.pressed(self.t)), sorted(cfg.ARTIFACTS))


class Moving(Base):
    def toggle(self):
        self.c.on_hub_button("right", True, self.t)
        self.c.on_hub_button("right", False, self.t)

    def stick(self):
        return tuple(round(v, 2) for v in self.c.pad.left_stick)

    def test_standing_still_at_start(self):
        self.c.on_tilt(40, self.t)
        self.at(1.0)
        self.assertEqual(self.stick(), (0.0, 0.0))
        self.assertEqual(self.c.heading, cfg.START_HEADING)  # tilt only steers while moving

    def test_hub_button_toggles_moving(self):
        self.c.on_hub_button("right", True, self.t)
        self.at(0.01)
        self.assertEqual(self.stick(), (0.0, -cfg.MOVE_SPEED))
        self.c.on_hub_button("right", True, self.t)  # repeated "down" while held
        self.at(1.0)
        self.assertTrue(self.c.moving)
        self.c.on_hub_button("right", False, self.t)
        self.assertTrue(self.c.moving)  # letting go doesn't stop
        self.toggle()
        self.at(0.01)
        self.assertEqual(self.stick(), (0.0, 0.0))
        self.assertEqual(self.down(), [])  # no Xbox button for it

    def test_tilt_steers(self):
        self.toggle()
        self.c.on_tilt(cfg.STEER_FULL_DEG + 10, self.t)
        for _ in range(50):
            self.at(0.01)
        self.assertAlmostEqual(self.c.heading, cfg.STEER_TURN_RATE * 0.5, places=3)
        self.c.on_tilt(0, self.t)
        self.at(1.0)
        self.assertAlmostEqual(self.c.heading, cfg.STEER_TURN_RATE * 0.5, places=3)  # keeps that direction
        self.c.on_tilt(-cfg.STEER_FULL_DEG, self.t)
        for _ in range(50):
            self.at(0.01)
        self.assertAlmostEqual(self.c.heading, 0.0, places=3)

    def test_small_tilt_does_not_steer(self):
        self.toggle()
        self.c.on_tilt(cfg.STEER_DEADZONE_DEG - 1, self.t)
        self.at(0.05)
        self.at(1.0)
        self.assertEqual(self.c.heading, cfg.START_HEADING)

    def test_steer_amount(self):
        self.assertEqual(steer_amount(0), 0.0)
        self.assertEqual(steer_amount(cfg.STEER_DEADZONE_DEG), 0.0)
        self.assertAlmostEqual(steer_amount((cfg.STEER_DEADZONE_DEG + cfg.STEER_FULL_DEG) / 2), 0.5)
        self.assertEqual(steer_amount(-90), -1.0)

    def test_release_all_stops(self):
        self.toggle()
        self.c.on_lever("right_trigger", "+", self.t)
        self.c.release_all()
        self.at(0.01)
        self.assertEqual(self.down(), [])
        self.assertEqual(self.stick(), (0.0, 0.0))

    def test_dashboard_snapshot(self):
        self.c.on_distance(60, self.t)
        d = self.c.dashboard()
        self.assertTrue(d["near"])
        self.assertEqual(d["distance"], 60)
        self.assertIn("all artifacts", d["log"][-1])


class HubLever(unittest.TestCase):
    def setUp(self):
        hub = load_hub_program()
        self.lever = hub["Lever"](20, 5, 10)
        self.hub = hub

    def run_angles(self, angles):
        return [e for e in (self.lever.update(a) for a in angles) if e]

    def test_pull_hold_release(self):
        self.assertEqual(self.run_angles([0, 10, 19, 21, 40, 40, 38, 36, 35, 20, 10, 2]), ["+", "0", "Z"])

    def test_jitter_does_not_repeat(self):
        self.assertEqual(self.run_angles([25, 22, 25, 22, 25, 30, 26, 30]), ["+"])

    def test_rearms_only_near_rest(self):
        self.assertEqual(self.run_angles([30, 20, 15, 30, 15, 5, 30]), ["+", "0", "Z", "+"])

    def test_other_direction_does_nothing(self):
        self.assertEqual(self.run_angles([0, -30, -60, 0]), [])

    def test_spring_pushes_back_towards_rest(self):
        duty = self.hub["spring_duty"]
        self.assertEqual(duty(0), 0)
        self.assertEqual(duty(2), 0)  # dead band: no buzzing at rest
        self.assertEqual(duty(10), -10 * self.hub["SPRING_STRENGTH"])
        self.assertEqual(duty(-10), 10 * self.hub["SPRING_STRENGTH"])
        self.assertEqual(duty(500), -self.hub["SPRING_MAX"])

    def test_distance_reports(self):
        changed = self.hub["distance_changed"]
        self.assertTrue(changed(100, None))
        self.assertFalse(changed(102, 100))
        self.assertTrue(changed(105, 100))
        self.assertTrue(changed(-1, 100))
        self.assertTrue(changed(60, -1))
        self.assertFalse(changed(-1, -1))


class HubConfig(unittest.TestCase):
    def test_source_has_config_and_compiles(self):
        calibration = {"levers": {"right_button": {"port": "A", "sign": 1}, "left_button": {"port": "E", "sign": -1},
                                  "right_trigger": {"port": "B", "sign": -1},
                                  "left_trigger": {"port": "F", "sign": 1}}}
        source = hub_source(calibration)
        compile(source, "hub_program.py", "exec")
        self.assertIn(f"('right_trigger', 'B', -1, {cfg.TRIGGER_PULL_DEG}, {cfg.TRIGGER_RELEASE_DEG}, ", source)
        self.assertIn(f"('left_button', 'E', -1, {cfg.BUTTON_PRESS_DEG}, {cfg.BUTTON_RELEASE_DEG}, "
                      f"{cfg.AUTO_RETURN}, {cfg.BUTTON_SPRING})", source)
        self.assertIn(f"{cfg.AUTO_RETURN}, False)", source)  # the triggers have no spring
        self.assertIn(f"SPRING_STRENGTH = {cfg.SPRING_STRENGTH}", source)
        self.assertIn("FORCE_PORT = 'C'", source)
        self.assertIn("DISTANCE_PORT = 'D'", source)
        self.assertEqual(source.count("LEVERS = "), 1)

    def test_ports(self):
        good = {"E": p.Motor("E", "medium", 0, 0, 0, 0), "A": p.Motor("A", "small", 0, 0, 0, 0),
                "B": p.Motor("B", "large", 0, 0, 0, 0), "F": p.Motor("F", "large", 0, 0, 0, 0),
                "C": p.ForceSensor("C", 0, False), "D": p.DistanceSensor("D", -1)}
        check_ports(good)
        swapped = dict(good, C=p.DistanceSensor("C", -1), D=p.ForceSensor("D", 0, False))
        with self.assertRaises(RuntimeError) as e:
            check_ports(swapped)
        self.assertIn("port C should have a force sensor, found distance sensor", str(e.exception))
        with self.assertRaises(RuntimeError) as e:
            check_ports({k: v for k, v in good.items() if k != "F"})
        self.assertIn("port F should have a large motor (left trigger), found nothing", str(e.exception))


class Calibration(unittest.TestCase):
    def test_tilt_right_skewed_mount(self):
        tilt = {"level": [-1, 0], "right": [43, -10]}
        self.assertAlmostEqual(tilt_right(tilt, -1 + 43, -10), 44.1, places=1)
        self.assertAlmostEqual(tilt_right(tilt, -1 - 43, 10), -44.1, places=1)
        self.assertAlmostEqual(tilt_right(tilt, -1 + 10, 43), 0.0, places=6)  # forward: no steering

    def test_distance_settings(self):
        self.assertEqual(distance_settings(50, 900), {"near_mm": 75, "covered": False})
        self.assertEqual(distance_settings(50, -1), {"near_mm": 75, "covered": False})
        self.assertEqual(distance_settings(-1, 900), {"near_mm": 50, "covered": True})
        with self.assertRaises(RuntimeError):
            distance_settings(50, 90)
        with self.assertRaises(RuntimeError):
            distance_settings(-1, -1)


if __name__ == "__main__":
    unittest.main()

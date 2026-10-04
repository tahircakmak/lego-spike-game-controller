"""Tests for the V2 rules, the hub's lever logic and the hub config. No hardware needed.

    python -m unittest discover -s tests
"""

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg  # noqa: E402
from bridge import hub_source, tilt_degrees  # noqa: E402
from controller import GAMEPLAY, INVENTORY, ControllerLogic  # noqa: E402

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
        self.c = ControllerLogic(log=lambda text: None)
        self.t = 100.0
        self.c.update(self.t)

    def at(self, dt=0.0):
        self.t += dt
        self.c.update(self.t)
        return self.t

    def down(self, dt=0.0):
        return self.c.pad.pressed(self.at(dt))

    def neutral(self):
        self.c.on_tilt(0, 0, self.t)
        self.at(cfg.GYRO_NEUTRAL_HOLD_S + 0.01)


class ForceSensor(Base):
    def test_x_held_while_pressed(self):
        self.c.on_force(True, self.t)
        self.assertEqual(self.down(), ["x"])
        self.assertEqual(self.down(5.0), ["x"])  # still held, not repeated
        self.c.on_force(False, self.t)
        self.assertEqual(self.down(), [])

    def test_quick_tap_still_lasts_min_press(self):
        self.c.on_force(True, self.t)
        self.c.on_force(False, self.t + 0.001)
        self.assertEqual(self.down(0.05), ["x"])
        self.assertEqual(self.down(LATER), [])

    def test_a_in_inventory(self):
        self.c.on_lever("left_button", "-", self.t)
        self.at(LATER)
        self.c.on_force(True, self.t)
        self.assertEqual(self.down(), ["a"])
        self.c.on_force(False, self.t)
        self.assertEqual(self.down(LATER), [])


class Gyro(Base):
    def test_one_gesture_per_tilt(self):
        self.neutral()
        self.c.on_tilt(0, 30, self.t)  # right
        self.assertEqual(self.down(), ["rb"])
        for _ in range(10):  # stays tilted: nothing more
            self.c.on_tilt(0, 32, self.at(0.05))
        self.assertEqual(self.down(), [])
        self.neutral()
        self.c.on_tilt(0, 30, self.t)
        self.assertEqual(self.down(), ["rb"])

    def test_needs_neutral_hold_before_next(self):
        self.neutral()
        self.c.on_tilt(-30, 0, self.t)  # backward
        self.assertEqual(self.down(), ["y"])
        self.c.on_tilt(0, 0, self.at(LATER))
        self.c.on_tilt(30, 0, self.at(0.02))  # snap-back overshoot, too soon
        self.assertEqual(self.down(), [])

    def test_started_tilted_does_nothing(self):
        self.c.on_tilt(0, -30, self.t)
        self.assertEqual(self.down(), [])

    def test_below_threshold_does_nothing(self):
        self.neutral()
        self.c.on_tilt(0, cfg.GYRO_THRESHOLD_DEG["left"] - 1, self.t)
        self.assertEqual(self.down(), [])

    def test_forward_rolls_with_lb(self):
        self.neutral()
        self.c.on_tilt(30, 0, self.t)
        self.assertEqual(self.down(), ["lb"])
        self.assertEqual(self.c.pad.snapshot(self.t)["axes"], [0.0] * 4)  # no stick flick
        self.assertEqual(self.down(LATER), [])

    def test_inventory_mapping(self):
        self.c.on_lever("left_button", "-", self.t)
        self.c.on_lever("left_button", "0", self.at(LATER))
        self.neutral()
        self.c.on_tilt(0, -30, self.t)
        self.assertEqual(self.down(), ["x"])


class MotorisedButtons(Base):
    def test_left_press_holds_lt(self):
        self.c.on_lever("left_button", "+", self.t)
        self.assertEqual(self.down(1.0), ["lt"])
        self.c.on_lever("left_button", "0", self.t)
        self.assertEqual(self.down(), [])

    def test_right_press_holds_a(self):
        self.c.on_lever("right_button", "+", self.t)
        self.assertEqual(self.down(), ["a"])
        self.c.on_lever("right_button", "0", self.t)
        self.assertEqual(self.down(LATER), [])

    def test_back_toggles_moving(self):
        self.assertFalse(self.c.moving)
        for expected in (True, False, True):
            self.c.on_lever("right_button", "-", self.t)
            self.assertEqual(self.c.moving, expected)
            self.c.on_lever("right_button", "0", self.at(0.1))
            self.c.on_lever("right_button", "Z", self.at(0.1))
        self.assertEqual(self.down(), [])  # the toggle presses no button

    def test_left_back_opens_inventory_once(self):
        self.c.on_lever("left_button", "-", self.t)
        self.assertEqual(self.c.mode, INVENTORY)
        self.assertEqual(self.down(), ["dpad_up"])
        self.assertEqual(self.down(LATER), [])  # a tap: a hold would open the mini overlay
        self.c.on_lever("left_button", "-", self.t)  # pushed back again in the inventory
        self.assertEqual(self.c.mode, INVENTORY)
        self.assertEqual(self.down(), [])


class MovingAndStanding(Base):
    def moving_on(self):
        self.c.on_lever("right_button", "-", self.t)
        self.c.on_lever("right_button", "0", self.at())

    def stick(self):
        return self.c.pad.snapshot(self.t)["axes"][:2]

    def test_standing_still(self):
        self.at(1.0)
        self.assertEqual(self.stick(), [0.0, 0.0])

    def test_moving_walks_and_steers(self):
        self.moving_on()
        self.at(0.01)
        self.assertEqual(self.stick(), [0.0, -1.0])  # straight up
        self.c.on_lever("trigger", "+", self.t)  # right trigger
        for _ in range(75):  # 0.75 s at 120 deg/s = 90 deg clockwise
            self.at(0.01)
        self.c.on_lever("trigger", "0", self.t)
        self.at(0.5)  # released: the direction stays
        x, y = self.stick()
        self.assertAlmostEqual(x, 1.0, places=1)
        self.assertAlmostEqual(y, 0.0, places=1)
        self.c.on_lever("trigger", "-", self.t)  # left trigger turns back
        for _ in range(75):
            self.at(0.01)
        x, y = self.stick()
        self.assertAlmostEqual(y, -1.0, places=1)

    def test_triggers_dont_shoot_while_moving(self):
        self.moving_on()
        self.c.on_lever("trigger", "+", self.t)
        self.assertEqual(self.down(), [])

    def test_ranged_held_until_released(self):
        self.c.on_lever("trigger", "+", self.t)
        self.assertEqual(self.down(), ["rt"])
        self.assertEqual(self.down(3.0), ["rt"])
        self.assertEqual(self.c.pad.snapshot(self.t)["buttons"][7], 1)
        self.c.on_lever("trigger", "0", self.t)  # the hub saw it move back 5 degrees
        self.assertEqual(self.down(), [])

    def test_left_trigger_does_nothing_standing(self):
        self.c.on_lever("trigger", "-", self.t)
        self.assertEqual(self.down(), [])
        self.assertEqual(self.stick(), [0.0, 0.0])

    def test_toggling_while_shooting_releases_rt(self):
        self.c.on_lever("trigger", "+", self.t)
        self.at(LATER)
        self.moving_on()
        self.assertEqual(self.down(), [])
        self.c.on_lever("trigger", "0", self.at(0.1))  # its release does not steer or shoot
        self.assertEqual(self.c.steer, None)

    def test_toggling_while_steering_stops_steering(self):
        self.moving_on()
        self.c.on_lever("trigger", "-", self.t)
        self.c.on_lever("right_button", "-", self.at(0.1))  # MOVING off
        self.assertEqual(self.c.steer, None)
        self.assertEqual(self.down(), [])
        self.assertEqual(self.stick(), [0.0, 0.0])


class Inventory(Base):
    def open(self):
        self.c.on_lever("left_button", "-", self.t)
        self.c.on_lever("left_button", "0", self.at(LATER))
        self.at(LATER)

    def test_hub_buttons_move_up_down(self):
        self.open()
        self.c.on_hub_button("left", True, self.t)
        self.assertEqual(self.down(), ["dpad_up"])
        self.c.on_hub_button("left", False, self.at(LATER))
        self.c.on_hub_button("right", True, self.t)
        self.assertEqual(self.down(), ["dpad_down"])

    def test_short_pull_moves_slot(self):
        self.open()
        self.c.on_lever("trigger", "-", self.t)
        self.assertEqual(self.down(), ["dpad_left"])
        self.c.on_lever("trigger", "0", self.at(0.2))
        self.assertEqual(self.down(1.0), [])
        self.assertEqual(self.c.mode, INVENTORY)

    def test_long_hold_closes(self):
        self.open()
        self.c.on_lever("trigger", "+", self.t)
        self.assertEqual(self.down(), ["dpad_right"])
        self.at(cfg.INVENTORY_CLOSE_HOLD_S + 1)
        self.assertEqual(self.c.mode, INVENTORY)  # the hub times the hold, not the Mac
        self.c.on_lever("trigger", "C", self.t)
        self.assertEqual(self.c.mode, GAMEPLAY)
        self.assertEqual(self.down(), ["b"])
        self.c.on_lever("trigger", "0", self.at(0.5))  # letting go afterwards: no ranged shot
        self.assertEqual(self.down(), [])
        self.assertFalse(self.c.moving)

    def test_gameplay_held_inputs_end_when_inventory_opens(self):
        self.c.on_force(True, self.t)
        self.c.on_hub_button("left", True, self.t)
        self.assertEqual(self.down(), ["x", "view"])
        self.open()
        self.assertEqual(self.down(), [])
        self.c.on_force(False, self.t)  # released inside the inventory: no A
        self.c.on_hub_button("left", False, self.t)
        self.assertEqual(self.down(), [])

    def test_motorised_buttons_do_nothing_in_inventory(self):
        self.open()
        self.c.on_lever("left_button", "+", self.t)
        self.c.on_lever("right_button", "+", self.t)
        self.c.on_lever("right_button", "-", self.t)
        self.assertEqual(self.down(), [])
        self.assertFalse(self.c.moving)

    def test_opening_while_moving_stops_the_player(self):
        self.c.on_lever("right_button", "-", self.t)
        self.c.on_lever("right_button", "0", self.at())
        self.open()
        self.at(0.5)
        self.assertEqual(self.c.pad.snapshot(self.t)["axes"][:2], [0.0, 0.0])

    def test_dashboard_snapshot(self):
        import json
        self.open()
        self.c.on_force(True, self.t)
        state = self.c.dashboard()
        self.assertEqual((state["mode"], state["force"]), (INVENTORY, True))
        self.assertIn("Force sensor pressed -> a down", state["log"])
        json.dumps(state)  # it is sent to the page as JSON

    def test_release_all(self):
        self.c.on_force(True, self.t)
        self.c.on_lever("trigger", "+", self.t)
        self.c.release_all()
        self.assertEqual(self.c.pad.snapshot(self.t), {"axes": [0.0] * 4, "buttons": [0] * 17})


class HubLever(unittest.TestCase):
    def setUp(self):
        Lever = load_hub_program()["Lever"]
        self.lever = Lever(plus_deg=20, minus_deg=20, release_deg=5, rearm_deg=10)

    def run_angles(self, angles):
        return [e for e in (self.lever.update(a) for a in angles) if e]

    def test_pull_hold_release_five_degrees(self):
        self.assertEqual(self.run_angles([0, 10, 19, 20, 30, 40, 41, 39, 38, 37]), ["+"])
        self.assertEqual(self.run_angles([36]), ["0"])  # 5 below the deepest point (41)

    def test_jitter_does_not_repeat(self):
        self.assertEqual(self.run_angles([25, 27, 24, 26, 23, 25, 27, 26]), ["+"])

    def test_rearms_only_near_rest(self):
        self.assertEqual(self.run_angles([30, 20, 30, 40, 25]), ["+", "0"])
        self.assertEqual(self.run_angles([10]), ["Z"])
        self.assertEqual(self.run_angles([25]), ["+"])

    def test_other_direction(self):
        self.assertEqual(self.run_angles([-5, -21, -35, -30, 0, -30]), ["-", "0", "Z", "-"])


class HubModesAndSpring(unittest.TestCase):
    def setUp(self):
        self.hub = load_hub_program()
        self.modes = self.hub["Modes"](800)
        self.trigger = self.hub["Lever"](20, 20, 5, 10)
        self.diff = lambda a, b: a - b

    def pull(self, angle, now=0):
        event = self.trigger.update(angle)
        if event in ("+", "-"):
            self.trigger.ranged_press = event == "+" and self.modes.ranged()
        if event:
            self.modes.lever_event("trigger", event, now)
        return event

    def free(self, angle):
        return self.hub["trigger_free"](self.modes, self.trigger, angle)

    def test_standing_right_pull_is_free_for_ranged(self):
        self.assertTrue(self.free(10))   # on its way to a ranged pull
        self.assertFalse(self.free(-10))  # the left trigger springs back while standing
        self.pull(30)
        self.assertTrue(self.free(30))   # held: no spring

    def test_moving_springs_back(self):
        self.modes.lever_event("right_button", "-", 0)
        self.assertTrue(self.modes.moving)
        self.assertFalse(self.free(10))
        self.pull(30)
        self.assertFalse(self.free(30))

    def test_ranged_pull_then_moving_springs_back(self):
        self.pull(30)
        self.modes.lever_event("right_button", "-", 0)
        self.assertFalse(self.free(30))

    def test_inventory_springs_back_and_hold_closes(self):
        self.modes.lever_event("left_button", "-", 0)
        self.assertTrue(self.modes.inventory)
        self.assertFalse(self.free(10))
        self.pull(30, now=1000)
        self.assertFalse(self.modes.tick(1700, self.diff))
        self.assertTrue(self.modes.tick(1800, self.diff))
        self.assertFalse(self.modes.tick(1900, self.diff))  # once
        self.assertFalse(self.modes.inventory)
        # That pull started in the inventory, so it is no ranged pull: it springs back.
        self.assertFalse(self.free(30))

    def test_short_pull_in_inventory_does_not_close(self):
        self.modes.lever_event("left_button", "-", 0)
        self.pull(30, now=1000)
        self.pull(24, now=1200)  # spring brings it back: released
        self.assertFalse(self.modes.tick(5000, self.diff))
        self.assertTrue(self.modes.inventory)

    def test_inventory_ignores_other_toggles(self):
        self.modes.lever_event("left_button", "-", 0)
        self.modes.lever_event("right_button", "-", 0)
        self.assertFalse(self.modes.moving)

    def test_spring_duty(self):
        duty = self.hub["spring_duty"]
        self.assertEqual(duty(1), 0)  # dead band
        self.assertEqual(duty(10), -cfg.TRIGGER_SPRING_STRENGTH * 10)  # pushes back towards 0
        self.assertEqual(duty(-10), cfg.TRIGGER_SPRING_STRENGTH * 10)
        self.assertEqual(duty(1000), -cfg.TRIGGER_SPRING_MAX)  # capped


class HubConfig(unittest.TestCase):
    def test_source_has_config_and_compiles(self):
        calibration = {"levers": {"left_button": {"port": "C", "sign": 1},
                                  "right_button": {"port": "D", "sign": -1},
                                  "trigger": {"port": "E", "sign": -1}}}
        source = hub_source({"force": "B", "buttons": ["C", "D"], "large": "E"}, calibration)
        compile(source, "hub_program.py", "exec")
        self.assertIn(f"('trigger', 'E', -1, {cfg.TRIGGER_PULL_DEG}, {cfg.TRIGGER_PULL_DEG}, {cfg.TRIGGER_RELEASE_DEG}, ", source)
        self.assertIn("FORCE_PORT = 'B'", source)
        self.assertEqual(source.count("LEVERS = "), 1)


class Tilt(unittest.TestCase):
    def test_skewed_mount(self):
        tilt = {"level": [-1, 0], "forward": [1, -23], "right": [43, -10]}  # V1's real calibration
        forward, right = tilt_degrees(tilt, -1 + 43, -10)
        self.assertAlmostEqual(forward, 0.0, places=6)
        self.assertAlmostEqual(right, 44.1, places=1)
        forward, right = tilt_degrees(tilt, -1 - 1, 23)  # backward
        self.assertLess(forward, -20)
        self.assertAlmostEqual(right, 0.0, places=6)


if __name__ == "__main__":
    unittest.main()

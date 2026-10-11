"""
The V2.1 control rules: physical controller events in, virtual Xbox controller state out.

    right motorised button   X  (melee)              held while pressed
    left motorised button    A  (jump)               held while pressed
    right back trigger       RT (ranged)             held while pulled
    left back trigger        LT                      held while pulled
    force sensor             LB                      held while pressed
    left hub button          D-pad up                held while pressed
    hand over distance       Y + B + RB (artifacts)  all at once, once per hand
    right hub button         MOVING on / off: the player walks by themselves
    hub tilt left / right    turns the walking direction (like a steering wheel)

Everything is edge-driven: a held action starts on the press event and ends on the
matching release, so nothing repeats while an input is held. Each held Xbox button is
owned by the physical input that pressed it (see VirtualGamepad). No I/O happens here:
bridge.py feeds events in and sends the pad's state to Chrome.
"""

import math
from collections import deque

import config as cfg
from gamepad import BUTTON_INDEX, VirtualGamepad

LEVERS = ("right_button", "left_button", "right_trigger", "left_trigger")
WHAT = {"right_button": "melee", "left_button": "jump", "right_trigger": "ranged",
        "left_trigger": "left trigger", "force_sensor": "", "hub_left": "inventory"}
LABEL = {"right_button": "Right button", "left_button": "Left button",
         "right_trigger": "Right trigger", "left_trigger": "Left trigger"}


def check_config() -> None:
    """Fail early on a typo in config.py's mappings."""
    for key, button in cfg.BUTTONS.items():
        if button is not None and button not in BUTTON_INDEX:
            raise ValueError(f"config.BUTTONS[{key!r}] = {button!r} is not an Xbox button")
    for button in cfg.ARTIFACTS:
        if button not in BUTTON_INDEX:
            raise ValueError(f"config.ARTIFACTS has {button!r}, which is not an Xbox button")
    if not 0 <= cfg.STEER_DEADZONE_DEG < cfg.STEER_FULL_DEG:
        raise ValueError("config.STEER_DEADZONE_DEG must be smaller than STEER_FULL_DEG")


def steer_amount(right_deg: float) -> float:
    """Tilt to the right in degrees -> steering from -1 (full left) to 1 (full right)."""
    size = (abs(right_deg) - cfg.STEER_DEADZONE_DEG) / (cfg.STEER_FULL_DEG - cfg.STEER_DEADZONE_DEG)
    return math.copysign(max(0.0, min(1.0, size)), right_deg)


class ArtifactSensor:
    """
    Turns distance readings into "hand arrived" moments (one per hand).

    near_mm: a reading at or below this is a hand. covered: the sensor reads -1 (nothing)
    when the hand is right on it, so -1 counts as a hand too. Otherwise -1 means
    "nothing in front", and a hand that was there is gone after DISTANCE_LOST_S.
    """

    def __init__(self, near_mm: int, covered: bool = False):
        self.near_mm = near_mm
        self.covered = covered
        self.distance = -1
        self.near = False
        self.blank_since: float | None = None
        self.last_use: float | None = None

    def on_distance(self, mm: int, now: float) -> bool:
        """True when the hand just arrived and the artifacts should be used."""
        self.distance = mm
        if mm < 0 and not self.covered:
            if self.blank_since is None:
                self.blank_since = now
            return False
        self.blank_since = None
        if mm < 0 or mm <= self.near_mm:
            if self.near:
                return False
            self.near = True
            if self.last_use is not None and now - self.last_use < cfg.ARTIFACT_COOLDOWN_S:
                return False
            self.last_use = now
            return True
        if mm > self.near_mm + cfg.ARTIFACT_FAR_EXTRA_MM:
            self.near = False
        return False

    def tick(self, now: float) -> None:
        if self.near and self.blank_since is not None and now - self.blank_since >= cfg.DISTANCE_LOST_S:
            self.near = False


class ControllerLogic:
    def __init__(self, distance: dict, log=print):
        """distance: the calibration's {"near_mm": ..., "covered": ...} for the artifacts."""
        self.recent_log: deque[str] = deque(maxlen=7)  # for the dashboard

        def log_and_keep(text: str) -> None:
            self.recent_log.append(text)
            log(text)

        self.log = log_and_keep
        self.pad = VirtualGamepad(cfg.MIN_PRESS_S)
        near_mm = cfg.ARTIFACT_NEAR_MM if cfg.ARTIFACT_NEAR_MM is not None else distance["near_mm"]
        self.artifacts = ArtifactSensor(near_mm, distance.get("covered", False))
        self.moving = False
        self.heading = float(cfg.START_HEADING)  # deg clockwise from screen-up
        self.tilt_right = 0.0                    # degrees, from the calibrated level
        self.last_update: float | None = None
        # Physical state, for the status line and the dashboard.
        self.force_down = False
        self.hub_buttons = {"left": False, "right": False}
        self.levers = {name: "0" for name in LEVERS}
        self.angles = {name: 0 for name in LEVERS}

    # ---- physical inputs -----------------------------------------------------

    def on_force(self, down: bool, now: float) -> None:
        if down == self.force_down:
            return
        self.force_down = down
        if down:
            self.pad.hold("force_sensor", cfg.BUTTONS["force_sensor"], now)
            self.log(f"Force sensor pressed -> {cfg.BUTTONS['force_sensor']} down")
        else:
            self.pad.release("force_sensor", now)

    def on_hub_button(self, which: str, down: bool, now: float) -> None:
        if down == self.hub_buttons[which]:
            return
        self.hub_buttons[which] = down
        if which == "right":
            if down:
                self.moving = not self.moving
                self.log("Right hub button -> MOVING = ON" if self.moving else "Right hub button -> MOVING = OFF")
        elif down:
            self.pad.hold("hub_left", cfg.BUTTONS["hub_left"], now)
            self.log(f"Left hub button -> {cfg.BUTTONS['hub_left']} down (inventory)")
        else:
            self.pad.release("hub_left", now)

    def on_tilt(self, right_deg: float, now: float) -> None:
        self.tilt_right = right_deg

    def on_lever(self, lever: str, event: str, now: float) -> None:
        """lever: one of LEVERS. event: + 0 Z (see hub_program.py)."""
        self.levers[lever] = event
        if event == "+":
            button = cfg.BUTTONS[lever]
            self.pad.hold(lever, button, now)
            self.log(f"{LABEL[lever]} {'pulled' if 'trigger' in lever else 'pressed'} -> {button} down"
                     f" ({WHAT[lever]})")
        elif event == "0":
            self.pad.release(lever, now)

    def on_distance(self, mm: int, now: float) -> None:
        if self.artifacts.on_distance(mm, now):
            for button in cfg.ARTIFACTS:
                self.pad.pulse(button, now)
            self.log(f"Hand over the distance sensor -> {' + '.join(cfg.ARTIFACTS)} (all artifacts)")

    # ---- time ----------------------------------------------------------------

    def update(self, now: float) -> None:
        """Call often (every ~10 ms): steering and the distance sensor's time-out."""
        dt = 0.0 if self.last_update is None else min(0.1, now - self.last_update)
        self.last_update = now
        self.artifacts.tick(now)
        if self.moving:
            self.heading = (self.heading + steer_amount(self.tilt_right) * cfg.STEER_TURN_RATE * dt) % 360
            h = math.radians(self.heading)
            self.pad.left_stick = (cfg.MOVE_SPEED * math.sin(h), -cfg.MOVE_SPEED * math.cos(h))  # up is -1
        else:
            self.pad.left_stick = (0.0, 0.0)

    def release_all(self) -> None:
        """Lost the hub or shutting down: every button up, sticks centred, standing still."""
        self.pad.release_all()
        self.moving = False

    # ---- reporting -----------------------------------------------------------

    def dashboard(self) -> dict:
        """Everything the dashboard page shows about the LEGO side."""
        return {
            "moving": self.moving, "heading": round(self.heading, 1),
            "steer": round(steer_amount(self.tilt_right), 2), "tilt": round(self.tilt_right, 1),
            "force": self.force_down, "hub": dict(self.hub_buttons),
            "levers": dict(self.levers), "angles": dict(self.angles),
            "distance": self.artifacts.distance, "near": self.artifacts.near,
            "near_mm": self.artifacts.near_mm, "log": list(self.recent_log),
        }

    def status(self, now: float) -> str:
        buttons = " ".join(self.pad.pressed(now)) or "-"
        lx, ly = self.pad.left_stick
        return (f"{'MOVING' if self.moving else 'STANDING':8}  heading {self.heading:5.0f}°  "
                f"stick {lx:+.2f},{ly:+.2f}  buttons: {buttons}")

"""
The V2 control rules: physical controller events in, virtual Xbox controller state out.

    GAMEPLAY ── left button pushed back ──▶ INVENTORY
        ▲                                       │
        └──── shoulder trigger held 0.8 s ──────┘  (timed on the hub: "M <port> C")

    GAMEPLAY has two movement states, toggled by pushing the right button back:
        STANDING (start)  right shoulder trigger = ranged attack (held)
        MOVING            shoulder triggers steer the walking direction

Everything is edge-driven: an action starts on a press / pull event and ends on the
matching release, so nothing repeats while an input is held. Each held Xbox button is
owned by the physical input that pressed it (see VirtualGamepad), and switching mode
lets go of everything, so no input leaks from gameplay into the inventory or back.
No I/O happens here: bridge.py feeds events in and sends the pad's state to Chrome.
"""

import math

import config as cfg
from gamepad import BUTTON_INDEX, VirtualGamepad

GAMEPLAY, INVENTORY = "GAMEPLAY", "INVENTORY"
GAMEPLAY_MAP = cfg.GAMEPLAY
INVENTORY_MAP = cfg.INVENTORY


def check_config() -> None:
    """Fail early on a typo in config.py's mappings."""
    for table_name, table in (("GAMEPLAY", GAMEPLAY_MAP), ("INVENTORY", INVENTORY_MAP)):
        for key, action in table.items():
            if action is not None and action != "rs_flick" and action not in BUTTON_INDEX:
                raise ValueError(f"config.{table_name}[{key!r}] = {action!r} is not an Xbox button")
    if cfg.STEER_STYLE not in ("turn", "strafe"):
        raise ValueError(f"config.STEER_STYLE must be 'turn' or 'strafe', not {cfg.STEER_STYLE!r}")


class GyroGestures:
    """
    Turns tilt into one-shot gestures: forward / backward / left / right.

    A gesture fires once when the tilt passes its threshold, then nothing fires until the
    hub has been back inside the dead zone for GYRO_NEUTRAL_HOLD_S. It also starts
    disarmed, so starting the program with the hub tilted does nothing.
    """

    def __init__(self):
        self.forward = self.right = 0.0
        self.armed = False
        self.neutral_since: float | None = None

    def on_tilt(self, forward: float, right: float, now: float) -> str | None:
        self.forward, self.right = forward, right
        return self.tick(now)

    def tick(self, now: float) -> str | None:
        f, r = self.forward, self.right
        if max(abs(f), abs(r)) <= cfg.GYRO_DEADZONE_DEG:
            if self.neutral_since is None:
                self.neutral_since = now
            if not self.armed and now - self.neutral_since >= cfg.GYRO_NEUTRAL_HOLD_S:
                self.armed = True
            return None
        self.neutral_since = None
        if not self.armed:
            return None
        if abs(f) >= abs(r):
            direction, amount = ("forward" if f > 0 else "backward"), abs(f)
        else:
            direction, amount = ("right" if r > 0 else "left"), abs(r)
        if amount < cfg.GYRO_THRESHOLD_DEG[direction]:
            return None
        self.armed = False
        return direction


class ControllerLogic:
    def __init__(self, log=print):
        self.log = log
        self.pad = VirtualGamepad(cfg.MIN_PRESS_S)
        self.gyro = GyroGestures()
        self.mode = GAMEPLAY
        self.moving = False
        self.heading = float(cfg.START_HEADING)  # deg clockwise from screen-up
        self.steer: str | None = None            # "left" / "right" while steering
        self.ranged = False
        self.last_update: float | None = None
        # Physical state, for the status line.
        self.force_down = False
        self.hub_buttons = {"left": False, "right": False}
        self.levers = {"left_button": "0", "right_button": "0", "trigger": "0"}
        self.angles = {"left_button": 0, "right_button": 0, "trigger": 0}

    # ---- physical inputs -----------------------------------------------------

    def on_force(self, down: bool, now: float) -> None:
        if down == self.force_down:
            return
        self.force_down = down
        if down:
            button = (GAMEPLAY_MAP if self.mode == GAMEPLAY else INVENTORY_MAP)["force_sensor"]
            self.pad.hold("force", button, now)
            self.log(f"Force sensor pressed -> {button} down")
        elif self.pad.held_by("force"):
            self.log(f"Force sensor released -> {self.pad.held_by('force')} up")
            self.pad.release("force", now)

    def on_hub_button(self, which: str, down: bool, now: float) -> None:
        if down == self.hub_buttons[which]:
            return
        self.hub_buttons[which] = down
        owner = f"hub_{which}"
        if down:
            button = (GAMEPLAY_MAP if self.mode == GAMEPLAY else INVENTORY_MAP)[owner]
            self.pad.hold(owner, button, now)
            self.log(f"Hub {which} button -> {button} down")
        else:
            self.pad.release(owner, now)

    def on_tilt(self, forward: float, right: float, now: float) -> None:
        gesture = self.gyro.on_tilt(forward, right, now)
        if gesture:
            self._gesture(gesture, now)

    def on_lever(self, lever: str, event: str, now: float) -> None:
        """lever: left_button / right_button / trigger. event: + - 0 Z C (see hub_program.py)."""
        self.levers[lever] = event
        if event == "Z":
            return
        if lever == "trigger":
            self._trigger(event, now)
        elif event == "0":
            self.pad.release(lever, now)
        elif self.mode == INVENTORY:
            pass  # the motorised buttons have no inventory mapping
        elif lever == "left_button" and event == "+":
            self.pad.hold(lever, GAMEPLAY_MAP["left_button_press"], now)
            self.log(f"Left button pressed -> {GAMEPLAY_MAP['left_button_press']} down (health potion)")
        elif lever == "left_button":
            self._open_inventory(now)
        elif event == "+":
            self.pad.hold(lever, GAMEPLAY_MAP["right_button_press"], now)
            self.log(f"Right button pressed -> {GAMEPLAY_MAP['right_button_press']} down (jump)")
        else:
            self._set_moving(not self.moving, now)

    # ---- time ----------------------------------------------------------------

    def update(self, now: float) -> None:
        """Call often (every ~10 ms): steering and gyro re-arm."""
        dt = 0.0 if self.last_update is None else min(0.1, now - self.last_update)
        self.last_update = now
        gesture = self.gyro.tick(now)
        if gesture:
            self._gesture(gesture, now)

        if self.mode == GAMEPLAY and self.moving:
            if cfg.STEER_STYLE == "strafe":
                x = {"left": -1.0, "right": 1.0, None: 0.0}[self.steer]
                self.pad.left_stick = (x * cfg.MOVE_SPEED, 0.0)
            else:
                if self.steer:
                    turn = cfg.STEER_TURN_RATE * dt
                    self.heading = (self.heading + (turn if self.steer == "right" else -turn)) % 360
                self.pad.left_stick = self._heading_vector(cfg.MOVE_SPEED)
        else:
            self.pad.left_stick = (0.0, 0.0)

    def release_all(self) -> None:
        """Lost the hub or shutting down: every button up, sticks centred."""
        self.pad.release_all()
        self.steer = None
        self.ranged = False

    # ---- rules ---------------------------------------------------------------

    def _trigger(self, event: str, now: float) -> None:
        if event == "C":  # the hub timed a trigger hold in the inventory
            if self.mode == INVENTORY:
                self._close_inventory(now)
            return
        side = {"+": "right", "-": "left", "0": None}[event]
        if self.mode == INVENTORY:
            if side:
                button = INVENTORY_MAP[f"trigger_{side}"]
                self._send(button, now)
                self.log(f"{side.capitalize()} shoulder trigger -> {button} (slot {side}); "
                         f"hold {cfg.INVENTORY_CLOSE_HOLD_S} s to close the inventory")
        elif self.moving:
            if side or self.steer:
                self.log(f"{side.capitalize()} shoulder trigger -> steering {side}" if side else "Steering released")
            self.steer = side
        elif side == "right":
            self.ranged = True
            self.pad.hold("trigger", GAMEPLAY_MAP["ranged"], now)
            self.log(f"Right shoulder trigger pulled -> {GAMEPLAY_MAP['ranged']} held (ranged)")
        elif side is None and self.ranged:
            self.ranged = False
            self.pad.release("trigger", now)
            self.log(f"Right shoulder trigger back {cfg.TRIGGER_RELEASE_DEG}° -> "
                     f"{GAMEPLAY_MAP['ranged']} released")

    def _gesture(self, direction: str, now: float) -> None:
        action = (GAMEPLAY_MAP if self.mode == GAMEPLAY else INVENTORY_MAP)[f"gyro_{direction}"]
        if action is None:
            return
        self._send(action, now)
        self.log(f"Tilt {direction} -> {action}")

    def _send(self, action: str, now: float) -> None:
        """A one-shot action: a button pulse or a right stick flick."""
        if action == "rs_flick":
            self.pad.flick_right_stick(*self._heading_vector(1.0), cfg.ROLL_FLICK_S, now)
        else:
            self.pad.pulse(action, now)

    def _set_moving(self, moving: bool, now: float) -> None:
        self.moving = moving
        # Whatever the trigger was doing belongs to the old state: stop it. It has to be
        # pulled again to steer or shoot in the new state.
        self.steer = None
        if self.ranged:
            self.ranged = False
            self.pad.release("trigger", now)
        self.log("MOVING = ON" if moving else "MOVING = OFF (standing)")

    def _open_inventory(self, now: float) -> None:
        self._leave_mode(now)
        self.mode = INVENTORY
        self._send(GAMEPLAY_MAP["left_button_back"], now)
        self.log(f"Left button back -> {GAMEPLAY_MAP['left_button_back']}: INVENTORY")

    def _close_inventory(self, now: float) -> None:
        self._leave_mode(now)
        self.mode = GAMEPLAY
        if INVENTORY_MAP["close"]:
            self._send(INVENTORY_MAP["close"], now)
        self.log(f"Shoulder trigger held -> {INVENTORY_MAP['close']}: GAMEPLAY (standing)")

    def _leave_mode(self, now: float) -> None:
        for owner in list(self.pad.holds):
            self.pad.release(owner, now)
        self.steer = None
        self.ranged = False
        if self.moving:
            self.moving = False  # back from the inventory standing still, never walking off
            self.log("MOVING = OFF (standing)")
        self.pad.left_stick = (0.0, 0.0)

    def _heading_vector(self, length: float) -> tuple[float, float]:
        h = math.radians(self.heading)
        return (length * math.sin(h), -length * math.cos(h))  # Gamepad API: up is -1

    def status(self, now: float) -> str:
        state = self.mode if self.mode == INVENTORY else f"{self.mode}/{'MOVING' if self.moving else 'STANDING'}"
        buttons = " ".join(self.pad.pressed(now)) or "-"
        lx, ly = self.pad.left_stick
        return f"{state:18} heading {self.heading:5.0f}°  stick {lx:+.2f},{ly:+.2f}  buttons: {buttons}"


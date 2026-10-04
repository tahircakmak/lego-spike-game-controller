# V2 hub program: runs ON the SPIKE Prime hub (MicroPython, LEGO firmware).
# bridge.py fills in the CONFIG block from config.py, uploads this file and starts it.
#
# Start it with the motorised buttons and shoulder triggers at rest: that is angle 0.
#
# Printed lines (one event per line):
#   READY                  program started
#   H                      heartbeat, every HEARTBEAT_MS
#   T <pitch> <roll>       hub tilt in degrees, when it changes
#   F 1 / F 0              force sensor pressed / released
#   B L 1 / B L 0          left hub button down / up   (B R ... for the right one)
#   A <port> <angle>       a motor's angle from rest (raw motor direction), when it changes
#   M <port> +             lever pressed / pulled in its "+" direction
#   M <port> -             lever pushed / pulled the other way
#   M <port> 0             lever released (moved back RELEASE degrees from its deepest point)
#   M <port> Z             lever back at rest: it can fire again
#   M <port> C             trigger held INVENTORY_CLOSE_HOLD_MS in the inventory: close it
#   W <text>               warning (for example the trigger spring switched itself off)
# "+" is a normal press for a motorised button and the RIGHT shoulder trigger for the
# trigger motor; bridge.py sets SIGN from the calibration so that this holds.
#
# The hub follows the same modes as bridge.py (GAMEPLAY / INVENTORY, MOVING / STANDING),
# driven by the same lever events, so it knows when the shoulder triggers are for a
# ranged attack. Only then do they stay where they are pulled; otherwise the large motor
# works as a spring and brings them back to rest as soon as they are let go.
import runloop

# <config> (replaced by bridge.py)
FORCE_PORT = "B"
MOTOR_PORTS = ["C", "D", "E"]
# role, port, sign, plus_deg, minus_deg, release_deg, auto_return
LEVERS = []
REARM_DEG = 10
RETURN_SPEED = 600
RETURN_TIMEOUT_MS = 1500
FORCE_PRESS = 10
FORCE_RELEASE = 5
GYRO_REPORT_MS = 20
HUB_LOOP_MS = 10
HEARTBEAT_MS = 500
INVENTORY_CLOSE_HOLD_MS = 800
TRIGGER_SPRING = True
TRIGGER_SPRING_STRENGTH = 120
TRIGGER_SPRING_MAX = 4000
TRIGGER_SPRING_DEADBAND_DEG = 2
TRIGGER_SPRING_LIMIT_DEG = 150
# </config>

ANGLE_REPORT_STEP = 2  # degrees


class Lever:
    """Edge detection for one motor used as a lever. update() takes the signed angle."""

    REST, PLUS, MINUS, REARMING = 0, 1, -1, 2

    def __init__(self, plus_deg, minus_deg, release_deg, rearm_deg):
        self.plus_deg = plus_deg
        self.minus_deg = minus_deg
        self.release_deg = release_deg
        self.rearm_deg = rearm_deg
        self.state = Lever.REST
        self.peak = 0
        self.ranged_press = False  # the current pull is a ranged attack (trigger only)

    def update(self, angle):
        """Returns "+", "-", "0", "Z" or None."""
        if self.state == Lever.REST:
            if angle >= self.plus_deg:
                self.state, self.peak = Lever.PLUS, angle
                return "+"
            if angle <= -self.minus_deg:
                self.state, self.peak = Lever.MINUS, -angle
                return "-"
        elif self.state == Lever.REARMING:
            if abs(angle) <= self.rearm_deg:
                self.state = Lever.REST
                return "Z"
        else:
            depth = angle * self.state  # how far in the pressed direction
            if depth > self.peak:
                self.peak = depth
            elif depth <= self.peak - self.release_deg:
                self.state = Lever.REARMING
                return "0"
        return None


class Modes:
    """The hub's copy of the controller modes, updated from the hub's own lever events."""

    def __init__(self, close_hold_ms):
        self.close_hold_ms = close_hold_ms
        self.inventory = False
        self.moving = False
        self.close_since = None

    def lever_event(self, role, event, now):
        if role == "left_button" and event == "-" and not self.inventory:
            self.inventory, self.moving = True, False
        elif role == "right_button" and event == "-" and not self.inventory:
            self.moving = not self.moving
        elif role == "trigger" and self.inventory:
            if event == "+" or event == "-":
                self.close_since = now
            elif event == "0":
                self.close_since = None

    def tick(self, now, ticks_diff):
        """True once when a trigger held in the inventory closes it."""
        if self.inventory and self.close_since is not None and ticks_diff(now, self.close_since) >= self.close_hold_ms:
            self.inventory, self.moving, self.close_since = False, False, None
            return True
        return False

    def ranged(self):
        """GAMEPLAY + STANDING: the right trigger is the ranged attack."""
        return not self.inventory and not self.moving


def trigger_free(modes, lever, angle):
    """Leave the trigger where it is (no spring): a ranged pull, or about to become one."""
    if not modes.ranged():
        return False
    if lever.state == Lever.PLUS:
        return lever.ranged_press
    return lever.state == Lever.REST and angle > 0


def spring_duty(raw_angle):
    """Motor power (-10000..10000) that pulls the trigger back to rest, like a spring."""
    if abs(raw_angle) <= TRIGGER_SPRING_DEADBAND_DEG:
        return 0
    duty = TRIGGER_SPRING_STRENGTH * raw_angle
    return -max(-TRIGGER_SPRING_MAX, min(TRIGGER_SPRING_MAX, duty))


async def main():
    from hub import port, button, motion_sensor, light_matrix
    import force_sensor
    import motor
    import time

    force_port = getattr(port, FORCE_PORT)
    motors = [(name, getattr(port, name)) for name in MOTOR_PORTS]
    for _, p in motors:
        motor.stop(p, stop=motor.COAST)
        motor.reset_relative_position(p, 0)
    levers = []  # (role, name, port, sign, Lever, auto_return)
    for role, name, sign, plus_deg, minus_deg, release_deg, auto_return in LEVERS:
        levers.append((role, name, getattr(port, name), sign,
                       Lever(plus_deg, minus_deg, release_deg, REARM_DEG), auto_return))
    modes = Modes(INVENTORY_CLOSE_HOLD_MS)
    spring_on = TRIGGER_SPRING
    spring_duty_now = 0
    returning = {}  # port name -> time the auto-return started
    light_matrix.show_image(light_matrix.IMAGE_HAPPY)

    last_tilt = None
    last_tilt_ms = last_beat_ms = time.ticks_ms()
    force_down = left_down = right_down = False
    last_angle = {}
    print("READY")

    while True:
        now = time.ticks_ms()

        for role, name, p, sign, lever, auto_return in levers:
            raw = motor.relative_position(p)
            event = lever.update(sign * raw)
            if event is None:
                pass
            elif event == "+" or event == "-":
                if name in returning:  # pressed again while driving back: let go of it
                    motor.stop(p, stop=motor.COAST)
                    del returning[name]
                lever.ranged_press = role == "trigger" and event == "+" and modes.ranged()
                print("M", name, event)
            else:
                print("M", name, event)
                # The trigger's spring brings a non-ranged pull back by itself.
                springs_back = role == "trigger" and spring_on and not lever.ranged_press
                if event == "0" and auto_return and not springs_back:
                    # Not awaited: the motor drives back while this loop keeps running.
                    motor.run_to_relative_position(p, 0, RETURN_SPEED, stop=motor.COAST)
                    returning[name] = now
            if event:
                modes.lever_event(role, event, now)
            if role == "trigger" and modes.tick(now, time.ticks_diff):
                print("M", name, "C")
            if role == "trigger" and spring_on and name not in returning:
                duty = 0 if trigger_free(modes, lever, sign * raw) else spring_duty(raw)
                if duty and abs(raw) > TRIGGER_SPRING_LIMIT_DEG:
                    spring_on, duty = False, 0  # pushing it the wrong way? stop for safety
                    print("W trigger spring off: the trigger went past", TRIGGER_SPRING_LIMIT_DEG, "degrees")
                if duty != spring_duty_now:
                    if duty:
                        motor.set_duty_cycle(p, duty)
                    else:
                        motor.stop(p, stop=motor.COAST)
                    spring_duty_now = duty
        for name in list(returning):
            p = getattr(port, name)
            if (abs(motor.relative_position(p)) <= 3
                    or time.ticks_diff(now, returning[name]) > RETURN_TIMEOUT_MS):
                motor.stop(p, stop=motor.COAST)
                del returning[name]
                spring_duty_now = 0

        for name, p in motors:
            angle = motor.relative_position(p)
            if abs(angle - last_angle.get(name, 9999)) >= ANGLE_REPORT_STEP:
                print("A", name, angle)
                last_angle[name] = angle

        force = force_sensor.force(force_port)
        if not force_down and force >= FORCE_PRESS:
            force_down = True
            print("F 1")
        elif force_down and force < FORCE_RELEASE:
            force_down = False
            print("F 0")

        left = bool(button.pressed(button.LEFT))
        right = bool(button.pressed(button.RIGHT))
        if left != left_down:
            left_down = left
            print("B L", 1 if left else 0)
        if right != right_down:
            right_down = right
            print("B R", 1 if right else 0)

        if time.ticks_diff(now, last_tilt_ms) >= GYRO_REPORT_MS:
            _, pitch, roll = motion_sensor.tilt_angles()  # tenths of a degree
            tilt = (int(pitch / 10), int(roll / 10))
            if tilt != last_tilt:
                print("T", tilt[0], tilt[1])
                last_tilt, last_tilt_ms = tilt, now

        if time.ticks_diff(now, last_beat_ms) >= HEARTBEAT_MS:
            print("H")
            last_beat_ms = now

        await runloop.sleep_ms(HUB_LOOP_MS)


runloop.run(main())

# V2.1 hub program: runs ON the SPIKE Prime hub (MicroPython, LEGO firmware).
# bridge.py fills in the CONFIG block from config.py, uploads this file and starts it.
#
# Start it with the motorised buttons and back triggers at rest: that is angle 0.
#
# Printed lines (one event per line):
#   READY                  program started
#   H                      heartbeat, every HEARTBEAT_MS
#   T <pitch> <roll>       hub tilt in degrees, when it changes
#   F 1 / F 0              force sensor pressed / released
#   B L 1 / B L 0          left hub button down / up   (B R ... for the right one)
#   D <mm>                 distance sensor reading when it changes (-1 = sees nothing)
#   A <port> <angle>       a motor's angle from rest (raw motor direction), when it changes
#   M <port> +             button pressed / trigger pulled
#   M <port> 0             released (moved back RELEASE degrees from its deepest point)
#   M <port> Z             back at rest: it can fire again
#   W <text>               warning (for example a spring switched itself off)
# bridge.py sets each lever's SIGN from the calibration, so "+" is always the pressed
# direction. The press and release logic runs here, so nothing waits for Bluetooth.
#
# Levers with a spring (the motorised buttons) are pushed back towards rest by their
# motor, harder the further they are pressed, so they pop back up when you let go.
import runloop

# <config> (replaced by bridge.py)
FORCE_PORT = "C"
DISTANCE_PORT = "D"
MOTOR_PORTS = ["E", "A", "B", "F"]
# role, port, sign, press_deg, release_deg, auto_return, spring
LEVERS = []
REARM_DEG = 10
RETURN_SPEED = 600
RETURN_TIMEOUT_MS = 1500
FORCE_PRESS = 10
FORCE_RELEASE = 5
GYRO_REPORT_MS = 20
DISTANCE_REPORT_MS = 30
DISTANCE_REPORT_STEP_MM = 4
HUB_LOOP_MS = 10
HEARTBEAT_MS = 500
SPRING_STRENGTH = 100
SPRING_MAX = 3000
SPRING_DEADBAND_DEG = 2
SPRING_LIMIT_DEG = 90
# </config>

ANGLE_REPORT_STEP = 2  # degrees


class Lever:
    """Edge detection for one motor used as a button or trigger. update() takes the signed angle."""

    REST, PRESSED, REARMING = 0, 1, 2

    def __init__(self, press_deg, release_deg, rearm_deg):
        self.press_deg = press_deg
        self.release_deg = release_deg
        self.rearm_deg = rearm_deg
        self.state = Lever.REST
        self.peak = 0

    def update(self, angle):
        """Returns "+", "0", "Z" or None."""
        if self.state == Lever.REST:
            if angle >= self.press_deg:
                self.state, self.peak = Lever.PRESSED, angle
                return "+"
        elif self.state == Lever.REARMING:
            if abs(angle) <= self.rearm_deg:
                self.state = Lever.REST
                return "Z"
        elif angle > self.peak:
            self.peak = angle
        elif angle <= self.peak - self.release_deg:
            self.state = Lever.REARMING
            return "0"
        return None


def spring_duty(raw_angle):
    """Motor power (-10000..10000) that pushes a lever back to rest, like a spring."""
    if abs(raw_angle) <= SPRING_DEADBAND_DEG:
        return 0
    duty = SPRING_STRENGTH * raw_angle
    return -max(-SPRING_MAX, min(SPRING_MAX, duty))


def distance_changed(new, old):
    """Report a distance reading? Always when the sensor starts or stops seeing something."""
    if old is None or (new < 0) != (old < 0):
        return True
    return new >= 0 and abs(new - old) >= DISTANCE_REPORT_STEP_MM


async def main():
    from hub import port, button, motion_sensor, light_matrix
    import distance_sensor
    import force_sensor
    import motor
    import time

    force_port = getattr(port, FORCE_PORT)
    distance_port = getattr(port, DISTANCE_PORT)
    motors = [(name, getattr(port, name)) for name in MOTOR_PORTS]
    for _, p in motors:
        motor.stop(p, stop=motor.COAST)
        motor.reset_relative_position(p, 0)
    levers = []  # (name, port, sign, Lever, auto_return)
    springs = set()  # port names of the levers with a spring
    for role, name, sign, press_deg, release_deg, auto_return, spring in LEVERS:
        levers.append((name, getattr(port, name), sign, Lever(press_deg, release_deg, REARM_DEG), auto_return))
        if spring:
            springs.add(name)
    returning = {}  # port name -> time the auto-return started
    duty_now = {}  # port name -> spring power being applied
    light_matrix.show_image(light_matrix.IMAGE_HAPPY)

    last_tilt = last_distance = None
    last_tilt_ms = last_distance_ms = last_beat_ms = time.ticks_ms()
    force_down = left_down = right_down = False
    last_angle = {}
    print("READY")

    while True:
        now = time.ticks_ms()

        for name, p, sign, lever, auto_return in levers:
            raw = motor.relative_position(p)
            event = lever.update(sign * raw)
            if name in springs and name not in returning:
                duty = spring_duty(raw)
                if duty and abs(raw) > SPRING_LIMIT_DEG:
                    springs.discard(name)  # pushing it the wrong way? stop for safety
                    duty = 0
                    print("W spring on", name, "off: it went past", SPRING_LIMIT_DEG, "degrees")
                if duty != duty_now.get(name, 0):
                    if duty:
                        motor.set_duty_cycle(p, duty)
                    else:
                        motor.stop(p, stop=motor.COAST)
                    duty_now[name] = duty
            if event is None:
                continue
            print("M", name, event)
            if event == "+" and name in returning:  # pressed again while driving back: let go
                motor.stop(p, stop=motor.COAST)
                del returning[name]
            elif event == "0" and auto_return:
                # Not awaited: the motor drives back while this loop keeps running.
                motor.run_to_relative_position(p, 0, RETURN_SPEED, stop=motor.COAST)
                returning[name] = now
                duty_now[name] = 0  # the spring rests while it drives back
        for name in list(returning):
            p = getattr(port, name)
            if (abs(motor.relative_position(p)) <= 3
                    or time.ticks_diff(now, returning[name]) > RETURN_TIMEOUT_MS):
                motor.stop(p, stop=motor.COAST)
                del returning[name]

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

        if time.ticks_diff(now, last_distance_ms) >= DISTANCE_REPORT_MS:
            last_distance_ms = now
            distance = distance_sensor.distance(distance_port)
            if distance_changed(distance, last_distance):
                print("D", distance)
                last_distance = distance

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

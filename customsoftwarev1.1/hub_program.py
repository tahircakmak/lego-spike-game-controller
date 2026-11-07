# Custom software V1.1 hub program for HubOS 3 (SPIKE App 3 firmware): runs ON the hub.
# app.py fills in the CONFIG block for whatever is plugged in, uploads this file and starts it.
# It knows nothing about Xbox buttons: it only reports readings, and the computer decides.
# hub_program_legacy.py is the same program for HubOS 2 (MINDSTORMS Robot Inventor firmware).
#
# Printed lines (one reading per line, only when something changed):
#   READY                    program started
#   H                        heartbeat, every HEARTBEAT_MS
#   A <port> <angle>         motor angle: absolute position (-180..179) or degrees turned since start
#   F <port> <force>         force sensor, 0-100
#   D <port> <mm>            distance sensor (-1 = sees nothing)
#   C <port> <color> <refl>  colour sensor: colour number and reflected light (0-100)
#   T <yaw> <pitch> <roll>   hub tilt in degrees
#   B L 1 / B L 0            left hub button down / up   (B R ... for the right one)
#   W <text>                 warning (for example a spring switched itself off)
#   R <port> 1 / R <port> 0  return to rest started / ended: the hub is moving that motor, not a hand
#
# Each motor can push itself back to its rest position in one of two ways:
#   spring          pushes back all the time, harder the further it is moved (motorised buttons)
#   return to rest  once you stop moving it, it slowly turns back to rest at a set speed, and
#                   lets go when you move it again (steering wheels, levers)
import runloop

# <config> (replaced by app.py)
# port, absolute (True) or relative (False), rest position (None = where it is at the start),
# spring, spring strength (power per degree), spring maximum power (power runs 0-10000),
# return to rest, return speed (degrees per second)
MOTORS = []
FORCE_PORTS = []
DISTANCE_PORTS = []
COLOR_PORTS = []
GYRO_REPORT_MS = 20
DISTANCE_REPORT_MS = 30
HUB_LOOP_MS = 10
HEARTBEAT_MS = 500
SPRING_DEADBAND_DEG = 2
SPRING_LIMIT_DEG = 90
RETURN_DELAY_MS = 250
RETURN_DEADBAND_DEG = 3
RETURN_MAX_POWER = 5000
# </config>

ANGLE_REPORT_STEP = 2     # degrees
FORCE_REPORT_STEP = 2     # force units
DISTANCE_REPORT_STEP = 4  # mm
REFLECTION_STEP = 3       # percent

STILL_DPS = 15         # slower than this counts as "stopped"
PUSHED_DPS = 60        # moved away from rest faster than this while returning: a hand took over
MIN_RETURN_DPS = 25    # the slowest it creeps back, right next to rest
SLOWDOWN = 3           # target speed near rest: this many deg/s per degree still to go
RETURN_GAIN = 35       # power added per (deg/s too slow) per second
RETURN_START_POWER = 1200
STALL_BOOST = 6000     # power added per second while it isn't turning yet (a real motor stuck at 900)
BRAKE_MS = 150         # brake this long on arriving at rest, so it doesn't overshoot, then let go
HELD_MS = 300          # pushing this long without it turning: a hand is holding it, so let go
HELD_RETRY_MS = 1000   # then leave it alone until it has been still this long (or moved and stopped again)


def wrap180(angle):
    return (angle + 180) % 360 - 180


def spring_duty(offset, strength, most):
    """Motor power (-10000..10000) that pushes a motor `offset` degrees from rest back, like a spring."""
    if abs(offset) <= SPRING_DEADBAND_DEG:
        return 0
    return -max(-most, min(most, strength * offset))


class ReturnToRest:
    """
    Once a motor is left still away from rest, drive it slowly back to rest.

    update() takes the motor's offset from rest (degrees) and its measured speed (deg/s) and
    returns the power to apply (-10000..10000, 0 = let it coast). The speed is controlled, so
    it creeps back at `speed` deg/s whatever the motor's load, slowing down near rest.
    Holding it still (for example a gas lever held down) lets go after HELD_MS: it doesn't fight
    you, and only tries again once it has been still for HELD_RETRY_MS. Moving it away again also
    lets go, and it waits until it is left still once more. On arriving at
    rest, `brake_ms` > 0 asks for the brake for a moment (so it stops there instead of coasting past).
    """

    def __init__(self, speed):
        self.speed = speed
        self.returning = False
        self.still_ms = 0
        self.duty = 0
        self.brake_ms = 0
        self.stalled_ms = 0

    def stop(self, arrived=False):
        self.returning = False
        self.still_ms = 0
        self.stalled_ms = 0
        self.duty = 0
        self.brake_ms = BRAKE_MS if arrived else 0
        return 0

    def update(self, offset, speed, dt_ms):
        if self.brake_ms > 0:
            self.brake_ms = max(0, self.brake_ms - dt_ms)
        if not self.returning:
            if abs(speed) < STILL_DPS and abs(offset) > RETURN_DEADBAND_DEG:
                self.still_ms += dt_ms
            else:
                self.still_ms = 0
            if self.still_ms < RETURN_DELAY_MS:
                return 0
            self.returning = True
            self.duty = RETURN_START_POWER if offset < 0 else -RETURN_START_POWER
        if abs(offset) <= RETURN_DEADBAND_DEG:
            return self.stop(arrived=True)
        toward = 1 if offset < 0 else -1
        if -toward * speed > PUSHED_DPS:
            return self.stop()
        target = toward * min(self.speed, max(MIN_RETURN_DPS, abs(offset) * SLOWDOWN))
        self.duty += RETURN_GAIN * (target - speed) * dt_ms / 1000
        if abs(speed) < STILL_DPS:
            self.stalled_ms += dt_ms
            if self.stalled_ms >= HELD_MS:  # still not turning: someone is holding it
                self.stop()
                self.still_ms = RETURN_DELAY_MS - HELD_RETRY_MS
                return 0
            self.duty += toward * STALL_BOOST * dt_ms / 1000  # a stiff motor: push harder until it breaks away
        else:
            self.stalled_ms = 0
        if self.duty * toward < 0:
            self.duty = 0  # never push away from rest
        self.duty = max(-RETURN_MAX_POWER, min(RETURN_MAX_POWER, self.duty))
        return int(self.duty)


def changed(new, old, step):
    """Report a reading? Always when it starts or stops being -1 (nothing seen)."""
    if old is None or (new < 0) != (old < 0):
        return True
    return abs(new - old) >= step


async def main():
    from hub import port, button, motion_sensor, light_matrix
    import color_sensor
    import distance_sensor
    import force_sensor
    import motor
    import time

    motors = []  # [name, port, absolute, rest, spring, strength, most, returner, last raw, speed]
    for name, absolute, rest, spring, strength, most, back, back_speed in MOTORS:
        p = getattr(port, name)
        motor.stop(p, stop=motor.COAST)
        if absolute:
            if rest is None:
                rest = motor.absolute_position(p)
        else:
            motor.reset_relative_position(p, 0)
            rest = 0 if rest is None else rest
        returner = ReturnToRest(back_speed) if back else None
        motors.append([name, p, absolute, rest, spring and not back, strength, most, returner, None, 0.0])
    light_matrix.show_image(light_matrix.IMAGE_HAPPY)

    last = {}
    duty_now = {}
    braked = {}
    was_returning = {}
    last_tilt = None
    last_loop_ms = last_distance_ms = last_tilt_ms = last_beat_ms = time.ticks_ms()
    left_down = right_down = False
    print("READY")

    while True:
        now = time.ticks_ms()
        dt = max(1, time.ticks_diff(now, last_loop_ms))
        last_loop_ms = now

        for m in motors:
            name, p, absolute, rest, spring, strength, most, returner, last_raw, speed = m
            raw = motor.absolute_position(p) if absolute else motor.relative_position(p)
            if name not in last or abs(raw - last[name]) >= ANGLE_REPORT_STEP:
                print("A", name, raw)
                last[name] = raw
            moved = 0 if last_raw is None else (wrap180(raw - last_raw) if absolute else raw - last_raw)
            speed = 0.7 * speed + 0.3 * moved * 1000 / dt  # deg/s, smoothed
            m[8], m[9] = raw, speed
            offset = wrap180(raw - rest) if absolute else raw - rest
            duty = 0
            if returner:
                duty = returner.update(offset, speed, dt)
                if returner.returning != was_returning.get(name, False):  # the computer ignores it meanwhile
                    was_returning[name] = returner.returning
                    print("R", name, 1 if returner.returning else 0)
                if returner.brake_ms > 0:  # just arrived at rest: brake for a moment
                    if not braked.get(name):
                        motor.stop(p, stop=motor.BRAKE)
                        braked[name], duty_now[name] = True, 0
                    continue
                if braked.get(name):  # the moment is over: let it turn freely again
                    motor.stop(p, stop=motor.COAST)
                    braked[name] = False
            elif spring:
                duty = spring_duty(offset, strength, most)
                if duty and abs(offset) > SPRING_LIMIT_DEG:
                    m[4] = False  # pushing it the wrong way? stop for safety
                    duty = 0
                    print("W spring on", name, "off: it went past", SPRING_LIMIT_DEG, "degrees")
            if duty != duty_now.get(name, 0):
                if duty:
                    motor.set_duty_cycle(p, duty)
                else:
                    motor.stop(p, stop=motor.COAST)
                duty_now[name] = duty

        for name in FORCE_PORTS:
            force = force_sensor.force(getattr(port, name))
            if changed(force, last.get(name), FORCE_REPORT_STEP) or (force == 0 and last.get(name) != 0):
                print("F", name, force)
                last[name] = force

        if time.ticks_diff(now, last_distance_ms) >= DISTANCE_REPORT_MS:
            last_distance_ms = now
            for name in DISTANCE_PORTS:
                distance = distance_sensor.distance(getattr(port, name))
                if changed(distance, last.get(name), DISTANCE_REPORT_STEP):
                    print("D", name, distance)
                    last[name] = distance
            for name in COLOR_PORTS:
                p = getattr(port, name)
                reading = (color_sensor.color(p), color_sensor.reflection(p))
                old = last.get(name)
                if old is None or reading[0] != old[0] or abs(reading[1] - old[1]) >= REFLECTION_STEP:
                    print("C", name, reading[0], reading[1])
                    last[name] = reading

        left = bool(button.pressed(button.LEFT))
        right = bool(button.pressed(button.RIGHT))
        if left != left_down:
            left_down = left
            print("B L", 1 if left else 0)
        if right != right_down:
            right_down = right
            print("B R", 1 if right else 0)

        if time.ticks_diff(now, last_tilt_ms) >= GYRO_REPORT_MS:
            yaw, pitch, roll = motion_sensor.tilt_angles()  # tenths of a degree
            tilt = (int(yaw / 10), int(pitch / 10), int(roll / 10))
            if tilt != last_tilt:
                print("T", tilt[0], tilt[1], tilt[2])
                last_tilt, last_tilt_ms = tilt, now

        if time.ticks_diff(now, last_beat_ms) >= HEARTBEAT_MS:
            print("H")
            last_beat_ms = now

        await runloop.sleep_ms(HUB_LOOP_MS)


runloop.run(main())

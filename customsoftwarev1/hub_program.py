# Custom software V1 hub program: runs ON the SPIKE Prime hub (MicroPython, LEGO firmware).
# app.py fills in the CONFIG block for whatever is plugged in, uploads this file and starts it.
# It knows nothing about Xbox buttons: it only reports readings, and the computer decides.
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
#
# Motors with a spring are pushed back towards their rest position by their own motor,
# harder the further they are moved, so motorised buttons pop back up when you let go.
import runloop

# <config> (replaced by app.py)
# port, absolute (True) or relative (False), rest position (None = where it is at the start),
# spring, spring strength (power per degree), spring maximum power (power runs 0-10000)
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
# </config>

ANGLE_REPORT_STEP = 2     # degrees
FORCE_REPORT_STEP = 2     # force units
DISTANCE_REPORT_STEP = 4  # mm
REFLECTION_STEP = 3       # percent


def wrap180(angle):
    return (angle + 180) % 360 - 180


def spring_duty(offset, strength, most):
    """Motor power (-10000..10000) that pushes a motor `offset` degrees from rest back, like a spring."""
    if abs(offset) <= SPRING_DEADBAND_DEG:
        return 0
    return -max(-most, min(most, strength * offset))


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

    motors = []  # [port name, port, absolute, rest, spring, strength, most]
    for name, absolute, rest, spring, strength, most in MOTORS:
        p = getattr(port, name)
        motor.stop(p, stop=motor.COAST)
        if absolute:
            if rest is None:
                rest = motor.absolute_position(p)
        else:
            motor.reset_relative_position(p, 0)
            rest = 0 if rest is None else rest
        motors.append([name, p, absolute, rest, spring, strength, most])
    light_matrix.show_image(light_matrix.IMAGE_HAPPY)

    last = {}
    duty_now = {}
    last_tilt = None
    last_distance_ms = last_tilt_ms = last_beat_ms = time.ticks_ms()
    left_down = right_down = False
    print("READY")

    while True:
        now = time.ticks_ms()

        for m in motors:
            name, p, absolute, rest, spring, strength, most = m
            raw = motor.absolute_position(p) if absolute else motor.relative_position(p)
            if name not in last or abs(raw - last[name]) >= ANGLE_REPORT_STEP:
                print("A", name, raw)
                last[name] = raw
            if spring:
                offset = wrap180(raw - rest) if absolute else raw - rest
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

# This file runs ON the SPIKE hub (MicroPython, LEGO firmware).
# bridge.py uploads and starts it, then reads what it prints over Bluetooth.
#
# Start it with the trigger (motor A) at its rest angle: that angle becomes 0.
#
# Printed events, one per line:
#   T <pitch> <roll>   hub tilt in degrees (only when it changes)
#   F 1 / F 0          force sensor pressed / released
#   P                  force sensor squeezed past POTION_FORCE (health potion)
#   B L / B R          left / right hub button pressed
#   D <angle>          artifact dial (motor F) absolute angle, -180..179
#   A <angle>          trigger angle away from rest
#   TP / TR / TZ       trigger pulled / pushed back (released) / back at rest
from hub import port, button, motion_sensor, light_matrix
import force_sensor
import motor
import runloop
import time

TRIGGER = port.A
DIAL = port.F
FORCE_PORT = port.D  # bridge.py fills in the port it detected

PULL_THRESHOLD = 45   # degrees from rest that count as "pulled"
RELEASE_DELTA = 10    # pushing back this far from the deepest point = release
RETURN_SPEED = 600    # deg/s the motor drives the trigger back to rest
RETURN_TIMEOUT_MS = 1500
POTION_FORCE = 90     # squeeze harder than this (0-100 %) to drink a potion
POTION_RESET = 70     # ...and ease off below this before it can happen again
TILT_EVERY_MS = 50
LOOP_MS = 10


def angle_diff(a, b):
    return (a - b + 180) % 360 - 180


async def main():
    motor.reset_relative_position(TRIGGER, 0)
    light_matrix.show_image(light_matrix.IMAGE_HAPPY)

    pulled = returning = False
    peak = 0
    return_start = 0
    last_tilt = None
    last_tilt_ms = 0
    last_force = last_left = last_right = False
    potion_sent = False
    last_dial = None
    last_trigger = 0
    print("READY")

    while True:
        now = time.ticks_ms()

        _, pitch, roll = motion_sensor.tilt_angles()  # tenths of a degree
        tilt = (int(pitch / 10), int(roll / 10))
        if tilt != last_tilt and time.ticks_diff(now, last_tilt_ms) >= TILT_EVERY_MS:
            print("T", tilt[0], tilt[1])
            last_tilt, last_tilt_ms = tilt, now

        force_percent = force_sensor.force(FORCE_PORT)
        force = force_sensor.pressed(FORCE_PORT) or force_percent >= 20
        if force != last_force:
            print("F", 1 if force else 0)
            last_force = force
        if force_percent >= POTION_FORCE and not potion_sent:
            print("P")
            potion_sent = True
        elif force_percent < POTION_RESET:
            potion_sent = False

        left = bool(button.pressed(button.LEFT))
        right = bool(button.pressed(button.RIGHT))
        if left and not last_left:
            print("B L")
        if right and not last_right:
            print("B R")
        last_left, last_right = left, right

        dial = motor.absolute_position(DIAL)
        if last_dial is None or abs(angle_diff(dial, last_dial)) >= 3:
            print("D", dial)
            last_dial = dial

        trigger = abs(motor.relative_position(TRIGGER))
        if returning:
            if trigger <= 5 or time.ticks_diff(now, return_start) > RETURN_TIMEOUT_MS:
                motor.stop(TRIGGER, stop=motor.COAST)
                returning = False
                print("TZ")
        elif not pulled:
            if trigger >= PULL_THRESHOLD:
                pulled, peak = True, trigger
                print("TP")
        else:
            peak = max(peak, trigger)
            if trigger <= peak - RELEASE_DELTA:
                pulled = False
                print("TR")
                # Not awaited: the motor drives back while this loop keeps running.
                motor.run_to_relative_position(TRIGGER, 0, RETURN_SPEED, stop=motor.COAST)
                returning, return_start = True, now
        if abs(trigger - last_trigger) >= 3:
            print("A", trigger)
            last_trigger = trigger

        await runloop.sleep_ms(LOOP_MS)


runloop.run(main())

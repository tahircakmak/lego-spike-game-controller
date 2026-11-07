# Custom software V1.1 hub program for HubOS 2: runs ON the hub.
# HubOS 2 is the MINDSTORMS Robot Inventor (51515) firmware, and the older SPIKE Prime firmware
# (SPIKE App 2). It has a different MicroPython API from HubOS 3 (the low-level `hub` module),
# so this is hub_program.py rewritten for it. It prints exactly the same lines, so the computer
# side doesn't care which firmware is running. app.py fills in the CONFIG block and uploads it.
#
# Printed lines: see hub_program.py (READY, H, A, F, D, C, T, B, W, R).
import hub
import utime

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


def scale(mode, kind):
    """A function turning this mode's raw values into its "pct" or "si" range (from Port.info())."""
    (r0, r1), (t0, t1) = mode["raw"], mode[kind]
    if r1 == r0:
        return lambda v: v
    return lambda v: None if v is None else t0 + (v - r0) * (t1 - t0) / (r1 - r0)


def sensor(name, wanted):
    """Put the device on port `name` into the modes called `wanted`. Returns (name, device, scalers) or None."""
    p = getattr(hub.port, name)
    if p.device is None:
        print("W port", name, "has nothing plugged in")
        return None
    info = p.info()
    modes = info.get("modes") or []
    picked = []
    for mode_name, kind in wanted:
        index = None
        for i, mode in enumerate(modes):
            if mode.get("name") == mode_name:
                index = i
        if index is None:
            print("W port", name, "has no", mode_name, "mode: reading mode 0")
            index = 0
        picked.append((index, scale(modes[index], kind) if index < len(modes) and kind else (lambda v: v)))
    p.device.mode([(index, 0) for index, _ in picked])
    return name, p.device, [f for _, f in picked]


def read(device, scalers):
    values = device.get(0)  # raw values, scaled here
    return [f(values[i] if i < len(values) else None) for i, f in enumerate(scalers)]


def set_power(m, duty):
    if duty:
        m.pwm(max(-100, min(100, int(duty / 100))))  # HubOS 2 motor power runs -100..100
    else:
        m.float()


def main():
    motors = []  # [name, motor, absolute, rest, spring, strength, most, returner, last raw, speed]
    for name, absolute, rest, spring, strength, most, back, back_speed in MOTORS:
        m = getattr(hub.port, name).motor
        if m is None:
            print("W port", name, "has no motor")
            continue
        m.mode([(1, 0), (2, 0), (3, 0), (0, 0)])  # speed %, relative position, absolute position, power
        m.float()
        if absolute:
            if rest is None:
                rest = m.get()[2]
        else:
            m.preset(0)
            rest = 0 if rest is None else rest
        returner = ReturnToRest(back_speed) if back else None
        motors.append([name, m, absolute, rest, spring and not back, strength, most, returner, None, 0.0])
    forces = [s for s in (sensor(name, [("FORCE", "pct")]) for name in FORCE_PORTS) if s]
    distances = [s for s in (sensor(name, [("DISTL", "si")]) for name in DISTANCE_PORTS) if s]  # cm
    colors = [s for s in (sensor(name, [("COLOR", None), ("REFLT", "pct")]) for name in COLOR_PORTS) if s]
    try:
        hub.display.show(hub.Image.HAPPY)
    except Exception:
        pass

    last = {}
    duty_now = {}
    braked = {}
    was_returning = {}
    last_tilt = None
    last_loop_ms = last_distance_ms = last_tilt_ms = last_beat_ms = utime.ticks_ms()
    left_down = right_down = False
    print("READY")

    while True:
        now = utime.ticks_ms()
        dt = max(1, utime.ticks_diff(now, last_loop_ms))
        last_loop_ms = now

        for mm in motors:
            name, m, absolute, rest, spring, strength, most, returner, last_raw, speed = mm
            _, relative, absolute_position, _ = m.get()
            raw = absolute_position if absolute else relative
            if name not in last or abs(raw - last[name]) >= ANGLE_REPORT_STEP:
                print("A", name, raw)
                last[name] = raw
            moved = 0 if last_raw is None else (wrap180(raw - last_raw) if absolute else raw - last_raw)
            speed = 0.7 * speed + 0.3 * moved * 1000 / dt  # deg/s, smoothed
            mm[8], mm[9] = raw, speed
            offset = wrap180(raw - rest) if absolute else raw - rest
            duty = 0
            if returner:
                duty = returner.update(offset, speed, dt)
                if returner.returning != was_returning.get(name, False):  # the computer ignores it meanwhile
                    was_returning[name] = returner.returning
                    print("R", name, 1 if returner.returning else 0)
                if returner.brake_ms > 0:  # just arrived at rest: brake for a moment
                    if not braked.get(name):
                        m.brake()
                        braked[name], duty_now[name] = True, 0
                    continue
                if braked.get(name):  # the moment is over: let it turn freely again
                    m.float()
                    braked[name] = False
            elif spring:
                duty = spring_duty(offset, strength, most)
                if duty and abs(offset) > SPRING_LIMIT_DEG:
                    mm[4] = False  # pushing it the wrong way? stop for safety
                    duty = 0
                    print("W spring on", name, "off: it went past", SPRING_LIMIT_DEG, "degrees")
            if duty != duty_now.get(name, 0):
                set_power(m, duty)
                duty_now[name] = duty

        for name, device, scalers in forces:
            force = int(read(device, scalers)[0] or 0)
            if changed(force, last.get(name), FORCE_REPORT_STEP) or (force == 0 and last.get(name) != 0):
                print("F", name, force)
                last[name] = force

        if utime.ticks_diff(now, last_distance_ms) >= DISTANCE_REPORT_MS:
            last_distance_ms = now
            for name, device, scalers in distances:
                cm = read(device, scalers)[0]
                distance = -1 if cm is None or cm < 0 else int(cm * 10)
                if changed(distance, last.get(name), DISTANCE_REPORT_STEP):
                    print("D", name, distance)
                    last[name] = distance
            for name, device, scalers in colors:
                color, reflection = read(device, scalers)
                reading = (-1 if color is None else int(color), int(reflection or 0))
                old = last.get(name)
                if old is None or reading[0] != old[0] or abs(reading[1] - old[1]) >= REFLECTION_STEP:
                    print("C", name, reading[0], reading[1])
                    last[name] = reading

        left = hub.button.left.is_pressed()
        right = hub.button.right.is_pressed()
        if left != left_down:
            left_down = left
            print("B L", 1 if left else 0)
        if right != right_down:
            right_down = right
            print("B R", 1 if right else 0)

        if utime.ticks_diff(now, last_tilt_ms) >= GYRO_REPORT_MS:
            tilt = tuple(int(v) for v in hub.motion.yaw_pitch_roll())  # degrees
            if tilt != last_tilt:
                print("T", tilt[0], tilt[1], tilt[2])
                last_tilt, last_tilt_ms = tilt, now

        if utime.ticks_diff(now, last_beat_ms) >= HEARTBEAT_MS:
            print("H")
            last_beat_ms = now

        utime.sleep_ms(HUB_LOOP_MS)


main()

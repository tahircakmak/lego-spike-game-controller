"""
V2.1 controller configuration: ports, every tuning value and every Xbox mapping live here.

Edit this file, then restart `bridge.py`. The hub-side values (motor, force and distance
settings) are copied into hub_program.py automatically when it is uploaded.

Xbox button names (W3C "standard" gamepad layout, which Chrome and Xbox Remote Play use):
    a b x y lb rb lt rt view menu ls rs dpad_up dpad_down dpad_left dpad_right xbox
"""

# --------------------------------------------------------------------------------------
# Ports (where each part is plugged into the hub)
# --------------------------------------------------------------------------------------
BUTTON_PORTS = ["E", "A"]    # the two motorised buttons (small or medium angular motors);
                             # calibration finds out which one is left and which is right
RIGHT_TRIGGER_PORT = "B"     # large angular motor, right back trigger
LEFT_TRIGGER_PORT = "F"      # large angular motor, left back trigger
FORCE_PORT = "C"             # force sensor
DISTANCE_PORT = "D"          # distance sensor, next to the right trigger

# --------------------------------------------------------------------------------------
# Xbox mappings (Minecraft Dungeons II defaults: dungeons.tools/2/controls)
# --------------------------------------------------------------------------------------
BUTTONS = {
    "right_button": "x",      # motorised button on the right: melee, held while pressed
    "left_button": "a",       # motorised button on the left: jump, held while pressed
    "right_trigger": "rt",    # right back trigger: ranged attack, held while pulled
    "left_trigger": "lt",     # left back trigger: LT (health potion), held while pulled
    "force_sensor": "lb",     # force sensor: LB (forward dodge), held while pressed
    "hub_left": "dpad_up",    # left hub button: D-pad up (inventory), held while pressed
}
# The right hub button starts / stops walking (no Xbox button).
ARTIFACTS = ["y", "b", "rb"]  # hand over the distance sensor: all three artifacts at once

# --------------------------------------------------------------------------------------
# Motorised buttons and back triggers. Angles are degrees from the rest position the
# motor had when the hub program started.
# --------------------------------------------------------------------------------------
BUTTON_PRESS_DEG = 25        # pressed past this = pressed
BUTTON_RELEASE_DEG = 8       # moving back this far from the deepest point = released
TRIGGER_PULL_DEG = 20        # pulled past this = pulled
TRIGGER_RELEASE_DEG = 5      # easing back this far from the deepest point = released
AUTO_RETURN = True           # after a release the motor drives itself back to rest
BUTTON_SPRING = True         # the motorised buttons' motors push them back towards rest,
                             # like a real button's spring, so they pop up when let go
SPRING_STRENGTH = 100        # motor power per degree pressed (power runs 0-10000)
SPRING_MAX = 3000            # the spring never pushes harder than this
SPRING_DEADBAND_DEG = 2      # closer to rest than this: no force (no buzzing)
SPRING_LIMIT_DEG = 90        # safety: past this the spring switches itself off
REARM_DEG = 10               # after a release, a motor must come back within this of rest
                             # before it can fire again (no repeats while it is still out)
RETURN_SPEED = 600           # deg/s for the auto-return
RETURN_TIMEOUT_MS = 1500     # give up driving back (coast) after this long

# --------------------------------------------------------------------------------------
# Force sensor (0-100, the hub's force reading)
# --------------------------------------------------------------------------------------
FORCE_PRESS = 10             # pressed at or above this
FORCE_RELEASE = 5            # released below this (hysteresis: no flicker in between)

# --------------------------------------------------------------------------------------
# Distance sensor: artifacts
# --------------------------------------------------------------------------------------
ARTIFACT_NEAR_MM = None      # hand closer than this = use the artifacts. None: use the
                             # value calibration measured with your hand over the sensor
ARTIFACT_FAR_EXTRA_MM = 25   # the hand must move this much further away before it counts
                             # as gone again (hysteresis)
ARTIFACT_COOLDOWN_S = 0.8    # after using the artifacts, wait at least this long before
                             # the next use (no double use while taking the hand away)
DISTANCE_LOST_S = 0.4        # the sensor sees nothing for this long = the hand is gone
DISTANCE_REPORT_MS = 30      # hub reads the distance this often
DISTANCE_REPORT_STEP_MM = 4  # and reports it when it changed at least this much

# --------------------------------------------------------------------------------------
# Walking and steering with the hub gyro
# --------------------------------------------------------------------------------------
# The right hub button turns walking on and off. While walking the player keeps going,
# and tilting the controller left / right (like a steering wheel) turns the walking
# direction. The more you tilt, the faster it turns.
STEER_DEADZONE_DEG = 6       # tilts smaller than this don't turn (hold it roughly level)
STEER_FULL_DEG = 30          # tilt this far (or more) for the fastest turn
STEER_TURN_RATE = 180        # deg/s the walking direction turns at a full tilt
MOVE_SPEED = 1.0             # left stick length while walking, 0-1
START_HEADING = 0            # initial walking direction, deg clockwise from screen-up
GYRO_REPORT_MS = 20          # hub sends tilt at most this often

# --------------------------------------------------------------------------------------
# Timing
# --------------------------------------------------------------------------------------
MIN_PRESS_S = 0.1            # every virtual press lasts at least this long, so the
                             # Remote Play page (polling ~60x a second) never misses one
HUB_LOOP_MS = 10             # hub program loop period
HEARTBEAT_MS = 500           # hub says "still here" this often
WATCHDOG_S = 2.0             # nothing from the hub for this long -> release everything

# --------------------------------------------------------------------------------------
# Chrome / Xbox Remote Play
# --------------------------------------------------------------------------------------
REMOTE_PLAY_URL = "https://www.xbox.com/play/consoles"

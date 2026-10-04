"""
V2 controller configuration: every tuning value and every Xbox mapping lives here.

Edit this file, then restart `bridge.py`. The hub-side values (motor and force
thresholds) are copied into hub_program.py automatically when it is uploaded.

Xbox button names (W3C "standard" gamepad layout, which Chrome and Xbox Remote Play use):
    a b x y lb rb lt rt view menu ls rs dpad_up dpad_down dpad_left dpad_right xbox
Special action names (in the mapping tables below):
    "rs_flick"   push the right stick briefly towards the player's facing direction
                 (the dodge roll in Minecraft Dungeons is on the right stick)
    None         do nothing
"""

# --------------------------------------------------------------------------------------
# Gyro (hub tilt) gestures, in degrees from the calibrated level position
# --------------------------------------------------------------------------------------
GYRO_THRESHOLD_DEG = {   # tilt past this to fire the gesture once
    "forward": 20,
    "backward": 20,
    "left": 20,
    "right": 20,
}
GYRO_DEADZONE_DEG = 8        # back inside this = neutral; required before the next gesture
GYRO_NEUTRAL_HOLD_S = 0.12   # must stay neutral this long to re-arm (stops snap-back overshoot
                             # from firing the opposite gesture)
GYRO_REPORT_MS = 20          # hub sends tilt at most this often (lower = less latency, more Bluetooth)

# --------------------------------------------------------------------------------------
# Motorised buttons (two small angular motors) and the shoulder triggers (one large motor).
# Angles are degrees from the rest position the motor had when the hub program started.
# "Press" = a normal press of a motorised button; "back" = pushing it backward.
# --------------------------------------------------------------------------------------
BUTTON_PRESS_DEG = 25        # normal press past this = pressed
BUTTON_BACK_DEG = 25         # pushed backward past this = backward action
BUTTON_RELEASE_DEG = 8       # moving back this far from the deepest point = released
BUTTON_AUTO_RETURN = True    # after a release the motor drives the button back to rest

TRIGGER_PULL_DEG = 20        # left or right shoulder trigger pulled past this = pulled
TRIGGER_RELEASE_DEG = 5      # moving back this far from the deepest point = released
                             # (this is the 5-degree ranged-attack release)
TRIGGER_AUTO_RETURN = True   # after a ranged release the motor drives the triggers back to rest

# When the shoulder triggers are NOT used for a ranged attack (MOVING, INVENTORY, or the
# left trigger while STANDING), the large motor works as a spring: it pushes back harder
# the further a trigger is pulled, and returns it to rest as soon as it is let go.
TRIGGER_SPRING = True
TRIGGER_SPRING_STRENGTH = 120   # motor power per degree pulled (power runs 0-10000)
TRIGGER_SPRING_MAX = 4000       # the spring never pushes harder than this
TRIGGER_SPRING_DEADBAND_DEG = 2  # closer to rest than this: no force (no buzzing)
TRIGGER_SPRING_LIMIT_DEG = 150  # safety: past this the spring switches itself off

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
# Timing
# --------------------------------------------------------------------------------------
MIN_PRESS_S = 0.1            # every virtual press lasts at least this long, so the
                             # Remote Play page (polling ~60x a second) never misses one
ROLL_FLICK_S = 0.1           # how long "rs_flick" holds the right stick
HUB_LOOP_MS = 10             # hub program loop period
HEARTBEAT_MS = 500           # hub says "still here" this often
WATCHDOG_S = 2.0             # nothing from the hub for this long -> release everything

# --------------------------------------------------------------------------------------
# Movement (GAMEPLAY + MOVING): the shoulder triggers steer
# --------------------------------------------------------------------------------------
STEER_STYLE = "turn"         # "turn": the player walks continuously and the triggers turn
                             #         the walking direction left / right (like steering)
                             # "strafe": the triggers push the left stick straight left /
                             #         right; no trigger = stand still
STEER_TURN_RATE = 120        # deg/s the walking direction turns while a trigger is held ("turn")
MOVE_SPEED = 1.0             # left stick length while moving, 0-1
START_HEADING = 0            # initial walking direction, deg clockwise from screen-up

# --------------------------------------------------------------------------------------
# Xbox mappings. Held inputs stay down while the physical input is held.
# --------------------------------------------------------------------------------------
GAMEPLAY = {
    "force_sensor": "x",          # melee, held while pressed
    "left_button_press": "lt",    # health potion
    "left_button_back": "dpad_up",  # open inventory (a tap: holding opens the mini overlay)
    "right_button_press": "a",    # jump
    # right_button_back toggles MOVING / STANDING (no Xbox button)
    "ranged": "rt",               # STANDING: held while the right shoulder trigger is pulled
    "hub_left": "view",           # map
    "hub_right": "menu",          # options
    "gyro_forward": "lb",         # roll / dodge (the controller's custom roll mapping)
    "gyro_backward": "y",
    "gyro_left": "b",
    "gyro_right": "rb",
}

INVENTORY = {
    "hub_left": "dpad_up",        # selected slot up
    "hub_right": "dpad_down",     # selected slot down
    "trigger_left": "dpad_left",  # selected slot left (short pull)
    "trigger_right": "dpad_right",  # selected slot right (short pull)
    "gyro_forward": "y",
    "gyro_left": "x",
    "gyro_right": "b",
    "gyro_backward": None,
    "force_sensor": "a",          # held while pressed
    "close": "b",                 # sent when the inventory is closed
}
INVENTORY_CLOSE_HOLD_S = 0.8      # hold a shoulder trigger this long to close the inventory
                                  # (a short pull only moves the selection)

# --------------------------------------------------------------------------------------
# Chrome / Xbox Remote Play
# --------------------------------------------------------------------------------------
REMOTE_PLAY_URL = "https://www.xbox.com/play/consoles"

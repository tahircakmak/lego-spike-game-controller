# How it works (V2)

Technical documentation for the V2 SPIKE controller: how a pull on a LEGO trigger ends up as a held RT on an Xbox in another room.

For setup and everyday use, see [README.md](README.md).

## Contents

1. [The big picture](#1-the-big-picture)
2. [On the hub: `hub_program.py`](#2-on-the-hub-hub_programpy)
3. [The virtual Xbox controller in Chrome](#3-the-virtual-xbox-controller-in-chrome)
4. [The state machine](#4-the-state-machine)
5. [One motor, two triggers](#5-one-motor-two-triggers)
6. [Ranged attack and the 5° release](#6-ranged-attack-and-the-5-release)
7. [Trigger spring-back](#7-trigger-spring-back)
8. [Force sensor, gyro and motorised buttons](#8-force-sensor-gyro-and-motorised-buttons)
9. [Closing the inventory with a trigger](#9-closing-the-inventory-with-a-trigger)
10. [No stuck or repeated inputs](#10-no-stuck-or-repeated-inputs)
11. [File map](#11-file-map)

---

## 1. The big picture

```
LEGO SPIKE Prime ──Bluetooth──▶ bridge.py ──▶ ControllerLogic ──▶ VirtualGamepad ──▶ Chrome Gamepad API ──▶ Xbox Remote Play ──▶ Xbox
 (hub_program.py)               (lines)       (state machine)     (buttons, sticks)   navigator.getGamepads()
```

- **The hub** runs `hub_program.py` under LEGO's normal firmware. It reads every sensor, works out lever presses and releases, drives the motors (auto-return and trigger spring), and `print()`s short event lines.
- **`bridge.py`** connects over Bluetooth Low Energy with the `spike/` client (the same one V1 uses). It finds the ports, calibrates, uploads `hub_program.py` with the values from `config.py` filled in, and turns each line into a `ControllerLogic` call.
- **`ControllerLogic`** (`controller.py`) holds the rules: GAMEPLAY / INVENTORY and MOVING / STANDING. It sets buttons and sticks on a `VirtualGamepad`.
- **`ChromeGamepad`** (`gamepad.py`) shows that pad to the Xbox Remote Play page as an Xbox controller.

No keyboard or mouse emulation is used anywhere.

The main loop wakes as soon as a line arrives from the hub, or every 10 ms for timed things like steering and the minimum press length.

## 2. On the hub: `hub_program.py`

`bridge.py` replaces the `# <config>` block at the top with values from `config.py` and the calibration, uploads the file into a program slot and starts it. The loop runs every 10 ms (`HUB_LOOP_MS`) and **only prints when something changes**.

| Line | Meaning |
|---|---|
| `READY` | program started |
| `H` | heartbeat, every 0.5 s |
| `T <pitch> <roll>` | hub tilt in degrees |
| `F 1` / `F 0` | force sensor pressed / released |
| `B L 1` / `B L 0` | left hub button down / up (`B R …` for the right one) |
| `A <port> <angle>` | a motor's angle from rest, in the motor's own direction |
| `M <port> +` | lever pressed / pulled in its "+" direction |
| `M <port> -` | lever pushed / pulled the other way |
| `M <port> 0` | lever released |
| `M <port> Z` | lever back at rest: it can fire again |
| `M <port> C` | trigger held 0.8 s in the inventory: close it |
| `W <text>` | warning, for example the trigger spring switched itself off |

Anything else, such as a MicroPython traceback, is printed by the bridge as `hub: …`, and every input is released.

### Lever logic

All three motors use the same `Lever` class. The angle is relative to where the motor was when the program started, and the calibrated sign makes "+" mean a normal press, or the right trigger.

```
REST ── angle ≥ press threshold ──▶ PRESSED(+)   print "+"
REST ── angle ≤ −back threshold ──▶ PRESSED(−)   print "-"
PRESSED ── deeper ──▶ remember the deepest point
PRESSED ── moved back RELEASE degrees from the deepest point ──▶ REARMING   print "0"
REARMING ── within REARM_DEG of rest ──▶ REST   print "Z"
```

A lever can only fire again after it has been released **and** has come back near rest, so holding it never repeats. The detection runs on the hub, so the release and the motor's reaction need no Bluetooth round trip.

## 3. The virtual Xbox controller in Chrome

`ChromeGamepad` starts your installed Chrome with [Playwright](https://playwright.dev/python/). Before any page script runs, it adds a small script to every page (`VIRTUAL_PAD_JS` in `gamepad.py`). That script makes `navigator.getGamepads()` return one controller:

- `id: "Xbox 360 Controller (XInput STANDARD GAMEPAD)"` and `mapping: "standard"`
- 17 buttons in the W3C standard order: A B X Y LB RB LT RT View Menu LS RS D-pad↑↓←→ Xbox. LT and RT carry a `value` from 0 to 1.
- 4 axes: left stick x/y and right stick x/y, with up = −1

When something changes, the bridge sends the pad's snapshot to the page through the DevTools protocol. Remote Play reads `navigator.getGamepads()` every frame and forwards the controller to the Xbox, as it would a real one.

**Why not an OS-level virtual gamepad?** That was tried first. macOS only creates virtual HID devices (`IOHIDUserDevice` / `CoreHID`) for programs with Apple's restricted `com.apple.developer.hid.virtual.device` entitlement. Without it, creation fails; with a self-signed entitlement, macOS kills the program. A DriverKit extension needs the same Apple approval. So the virtual controller lives in the Gamepad API of the Chrome tab, which is exactly what Remote Play reads. V1 used the same mechanism. **Limitation:** only the Chrome window that `bridge.py` opens sees the controller.

## 4. The state machine

```
            left button pushed back (D-pad up tap)
 GAMEPLAY ──────────────────────────────────────────▶ INVENTORY
   │  ▲                                                   │
   │  └──────── either shoulder trigger held 0.8 s (B) ◀──┘
   │
   ├─ STANDING (start): right trigger = ranged (RT held); left trigger does nothing
   └─ MOVING:           player walks; left / right trigger steer
        (right button pushed back toggles STANDING ⇄ MOVING)
```

- `MOVING` starts **OFF**. Each push back of the right motorised button flips it.
- **Opening the inventory sets STANDING**, so the player doesn't walk off by themselves after it closes.
- The hub keeps its own copy of these modes (`Modes` in `hub_program.py`), driven by the same lever events, so it knows when a trigger pull is a ranged attack (section 7).

### Steering

With `STEER_STYLE = "turn"` (default), MOVING means the player walks continuously. The left stick points in a **walking direction** at `MOVE_SPEED`:

- Holding the left trigger turns the direction anticlockwise at `STEER_TURN_RATE` (120°/s).
- Holding the right trigger turns it clockwise.
- When you let go, the direction stays where it is.

That way two triggers can reach every direction in the isometric game. With `STEER_STYLE = "strafe"`, the triggers push the stick straight left or right instead, and the player stands still when no trigger is pulled.

Toggling MOVING while a trigger is pulled stops what that trigger was doing: steering stops, and RT is released. The trigger has to be pulled again to act in the new state.

## 5. One motor, two triggers

The two rear triggers are geared to **one** large motor and turn it in opposite directions. The hub reads one signed angle. During calibration you pull the right trigger, which fixes the sign so that **+ is the right trigger** and **− is the left trigger**:

- `+`: angle ≥ `TRIGGER_PULL_DEG` (20°), the right trigger is pulled
- `−`: angle ≤ −20°, the left trigger is pulled
- `0`: released

Only one trigger can be pulled at a time, because they share the motor.

## 6. Ranged attack and the 5° release

In GAMEPLAY + STANDING, pulling the right trigger past 20° holds RT (value 1.0). While the trigger stays pulled, the hub tracks its deepest angle. **As soon as the trigger has moved back `TRIGGER_RELEASE_DEG` (5°) from that deepest point, the hub sends `0` and RT is released.** Small jitter never reaches 5° below the deepest point, so RT is never pressed and released over and over while the trigger stays put.

```
STANDING + right trigger pulled   →  RT held
right trigger stays pulled        →  RT stays held
right trigger moves back ≥ 5°     →  RT released, motor drives the trigger back to rest
```

## 7. Trigger spring-back

Only a **ranged pull** stays where you leave it. In every other case, the large motor works as a **spring**:

- MOVING (steering)
- INVENTORY (slot moves and the close hold)
- the left trigger while STANDING

The further you pull, the harder it pushes back (`TRIGGER_SPRING_STRENGTH`, up to `TRIGGER_SPRING_MAX`). When you let go, it returns the trigger to rest. So steering lasts exactly as long as you hold a trigger, and a short pull in the inventory is one slot step.

After a ranged release, the hub drives the trigger back to rest at `RETURN_SPEED` (`TRIGGER_AUTO_RETURN`, like V1's trigger).

**Safety:** if the trigger goes past `TRIGGER_SPRING_LIMIT_DEG` (150°) while the spring is on, the spring switches itself off and the terminal shows a hub warning. Set `TRIGGER_SPRING = False` to turn the spring off completely.

## 8. Force sensor, gyro and motorised buttons

**Force sensor: X held.** The hub sends `F 1` at force ≥ `FORCE_PRESS` (10) and `F 0` below `FORCE_RELEASE` (5). The gap between the two stops flicker. X is down exactly from `F 1` to `F 0` (A in the inventory), with no repeats.

**Gyro gestures.** The calibration records level, forward and right, so the tilt can be turned into degrees forward/backward and right/left, however the hub is mounted. A gesture fires once, as a 0.1 s press, when the stronger direction passes `GYRO_THRESHOLD_DEG` (20°). Nothing more fires until the hub has been back inside `GYRO_DEADZONE_DEG` (8°) for `GYRO_NEUTRAL_HOLD_S` (0.12 s). That pause stops the snap back from overshooting into the opposite gesture. Starting with the hub tilted does nothing.

**Motorised buttons.**
- A normal press holds LT (left) or A (right) until the button is released.
- Pushing back is a one-shot action: inventory (left) or the MOVING toggle (right).
- After a release, the motor drives the button back to rest (`BUTTON_AUTO_RETURN`).

## 9. Closing the inventory with a trigger

The triggers also move the selection left and right, so not every pull may close the inventory:

- A **short pull** sends one D-pad left/right step straight away, and the spring brings the trigger back when you let go.
- **Holding either trigger against the spring for 0.8 s** (`INVENTORY_CLOSE_HOLD_S`) closes the inventory. The hub times this and sends `M <port> C`. The bridge sends B and switches to GAMEPLAY / STANDING.
- Letting go of the trigger afterwards does nothing, so closing never fires a ranged shot.

If the game's inventory ever closes some other way while the terminal still shows INVENTORY, a 0.8 s trigger hold brings the two back in step.

## 10. No stuck or repeated inputs

- **Edge-driven.** An action starts on a press or pull event and ends on its release. The hub only sends a new press after a release and a return near rest.
- **Owned buttons.** Each held Xbox button belongs to the physical input that pressed it, and its release lets go of exactly that button. Changing mode releases everything and centres the left stick. An input still held when the mode changes does nothing in the new mode until it's pressed again. So gameplay inputs never leak into the inventory, or the other way round.
- **No missed taps.** Every Xbox press lasts at least `MIN_PRESS_S` (0.1 s), longer than the page's polling interval.
- **Safety releases.** Everything is released in all of these cases:
  - the hub disconnects
  - the hub program stops or errors
  - Chrome closes
  - you quit
  - nothing has arrived from the hub for `WATCHDOG_S` (2 s), even though the hub sends a heartbeat every 0.5 s

## 11. File map

| File | Runs on | Role |
|---|---|---|
| `hub_program.py` | hub | reads sensors, lever logic, motor auto-return and trigger spring, prints events |
| `bridge.py` | Mac | main program: ports, calibration, upload, event loop, Chrome |
| `controller.py` | Mac | `ControllerLogic`: the rules and state machine |
| `gamepad.py` | Mac + Chrome | `VirtualGamepad` state and `ChromeGamepad` (the virtual pad in Chrome) |
| `config.py` | Mac | every tuning value and Xbox mapping |
| `dashboard.html` | Chrome | the live dashboard (`bridge.py --tester`): LEGO controller next to `navigator.getGamepads()` |
| `check_chrome.py` | Mac + Chrome | checks without the hub that Chrome's Gamepad API sees every input |
| `recorder.py` | Mac + Chrome | records the Remote Play game picture (or the dashboard) through Chrome's screencast |
| `make_demo.py` | Mac | turns a recording into an MP4 and a GIF with a caption bar |
| `make_media.py` | Mac + Chrome | scripted scenes through the real software, for the README's screenshots and GIFs |
| `tests/` | Mac | unit tests: rules, lever logic, hub modes and spring, config injection |
| `spike/` | Mac | SPIKE Prime Bluetooth protocol client (from V1) |
| `calibration.json` | Mac | your ports, lever directions and tilt calibration (not in git) |

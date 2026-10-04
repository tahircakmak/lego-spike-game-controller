# LEGO SPIKE controller for Minecraft Dungeons II (V2)

> This is the second version of the controller. The first one is in [`../v1`](../v1/README.md).

We built this together: [@yusufkeremcakmak](https://github.com/yusufkeremcakmak) designed and built the LEGO controller, and together we wrote the software that turns it into a game controller.

The V2 controller is a LEGO SPIKE Prime hub with:
- a force sensor
- two motorised buttons
- two rear shoulder triggers geared to one large motor

It connects to the computer over Bluetooth using the normal LEGO firmware. It shows up in Chrome as an **Xbox controller** and plays Minecraft Dungeons II on an Xbox through Xbox Remote Play.

## Building tutorial

[![How to build the V2 LEGO SPIKE controller (video)](https://img.youtube.com/vi/_lUhxB4JkKw/hqdefault.jpg)](https://www.youtube.com/watch?v=_lUhxB4JkKw)

*Building tutorial by [@yusufkeremcakmak](https://github.com/yusufkeremcakmak): [watch it on YouTube](https://www.youtube.com/watch?v=_lUhxB4JkKw).*

For how everything works inside (lever logic, the virtual gamepad in Chrome, the state machine), see [HOW_IT_WORKS.md](HOW_IT_WORKS.md).

| Program | What it does |
|---|---|
| `bridge.py` | Connect the real controller. It opens Chrome on Xbox Remote Play with the virtual Xbox controller, or on a test page with `--tester`. |
| `hub_program.py` | Runs **on the hub**. `bridge.py` uploads and starts it for you. |
| `config.py` | Every threshold, timing and Xbox button mapping, in one place. |
| `check_chrome.py` | Check that Chrome sees the virtual controller. No hub needed. |

## Setup

From the repository root (one virtual environment for V1 and V2):

```bash
python3 -m venv .venv
.venv/bin/pip install -r v2/requirements.txt
cd v2
```

Google Chrome must be installed. All commands below run from the `v2/` folder.

## The controller

| Part | Port | What it is |
|---|---|---|
| Hub | - | built-in gyro (tilt), left and right buttons |
| Force sensor | any | the round button |
| Left motorised button | any | small / medium angular motor: press it, or push it backward |
| Right motorised button | any | small / medium angular motor: press it, or push it backward |
| Shoulder triggers | any | **one** large angular motor geared to both rear triggers: the left trigger turns it one way, the right trigger the other way |

The ports are found automatically, and calibration works out which motor is which.

## Controls

The controller has two modes, **GAMEPLAY** and **INVENTORY**. In GAMEPLAY you are either **STANDING** (the start) or **MOVING**.

| Controller | In the game | In the inventory |
|---|---|---|
| Tilt the hub forward | Roll / dodge (LB) | Y |
| Tilt the hub backward | Y | - |
| Tilt the hub left | B | X |
| Tilt the hub right | RB | B |
| Force sensor | Melee (X, held while pressed) | Select (A, held while pressed) |
| Left motorised button: press | Health potion (LT) | - |
| Left motorised button: push back | Open the inventory (D-pad up) | - |
| Right motorised button: press | Jump (A) | - |
| Right motorised button: push back | MOVING on / off | - |
| Left shoulder trigger | MOVING: steer left | Slot left (D-pad left) |
| Right shoulder trigger | STANDING: ranged attack (RT, held). MOVING: steer right | Slot right (D-pad right) |
| Hold either shoulder trigger 0.8 s | - | Close the inventory (B) |
| Left hub button | Map (View) | Slot up (D-pad up) |
| Right hub button | Menu | Slot down (D-pad down) |

How the special controls feel:

- **Tilting** fires once per tilt. Bring the hub back level to tilt again.
- **Moving:** push the right button back to start walking. The player walks in a direction you steer with the shoulder triggers: hold the left one to turn left, the right one to turn right. Push the right button back again to stand still.
- **Ranged attack:** while standing, pull the right trigger to draw and keep holding to aim. Ease it back about **5°** to let go. Then the motor puts the trigger back at rest.
- **Shoulder triggers otherwise** (moving, inventory, or the left trigger while standing) work like springs: the motor pulls them back to rest as soon as you let go.
- **Inventory:** a short trigger pull moves one slot. Holding the trigger for 0.8 s closes the inventory. You come back standing still.
- **Motorised buttons:** after a release, the motor puts the button back at rest.

The Xbox buttons are the Minecraft Dungeons II defaults from the [dungeons2.wiki controls guide](https://dungeons2.wiki/guides/controls/), which says the final bindings weren't confirmed. Roll is the controller's own mapping on LB. You can change any of them in `config.py`.

## Using the real controller

Before running:

1. Turn on the hub and press its **Bluetooth button** so the light blinks.
2. Close the SPIKE app. The hub accepts **only one connection at a time**.
3. Leave both motorised buttons and the shoulder triggers **at rest**. Their position at start-up counts as 0°.

### Commands

| Command | What it does |
|---|---|
| `../.venv/bin/python bridge.py` | Play: Chrome opens on Xbox Remote Play with the controller |
| `../.venv/bin/python bridge.py --tester` | Chrome opens a test page that shows the controller's buttons and sticks live |
| `../.venv/bin/python bridge.py --no-chrome` | Don't open Chrome, only print what the controller does |
| `../.venv/bin/python bridge.py --calibrate` | Find the motors and the tilt directions again, then play |
| `../.venv/bin/python bridge.py --name <hub name>` | Only connect to the hub with this Bluetooth name |
| `../.venv/bin/python bridge.py --slot 5` | Use another program slot on the hub (0-19, default 0) |
| `../.venv/bin/python check_chrome.py` | No hub needed: checks that Chrome's Gamepad API sees every input |
| `../.venv/bin/python check_chrome.py --show` | The same, with the Chrome window visible |
| `../.venv/bin/python -m unittest discover -s tests` | Run the tests |

The terminal shows every action and the current mode, for example:

```
  Right shoulder trigger pulled -> rt held (ranged)
GAMEPLAY/STANDING  heading     0°  stick +0.00,+0.00  buttons: rt
  MOVING = ON
GAMEPLAY/MOVING    heading     0°  stick +0.00,-1.00  buttons: -
```

To quit, close Chrome or press Ctrl+C. The hub's centre button also stops it. Every button is released when it stops.

### Calibrating

Calibration runs by itself the first time, or when the force sensor or a motor has moved to another port. To run it yourself:

```bash
../.venv/bin/python bridge.py --calibrate
```

The terminal asks you to:

1. Press the **left** motorised button, then let it go back to rest.
2. Press the **right** motorised button, then let it go back to rest.
3. Pull the **right** shoulder trigger, then let it go back to rest.
4. Hold the controller **level**, tilt it **forward**, and tilt it **right**, pressing the **right hub button** after each.

The first three steps tell it which motor is which and which way is "pressed". The tilt steps work however the hub is mounted. Everything is saved in `calibration.json`.

The two motorised buttons are the same kind of motor, so swapping their cables isn't detected. **Run `--calibrate` after you re-cable.**

### Playing on your Xbox

Minecraft Dungeons II runs on the Xbox, and the Mac streams it with **Xbox Remote Play** in Chrome (`xbox.com/play/consoles`).

One-time setup on the Xbox: **Settings → Devices & connections → Remote features → Enable remote features**, and set the power mode to **Sleep** so it can be woken remotely.

Then run `../.venv/bin/python bridge.py`. Chrome opens on Remote Play: sign in if asked, pick your Xbox, press **Remote play**, and start the game. The LEGO controller is the page's Xbox controller.

Chrome uses its own profile, shared with V1, so you stay signed in. It is stored outside the project in `~/Library/Application Support/lego-spike-game-controller/chrome-profile`. Delete that folder to sign out.

## Tuning

Everything is in [`config.py`](config.py), with a comment on each value. The ones you are most likely to change:

| Setting | Default | What it does |
|---|---|---|
| `GYRO_THRESHOLD_DEG` | 20° each way | how far to tilt for a gesture |
| `GYRO_DEADZONE_DEG` | 8° | how level counts as "back to neutral" |
| `BUTTON_PRESS_DEG` / `BUTTON_BACK_DEG` | 25° / 25° | how far to press / push back a motorised button |
| `TRIGGER_PULL_DEG` | 20° | how far to pull a shoulder trigger |
| `TRIGGER_RELEASE_DEG` | 5° | how far to ease the trigger back to stop a ranged attack |
| `TRIGGER_SPRING_STRENGTH` / `TRIGGER_SPRING_MAX` | 120 / 4000 | how hard the trigger spring pulls back |
| `STEER_TURN_RATE` | 120°/s | how fast the triggers turn the walking direction |
| `STEER_STYLE` | `"turn"` | `"strafe"` makes the triggers move straight left / right instead |
| `INVENTORY_CLOSE_HOLD_S` | 0.8 s | how long to hold a trigger to close the inventory |
| `FORCE_PRESS` / `FORCE_RELEASE` | 10 / 5 | force sensor press / release levels |
| `GAMEPLAY`, `INVENTORY` | see Controls | which Xbox button each input sends |

Restart `bridge.py` after changing a value: it uploads the new values to the hub.

## How it works

```
 Gyro, hub buttons ─┐
 Force sensor ──────┼─ hub_program.py ── print() ── Bluetooth ──▶ bridge.py ──▶ ControllerLogic ──▶ virtual Xbox ──▶ Chrome ──▶ Xbox Remote Play
 3 motors ──────────┘   (on the hub)                              (reads lines)   (game rules)        controller       Gamepad API
```

1. `bridge.py` briefly streams sensor data to find the ports. Then it uploads `hub_program.py` with the values from `config.py` and starts it.
2. The hub program prints a short line whenever something changes, for example `F 1` (force), `M D +` (trigger pulled), `T 12 -3` (tilt).
3. The hub handles everything physical itself: press and release detection, putting motors back at rest, and the trigger spring. So it reacts without a round trip over Bluetooth.
4. `ControllerLogic` turns the lines into Xbox buttons and sticks. Chrome is started with a virtual Xbox controller in its Gamepad API, and Remote Play reads it like a real one. No keyboard or mouse is involved.

The details are in [HOW_IT_WORKS.md](HOW_IT_WORKS.md).

## Troubleshooting

| Problem | Fix |
|---|---|
| `No SPIKE hub found` | Wake the hub, press its Bluetooth button, and close the SPIKE app and other scripts. |
| `../.venv/bin/python: no such file or directory` | Run the commands from the `v2/` folder. |
| `Check the cables: ...` | The controller needs 1 force sensor, 2 small or medium angular motors and 1 large angular motor. The message lists what the hub found on each port. |
| Buttons act swapped or the wrong way round | Run `--calibrate`, starting with everything at rest. |
| A tilt gesture is hard to trigger | Lower that direction in `GYRO_THRESHOLD_DEG`, or tilt further. Calibration prints how far you tilted. |
| `hub warning: trigger spring off` | The spring pushed the wrong way or the trigger went very far. Check the build, or set `TRIGGER_SPRING = False`. |
| Trigger spring too weak or too strong | Change `TRIGGER_SPRING_STRENGTH` / `TRIGGER_SPRING_MAX`. |
| Lines starting with `hub:` | An error from the hub program: the message comes from the hub. Every input is released. |
| A button does the wrong thing in the game | Change it in `GAMEPLAY` / `INVENTORY` in `config.py`. |

## Authors

- [@yusufkeremcakmak](https://github.com/yusufkeremcakmak): controller design, LEGO build, building tutorial, control scheme and testing
- [@tahircakmak](https://github.com/tahircakmak): software

## License

MIT, see [LICENSE](../LICENSE). `spike/protocol.py` is modified from the LEGO Group's SPIKE Prime protocol example (Apache 2.0); the details are in LICENSE. Not affiliated with the LEGO Group or Microsoft.

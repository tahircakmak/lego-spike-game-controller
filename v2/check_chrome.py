"""
Check, without the hub, that Chrome's Gamepad API sees the virtual Xbox controller.

Starts Chrome (with a throwaway profile, not your Remote Play sign-in), drives the
controller rules with scripted LEGO events, and reads navigator.getGamepads() back
inside the page after each step.

    python check_chrome.py            # Chrome in the background
    python check_chrome.py --show     # watch it on the gamepad tester page
"""

import argparse
import asyncio
import sys
import tempfile

import config as cfg
from controller import ControllerLogic, check_config
from gamepad import BUTTONS, TESTER_URL, ChromeGamepad


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--show", action="store_true", help="show the Chrome window and slow down")
    args = parser.parse_args()
    check_config()

    logic = ControllerLogic(log=lambda text: None)
    clock = [0.0]
    failures = 0

    with tempfile.TemporaryDirectory() as profile:
        async with ChromeGamepad(TESTER_URL, profile, headless=not args.show) as chrome:

            async def step(label, action, buttons, stick=None, wait=0.02):
                nonlocal failures
                action()
                clock[0] += wait
                logic.update(clock[0])
                await chrome.push(logic.pad.snapshot(clock[0]))
                seen = await chrome.read_back()
                got = [BUTTONS[i] for i, v in enumerate(seen["buttons"]) if v]
                ok = got == buttons and (stick is None or [round(v, 2) for v in seen["axes"][:2]] == stick)
                failures += not ok
                print(f"{'ok  ' if ok else 'FAIL'} {label:55} page sees {got or '-'} "
                      f"left stick {[round(v, 2) for v in seen['axes'][:2]]}")
                if args.show:
                    await asyncio.sleep(0.6)

            t = lambda: clock[0]  # noqa: E731
            pad = await chrome.read_back()
            print(f"Chrome sees: {pad['id']!r}, mapping {pad['mapping']!r}")
            await step("force sensor pressed", lambda: logic.on_force(True, t()), ["x"])
            await step("force sensor still pressed (1 s)", lambda: None, ["x"], wait=1.0)
            await step("force sensor released", lambda: logic.on_force(False, t()), [], wait=0.2)
            await step("left button pressed", lambda: logic.on_lever("left_button", "+", t()), ["lt"])
            await step("left button released", lambda: logic.on_lever("left_button", "0", t()), [], wait=0.2)
            await step("hub left / right buttons", lambda: (logic.on_hub_button("left", True, t()),
                                                            logic.on_hub_button("right", True, t())),
                       ["view", "menu"])
            await step("hub buttons released", lambda: (logic.on_hub_button("left", False, t()),
                                                        logic.on_hub_button("right", False, t())), [], wait=0.2)
            await step("gyro neutral", lambda: logic.on_tilt(0, 0, t()), [], wait=0.2)
            await step("gyro tilted left", lambda: logic.on_tilt(0, -30, t()), ["b"], wait=0.0)
            await step("gyro kept left (no repeat)", lambda: logic.on_tilt(0, -31, t()), [], wait=0.2)
            await step("standing: right trigger pulled", lambda: logic.on_lever("trigger", "+", t()), ["rt"])
            await step("right trigger held (2 s)", lambda: None, ["rt"], wait=2.0)
            await step("right trigger moved back 5 degrees", lambda: logic.on_lever("trigger", "0", t()), [],
                       wait=0.2)
            await step("right button pushed back: MOVING = ON",
                       lambda: logic.on_lever("right_button", "-", t()), [], [0.0, -cfg.MOVE_SPEED])
            await step("left trigger steers", lambda: logic.on_lever("trigger", "-", t()), [], wait=0.0)
            for _ in range(5):
                await step("  ...steering left", lambda: None, [], wait=0.15)
            await step("left trigger released", lambda: logic.on_lever("trigger", "0", t()), [])
            await step("left button pushed back: INVENTORY",
                       lambda: logic.on_lever("left_button", "-", t()), ["dpad_up"], [0.0, 0.0])
            await step("inventory: force sensor", lambda: logic.on_force(True, t()), ["a"], wait=0.2)
            await step("inventory: force released", lambda: logic.on_force(False, t()), [], wait=0.2)
            await step("inventory: short left-trigger pull", lambda: logic.on_lever("trigger", "-", t()),
                       ["dpad_left"])
            await step("inventory: trigger still held (no close yet)", lambda: None, [],
                       wait=cfg.INVENTORY_CLOSE_HOLD_S - 0.05)
            await step("inventory: trigger held -> close (hub's C event)",
                       lambda: logic.on_lever("trigger", "C", t()), ["b"])
            await step("trigger released, back in gameplay", lambda: logic.on_lever("trigger", "0", t()), [],
                       wait=0.2)
            await step("release everything", logic.release_all, [], [0.0, 0.0])

    print("\nAll good: Chrome's Gamepad API saw every input." if not failures else f"\n{failures} step(s) failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

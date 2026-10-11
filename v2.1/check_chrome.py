"""
Check, without the hub, that Chrome's Gamepad API sees the virtual Xbox controller.

Starts Chrome (with a throwaway profile, not your Remote Play sign-in), drives the
V2.1 controller rules with scripted LEGO events, and reads navigator.getGamepads() back
inside the page after each step.

    python check_chrome.py            # Chrome in the background
    python check_chrome.py --show     # watch it on the dashboard
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

    logic = ControllerLogic({"near_mm": 80, "covered": False}, log=lambda text: None)
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
                await chrome.push_status(logic.dashboard())
                seen = await chrome.read_back()
                got = [BUTTONS[i] for i, v in enumerate(seen["buttons"]) if v]
                axes = [round(v, 2) for v in seen["axes"][:2]]
                ok = sorted(got) == sorted(buttons) and (stick is None or axes == stick)
                failures += not ok
                print(f"{'ok  ' if ok else 'FAIL'} {label:50} page sees {got or '-'} left stick {axes}")
                if args.show:
                    await asyncio.sleep(0.6)

            t = lambda: clock[0]  # noqa: E731
            pad = await chrome.read_back()
            print(f"Chrome sees: {pad['id']!r}, mapping {pad['mapping']!r}")
            await step("right button pressed (melee)", lambda: logic.on_lever("right_button", "+", t()), ["x"])
            await step("right button still pressed (1 s)", lambda: None, ["x"], wait=1.0)
            await step("right button released", lambda: logic.on_lever("right_button", "0", t()), [], wait=0.2)
            await step("left button pressed (jump)", lambda: logic.on_lever("left_button", "+", t()), ["a"])
            await step("left button released", lambda: logic.on_lever("left_button", "0", t()), [], wait=0.2)
            await step("right trigger pulled (ranged)", lambda: logic.on_lever("right_trigger", "+", t()), ["rt"])
            await step("right trigger held (2 s)", lambda: None, ["rt"], wait=2.0)
            await step("left trigger pulled too", lambda: logic.on_lever("left_trigger", "+", t()), ["rt", "lt"])
            await step("both triggers released", lambda: (logic.on_lever("right_trigger", "0", t()),
                                                          logic.on_lever("left_trigger", "0", t())), [], wait=0.2)
            await step("force sensor pressed", lambda: logic.on_force(True, t()), ["lb"])
            await step("force sensor released", lambda: logic.on_force(False, t()), [], wait=0.2)
            await step("left hub button", lambda: logic.on_hub_button("left", True, t()), ["dpad_up"])
            await step("left hub button released", lambda: logic.on_hub_button("left", False, t()), [], wait=0.2)
            await step("hand over the distance sensor", lambda: logic.on_distance(60, t()), cfg.ARTIFACTS)
            await step("hand still there (no repeat)", lambda: logic.on_distance(55, t()), [], wait=0.3)
            await step("hand gone", lambda: logic.on_distance(400, t()), [], wait=1.0)
            await step("right hub button: MOVING = ON", lambda: logic.on_hub_button("right", True, t()), [],
                       [0.0, -cfg.MOVE_SPEED])
            await step("right hub button let go (still moving)", lambda: logic.on_hub_button("right", False, t()),
                       [], [0.0, -cfg.MOVE_SPEED])
            await step("tilt right: steering", lambda: logic.on_tilt(cfg.STEER_FULL_DEG, t()), [])
            for _ in range(5):
                await step("  ...turning right", lambda: None, [], wait=0.1)
            await step("level again: keeps walking that way", lambda: logic.on_tilt(0, t()), [])
            await step("right hub button: MOVING = OFF", lambda: logic.on_hub_button("right", True, t()), [],
                       [0.0, 0.0])
            await step("release everything", logic.release_all, [], [0.0, 0.0])

    print("\nAll good: Chrome's Gamepad API saw every input." if not failures else f"\n{failures} step(s) failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

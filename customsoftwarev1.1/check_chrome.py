"""
Check, without the hub, that Chrome's Gamepad API sees the virtual Xbox controller.

Starts Chrome (with a throwaway profile, not your Remote Play sign-in), drives the engine
with the example loadout and scripted LEGO readings, and reads navigator.getGamepads()
back inside the page after each step.

    python check_chrome.py            # Chrome in the background
    python check_chrome.py --show     # watch it
"""

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from chrome import ChromeGamepad
from engine import BUTTONS, Engine

PAGE = "data:text/html,<h1>SPIKE custom software: Gamepad API check</h1>"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--show", action="store_true", help="show the Chrome window and slow down")
    args = parser.parse_args()

    loadout = json.loads((Path(__file__).parent / "loadouts" / "example-everything.json").read_text())
    engine = Engine(loadout, log=lambda text: None)
    clock = [0.0]
    failures = 0

    with tempfile.TemporaryDirectory() as profile:
        async with ChromeGamepad([PAGE], "http://127.0.0.1:1/", profile, headless=not args.show) as chrome:
            page = chrome.context.pages[0]

            async def step(label, sources, buttons, axes=None, wait=0.02):
                nonlocal failures
                for name, value in sources.items():
                    engine.set_source(name, value)
                clock[0] += wait
                engine.tick(clock[0])
                await chrome.push(engine.snapshot())
                seen = await page.evaluate("""() => { const p = navigator.getGamepads()[0];
                    return { id: p.id, axes: p.axes.slice(), buttons: p.buttons.map(b => b.pressed ? 1 : 0) }; }""")
                got = [BUTTONS[i] for i, v in enumerate(seen["buttons"]) if v]
                got_axes = [round(v, 2) for v in seen["axes"]]
                ok = sorted(got) == sorted(buttons) and (axes is None or got_axes == axes)
                failures += not ok
                print(f"{'ok  ' if ok else 'FAIL'} {label:48} page sees {got or '-'} axes {got_axes}")
                if args.show:
                    await asyncio.sleep(0.6)

            rest = {"motor:A": 0, "motor:B": 0, "motor:E": 0, "motor:F": 0, "force:C": 0, "distance:D": 100,
                    "hub:left": 0, "hub:right": 0, "gyro:right": 0, "gyro:forward": 0}
            await step("everything at rest", rest, [], [0, 0, 0, 0])
            await step("motor A pressed: A", {"motor:A": 30}, ["a"])
            await step("motor A released", {"motor:A": 0}, [], wait=0.2)
            await step("motor E at -40°: D-pad up", {"motor:E": -40}, ["dpad_up"])
            await step("force sensor too: D-pad up becomes Y", {"force:C": 50}, ["y"], wait=0.2)
            await step("both released", {"motor:E": 0, "force:C": 0}, [], wait=0.2)
            await step("motor F at -90°: right stick west", {"motor:F": -90}, [], [0, 0, -1, 0])
            await step("motor B at 90°: left stick north (up = -1)", {"motor:F": 0, "motor:B": 90}, [], [0, -1, 0, 0])
            await step("hand at 5 cm: left stick becomes right stick", {"distance:D": 5}, [], [0, 0, 0, -1])
            await step("hand gone", {"distance:D": 100, "motor:B": 0}, [], [0, 0, 0, 0])
            await step("hand at 15 cm: Y + B + RB", {"distance:D": 15}, ["y", "b", "rb"])
            await step("right hub button: mode 1 (gyro walking)", {"distance:D": 100, "hub:right": 1}, [], wait=0.2)
            await step("tilt right + forward: north-east", {"hub:right": 0, "gyro:right": 30, "gyro:forward": 25},
                       [], [0.71, -0.71, 0, 0])

    print("\nAll good: Chrome's Gamepad API saw every input." if not failures else f"\n{failures} step(s) failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

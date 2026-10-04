"""
Make the README's screenshots and GIFs, without the hub or the Xbox.

Plays scripted scenes ("press the force sensor", "pull the right trigger", ...) through
the real V2 software: hub_program.py's own Lever and Modes logic turns scripted motor
angles into hub events, ControllerLogic applies the rules, and the virtual controller
goes into Chrome's Gamepad API. The dashboard page shows both sides; recorder.py records
it and make_demo.py cuts it into GIFs.

    python make_media.py            # -> docs/images/*.png, *.gif and docs/demo/v2-tour.mp4
    python make_media.py --show     # watch it happen in a visible Chrome window

Everything shown is the software's real output for those inputs; the inputs are scripted.
"""

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path

import config as cfg
from controller import ControllerLogic, check_config
from gamepad import TESTER_URL, ChromeGamepad
from make_demo import make
from recorder import Recorder

HERE = Path(__file__).parent
IMAGES = HERE / "docs" / "images"
FPS = 60


def load_hub_program() -> dict:
    """Run hub_program.py's top level on the Mac (runloop stubbed, main() never runs)."""
    runloop = types.ModuleType("runloop")
    runloop.run = lambda coroutine: coroutine.close()
    sys.modules["runloop"] = runloop
    namespace = {"__name__": "hub_program"}
    exec(compile((HERE / "hub_program.py").read_text(), "hub_program.py", "exec"), namespace)
    return namespace


class SimulatedHub:
    """The hub program's lever and mode logic, fed with scripted angles instead of motors."""

    def __init__(self, logic: ControllerLogic):
        hub = load_hub_program()
        self.logic = logic
        Lever = hub["Lever"]
        self.levers = {
            "left_button": Lever(cfg.BUTTON_PRESS_DEG, cfg.BUTTON_BACK_DEG, cfg.BUTTON_RELEASE_DEG, cfg.REARM_DEG),
            "right_button": Lever(cfg.BUTTON_PRESS_DEG, cfg.BUTTON_BACK_DEG, cfg.BUTTON_RELEASE_DEG, cfg.REARM_DEG),
            "trigger": Lever(cfg.TRIGGER_PULL_DEG, cfg.TRIGGER_PULL_DEG, cfg.TRIGGER_RELEASE_DEG, cfg.REARM_DEG),
        }
        self.modes = hub["Modes"](round(cfg.INVENTORY_CLOSE_HOLD_S * 1000))

    def angle(self, role: str, angle: float) -> None:
        now = time.monotonic()
        self.logic.angles[role] = angle
        lever = self.levers[role]
        event = lever.update(angle)
        if event in ("+", "-"):
            lever.ranged_press = role == "trigger" and event == "+" and self.modes.ranged()
        if event:
            self.modes.lever_event(role, event, round(now * 1000))
            self.logic.on_lever(role, event, now)

    def tick(self) -> None:
        now = time.monotonic()
        if self.modes.tick(round(now * 1000), lambda a, b: a - b):
            self.logic.on_lever("trigger", "C", now)


class Director:
    """Runs the scenes in real time and keeps Chrome up to date."""

    def __init__(self, chrome: ChromeGamepad, logic: ControllerLogic, hub: SimulatedHub):
        self.chrome, self.logic, self.hub = chrome, logic, hub
        self.angles = {"left_button": 0.0, "right_button": 0.0, "trigger": 0.0}
        self.tilt = (0.0, 0.0)
        self.scenes: list[tuple[str, float, float]] = []
        self.frozen = False

    async def pump(self) -> None:
        """Like bridge.py's loop: update the rules and push to Chrome every 10 ms."""
        while True:
            if self.frozen:  # a screenshot of a short press is being taken
                await asyncio.sleep(0.01)
                continue
            now = time.monotonic()
            self.hub.tick()
            self.logic.update(now)
            await self.chrome.push(self.logic.pad.snapshot(now))
            await self.chrome.push_status(self.logic.dashboard())
            await asyncio.sleep(0.01)

    async def wait(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def move(self, role: str, to: float, seconds: float) -> None:
        """Turn a motor smoothly to an angle (a finger pulling it, or the motor returning it)."""
        start = self.angles[role]
        steps = max(1, round(seconds * FPS))
        for i in range(1, steps + 1):
            self.angles[role] = start + (to - start) * i / steps
            self.hub.angle(role, self.angles[role])
            await asyncio.sleep(seconds / steps)

    async def tilt_to(self, forward: float, right: float, seconds: float = 0.2) -> None:
        (f0, r0), steps = self.tilt, max(1, round(seconds * FPS))
        for i in range(1, steps + 1):
            self.tilt = (f0 + (forward - f0) * i / steps, r0 + (right - r0) * i / steps)
            self.logic.on_tilt(*self.tilt, time.monotonic())
            await asyncio.sleep(seconds / steps)

    def force(self, down: bool) -> None:
        self.logic.on_force(down, time.monotonic())

    def hub_button(self, which: str, down: bool) -> None:
        self.logic.on_hub_button(which, down, time.monotonic())

    async def press(self, role: str, angle: float, hold: float) -> None:
        """Press a motorised button (or push it back with a negative angle), let go, motor returns it."""
        await self.move(role, angle, 0.18)
        await self.wait(hold)
        await self.move(role, angle * 0.6, 0.12)  # finger lets go: it springs back a little = release
        await self.move(role, 0, 0.3)             # the motor drives it back to rest

    async def shot(self, name: str, during_press: bool = False) -> None:
        """Screenshot the dashboard. during_press: catch a 0.1 s press while it is still down."""
        if during_press:
            self.frozen = True  # keep the page on the current state while the screenshot is taken
        else:
            await asyncio.sleep(0.05)  # let the page draw the latest state
        await self.chrome.page.locator("#stage").screenshot(path=IMAGES / f"{name}.png")
        self.frozen = False
        print(f"  docs/images/{name}.png")

    async def scene(self, name: str, play) -> None:
        print(f"Scene: {name}")
        start = time.time()
        await play(self)
        self.scenes.append((name, start, time.time()))


# ---- the scenes ---------------------------------------------------------------------------

async def overview(d: Director) -> None:
    await d.wait(1.2)
    await d.shot("v2-dashboard")


async def melee(d: Director) -> None:
    await d.wait(0.5)
    d.force(True)
    await d.wait(0.8)
    await d.shot("v2-melee")
    await d.wait(0.8)
    d.force(False)
    await d.wait(1.0)


async def ranged(d: Director) -> None:
    await d.wait(0.5)
    await d.move("trigger", 45, 0.5)   # pull the right trigger: RT held
    await d.wait(0.6)
    await d.shot("v2-ranged")
    await d.wait(1.0)
    await d.move("trigger", 38, 0.35)  # ease it back 5 degrees: RT released
    await d.wait(0.3)
    await d.move("trigger", 0, 0.45)   # the motor drives it back to rest
    await d.wait(1.0)


async def buttons(d: Director) -> None:
    await d.wait(0.5)
    await d.press("left_button", 35, 0.7)   # health potion (LT)
    await d.wait(0.6)
    await d.press("right_button", 35, 0.5)  # jump (A)
    await d.wait(1.0)


async def moving(d: Director) -> None:
    await d.wait(0.5)
    await d.press("right_button", -35, 0.2)  # push back: MOVING = ON
    await d.wait(1.0)
    await d.move("trigger", -38, 0.3)        # hold the left trigger: steer left
    await d.wait(0.5)
    await d.shot("v2-moving")
    await d.wait(0.7)
    await d.move("trigger", 0, 0.25)         # let go: the spring brings it back
    await d.wait(0.8)
    await d.move("trigger", 38, 0.3)         # right trigger: steer right
    await d.wait(1.6)
    await d.move("trigger", 0, 0.25)
    await d.wait(0.8)
    await d.press("right_button", -35, 0.2)  # push back again: STANDING
    await d.wait(1.0)


async def gyro(d: Director) -> None:
    await d.wait(0.4)
    for forward, right, shot in ((26, 0, None), (-26, 0, None), (0, -26, None), (0, 26, "v2-gyro")):
        await d.tilt_to(forward, right)
        if shot:
            await d.shot(shot, during_press=True)
        await d.wait(0.5)
        await d.tilt_to(0, 0)
        await d.wait(0.6)
    await d.wait(0.6)


async def inventory(d: Director) -> None:
    await d.wait(0.5)
    await d.press("left_button", -35, 0.2)  # push back: INVENTORY
    await d.wait(0.8)
    d.hub_button("right", True)              # slot down
    await d.wait(0.2)
    d.hub_button("right", False)
    await d.wait(0.6)
    await d.move("trigger", 34, 0.15)        # short pull: slot right
    await d.shot("v2-inventory", during_press=True)
    await d.move("trigger", 0, 0.2)          # spring back
    await d.wait(0.6)
    await d.move("trigger", -34, 0.15)       # short pull: slot left
    await d.move("trigger", 0, 0.2)
    await d.wait(0.6)
    d.force(True)                            # select: A
    await d.wait(0.4)
    d.force(False)
    await d.wait(0.6)
    await d.tilt_to(0, -26)                  # tilt left: X
    await d.wait(0.3)
    await d.tilt_to(0, 0)
    await d.wait(0.6)
    await d.move("trigger", 36, 0.2)         # hold a trigger 0.8 s: close (B)
    await d.wait(1.1)
    await d.move("trigger", 0, 0.25)
    await d.wait(1.2)


SCENES = [("overview", overview), ("melee", melee), ("ranged", ranged), ("buttons", buttons),
          ("moving", moving), ("gyro", gyro), ("inventory", inventory)]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--show", action="store_true", help="show the Chrome window")
    args = parser.parse_args()
    check_config()
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg is needed for the GIFs: brew install ffmpeg")
    IMAGES.mkdir(parents=True, exist_ok=True)
    (HERE / "docs" / "demo").mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        profile, folder = Path(tmp) / "profile", Path(tmp) / "recording"
        async with ChromeGamepad(TESTER_URL, profile, headless=not args.show) as chrome:
            await chrome.page.set_viewport_size({"width": 1280, "height": 720})
            logic = ControllerLogic(log=lambda text: print(f"    {text}"))
            director = Director(chrome, logic, SimulatedHub(logic))
            pump = asyncio.create_task(director.pump())
            await asyncio.sleep(0.5)
            recorder = Recorder(chrome.page, "#stage", folder=folder)
            await recorder.start()
            for name, play in SCENES:
                await director.scene(name, play)
            await recorder.stop()
            pump.cancel()

        t0 = min(f["t"] for f in json.loads((folder / "recording.json").read_text())["frames"])
        print("Encoding...")
        make(folder, HERE / "docs" / "demo" / "v2-tour.mp4", None, width=1280, captions=False)
        print("  docs/demo/v2-tour.mp4")
        for name, start, end in director.scenes:
            if name == "overview":
                continue
            gif = IMAGES / f"v2-{name}.gif"
            make(folder, None, gif, start=start - t0, end=end - t0, width=1280, gif_width=800, gif_fps=12,
                 captions=False)
            print(f"  docs/images/{gif.name}  {gif.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    asyncio.run(main())

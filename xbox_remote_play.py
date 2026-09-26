"""
Play Minecraft Dungeons on your Xbox through Xbox Remote Play in Chrome, with the
SPIKE controller showing up in the page as a normal Xbox controller.

    python bridge.py --xbox          # the real thing: hub + Remote Play
    python xbox_remote_play.py       # only open Remote Play (e.g. to sign in the first time)

How: Chrome is started with Playwright and a small script is added to every page.
The script replaces navigator.getGamepads() so the page sees one virtual Xbox
controller, whose buttons and sticks this program sets. This is what extensions
like Emux do, but without keyboard keys in between: tilt becomes a real analog stick.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

HERE = Path(__file__).parent
REMOTE_PLAY_URL = "https://www.xbox.com/play/consoles"


def _profile_dir() -> Path:
    """Chrome profile for Remote Play, kept outside the project because it holds your sign-in."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "lego-spike-game-controller" / "chrome-profile"


PROFILE_DIR = _profile_dir()  # keeps you signed in between runs

VIRTUAL_PAD_JS = """
(() => {
    if (window.__spikePad) return;
    const button = (on) => ({ pressed: on, touched: on, value: on ? 1 : 0 });
    const pad = {
        id: "Xbox 360 Controller (XInput STANDARD GAMEPAD)",
        index: 0,
        connected: true,
        mapping: "standard",
        timestamp: performance.now(),
        axes: [0, 0, 0, 0],
        buttons: Array.from({ length: 17 }, () => button(false)),
        vibrationActuator: null,
        hapticActuators: [],
    };
    navigator.getGamepads = () => [pad, null, null, null];

    const announce = () => {
        const event = new Event("gamepadconnected");
        Object.defineProperty(event, "gamepad", { value: pad });
        window.dispatchEvent(event);
    };
    let announced = false;
    window.__spikePad = {
        set(state) {
            pad.axes = state.axes.slice();
            pad.buttons = pad.buttons.map((_, i) => button(state.pressed.includes(i)));
            pad.timestamp = performance.now();
            if (!announced) {
                announced = true;
                announce();
            }
        },
    };
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", announce);
    } else {
        announce();
    }
})();
"""

# Standard gamepad button numbers (Xbox layout).
BUTTONS = {
    "a": 0, "b": 1, "x": 2, "y": 3, "lb": 4, "rb": 5, "lt": 6, "rt": 7,
    "view": 8, "menu": 9, "ls": 10, "rs": 11, "up": 12, "down": 13, "left": 14, "right": 15,
}

# ControllerLogic's key names -> Minecraft Dungeons controller buttons.
GAME_BUTTONS = {
    "mouse_left": "a",    # melee attack / select in the inventory
    "mouse_right": "rt",  # ranged attack (held while the trigger is pulled)
    "space": "rb",        # roll
    "1": "x",             # artifact 1
    "2": "y",             # artifact 2
    "3": "b",             # artifact 3
    "i": "up",            # open the inventory
    "esc": "b",           # close the inventory (back)
    "e": "lb",            # health potion
}
TAP_SECONDS = 0.1  # how long a tapped button stays down, so the game's polling sees it


class GamepadOutput:
    """ControllerLogic output that drives the virtual Xbox controller."""

    def __init__(self):
        self.held: set[str] = set()
        self.tap_until: dict[str, float] = {}
        self.left_stick = (0.0, 0.0)

    def hold(self, key):
        if key in GAME_BUTTONS:
            self.held.add(GAME_BUTTONS[key])

    def release(self, key):
        self.held.discard(GAME_BUTTONS.get(key))

    def tap(self, key):
        if key in GAME_BUTTONS:
            self.tap_until[GAME_BUTTONS[key]] = time.monotonic() + TAP_SECONDS

    def set_move(self, keys):
        pass  # movement uses the analog stick instead of W A S D

    def mouse_move(self, dx, dy):
        pass  # the inventory cursor uses the stick too

    def stick(self, x, y):
        self.left_stick = (x, y)

    def release_all(self):
        self.held.clear()
        self.tap_until.clear()
        self.left_stick = (0.0, 0.0)

    def state(self) -> dict:
        now = time.monotonic()
        pressed = self.held | {b for b, until in self.tap_until.items() if until > now}
        x, y = self.left_stick
        return {"axes": [round(x, 3) + 0.0, round(y, 3) + 0.0, 0, 0], "pressed": sorted(BUTTONS[b] for b in pressed)}


class RemotePlay:
    """
    Chrome on Xbox Remote Play with the virtual controller.

        async with RemotePlay() as remote:
            await remote.push(output.state())
    """

    def __init__(self, url: str = REMOTE_PLAY_URL):
        self.url = url
        self._last_state = None

    async def __aenter__(self):
        self._playwright = await async_playwright().start()
        self.context = await self._playwright.chromium.launch_persistent_context(
            PROFILE_DIR,
            channel="chrome",
            headless=False,
            no_viewport=True,
            ignore_default_args=["--enable-automation"],
            args=["--start-maximized"],
        )
        await self.context.add_init_script(VIRTUAL_PAD_JS)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        await self.page.goto(self.url)
        return self

    async def __aexit__(self, *exc):
        try:
            await self.context.close()
        except PlaywrightError:
            pass  # Chrome was already closed
        await self._playwright.stop()

    @property
    def closed(self) -> bool:
        return self.page.is_closed()

    async def push(self, state: dict) -> None:
        """Send the controller state to the page, only when it changed."""
        if state == self._last_state or self.closed:
            return
        try:
            await self.page.evaluate("s => window.__spikePad && window.__spikePad.set(s)", state)
            self._last_state = state
        except PlaywrightError:
            pass  # the page is navigating; try again next time


async def main() -> None:
    print("Opening Xbox Remote Play in Chrome...")
    async with RemotePlay() as remote:
        print("Sign in if asked, pick your Xbox and press Remote play.")
        print("Close Chrome or press Ctrl+C here when done.")
        while not remote.closed:
            await asyncio.sleep(0.5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)

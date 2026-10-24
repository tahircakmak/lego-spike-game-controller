"""
Chrome (started with Playwright) with the virtual Xbox controller in its Gamepad API.

Every page in this Chrome gets a script, before the page's own scripts run, that makes
navigator.getGamepads() return a standard-mapping Xbox controller. app.py updates it live,
so Xbox Remote Play reads the LEGO controller like a real pad. This is the same approach
as V1, V2 and V2.1: macOS only lets Apple-approved apps create a system-wide virtual
controller, so the controller lives inside this Chrome window.

The control panel opens as a second tab in the same window (drag the tab out to give it its
own window). It is served by panel_server.py, so it also works in any other browser.
"""

import os
import sys
import time
from pathlib import Path

from playwright.async_api import Error as PlaywrightError

REMOTE_PLAY_URL = "https://www.xbox.com/play/consoles"

VIRTUAL_PAD_JS = """
(() => {
    if (window.__spikePad) return;
    const button = (v) => ({ pressed: v > 0.5, touched: v > 0, value: v });
    const pad = {
        id: "Xbox 360 Controller (XInput STANDARD GAMEPAD)",
        index: 0,
        connected: true,
        mapping: "standard",
        timestamp: performance.now(),
        axes: [0, 0, 0, 0],
        buttons: Array.from({ length: 17 }, () => button(0)),
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
            pad.buttons = state.buttons.map(button);
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


def profile_dir() -> Path:
    """Chrome profile for Remote Play (shared with V1, V2 and V2.1), outside the project: it holds your sign-in."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "lego-spike-game-controller" / "chrome-profile"


class ChromeGamepad:
    """
        async with ChromeGamepad(["https://www.xbox.com/play/consoles", panel_url], panel_url) as chrome:
            await chrome.push(engine.snapshot())
    """

    def __init__(self, urls: list[str], panel_url: str, profile=None, headless: bool = False):
        self.urls = urls
        self.panel_url = panel_url
        self.profile = profile or profile_dir()
        self.headless = headless
        self._sent: dict[int, tuple] = {}  # id(page) -> (state, when)
        self._closed = False

    async def __aenter__(self):
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self.context = await self._playwright.chromium.launch_persistent_context(
            self.profile,
            channel="chrome",
            headless=self.headless,
            no_viewport=True,
            ignore_default_args=["--enable-automation"],
            args=["--start-maximized"],
        )
        self.context.on("close", lambda _: setattr(self, "_closed", True))
        await self.context.add_init_script(VIRTUAL_PAD_JS)
        first = self.context.pages[0] if self.context.pages else await self.context.new_page()
        await first.goto(self.urls[0])
        for url in self.urls[1:]:
            page = await self.context.new_page()
            await page.goto(url)
        await first.bring_to_front()
        return self

    async def __aexit__(self, *exc):
        try:
            await self.context.close()
        except PlaywrightError:
            pass  # Chrome was already closed
        await self._playwright.stop()

    @property
    def closed(self) -> bool:
        return self._closed or not self.context.pages

    async def push(self, state: dict) -> None:
        """Send the controller state to every page except the control panel: when it changed, and
        every half second anyway (a page that just loaded starts with an empty controller)."""
        if self.closed:
            return
        now = time.monotonic()
        key = (tuple(state["axes"]), tuple(state["buttons"]))
        for page in list(self.context.pages):
            if page.is_closed() or page.url.startswith(self.panel_url):
                continue
            sent = self._sent.get(id(page))
            if sent and sent[0] == key and now - sent[1] < 0.5:
                continue
            try:
                await page.evaluate("s => window.__spikePad && window.__spikePad.set(s)", state)
                self._sent[id(page)] = (key, now)
            except PlaywrightError:
                pass  # the page is navigating; the next push retries

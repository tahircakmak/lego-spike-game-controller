"""
The virtual Xbox controller.

VirtualGamepad   the controller's state: which buttons are down and where the sticks are.
                 Pure Python, no I/O; ControllerLogic drives it.
ChromeGamepad    Chrome (started with Playwright) on Xbox Remote Play, with the virtual
                 controller in the page's Gamepad API: navigator.getGamepads() returns it
                 as a standard-mapping Xbox controller. Remote Play reads it like a real pad.

Why the Gamepad API inside Chrome and not an OS-level virtual HID device: macOS only lets
a process create a virtual HID device (IOHIDUserDevice / CoreHID) with Apple's restricted
"com.apple.developer.hid.virtual.device" entitlement, so an unsigned script can't make
one. The page-level pad is what V1 used with Remote Play, and no keyboard is involved.
"""

from pathlib import Path

from playwright.async_api import Error as PlaywrightError

# W3C "standard" gamepad layout: index = position in Gamepad.buttons.
BUTTONS = ["a", "b", "x", "y", "lb", "rb", "lt", "rt", "view", "menu", "ls", "rs",
           "dpad_up", "dpad_down", "dpad_left", "dpad_right", "xbox"]
BUTTON_INDEX = {name: i for i, name in enumerate(BUTTONS)}


class VirtualGamepad:
    """
    Button state with owners: pad.hold("force", "x") ... pad.release("force").

    An owner is the physical input holding the button, so a release always lets go of
    the button that input pressed, even if the mapping changed in between (for example
    the inventory opened). Every press lasts at least min_press_s so the page can't miss
    it; pulse() is a press of exactly that length.
    """

    def __init__(self, min_press_s: float):
        self.min_press_s = min_press_s
        self.holds: dict[str, str] = {}         # owner -> button
        self.pressed_at: dict[str, float] = {}  # button -> when it went down
        self.until: dict[str, float] = {}       # button -> stays down until (min press / pulse)
        self.left_stick = (0.0, 0.0)
        self.right_stick = (0.0, 0.0)
        self.right_stick_until = 0.0

    def hold(self, owner: str, button: str | None, now: float) -> None:
        self.release(owner, now)
        if button is None:
            return
        if button not in BUTTON_INDEX:
            raise ValueError(f"unknown Xbox button {button!r}")
        if not self._down(button, now):
            self.pressed_at[button] = now
        self.holds[owner] = button

    def release(self, owner: str, now: float) -> None:
        button = self.holds.pop(owner, None)
        if button is not None and button not in self.holds.values():
            self.until[button] = max(self.until.get(button, 0.0), self.pressed_at[button] + self.min_press_s)

    def pulse(self, button: str, now: float) -> None:
        """Press and release: down for min_press_s."""
        if button not in BUTTON_INDEX:
            raise ValueError(f"unknown Xbox button {button!r}")
        if not self._down(button, now):
            self.pressed_at[button] = now
        self.until[button] = max(self.until.get(button, 0.0), now + self.min_press_s)

    def flick_right_stick(self, x: float, y: float, seconds: float, now: float) -> None:
        self.right_stick = (x, y)
        self.right_stick_until = now + seconds

    def release_all(self) -> None:
        """Everything up and centred, immediately (shutdown, lost hub)."""
        self.holds.clear()
        self.until.clear()
        self.left_stick = self.right_stick = (0.0, 0.0)
        self.right_stick_until = 0.0

    def held_by(self, owner: str) -> str | None:
        return self.holds.get(owner)

    def _down(self, button: str, now: float) -> bool:
        return button in self.holds.values() or self.until.get(button, 0.0) > now

    def pressed(self, now: float) -> list[str]:
        return [b for b in BUTTONS if self._down(b, now)]

    def snapshot(self, now: float) -> dict:
        """The state for the page: {"axes": [lx, ly, rx, ry], "buttons": [0/1 x 17]}."""
        for button in [b for b, t in self.until.items() if t <= now]:
            del self.until[button]
        if self.right_stick_until <= now:
            self.right_stick = (0.0, 0.0)
        axes = [round(v, 3) + 0.0 for v in (*self.left_stick, *self.right_stick)]
        return {"axes": axes, "buttons": [1 if self._down(b, now) else 0 for b in BUTTONS]}


# Runs in every page and frame before the page's own scripts (CDP addScriptToEvaluateOnNewDocument).
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

# The live dashboard (dashboard.html): the LEGO controller next to what navigator.getGamepads()
# returns. Served at a made-up https address that Playwright answers itself, so it needs no server.
TESTER_URL = "https://dashboard.spike.invalid/"
DASHBOARD = Path(__file__).parent / "dashboard.html"


class ChromeGamepad:
    """
    Chrome with the virtual controller in its Gamepad API.

        async with ChromeGamepad(url) as chrome:
            await chrome.push(pad.snapshot(now))
    """

    def __init__(self, url: str, profile_dir, headless: bool = False):
        self.url = url
        self.profile_dir = profile_dir
        self.headless = headless
        self._last_state = None
        self._last_status = None

    async def __aenter__(self):
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self.context = await self._playwright.chromium.launch_persistent_context(
            self.profile_dir,
            channel="chrome",
            headless=self.headless,
            no_viewport=True,
            ignore_default_args=["--enable-automation"],
            args=["--start-maximized"],
        )
        await self.context.add_init_script(VIRTUAL_PAD_JS)
        await self.context.route(TESTER_URL, lambda route: route.fulfill(content_type="text/html",
                                                                          body=DASHBOARD.read_text()))
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
            pass  # the page is navigating; the next push retries

    async def push_status(self, status: dict) -> None:
        """Send the controller's mode and inputs to the dashboard page (ignored by other pages)."""
        if status == self._last_status or self.closed:
            return
        try:
            await self.page.evaluate("s => window.__spikeStatus && window.__spikeStatus.set(s)", status)
            self._last_status = status
        except PlaywrightError:
            pass

    async def read_back(self) -> dict | None:
        """What the page itself sees in navigator.getGamepads()[0] (for checks)."""
        return await self.page.evaluate("""() => {
            const p = navigator.getGamepads()[0];
            return p && { id: p.id, mapping: p.mapping, axes: p.axes.slice(),
                          buttons: p.buttons.map(b => b.pressed ? 1 : 0) };
        }""")


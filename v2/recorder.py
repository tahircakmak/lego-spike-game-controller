"""
Record what Chrome shows, for demos: the Xbox Remote Play game picture or the dashboard.

Uses Chrome's own screencast (DevTools protocol), so macOS Screen Recording
permission is not needed. Each frame is saved with the position of the part to keep:

    Recorder(page)                    the biggest <video> (the Remote Play game). Frames
                                      without a game video (sign-in, console list with your
                                      gamertag) are never saved.
    Recorder(page, "#stage")          an element, for example the dashboard

`bridge.py --record` uses it, and so does make_media.py; make_demo.py turns a
recording into an MP4 and a GIF. (Remade from V1's recorder.py.)
"""

import asyncio
import base64
import json
import time
from pathlib import Path

from playwright.async_api import Error as PlaywrightError

RECORDINGS = Path(__file__).parent / "recordings"

# Position of the biggest visible <video> in CSS pixels, or null when there is none.
VIDEO_BOX_JS = """() => {
    let best = null;
    for (const v of document.querySelectorAll("video")) {
        const r = v.getBoundingClientRect();
        if (v.videoWidth && r.width * r.height > (best ? best.width * best.height : 0)) {
            best = { x: r.x, y: r.y, width: r.width, height: r.height,
                     videoWidth: v.videoWidth, videoHeight: v.videoHeight };
        }
    }
    return best;
}"""

# Position of an element, in the same shape (its "video size" is its own size).
ELEMENT_BOX_JS = """(selector) => {
    const e = document.querySelector(selector);
    if (!e) return null;
    const r = e.getBoundingClientRect();
    return { x: r.x, y: r.y, width: r.width, height: r.height, videoWidth: r.width, videoHeight: r.height };
}"""


class Recorder:
    """
        recorder = Recorder(page)          # or Recorder(page, "#stage")
        await recorder.start()
        recorder.event("Force sensor pressed -> x down")   # for the captions
        folder = await recorder.stop()     # -> recordings/<time>/frames + recording.json
    """

    def __init__(self, page, selector: str | None = None, folder: Path | None = None):
        self.page = page
        self.selector = selector
        self.base = folder
        self.recording = False
        self.folder: Path | None = None

    async def start(self) -> None:
        self.folder = self.base or RECORDINGS / time.strftime("%Y%m%d-%H%M%S")
        (self.folder / "frames").mkdir(parents=True, exist_ok=True)
        self.frames: list[dict] = []
        self.events: list[dict] = []
        self.skipped = 0
        self.box = await self._box()
        self.page.on("framenavigated", self._on_navigate)
        self._cdp = await self.page.context.new_cdp_session(self.page)
        self._cdp.on("Page.screencastFrame", self._on_frame)
        await self._cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "everyNthFrame": 1})
        self._box_task = asyncio.create_task(self._track_box())
        self.recording = True

    def event(self, text: str) -> None:
        """A controller action, shown as a caption. "source -> action" is split in two."""
        if self.recording:
            source, _, action = text.partition(" -> ")
            self.events.append({"t": time.time(), "source": source, "action": action or source})

    async def stop(self) -> Path:
        self.recording = False
        self._box_task.cancel()
        self.page.remove_listener("framenavigated", self._on_navigate)
        try:
            await self._cdp.send("Page.stopScreencast")
            await self._cdp.detach()
        except PlaywrightError:
            pass  # Chrome is already gone
        info = {"frames": self.frames, "events": self.events}
        (self.folder / "recording.json").write_text(json.dumps(info, indent=1))
        return self.folder

    async def _box(self):
        try:
            if self.selector:
                return await self.page.evaluate(ELEMENT_BOX_JS, self.selector)
            return await self.page.evaluate(VIDEO_BOX_JS)
        except PlaywrightError:
            return None

    async def _track_box(self) -> None:
        # The video can move, e.g. when switching to full screen.
        while True:
            await asyncio.sleep(0.2)
            self.box = await self._box()

    def _on_navigate(self, frame) -> None:
        if frame == self.page.main_frame:
            self.box = None  # new page: record nothing until the target shows up again

    def _on_frame(self, params: dict) -> None:
        asyncio.ensure_future(self._ack(params["sessionId"]))
        if not self.recording:
            return
        if self.box is None:
            self.skipped += 1  # nothing to keep on screen: never store it
            return
        meta = params["metadata"]
        name = f"{len(self.frames):06}.jpg"
        (self.folder / "frames" / name).write_bytes(base64.b64decode(params["data"]))
        self.frames.append({
            "file": name,
            "t": meta["timestamp"],
            "viewport": [meta["deviceWidth"], meta["deviceHeight"]],
            "offset_top": meta.get("offsetTop", 0),
            "box": self.box,
        })

    async def _ack(self, session_id: int) -> None:
        try:
            await self._cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
        except PlaywrightError:
            pass

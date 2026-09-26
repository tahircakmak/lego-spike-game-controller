"""
Record the Xbox Remote Play game picture for demos.

Uses Chrome's own screencast (DevTools protocol), so macOS Screen Recording
permission is not needed. Only the game video is kept: frames are saved together
with the position of the page's <video> element, and frames without a game video
(sign-in page, console list with your gamertag) are dropped.

bridge.py --xbox starts and stops it with the R key; make_demo.py turns a
recording into an MP4 and a GIF.
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


class Recorder:
    """
        recorder = Recorder(page)
        await recorder.start()
        recorder.event("FORCE SENSOR pressed", "Melee attack")  # for the captions
        await recorder.stop()   # -> recordings/<time>/frames + recording.json
    """

    def __init__(self, page):
        self.page = page
        self.recording = False
        self.folder: Path | None = None

    async def start(self) -> None:
        self.folder = RECORDINGS / time.strftime("%Y%m%d-%H%M%S")
        (self.folder / "frames").mkdir(parents=True)
        self.frames: list[dict] = []
        self.events: list[dict] = []
        self.skipped = 0
        self.box = await self._video_box()
        self.page.on("framenavigated", self._on_navigate)
        self._cdp = await self.page.context.new_cdp_session(self.page)
        self._cdp.on("Page.screencastFrame", self._on_frame)
        await self._cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 90, "everyNthFrame": 1})
        self._box_task = asyncio.create_task(self._track_video_box())
        self.recording = True

    def event(self, source: str, action: str) -> None:
        if self.recording:
            self.events.append({"t": time.time(), "source": source, "action": action})

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

    async def _video_box(self):
        try:
            return await self.page.evaluate(VIDEO_BOX_JS)
        except PlaywrightError:
            return None

    async def _track_video_box(self) -> None:
        # The video can move, e.g. when switching to full screen.
        while True:
            await asyncio.sleep(0.2)
            self.box = await self._video_box()

    def _on_navigate(self, frame) -> None:
        if frame == self.page.main_frame:
            self.box = None  # new page: record nothing until a game video shows up again

    def _on_frame(self, params: dict) -> None:
        asyncio.ensure_future(self._ack(params["sessionId"]))
        if not self.recording:
            return
        if self.box is None:
            self.skipped += 1  # no game video on screen: never store it
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

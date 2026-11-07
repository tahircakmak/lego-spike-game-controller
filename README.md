# LEGO SPIKE controller for Minecraft Dungeons

LEGO SPIKE Prime game controllers that play Minecraft Dungeons on an Xbox through Xbox Remote Play in Chrome. The controller shows up in Chrome as a virtual Xbox controller. Built by [@yusufkeremcakmak](https://github.com/yusufkeremcakmak) (controller design and LEGO build) and [@tahircakmak](https://github.com/tahircakmak) (software).

V1, V2 and V2.1 each have software written for one controller design. The **custom software** works with any design, any game, and MINDSTORMS Robot Inventor hubs too: **[`customsoftwarev1.1/`](customsoftwarev1.1/README.md) is the one to use for new builds.**

| Folder | Project |
|---|---|
| [`v1/`](v1/README.md) | The first controller: hub tilt to move, force sensor, a motorised trigger and an artifact dial. Has a simulator, demo recording, and [HOW_IT_WORKS.md](v1/HOW_IT_WORKS.md). [Building tutorial on YouTube](https://www.youtube.com/watch?v=3bgCyz23Tdw&t=104s) by [@yusufkeremcakmak](https://github.com/yusufkeremcakmak). |
| [`v2/`](v2/README.md) | The V2 controller for Minecraft Dungeons II: gyro gestures, force sensor, two motorised buttons, and two rear shoulder triggers on one large motor. It has GAMEPLAY / INVENTORY and MOVING / STANDING modes. [Building tutorial on YouTube](https://www.youtube.com/watch?v=_lUhxB4JkKw) by [@yusufkeremcakmak](https://github.com/yusufkeremcakmak). |
| [`v2.1/`](v2.1/README.md) | The V2.1 controller for Minecraft Dungeons II: two motorised buttons (melee, jump), two back triggers on their own large motors (RT ranged, LT), a force sensor (LB), a distance sensor that fires all artifacts, and gyro steering with walking toggled by the hub button. |
| [`customsoftwarev1/`](customsoftwarev1/README.md) | **Custom software V1: one program for every design.** It detects what is plugged into the hub, calibrates it, and lets you build the controls in a control panel: any LEGO input to any Xbox input (several at once), activation areas and deadzones for motors and the gyro, stick axes and 8 directions, variables, systems and remaps. Saved as loadouts. |
| [`customsoftwarev1.1/`](customsoftwarev1.1/README.md) | **Custom software V1.1**: everything in V1, plus **return to rest** (a motor you stop moving slowly turns back to rest, speed adjustable, 0.5× by default) and support for **MINDSTORMS Robot Inventor hubs** (HubOS 2, over Bluetooth or the USB cable). Includes a loadout for a MINDSTORMS steering wheel rig. |

## Setup

All the projects share one Python virtual environment at the root of the repository. This installs what every folder needs:

```bash
python3 -m venv .venv
.venv/bin/pip install -r v1/requirements.txt -r v2/requirements.txt -r customsoftwarev1.1/requirements.txt
```

`customsoftwarev1.1/requirements.txt` on its own is enough for V2.1, custom software V1 and V1.1.

Run each project from its own folder, for example:

```bash
cd customsoftwarev1.1 && ../.venv/bin/python app.py
```

Each folder's README explains its own commands.

## License

MIT, see [LICENSE](LICENSE). `spike/protocol.py` (in every project folder) is modified from the LEGO Group's SPIKE Prime protocol example (Apache 2.0); the details are in LICENSE. Not affiliated with the LEGO Group or Microsoft.

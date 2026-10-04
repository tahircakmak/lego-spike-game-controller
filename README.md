# LEGO SPIKE controller for Minecraft Dungeons

A LEGO SPIKE Prime game controller that plays Minecraft Dungeons on an Xbox through Xbox Remote Play in Chrome. The controller shows up in Chrome as a virtual Xbox controller. Built by [@yusufkeremcakmak](https://github.com/yusufkeremcakmak) (controller design and LEGO build) and [@tahircakmak](https://github.com/tahircakmak) (software).

| Folder | Project |
|---|---|
| [`v1/`](v1/README.md) | The first controller: hub tilt to move, force sensor, a motorised trigger and an artifact dial. Has a simulator, demo recording, and [HOW_IT_WORKS.md](v1/HOW_IT_WORKS.md). |
| [`v2/`](v2/README.md) | The V2 controller for Minecraft Dungeons II: gyro gestures, force sensor, two motorised buttons, and two rear shoulder triggers on one large motor. It has GAMEPLAY / INVENTORY and MOVING / STANDING modes. [Building tutorial on YouTube](https://www.youtube.com/watch?v=_lUhxB4JkKw) by [@yusufkeremcakmak](https://github.com/yusufkeremcakmak). |

Both projects use one virtual environment at the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -r v2/requirements.txt   # and/or v1/requirements.txt
```

Run each project from its own folder, for example `cd v2 && ../.venv/bin/python bridge.py`.

## License

MIT, see [LICENSE](LICENSE). `spike/protocol.py` (in both projects) is modified from the LEGO Group's SPIKE Prime protocol example (Apache 2.0); the details are in LICENSE. Not affiliated with the LEGO Group or Microsoft.

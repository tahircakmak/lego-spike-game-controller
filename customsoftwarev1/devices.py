"""
What is plugged into the hub, and how its readings become source values.

    detect_ports(hub)      asks the hub's sensor stream what is on ports A-F
    hub_source(...)        hub_program.py with its CONFIG block filled in for those ports
    Devices                turns the hub program's lines into source values, using the
                           calibration (zero points, directions, deadzones, ...)

Sources (the names bindings use):

    motor:A               angle in degrees from the calibrated rest position
    force:C               0-100
    distance:D            centimetres (nothing seen = 250 cm, or 0 when calibration found
                          that your hand covering the sensor reads as "nothing")
    color:E               the colour number (-1 none, 0 black, 1 magenta, 2 purple, 3 blue,
                          4 azure, 5 turquoise, 6 green, 7 yellow, 8 orange, 9 red, 10 white)
    color:E:reflection    reflected light, 0-100
    gyro:pitch / gyro:roll / gyro:yaw
                          degrees from the calibrated "level" position
    gyro:right            tilt to the right, like a steering wheel (negative = left)
    gyro:forward          tilt away from you (negative = towards you)
    hub:left / hub:right  the hub's buttons, 1 while pressed
"""

from __future__ import annotations

import asyncio
import copy
import math
from pathlib import Path

from spike import protocol as p

HERE = Path(__file__).parent
HUB_PROGRAM = HERE / "hub_program.py"
PORTS = "ABCDEF"
NOTHING_FAR_CM = 250
COLOR_NAMES = {-1: "none", 0: "black", 1: "magenta", 2: "purple", 3: "blue", 4: "azure", 5: "turquoise",
               6: "green", 7: "yellow", 8: "orange", 9: "red", 10: "white"}
GYRO_SOURCES = ("gyro:right", "gyro:forward", "gyro:pitch", "gyro:roll", "gyro:yaw")
DEMO_PORTS = {"A": {"kind": "motor", "type": "medium"}, "B": {"kind": "motor", "type": "large"},
              "C": {"kind": "force"}, "D": {"kind": "distance"}, "E": {"kind": "motor", "type": "medium"},
              "F": {"kind": "motor", "type": "large"}}


def wrap180(angle: float) -> float:
    return (angle + 180) % 360 - 180


# ---- ports ---------------------------------------------------------------------------

def kind_of(reading) -> dict | None:
    if isinstance(reading, p.Motor):
        return {"kind": "motor", "type": reading.type}
    if isinstance(reading, p.ForceSensor):
        return {"kind": "force"}
    if isinstance(reading, p.DistanceSensor):
        return {"kind": "distance"}
    if isinstance(reading, p.ColorSensor):
        return {"kind": "color"}
    if isinstance(reading, p.ColorMatrix):
        return {"kind": "unsupported", "type": "3x3 colour light matrix"}
    return None


def ports_from_readings(readings) -> dict:
    """The port devices in one sensor-stream message. Battery, Imu and Display readings have no port."""
    found = {}
    for r in readings:
        port = getattr(r, "port", None)
        if isinstance(port, str) and port in PORTS and (k := kind_of(r)):
            found[port] = k
    return found


async def detect_ports(hub) -> dict:
    """Port letter -> {"kind": motor/force/distance/color/unsupported, "type": ...}, from the sensor stream."""
    found: dict[str, dict] = {}
    seen = asyncio.Event()

    def on_sensors(readings):
        found.update(ports_from_readings(readings))
        seen.set()

    await hub.start_sensor_stream(on_sensors, interval_ms=50)
    try:
        await asyncio.wait_for(seen.wait(), timeout=3)
        await asyncio.sleep(0.25)
    except asyncio.TimeoutError:
        raise RuntimeError("The hub didn't send its sensor readings, so the ports couldn't be detected. "
                           "Press Connect to try again.") from None
    finally:
        if hub.connected:
            await hub.stop_sensor_stream()
    return dict(sorted(found.items()))


def describe_port(port: str, info: dict) -> str:
    if info["kind"] == "motor":
        return f"{info.get('type', '')} motor".strip()
    return {"force": "force sensor", "distance": "distance sensor", "color": "colour sensor"}.get(
        info["kind"], info.get("type", "unsupported device"))


def ports_summary(ports: dict) -> str:
    return ", ".join(f"{port}: {describe_port(port, info)}" for port, info in ports.items()) or "nothing plugged in"


# ---- calibration defaults --------------------------------------------------------------

def default_calibration() -> dict:
    return {"ports": {}, "motors": {}, "force": {}, "distance": {}, "color": {},
            "gyro": {"level": [0, 0, 0], "right": None, "forward": None}, "deadzones": {}}


def complete_calibration(calibration: dict | None, ports: dict) -> dict:
    """Fill in defaults for every detected device that has no calibration yet."""
    cal = copy.deepcopy(calibration) if calibration else default_calibration()
    for key, value in default_calibration().items():
        cal.setdefault(key, value)
    for port, info in ports.items():
        if info["kind"] == "motor":
            m = cal["motors"].setdefault(port, {})
            for key, value in {"mode": "absolute", "zero": None, "min": -90, "max": 90, "invert": False,
                               "spring": False, "spring_strength": 100, "spring_max": 3000}.items():
                m.setdefault(key, value)
        elif info["kind"] == "force":
            f = cal["force"].setdefault(port, {})
            for key, value in {"rest": 0, "max": 100, "press": 10}.items():
                f.setdefault(key, value)
        elif info["kind"] == "distance":
            d = cal["distance"].setdefault(port, {})
            for key, value in {"hand_cm": None, "away_cm": None, "near_cm": 8, "nothing_means": "far"}.items():
                d.setdefault(key, value)
        elif info["kind"] == "color":
            cal["color"].setdefault(port, {}).setdefault("ambient", 0)
    return cal


def needs_calibration(calibration: dict | None, ports: dict) -> bool:
    """True when there is no calibration, or the devices on the ports changed since."""
    if not calibration:
        return True
    return {p: i["kind"] for p, i in calibration.get("ports", {}).items()} != {p: i["kind"] for p, i in ports.items()}


# ---- the hub program ---------------------------------------------------------------------

def hub_source(ports: dict, calibration: dict, settings: dict | None = None) -> str:
    """hub_program.py with its CONFIG block filled in for these ports and this calibration."""
    s = {"GYRO_REPORT_MS": 20, "DISTANCE_REPORT_MS": 30, "HUB_LOOP_MS": 10, "HEARTBEAT_MS": 500,
         "SPRING_DEADBAND_DEG": 2, "SPRING_LIMIT_DEG": 90, **(settings or {})}
    motors = []
    for port, info in ports.items():
        if info["kind"] != "motor":
            continue
        m = calibration["motors"][port]
        motors.append((port, m["mode"] == "absolute", m["zero"], bool(m["spring"]),
                       int(m["spring_strength"]), int(m["spring_max"])))
    values = {
        "MOTORS": motors,
        "FORCE_PORTS": [port for port, i in ports.items() if i["kind"] == "force"],
        "DISTANCE_PORTS": [port for port, i in ports.items() if i["kind"] == "distance"],
        "COLOR_PORTS": [port for port, i in ports.items() if i["kind"] == "color"],
        **s,
    }
    block = "\n".join(f"{key} = {value!r}" for key, value in values.items())
    source = HUB_PROGRAM.read_text()
    start, end = source.index("# <config>"), source.index("# </config>")
    head = source[start:].split("\n", 1)[0]
    return source[:start] + head + "\n" + block + "\n" + source[end:]


# ---- readings -> source values ---------------------------------------------------------------

class Devices:
    """
    Keeps the newest raw reading of everything and computes the source values from it.

        devices.handle_line("A B -31")   # from the hub program
        devices.values()                 # {"motor:B": -31.0, ...}
    """

    def __init__(self, ports: dict, calibration: dict):
        self.ports = ports
        self.calibration = complete_calibration(calibration, ports)
        self.raw: dict = {}
        self.simulated: dict[str, float] = {}  # --no-hub: values set from the control panel
        self.auto_zero: dict[str, int] = {}    # motors without a calibrated zero: the first reading

    def set_ports(self, ports: dict) -> None:
        self.ports = ports
        self.calibration = complete_calibration(self.calibration, ports)
        self.raw = {k: v for k, v in self.raw.items() if k in ("tilt", "L", "R")}

    def set_calibration(self, calibration: dict) -> None:
        self.calibration = complete_calibration(calibration, self.ports)
        self.auto_zero.clear()

    # ---- hub lines -------------------------------------------------------------

    def handle_line(self, line: str) -> str | None:
        """Store a reading. Returns the line's kind (A F D C T B H READY W), or None if it isn't a reading."""
        kind, *v = line.split()
        try:
            if kind == "A":
                self.raw[v[0]] = int(v[1])
                if self.calibration["motors"].get(v[0], {}).get("zero") is None:
                    self.auto_zero.setdefault(v[0], int(v[1]))
            elif kind in ("F", "D"):
                self.raw[v[0]] = int(v[1])
            elif kind == "C":
                self.raw[v[0]] = (int(v[1]), int(v[2]))
            elif kind == "T":
                self.raw["tilt"] = (int(v[0]), int(v[1]), int(v[2]))
            elif kind == "B":
                self.raw[v[0]] = int(v[1])
            elif kind not in ("H", "READY", "W"):
                return None
        except (IndexError, ValueError):
            return None
        return kind

    # ---- source values ---------------------------------------------------------

    def catalog(self) -> list[dict]:
        """Every source the control panel can offer, with its unit and a sensible slider range."""
        out = []
        for port, info in self.ports.items():
            k = info["kind"]
            label = f"{describe_port(port, info)} on {port}"
            if k == "motor":
                m = self.calibration["motors"][port]
                out.append({"source": f"motor:{port}", "label": label, "unit": "°", "min": -180, "max": 180,
                            "range": [m["min"], m["max"]], "kind": "motor", "port": port})
            elif k == "force":
                out.append({"source": f"force:{port}", "label": label, "unit": "", "min": 0, "max": 100,
                            "range": [0, 100], "kind": "force", "port": port})
            elif k == "distance":
                out.append({"source": f"distance:{port}", "label": label, "unit": "cm", "min": 0,
                            "max": NOTHING_FAR_CM, "range": [0, 50], "kind": "distance", "port": port})
            elif k == "color":
                out.append({"source": f"color:{port}", "label": label + " (colour)", "unit": "", "min": -1,
                            "max": 10, "range": [-1, 10], "kind": "color", "port": port})
                out.append({"source": f"color:{port}:reflection", "label": label + " (reflection)", "unit": "%",
                            "min": 0, "max": 100, "range": [0, 100], "kind": "reflection", "port": port})
        for source, label, lim in (("gyro:right", "hub tilt right / left", 90),
                                   ("gyro:forward", "hub tilt forward / back", 90),
                                   ("gyro:pitch", "hub pitch", 90), ("gyro:roll", "hub roll", 180),
                                   ("gyro:yaw", "hub yaw (turn)", 180)):
            out.append({"source": source, "label": label, "unit": "°", "min": -lim, "max": lim,
                        "range": [-45, 45], "kind": "gyro", "port": "hub"})
        for side in ("left", "right"):
            out.append({"source": f"hub:{side}", "label": f"{side} hub button", "unit": "", "min": 0, "max": 1,
                        "range": [0, 1], "kind": "button", "port": "hub"})
        return out

    def ranges(self) -> dict:
        """Source -> (min, max) an axis binding uses when it has no range of its own."""
        return {s["source"]: tuple(s["range"]) for s in self.catalog()}

    def motor_angle(self, port: str, raw: int) -> float:
        m = self.calibration["motors"][port]
        zero = m["zero"] if m["zero"] is not None else self.auto_zero.get(port, 0)
        angle = wrap180(raw - zero) if m["mode"] == "absolute" else raw - zero
        return -angle if m["invert"] else angle

    def tilt(self) -> dict:
        """pitch / roll / yaw from level, and the calibrated right / forward tilts."""
        if "tilt" not in self.raw:
            return {}
        yaw, pitch, roll = self.raw["tilt"]
        y0, p0, r0 = self.calibration["gyro"]["level"]
        dp, dr = pitch - p0, roll - r0
        out = {"gyro:pitch": dp, "gyro:roll": dr, "gyro:yaw": wrap180(yaw - y0)}
        for name, fallback in (("right", dr), ("forward", dp)):
            vector = self.calibration["gyro"].get(name)
            if vector and math.hypot(*vector) > 0:
                out[f"gyro:{name}"] = round((dp * vector[0] + dr * vector[1]) / math.hypot(*vector), 1)
            else:
                out[f"gyro:{name}"] = fallback
        return out

    def values(self) -> dict:
        """Every source's current value (None until the hub reported it), deadzones applied."""
        out: dict = {}
        for port, info in self.ports.items():
            raw = self.raw.get(port)
            k = info["kind"]
            if k == "motor":
                out[f"motor:{port}"] = None if raw is None else self.motor_angle(port, raw)
            elif k == "force":
                out[f"force:{port}"] = raw
            elif k == "distance":
                if raw is None:
                    out[f"distance:{port}"] = None
                elif raw < 0:
                    near = self.calibration["distance"][port]["nothing_means"] == "near"
                    out[f"distance:{port}"] = 0 if near else NOTHING_FAR_CM
                else:
                    out[f"distance:{port}"] = raw / 10
            elif k == "color":
                out[f"color:{port}"] = None if raw is None else raw[0]
                out[f"color:{port}:reflection"] = None if raw is None else raw[1]
        out.update({s: None for s in GYRO_SOURCES})
        out.update(self.tilt())
        out["hub:left"] = self.raw.get("L", 0)
        out["hub:right"] = self.raw.get("R", 0)
        out.update(self.simulated)
        for source, (lo, hi) in self.calibration["deadzones"].items():
            if out.get(source) is not None and lo <= out[source] <= hi:
                out[source] = 0
        return out

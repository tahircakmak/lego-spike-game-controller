"""
The calibration wizard. Its steps are made from whatever devices were detected:

    1. rest       every motor at rest, hands off, controller level: motor zero points,
                  force sensor resting value, distance "away" reading, gyro level
    2. per motor  move it as far as it goes both ways: its range (min / max)
    3. per force  press it as hard as you will: its maximum and a press threshold
    4. per distance sensor
                  hand over it: the "near" distance (and whether a covered sensor reads nothing)
    5. gyro       tilt right, then tilt forward: which way "right" and "forward" are,
                  however the hub sits in the build

Each step is captured with the control panel's Capture button or the RIGHT hub button.
Every value ends up in calibration.json and can be changed by hand in the control panel.
"""

from __future__ import annotations

import math

from devices import complete_calibration, describe_port, wrap180


def distance_settings(hand_mm: int, away_mm: int) -> dict:
    """Distance sensor settings from two readings: the hand over the sensor, and no hand."""
    if hand_mm >= 0:
        near_cm = round((hand_mm + max(20, hand_mm // 2)) / 10, 1)
        note = None
        if 0 <= away_mm <= hand_mm + 45:
            note = "the readings with and without your hand are very close: point the sensor at open space"
        return {"hand_cm": hand_mm / 10, "away_cm": None if away_mm < 0 else away_mm / 10, "near_cm": near_cm,
                "nothing_means": "far", "note": note}
    if away_mm < 0:
        return {"hand_cm": None, "away_cm": None, "near_cm": 8, "nothing_means": "far",
                "note": "the sensor saw nothing with or without your hand: hold your hand a little higher"}
    # Right on top of the sensor it reads nothing, and something further away without the hand:
    # "nothing" is the hand.
    return {"hand_cm": 0, "away_cm": away_mm / 10, "near_cm": 5, "nothing_means": "near",
            "note": "your hand covers the sensor, so 'sees nothing' counts as 0 cm"}


class Wizard:
    def __init__(self, ports: dict, calibration: dict, raw: dict):
        """raw: Devices.raw, read live (the newest hub readings)."""
        self.ports = ports
        self.cal = complete_calibration(calibration, ports)
        self.raw = raw
        self.steps = self._make_steps()
        self.index = 0
        self.messages: list[str] = []
        self.done = False
        self._begin()

    def _make_steps(self) -> list[dict]:
        steps = [{"id": "rest", "title": "Rest position",
                  "text": "Put every motor at rest, take your hands off the sensors, and hold the controller "
                          "level, the way you play."}]
        for port, info in self.ports.items():
            what = f"{describe_port(port, info)} on port {port}"
            if info["kind"] == "motor":
                steps.append({"id": "motor", "port": port, "title": f"Range of the {what}",
                              "text": f"Move the {what} as far as it goes one way, then as far as it goes the "
                                      "other way, then back to rest."})
            elif info["kind"] == "force":
                steps.append({"id": "force", "port": port, "title": f"Force sensor {port}",
                              "text": f"Press the force sensor on port {port} as hard as you will while playing, "
                                      "then let go."})
            elif info["kind"] == "distance":
                steps.append({"id": "distance", "port": port, "title": f"Distance sensor {port}",
                              "text": f"Hold your hand over the distance sensor on port {port}, as close as you "
                                      "will while playing, and keep it there."})
        steps.append({"id": "gyro_right", "title": "Tilt right",
                      "text": "Tilt the controller to the RIGHT, like turning a steering wheel, and hold it there."})
        steps.append({"id": "gyro_forward", "title": "Tilt forward",
                      "text": "Tilt the top of the controller AWAY from you, and hold it there."})
        return steps

    @property
    def step(self) -> dict | None:
        return None if self.done else self.steps[self.index]

    def _begin(self) -> None:
        self.low: dict = {}
        self.high: dict = {}

    def _offset(self, port: str) -> float | None:
        raw = self.raw.get(port)
        if raw is None:
            return None
        m = self.cal["motors"][port]
        zero = m["zero"] or 0
        return wrap180(raw - zero) if m["mode"] == "absolute" else raw - zero

    def feed(self) -> None:
        """Call after new readings: tracks the lowest / highest value during the current step."""
        step = self.step
        if step is None:
            return
        if step["id"] == "motor":
            value = self._offset(step["port"])
        elif step["id"] == "force":
            value = self.raw.get(step["port"])
        else:
            return
        if value is not None:
            self.low[step["id"]] = min(self.low.get(step["id"], value), value)
            self.high[step["id"]] = max(self.high.get(step["id"], value), value)

    def live(self) -> str:
        """What the current step measures right now, for the control panel."""
        step = self.step
        if step is None:
            return ""
        if step["id"] == "motor":
            if "motor" not in self.low:
                return "waiting for the motor to move..."
            return f"now {self._offset(step['port']):.0f}°, range {self.low['motor']:.0f}° to {self.high['motor']:.0f}°"
        if step["id"] == "force":
            return f"now {self.raw.get(step['port'], 0)}, strongest {self.high.get('force', 0)}"
        if step["id"] == "distance":
            mm = self.raw.get(step["port"])
            return "no reading yet" if mm is None else "sees nothing" if mm < 0 else f"{mm / 10:.1f} cm"
        if "tilt" in self.raw:
            _, pitch, roll = self.raw["tilt"]
            level = self.cal["gyro"]["level"]
            return f"pitch {pitch - level[1]:+d}°, roll {roll - level[2]:+d}° from level"
        return "no tilt reading yet"

    def capture(self) -> str:
        """Use the current readings for this step and move on. Returns what was measured."""
        step = self.step
        if step is None:
            return ""
        port = step.get("port")
        if step["id"] == "rest":
            said = []
            for p, info in self.ports.items():
                raw = self.raw.get(p)
                if raw is None:
                    continue
                if info["kind"] == "motor":
                    m = self.cal["motors"][p]
                    m["zero"] = raw if m["mode"] == "absolute" else 0
                    said.append(f"motor {p} rest at {raw}°")
                elif info["kind"] == "force":
                    self.cal["force"][p]["rest"] = raw
                elif info["kind"] == "distance":
                    self.cal["distance"][p]["away_cm"] = None if raw < 0 else raw / 10
                elif info["kind"] == "color":
                    self.cal["color"][p]["ambient"] = raw[1]
            if "tilt" in self.raw:
                self.cal["gyro"]["level"] = list(self.raw["tilt"])
                said.append("level tilt saved")
            result = ", ".join(said) or "no readings yet"
        elif step["id"] == "motor":
            low, high = self.low.get("motor", 0), self.high.get("motor", 0)
            if high - low < 10:
                result = f"motor {port} hardly moved ({low:.0f}° to {high:.0f}°): kept the old range"
            else:
                self.cal["motors"][port].update({"min": round(low), "max": round(high)})
                result = f"motor {port}: {low:.0f}° to {high:.0f}°"
        elif step["id"] == "force":
            f = self.cal["force"][port]
            strongest = self.high.get("force", 0)
            if strongest - f["rest"] < 10:
                result = f"force sensor {port} was hardly pressed: kept the old settings"
            else:
                f["max"] = strongest
                f["press"] = round(f["rest"] + max(5, (strongest - f["rest"]) * 0.3))
                result = f"force sensor {port}: strongest {strongest}, counts as pressed from {f['press']}"
        elif step["id"] == "distance":
            d = self.cal["distance"][port]
            hand = self.raw.get(port, -1)
            away = -1 if d["away_cm"] is None else round(d["away_cm"] * 10)
            settings = distance_settings(hand, away)
            note = settings.pop("note")
            d.update(settings)
            result = (f"distance sensor {port}: hand at "
                      f"{'nothing (covered)' if hand < 0 else f'{hand / 10:.1f} cm'}, near below {d['near_cm']} cm"
                      + (f" ({note})" if note else ""))
        else:
            name = step["id"].split("_")[1]
            if "tilt" not in self.raw:
                result = "no tilt reading: skipped"
            else:
                _, pitch, roll = self.raw["tilt"]
                level = self.cal["gyro"]["level"]
                vector = [pitch - level[1], roll - level[2]]
                if math.hypot(*vector) < 10:
                    result = f"that tilt was too small to measure ({math.hypot(*vector):.0f}°): skipped"
                else:
                    self.cal["gyro"][name] = vector
                    result = f"{name} tilt: {math.hypot(*vector):.0f}°"
        self.messages.append(result)
        self._next()
        return result

    def skip(self) -> None:
        if self.step:
            self.messages.append(f"{self.step['title']}: skipped")
            self._next()

    def _next(self) -> None:
        self.index += 1
        self._begin()
        if self.index >= len(self.steps):
            self.done = True
            self.cal["ports"] = {p: dict(i) for p, i in self.ports.items()}

    def state(self) -> dict:
        step = self.step
        return {"active": not self.done, "index": self.index, "total": len(self.steps),
                "title": step["title"] if step else "Done", "text": step["text"] if step else "",
                "live": self.live(), "messages": self.messages[-8:]}


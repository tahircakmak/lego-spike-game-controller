"""
The binding engine: LEGO source values in, virtual Xbox controller out, driven by a loadout.

A loadout (a JSON file in loadouts/) holds:

    variables   numbers with a start value, a minimum and a maximum (optionally wrapping)
    systems     named groups of bindings that can be switched on and off, or that only
                work while their conditions are true (for example "mode = 1")
    bindings    each one reads a source and does something:
                  zone    an activation area: while the source is above / below / between /
                          outside / at its numbers, the binding is ACTIVE and runs its actions
                  axis    the source moves one stick axis (or LT / RT), with a deadzone
                  stick   two sources move a whole stick (for example the gyro), with a round
                          deadzone and optional snapping to 4 or 8 directions

Sources are names like "motor:A", "force:C", "distance:D", "gyro:right", "hub:left" and
"var:ammo" (variables are sources too, so "do something when ammo is above 5" is just a
zone binding on var:ammo). devices.py turns the hub's readings into these values.

Zone actions:
    hold        hold Xbox outputs while active (several at once: ["y", "b", "rb"])
    tap         press outputs once (when it activates or deactivates; repeat_ms repeats it)
    toggle      each activation latches the outputs on / off
    var_add     add an amount to a variable (repeat_ms keeps adding while active)
    var_set     set a variable
    var_reset   put a variable back to its start value
    remap       while active, other bindings' output X comes out as Y instead
                ("dpad_up" -> "y", or a whole stick: "ls" -> "rs")
    system      switch a system on, off, or toggle it

Xbox outputs:
    buttons     a b x y lb rb lt rt view menu ls_click rs_click
                dpad_up dpad_down dpad_left dpad_right xbox
    directions  ls:n ls:ne ls:e ls:se ls:s ls:sw ls:w ls:nw   (rs:... for the right stick)
    axes        ls:x ls:y rs:x rs:y (+x = east, +y = north), lt rt (0..1)

Pure Python, no I/O: app.py feeds source values in and sends snapshot() to Chrome.
"""

from __future__ import annotations

import copy
import math
import re
from collections import deque

# W3C "standard" gamepad layout: index = position in Gamepad.buttons.
BUTTONS = ["a", "b", "x", "y", "lb", "rb", "lt", "rt", "view", "menu", "ls_click", "rs_click",
           "dpad_up", "dpad_down", "dpad_left", "dpad_right", "xbox"]
BUTTON_INDEX = {name: i for i, name in enumerate(BUTTONS)}
TRIGGERS = ("lt", "rt")
STICKS = ("ls", "rs")
_D = math.sqrt(0.5)
DIRECTIONS = {"n": (0.0, 1.0), "ne": (_D, _D), "e": (1.0, 0.0), "se": (_D, -_D),
              "s": (0.0, -1.0), "sw": (-_D, -_D), "w": (-1.0, 0.0), "nw": (-_D, _D)}
DIRECTION_OUTPUTS = [f"{stick}:{d}" for stick in STICKS for d in DIRECTIONS]
AXIS_OUTPUTS = ["ls:x", "ls:y", "rs:x", "rs:y", "lt", "rt"]
DIGITAL_OUTPUTS = BUTTONS + DIRECTION_OUTPUTS
ALL_OUTPUTS = DIGITAL_OUTPUTS + ["ls:x", "ls:y", "rs:x", "rs:y"]
REMAP_NAMES = ALL_OUTPUTS + list(STICKS)  # "ls" -> "rs" moves a whole stick

ZONE_MODES = ("above", "below", "between", "outside", "at")
KINDS = ("zone", "axis", "stick")
ACTION_TYPES = ("hold", "tap", "toggle", "var_add", "var_set", "var_reset", "remap", "system")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ---- loadouts ------------------------------------------------------------------------

def empty_loadout(name: str = "New loadout") -> dict:
    return {"name": name, "description": "", "settings": {"min_press_ms": 100},
            "variables": [], "systems": [{"id": "main", "name": "Main", "enabled": True, "conditions": []}],
            "bindings": []}


def _num(value, what: str, allow_none: bool = False):
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{what} must be a number, got {value!r}")
    return value


def _condition(c: dict, where: str) -> dict:
    out = {"source": str(c.get("source", "")), "mode": c.get("mode", "above"),
           "a": _num(c.get("a", 0), f"{where}: a"), "b": _num(c.get("b"), f"{where}: b", allow_none=True)}
    if out["mode"] not in ZONE_MODES:
        raise ValueError(f"{where}: mode must be one of {', '.join(ZONE_MODES)}")
    if out["mode"] in ("between", "outside") and out["b"] is None:
        raise ValueError(f"{where}: '{out['mode']}' needs two numbers (a and b)")
    return out


def _outputs(names, where: str, allowed=DIGITAL_OUTPUTS) -> list[str]:
    if isinstance(names, str):
        names = [names]
    for name in names:
        if name not in allowed:
            raise ValueError(f"{where}: {name!r} is not an Xbox output")
    return list(names)


def normalize_loadout(data: dict) -> dict:
    """Check a loadout and fill in every default. Raises ValueError with a readable message."""
    if not isinstance(data, dict):
        raise ValueError("a loadout must be a JSON object")
    out = empty_loadout(str(data.get("name") or "Untitled"))
    out["description"] = str(data.get("description", ""))
    out["settings"]["min_press_ms"] = _num(data.get("settings", {}).get("min_press_ms", 100), "min_press_ms")

    names = set()
    out["variables"] = []
    for v in data.get("variables", []):
        name = str(v.get("name", ""))
        if not _NAME.match(name):
            raise ValueError(f"variable name {name!r}: use letters, digits and _ (not starting with a digit)")
        if name in names:
            raise ValueError(f"two variables are called {name!r}")
        names.add(name)
        lo, hi = _num(v.get("min"), f"{name}.min", True), _num(v.get("max"), f"{name}.max", True)
        if lo is not None and hi is not None and lo > hi:
            raise ValueError(f"variable {name}: min is bigger than max")
        initial = _num(v.get("initial", 0), f"{name}.initial")
        out["variables"].append({"name": name, "initial": initial, "min": lo, "max": hi,
                                 "wrap": bool(v.get("wrap", False))})

    systems = data.get("systems") or out["systems"]
    out["systems"] = []
    for s in systems:
        sid = str(s.get("id") or s.get("name") or "")
        if not sid:
            raise ValueError("every system needs an id")
        out["systems"].append({"id": sid, "name": str(s.get("name") or sid), "enabled": bool(s.get("enabled", True)),
                               "conditions": [_condition(c, f"system {sid}") for c in s.get("conditions", [])]})
    system_ids = {s["id"] for s in out["systems"]}

    out["bindings"] = []
    ids = set()
    for i, b in enumerate(data.get("bindings", [])):
        bid = str(b.get("id") or f"b{i + 1}")
        while bid in ids:
            bid += "_"
        ids.add(bid)
        where = f"binding {b.get('name') or bid}"
        kind = b.get("kind", "zone")
        if kind not in KINDS:
            raise ValueError(f"{where}: kind must be one of {', '.join(KINDS)}")
        system = str(b.get("system") or out["systems"][0]["id"])
        if system not in system_ids:
            raise ValueError(f"{where}: there is no system {system!r}")
        nb = {"id": bid, "name": str(b.get("name") or bid), "system": system, "kind": kind,
              "enabled": bool(b.get("enabled", True)),
              "conditions": [_condition(c, where) for c in b.get("conditions", [])]}
        if kind == "zone":
            z = b.get("zone", {})
            nb["source"] = str(b.get("source", ""))
            nb["zone"] = {**_condition({"source": nb["source"], **z}, where),
                          "hysteresis": abs(_num(z.get("hysteresis", 0), f"{where}: hysteresis"))}
            del nb["zone"]["source"]
            nb["actions"] = [_action(a, where, names, system_ids) for a in b.get("actions", [])]
        elif kind == "axis":
            nb["source"] = str(b.get("source", ""))
            nb["output"] = b.get("output", "ls:x")
            if nb["output"] not in AXIS_OUTPUTS:
                raise ValueError(f"{where}: output must be one of {', '.join(AXIS_OUTPUTS)}")
            dz = b.get("deadzone") or [0, 0]
            nb["deadzone"] = sorted([_num(dz[0], f"{where}: deadzone"), _num(dz[1], f"{where}: deadzone")])
            rng = b.get("range")
            nb["range"] = None if rng is None else [_num(rng[0], f"{where}: range"), _num(rng[1], f"{where}: range")]
            nb["invert"] = bool(b.get("invert", False))
        else:
            nb["x_source"] = str(b.get("x_source", ""))
            nb["y_source"] = str(b.get("y_source", ""))
            nb["output"] = b.get("output", "ls")
            if nb["output"] not in STICKS:
                raise ValueError(f"{where}: output must be 'ls' or 'rs'")
            nb["deadzone"] = abs(_num(b.get("deadzone", 5), f"{where}: deadzone"))
            nb["full"] = abs(_num(b.get("full", 30), f"{where}: full"))
            if nb["full"] <= nb["deadzone"]:
                raise ValueError(f"{where}: 'full' must be bigger than the deadzone")
            nb["invert_x"] = bool(b.get("invert_x", False))
            nb["invert_y"] = bool(b.get("invert_y", False))
            nb["directions"] = int(b.get("directions", 0))
            if nb["directions"] not in (0, 4, 8):
                raise ValueError(f"{where}: directions must be 0 (smooth), 4 or 8")
        out["bindings"].append(nb)
    return out


def _action(a: dict, where: str, variables: set, systems: set) -> dict:
    t = a.get("type")
    if t not in ACTION_TYPES:
        raise ValueError(f"{where}: action type must be one of {', '.join(ACTION_TYPES)}")
    out = {"type": t}
    if t in ("tap", "var_add", "var_set", "var_reset", "system"):
        out["on"] = a.get("on", "activate")
        if out["on"] not in ("activate", "deactivate"):
            raise ValueError(f"{where}: 'on' must be activate or deactivate")
    if t in ("hold", "tap", "toggle"):
        out["outputs"] = _outputs(a.get("outputs", []), where)
        if not out["outputs"]:
            raise ValueError(f"{where}: a {t} action needs at least one Xbox output")
    if t in ("tap", "var_add"):
        out["repeat_ms"] = max(0, _num(a.get("repeat_ms", 0), f"{where}: repeat_ms"))
    if t in ("var_add", "var_set", "var_reset"):
        out["var"] = str(a.get("var", ""))
        if out["var"] not in variables:
            raise ValueError(f"{where}: there is no variable {out['var']!r}")
    if t == "var_add":
        out["amount"] = _num(a.get("amount", 1), f"{where}: amount")
    if t == "var_set":
        out["value"] = _num(a.get("value", 0), f"{where}: value")
    if t == "remap":
        out["from"], out["to"] = a.get("from"), a.get("to")
        for key in ("from", "to"):
            if out[key] not in REMAP_NAMES:
                raise ValueError(f"{where}: remap {key} {out[key]!r} is not an Xbox output or stick")
        if (out["from"] in STICKS) != (out["to"] in STICKS):
            raise ValueError(f"{where}: a whole stick can only be remapped to the other stick")
        out["targets"] = [str(x) for x in a.get("targets", [])]
    if t == "system":
        out["system"] = str(a.get("system", ""))
        if out["system"] not in systems:
            raise ValueError(f"{where}: there is no system {out['system']!r}")
        out["mode"] = a.get("mode", "toggle")
        if out["mode"] not in ("on", "off", "toggle"):
            raise ValueError(f"{where}: system mode must be on, off or toggle")
    return out


# ---- maths ---------------------------------------------------------------------------

def in_zone(zone: dict, value, was_active: bool = False) -> bool:
    """Is value inside the zone? hysteresis widens the zone while it is already active."""
    if value is None:
        return False
    h = zone.get("hysteresis", 0) if was_active else 0
    a, b, mode = zone["a"], zone.get("b"), zone["mode"]
    if mode == "above":
        return value >= a - h
    if mode == "below":
        return value <= a + h
    lo, hi = (a, a) if mode == "at" else sorted((a, b))
    if mode == "outside":
        return value < lo + h or value > hi - h
    return lo - h <= value <= hi + h


def axis_value(value, deadzone, rng, invert=False) -> float:
    """Source value -> -1..1. Inside the deadzone it is 0; at the range's ends it is -1 / 1."""
    if value is None:
        return 0.0
    lo, hi = deadzone
    mn, mx = rng
    if value > hi:
        out = (value - hi) / (mx - hi) if mx > hi else 1.0
    elif value < lo:
        out = -(lo - value) / (lo - mn) if lo > mn else -1.0
    else:
        out = 0.0
    out = max(-1.0, min(1.0, out))
    return -out if invert else out


def stick_value(x, y, deadzone, full, directions=0) -> tuple[float, float]:
    """Two source values -> a stick vector, with a round deadzone and optional 4 / 8 way snapping."""
    x, y = x or 0.0, y or 0.0
    r = math.hypot(x, y)
    if r <= deadzone:
        return 0.0, 0.0
    size = min(1.0, (r - deadzone) / (full - deadzone))
    angle = math.atan2(y, x)
    if directions:
        step = 2 * math.pi / directions
        angle = round(angle / step) * step
    return size * math.cos(angle), size * math.sin(angle)


def remap_output(name: str, remaps: list) -> str:
    """Apply the first remap that matches. remaps: [(from, to, targets)] that apply to this output's binding."""
    for src, dst, _ in remaps:
        if name == src:
            return dst
        if src in STICKS and name.startswith(src + ":"):
            return dst + name[len(src):]
    return name


# ---- the engine ----------------------------------------------------------------------

class Engine:
    """
        engine = Engine(loadout)
        engine.set_source("motor:A", -31)
        engine.tick(now)
        chrome.push(engine.snapshot(now))
    """

    def __init__(self, loadout: dict | None = None, log=print):
        self.recent_log: deque[str] = deque(maxlen=12)
        self._print = log
        self.sources: dict[str, float | None] = {}
        self.ranges: dict[str, tuple[float, float]] = {}  # source -> default (min, max) for axes
        self.load(loadout or empty_loadout())

    def log(self, text: str) -> None:
        self.recent_log.append(text)
        self._print(text)

    def load(self, loadout: dict) -> None:
        """Use a new loadout (already normalized or not). Releases everything, variables start over."""
        self.loadout = normalize_loadout(loadout)
        self.min_press_s = self.loadout["settings"]["min_press_ms"] / 1000
        self.vars = {v["name"]: v for v in self.loadout["variables"]}
        values = {name: v["initial"] for name, v in self.vars.items()}
        self.var_values = {name: self._limit(name, value) for name, value in values.items()}
        self.system_on = {s["id"]: s["enabled"] for s in self.loadout["systems"]}
        self.state = {b["id"]: {"active": False, "value": 0.0, "vec": (0.0, 0.0), "toggled": set(), "repeat": {}}
                      for b in self.loadout["bindings"]}
        self.pulses: list[tuple[str, str, float]] = []  # (owner, output, until)
        self.down_since: dict[str, float] = {}
        self.out_buttons = [0.0] * len(BUTTONS)
        self.out_axes = [0.0, 0.0, 0.0, 0.0]  # lx, ly, rx, ry with +y = north
        self.applied_remaps: list = []

    def update_loadout(self, loadout: dict) -> None:
        """Edit the loadout while playing: keeps variable values and the state of unchanged bindings."""
        old_vars, old_state, old_systems = self.var_values, self.state, self.system_on
        self.load(loadout)
        for name in self.var_values:
            if name in old_vars:
                self.var_values[name] = self._limit(name, old_vars[name])
        for sid in self.system_on:
            if sid in old_systems:
                self.system_on[sid] = old_systems[sid]
        for bid in self.state:
            if bid in old_state:  # an input held during the edit must not fire again
                self.state[bid] = old_state[bid]

    # ---- inputs ----------------------------------------------------------------

    def set_source(self, name: str, value) -> None:
        self.sources[name] = value

    def value(self, source: str):
        if source.startswith("var:"):
            return self.var_values.get(source[4:])
        return self.sources.get(source)

    def _limit(self, name: str, value: float) -> float:
        v = self.vars[name]
        if v["wrap"] and v["min"] is not None and v["max"] is not None:
            if value > v["max"]:
                return v["min"]
            if value < v["min"]:
                return v["max"]
        if v["min"] is not None:
            value = max(v["min"], value)
        if v["max"] is not None:
            value = min(v["max"], value)
        return value

    def set_var(self, name: str, value: float) -> None:
        if name in self.vars:
            self.var_values[name] = self._limit(name, value)

    def reset_var(self, name: str) -> None:
        if name in self.vars:
            self.var_values[name] = self._limit(name, self.vars[name]["initial"])

    def conditions_ok(self, conditions: list) -> bool:
        return all(in_zone(c, self.value(c["source"])) for c in conditions)

    # ---- time ------------------------------------------------------------------

    def tick(self, now: float) -> None:
        """Evaluate every binding and work out the Xbox controller's state."""
        live = {s["id"]: self.system_on[s["id"]] and self.conditions_ok(s["conditions"])
                for s in self.loadout["systems"]}
        for b in self.loadout["bindings"]:
            st = self.state[b["id"]]
            gate = b["enabled"] and live.get(b["system"], False) and self.conditions_ok(b["conditions"])
            if b["kind"] == "zone":
                active = gate and in_zone(b["zone"], self.value(b["source"]), st["active"])
                if active and not st["active"]:
                    st["repeat"] = {}
                    self._fire(b, "activate", now)
                elif st["active"] and not active:
                    self._fire(b, "deactivate", now)
                elif active:
                    self._repeat(b, now)
                st["active"] = active
            elif b["kind"] == "axis":
                rng = b["range"] or self.ranges.get(b["source"], (-90, 90))
                st["value"] = axis_value(self.value(b["source"]), b["deadzone"], rng, b["invert"]) if gate else 0.0
                st["active"] = st["value"] != 0
            else:
                x, y = self.value(b["x_source"]), self.value(b["y_source"])
                x = -(x or 0) if b["invert_x"] else x
                y = -(y or 0) if b["invert_y"] else y
                st["vec"] = stick_value(x, y, b["deadzone"], b["full"], b["directions"]) if gate else (0.0, 0.0)
                st["active"] = st["vec"] != (0.0, 0.0)
        self._resolve(now)

    def _fire(self, b: dict, when: str, now: float) -> None:
        st = self.state[b["id"]]
        done = []
        for i, a in enumerate(b["actions"]):
            t = a["type"]
            if when == "activate" and t == "toggle":
                for o in a["outputs"]:
                    st["toggled"].symmetric_difference_update({o})
                done.append("toggle " + "+".join(a["outputs"]))
            if a.get("on", "activate") != when:
                continue
            if t in ("tap", "var_add"):
                st["repeat"][i] = now
            if t == "tap":
                self._tap(b["id"], a["outputs"], now)
                done.append("tap " + "+".join(a["outputs"]))
            elif t == "var_add":
                self.set_var(a["var"], self.var_values[a["var"]] + a["amount"])
                done.append(f"{a['var']} = {self.var_values[a['var']]:g}")
            elif t == "var_set":
                self.set_var(a["var"], a["value"])
                done.append(f"{a['var']} = {self.var_values[a['var']]:g}")
            elif t == "var_reset":
                self.reset_var(a["var"])
                done.append(f"{a['var']} reset to {self.var_values[a['var']]:g}")
            elif t == "system":
                sid = a["system"]
                self.system_on[sid] = {"on": True, "off": False}.get(a["mode"], not self.system_on[sid])
                done.append(f"system {sid} {'on' if self.system_on[sid] else 'off'}")
        if when == "activate":
            held = [o for a in b["actions"] if a["type"] == "hold" for o in a["outputs"]]
            remaps = [f"{a['from']}→{a['to']}" for a in b["actions"] if a["type"] == "remap"]
            done = (["hold " + "+".join(held)] if held else []) + (["remap " + ", ".join(remaps)] if remaps else []) + done
        if done:
            self.log(f"{b['name']} {'on' if when == 'activate' else 'off'} -> {', '.join(done)}")

    def _repeat(self, b: dict, now: float) -> None:
        st = self.state[b["id"]]
        for i, a in enumerate(b["actions"]):
            if a["type"] not in ("tap", "var_add") or not a["repeat_ms"] or a["on"] != "activate":
                continue
            if now - st["repeat"].get(i, now) >= a["repeat_ms"] / 1000 - 1e-6:
                st["repeat"][i] = now
                if a["type"] == "tap":
                    self._tap(b["id"], a["outputs"], now)
                else:
                    self.set_var(a["var"], self.var_values[a["var"]] + a["amount"])

    def _tap(self, owner: str, outputs: list, now: float) -> None:
        for o in outputs:
            self.pulses.append((owner, o, now + self.min_press_s))

    def _resolve(self, now: float) -> None:
        bindings = self.loadout["bindings"]
        remaps = [(a["from"], a["to"], a["targets"], b["id"]) for b in bindings
                  if b["kind"] == "zone" and self.state[b["id"]]["active"]
                  for a in b["actions"] if a["type"] == "remap"]
        self.applied_remaps = [(f, t) for f, t, _, _ in remaps]
        wanted: list[tuple[str, str, float]] = []  # (owner, output, value)
        for b in bindings:
            st = self.state[b["id"]]
            if b["kind"] == "zone":
                if st["active"]:
                    wanted += [(b["id"], o, 1.0) for a in b["actions"] if a["type"] == "hold" for o in a["outputs"]]
                wanted += [(b["id"], o, 1.0) for o in st["toggled"]]
            elif b["kind"] == "axis":
                if st["value"]:
                    wanted.append((b["id"], b["output"], st["value"]))
            elif st["vec"] != (0.0, 0.0):
                wanted += [(b["id"], b["output"] + ":x", st["vec"][0]), (b["id"], b["output"] + ":y", st["vec"][1])]
        self.pulses = [p for p in self.pulses if p[2] > now]
        wanted += [(owner, o, 1.0) for owner, o, _ in self.pulses]

        buttons = [0.0] * len(BUTTONS)
        sticks = {"ls": [0.0, 0.0], "rs": [0.0, 0.0]}
        for owner, name, value in wanted:
            mine = [(f, t, tg) for f, t, tg, by in remaps if by != owner and (not tg or owner in tg)]
            name = remap_output(name, mine)
            if name in BUTTON_INDEX:
                i = BUTTON_INDEX[name]
                buttons[i] = max(buttons[i], max(0.0, value) if name in TRIGGERS else abs(value))
            else:
                stick, part = name.split(":")
                if part in DIRECTIONS:
                    dx, dy = DIRECTIONS[part]
                    sticks[stick][0] += dx * abs(value)
                    sticks[stick][1] += dy * abs(value)
                else:
                    sticks[stick][0 if part == "x" else 1] += value

        # Digital buttons: on at 0.5 and up, and every press lasts at least min_press_s.
        for i, name in enumerate(BUTTONS):
            if buttons[i] >= 0.5:
                self.down_since.setdefault(name, now)
                if name not in TRIGGERS:
                    buttons[i] = 1.0
            elif name in self.down_since:
                if now - self.down_since[name] < self.min_press_s:
                    buttons[i] = 1.0
                else:
                    del self.down_since[name]
        self.out_buttons = buttons
        axes = []
        for stick in STICKS:
            x, y = sticks[stick]
            r = math.hypot(x, y)
            if r > 1:
                x, y = x / r, y / r
            axes += [x, y]
        self.out_axes = axes

    def release_all(self) -> None:
        """Everything up and centred right now (lost the hub, calibrating, shutting down)."""
        for st in self.state.values():
            st["active"], st["value"], st["vec"] = False, 0.0, (0.0, 0.0)
            st["toggled"].clear()
        self.pulses.clear()
        self.down_since.clear()
        self.out_buttons = [0.0] * len(BUTTONS)
        self.out_axes = [0.0, 0.0, 0.0, 0.0]

    # ---- output ----------------------------------------------------------------

    def snapshot(self) -> dict:
        """The state for the page's Gamepad API: {"axes": [lx, ly, rx, ry], "buttons": [17 values]}.
        The Gamepad API's y axis points down, so north (+y here) becomes -1 there."""
        lx, ly, rx, ry = self.out_axes
        axes = [round(v, 3) + 0.0 for v in (lx, -ly, rx, -ry)]
        return {"axes": axes, "buttons": [round(v, 3) for v in self.out_buttons]}

    def pressed(self) -> list[str]:
        return [name for name, v in zip(BUTTONS, self.out_buttons) if v >= 0.5]

    def status(self) -> dict:
        """Live engine state for the control panel."""
        return {
            "vars": dict(self.var_values),
            "systems": dict(self.system_on),
            "systems_live": {s["id"]: self.system_on[s["id"]] and self.conditions_ok(s["conditions"])
                             for s in self.loadout["systems"]},
            "bindings": {bid: {"active": st["active"], "value": round(st["value"], 3),
                               "vec": [round(v, 3) for v in st["vec"]], "toggled": sorted(st["toggled"])}
                         for bid, st in self.state.items()},
            "remaps": [f"{f} → {t}" for f, t in self.applied_remaps],
            "pad": self.snapshot(), "pressed": self.pressed(),
            "log": list(self.recent_log),
        }

    def copy_loadout(self) -> dict:
        return copy.deepcopy(self.loadout)

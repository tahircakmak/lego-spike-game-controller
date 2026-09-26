#!/usr/bin/env python3
"""
LEGO SPIKE -> Minecraft Dungeons controller simulator.

Shows what every action on the SPIKE controller will do in the game, so the
control scheme can be tried out before Minecraft Dungeons is installed.

Run:  python3 controller_simulator.py

Keyboard stand-ins for the real controller:
  Arrows / W A S D   tilt the hub
  Space              press the force sensor
  H                  squeeze the force sensor past 90% (health potion)
  F (hold)           pull the trigger down (motor A); let go = push it back a bit
  Q / E              turn the artifact dial (large motor F)
  Z / X              left / right hub button
  C                  centre (main) hub button
You can also use the mouse on every control in the left panel.

All controller behaviour lives in ControllerLogic. bridge.py feeds it events
from the real hub and gives it an output that presses real keys.
"""

import math
import time
import tkinter as tk
from tkinter import ttk

TICK_MS = 33
WORLD_W, WORLD_H = 640, 400

# The PC input each in-game action will be sent as once the game is installed.
BINDING = {
    "melee": "Left mouse",
    "potion": "E",
    "ranged": "Right mouse",
    "artifact": ["1", "2", "3"],
    "inventory": "I",
    "roll": "Space",
    "inv_move": "Mouse move",
    "inv_click": "Left mouse",
    "inv_close": "Esc",
}

# Artifact dial positions: name, colour, pointer angle (deg, 0 = right, CCW), action
DIAL = [
    ("Yellow", "#f2c200", 150, "Artifact 1"),
    ("Magenta", "#e0218a", 90, "Artifact 2"),
    ("Green", "#1fa84f", 30, "Artifact 3"),
    ("Down", "#9a9a9a", 270, "Inventory"),
]
DIAL_INVENTORY = 3

INVENTORY_ITEMS = [
    "Sword", "Axe", "Katana", "Bow",
    "Crossbow", "Health Potion", "Firework Arrow", "Totem of Regen",
    "Iron Hide Amulet", "Harvester", "Fishing Rod", "Corrupted Beacon",
]
INV_COLS, INV_ROWS = 4, 3
RANGED_WEAPONS = ("Bow", "Crossbow")
MELEE_WEAPONS = ("Sword", "Axe", "Katana")

TRIGGER_MAX = 90           # degrees pulled down from the rest angle
PULL_THRESHOLD = 45        # past this the trigger counts as "pulled"
RELEASE_DELTA = 10         # pushing back this far from the deepest point = release
TRIGGER_RETURN_SPEED = 180  # deg/s the motor drives the trigger back to rest
BOW_FULL_CHARGE_S = 1.2
CROSSBOW_INTERVAL_S = 0.6

TILT_MAX = 25              # degrees of hub tilt for full speed
TILT_DEADZONE = 5
MOVE_SPEED = 150
ROLL_SPEED = 420
ROLL_TIME = 0.3
INV_STEP_DELAY = 0.3
INV_MOUSE_SPEED = 600      # px/s the mouse moves in the inventory at full tilt

COMPASS = [
    ("east", "D"), ("north-east", "W+D"), ("north", "W"), ("north-west", "W+A"),
    ("west", "A"), ("south-west", "S+A"), ("south", "S"), ("south-east", "S+D"),
]


class NullOutput:
    """Sends nothing to the game. bridge.py swaps in one that presses real keys.

    Key names: "w" "a" "s" "d" "1" "2" "3" "i" "space" "esc" "mouse_left" "mouse_right".
    """

    def tap(self, key): pass
    def hold(self, key): pass
    def release(self, key): pass
    def set_move(self, keys): pass
    def mouse_move(self, dx, dy): pass
    def stick(self, x, y): pass  # analog left stick, -1..1 each, y = -1 is up
    def release_all(self): pass


class ControllerLogic:
    """Turns controller inputs into game actions and runs a tiny fake game world."""

    def __init__(self, log, output=None, hardware_trigger=False):
        self.log = log
        self.out = output or NullOutput()
        # With the real hub, motor A drives itself back to rest and says when it is done.
        self.hardware_trigger = hardware_trigger
        self.mode = "game"            # "game" or "inventory"
        self.weapon = "Bow"
        self.melee = "Sword"
        self.dial = 0
        self.tilt = (0.0, 0.0)        # (right, forward) in degrees
        self.move_dir = None
        self.force_down = False
        self.trigger_angle = 0.0
        self.trigger_pulled = False
        self.trigger_peak = 0.0
        self.trigger_returning = False
        self.charge_start = None
        self.next_crossbow_shot = 0.0
        self.inv_sel = 0
        self.inv_next_step = 0.0
        self.px, self.py = WORLD_W / 2, WORLD_H / 2
        self.facing = (0.0, -1.0)
        self.roll_until = 0.0
        self.projectiles = []
        self.effects = []

    # ---- controller inputs -------------------------------------------------

    def on_tilt(self, right, forward):
        self.tilt = (right, forward)

    def on_force(self, pressed):
        if pressed == self.force_down:
            return
        self.force_down = pressed
        if not pressed:
            return
        if self.mode == "inventory":
            self._inventory_click()
        else:
            self._effect("melee", 0.2)
            self.out.tap("mouse_left")
            self.log("FORCE SENSOR pressed", f"Melee attack with {self.melee}", BINDING["melee"])

    def on_trigger(self, angle):
        """Simulated trigger: decide pull / release from the angle, like the hub program does."""
        if self.trigger_returning:
            return
        self.trigger_angle = angle
        if not self.trigger_pulled:
            if angle >= PULL_THRESHOLD:
                self.trigger_peak = angle
                self.trigger_pull()
        else:
            self.trigger_peak = max(self.trigger_peak, angle)
            if angle <= self.trigger_peak - RELEASE_DELTA:
                self.trigger_release()

    def trigger_pull(self):
        if self.mode == "inventory":
            self.log("TRIGGER pulled down", "No effect while the inventory is open")
            self.trigger_returning = not self.hardware_trigger
            return
        self.trigger_pulled = True
        self.out.hold("mouse_right")
        if self.weapon == "Bow":
            self.charge_start = time.monotonic()
            self.log("TRIGGER pulled down", "Draw bow - charging...", f"hold {BINDING['ranged']}")
        else:
            self.log("TRIGGER pulled down", "Crossbow - start firing", f"hold {BINDING['ranged']}")
            self._shoot(1.0)
            self.next_crossbow_shot = time.monotonic() + CROSSBOW_INTERVAL_S

    def trigger_release(self):
        if not self.trigger_pulled:
            return
        self.trigger_pulled = False
        self.out.release("mouse_right")
        if self.weapon == "Bow":
            charge = self.bow_charge()
            self.charge_start = None
            self._shoot(charge)
            self.log("TRIGGER pushed back", f"Release bow - arrow fired at {charge:.0%} power",
                     f"release {BINDING['ranged']}")
        else:
            self.log("TRIGGER pushed back", "Crossbow - stop firing", f"release {BINDING['ranged']}")
        self.trigger_returning = True
        self.log("Motor A", "Auto-returns trigger to its rest angle")

    def on_potion(self):
        """The force sensor was squeezed past 90%."""
        if self.mode == "inventory":
            return  # a hard press in the inventory is just a click
        self._effect("artifact", 0.8, colour="#ff4d6d", text="Health potion!")
        self.out.tap("e")
        self.log("FORCE SENSOR squeezed > 90%", "Drink health potion", BINDING["potion"])

    def on_dial(self, index):
        index %= len(DIAL)
        if index == self.dial:
            return
        self.dial = index
        name, _, _, action = DIAL[index]
        self.log(f"DIAL (motor F) -> {name}", f"{action} selected")

    def on_button(self, which):
        if which == "center":
            self.log("CENTRE button", "No action (main hub button)")
        elif self.mode == "inventory":
            self.mode = "game"
            self.out.tap("esc")
            self.log(f"{which.upper()} button", "Close inventory", BINDING["inv_close"])
        elif which == "left":
            self.roll_until = time.monotonic() + ROLL_TIME
            self._effect("roll", ROLL_TIME)
            self.out.tap("space")
            self.log("LEFT button", "Roll / dodge", BINDING["roll"])
        elif self.dial == DIAL_INVENTORY:
            self._cancel_ranged()
            self.mode = "inventory"
            self.move_dir = None
            self.inv_next_step = 0.0
            self.out.set_move(set())
            self.out.tap("i")
            self.log("RIGHT button (dial Down)", "Open inventory", BINDING["inventory"])
        else:
            name, colour, _, action = DIAL[self.dial]
            self._effect("artifact", 0.8, colour=colour, text=f"{action}!")
            self.out.tap(BINDING["artifact"][self.dial])
            self.log(f"RIGHT button (dial {name})", f"Use {action}", BINDING["artifact"][self.dial])

    def set_weapon(self, weapon):
        if weapon != self.weapon:
            self._cancel_ranged()
            self.weapon = weapon

    # ---- helpers -----------------------------------------------------------

    def bow_charge(self):
        if self.charge_start is None:
            return 0.0
        return min(1.0, (time.monotonic() - self.charge_start) / BOW_FULL_CHARGE_S)

    def _cancel_ranged(self):
        if self.trigger_pulled:
            self.trigger_pulled = False
            self.charge_start = None
            self.out.release("mouse_right")
            if self.hardware_trigger:
                self.log("Trigger", "Ranged attack cancelled")
            else:
                self.trigger_returning = True
                self.log("Motor A", "Ranged attack cancelled, trigger returns to rest")

    def _inventory_click(self):
        item = INVENTORY_ITEMS[self.inv_sel]
        if item in RANGED_WEAPONS:
            self.weapon = item
            result = f"Equip {item}"
        elif item in MELEE_WEAPONS:
            self.melee = item
            result = f"Equip {item}"
        else:
            result = f"Select {item}"
        self.out.tap("mouse_left")
        self.log("FORCE SENSOR pressed (inventory)", result, BINDING["inv_click"])

    def _shoot(self, power):
        fx, fy = self.facing
        speed = 250 + 300 * power
        self.projectiles.append({"x": self.px, "y": self.py, "vx": fx * speed, "vy": fy * speed,
                                 "kind": self.weapon})

    def _effect(self, kind, duration, **extra):
        self.effects.append({"kind": kind, "t0": time.monotonic(), "dur": duration, **extra})

    # ---- world update ------------------------------------------------------

    def update(self, dt, now):
        if self.trigger_returning and not self.hardware_trigger:
            self.trigger_angle = max(0.0, self.trigger_angle - TRIGGER_RETURN_SPEED * dt)
            if self.trigger_angle == 0.0:
                self.trigger_returning = False

        right, forward = self.tilt
        mag = math.hypot(right, forward)
        if mag > TILT_DEADZONE:
            scale = min(1.0, mag / TILT_MAX) / mag  # keep the direction, cap the length at 1
            self.out.stick(right * scale, -forward * scale)
        else:
            self.out.stick(0.0, 0.0)
        if self.mode == "game":
            self._update_movement(dt, now, right, forward, mag)
            if self.trigger_pulled and self.weapon == "Crossbow" and now >= self.next_crossbow_shot:
                self._shoot(1.0)
                self.next_crossbow_shot += CROSSBOW_INTERVAL_S
        else:
            self._update_inventory_cursor(dt, now, right, forward, mag)

        for p in self.projectiles:
            p["x"] += p["vx"] * dt
            p["y"] += p["vy"] * dt
        self.projectiles = [p for p in self.projectiles
                            if -20 < p["x"] < WORLD_W + 20 and -20 < p["y"] < WORLD_H + 20]
        self.effects = [e for e in self.effects if now - e["t0"] < e["dur"]]

    def _update_movement(self, dt, now, right, forward, mag):
        direction = None
        speed = 0.0
        if mag > TILT_DEADZONE:
            self.facing = (right / mag, -forward / mag)
            speed = MOVE_SPEED * min(1.0, mag / TILT_MAX)
            direction = COMPASS[round(math.degrees(math.atan2(forward, right)) / 45) % 8]
        if now < self.roll_until:
            speed = ROLL_SPEED
        self.px = min(WORLD_W - 12, max(12, self.px + self.facing[0] * speed * dt))
        self.py = min(WORLD_H - 12, max(12, self.py + self.facing[1] * speed * dt))
        if direction != self.move_dir:
            self.out.set_move(set(direction[1].lower().split("+")) if direction else set())
            if direction:
                self.log("TILT hub", f"Move {direction[0]}", direction[1])
            else:
                self.log("Hub level", "Stop moving")
            self.move_dir = direction

    def _update_inventory_cursor(self, dt, now, right, forward, mag):
        if mag > TILT_DEADZONE:
            k = INV_MOUSE_SPEED * dt / TILT_MAX
            self.out.mouse_move(right * k, -forward * k)
        if mag <= TILT_DEADZONE * 2:
            self.inv_next_step = 0.0
            return
        if now < self.inv_next_step:
            return
        self.inv_next_step = now + INV_STEP_DELAY
        col, row = self.inv_sel % INV_COLS, self.inv_sel // INV_COLS
        if abs(right) > abs(forward):
            col = min(INV_COLS - 1, max(0, col + (1 if right > 0 else -1)))
        else:
            row = min(INV_ROWS - 1, max(0, row + (-1 if forward > 0 else 1)))
        new_sel = row * INV_COLS + col
        if new_sel != self.inv_sel:
            self.inv_sel = new_sel
            self.log("TILT hub (inventory)", f"Cursor -> {INVENTORY_ITEMS[new_sel]}", BINDING["inv_move"])


class App:
    def __init__(self, root, output=None, hardware=False):
        """hardware=True: inputs come from the real hub (bridge.py), not the keyboard."""
        self.root = root
        self.hardware = hardware
        root.title("SPIKE Dungeons Controller - " + ("Live hub" if hardware else "Simulator"))
        self.model = ControllerLogic(self.log, output, hardware_trigger=hardware)
        self.held = set()
        self._release_jobs = {}
        self.pad_tilt = None
        self.dial_draw_angle = DIAL[0][2]
        self.last = time.monotonic()
        self._build()
        if not hardware:
            root.bind("<KeyPress>", self._key_down)
            root.bind("<KeyRelease>", self._key_up)
        root.focus_set()
        self._tick()

    # ---- layout ------------------------------------------------------------

    def _build(self):
        left = ttk.Frame(self.root, padding=8)
        left.grid(row=0, column=0, sticky="ns")
        right = ttk.Frame(self.root, padding=8)
        right.grid(row=0, column=1, sticky="nsew")
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)

        hub = ttk.LabelFrame(left, text="Hub: tilt + buttons", padding=6)
        hub.pack(fill="x")
        self.pad = tk.Canvas(hub, width=170, height=170, bg="#20242b", highlightthickness=0)
        self.pad.pack()
        self.pad.bind("<ButtonPress-1>", self._pad_drag)
        self.pad.bind("<B1-Motion>", self._pad_drag)
        self.pad.bind("<ButtonRelease-1>", self._pad_release)
        ttk.Label(hub, text="drag the bubble or use arrows / WASD").pack()
        btns = ttk.Frame(hub)
        btns.pack(pady=(6, 0))
        for label, which in (("◀ Left (Z)", "left"), ("● Main (C)", "center"), ("Right (X) ▶", "right")):
            ttk.Button(btns, text=label, takefocus=False,
                       command=lambda w=which: self.model.on_button(w)).pack(side="left", padx=2)

        force = ttk.LabelFrame(left, text="Force sensor", padding=6)
        force.pack(fill="x", pady=6)
        self.force_btn = tk.Label(force, text="PRESS (Space)\nhard: potion (H)", width=20, height=2,
                                  bg="#444", fg="white", relief="raised", font=("Helvetica", 13, "bold"))
        self.force_btn.pack()
        self.force_btn.bind("<ButtonPress-1>", lambda e: self.model.on_force(True))
        self.force_btn.bind("<ButtonRelease-1>", lambda e: self.model.on_force(False))

        trig = ttk.LabelFrame(left, text="Trigger: motor port A", padding=6)
        trig.pack(fill="x")
        self.trigger_var = tk.DoubleVar(value=0)
        self.scale = tk.Scale(trig, from_=0, to=TRIGGER_MAX, orient="vertical", resolution=1,
                              variable=self.trigger_var, command=self._scale_moved,
                              showvalue=False, length=140, width=22, takefocus=0)
        self.scale.grid(row=0, column=0, rowspan=5, padx=(0, 8))
        self.scale.bind("<ButtonRelease-1>", lambda e: self.root.focus_set(), add="+")
        if self.hardware:
            self.scale.configure(state="disabled")
        ttk.Label(trig, text="Drag down = pull\nDrag back a bit = release\n(or hold F)").grid(
            row=0, column=1, sticky="w")
        self.weapon_var = tk.StringVar(value=self.model.weapon)
        for i, w in enumerate(RANGED_WEAPONS):
            ttk.Radiobutton(trig, text=w, value=w, variable=self.weapon_var, takefocus=False,
                            command=lambda: self.model.set_weapon(self.weapon_var.get())).grid(
                row=1 + i, column=1, sticky="w")
        self.trigger_state = ttk.Label(trig, text="")
        self.trigger_state.grid(row=3, column=1, sticky="w")
        self.charge_bar = ttk.Progressbar(trig, length=130, maximum=1.0)
        self.charge_bar.grid(row=4, column=1, sticky="w")

        dial = ttk.LabelFrame(left, text="Artifact dial: large motor port F", padding=6)
        dial.pack(fill="x", pady=6)
        self.dial_cv = tk.Canvas(dial, width=170, height=170, bg="#20242b", highlightthickness=0)
        self.dial_cv.pack()
        self.dial_cv.bind("<ButtonPress-1>", self._dial_click)
        ttk.Label(dial, text="click a colour or use Q / E").pack()

        self.banner = tk.StringVar(value="Use the controller...")
        ttk.Label(right, textvariable=self.banner, font=("Helvetica", 20, "bold")).pack(anchor="w")
        self.game_cv = tk.Canvas(right, width=WORLD_W, height=WORLD_H, bg="#2e4d2c", highlightthickness=0)
        self.game_cv.pack(pady=6)
        ttk.Label(right, text="Action log  (controller input  ->  game action   [PC input it will send])").pack(
            anchor="w")
        self.log_txt = tk.Text(right, height=12, bg="#15171b", fg="#ddd", font=("Menlo", 12),
                               state="disabled", takefocus=0, wrap="none")
        self.log_txt.pack(fill="both", expand=True)
        self.log_txt.tag_configure("ts", foreground="#777")
        self.log_txt.tag_configure("src", foreground="#6cb6ff")
        self.log_txt.tag_configure("act", foreground="#9be38f")
        self.log_txt.tag_configure("key", foreground="#e3c46f")

    # ---- logging -----------------------------------------------------------

    def log(self, source, action, binding=None):
        self.log_txt.configure(state="normal")
        self.log_txt.insert("end", time.strftime("%H:%M:%S  "), "ts")
        self.log_txt.insert("end", source, "src")
        self.log_txt.insert("end", "  ->  ")
        self.log_txt.insert("end", action, "act")
        if binding:
            self.log_txt.insert("end", f"   [{binding}]", "key")
        self.log_txt.insert("end", "\n")
        self.log_txt.see("end")
        self.log_txt.configure(state="disabled")
        self.banner.set(action)

    # ---- inputs ------------------------------------------------------------

    @staticmethod
    def _key_name(event):
        return event.keysym.lower() if len(event.keysym) == 1 else event.keysym

    def _key_down(self, event):
        key = self._key_name(event)
        job = self._release_jobs.pop(key, None)
        if job:  # key auto-repeat, not a real release
            self.root.after_cancel(job)
            return
        if key in self.held:
            return
        self.held.add(key)
        self._key_edge(key, True)

    def _key_up(self, event):
        key = self._key_name(event)
        if key in self.held:
            self._release_jobs[key] = self.root.after(40, self._key_released, key)

    def _key_released(self, key):
        self._release_jobs.pop(key, None)
        self.held.discard(key)
        self._key_edge(key, False)

    def _key_edge(self, key, pressed):
        m = self.model
        if key == "space":
            m.on_force(pressed)
        elif key == "f" and not pressed and not m.trigger_returning:
            m.on_trigger(max(0.0, m.trigger_angle - 15))  # push the trigger back a bit
        elif pressed and key == "h":
            m.on_force(True)
            m.on_potion()
            m.on_force(False)
        elif pressed and key in ("q", "e"):
            m.on_dial(m.dial + (1 if key == "e" else -1))
        elif pressed and key in ("z", "x", "c"):
            m.on_button({"z": "left", "x": "right", "c": "center"}[key])

    def _pad_drag(self, event):
        if self.hardware:
            return
        dx, dy = (event.x - 85) / 70, (85 - event.y) / 70
        mag = math.hypot(dx, dy)
        if mag > 1:
            dx, dy = dx / mag, dy / mag
        self.pad_tilt = (dx * TILT_MAX, dy * TILT_MAX)

    def _pad_release(self, _event):
        self.pad_tilt = None

    def _scale_moved(self, value):
        value = float(value)
        if abs(value - self.model.trigger_angle) >= 0.5:
            self.model.on_trigger(value)

    def _dial_click(self, event):
        angle = math.degrees(math.atan2(85 - event.y, event.x - 85)) % 360
        nearest = min(range(len(DIAL)),
                      key=lambda i: abs((DIAL[i][2] - angle + 180) % 360 - 180))
        self.model.on_dial(nearest)

    # ---- main loop ---------------------------------------------------------

    def _tick(self):
        now = time.monotonic()
        dt = min(0.1, now - self.last)
        self.last = now
        m = self.model

        if self.hardware:
            pass  # tilt and trigger arrive from the hub
        elif self.pad_tilt is not None:
            m.on_tilt(*self.pad_tilt)
        else:
            h = self.held
            kx = (("Right" in h) or ("d" in h)) - (("Left" in h) or ("a" in h))
            ky = (("Up" in h) or ("w" in h)) - (("Down" in h) or ("s" in h))
            m.on_tilt(kx * TILT_MAX * 0.8, ky * TILT_MAX * 0.8)

        if not self.hardware and "f" in self.held and not m.trigger_returning:
            m.on_trigger(min(TRIGGER_MAX, m.trigger_angle + 220 * dt))

        m.update(dt, now)

        if self.hardware or "f" in self.held or m.trigger_returning:
            self.trigger_var.set(min(TRIGGER_MAX, round(m.trigger_angle)))
        if self.weapon_var.get() != m.weapon:
            self.weapon_var.set(m.weapon)
        if m.trigger_returning:
            state = "returning to rest..."
        elif m.trigger_pulled:
            state = "PULLED" + (" - charging" if m.weapon == "Bow" else " - firing")
        else:
            state = "at rest"
        self.trigger_state.configure(text=f"{m.trigger_angle:3.0f}°  {state}")
        self.charge_bar["value"] = m.bow_charge()
        self.force_btn.configure(bg="#c0392b" if m.force_down else "#444",
                                 relief="sunken" if m.force_down else "raised")

        self._draw_pad()
        self._draw_dial(dt)
        self._draw_game(now)
        self.root.after(TICK_MS, self._tick)

    # ---- drawing -----------------------------------------------------------

    def _draw_pad(self):
        cv = self.pad
        cv.delete("all")
        cv.create_oval(15, 15, 155, 155, outline="#556", width=2)
        cv.create_oval(85 - 70 * TILT_DEADZONE / TILT_MAX, 85 - 70 * TILT_DEADZONE / TILT_MAX,
                       85 + 70 * TILT_DEADZONE / TILT_MAX, 85 + 70 * TILT_DEADZONE / TILT_MAX,
                       outline="#445", dash=(2, 2))
        cv.create_line(85, 15, 85, 155, fill="#334")
        cv.create_line(15, 85, 155, 85, fill="#334")
        cv.create_text(85, 8, text="forward", fill="#889", font=("Helvetica", 9))
        right, forward = self.model.tilt
        x, y = 85 + right / TILT_MAX * 70, 85 - forward / TILT_MAX * 70
        cv.create_oval(x - 12, y - 12, x + 12, y + 12, fill="#6cb6ff", outline="white")

    def _draw_dial(self, dt):
        cv = self.dial_cv
        cv.delete("all")
        target = DIAL[self.model.dial][2]
        diff = (target - self.dial_draw_angle + 180) % 360 - 180
        step = 400 * dt
        self.dial_draw_angle += max(-step, min(step, diff))
        cv.create_oval(35, 35, 135, 135, fill="#39c6d6", outline="#1b8d99", width=3)
        cv.create_oval(60, 60, 110, 110, fill="#222", outline="")
        for i, (name, colour, angle, _) in enumerate(DIAL):
            a = math.radians(angle)
            x, y = 85 + 70 * math.cos(a), 85 - 70 * math.sin(a)
            width = 3 if i == self.model.dial else 1
            cv.create_rectangle(x - 10, y - 10, x + 10, y + 10, fill=colour, outline="white", width=width)
        cv.create_text(85, 163, text="↓ inventory", fill="#aaa", font=("Helvetica", 9))
        a = math.radians(self.dial_draw_angle)
        cv.create_line(85, 85, 85 + 50 * math.cos(a), 85 - 50 * math.sin(a),
                       fill="white", width=8, capstyle="round")
        cv.create_oval(80, 80, 90, 90, fill="#f2c200", outline="")

    def _draw_game(self, now):
        m = self.model
        cv = self.game_cv
        cv.delete("all")
        for gx in range(0, WORLD_W, 40):
            cv.create_line(gx, 0, gx, WORLD_H, fill="#34573a")
        for gy in range(0, WORLD_H, 40):
            cv.create_line(0, gy, WORLD_W, gy, fill="#34573a")

        fx, fy = m.facing
        face_deg = math.degrees(math.atan2(-fy, fx))
        for e in m.effects:
            t = (now - e["t0"]) / e["dur"]
            if e["kind"] == "melee":
                cv.create_arc(m.px - 42, m.py - 42, m.px + 42, m.py + 42, start=face_deg - 60,
                              extent=120, style="arc", outline="white", width=5)
            elif e["kind"] == "roll":
                for k in range(1, 4):
                    cv.create_oval(m.px - fx * 14 * k - 9, m.py - fy * 14 * k - 9,
                                   m.px - fx * 14 * k + 9, m.py - fy * 14 * k + 9,
                                   outline="#9fd3ff")
            elif e["kind"] == "artifact":
                r = 20 + 90 * t
                cv.create_oval(m.px - r, m.py - r, m.px + r, m.py + r, outline=e["colour"], width=4)
                cv.create_text(m.px, m.py - 40 - 20 * t, text=e["text"], fill=e["colour"],
                               font=("Helvetica", 16, "bold"))

        for p in m.projectiles:
            speed = math.hypot(p["vx"], p["vy"])
            ux, uy = p["vx"] / speed, p["vy"] / speed
            colour = "#f5deb3" if p["kind"] == "Bow" else "#ffb347"
            cv.create_line(p["x"] - ux * 16, p["y"] - uy * 16, p["x"], p["y"], fill=colour, width=3,
                           arrow="last")

        cv.create_oval(m.px - 12, m.py - 12, m.px + 12, m.py + 12, fill="#3aa0ff", outline="white", width=2)
        cv.create_line(m.px, m.py, m.px + fx * 22, m.py + fy * 22, fill="white", width=3)
        if m.charge_start is not None:
            c = m.bow_charge()
            cv.create_rectangle(m.px - 20, m.py - 26, m.px + 20, m.py - 21, outline="white")
            cv.create_rectangle(m.px - 20, m.py - 26, m.px - 20 + 40 * c, m.py - 21,
                                fill="#ffd54f" if c < 1 else "#ff5252", outline="")

        name, colour, _, action = DIAL[m.dial]
        cv.create_text(10, 10, anchor="nw", fill="white", font=("Helvetica", 12, "bold"),
                       text=f"Mode: {m.mode.upper()}    Melee: {m.melee}    Ranged: {m.weapon}")
        cv.create_text(10, 30, anchor="nw", fill=colour, font=("Helvetica", 12, "bold"),
                       text=f"Dial: {name} -> {action}")

        if m.mode == "inventory":
            self._draw_inventory(cv, m)

    def _draw_inventory(self, cv, m):
        cv.create_rectangle(60, 50, WORLD_W - 60, WORLD_H - 30, fill="#3b2f25", outline="#c9a26b", width=3)
        cv.create_text(WORLD_W / 2, 70, text="INVENTORY", fill="#f3e2c0", font=("Helvetica", 16, "bold"))
        cell_w, cell_h = 118, 80
        x0, y0 = (WORLD_W - INV_COLS * cell_w) / 2, 95
        for i, item in enumerate(INVENTORY_ITEMS):
            c, r = i % INV_COLS, i // INV_COLS
            x, y = x0 + c * cell_w, y0 + r * cell_h
            equipped = item in (m.melee, m.weapon)
            selected = i == m.inv_sel
            cv.create_rectangle(x + 4, y + 4, x + cell_w - 4, y + cell_h - 4,
                                fill="#5a4636" if not selected else "#8a6a3a",
                                outline="#ffe27a" if selected else "#7a6450", width=4 if selected else 1)
            cv.create_text(x + cell_w / 2, y + cell_h / 2, text=item, fill="white", width=cell_w - 14,
                           font=("Helvetica", 11, "bold" if equipped else "normal"), justify="center")
            if equipped:
                cv.create_text(x + cell_w - 10, y + 12, text="★", fill="#ffe27a", anchor="ne")
        cv.create_text(WORLD_W / 2, WORLD_H - 45, fill="#d8c7a5", font=("Helvetica", 11),
                       text="tilt = move cursor   force sensor = click   left/right button = close")


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

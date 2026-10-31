"""Posture Puck - Windows companion app.

Listens for Bluetooth broadcasts from the Puck.js worn at the back of your
neck, works out whether you've leaned away from the chair's head pillow, and
plays an alert sound after a grace period. Lives in the system tray.

    pythonw posture_alert.py            normal use (tray app, no console)
    python  posture_alert.py --monitor  print live readings for testing

Settings live in %APPDATA%\\PosturePuck\\config.json and are reloaded
automatically when you save the file.
"""

import argparse
import asyncio
import csv
import ctypes
import json
import math
import os
import struct
import sys
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

try:
    import winsound
except ImportError:  # not on Windows
    winsound = None

try:
    import psutil
except ImportError:
    psutil = None

from bleak import BleakScanner

ESPRUINO_ID = 0x0590
MAGIC = 0x50

APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "PosturePuck"
CONFIG_PATH = APP_DIR / "config.json"
LOG_DIR = APP_DIR / "logs"
SOUND_DIR = APP_DIR / "sounds"

DEFAULTS = {
    "contact": None,                  # field reading sitting back on the pillow
    "limit": None,                    # field reading at your furthest acceptable lean
    "grace_seconds_normal": 20,       # how long past the limit before alerting
    "grace_seconds_gaming": 45,
    "alert_repeat_seconds": 12,       # repeat the alert this often while still leaning
    "recovery_margin": 0.25,          # must come back 25% of the way to contact to count as fixed
    "sound_on_recovery": True,        # soft chime when you sit back after an alert
    "snooze_minutes": 10,
    "idle_seconds": 120,              # no keyboard/mouse for this long = away from desk
    "away_after_movement_seconds": 10,
    "headphones_off_still_seconds": 30,  # Puck hasn't changed angle for this long + off the pillow = headphones off
    "offline_seconds": 90,            # warn if the Puck goes quiet this long
    "calibration_countdown_seconds": 3,
    "volume": 0.6,                    # 0.0 - 1.0
    "game_processes": [],             # e.g. ["eldenring.exe", "cs2.exe"] -> gaming mode auto
    "show_notifications": True,
    "puck_address": None,             # set to your Puck's address if neighbours have Pucks too
}


# ---------------------------------------------------------------- config ----
def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass  # keep defaults; a half-saved file shouldn't crash the app
    return cfg


def save_config(cfg):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- sounds ----
SOUNDS = {
    "alert": [(660, 0.15, 0.05), (880, 0.15, 0.05), (1100, 0.25, 0.0)],
    "recovered": [(880, 0.10, 0.03), (1320, 0.18, 0.0)],
    "confirm1": [(1000, 0.08, 0.0)],
    "confirm2": [(1000, 0.08, 0.08)] * 2,
    "confirm3": [(1000, 0.08, 0.08)] * 3,
    "confirm4": [(1000, 0.08, 0.08)] * 4,
    "tick": [(700, 0.05, 0.0)],
    "error": [(440, 0.20, 0.05), (330, 0.30, 0.0)],
    "offline": [(523, 0.20, 0.10), (392, 0.30, 0.0)],
}


def write_tone(path, notes, volume):
    rate = 44100
    frames = bytearray()
    for freq, dur, gap in notes:
        n = int(rate * dur)
        for i in range(n):
            env = min(1.0, i / (0.01 * rate), (n - i) / (0.04 * rate))
            s = volume * env * math.sin(2 * math.pi * freq * i / rate)
            frames += struct.pack("<h", int(s * 32767))
        frames += b"\x00\x00" * int(rate * gap)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


def make_sounds(volume):
    SOUND_DIR.mkdir(parents=True, exist_ok=True)
    volume = max(0.0, min(1.0, float(volume)))
    for name, notes in SOUNDS.items():
        write_tone(SOUND_DIR / f"{name}.wav", notes, volume)


def play(name):
    if winsound is None:
        print(f"[sound] {name}")
        return
    path = SOUND_DIR / f"{name}.wav"
    winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)


# ----------------------------------------------------------- idle / games ----
class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def seconds_since_input():
    if sys.platform != "win32":
        return 0.0
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(lii)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    ctypes.windll.kernel32.GetTickCount.restype = ctypes.c_uint
    millis = (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF
    return millis / 1000.0


def game_running(names):
    if not names or psutil is None:
        return False
    wanted = {n.lower() for n in names}
    try:
        for p in psutil.process_iter(["name"]):
            if (p.info.get("name") or "").lower() in wanted:
                return True
    except Exception:
        pass
    return False


# ----------------------------------------------------------------- monitor ----
STATE_COLOURS = {
    "Good": (46, 160, 67),
    "Drifting": (230, 160, 30),
    "ALERT": (215, 58, 48),
    "Away": (130, 130, 130),
    "Headphones off": (130, 130, 130),
    "Snoozed": (130, 130, 130),
    "Paused": (130, 130, 130),
    "Needs calibration": (60, 120, 215),
    "Sensor offline": (90, 90, 90),
    "Waiting for Puck": (90, 90, 90),
    "Bluetooth problem": (90, 90, 90),
}


class PostureMonitor:
    def __init__(self, cfg):
        self.lock = threading.RLock()
        self.cfg = cfg
        self.cfg_mtime = CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else None
        self.icon = None

        self.smoothed = None
        self.moving = False
        self.last_move = 0.0
        self.last_packet = None
        self.battery = None
        self.rssi = None
        self.last_btn_seq = None
        self.adapter_error = None
        self.still_s = 0

        self.state = "Waiting for Puck"
        self.bad_since = None
        self.alerting = False
        self.last_alert = None
        self.snooze_until = 0.0
        self.paused = False
        self.gaming_manual = False
        self.gaming_auto = False
        self.offline_warned = False

        self.minute_key = None
        self.minute = {"good": 0.0, "drifting": 0.0, "alert": 0.0, "away": 0.0}
        self.today_key = datetime.now().strftime("%Y-%m-%d")
        self.today = {"good": 0.0, "drifting": 0.0, "alert": 0.0}
        self.last_tick = time.monotonic()
        self.last_cfg_check = 0.0
        self.last_game_check = 0.0

    # -- helpers --
    def notify(self, message):
        if self.icon is not None and self.cfg.get("show_notifications", True):
            try:
                self.icon.notify(message, "Posture Puck")
            except Exception:
                pass

    def calibrated(self):
        c, l = self.cfg.get("contact"), self.cfg.get("limit")
        return c is not None and l is not None and c != l

    def position(self):
        """1.0 = at contact, 0.0 = at your limit, below 0 = past the limit."""
        if self.smoothed is None or not self.calibrated():
            return None
        c, l = self.cfg["contact"], self.cfg["limit"]
        return (self.smoothed - l) / (c - l)

    def gaming(self):
        return self.gaming_manual or self.gaming_auto

    def snoozed(self):
        return time.monotonic() < self.snooze_until

    # -- incoming data --
    def set_adapter_error(self, err):
        with self.lock:
            self.adapter_error = err

    def headphones_off(self):
        """Puck motionless for a while and nowhere near the pillow."""
        limit = float(self.cfg.get("headphones_off_still_seconds", 30) or 0)
        if limit <= 0 or self.still_s < limit:
            return False
        pos = self.position()
        return pos is not None and pos < 0

    def on_packet(self, field, moving, btn_seq, btn_count, battery, still_s, rssi, address):
        want = self.cfg.get("puck_address")
        if want and address and address.upper() != str(want).upper():
            return
        gesture = None
        with self.lock:
            now = time.monotonic()
            if not math.isfinite(field):
                return
            self.smoothed = field if self.smoothed is None else self.smoothed + 0.4 * (field - self.smoothed)
            self.moving = moving
            if moving:
                self.last_move = now
            self.battery, self.rssi = battery, rssi
            self.still_s = still_s
            if self.last_packet is None or self.offline_warned:
                self.offline_warned = False
            self.last_packet = now
            if self.last_btn_seq is None:
                self.last_btn_seq = btn_seq          # don't replay an old gesture at startup
            elif btn_seq != self.last_btn_seq:
                self.last_btn_seq = btn_seq
                gesture = btn_count
        if gesture:
            self.handle_gesture(gesture)

    def handle_gesture(self, presses):
        if presses == 1:
            self.toggle_snooze()
        elif presses == 2:
            self.set_point("contact")
        elif presses == 3:
            self.set_point("limit")
        elif presses == 4:
            self.toggle_gaming()

    # -- actions (tray menu and Puck button) --
    def toggle_snooze(self):
        with self.lock:
            if self.snoozed():
                self.snooze_until = 0.0
                play("tick")
            else:
                self.snooze_until = time.monotonic() + 60 * float(self.cfg["snooze_minutes"])
                self.bad_since, self.alerting = None, False
                play("confirm1")

    def toggle_gaming(self):
        with self.lock:
            self.gaming_manual = not self.gaming_manual
        play("confirm4")
        self.notify("Gaming mode on (longer grace period)" if self.gaming_manual else "Gaming mode off")

    def toggle_pause(self):
        with self.lock:
            self.paused = not self.paused
            self.bad_since, self.alerting = None, False
        play("tick")

    def set_point(self, which):
        with self.lock:
            value = self.smoothed
            if value is None:
                play("error")
                self.notify("No reading from the Puck yet, so calibration wasn't saved.")
                return
            self.cfg[which] = round(float(value), 2)
            save_config(self.cfg)
            self.cfg_mtime = CONFIG_PATH.stat().st_mtime
            self.bad_since, self.alerting = None, False
            c, l = self.cfg.get("contact"), self.cfg.get("limit")
        if c is not None and l is not None and abs(c - l) < 0.05 * max(abs(c), abs(l), 1e-9):
            play("error")
            self.notify("Contact and limit readings are almost the same. "
                        "Sit right back for contact, and lean further for the limit.")
            return
        play("confirm2" if which == "contact" else "confirm3")
        label = "Contact" if which == "contact" else "Limit"
        self.notify(f"{label} saved ({value:.0f}).")

    def calibrate_with_countdown(self, which):
        def run():
            for _ in range(int(self.cfg.get("calibration_countdown_seconds", 3))):
                play("tick")
                time.sleep(1)
            self.set_point(which)
        threading.Thread(target=run, daemon=True).start()

    # -- main loop, called ~4x per second --
    def tick(self):
        now = time.monotonic()
        with self.lock:
            dt, self.last_tick = now - self.last_tick, now
            self.maybe_reload_config(now)
            if now - self.last_game_check > 10:
                self.last_game_check = now
                self.gaming_auto = game_running(self.cfg.get("game_processes"))

            category = "other"
            if self.paused:
                self.state = "Paused"
            elif self.adapter_error:
                self.state = "Bluetooth problem"
            elif self.last_packet is None:
                self.state = "Waiting for Puck"
            elif now - self.last_packet > self.cfg["offline_seconds"]:
                self.state = "Sensor offline"
                if not self.offline_warned:
                    self.offline_warned = True
                    play("offline")
                    self.notify("Haven't heard from the Puck for a while. Is it on, and is the battery OK?")
            elif not self.calibrated():
                self.state = "Needs calibration"
            elif self.snoozed():
                self.state = "Snoozed"
            elif (seconds_since_input() > self.cfg["idle_seconds"]
                  or now - self.last_move < self.cfg["away_after_movement_seconds"]):
                self.state = "Away"
                category = "away"
                self.bad_since, self.alerting = None, False
            elif self.headphones_off():
                self.state = "Headphones off"
                category = "away"
                self.bad_since, self.alerting = None, False
            else:
                category = self.evaluate_posture(now)

            self.accumulate(category, dt)
        self.update_icon()

    def evaluate_posture(self, now):
        pos = self.position()
        margin = float(self.cfg.get("recovery_margin", 0.25))
        if self.bad_since is None:
            if pos < 0:
                self.bad_since = now
        elif pos > margin:
            if self.alerting and self.cfg.get("sound_on_recovery", True):
                play("recovered")
            self.bad_since, self.alerting, self.last_alert = None, False, None

        if self.bad_since is None:
            self.state = "Good"
            return "good"

        grace = self.cfg["grace_seconds_gaming"] if self.gaming() else self.cfg["grace_seconds_normal"]
        if now - self.bad_since >= grace:
            if not self.alerting:
                self.alerting = True
                self.notify("Sit back: you've been leaning in for a while.")
            if self.last_alert is None or now - self.last_alert >= self.cfg["alert_repeat_seconds"]:
                self.last_alert = now
                play("alert")
            self.state = "ALERT"
            return "alert"
        self.state = "Drifting"
        return "drifting"

    def maybe_reload_config(self, now):
        if now - self.last_cfg_check < 2:
            return
        self.last_cfg_check = now
        try:
            mtime = CONFIG_PATH.stat().st_mtime
        except OSError:
            return
        if mtime != self.cfg_mtime:
            self.cfg_mtime = mtime
            old_volume = self.cfg.get("volume")
            self.cfg = load_config()
            if self.cfg.get("volume") != old_volume:
                make_sounds(self.cfg.get("volume", 0.6))

    # -- stats & logging --
    def accumulate(self, category, dt):
        day = datetime.now().strftime("%Y-%m-%d")
        minute = datetime.now().strftime("%Y-%m-%d %H:%M")
        if self.minute_key is None:
            self.minute_key = minute
        if minute != self.minute_key:
            self.flush_minute()
            self.minute_key = minute
        if day != self.today_key:
            self.today_key = day
            self.today = {"good": 0.0, "drifting": 0.0, "alert": 0.0}
        if category in self.minute:
            self.minute[category] += dt
        if category in self.today:
            self.today[category] += dt

    def flush_minute(self):
        if self.minute_key is None or sum(self.minute.values()) < 1:
            self.minute = {k: 0.0 for k in self.minute}
            return
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"{self.minute_key[:10]}.csv"
        new = not path.exists()
        try:
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["minute", "good_s", "drifting_s", "alert_s", "away_s"])
                w.writerow([self.minute_key] + [round(self.minute[k]) for k in ("good", "drifting", "alert", "away")])
        except OSError:
            pass
        self.minute = {k: 0.0 for k in self.minute}

    # -- display --
    def reading_text(self):
        with self.lock:
            if self.smoothed is None:
                return "No reading yet"
            parts = [f"Field {self.smoothed:.0f}"]
            pos = self.position()
            if pos is not None:
                parts.append(f"position {max(0, min(100, pos * 100)):.0f}%")
            if self.battery is not None:
                parts.append(f"battery {self.battery}%")
            return " | ".join(parts)

    def today_text(self):
        with self.lock:
            seated = sum(self.today.values())
            if seated < 60:
                return "Today: not enough data yet"
            pct = 100 * self.today["good"] / seated
            return f"Today: {pct:.0f}% upright over {seated / 60:.0f} min"

    def mode_text(self):
        with self.lock:
            if self.gaming_auto and not self.gaming_manual:
                return "Gaming mode (auto: game running)"
            return "Gaming mode (longer grace)"

    def update_icon(self):
        if self.icon is None:
            return
        with self.lock:
            state = self.state
            tip = f"Posture Puck: {state}"
        if getattr(self, "_shown_state", None) != state:
            self._shown_state = state
            self.icon.icon = make_icon(state)
            self.icon.title = tip[:120]
            try:
                self.icon.update_menu()
            except Exception:
                pass


def make_icon(state):
    from PIL import Image, ImageDraw
    colour = STATE_COLOURS.get(state, (90, 90, 90))
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=colour + (255,), outline=(255, 255, 255, 255), width=3)
    return img


# --------------------------------------------------------------- bluetooth ----
def parse(adv):
    data = adv.manufacturer_data.get(ESPRUINO_ID)
    if not data or len(data) < 10 or data[0] != MAGIC:
        return None
    field = struct.unpack_from("<f", data, 2)[0]
    still = data[10] if data[1] >= 2 and len(data) >= 11 else 0  # older firmware: no stillness info
    return field, bool(data[6] & 1), data[7], data[8], data[9], still


async def ble_loop(mon, stop):
    def callback(device, adv):
        p = parse(adv)
        if p:
            mon.on_packet(*p, adv.rssi, device.address)

    while not stop.is_set():
        try:
            async with BleakScanner(detection_callback=callback):
                mon.set_adapter_error(None)
                started = time.monotonic()
                while not stop.is_set():
                    await asyncio.sleep(0.5)
                    # Windows can silently stop a scan (adapter replugged, driver
                    # reloaded, PC woke from sleep) without raising an error.
                    # If nothing has been heard for 30 s, restart the scanner.
                    last = mon.last_packet or 0.0
                    if time.monotonic() - max(last, started) > 30:
                        break
        except Exception as e:  # adapter unplugged, Bluetooth switched off, etc.
            mon.set_adapter_error(str(e))
            await asyncio.sleep(10)


async def monitor_mode():
    print("Listening for the Puck... (Ctrl+C to stop)")
    print("Hold it where it will sit on your neck, sit back, then lean forward to see the change.\n")
    last = [0.0]

    def callback(device, adv):
        p = parse(adv)
        if not p or time.monotonic() - last[0] < 0.5:
            return
        last[0] = time.monotonic()
        field, moving, seq, count, batt, still = p
        print(f"field {field:9.1f}   moving {'yes' if moving else 'no '}   still {still:3d}s   "
              f"battery {batt:3d}%   signal {adv.rssi:4d} dBm   button #{seq}x{count}   [{device.address}]")

    async with BleakScanner(detection_callback=callback):
        while True:
            await asyncio.sleep(1)


# ------------------------------------------------------------------- main ----
def single_instance():
    if sys.platform != "win32":
        return True
    ctypes.windll.kernel32.CreateMutexW(None, False, "PosturePuckSingleInstance")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def run_tray():
    import pystray

    cfg = load_config()
    if not CONFIG_PATH.exists():
        save_config(cfg)
    make_sounds(cfg.get("volume", 0.6))
    mon = PostureMonitor(cfg)
    stop = threading.Event()

    threading.Thread(target=lambda: asyncio.run(ble_loop(mon, stop)), daemon=True).start()

    def ticker():
        while not stop.is_set():
            try:
                mon.tick()
            except Exception as e:
                print("tick error:", e)
            time.sleep(0.25)

    def on_quit(icon):
        stop.set()
        with mon.lock:
            mon.flush_minute()
        icon.stop()

    M = pystray.MenuItem
    menu = pystray.Menu(
        M(lambda i: f"Status: {mon.state}", None, enabled=False),
        M(lambda i: mon.reading_text(), None, enabled=False),
        M(lambda i: mon.today_text(), None, enabled=False),
        pystray.Menu.SEPARATOR,
        M(lambda i: "Cancel snooze" if mon.snoozed() else f"Snooze {mon.cfg['snooze_minutes']} min",
          lambda: mon.toggle_snooze()),
        M(lambda i: mon.mode_text(), lambda: mon.toggle_gaming(), checked=lambda i: mon.gaming()),
        M("Pause", lambda: mon.toggle_pause(), checked=lambda i: mon.paused),
        pystray.Menu.SEPARATOR,
        M("Calibrate", pystray.Menu(
            M("Set contact (sit back on the pillow)", lambda: mon.calibrate_with_countdown("contact")),
            M("Set limit (lean to your furthest OK point)", lambda: mon.calibrate_with_countdown("limit")),
        )),
        M("Open settings file", lambda: os.startfile(str(CONFIG_PATH))),
        M("Open log folder", lambda: (LOG_DIR.mkdir(parents=True, exist_ok=True), os.startfile(str(LOG_DIR)))),
        pystray.Menu.SEPARATOR,
        M("Quit", on_quit),
    )

    icon = pystray.Icon("PosturePuck", make_icon(mon.state), "Posture Puck", menu)
    mon.icon = icon

    def setup(icon):
        icon.visible = True
        threading.Thread(target=ticker, daemon=True).start()
        if not mon.calibrated():
            mon.notify("Running. Right-click the tray icon > Calibrate to set your contact and limit.")

    icon.run(setup=setup)
    stop.set()


def main():
    ap = argparse.ArgumentParser(description="Posture Puck companion app")
    ap.add_argument("--monitor", action="store_true", help="print live readings instead of running the tray app")
    args = ap.parse_args()
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if args.monitor:
        try:
            asyncio.run(monitor_mode())
        except KeyboardInterrupt:
            pass
        return
    if not single_instance():
        return
    run_tray()


if __name__ == "__main__":
    main()

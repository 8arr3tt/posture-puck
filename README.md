# Posture Puck

A Puck.js worn at the back of your neck reads the magnets in your Secretlab head pillow. A small Windows tray app listens over Bluetooth and plays an alert when you've leaned away from the pillow for too long.

## What's in this folder

| File | What it is |
|---|---|
| `posture_puck.js` | Firmware for the Puck.js (uploaded once from a web browser) |
| `posture_alert.py` | The Windows tray app |
| `setup.bat` | Installs the app's packages and adds it to Windows startup |
| `requirements.txt` | Python packages the app needs |
| `bt_test.py` | 30-second test of how well your PC receives Bluetooth broadcasts |

## 1. Bluetooth on the PC

Plug in the USB Bluetooth adapter (e.g. TP-Link UB500). Windows installs it automatically. Check that Bluetooth shows as **On** in Settings > Bluetooth & devices. You don't need to pair the Puck: the app just listens for its broadcasts.

## 2. Install the app

1. Install Python 3.11 or newer from python.org. **Tick "Add python.exe to PATH"** in the installer.
2. Double-click `setup.bat`. It installs the packages, adds the app to startup and starts it.
3. A grey dot appears in the system tray (you may need to click the ^ arrow to see it). Drag it onto the taskbar so it's always visible.

## 3. Program the Puck

1. In **Chrome or Edge**, open https://www.espruino.com/ide
2. Click the connect icon (top left) and choose your Puck. If the IDE offers a firmware update, accept it first.
3. Open the settings (cog icon, top right) > **Communications** > set **Save on Send** to **Direct to Flash (execute code at boot)**.
4. Paste the contents of `posture_puck.js` into the right-hand editor and click **Send to Espruino** (the upload arrow).
5. The Puck's green LED blinks once when it starts. **Disconnect the IDE afterwards**: the Puck stops broadcasting while the IDE is connected.

## 4. Check the readings (optional but worth it)

Open a command prompt in this folder and run:

```
python posture_alert.py --monitor
```

Hold the Puck where it will sit on your neck. Sit back against the pillow, then lean forward. The `field` number should change clearly. This is also where you'll see the Puck's address if you ever need it. Close the tray app first if it's running, or just run this before setup.

## 5. Wear it

Put the Puck in a pocket on a soft collar, **at the back and slightly to one side of your neck**, not dead centre. The whole top face is a button, so keep it out from under your head's weight. Long holds are ignored anyway, so accidental pressure won't do anything.

## 6. Calibrate

Either use the tray menu (right-click the dot > Calibrate) or the Puck's button:

| Do this | Tray menu | Puck button |
|---|---|---|
| Sit back with your neck on the pillow | Set contact | 2 presses |
| Lean to the furthest point you're OK with | Set limit | 3 presses |

The tray menu gives you a 3-tick countdown to get into position. Each saved point plays confirmation beeps (2 or 3). A low two-tone sound means it didn't work. Usually that means contact and limit were too similar, so lean further for the limit.

## Everyday use

| Puck button | Does |
|---|---|
| 1 press | Snooze 10 minutes (press again to cancel) |
| 2 presses | Set contact |
| 3 presses | Set limit |
| 4 presses | Toggle gaming mode |

**Tray icon colours:**
- **Green:** good
- **Amber:** past your limit, grace period running
- **Red:** alerting
- **Grey:** away, snoozed, paused or offline
- **Blue:** needs calibration

Hover over the icon or right-click it for status, the live reading, battery level and today's upright percentage.

**How it decides:**
- You have to stay past your limit for the whole grace period (20 s normally, 45 s in gaming mode) before the alert plays.
- The alert repeats every 12 s until you sit back.
- A soft chime plays when you do.
- It stays quiet when there's been no keyboard or mouse input for 2 minutes, or when the Puck senses you standing up or walking.
- It also stays quiet when the headphones are off. A Puck sat on the desk is perfectly still, which never happens on your head, so after 30 seconds of stillness away from the pillow it shows "Headphones off". You might get one chime when you first put them down.

## Settings

Right-click > **Open settings file**. Edit and save; the app picks up changes within a couple of seconds.

| Setting | Default | Meaning |
|---|---|---|
| `grace_seconds_normal` | 20 | Seconds past the limit before alerting |
| `grace_seconds_gaming` | 45 | Same, in gaming mode |
| `alert_repeat_seconds` | 12 | How often the alert repeats while you're still leaning |
| `recovery_margin` | 0.25 | How far back towards contact you must come to count as fixed (0.25 = a quarter of the way) |
| `sound_on_recovery` | true | Chime when you sit back after an alert |
| `snooze_minutes` | 10 | Snooze length |
| `idle_seconds` | 120 | No keyboard or mouse input for this long counts as away |
| `away_after_movement_seconds` | 10 | Stay quiet this long after the Puck senses walking |
| `headphones_off_still_seconds` | 30 | If the Puck hasn't changed angle for this long and you're off the pillow, the headphones are off (on the desk), so stay quiet. 0 turns this off |
| `offline_seconds` | 90 | Warn once if the Puck goes silent this long |
| `volume` | 0.6 | Alert volume, 0.0 to 1.0 |
| `game_processes` | [] | Games that switch on gaming mode automatically, e.g. `["cs2.exe", "eldenring.exe"]` (find the name in Task Manager > Details) |
| `show_notifications` | true | Windows pop-up notifications alongside sounds |
| `puck_address` | null | Only listen to this Puck (address shown by `--monitor`) |
| `contact`, `limit` | | Your calibration values (set via calibration, but you can nudge them by hand) |

Logs: one CSV per day in the log folder (right-click > Open log folder), with seconds per minute spent good, drifting, alerting and away.

## Troubleshooting

- **Tray says "Waiting for Puck" or "Sensor offline":**
  - Make sure the IDE isn't still connected to the Puck.
  - Check the battery: the tray menu shows its percentage, and readings can go odd below about 30%.
  - Run `--monitor` to see whether anything is arriving.
- **"Bluetooth problem":** the adapter isn't plugged in or Bluetooth is switched off. The app retries every 10 seconds.
- **No sound:** alerts play through your default Windows output device, so make sure your headset is set as the default.
- **Too twitchy:** increase the grace period, or re-calibrate the limit a little further forward.
- **Not catching slouches:** re-calibrate the limit less far forward.
- **Uninstall:** quit from the tray, then delete `PosturePuck.vbs` from your Startup folder (press Win+R and type `shell:startup`).

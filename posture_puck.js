// Posture Puck firmware for Espruino Puck.js v2 / v2.1
//
// The Puck sits on your headphones (or a collar). It measures the magnetic
// field from the chair's pillow magnets and broadcasts it over Bluetooth for
// the Windows companion app, which decides when to alert.
//
// Built to sip power from a CR2032:
//   - Active (being worn): 1 magnetometer reading and 1 broadcast per second,
//     accelerometer in its 1.6 Hz low-power mode.
//   - Resting (hasn't changed angle for a minute, e.g. headphones on the desk):
//     a magnetometer reading every 10 s and a broadcast every 2 s. Wakes up as
//     soon as it's moved, or if the field changes noticeably.
//
// Broadcast (Espruino manufacturer ID 0x0590), 11 bytes:
//   [0]   0x50 magic ('P')
//   [1]   format version (2)
//   [2-5] smoothed field strength, float32 little-endian (raw sensor units)
//   [6]   flags: bit0 = moving (standing up / walking), bit1 = resting (low-power)
//   [7]   button gesture sequence number (changes on every new gesture)
//   [8]   number of presses in that gesture (1-9)
//   [9]   battery percentage
//   [10]  seconds since the Puck last changed angle (capped at 255).
//         Worn on a head it never stays still for long; sat on a desk it does,
//         so the PC app uses this to tell when the headphones are off.
//
// Upload from https://www.espruino.com/ide with
// Settings > Communications > Save on Send = "Direct to Flash (execute code at boot)".
// Disconnect the IDE afterwards: the Puck stops broadcasting while connected.

// ---- Settings -------------------------------------------------------------
var ACTIVE_MAG_MS = 1000;   // magnetometer reading interval while worn
var RESTING_MAG_MS = 10000; // ...and while resting
var ACTIVE_ADVERT_MS = 1000;
var RESTING_ADVERT_MS = 2000;
var REST_AFTER_S = 60;      // still for this long = resting (low power)
var WAKE_FIELD_CHANGE = 0.08; // a field change of 8%+ while resting wakes it up
var MAG_SMOOTH = 0.5;       // 0..1, higher reacts faster, lower is steadier
var MOVE_THRESHOLD = 0.15;  // a sample differing from 1 g by 15%+ is a jolt
var MOVE_WINDOW_S = 5;      // look at the last 5 seconds...
var MOVE_MIN_JOLTS = 2;     // ...and call it "moving" if 2+ jolts happened
var PRESS_GAP_MS = 700;     // presses closer together than this are one gesture
var LONG_PRESS_MS = 1000;   // holds longer than this are ignored (head pressure)
var STILL_ANGLE_DEG = 2;    // turning more than this counts as "moved"

// ---- State ----------------------------------------------------------------
var field = 0, haveField = false;
var gravity = 0, jolts = [], moving = false;
var btnSeq = 0, btnCount = 0;
var pressCount = 0, pressTimer, pressStart = 0;
var battery = 100;
var tiltRef = null, tiltRefTime = 0;
var COS_STILL = Math.cos(STILL_ANGLE_DEG * Math.PI / 180);
var resting = false;
var magTimer, advertTimer;

// ---- Magnetometer: one-shot readings on a timer ------------------------------
function readMag() {
  var xyz;
  try { xyz = Puck.mag(); } catch (e) { return; }
  var m = Math.sqrt(xyz.x * xyz.x + xyz.y * xyz.y + xyz.z * xyz.z);
  if (!haveField) { field = m; haveField = true; return; }
  if (resting && Math.abs(m - field) > WAKE_FIELD_CHANGE * Math.abs(field)) {
    field = m;           // jump straight to the new value
    wake();
    return;
  }
  field += (m - field) * MAG_SMOOTH;
}

// ---- Accelerometer (1.6 Hz low-power mode) ------------------------------------
function cosAngle(p, q) {
  var d = p.x * q.x + p.y * q.y + p.z * q.z;
  var n = Math.sqrt(p.x * p.x + p.y * p.y + p.z * p.z) * Math.sqrt(q.x * q.x + q.y * q.y + q.z * q.z);
  return n ? d / n : 1;
}

function onAccel(a) {
  var v = a.acc, now = getTime();
  var g = Math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z);
  if (!gravity) gravity = g;
  gravity += (g - gravity) * 0.05;           // slow average = 1 g in raw units
  if (Math.abs(g - gravity) / gravity > MOVE_THRESHOLD) jolts.push(now);
  while (jolts.length && now - jolts[0] > MOVE_WINDOW_S) jolts.shift();
  moving = jolts.length >= MOVE_MIN_JOLTS;

  // Angle tracking: has the Puck turned more than STILL_ANGLE_DEG?
  if (!tiltRef || cosAngle(v, tiltRef) < COS_STILL) {
    tiltRef = { x: v.x, y: v.y, z: v.z };
    tiltRefTime = now;
    if (resting) wake();
  } else if (!resting && now - tiltRefTime > REST_AFTER_S) {
    rest();
  }
}

function stillSeconds() {
  return tiltRef ? Math.min(255, Math.floor(getTime() - tiltRefTime)) : 0;
}

// ---- Power modes ---------------------------------------------------------------
function setTimers(magMs, advertMs) {
  if (magTimer) clearInterval(magTimer);
  if (advertTimer) clearInterval(advertTimer);
  magTimer = setInterval(readMag, magMs);
  advertTimer = setInterval(advertise, advertMs);
  advertise();
}

function wake() {
  resting = false;
  setTimers(ACTIVE_MAG_MS, ACTIVE_ADVERT_MS);
}

function rest() {
  resting = true;
  setTimers(RESTING_MAG_MS, RESTING_ADVERT_MS);
}

// ---- Button gestures --------------------------------------------------------
function startButton() {
  setWatch(function (e) {
    if (e.state) { pressStart = e.time; return; }
    var held = (e.time - pressStart) * 1000;
    if (held >= LONG_PRESS_MS) {
      // Long hold, probably your head leaning on it: cancel any gesture
      pressCount = 0;
      if (pressTimer) { clearTimeout(pressTimer); pressTimer = undefined; }
      return;
    }
    pressCount++;
    LED2.set();
    setTimeout(function () { LED2.reset(); }, 30);
    if (pressTimer) clearTimeout(pressTimer);
    pressTimer = setTimeout(function () {
      pressTimer = undefined;
      btnCount = Math.min(pressCount, 9);
      btnSeq = (btnSeq + 1) & 255;
      pressCount = 0;
      if (resting) wake();
      readMag();
      advertise();       // send the gesture straight away
    }, PRESS_GAP_MS);
  }, BTN, { edge: "both", repeat: true, debounce: 30 });
}

// ---- Broadcast ----------------------------------------------------------------
function advertise() {
  var buf = new Uint8Array(11);
  var dv = new DataView(buf.buffer);
  buf[0] = 0x50;
  buf[1] = 2;
  dv.setFloat32(2, field, true);
  buf[6] = (moving ? 1 : 0) | (resting ? 2 : 0);
  buf[7] = btnSeq;
  buf[8] = btnCount;
  buf[9] = battery;
  buf[10] = stillSeconds();
  var arr = [];
  for (var i = 0; i < buf.length; i++) arr.push(buf[i]);
  NRF.setAdvertising({}, {
    name: "Posture",
    manufacturer: 0x0590,
    manufacturerData: arr,
    interval: resting ? RESTING_ADVERT_MS : ACTIVE_ADVERT_MS
  });
}

function readBattery() {
  battery = E.clip(Math.round(E.getBattery()), 0, 100);
}

// ---- Start --------------------------------------------------------------------
function start() {
  try { Puck.magOff(); } catch (e) {}   // in case older firmware left it running
  readBattery();
  setInterval(readBattery, 300000);     // every 5 minutes
  readMag();
  Puck.accelOn(1.6);                    // low-power mode, no gyro
  Puck.on('accel', onAccel);
  startButton();
  wake();
  // Green blink = running
  LED2.set();
  setTimeout(function () { LED2.reset(); }, 300);
}

start();

// Posture Puck firmware for Espruino Puck.js v2 / v2.1
//
// The Puck sits in a collar at the back/side of your neck. It measures the
// magnetic field from the chair's pillow magnets and broadcasts it over
// Bluetooth for the Windows companion app, which decides when to alert.
//
// Broadcast (Espruino manufacturer ID 0x0590), 11 bytes:
//   [0]   0x50 magic ('P')
//   [1]   format version (2)
//   [2-5] smoothed field strength, float32 little-endian (raw sensor units)
//   [6]   flags: bit0 = moving (standing up / walking)
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
var MAG_HZ = 5;            // magnetometer readings per second
var ACCEL_HZ = 12.5;       // accelerometer readings per second
var ADVERT_MS = 250;       // how often the broadcast is refreshed
var MAG_SMOOTH = 0.35;     // 0..1, higher reacts faster, lower is steadier
var MOVE_THRESHOLD = 0.12; // jolt = acceleration differing from 1 g by 12%+
var MOVE_WINDOW_MS = 3000; // look at the last 3 seconds...
var MOVE_MIN_JOLTS = 6;    // ...and call it "moving" if 6+ jolts happened
var PRESS_GAP_MS = 700;    // presses closer together than this are one gesture
var LONG_PRESS_MS = 1000;  // holds longer than this are ignored (head pressure)
var STILL_ANGLE_DEG = 2;   // turning more than this counts as "moved" for headphones-off detection

// ---- State ----------------------------------------------------------------
var field = 0, haveField = false;
var gravity = 0, jolts = [], moving = false;
var btnSeq = 0, btnCount = 0;
var pressCount = 0, pressTimer, pressStart = 0;
var battery = 100;
var tilt = null, tiltRef = null, tiltRefTime = 0;
var COS_STILL = Math.cos(STILL_ANGLE_DEG * Math.PI / 180);

// ---- Magnetometer -----------------------------------------------------------
function onMag(xyz) {
  var m = Math.sqrt(xyz.x * xyz.x + xyz.y * xyz.y + xyz.z * xyz.z);
  if (!haveField) { field = m; haveField = true; }
  else field += (m - field) * MAG_SMOOTH;
}

function startMag() {
  try {
    Puck.magOn(MAG_HZ);
    Puck.on('mag', onMag);
  } catch (e) {
    // Fall back to polling if this rate isn't supported on this Puck version
    setInterval(function () { onMag(Puck.mag()); }, 1000 / MAG_HZ);
  }
}

// ---- Accelerometer: detects standing up / walking ---------------------------
function onAccel(a) {
  var g = Math.sqrt(a.acc.x * a.acc.x + a.acc.y * a.acc.y + a.acc.z * a.acc.z);
  if (!gravity) gravity = g;
  gravity += (g - gravity) * 0.02;            // slow average = 1 g in raw units
  var now = getTime() * 1000;
  if (Math.abs(g - gravity) / gravity > MOVE_THRESHOLD) jolts.push(now);
  while (jolts.length && now - jolts[0] > MOVE_WINDOW_MS) jolts.shift();
  moving = jolts.length >= MOVE_MIN_JOLTS;

  // Track the Puck's angle (smoothed, so desk bumps and typing average out)
  if (!tilt) tilt = { x: a.acc.x, y: a.acc.y, z: a.acc.z };
  else {
    tilt.x += (a.acc.x - tilt.x) * 0.1;
    tilt.y += (a.acc.y - tilt.y) * 0.1;
    tilt.z += (a.acc.z - tilt.z) * 0.1;
  }
  if (!tiltRef || cosAngle(tilt, tiltRef) < COS_STILL) {
    tiltRef = { x: tilt.x, y: tilt.y, z: tilt.z };
    tiltRefTime = getTime();
  }
}

function cosAngle(p, q) {
  var d = p.x * q.x + p.y * q.y + p.z * q.z;
  var n = Math.sqrt(p.x * p.x + p.y * p.y + p.z * p.z) * Math.sqrt(q.x * q.x + q.y * q.y + q.z * q.z);
  return n ? d / n : 1;
}

function stillSeconds() {
  return tiltRef ? Math.min(255, Math.floor(getTime() - tiltRefTime)) : 0;
}

function startAccel() {
  Puck.accelOn(ACCEL_HZ);
  Puck.on('accel', onAccel);
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
    setTimeout(function () { LED2.reset(); }, 60);
    if (pressTimer) clearTimeout(pressTimer);
    pressTimer = setTimeout(function () {
      pressTimer = undefined;
      btnCount = Math.min(pressCount, 9);
      btnSeq = (btnSeq + 1) & 255;
      pressCount = 0;
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
  buf[6] = moving ? 1 : 0;
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
    interval: ADVERT_MS
  });
}

function readBattery() {
  battery = E.clip(Math.round(E.getBattery()), 0, 100);
}

// ---- Start --------------------------------------------------------------------
function start() {
  readBattery();
  setInterval(readBattery, 60000);
  startMag();
  startAccel();
  startButton();
  setInterval(advertise, ADVERT_MS);
  // Green blink = running
  LED2.set();
  setTimeout(function () { LED2.reset(); }, 500);
}

start();

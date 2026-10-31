"""Bluetooth listening test for Posture Puck.

Listens for Bluetooth LE broadcasts for 30 seconds using the same library the
tray app uses, then reports how reliably your PC received them.

    pip install bleak
    python bt_test.py
"""
import asyncio
import struct
import time
from collections import defaultdict

from bleak import BleakScanner

DURATION = 30
ESPRUINO_ID = 0x0590

counts = defaultdict(int)
rssi_sum = defaultdict(int)
names = {}
per_second = defaultdict(int)
puck_seen = []


def callback(device, adv):
    now = time.monotonic()
    counts[device.address] += 1
    rssi_sum[device.address] += adv.rssi
    if adv.local_name:
        names[device.address] = adv.local_name
    per_second[int(now - start)] += 1
    data = adv.manufacturer_data.get(ESPRUINO_ID)
    if data and len(data) >= 10 and data[0] == 0x50:
        puck_seen.append((struct.unpack_from("<f", data, 2)[0], data[9]))


async def main():
    global start
    print(f"Listening for {DURATION} seconds...")
    start = time.monotonic()
    async with BleakScanner(detection_callback=callback):
        for i in range(DURATION):
            await asyncio.sleep(1)
            print(f"  {i + 1:2d}s: {per_second.get(i, 0):4d} broadcasts, {len(counts):3d} devices so far")

    total = sum(counts.values())
    print(f"\nTotal: {total} broadcasts from {len(counts)} devices "
          f"({total / DURATION:.1f} per second)\n")
    print("Busiest devices:")
    for addr, n in sorted(counts.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {addr}  {n / DURATION:5.1f}/s  signal {rssi_sum[addr] / n:6.1f} dBm  {names.get(addr, '')}")

    gaps = [s for s in range(DURATION) if per_second.get(s, 0) == 0]
    print()
    if total == 0:
        print("RESULT: nothing received. Check Bluetooth is on, or plan on the USB dongle.")
    elif gaps:
        print(f"RESULT: received broadcasts, but with {len(gaps)} silent seconds. "
              "Workable, but if the Puck drops out often, the dongle may be steadier.")
    else:
        print("RESULT: steady reception every second. Your built-in Bluetooth should be fine.")
    if puck_seen:
        field, batt = puck_seen[-1]
        print(f"Posture Puck broadcast detected: field {field:.1f}, battery {batt}%. "
              f"Received {len(puck_seen) / DURATION:.1f} per second.")


if __name__ == "__main__":
    asyncio.run(main())

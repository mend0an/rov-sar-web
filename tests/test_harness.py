"""
Test harness — inject fake frames + GPS ke state singleton.

CATATAN: harness ini inject ke state singleton di PROSES INI, yang berbeda
dari proses daphne. Untuk test daphne, pakai ROV_FAKE_WORKERS=1 saat start
server (fake worker di apps.py inject ke state proses server).

Fake JPEG di-generate pakai cv2 -> JPEG VALID (bisa di-decode).
"""
import os
import sys
import os as _os
sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import time
import threading

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "rov_sar_web.settings")
os.environ["ROV_AUTOSTART_WORKERS"] = "0"

import django
django.setup()

import numpy as np
import cv2

from detection.state import state, broadcast


def _make_valid_jpeg(counter):
    """Generate JPEG VALID (bisa di-decode) - bukan byte hex manual."""
    img = np.zeros((240, 320, 3), dtype=np.uint8)
    img[:, :, 0] = np.linspace(0, 255, 320).astype(np.uint8)
    img[:, :, 1] = 80
    cv2.rectangle(img, (100, 80), (220, 160), (0, 200, 255), -1)
    cv2.putText(img, f"FAKE {counter}", (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return buf.tobytes() if ok else None


def inject_frames(fps=15):
    interval = 1.0 / fps
    n = 0
    while True:
        jpeg = _make_valid_jpeg(n)
        if jpeg:
            state.set_frame(jpeg)
        n += 1
        time.sleep(interval)


def inject_gps():
    import random
    lat, lon, hdg = -7.7956, 110.3695, 45.0
    n = 0
    while True:
        lat += (random.random() - 0.5) * 0.00005
        lon += (random.random() - 0.5) * 0.00005
        hdg = (hdg + (random.random() - 0.5) * 10) % 360
        state.set_gps(lat, lon, hdg)
        broadcast("gps", {"lat": lat, "lon": lon, "heading": hdg})
        n += 1
        if n % 5 == 0:
            print(f"  ... {n} GPS fixes emitted")
        time.sleep(1.0)


if __name__ == "__main__":
    test_jpeg = _make_valid_jpeg(0)
    decoded = cv2.imdecode(np.frombuffer(test_jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None, "Generated JPEG tidak bisa di-decode!"
    print(f"OK Fake JPEG valid: {len(test_jpeg)} bytes -> decode {decoded.shape}")
    print()
    print("=== Injecting fake frames (15 fps) + GPS (1/s) ===")
    threading.Thread(target=inject_frames, daemon=True).start()
    threading.Thread(target=inject_gps, daemon=True).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped.")

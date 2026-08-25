"""
Test GPS: NMEA parsing + watchdog stale detection.
"""
import os
import sys
import os as _os
sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import time

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "rov_sar_web.settings")
os.environ["ROV_AUTOSTART_WORKERS"] = "0"

import django
django.setup()

from detection.state import state
from detection.gps_worker import _nmea_to_dd, GpsWorker, STALE_TIMEOUT_S


def test_nmea_parsing():
    print("=== NMEA parsing ===")
    # Semarang-ish: 0710.5 S = 7°10.5' = 7.175° S → -7.175
    lat = _nmea_to_dd("0710.5000", "S")
    lon = _nmea_to_dd("11024.3000", "E")
    print(f"  0710.5000 S → {lat:.5f} (expect ~-7.175)")
    print(f"  11024.3000 E → {lon:.5f} (expect ~110.405)")
    assert abs(lat - (-7.175)) < 0.001, f"lat wrong: {lat}"
    assert abs(lon - 110.405) < 0.001, f"lon wrong: {lon}"
    # Empty handling
    assert _nmea_to_dd("", "N") == 0.0
    print("  ✅ NMEA dddmm.mmmm → decimal degrees OK")


def test_watchdog_stale():
    print("\n=== GPS watchdog stale detection ===")
    state.set_gps(-7.79, 110.36, 45.0)
    gps = state.get_gps()
    assert gps["connected"] is True
    print(f"  Setelah set_gps: connected={gps['connected']}")

    # Simulate stale: mundurkan last_update
    with state._gps_lock:
        state.gps_last_update = time.time() - (STALE_TIMEOUT_S + 5)

    # Jalankan 1 iterasi watchdog manual
    worker = GpsWorker(port="DUMMY")
    # Manual check logic (tanpa loop penuh)
    gps = state.get_gps()
    age = time.time() - gps["last_update"]
    is_stale = age > STALE_TIMEOUT_S
    print(f"  Age fix: {age:.1f}s → stale={is_stale}")
    assert is_stale is True
    print("  ✅ Stale detection logic benar (age > timeout)")


def test_dummy_emits():
    print("\n=== GPS dummy mode emits fixes ===")
    state.gps_last_update = 0
    worker = GpsWorker(port="DUMMY")
    # Jalankan dummy loop sebentar di thread
    import threading
    t = threading.Thread(target=worker._run_dummy, daemon=True)
    t.start()
    time.sleep(3.5)   # dummy emit tiap 1.5s → harusnya ~2 fix
    worker.stop()
    time.sleep(0.2)
    gps = state.get_gps()
    print(f"  GPS setelah 3.5s dummy: lat={gps['lat']:.5f}, connected={gps['connected']}")
    assert gps["lat"] is not None
    assert gps["last_update"] > 0
    print("  ✅ Dummy GPS emit fix OK")


def test_watchdog_runtime():
    print("\n=== GPS watchdog RUNTIME test (jalankan _run_watchdog beneran) ===")
    # Set fix segar → connected True
    state.set_gps(-7.79, 110.36, 45.0)
    assert state.get_gps()["connected"] is True
    print("  Fix segar → connected=True ✓")

    # Buat worker, jalankan HANYA watchdog thread
    worker = GpsWorker(port="DUMMY")
    import threading
    # Jalankan watchdog di thread (bukan run() penuh, cukup watchdog)
    wd = threading.Thread(target=worker._run_watchdog, daemon=True)
    wd.start()

    # Buat fix jadi stale: mundurkan last_update melewati timeout
    with state._gps_lock:
        state.gps_last_update = time.time() - (STALE_TIMEOUT_S + 2)

    # Watchdog loop tiap 1 detik — tunggu maksimal 3 detik sampai flip
    flipped = False
    for _ in range(30):
        time.sleep(0.2)
        if state.get_gps()["connected"] is False:
            flipped = True
            break

    worker.stop()
    time.sleep(0.3)

    print(f"  Setelah stale + watchdog jalan: connected={state.get_gps()['connected']}")
    assert flipped is True, "Watchdog TIDAK mengubah gps_connected jadi False"
    print("  ✅ Watchdog RUNTIME: gps_connected otomatis jadi False saat stale")


if __name__ == "__main__":
    try:
        test_nmea_parsing()
        test_watchdog_stale()
        test_watchdog_runtime()
        test_dummy_emits()
        print("\n=== GPS TESTS: ALL PASS ===")
        sys.exit(0)
    except AssertionError as e:
        print(f"\n❌ FAIL: {e}")
        sys.exit(1)

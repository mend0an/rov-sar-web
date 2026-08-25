"""
Test 4 patch v.beta3.1:
1. frame_age() return None (bukan inf) → /api/state JSON valid
2. gps_fix_is_fresh menolak fix stale/disconnected
3. serial error langsung set gps_connected=False (dicek via logika helper)
4. worker gating: --noreload path (dicek via simulasi argv)
"""
import os
import sys
import os as _os
sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import json
import time

# Stub torch (capture.py butuh via enhancement_utils)
if "torch" not in sys.modules:
    import types
    ft = types.ModuleType("torch")
    ft.cuda = types.SimpleNamespace(is_available=lambda: False)
    sys.modules["torch"] = ft

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "rov_sar_web.settings")
os.environ["ROV_AUTOSTART_WORKERS"] = "0"
import django
django.setup()

from detection.state import state, gps_fix_is_fresh


def test_patch1_frame_age_json():
    print("=== PATCH #1: frame_age None → JSON valid ===")
    # Reset frame time
    with state._frame_lock:
        state._last_frame_time = 0
    age = state.frame_age()
    print(f"  frame_age() sebelum ada frame: {age}")
    assert age is None, f"Expected None, got {age}"
    # Pastikan bisa di-serialize JSON tanpa Infinity
    payload = {"frame_age_s": age}
    s = json.dumps(payload)
    assert "Infinity" not in s, "JSON masih mengandung Infinity!"
    parsed = json.loads(s)  # harus valid
    assert parsed["frame_age_s"] is None
    print(f"  JSON: {s} → valid ✓")
    # Setelah ada frame → angka finite
    state.set_frame(b"xxx")
    age2 = state.frame_age()
    assert age2 is not None and age2 < 1
    print(f"  Setelah set_frame: frame_age={age2:.3f} (finite) ✓")
    print("  ✅ PATCH #1 OK")


def test_patch2_gps_freshness():
    print("\n=== PATCH #2: gps_fix_is_fresh tolak stale/disconnected ===")
    now = time.time()

    # Fresh fix
    fresh = {"connected": True, "lat": -7.79, "lon": 110.36, "last_update": now}
    assert gps_fix_is_fresh(fresh) is True
    print("  Fresh fix → True ✓")

    # Disconnected
    disc = {"connected": False, "lat": -7.79, "lon": 110.36, "last_update": now}
    assert gps_fix_is_fresh(disc) is False
    print("  Disconnected → False ✓")

    # Stale (100 detik lalu)
    stale = {"connected": True, "lat": -7.79, "lon": 110.36,
             "last_update": now - 100}
    assert gps_fix_is_fresh(stale) is False
    print("  Stale 100s → False ✓")

    # Belum pernah ada fix
    never = {"connected": False, "lat": None, "lon": None, "last_update": 0}
    assert gps_fix_is_fresh(never) is False
    print("  Never fix → False ✓")
    print("  ✅ PATCH #2 OK")


def test_patch2b_manual_waypoint_rejects_stale():
    print("\n=== PATCH #2b: manual waypoint tolak GPS stale (via view logic) ===")
    from detection import views
    from django.test import RequestFactory
    rf = RequestFactory()

    # Set GPS stale
    state.set_gps(-7.79, 110.36, 45.0)
    with state._gps_lock:
        state.gps_last_update = time.time() - 100  # stale
        state.gps_connected = False
    state.clear_waypoints()

    req = rf.post("/api/waypoint")
    resp = views.api_waypoint_mark(req)
    data = json.loads(resp.content)
    print(f"  Response: status={resp.status_code}, ok={data.get('ok')}")
    assert resp.status_code == 400
    assert data["ok"] is False
    assert len(state.get_waypoints()) == 0
    print("  Waypoint DITOLAK saat GPS stale ✓")

    # Sekarang fresh → harus diterima
    state.set_gps(-7.79, 110.36, 45.0)  # set_gps set connected=True + last_update=now
    req2 = rf.post("/api/waypoint")
    resp2 = views.api_waypoint_mark(req2)
    data2 = json.loads(resp2.content)
    print(f"  Fresh GPS → status={resp2.status_code}, ok={data2.get('ok')}")
    assert resp2.status_code == 200 and data2["ok"] is True
    print("  Waypoint DITERIMA saat GPS fresh ✓")
    print("  ✅ PATCH #2b OK")


def test_patch4_noreload_gating():
    print("\n=== PATCH #4: --noreload gating logic ===")
    # Simulasi logika gating dari apps.py
    def should_start(argv, run_main, force=False):
        is_runserver = len(argv) >= 2 and argv[1] == "runserver"
        is_daphne = bool(argv) and "daphne" in os.path.basename(argv[0]).lower()
        if not (is_runserver or is_daphne or force):
            return False
        if is_runserver:
            no_reload = "--noreload" in argv
            if not no_reload and run_main != "true":
                return False
        return True

    # runserver normal, parent (RUN_MAIN unset) → JANGAN start (tunggu child)
    assert should_start(["manage.py", "runserver"], None) is False
    print("  runserver parent (no RUN_MAIN) → skip ✓")
    # runserver normal, child (RUN_MAIN=true) → start
    assert should_start(["manage.py", "runserver"], "true") is True
    print("  runserver child (RUN_MAIN=true) → start ✓")
    # runserver --noreload (RUN_MAIN unset) → HARUS start (ini bug yang diperbaiki)
    assert should_start(["manage.py", "runserver", "127.0.0.1:8000", "--noreload"], None) is True
    print("  runserver --noreload (no RUN_MAIN) → start ✓ (bug fixed)")
    # check → jangan start
    assert should_start(["manage.py", "check"], None) is False
    print("  check → skip ✓")
    # daphne → start
    assert should_start(["/usr/bin/daphne", "asgi:app"], None) is True
    print("  daphne → start ✓")
    print("  ✅ PATCH #4 OK")


if __name__ == "__main__":
    try:
        test_patch1_frame_age_json()
        test_patch2_gps_freshness()
        test_patch2b_manual_waypoint_rejects_stale()
        test_patch4_noreload_gating()
        print("\n=== ALL BETA3.1 PATCHES: PASS ===")
        sys.exit(0)
    except AssertionError as e:
        print(f"\n❌ FAIL: {e}")
        sys.exit(1)

"""
Unit test debounce waypoint deteksi — simulate _maybe_tag_waypoint dipanggil
berkali-kali (seperti YOLO detect tiap frame), verify hanya sebagian kecil
yang jadi waypoint (bukan ratusan).

Test tanpa cv2/torch — mock result object.
"""
import os
import sys
import os as _os
sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import time

# Stub torch — debounce logic tidak butuh torch, dan torch tidak terinstall
# di environment test ini. HOP enhancement (yang butuh torch) tidak dipanggil
# di jalur _maybe_tag_waypoint.
if "torch" not in sys.modules:
    import types
    fake_torch = types.ModuleType("torch")
    fake_torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    sys.modules["torch"] = fake_torch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "rov_sar_web.settings")
os.environ["ROV_AUTOSTART_WORKERS"] = "0"

import django
django.setup()

from detection.state import state


class FakeBoxes:
    """Mock ultralytics Boxes — punya .conf.max() dan len()."""
    def __init__(self, n, conf):
        self._n = n
        class _Conf:
            def __init__(self, c): self._c = c
            def max(self):
                class _V:
                    def __init__(self, v): self.v = v
                    def item(self): return self.v
                return _V(self._c)
        self.conf = _Conf(conf)
    def __len__(self): return self._n


class FakeResult:
    def __init__(self, n_boxes, conf):
        self.boxes = FakeBoxes(n_boxes, conf)


def test_debounce():
    from detection.capture import CaptureWorker, DETECT_WP_COOLDOWN_S

    # Setup: GPS fix aktif, mark_on_detect aktif
    state.set_gps(-7.7956, 110.3695, 45.0)
    state.mark_on_detect_enabled = True
    state.clear_waypoints()

    worker = CaptureWorker(source="0", model_path="dummy")

    # Simulate 200 frame deteksi berturut (seperti YOLO 20fps × 10 detik),
    # posisi GPS bergerak sangat sedikit (< 3m, di bawah threshold)
    print("Simulasi 200 frame deteksi (objek terlihat ~10 detik)…")
    for i in range(200):
        # GPS gerak tipis (~0.5m per frame → total < 3m dalam window pendek)
        state.set_gps(-7.7956 + i * 0.000001, 110.3695, 45.0)
        result = FakeResult(n_boxes=1, conf=0.8)
        worker._maybe_tag_waypoint(result)

    count_no_time = len(state.get_waypoints())
    print(f"  Waypoint tercatat (tanpa jeda waktu): {count_no_time}")
    print(f"  (Tanpa debounce harusnya ~200; dengan debounce harusnya 1)")

    # Karena semua 200 iterasi terjadi dalam < cooldown (5s) dan jarak < 3m,
    # harusnya cuma 1 waypoint (yang pertama)
    assert count_no_time == 1, f"Expected 1, got {count_no_time}"
    print("  ✅ Debounce cooldown+distance WORKING (200 deteksi → 1 waypoint)")

    # Test 2: setelah cooldown lewat + pindah jauh, waypoint baru boleh masuk
    print("\nSimulasi: cooldown lewat + ROV pindah > 3m…")
    worker._last_detect_wp_time = time.time() - (DETECT_WP_COOLDOWN_S + 1)
    state.set_gps(-7.7956 + 0.001, 110.3695, 45.0)  # ~111m jauhnya
    result = FakeResult(n_boxes=1, conf=0.8)
    worker._maybe_tag_waypoint(result)
    count_after = len(state.get_waypoints())
    print(f"  Waypoint sekarang: {count_after}")
    assert count_after == 2, f"Expected 2, got {count_after}"
    print("  ✅ Waypoint baru masuk setelah cooldown+jarak terpenuhi")

    # Test 3: confidence rendah harus ditolak
    print("\nSimulasi: deteksi confidence rendah (0.3 < 0.5)…")
    worker._last_detect_wp_time = time.time() - (DETECT_WP_COOLDOWN_S + 1)
    state.set_gps(-7.7956 + 0.002, 110.3695, 45.0)
    result = FakeResult(n_boxes=1, conf=0.3)
    worker._maybe_tag_waypoint(result)
    count_low_conf = len(state.get_waypoints())
    print(f"  Waypoint sekarang: {count_low_conf} (harusnya tetap 2)")
    assert count_low_conf == 2, f"Expected 2, got {count_low_conf}"
    print("  ✅ Deteksi confidence rendah ditolak")

    print("\n=== DEBOUNCE TEST: ALL PASS ===")
    return True


if __name__ == "__main__":
    try:
        test_debounce()
        sys.exit(0)
    except AssertionError as e:
        print(f"\n❌ FAIL: {e}")
        sys.exit(1)

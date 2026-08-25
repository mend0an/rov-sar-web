"""
Uji fitur v.beta6: ganti sumber runtime, pacing fps, statistik device.

Yang dibuktikan:
  1. Ganti sumber benar-benar mengganti video yang keluar — dibuktikan dengan
     membaca WARNA frame, bukan sekadar percaya nilai `worker.source`.
  2. Sumber yang gagal dibuka TIDAK membunuh stream: worker kembali ke sumber
     lama. Ini penting karena operator bisa salah pilih di lapangan.
  3. Pacing `process_fps` benar-benar membatasi laju.
  4. Model dimuat SEBELUM capture — kalau kamera gagal, YOLO tetap masuk GPU.
     Ini bug yang sebenarnya membuat GPU terlihat menganggur.
  5. Endpoint `/api/source` menolak jenis sumber yang tidak diizinkan.

Jalankan: python3 tests/test_source_switch.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "rov_sar_web.settings")
os.environ.setdefault("ROV_AUTOSTART_WORKERS", "0")

import django  # noqa: E402
django.setup()

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from django.test import Client  # noqa: E402
from detection.capture import CaptureWorker  # noqa: E402
from detection.state import state  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))


def make_video(path, bgr, n=60):
    """Video polos berwarna tertentu — warnanya jadi sidik jari sumber."""
    vw = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 20.0, (160, 120))
    for _ in range(n):
        img = np.zeros((120, 160, 3), dtype=np.uint8)
        img[:, :] = bgr
        vw.write(img)
    vw.release()
    return os.path.exists(path)


def dominant_bgr():
    """Warna rata-rata frame terakhir yang keluar dari state."""
    jpeg, _ = state.get_frame_with_id()
    if not jpeg:
        return None
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    return tuple(int(x) for x in img.reshape(-1, 3).mean(axis=0))


def near(a, b, tol=30):
    return a is not None and all(abs(x - y) < tol for x, y in zip(a, b))


def main():
    BLUE = (200, 30, 30)      # BGR
    RED = (30, 30, 200)
    v1, v2 = "/tmp/src_biru.mp4", "/tmp/src_merah.mp4"
    make_video(v1, BLUE)
    make_video(v2, RED)

    state.update_control(hop_enabled=False, clahe_enabled=False,
                         dehaze_enabled=False, wb_enabled=False,
                         yolo_enabled=False)

    print("── Model dimuat sebelum capture ──")
    # Sumber sengaja tidak ada. Sebelum perbaikan, _init_model() dipanggil
    # SETELAH capture berhasil, jadi model tidak pernah dimuat sama sekali
    # dan GPU terlihat menganggur — persis gejala yang membingungkan itu.
    w0 = CaptureWorker(source="/tmp/tidak_ada_sama_sekali.mp4",
                       model_path="tidak_ada.pt", process_fps=10)
    w0.start()
    time.sleep(2.5)
    st0 = w0.get_stats()
    w0.stop()
    w0.join(timeout=5)
    check("Model dicoba dimuat walau sumber gagal",
          st0["model_error"] is not None,
          "model_error terisi → _init_model dipanggil")
    check("Kegagalan sumber tercatat di stats",
          st0["source_error"] is not None)

    print("\n── Ganti sumber saat runtime ──")
    state._latest_jpeg = None
    w = CaptureWorker(source=v1, model_path="tidak_ada.pt", process_fps=20)
    w.start()
    time.sleep(2.5)

    c1 = dominant_bgr()
    check("Sumber awal keluar (biru)", near(c1, BLUE), f"BGR={c1}")

    w.request_source(("file", v2))
    time.sleep(2.5)
    c2 = dominant_bgr()
    check("Setelah ganti, video BERUBAH (merah)", near(c2, RED), f"BGR={c2}")
    check("Worker masih hidup (tidak di-restart)", w.is_alive())
    check("stats.source ikut berubah",
          w.get_stats()["source"][1] == v2, w.get_stats()["source"][1])

    print("\n── Sumber gagal → fallback ke sumber lama ──")
    w.request_source(("file", "/tmp/jelas_tidak_ada.mp4"))
    time.sleep(3.0)
    c3 = dominant_bgr()
    check("Video TIDAK mati saat salah pilih", near(c3, RED), f"BGR={c3}")
    check("Kembali ke sumber sebelumnya",
          w.get_stats()["source"][1] == v2, w.get_stats()["source"][1])

    print("\n── Pacing fps ──")
    fps = w.get_stats()["process_fps"]
    check("Laju proses dibatasi ~20 fps", 5 <= fps <= 26, f"{fps} fps")

    w.stop()
    w.join(timeout=5)

    print("\n── Endpoint /api/source ──")
    state.capture_worker = w
    c = Client()

    def post(payload):
        return c.post("/api/source", data=json.dumps(payload),
                      content_type="application/json")

    r = post({"spec": ["file", "/etc/passwd"]})
    check("Sumber file ditolak lewat API", r.status_code == 400,
          f"HTTP {r.status_code}")

    r = post({"spec": ["dshow", "bukan angka"]})
    check("Index kamera non-integer ditolak", r.status_code == 400)

    r = post({"spec": ["rtsp", "http://jahat/x"]})
    check("URL non-RTSP ditolak", r.status_code == 400)

    r = post({"spec": "salah bentuk"})
    check("Spec bentuk salah ditolak", r.status_code == 400)

    r = post({"spec": ["dshow", 1]})
    check("Webcam index diterima", r.status_code == 200, f"HTTP {r.status_code}")

    r = post({"spec": ["rtsp", "rtsp://192.168.8.9:8554/stream"]})
    check("RTSP diterima", r.status_code == 200)

    state.capture_worker = None
    r = post({"spec": ["dshow", 0]})
    check("Ditolak saat worker tidak ada", r.status_code == 409)

    print("\n── /api/sources ──")
    state.capture_worker = w
    r = c.get("/api/sources")
    j = r.json()
    check("Ada penanda active", "active" in j)
    check("Entri tanpa spec ditandai tidak selectable",
          all(x["selectable"] == (x["spec"] is not None) for x in j["sources"]))
    state.capture_worker = None

    print("\n── /api/state memuat blok capture ──")
    r = c.get("/api/state")
    s = r.json()
    check("Blok capture ada (null saat worker mati)", "capture" in s)

    test_cache()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'='*52}\n{passed}/{len(results)} PASS")
    return 0 if passed == len(results) else 1


# ─── Tambahan: cache enumerasi kamera ────────────────────────────────────────
#
# Bug lapangan v.beta6: dropdown menggantung di "memuat…" selamanya.
# Penyebabnya `/api/sources` memanggil enumerasi DirectShow di setiap request.
# COM butuh inisialisasi per-thread dan enumerasinya bisa makan beberapa detik
# — cukup untuk membuat request tidak pernah kembali.
def test_cache():
    import threading
    from detection import rov_camera

    print("\n── Cache enumerasi kamera ──")
    rov_camera._sources_cache = None
    a = rov_camera.list_sources()
    check("Panggilan pertama mengisi cache",
          rov_camera._sources_cache is not None)
    check("Panggilan kedua hasilnya sama", rov_camera.list_sources() == a)
    check("refresh=True tetap bekerja",
          len(rov_camera.list_sources(refresh=True)) == len(a))

    # Yang sebenarnya jadi masalah: dipanggil dari thread pool Django.
    res = {}
    t = threading.Thread(target=lambda: res.update(r=rov_camera.list_sources()))
    t.start()
    t.join(timeout=5)
    check("Tidak menggantung saat dipanggil dari thread lain",
          not t.is_alive() and res.get("r") is not None)

    c = Client()
    r = c.get("/api/sources?refresh=1")
    check("Endpoint menerima ?refresh=1", r.status_code == 200)


if __name__ == "__main__":
    sys.exit(main())

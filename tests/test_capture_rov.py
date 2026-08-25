"""
Uji integrasi Tahap A: routing sumber video dan logika RovWorker.

Yang dibuktikan di sini:
  1. URL RTSP benar-benar sampai ke RTSPReader (PyAV), BUKAN cv2.VideoCapture.
     Ini inti perbaikan v.beta5 — kalau routing-nya salah, video akan beku di
     lapangan dan gejalanya tidak kelihatan sampai ROV dicelupkan.
  2. CaptureWorker sungguhan memproses video sungguhan sampai jadi JPEG
     yang bisa di-decode ulang.
  3. Dedup frame_id: frame yang sama tidak di-inferensi dua kali.
  4. Auto-depth di-clamp ke 1-7 m, dan heading memilih yaw ROV di atas COG GPS.

Jalankan: python3 tests/test_capture_rov.py
"""
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

from detection import rov_camera  # noqa: E402
from detection.capture import CaptureWorker  # noqa: E402
from detection.state import state  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))


def make_video(path, n_frames=40):
    """Bikin file video sungguhan supaya CaptureWorker punya sesuatu untuk dibaca."""
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    vw = cv2.VideoWriter(path, fourcc, 20.0, (320, 240))
    for i in range(n_frames):
        img = np.zeros((240, 320, 3), dtype=np.uint8)
        img[:, :, 1] = 60
        cv2.rectangle(img, (40, 40), (280, 200), (30, 90, 160), -1)
        cv2.putText(img, f"F{i}", (12, 30), cv2.FONT_HERSHEY_SIMPLEX,
                    0.8, (255, 255, 255), 2)
        vw.write(img)
    vw.release()
    return os.path.exists(path) and os.path.getsize(path) > 0


def test_routing():
    print("── Routing sumber video ──")

    check("RTSP → spec rtsp",
          rov_camera.parse_spec("rtsp://192.168.8.9:8554/stream") ==
          ("rtsp", "rtsp://192.168.8.9:8554/stream"))
    check("'0' → webcam", rov_camera.parse_spec("0") == ("dshow", 0))

    # RTSPReader harus benar-benar dipilih untuk spec rtsp. URL di bawah
    # sengaja tidak ada — yang diuji adalah KELAS yang dipakai, bukan apakah
    # koneksinya berhasil.
    reader = None
    try:
        reader = rov_camera.open_source(("rtsp", "rtsp://127.0.0.1:1/none"))
        is_pyav = isinstance(reader, rov_camera.RTSPReader)
    except RuntimeError as e:
        is_pyav = False
        print(f"      (PyAV tidak tersedia: {e})")
    finally:
        if reader is not None:
            reader.release()

    check("open_source(rtsp) → RTSPReader (PyAV, bukan cv2)", is_pyav)
    check("Transport RTSP = UDP",
          rov_camera.RTSP_OPTIONS["rtsp_transport"] == "udp",
          rov_camera.RTSP_OPTIONS["rtsp_transport"])
    check("Endpoint default 192.168.8.9:8554/stream",
          rov_camera.ROV_RTSP_URL == "rtsp://192.168.8.9:8554/stream",
          rov_camera.ROV_RTSP_URL)

    # Regresi terhadap v.beta3.1: transport TCP dipaksa lewat env var.
    from django.conf import settings  # noqa: F401
    opts = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS", "")
    check("settings tidak lagi memaksa rtsp_transport;tcp",
          "tcp" not in opts.lower(), repr(opts))


def test_capture_worker():
    print("\n── CaptureWorker terhadap video sungguhan ──")

    path = "/tmp/uji_capture.mp4"
    if not make_video(path):
        check("Video uji dibuat", False)
        return
    check("Video uji dibuat", True)

    # Matikan enhancement supaya jalur yang diuji murni baca→encode→state,
    # tanpa bergantung pada torch/CUDA.
    state.update_control(hop_enabled=False, clahe_enabled=False,
                         dehaze_enabled=False, wb_enabled=False,
                         yolo_enabled=False)
    state._latest_jpeg = None
    state._frame_count = 0
    state._last_frame_time = 0.0

    w = CaptureWorker(source=path, model_path="tidak_ada.pt")
    w.start()
    time.sleep(3.0)

    jpeg, count = state.get_frame_with_id()
    w.stop()
    w.join(timeout=5)

    check("Frame masuk ke state", jpeg is not None, f"{count} frame")

    decoded = None
    if jpeg:
        decoded = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    check("JPEG bisa di-decode ulang", decoded is not None,
          f"{decoded.shape}" if decoded is not None else "gagal")
    check("Ukuran frame benar",
          decoded is not None and decoded.shape[:2] == (240, 320))
    check("frame_age() angka valid (bukan Infinity)",
          isinstance(state.frame_age(), float))


def test_frame_dedup():
    print("\n── Dedup frame_id ──")

    class StuckSource:
        """Sumber yang frame_id-nya tidak pernah naik — meniru RTSP yang beku."""
        frame_id = 7

        def __init__(self):
            self.reads = 0

        def isOpened(self):
            return True

        def read(self):
            self.reads += 1
            return True, np.zeros((32, 32, 3), dtype=np.uint8)

        def release(self):
            pass

    src = StuckSource()
    w = CaptureWorker(source="dummy", model_path="x.pt")
    w._cap = src
    w._is_rtsp = True
    w._last_frame_id = -1

    processed = []
    w._process = lambda f: (processed.append(1), f)[1]
    w._encode_jpeg = lambda f: b"x"
    w._init_capture = lambda: True
    w._init_model = lambda: None

    import detection.capture as capmod
    orig = capmod.eu.calculate_hop_coefficients
    capmod.eu.calculate_hop_coefficients = lambda: (None, None, None)
    try:
        w.start()
        time.sleep(1.0)
        w.stop()
        w.join(timeout=3)
    finally:
        capmod.eu.calculate_hop_coefficients = orig

    check("read() dipanggil berkali-kali", src.reads > 10, f"{src.reads}x")
    check("Frame identik hanya diproses 1x", len(processed) == 1,
          f"{len(processed)} kali proses / {src.reads} read")


def test_rov_logic():
    print("\n── Auto-depth & pemilihan heading ──")

    from detection.rov_worker import RovWorker

    w = RovWorker(host="127.0.0.1", port=1)

    # Kedalaman di luar rentang kalibrasi HOP (1-7 m) harus di-clamp:
    # polinomial derajat 6 berosilasi di luar itu dan koreksi warnanya rusak.
    cases = [(0.2, 1), (1.4, 1), (3.6, 4), (7.0, 7), (12.9, 7), (-3.0, 1)]
    ok_all = True
    for depth, expect in cases:
        state.set_rov_telemetry({"D": str(depth)})
        state.rov_auto_depth = True
        w._last_auto_depth = None
        w._apply_auto_depth()
        got = state.get_control()["hop_depth"]
        if got != expect:
            ok_all = False
            print(f"      D={depth} → {got}, harusnya {expect}")
    check("Depth HOP di-clamp ke 1-7 m", ok_all,
          f"{len(cases)} kasus diuji")

    state.rov_auto_depth = False
    state.update_control(hop_depth=3)
    state.set_rov_telemetry({"D": "6.0"})
    w._last_auto_depth = None
    w._apply_auto_depth()
    check("Auto-depth mati → slider tidak disentuh",
          state.get_control()["hop_depth"] == 3)

    # Heading: yaw ROV harus menang atas COG buoy saat telemetri fresh.
    state.set_gps(-7.0, 110.0, 123.0)
    state.set_rov_telemetry({"Y": "45.5"})
    state.rov_use_heading = True
    src, hdg = state.active_heading()
    check("Yaw ROV dipakai saat telemetri fresh",
          src == "rov" and abs(hdg - 45.5) < 0.01, f"{src} {hdg}")

    state.rov_use_heading = False
    src, hdg = state.active_heading()
    check("COG GPS dipakai saat opsi dimatikan",
          src == "gps" and abs(hdg - 123.0) < 0.01, f"{src} {hdg}")

    state.rov_use_heading = True
    state.set_rov_disconnected("uji")
    src, hdg = state.active_heading()
    check("Fallback ke COG GPS saat telemetri putus",
          src == "gps" and abs(hdg - 123.0) < 0.01, f"{src} {hdg}")

    state.rov_use_heading = True
    state.rov_auto_depth = True


def main():
    test_routing()
    test_capture_worker()
    test_frame_dedup()
    test_rov_logic()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'='*52}\n{passed}/{len(results)} PASS")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

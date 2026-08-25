"""
Uji bahwa kombinasi environment variable dari tiap menu jalan.bat benar-benar
menghasilkan konfigurasi Django yang diharapkan.

Kenapa ini perlu: batch file tidak bisa dijalankan di CI, dan kesalahan paling
umum di sana bukan sintaks melainkan **env var sisa dari menu sebelumnya**.
Misal habis menjalankan menu [4] (ROV) lalu pindah ke menu [2] (video):
kalau ROV_TELEMETRY_ENABLED tidak dibersihkan, aplikasi akan tetap mencoba
menyambung ke ROV yang tidak ada dan membanjiri log. Subroutine :BERSIH di
jalan.bat ada untuk itu, dan test ini memverifikasi daftar variabelnya lengkap.

Jalankan: python3 tests/test_batch_env.py
"""
import importlib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))


def load_settings(env):
    """Muat ulang settings.py dengan environment tertentu."""
    saved = dict(os.environ)
    for k in list(os.environ):
        if k.startswith("ROV_"):
            del os.environ[k]
    os.environ.update(env)
    try:
        import rov_sar_web.settings as s
        importlib.reload(s)
        return {
            "rtsp": s.ROV_RTSP_URL,
            "model": s.ROV_MODEL_PATH,
            "gps": s.ROV_GPS_PORT,
            "tele": s.ROV_TELEMETRY_ENABLED,
            "host": s.ROV_HOST,
            "token": s.ROV_CONTROL_TOKEN,
            "fps": s.ROV_PROCESS_FPS,
            "log": s.LOGGING["loggers"]["detection"]["level"],
        }
    finally:
        os.environ.clear()
        os.environ.update(saved)


def batch_vars(label):
    """Ambil daftar `set VAR=` dalam satu blok label di jalan.bat."""
    txt = open(os.path.join(BASE, "jalan.bat"), encoding="utf-8").read()
    m = re.search(rf"^:{label}\b(.*?)(?=^REM ─|^:\w)", txt, re.S | re.M)
    if not m:
        return {}
    out = {}
    for line in m.group(1).split("\n"):
        mm = re.match(r"\s*set (ROV_\w+)=(.*)", line)
        if mm:
            out[mm.group(1)] = mm.group(2).strip()
    return out


def main():
    print("── Menu [1] FAKE ──")
    v = batch_vars("FAKE")
    check("Set ROV_FAKE_WORKERS=1", v.get("ROV_FAKE_WORKERS") == "1", str(v))

    print("\n── Menu [2] VIDEO ──")
    v = batch_vars("VIDEO")
    check("Set sumber, model, GPS, fps",
          {"ROV_RTSP_URL", "ROV_MODEL_PATH", "ROV_GPS_PORT",
           "ROV_PROCESS_FPS"} <= set(v))
    check("TIDAK menyalakan telemetri ROV",
          "ROV_TELEMETRY_ENABLED" not in v)

    print("\n── Menu [4] ROV ──")
    v = batch_vars("ROV")
    check("Menyalakan telemetri", v.get("ROV_TELEMETRY_ENABLED") == "1")
    check("Set host & token",
          "ROV_HOST" in v and "ROV_CONTROL_TOKEN" in v)

    print("\n── Subroutine :BERSIH ──")
    bersih = set(batch_vars("BERSIH"))
    # Semua var yang PERNAH di-set menu mana pun harus ikut dibersihkan,
    # supaya berpindah menu tidak mewarisi konfigurasi lama.
    dipakai = set()
    for lbl in ("FAKE", "VIDEO", "KAMERA", "ROV"):
        dipakai |= set(batch_vars(lbl))
    kurang = sorted(dipakai - bersih)
    check("Semua var menu ikut dibersihkan", not kurang,
          f"kurang: {kurang}" if kurang else f"{len(bersih)} var")

    print("\n── settings.py membaca env dengan benar ──")
    s = load_settings({
        "ROV_RTSP_URL": "D:\\video\\uji.mp4",
        "ROV_MODEL_PATH": "C:\\model\\best.pt",
        "ROV_GPS_PORT": "COM7",
        "ROV_PROCESS_FPS": "15",
    })
    check("Sumber video terbaca", s["rtsp"] == "D:\\video\\uji.mp4")
    check("Model terbaca", s["model"] == "C:\\model\\best.pt")
    check("GPS terbaca", s["gps"] == "COM7")
    check("fps cap terbaca", s["fps"] == 15.0, str(s["fps"]))
    check("Telemetri ROV default MATI", s["tele"] is False)

    s = load_settings({
        "ROV_RTSP_URL": "rtsp://192.168.8.9:8554/stream",
        "ROV_TELEMETRY_ENABLED": "1",
        "ROV_HOST": "192.168.8.9",
        "ROV_CONTROL_TOKEN": "rahasia",
    })
    check("Telemetri menyala saat diminta", s["tele"] is True)
    check("Token terbaca", s["token"] == "rahasia")

    s = load_settings({})
    check("Default fps = 30", s["fps"] == 30.0, str(s["fps"]))
    check("Default log level INFO", s["log"] == "INFO", s["log"])
    check("Default RTSP aman (webcam, bukan URL salah)",
          s["rtsp"] == "0", s["rtsp"])

    print("\n── config.bat ──")
    cfg = open(os.path.join(BASE, "config.bat"), encoding="utf-8").read()
    perlu = ["CONDA_ENV", "MODEL_PATH", "VIDEO_UJI", "KAMERA_INDEX",
             "GPS_PORT", "ROV_RTSP", "ROV_IP", "ROV_TOKEN", "FPS_CAP", "PORT"]
    hilang = [k for k in perlu if f"set {k}=" not in cfg]
    check("Semua kunci konfigurasi ada", not hilang, f"hilang: {hilang}")
    check("Endpoint RTSP benar (bukan 192.168.8.8:554/live)",
          "192.168.8.9:8554/stream" in cfg)

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'='*52}\n{passed}/{len(results)} PASS")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

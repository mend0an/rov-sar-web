"""
Uji endpoint ROV lewat HTTP sungguhan (Django test client), termasuk gerbang
keamanan yang tidak ada di versi desktop.

Yang paling penting diuji di sini: perintah HARUS ditolak selama kontrol
terkunci, dan penolakan itu terjadi DI SERVER. Kalau gerbangnya hanya di
browser, siapa pun yang bisa mengirim POST bisa melewatinya — dan di jaringan
AP ROV itu berarti siapa pun yang tersambung ke WiFi.

Jalankan: python3 tests/test_rov_api.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "rov_sar_web.settings")
os.environ.setdefault("ROV_AUTOSTART_WORKERS", "0")

import django  # noqa: E402
django.setup()

from django.test import Client  # noqa: E402
from django.conf import settings  # noqa: E402
from detection.state import state  # noqa: E402


class FakeWorker:
    """RovWorker tiruan — mencatat perintah, bisa dibuat 'stale'."""

    def __init__(self, alive=True):
        self.alive = alive
        self.sent = []

    def send(self, key, value):
        if not self.alive:
            return False
        self.sent.append((key, value))
        return True


results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))


def post(c, url, payload, **kw):
    return c.post(url, data=json.dumps(payload),
                  content_type="application/json", **kw)


def main():
    c = Client()
    worker = FakeWorker()
    state.rov_worker = worker
    state.rov_control_unlocked = False

    print("── Gerbang unlock (server-side) ──")
    r = post(c, "/api/rov/command", {"key": "light", "value": 1})
    check("Perintah ditolak saat terkunci", r.status_code == 409,
          f"HTTP {r.status_code}")
    check("Tidak ada perintah sampai ke ROV", worker.sent == [],
          f"sent={worker.sent}")

    r = post(c, "/api/rov/unlock", {"unlocked": True})
    check("Unlock berhasil", r.status_code == 200 and r.json()["unlocked"])

    r = post(c, "/api/rov/command", {"key": "light", "value": 1})
    check("Perintah diterima setelah unlock", r.status_code == 200)
    check("ROV benar-benar menerima light:1", ("light", 1) in worker.sent,
          f"sent={worker.sent}")

    print("\n── Validasi input ──")
    r = post(c, "/api/rov/command", {"key": "thro", "value": 2})
    check("Perintah gerak (thro) ditolak", r.status_code == 400,
          f"HTTP {r.status_code}")

    r = post(c, "/api/rov/command", {"key": "light", "value": 99})
    check("Nilai di luar rentang ditolak", r.status_code == 400)

    r = post(c, "/api/rov/command", {"key": "light", "value": "nyala"})
    check("Nilai non-integer ditolak", r.status_code == 400)

    r = c.post("/api/rov/command", data="bukan json",
               content_type="application/json")
    check("Body rusak ditolak", r.status_code == 400)

    print("\n── ROV tidak tersambung ──")
    worker.alive = False
    r = post(c, "/api/rov/command", {"key": "holdd", "value": 1})
    check("Ditolak saat telemetri stale", r.status_code == 409,
          f"HTTP {r.status_code}")
    worker.alive = True

    state.rov_worker = None
    r = post(c, "/api/rov/command", {"key": "holdd", "value": 1})
    check("Ditolak saat worker tidak ada", r.status_code == 409)
    state.rov_worker = worker

    print("\n── Token ──")
    settings.ROV_CONTROL_TOKEN = "rahasia123"
    try:
        r = post(c, "/api/rov/command", {"key": "light", "value": 0})
        check("Ditolak tanpa token", r.status_code == 403,
              f"HTTP {r.status_code}")

        r = post(c, "/api/rov/command", {"key": "light", "value": 0},
                 HTTP_X_ROV_TOKEN="salah")
        check("Ditolak dengan token salah", r.status_code == 403)

        before = len(worker.sent)
        r = post(c, "/api/rov/command", {"key": "light", "value": 0},
                 HTTP_X_ROV_TOKEN="rahasia123")
        check("Diterima dengan token benar", r.status_code == 200)
        check("Perintah diteruskan", len(worker.sent) == before + 1)

        r = post(c, "/api/rov/unlock", {"unlocked": False})
        check("Unlock juga dilindungi token", r.status_code == 403)
    finally:
        settings.ROV_CONTROL_TOKEN = ""

    print("\n── Preferensi ──")
    r = post(c, "/api/rov/prefs", {"use_heading": False, "auto_depth": False})
    check("Prefs tersimpan", r.status_code == 200
          and state.rov_use_heading is False
          and state.rov_auto_depth is False)
    post(c, "/api/rov/prefs", {"use_heading": True, "auto_depth": True})

    print("\n── Snapshot /api/state ──")
    r = c.get("/api/state")
    s = r.json()
    check("Blok rov ada di state", "rov" in s)
    check("heading & heading_source ada",
          "heading" in s and "heading_source" in s,
          f"source={s.get('heading_source')}")
    check("JSON valid (tanpa Infinity)", "Infinity" not in r.content.decode())
    check("config.rov_label ada", "rov_label" in s["config"])

    print("\n── /api/sources ──")
    r = c.get("/api/sources")
    j = r.json()
    labels = [x["label"] for x in j.get("sources", [])]
    check("Sumber RTSP ROV terdaftar",
          any("Titan" in l for l in labels), f"{labels[:2]}")
    check("Spec RTSP benar (bukan cv2)",
          j["sources"][0]["spec"][0] == "rtsp",
          f"{j['sources'][0]['spec']}")

    state.rov_worker = None
    state.rov_control_unlocked = False

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'='*52}\n{passed}/{len(results)} PASS")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

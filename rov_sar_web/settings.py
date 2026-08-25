"""
Django settings untuk ROV SAR Detection Web.

Custom settings (di paling bawah file ini) bisa di-override via environment
variables — lihat README.md.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# ─── Django basics ─────────────────────────────────────────────────────────
SECRET_KEY = "rov-sar-dev-key-change-in-production"
DEBUG = True
ALLOWED_HOSTS = ["*"]   # LAN-only deployment, no public exposure

INSTALLED_APPS = [
    "daphne",               # ASGI server, harus di atas staticfiles
    "django.contrib.staticfiles",
    "channels",
    "detection",
]

MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "rov_sar_web.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [],
        },
    },
]

WSGI_APPLICATION = "rov_sar_web.wsgi.application"
ASGI_APPLICATION = "rov_sar_web.asgi.application"

# Channels in-memory layer — cukup untuk single-process deployment di laptop
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    },
}

# Static files
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]

# Database — sebenarnya nggak dipakai untuk feature utama, tapi Django butuh ini
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ─── ROV-specific settings ─────────────────────────────────────────────────
# Override via environment variables (lihat README.md)

# Sumber video. Nilai yang dikenali:
#   "rtsp://192.168.8.9:8554/stream"  → RTSPReader (PyAV) — ROV Titan T1
#   "0", "1", …                        → webcam USB (DirectShow di Windows)
#   path file                          → file video (untuk uji offline)
# Endpoint RTSP di bawah TERKONFIRMASI dari PCAP aplikasi vendor:
# OPTIONS/DESCRIBE/PLAY semuanya memakai host 192.168.8.9 port 8554 path
# /stream. Alamat lama 192.168.8.8:554/live SALAH.
ROV_RTSP_URL = os.environ.get(
    "ROV_RTSP_URL",
    "0",   # default aman: webcam. Set ke rtsp://192.168.8.9:8554/stream di lapangan
)

ROV_MODEL_PATH = os.environ.get(
    "ROV_MODEL_PATH",
    "runs/detect/rov_small_yolo11s_720_datatrain_hopv2/weights/best.pt",
)

ROV_GPS_PORT = os.environ.get("ROV_GPS_PORT", "AUTO")
ROV_GPS_BAUD = int(os.environ.get("ROV_GPS_BAUD", "4800"))

# Default state untuk enhancement pipeline
ROV_DEFAULT_HOP_DEPTH = int(os.environ.get("ROV_DEFAULT_HOP_DEPTH", "1"))

# Auto-waypoint settings
ROV_AUTO_WAYPOINT_MIN_DIST_M = float(
    os.environ.get("ROV_AUTO_WAYPOINT_MIN_DIST_M", "5.0")
)

# Jangan auto-start worker saat management command (migrate, makemigrations, dst)
# Cuma start saat runserver — di-handle di detection/apps.py
ROV_AUTOSTART_WORKERS = os.environ.get("ROV_AUTOSTART_WORKERS", "1") == "1"

# ─── Telemetri & kontrol ROV (TCP 6666) ───────────────────────────────────
ROV_HOST = os.environ.get("ROV_HOST", "192.168.8.9")
ROV_PORT = int(os.environ.get("ROV_PORT", "6666"))

# Matikan kalau sedang uji tanpa ROV — tanpa ini RovTelemetry akan mencoba
# menyambung terus-menerus dan membanjiri log dengan connection refused.
ROV_TELEMETRY_ENABLED = os.environ.get("ROV_TELEMETRY_ENABLED", "0") == "1"

# Token untuk endpoint kontrol ROV. Kosong = tanpa token.
#
# Di versi desktop kontrol ROV aman karena hanya bisa disentuh orang yang
# duduk di depan laptop. Dashboard ini melayani seluruh LAN, jadi tanpa token
# siapa pun yang tersambung ke WiFi ROV bisa menyalakan lampu atau mengunci
# kedalaman. Kosongkan hanya untuk uji kolam tertutup.
ROV_CONTROL_TOKEN = os.environ.get("ROV_CONTROL_TOKEN", "")

# CATATAN: OPENCV_FFMPEG_CAPTURE_OPTIONS SENGAJA TIDAK DI-SET.
#
# Versi sebelumnya memaksa "rtsp_transport;tcp" karena OpenCV putus-putus di
# stream lain. Untuk ROV ini justru keliru dua kali: (a) server RTSP-nya tidak
# men-deliver via TCP sama sekali, dan (b) OpenCV/FFmpeg memang tidak bisa
# mendekode stream ini dengan benar apa pun transportnya. Jalur RTSP sekarang
# lewat PyAV di rov_camera.RTSPReader dengan transport UDP + probesize kecil,
# yang sudah teruji lapangan 29.6 fps stabil selama 88 detik. Variabel env di
# atas tidak berpengaruh ke PyAV, dan menyisakannya cuma membingungkan.

# ─── Pacing loop capture ──────────────────────────────────────────────────
# Versi PyQt5 memakai QTimer 30 ms (plafon ~33 fps). Loop di web tanpa pacing
# akan memproses secepat GPU sanggup — yang terdengar bagus, tapi justru
# membuat browser di laptop yang sama tersendat karena berebut CPU, dan
# mengaburkan perilaku debounce waypoint yang berbasis waktu.
# 0 = tanpa batas.
ROV_PROCESS_FPS = float(os.environ.get("ROV_PROCESS_FPS", "30"))

# ─── Logging ──────────────────────────────────────────────────────────────
# Tanpa blok ini, Django hanya mengarahkan logger bernama `django.*` ke
# konsol. Logger `detection.*` jatuh ke root yang tidak punya handler,
# sehingga Python memakai `lastResort` yang HANYA meloloskan WARNING ke atas.
# Akibatnya semua logger.info hilang tanpa jejak — termasuk yang memberi tahu
# sumber video apa yang dibuka, apakah berhasil, dan model berjalan di device
# mana. Aplikasi desktop menampilkan itu di UI; versi web kehilangan jalur itu
# kalau logging tidak dikonfigurasi.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "ringkas": {
            "format": "{asctime} {levelname:7s} {message}",
            "datefmt": "%H:%M:%S",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "ringkas",
        },
    },
    "loggers": {
        "detection": {
            "handlers": ["console"],
            "level": os.environ.get("ROV_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
    },
}

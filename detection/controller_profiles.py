"""
Profil pemetaan controller — penyimpanan per perangkat.

Kenapa disimpan di SERVER, bukan di browser
───────────────────────────────────────────
Godaannya adalah localStorage: sederhana, tanpa endpoint. Tapi profil itu
menggambarkan PERANGKAT KERAS, bukan preferensi orang. Pad yang sama yang
dipindah dari HP operator ke tablet cadangan tetap punya tata letak tombol
yang sama, dan memaksa orang memetakan ulang di tengah operasi karena HP-nya
ganti adalah kegagalan yang bisa dihindari. Disimpan di server, satu kali
petakan berlaku untuk semua klien di LAN.

Kunci profil
────────────
String `id` dari Gamepad API, dinormalkan. Chrome melaporkan sesuatu seperti:

    "Xbox 360 Controller (STANDARD GAMEPAD Vendor: 045e Product: 028e)"

Pasangan vendor/product itu yang stabil; sisanya berubah antar browser dan
sistem operasi. Jadi kalau ada, kunci diambil dari situ — supaya profil yang
dibuat di Chrome HP tetap terpakai saat pad yang sama dicolok ke laptop.

Yang TIDAK divalidasi di sini
─────────────────────────────
Isi pemetaan tidak diperiksa terhadap kapabilitas. Operator boleh saja
memetakan tombol ke aksi yang belum aktif — itu justru yang diinginkan,
supaya tata letak sudah siap sebelum PCAP selesai. Penjagaan bahwa aksi mati
tidak sampai ke soket sudah dilakukan di `rov_caps.py` dan `views.py`, di
tempat yang tidak bisa dilewati. Memvalidasi dua kali di sini hanya akan
membuat pemetaan yang sah ditolak tanpa alasan yang bisa dijelaskan.
"""
import json
import logging
import re
import threading
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()

# Nama aksi yang boleh muncul di sebuah profil. Bukan soal keamanan — soal
# menangkap salah ketik saat profil disunting tangan, sebelum operator
# bingung kenapa satu tombol tidak melakukan apa pun.
VALID_ACTIONS = {
    "thro", "lift", "yaw", "lateral",
    "gear_up", "gear_down",
    "holdd", "holdy",
    "tilt_up", "tilt_down", "posture",
    "light", "photo", "mark", "record",
    "estop", "none",
}


def _store_path() -> Path:
    return Path(settings.BASE_DIR) / "controller_profiles.json"


def device_key(gamepad_id: str) -> str:
    """
    Kunci stabil dari string id Gamepad API.

    Utamakan vendor/product karena itu identitas perangkat kerasnya. Kalau
    tidak ada (beberapa pad BLE tidak melaporkannya), jatuh ke nama yang
    dibersihkan — kurang stabil, tapi tetap jauh lebih baik daripada
    memaksa pemetaan ulang tiap kali browser berganti.
    """
    gid = (gamepad_id or "").strip()
    m = re.search(r"Vendor:\s*([0-9a-fA-F]{4}).*?Product:\s*([0-9a-fA-F]{4})", gid)
    if m:
        return f"vp:{m.group(1).lower()}:{m.group(2).lower()}"
    cleaned = re.sub(r"\(.*?\)", "", gid).strip().lower()
    cleaned = re.sub(r"[^a-z0-9]+", "-", cleaned).strip("-")
    return f"name:{cleaned}" if cleaned else "name:unknown"


def load_all() -> dict:
    path = _store_path()
    if not path.exists():
        return {}
    try:
        with _LOCK:
            return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        # Berkas rusak tidak boleh membuat aplikasi gagal jalan. Pemetaan
        # bawaan masih bisa dipakai; operator kehilangan kustomisasinya,
        # bukan kendalinya.
        logger.warning(f"Profil controller tidak terbaca ({e}) — memakai bawaan")
        return {}


def get(gamepad_id: str) -> dict:
    return load_all().get(device_key(gamepad_id), {})


def save(gamepad_id: str, mapping: dict, label: str = "") -> dict:
    """Simpan profil. Return entri yang tersimpan."""
    key = device_key(gamepad_id)
    clean = {}
    for slot, action in (mapping or {}).items():
        if not isinstance(slot, str) or not isinstance(action, str):
            continue
        if action not in VALID_ACTIONS:
            logger.warning(f"Profil {key}: aksi '{action}' tidak dikenal, dilewati")
            continue
        clean[slot[:32]] = action

    entry = {"label": (label or gamepad_id or "")[:120], "mapping": clean}
    with _LOCK:
        path = _store_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (json.JSONDecodeError, OSError):
            data = {}
        data[key] = entry
        try:
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                            encoding="utf-8")
        except OSError as e:
            logger.error(f"Gagal menyimpan profil controller: {e}")
    logger.info(f"Profil controller '{key}' disimpan — {len(clean)} binding")
    return entry


def delete(gamepad_id: str) -> bool:
    key = device_key(gamepad_id)
    with _LOCK:
        path = _store_path()
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        if key not in data:
            return False
        del data[key]
        try:
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                            encoding="utf-8")
        except OSError:
            return False
    return True

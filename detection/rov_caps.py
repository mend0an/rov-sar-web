"""
Tabel kapabilitas ROV — SUMBER TUNGGAL untuk "perintah apa yang boleh dikirim".

Kenapa file ini ada
───────────────────
Layout tombol aplikasi vendor punya lebih banyak aksi daripada yang benar-benar
kita ketahui protokolnya. `rov_telemetry.py` cuma memverifikasi enam perintah
(thro/lift/yaw/light/holdd/holdy); sisanya — gear kecepatan (field `S`), tilt
kamera, posture recovery, lampu bertingkat — baru terlihat di manual, belum
pernah tertangkap PCAP.

Godaannya adalah menghilangkan aksi-aksi itu dari UI sampai PCAP selesai. Itu
salah dua kali: layout tombol berubah lagi nanti (operator harus belajar ulang),
dan tidak ada tempat yang jelas untuk menaruh hasil PCAP saat sudah ada.

Jadi semua aksi didefinisikan DI SINI, lengkap, sekali. Yang belum terverifikasi
ditandai `enabled=False` — tombolnya tetap muncul di layar tapi redup dan tidak
bisa ditekan, dan backend menolak perintahnya. Begitu PCAP memberi jawaban,
yang perlu diubah hanya SATU baris di file ini:

    "gear": Capability(..., key=None,  values=(),      enabled=False)
    "gear": Capability(..., key="S",   values=(0,1,2), enabled=True)

...dan tombolnya langsung hidup di semua klien, backend langsung menerimanya,
tanpa menyentuh views.py, JavaScript, atau template.

Override tanpa mengedit kode
────────────────────────────
Untuk uji lapangan cepat, aktifkan lewat environment variable:

    set ROV_CAPS_ENABLE=gear:S:0,1,2;tilt:CT:-1,0,1

Formatnya `id:key:v1,v2,...` dipisah titik koma. Berguna saat menguji tebakan
hasil PCAP di dermaga tanpa perlu mengedit dan me-restart dari editor. Yang
permanen tetap harus dituliskan di tabel bawah — env var itu untuk eksperimen,
bukan untuk konfigurasi produksi.

Prinsip yang tidak boleh dilanggar
──────────────────────────────────
Kapabilitas yang `enabled=False` TIDAK PERNAH lolos ke `RovTelemetry.send()`.
Menebak nama field lalu mengirimkannya ke wahana bukan eksperimen yang murah:
protokolnya tidak terdokumentasi, dan tidak ada yang tahu apa yang dilakukan
firmware saat menerima kunci yang tidak dikenal. Ketidaktahuan diselesaikan
dengan PCAP di darat, bukan dengan mencoba-coba di air.
"""
import logging
import os
from dataclasses import dataclass, field
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


# Alasan sebuah kapabilitas dimatikan — dipakai UI untuk menjelaskan ke
# operator KENAPA tombolnya redup. "Belum diketahui" dan "tidak ada
# perangkatnya" adalah dua hal yang sangat berbeda: yang pertama bisa
# berubah setelah PCAP, yang kedua butuh membeli thruster.
REASON_PENDING_PCAP = "pcap"
REASON_NO_HARDWARE = "hardware"
REASON_LOCAL_ONLY = "local"


@dataclass(frozen=True)
class Capability:
    """Satu aksi ROV: apa namanya di protokol, nilai apa yang sah, aktif atau tidak."""

    id: str
    label: str
    kind: str                       # "axis" | "toggle" | "oneshot" | "step"
    key: Optional[str] = None       # nama field protokol; None = belum diketahui
    values: Tuple[int, ...] = ()    # nilai yang diterima; kosong = belum diketahui
    enabled: bool = False
    reason: str = REASON_PENDING_PCAP
    note: str = ""

    @property
    def usable(self) -> bool:
        """
        Aktif DAN lengkap. Kapabilitas yang ditandai enabled tapi tanpa `key`
        atau tanpa `values` adalah salah tulis, bukan fitur — dan salah tulis
        semacam itu harus mati, bukan mengirim `None` ke soket.
        """
        return bool(self.enabled and self.key and self.values)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "enabled": self.usable,
            "reason": None if self.usable else self.reason,
            "note": self.note,
            "values": list(self.values) if self.usable else [],
        }


# ─── Rentang gerak ───────────────────────────────────────────────────────
# -2..2, lima level diskrit. Ini yang teramati di PCAP, bukan asumsi.
# Kehalusan sesungguhnya datang dari gear (field S) — 5 × 3 = 15 level efektif
# — bukan dari resolusi stick. Karena itu JANGAN kirim ±3 "untuk mencoba".
MOVE_VALUES = (-2, -1, 0, 1, 2)


CAPABILITIES = {
    # ══ TERVERIFIKASI ══════════════════════════════════════════════════
    "thro": Capability(
        id="thro", label="Maju / mundur", kind="axis",
        key="thro", values=MOVE_VALUES, enabled=True,
    ),
    "yaw": Capability(
        id="yaw", label="Putar kiri / kanan", kind="axis",
        key="yaw", values=MOVE_VALUES, enabled=True,
    ),
    "lift": Capability(
        id="lift", label="Naik / turun", kind="axis",
        key="lift", values=MOVE_VALUES, enabled=True,
    ),
    "light": Capability(
        id="light", label="Lampu", kind="toggle",
        key="light", values=(0, 1), enabled=True,
        note="Hidup/mati. Kalau PCAP membuktikan lampu bertingkat, "
             "ganti values jadi (0,1,2,3,…) dan kind jadi 'step'.",
    ),
    "holdd": Capability(
        id="holdd", label="Depth lock", kind="toggle",
        key="holdd", values=(0, 1), enabled=True,
    ),
    "holdy": Capability(
        id="holdy", label="Heading lock", kind="toggle",
        key="holdy", values=(0, 1), enabled=True,
    ),

    # ══ MENUNGGU PCAP ══════════════════════════════════════════════════
    "gear": Capability(
        id="gear", label="Gear kecepatan (L/M/H)", kind="step",
        key=None, values=(), enabled=False, reason=REASON_PENDING_PCAP,
        note="Manual vendor menunjukkan X/Y menggeser gear dan telemetri "
             "punya field 'S'. Yang belum diketahui: apakah 'S' bisa DITULIS, "
             "dan encodingnya angka 0/1/2 atau huruf L/M/H. "
             "Uji: geser selektor kecepatan di app vendor sambil capture.",
    ),
    "tilt": Capability(
        id="tilt", label="Tilt kamera (±60°)", kind="step",
        key=None, values=(), enabled=False, reason=REASON_PENDING_PCAP,
        note="D-pad atas/bawah di app vendor. Nama field sama sekali belum "
             "terlihat di PCAP — ini gimbal, kemungkinan perintah terpisah. "
             "Uji: tekan tilt atas/bawah masing-masing 3× dengan jeda.",
    ),
    "posture": Capability(
        id="posture", label="Posture recovery", kind="oneshot",
        key=None, values=(), enabled=False, reason=REASON_PENDING_PCAP,
        note="D-pad kiri di app vendor — sekali tembak, mengembalikan wahana "
             "ke sikap rata. Uji: tekan sekali, cari paket tunggal yang muncul.",
    ),

    # ══ TIDAK ADA PERANGKATNYA ═════════════════════════════════════════
    "lateral": Capability(
        id="lateral", label="Geser samping", kind="axis",
        key=None, values=(), enabled=False, reason=REASON_NO_HARDWARE,
        note="Butuh thruster samping yang tidak terpasang di unit ini. "
             "Berbeda dari yang menunggu PCAP: ini tidak akan pernah muncul "
             "di capture, karena app vendor pun tidak mengirimkannya.",
    ),
}


# ─── Aksi lokal (tidak pernah menyentuh ROV) ─────────────────────────────
# Foto dan rekam ditangani di sisi server dari frame yang sudah ada, jadi
# tidak lewat protokol TCP sama sekali. Didaftarkan di sini supaya UI bisa
# menampilkannya sebaris dengan aksi lain tanpa memperlakukannya sebagai
# perintah wahana.
LOCAL_ACTIONS = {
    "photo": {"label": "Ambil foto", "endpoint": "/api/screenshot"},
    "mark": {"label": "Tandai waypoint", "endpoint": "/api/waypoint"},
}


# ═════════════════════════════════════════════════════════════════════════
#  Override lewat environment
# ═════════════════════════════════════════════════════════════════════════
def _parse_env_override(raw: str) -> dict:
    """
    Urai "gear:S:0,1,2;tilt:CT:-1,0,1" jadi {"gear": ("S", (0,1,2)), ...}.

    Entri yang salah format DILEWATI dengan peringatan, bukan melempar
    exception: environment variable yang salah ketik tidak boleh membuat
    server gagal start di tengah operasi lapangan.
    """
    out = {}
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) != 3:
            logger.warning(f"ROV_CAPS_ENABLE: entri '{chunk}' diabaikan "
                           f"(format harus id:key:v1,v2,...)")
            continue
        cap_id, key, vals_raw = (p.strip() for p in parts)
        if cap_id not in CAPABILITIES:
            logger.warning(f"ROV_CAPS_ENABLE: kapabilitas '{cap_id}' tidak dikenal")
            continue
        if not key:
            logger.warning(f"ROV_CAPS_ENABLE: '{cap_id}' tanpa nama field")
            continue
        try:
            values = tuple(int(v) for v in vals_raw.split(",") if v.strip())
        except ValueError:
            logger.warning(f"ROV_CAPS_ENABLE: nilai '{vals_raw}' untuk "
                           f"'{cap_id}' bukan integer")
            continue
        if not values:
            logger.warning(f"ROV_CAPS_ENABLE: '{cap_id}' tanpa daftar nilai")
            continue
        out[cap_id] = (key, values)
    return out


def _apply_env_overrides():
    raw = os.environ.get("ROV_CAPS_ENABLE", "").strip()
    if not raw:
        return
    for cap_id, (key, values) in _parse_env_override(raw).items():
        old = CAPABILITIES[cap_id]
        if old.reason == REASON_NO_HARDWARE:
            logger.warning(
                f"ROV_CAPS_ENABLE: '{cap_id}' diabaikan — dimatikan karena "
                f"perangkat kerasnya tidak ada, bukan karena protokolnya "
                f"belum diketahui. Mengaktifkannya tidak menumbuhkan thruster."
            )
            continue
        CAPABILITIES[cap_id] = Capability(
            id=old.id, label=old.label, kind=old.kind,
            key=key, values=values, enabled=True,
            reason=old.reason,
            note=f"[override env] {old.note}",
        )
        logger.warning(
            f"⚠  Kapabilitas '{cap_id}' diaktifkan lewat ROV_CAPS_ENABLE "
            f"sebagai '{key}' {values} — ini TEBAKAN yang belum diverifikasi. "
            f"Uji di darat sebelum menyelam."
        )


_apply_env_overrides()


# ═════════════════════════════════════════════════════════════════════════
#  API untuk views / worker
# ═════════════════════════════════════════════════════════════════════════
def allowed_commands() -> dict:
    """
    {nama_field_protokol: (nilai sah, ...)} untuk semua kapabilitas aktif.

    Inilah yang menggantikan konstanta `_ROV_ALLOWED_COMMANDS` yang dulu
    ditulis tangan di views.py. Diturunkan, bukan disalin — supaya tidak
    mungkin ada kapabilitas yang hidup di UI tapi ditolak backend, atau
    sebaliknya.
    """
    return {
        cap.key: cap.values
        for cap in CAPABILITIES.values()
        if cap.usable
    }


def capability_for_key(key: str) -> Optional[Capability]:
    for cap in CAPABILITIES.values():
        if cap.usable and cap.key == key:
            return cap
    return None


def move_keys() -> dict:
    """{id_sumbu: nama_field} untuk sumbu gerak yang aktif."""
    return {
        cap.id: cap.key
        for cap in CAPABILITIES.values()
        if cap.kind == "axis" and cap.usable
    }


def to_dict() -> dict:
    """Payload untuk /api/rov/caps — dipakai browser membangun tombol."""
    return {
        "capabilities": [c.to_dict() for c in CAPABILITIES.values()],
        "local_actions": [
            {"id": k, **v} for k, v in LOCAL_ACTIONS.items()
        ],
        "move_range": {"min": min(MOVE_VALUES), "max": max(MOVE_VALUES)},
    }

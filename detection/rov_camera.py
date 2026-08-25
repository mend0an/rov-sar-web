"""
rov_camera.py — Abstraksi sumber video untuk aplikasi ROV SAR.

Tujuan: satu pintu masuk untuk semua sumber video (webcam USB via DirectShow
maupun stream RTSP ROV Geneinno Titan T1), dengan interface yang MENIRU
cv2.VideoCapture sehingga kode pemanggil tidak perlu berubah:

    cap = open_source(spec)
    ret, frame = cap.read()
    cap.isOpened()
    cap.release()

Kenapa RTSP tidak bisa pakai cv2.VideoCapture:
    OpenCV/FFmpeg salah menangani timing stream ini dan membuang hampir semua
    frame -> gambar beku. PyAV (binding langsung ke libav*, mesin yang sama
    dengan ffplay) mendekode stream yang sama dengan stabil.

    CATATAN KOREKSI (PCAP 09-08-2026): sebelumnya ditulis bahwa server "salah
    menandai clock RTP 90kHz sebagai frame rate". Itu KELIRU. SDP asli ROV
    berbunyi "a=rtpmap:96 H264/90000", dan 90 kHz adalah clock rate RTP
    standar untuk H.264 (RFC 6184) — server tidak salah. Yang terjadi: SDP
    tidak memuat informasi framerate sama sekali, sehingga FFmpeg menyimpulkan
    sendiri dan melaporkan "90k tbr". Penyebab pasti kegagalan OpenCV belum
    dipastikan; yang terbukti hanyalah PyAV bekerja dan OpenCV tidak.

Kenapa perlu thread:
    PyAV bekerja dengan generator blocking (container.demux()), bukan polling.
    Kalau enhancement + YOLO diselipkan di dalam loop demux, decode tertinggal
    dari laju kedatangan packet, buffer menumpuk, dan latency merambat naik
    terus-menerus (tidak crash, tapi makin lama makin telat). Karena itu demux
    dijalankan di thread sendiri yang hanya menyimpan frame TERBARU (overwrite,
    bukan antrian). Frame yang telat ditimpa dan hilang — itu perilaku yang
    benar untuk live feed.

Dependensi: pip install av
Modul ini sengaja tidak mengimpor PyQt agar bisa dipakai ulang di Django.
"""

import os
import threading
import time

import cv2

try:
    import av
    import av.error
    import av.logging
    _AV_AVAILABLE = True
except ImportError:
    _AV_AVAILABLE = False


# Decoder libav menulis peringatan ("Missing reference picture", "reference
# count overflow", dst.) LANGSUNG ke stderr di level C — tidak lewat exception
# Python, jadi tidak bisa ditangkap try/except. Peringatan ini normal saat join
# di tengah GOP dan tidak menandakan kerusakan. Set VERBOSE_AV_LOG = True kalau
# sedang mendiagnosis masalah stream yang sebenarnya.
VERBOSE_AV_LOG = False

if _AV_AVAILABLE and not VERBOSE_AV_LOG:
    av.logging.set_level(av.logging.FATAL)


# ─────────────────────────────────────────────────────────────────────────────
#  Konfigurasi default ROV Geneinno Titan T1
# ─────────────────────────────────────────────────────────────────────────────
# Endpoint terkonfirmasi dari PCAP aplikasi vendor: OPTIONS/DESCRIBE/PLAY
# semuanya memakai path /stream (track video = /stream/track0).
# Tanpa /stream server ternyata tetap melayani, tapi ikuti aplikasi resmi.
ROV_RTSP_URL = "rtsp://192.168.8.9:8554/stream"
ROV_LABEL    = "[ROV] Geneinno Titan T1 (RTSP)"

# Opsi FFmpeg yang sudah TERUJI di lapangan (29.6 fps stabil, 88 detik).
# probesize/analyzeduration WAJIB kecil: timestamp stream rusak, kalau
# probesize besar analisa header tidak pernah selesai.
# Transport UDP, bukan TCP — server RTSP ROV ini tidak men-deliver via TCP.
RTSP_OPTIONS = {
    "rtsp_transport":   "udp",
    "fflags":           "nobuffer",
    "flags":            "low_delay",
    "analyzeduration":  "1000000",
    "probesize":        "2000000",
    "max_delay":        "500000",
}


# ─────────────────────────────────────────────────────────────────────────────
#  RTSPReader — pembaca RTSP berbasis PyAV dengan interface ala VideoCapture
# ─────────────────────────────────────────────────────────────────────────────
class RTSPReader:
    """
    Pembaca RTSP non-blocking. Thread internal terus demux + decode dan
    menyimpan frame terbaru; read() hanya mengambil snapshot frame itu.

    Atribut publik yang berguna:
        frame_id   int  — naik monoton tiap frame baru. Pakai ini untuk dedup
                          (lewati inferensi YOLO kalau frame_id belum berubah).
        n_ok       int  — jumlah frame berhasil di-decode
        n_skip     int  — jumlah packet rusak yang di-skip
        last_error str  — pesan error terakhir (None kalau bersih)
    """

    def __init__(self, url=ROV_RTSP_URL, options=None,
                 open_timeout=10, stale_timeout=5.0, reconnect=True):
        if not _AV_AVAILABLE:
            raise RuntimeError(
                "Modul 'av' (PyAV) tidak terpasang. Jalankan: pip install av"
            )

        self.url           = url
        self.options       = dict(options or RTSP_OPTIONS)
        self.open_timeout  = open_timeout
        self.stale_timeout = stale_timeout   # detik; lewat ini read() -> False
        self.reconnect     = reconnect

        self.frame_id   = 0
        self.n_ok       = 0
        self.n_skip     = 0
        self.last_error = None
        self.width      = None
        self.height     = None

        self._frame     = None
        self._frame_ts  = 0.0
        self._lock      = threading.Lock()
        self._running   = True
        self._opened    = threading.Event()   # set saat frame pertama tiba

        self._thread = threading.Thread(
            target=self._worker, name="RTSPReader", daemon=True
        )
        self._thread.start()

        # Tunggu frame pertama. Stream butuh waktu sampai ketemu keyframe;
        # 13 packet awal biasanya di-skip sebelum gambar pertama muncul.
        self._opened.wait(timeout=self.open_timeout)

    # ── interface ala cv2.VideoCapture ──────────────────────────────────────
    def isOpened(self):
        """True kalau sudah pernah dapat frame dan thread masih hidup."""
        return self._opened.is_set() and self._running

    def read(self):
        """
        Mengembalikan (ret, frame) seperti cv2.VideoCapture.read().
        ret=False kalau belum ada frame sama sekali, atau frame terakhir
        sudah lebih tua dari stale_timeout (stream mati/putus).
        """
        with self._lock:
            frame = self._frame
            ts    = self._frame_ts

        if frame is None:
            return False, None
        if time.time() - ts > self.stale_timeout:
            return False, None
        return True, frame

    def release(self):
        """
        Hentikan thread. Loop demux memeriksa flag tiap iterasi, jadi
        penghentian terjadi pada packet berikutnya (biasanya <50ms).
        Thread bersifat daemon, jadi kalaupun stream benar-benar mati
        proses tetap bisa keluar.
        """
        self._running = False
        self._opened.clear()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def get(self, prop):
        """Dukungan minimal untuk cv2.CAP_PROP_FRAME_WIDTH/HEIGHT."""
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self.width or 0)
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self.height or 0)
        return 0.0

    # ── thread pekerja ──────────────────────────────────────────────────────
    def _worker(self):
        backoff = 1.0
        while self._running:
            container = None
            try:
                container = av.open(
                    self.url, options=self.options, timeout=self.open_timeout
                )
                stream = container.streams.video[0]
                stream.thread_type = "AUTO"
                self.width  = stream.codec_context.width
                self.height = stream.codec_context.height
                self.last_error = None
                backoff = 1.0

                self._demux_loop(container, stream)

            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
            finally:
                if container is not None:
                    try:
                        container.close()
                    except Exception:
                        pass

            if not self._running or not self.reconnect:
                break

            # Backoff sebelum reconnect. JANGAN agresif: server RTSP embedded
            # mudah rusak kalau dihujani permintaan koneksi.
            self._opened.clear()
            time.sleep(backoff)
            backoff = min(backoff * 2, 10.0)

        self._running = False

    def _demux_loop(self, container, stream):
        """
        Demux packet manual. Satu packet rusak di-skip, TIDAK mematikan loop.
        container.decode() langsung akan mati kalau kena InvalidDataError
        di awal (belum ketemu keyframe) — ini meniru cara ffplay menelan error.

        Sebelum keyframe pertama, packet dibuang TANPA di-decode. P-frame yang
        mereferensi frame yang belum pernah dilihat decoder pasti gagal dan
        memuntahkan puluhan baris peringatan libav ke stderr. Menunggu keyframe
        menghilangkan penyebabnya, bukan sekadar menyembunyikan pesannya.
        """
        seen_keyframe = False

        for packet in container.demux(stream):
            if not self._running:
                return

            if not seen_keyframe:
                if not packet.is_keyframe:
                    self.n_skip += 1
                    continue
                seen_keyframe = True

            try:
                frames = packet.decode()
            except av.error.FFmpegError:
                self.n_skip += 1
                continue

            for frame in frames:
                try:
                    img = frame.to_ndarray(format="bgr24")
                except Exception:
                    self.n_skip += 1
                    continue

                self.n_ok += 1
                with self._lock:
                    self._frame    = img
                    self._frame_ts = time.time()
                    self.frame_id += 1

                if not self._opened.is_set():
                    self._opened.set()


# ─────────────────────────────────────────────────────────────────────────────
#  Penemuan & pembukaan sumber
# ─────────────────────────────────────────────────────────────────────────────
_sources_cache = None
_sources_lock = threading.Lock()


def _enumerate_webcams():
    """
    Enumerasi perangkat DirectShow. Dipisah karena ini bagian yang berbahaya:
    pygrabber memakai COM, dan COM harus di-inisialisasi PER THREAD.

    Di aplikasi PyQt5 ini tidak pernah jadi masalah karena selalu dipanggil
    dari thread utama yang COM-nya sudah siap. Di Django, view berjalan di
    thread pool — tanpa CoInitialize, pembuatan FilterGraph bisa gagal atau
    (lebih buruk) menggantung, dan request-nya tidak pernah kembali.
    """
    co_init = False
    try:
        import comtypes
        try:
            comtypes.CoInitialize()
            co_init = True
        except Exception:
            pass
    except ImportError:
        pass

    try:
        from pygrabber.dshow_graph import FilterGraph
        return [(f"[USB {i}] {name}", ("dshow", i))
                for i, name in enumerate(FilterGraph().get_input_devices())]
    finally:
        if co_init:
            try:
                import comtypes
                comtypes.CoUninitialize()
            except Exception:
                pass


def list_sources(rtsp_url=ROV_RTSP_URL, refresh=False):
    """
    Kembalikan daftar (label, spec) untuk diisikan ke dropdown.

    spec berformat:
        ("rtsp", url)   — stream RTSP
        ("dshow", idx)  — webcam DirectShow pada index idx

    HASILNYA DI-CACHE. Enumerasi DirectShow bisa memakan waktu beberapa detik
    — apalagi kalau ada virtual camera seperti OBS, atau kalau salah satu
    device sedang dipakai capture worker. Memanggilnya di setiap request HTTP
    membuat dropdown menggantung di "memuat…". Panggil dengan refresh=True
    kalau operator memang menancapkan kamera baru.

    PENTING: spec harus disimpan lewat addItem(label, spec) dan diambil lewat
    currentData(), BUKAN currentIndex(). Posisi item di dropdown tidak lagi
    sama dengan index device DirectShow setelah item RTSP disisipkan.
    """
    global _sources_cache

    with _sources_lock:
        if _sources_cache is not None and not refresh:
            return list(_sources_cache)

    sources = [(ROV_LABEL, ("rtsp", rtsp_url))]
    try:
        sources.extend(_enumerate_webcams())
    except Exception as e:
        sources.append((f"(gagal deteksi webcam: {e})", None))

    with _sources_lock:
        _sources_cache = list(sources)
    return list(sources)


def open_source(spec):
    """
    Buka sumber video dari spec dan kembalikan objek ber-interface
    VideoCapture. Raise ValueError kalau spec tidak dikenali.
    """
    if not spec:
        raise ValueError("Sumber video tidak valid.")

    kind, value = spec

    if kind == "rtsp":
        return RTSPReader(url=value)

    if kind == "dshow":
        # DirectShow hanya ada di Windows. Di Linux (misal dev/CI) pakai
        # backend default supaya modul ini tetap bisa dites lintas OS.
        if os.name == "nt":
            return cv2.VideoCapture(value, cv2.CAP_DSHOW)
        return cv2.VideoCapture(value)

    if kind == "file":
        return cv2.VideoCapture(value, cv2.CAP_FFMPEG)

    raise ValueError(f"Jenis sumber tidak dikenal: {kind}")


def parse_spec(value):
    """
    Ubah nilai konfigurasi (string dari settings/env, atau spec yang sudah
    berbentuk tuple) menjadi spec yang dimengerti open_source().

        "0"                          -> ("dshow", 0)
        "rtsp://192.168.8.9:8554/…"  -> ("rtsp", url)
        "C:/video/uji.mp4"           -> ("file", path)
        ("rtsp", url)                -> diteruskan apa adanya

    Dipakai supaya ROV_RTSP_URL di settings.py tetap berupa string sederhana
    tapi tetap sampai ke RTSPReader (PyAV), bukan cv2.VideoCapture.
    """
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return tuple(value)

    if isinstance(value, int):
        return ("dshow", value)

    s = str(value).strip()
    if s.isdigit():
        return ("dshow", int(s))
    if s.lower().startswith(("rtsp://", "rtsps://")):
        return ("rtsp", s)
    return ("file", s)


# ─────────────────────────────────────────────────────────────────────────────
#  Uji mandiri: python -m detection.rov_camera
#
#  Dipertahankan setelah porting ke Django app karena inilah cara tercepat
#  memisahkan "RTSP-nya yang bermasalah" dari "Django-nya yang bermasalah"
#  saat di lapangan. Jalankan ini SEBELUM menyalakan server.
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    url = sys.argv[1] if len(sys.argv) > 1 else ROV_RTSP_URL

    print("Sumber yang terdeteksi:")
    for label, spec in list_sources():
        print(f"  {label:45s} -> {spec}")

    print(f"\nMembuka {url} ...")
    cap = open_source(parse_spec(url))

    if not cap.isOpened():
        print(f"GAGAL. Error terakhir: {getattr(cap, 'last_error', None)}")
        raise SystemExit(1)

    print(f"Resolusi : {getattr(cap, 'width', '?')}x{getattr(cap, 'height', '?')}")
    print("Ctrl-C untuk berhenti.\n")

    t0, last_id, shown = time.time(), -1, 0
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                print(f"  stale — {getattr(cap, 'last_error', None)}")
                time.sleep(0.2)
                continue

            fid = getattr(cap, "frame_id", None)
            if fid is None or fid != last_id:
                last_id = fid
                shown += 1

            if shown % 30 == 0:
                el = time.time() - t0
                print(f"  unik {shown} | decode {getattr(cap,'n_ok','?')} | "
                      f"skip {getattr(cap,'n_skip','?')} | {shown/el:.1f} fps")
            time.sleep(0.005)
    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
        el = time.time() - t0
        print(f"\nDurasi {el:.1f}s | frame unik {shown} ({shown/max(el,0.01):.1f} fps)")

# ROV SAR Detection — Django Web Edition (v.beta8.1c)

Versi web-based dari aplikasi PyQt5 **ROV SAR Detection** (human body
detection untuk search-and-rescue). Backend (Python/Django) jalan di laptop
yang terhubung ke router ROV; UI (HTML/CSS/JS) diakses lewat browser dari
device manapun di jaringan yang sama.

> **Konfigurasi lokal:** salin `config.example.bat` menjadi `config.bat`, lalu
> isi path perangkat/model dan token untuk laptop operator. `config.bat`
> sengaja diabaikan Git agar konfigurasi lokal tidak ikut terunggah.

**Status: paritas dengan `yolo_hop_v10_gps.py`.** Versi ini menutup dua
blocker dari v.beta3.1: jalur RTSP dipindah dari OpenCV ke PyAV (OpenCV tidak
bisa mendekode stream ROV Titan T1), dan telemetri ROV lewat TCP 6666 masuk
lengkap dengan panel UI-nya. Lihat CHANGELOG.md dan TEST_RESULTS.md.

Semua lolos test logic/plumbing/integrasi, termasuk uji terhadap ROV tiruan
yang bicara protokol TCP asli. **Validasi terhadap ROV sungguhan adalah
langkah lapangan berikutnya** — yang belum pernah diuji di sini adalah stream
RTSP dan socket telemetri dari perangkat keras yang nyata.

**Ini aplikasi SAR, bukan korosi.** Fitur inspeksi korosi (segmentasi,
severity, XAI, hull-relative coordinate, audit trail) DITUNDA ke branch
terpisah sampai dataset korosi tersedia. Jangan menilai versi ini sebagai
prototipe korosi RIIM.

---

## Arsitektur

```
                     Laptop (ini)
   ┌──────────────────────────────────────────────────┐
   │                                                   │
   │   ┌─── Capture Thread (background) ─────────┐    │
ROV│──►│  • RTSPReader (PyAV, UDP) via rov_camera  │   │
   │   │  • HOP / CLAHE / DCP / WB enhancement      │   │
   │   │  • YOLO v11 inference                       │   │
   │   │  • Update shared frame buffer              │   │
   │   └──────────────────────────────────────────┘    │
   │                                                    │
   │   ┌─── GPS Thread (background) ────────────┐     │
GPS│──►│  • Read NMEA via pyserial                  │   │
   │   │  • Parse $GPRMC / $GNRMC                   │   │
   │   │  • Push update via Channels group           │   │
   │   └──────────────────────────────────────────┘    │
   │                                                    │
   │   ┌─── ROV Thread (TCP 6666) ─────────────┐      │
ROV│◄─►│  • Telemetri R/P/Y/D, suhu, baterai       │    │
   │   │  • Yaw → heading peta, D → depth HOP       │    │
   │   │  • Perintah light / holdd / holdy           │    │
   │   └──────────────────────────────────────────┘    │
   │                                                    │
   │   ┌─── Django + Channels (ASGI) ──────────┐      │
   │   │  HTTP:                                   │     │
   │   │   GET  /              dashboard page    │     │
   │   │   GET  /video         MJPEG stream      │     │
   │   │   GET  /api/state     full snapshot     │     │
   │   │   POST /api/control   toggle HOP/YOLO   │     │
   │   │   POST /api/waypoint  manual mark        │     │
   │   │   GET  /api/waypoints list waypoints     │     │
   │   │   POST /api/waypoints/clear              │     │
   │   │   GET  /api/screenshot download JPG     │     │
   │   │   GET  /api/export    GPX download      │     │
   │   │   POST /api/rov/unlock   buka kunci      │     │
   │   │   POST /api/rov/command  light/hold      │     │
   │   │   POST /api/rov/prefs    heading/depth   │     │
   │   │   GET  /api/sources      daftar kamera   │     │
   │   │  WebSocket:                              │     │
   │   │   ws://.../ws/telemetry  GPS push       │     │
   │   └─────────────────────────────────────────┘     │
   └───────────────────────────────────────────────────┘
                          ▲
                          │ HTTP + WebSocket (port 8000)
                          │ via WiFi/LAN
                  ┌───────┴────────────┐
                  │  Browser Client    │  (laptop, tablet, HP)
                  │  HTML+CSS+JS       │
                  │  + Leaflet.js map  │
                  └────────────────────┘
```

---

## Setup

### 1. Install dependencies

```bash
python -m venv venv
source venv/bin/activate    # Linux/Mac
venv\Scripts\activate       # Windows

pip install -r requirements.txt
```

Catatan: `ultralytics`, `opencv-python`, dan `torch` sudah harus terinstall
dengan CUDA support yang sama seperti project lama-mu. Kalau sudah ada
environment Python lama yang work, sebaiknya pakai itu saja, tinggal tambah:

```bash
pip install Django==5.0.* channels==4.* pyserial
```

### 2. Konfigurasi

Edit `rov_sar_web/settings.py` di bagian bawah, atau pakai environment variables:

Linux/Mac:
```bash
export ROV_RTSP_URL="rtsp://192.168.8.9:8554/stream"   # atau index kamera "0"
export ROV_MODEL_PATH="/path/lengkap/ke/best.pt"
export ROV_GPS_PORT="/dev/ttyUSB0"                     # atau "DUMMY"
export ROV_GPS_BAUD="9600"
export ROV_TELEMETRY_ENABLED="1"                       # nyalakan telemetri ROV
```

Windows (CMD):
```bat
set ROV_RTSP_URL=rtsp://192.168.8.9:8554/stream
set ROV_MODEL_PATH=C:\RISET\Human_Body_Detection\runs\detect\rov_small_yolo11s_720_datatrain_hopv2\weights\best.pt
set ROV_GPS_PORT=COM7
set ROV_GPS_BAUD=9600
set ROV_TELEMETRY_ENABLED=1
```

| Variable | Default | Keterangan |
|---|---|---|
| `ROV_RTSP_URL` | `0` | `rtsp://…` → PyAV; `"0"`/`"1"` → webcam; path → file video |
| `ROV_MODEL_PATH` | `runs/detect/…/best.pt` | Pakai path ABSOLUT kalau ragu |
| `ROV_GPS_PORT` | `DUMMY` | COM port, atau `DUMMY` untuk simulasi |
| `ROV_HOST` / `ROV_PORT` | `192.168.8.9` / `6666` | Telemetri ROV |
| `ROV_TELEMETRY_ENABLED` | `0` | **Default MATI.** Set `1` saat ROV tersedia |
| `ROV_CONTROL_TOKEN` | *(kosong)* | Token kontrol ROV — lihat catatan keamanan |
| `ROV_FAKE_WORKERS` | *(kosong)* | `1` = mode uji tanpa hardware sama sekali |
| `ROV_PROCESS_FPS` | `30` | Plafon laju proses (`0` = tanpa batas) |
| `ROV_LOG_LEVEL` | `INFO` | `DEBUG` untuk log lebih rinci |

**Kenapa `ROV_TELEMETRY_ENABLED` default mati:** tanpa ROV di jaringan,
`RovTelemetry` akan mencoba menyambung tiap 2 detik dan membanjiri log dengan
connection refused. Nyalakan hanya saat ROV benar-benar ada.

### 3. Migrasi (untuk Django admin, optional)

```bash
python manage.py migrate
```

### 4. Jalankan server

```bash
python manage.py runserver 0.0.0.0:8000
```

`0.0.0.0` penting — supaya device lain di jaringan WiFi bisa akses, bukan
cuma laptop ini sendiri.

### 5. Buka di browser

Cek IP laptop (`ipconfig` di Windows, `ifconfig` di Linux), misal `192.168.8.100`.
Lalu dari browser device manapun di jaringan yang sama:

```
http://192.168.8.100:8000/
```

---

## Testing

Test scripts ada di `tests/`. Bisa dijalankan tanpa hardware ROV:

```bash
# Async MJPEG + broadcast (butuh server jalan di mode fake worker)
ROV_FAKE_WORKERS=1 daphne -b 127.0.0.1 -p 8768 rov_sar_web.asgi:application &
python3 tests/test_mjpeg_client.py http://127.0.0.1:8768/video 4
python3 tests/test_ws_broadcast.py http://127.0.0.1:8768

# Unit & integration tests (tanpa server, tanpa hardware)
python3 tests/test_debounce.py
python3 tests/test_gps.py
python3 tests/test_beta31_patches.py
python3 tests/test_rov_telemetry.py   # ROV tiruan bicara protokol TCP asli
python3 tests/test_rov_api.py         # gerbang unlock + token
python3 tests/test_capture_rov.py     # routing PyAV, dedup, clamp depth
python3 tests/test_source_switch.py   # ganti sumber runtime, pacing, stats
```

`ROV_FAKE_WORKERS=1` menjalankan fake frame + GPS injector untuk test tanpa
hardware ROV/GPS/RTSP dan tanpa torch/YOLO. Fake frame tetap di-generate pakai
`cv2.imencode()` (OpenCV memang dependency aplikasi) supaya JPEG-nya valid dan
bisa di-decode. Untuk operasi nyata, jangan set variable ini.

Hasil test terakhir: lihat TEST_RESULTS.md.

## Struktur File

```
rov_sar_web/
├── manage.py
├── requirements.txt
├── README.md
├── rov_sar_web/              ← Django project config
│   ├── settings.py
│   ├── urls.py
│   ├── asgi.py               ← ASGI (untuk WebSocket)
│   └── wsgi.py
├── detection/                ← Django app utama
│   ├── apps.py               ← Start background workers di sini
│   ├── views.py              ← HTTP endpoints (MJPEG, REST)
│   ├── consumers.py          ← WebSocket consumer
│   ├── routing.py            ← WS URL routing
│   ├── urls.py               ← HTTP URL routing
│   ├── state.py              ← Shared state singleton
│   ├── capture.py            ← video + enhancement + YOLO worker
│   ├── gps_worker.py         ← GPS NMEA worker
│   ├── rov_camera.py         ← RTSPReader (PyAV) — port dari project desktop
│   ├── rov_telemetry.py      ← klien TCP 6666 — port dari project desktop
│   ├── rov_worker.py         ← jembatan telemetri → state → WebSocket
│   └── enhancement_utils.py  ← copy dari project lama (tidak diubah)
├── templates/detection/
│   └── dashboard.html        ← UI meniru PyQt5
└── static/detection/
    ├── css/dashboard.css
    ├── js/dashboard.js
    └── img/
        └── ROV_BANNER.jpg    ← TARUH BANNER DI SINI (copy dari project lama)
```

**Banner image:** copy `ROV_BANNER.jpg` dari project lama ke
`static/detection/img/ROV_BANNER.jpg`. Kalau tidak ada, banner otomatis
disembunyikan via `onerror` handler — UI tetap berfungsi tanpa banner.

---

## UI Design

UI dibuat meniru tampilan PyQt5 yang sudah ada:

- **Banner** di atas (copy `ROV_BANNER.jpg` ke `static/detection/img/`)
- **3-kolom layout**: video (flex) | controls (280px) | GPS panel (400px)
- **Palette warna**: light desktop-app theme, map dark navy (`#1c2e45`),
  koordinat monospace warna `#003d80`
- **Status bar** di paling bawah

Layout otomatis stack vertikal di layar < 1100px (responsive untuk tablet/HP).

## Perbedaan dengan PyQt5

Beberapa elemen disesuaikan karena perbedaan arsitektur web vs desktop:

| PyQt5 Original | Web Version |
|---|---|
| Dropdown kamera (DirectShow) | Read-only label (source di-set via env var) |
| Tombol "Mulai/Hentikan Kamera" | Auto-start, indikator `● Streaming aktif` |
| Dropdown port GPS | Read-only label |
| Tombol "Hubungkan/Putus GPS" | Auto-start, indikator `● GPS aktif` |
| Map render QPainter | Leaflet.js dengan tile OpenStreetMap |
| Zoom buttons di map | Sama (zoom in/out + center ke ROV) |

---

## Catatan Penting

1. **Single-process only.** Karena pakai in-memory channel layer dan shared
   state, server ini tidak bisa di-scale ke multiple workers. Untuk
   single-laptop deployment ini OK. Kalau nanti perlu multi-process, ganti ke
   Redis channel layer.

2. **YOLO model dimuat sekali** saat capture thread start. Ini hemat memori
   tapi artinya ganti model = restart server.

3. **MJPEG, bukan WebRTC.** FPS realistis 15-25 di LAN. Cukup untuk SAR
   monitoring; kalau butuh sub-second latency suatu hari, upgrade ke WebRTC.

4. **GPS dummy mode.** Set `ROV_GPS_PORT=DUMMY` untuk test tanpa hardware.

5. **Tidak ada auth.** Untuk LAN tertutup OK; kalau mau dibuka ke internet,
   tambah Django auth atau reverse proxy dengan basic auth.

---

## Keamanan kontrol ROV — BACA SEBELUM DIPAKAI DI LAPANGAN

Ini perbedaan mendasar antara versi web dan versi desktop.

Di PyQt5, kontrol ROV aman karena satu-satunya cara menyentuhnya adalah duduk
di depan laptop. Dashboard ini melayani seluruh LAN. Artinya **siapa pun yang
tersambung ke WiFi ROV bisa mengirim POST dan menggerakkan wahana** — termasuk
orang yang tidak sengaja membuka halaman dari HP-nya.

Dua lapis penjagaan:

1. **Unlock adalah state SERVER.** Checkbox "Buka Kunci Kontrol ROV" tidak
   menonaktifkan tombol secara kosmetik — dia mengubah flag di server, dan
   perintah ditolak dengan HTTP 409 selama flag itu False. Klien tidak bisa
   melewatinya dengan mengirim request langsung.

2. **Token.** Set `ROV_CONTROL_TOKEN` ke string acak, lalu setiap perintah
   harus membawa header `X-ROV-Token`. Kosong = tanpa token, cukup untuk uji
   kolam tertutup, **jangan untuk lapangan terbuka**.

### Perintah gerak tidak tersedia lewat HTTP — ini disengaja

`lift` / `thro` / `yaw` **tidak** diekspos. Kendali gerak butuh laju tinggi dan
dead-man switch: kalau browser mati atau WiFi putus di tengah gerakan, ROV akan
menahan perintah terakhir dan terus bergerak. Request/response HTTP bukan
transport yang tepat untuk itu — perlu WebSocket dengan heartbeat dan auto-stop.
Yang diizinkan hanya `light`, `holdd`, `holdy`; ketiganya idempoten dan aman
kalau koneksi putus.

---

## Urutan uji lapangan yang disarankan

Naik bertahap, jangan langsung ke ROV — kalau ada yang gagal, kamu perlu tahu
lapisan mana yang bermasalah.

**Tingkat 0 — tanpa hardware.** Membuktikan Django/Channels/MJPEG sehat.
```bat
set ROV_FAKE_WORKERS=1
python manage.py runserver 0.0.0.0:8000 --noreload
```
Frame "FAKE" bergerak, marker peta jalan, panel telemetri ROV terisi angka
yang berubah. Pakai `runserver`, bukan `daphne` — daphne tidak melayani file
static, jadi CSS/JS akan 404 dan halaman tampak polos.

**Tingkat 1 — webcam + GPS dummy.** Membuktikan HOP/YOLO/GPU jalan.
```bat
set ROV_FAKE_WORKERS=
set ROV_RTSP_URL=0
set ROV_GPS_PORT=DUMMY
set ROV_MODEL_PATH=C:\path\lengkap\ke\best.pt
python manage.py runserver 0.0.0.0:8000 --noreload
```

**Tingkat 2 — GPS asli.** Ganti `ROV_GPS_PORT` ke COM sungguhan, bawa receiver
ke luar, tunggu fix pertama.

**Tingkat 3 — RTSP ROV.** Uji `rov_camera` sendirian dulu sebelum lewat Django:
```bash
python -m detection.rov_camera
python -m detection.rov_camera rtsp://192.168.8.9:8554/stream   # URL eksplisit
```
Kalau itu jalan, baru set `ROV_RTSP_URL=rtsp://192.168.8.9:8554/stream`.

**Tingkat 4 — telemetri ROV.** Uji sendirian dulu juga:
```bash
python -m detection.rov_telemetry
```
ROV butuh waktu boot lama — pada uji tercatat ~146 detik. Sabar sebelum
menyimpulkan gagal. Kalau jalan, set `ROV_TELEMETRY_ENABLED=1`.

**Tingkat 5 — akses dari HP.** Buka port 8000 di firewall Windows:
```powershell
New-NetFirewallRule -DisplayName "ROV SAR Web 8000" -Direction Inbound `
    -LocalPort 8000 -Protocol TCP -Action Allow
```
Cek IP laptop dengan `ipconfig`, lalu buka `http://192.168.x.x:8000` dari HP.

> Kedua modul di atas punya mode uji mandiri (`python -m detection.…`) yang
> jalan tanpa Django. Itu cara tercepat memisahkan "hardware-nya bermasalah"
> dari "aplikasinya bermasalah" saat di lapangan.

---

## Cara tercepat: jalan.bat

Windows saja. Edit `config.bat` sekali (path model, path video, index kamera),
lalu klik dua kali `jalan.bat`:

```
  [1]  Uji tanpa hardware        (mode FAKE)
  [2]  Trial deteksi dari VIDEO
  [3]  Trial deteksi dari KAMERA / OBS
  [4]  Operasi penuh dengan ROV  (RTSP + telemetri)

  [5]  Cek environment           (GPU, model, paket)
  [6]  Lihat daftar kamera       (cari index OBS)
  [7]  Uji koneksi ROV           (RTSP / telemetri, tanpa Django)
  [8]  Buka firewall port 8000
```

Menu [5] menjalankan inferensi sungguhan lalu melaporkan device dan VRAM —
itu cara paling langsung menjawab "pakai CUDA atau tidak".

Menu [7] menjalankan modul secara mandiri tanpa Django, untuk memisahkan
"hardware bermasalah" dari "aplikasi bermasalah" saat di lapangan.

Kalau lebih suka manual, env var-nya ada di bawah.

---

## Membaca statistik performa

Baris kecil di bawah status stream, dan blok `capture` di `GET /api/state`:

```
cuda:0 (NVIDIA GeForce RTX 5070) · 28.4 fps / cap 30 · YOLO 18.2 ms · proc 4.1 ms · jpeg 3.0 ms
```

- **Device hijau (`cuda:…`)** → inferensi di GPU. **Oranye (`cpu`)** → di CPU.
- **fps jauh di bawah cap** → ada yang jadi bottleneck; lihat angka ms-nya.
- **Error model / sumber** ditampilkan di baris yang sama kalau ada.

Kalau angka ini tidak muncul (`—`), artinya capture worker tidak jalan —
periksa log konsol, sekarang pesan errornya pasti tercetak.

## Ganti sumber kamera

Dropdown "Pilih Sumber Kamera" mengganti sumber tanpa restart server. Model
YOLO tidak dimuat ulang, jadi jeda hanya sepersekian detik. Kalau sumber baru
gagal dibuka, sistem otomatis kembali ke sumber sebelumnya — kamu tidak akan
kehilangan video karena salah pilih.

**File video tidak bisa dipilih lewat dropdown.** Ini disengaja: menerima path
sembarang dari jaringan berarti siapa pun di LAN bisa menyuruh server membuka
file apa pun di laptop dan menyiarkan isinya. Untuk trial dengan file video,
set `ROV_RTSP_URL` ke path file saat start — sumbernya akan muncul di dropdown
sebagai `[aktif] …` supaya kamu tetap bisa berpindah ke kamera lain.

### Trial deteksi dengan video underwater

Dua cara, keduanya didukung:

**Langsung dari file** — reproducible, frame ke-N selalu sama, cocok untuk
membandingkan model atau kombinasi enhancement:
```bat
set ROV_RTSP_URL=D:\video_uji\underwater.mp4
```

**Lewat OBS Virtual Camera** — bisa ganti klip tanpa restart server, cocok
untuk demo.

1. Di OBS, klik **"Start Virtual Camera"** (tombol di panel Controls kanan
   bawah). Tanpa ini OBS tidak muncul sebagai perangkat kamera sama sekali.
2. Pilih dari dropdown di halaman web.

Kalau OBS baru dinyalakan **setelah** server hidup, pilih **"↻ Deteksi ulang
kamera"** di dasar dropdown. Daftar kamera di-cache saat startup supaya
enumerasi DirectShow yang lambat tidak menggantungkan halaman.

> **Penting:** model `…_hopv2` dilatih di atas data yang sudah di-HOP. Kalau
> HOP dimatikan saat trial, distribusi input tidak cocok dengan distribusi
> training dan deteksinya akan jelek — mudah disalahartikan sebagai model yang
> buruk. Biarkan HOP tetap aktif, dan pakai footage underwater mentah (bukan
> yang sudah di-color-correct).

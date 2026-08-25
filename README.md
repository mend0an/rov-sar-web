# ROV SAR Detection — Django Web Edition (v.beta8.1f)

Versi web-based dari aplikasi PyQt5 **ROV SAR Detection** (human body
detection untuk search-and-rescue). Backend (Python/Django) jalan di laptop
yang terhubung ke router ROV; UI (HTML/CSS/JS) diakses lewat browser dari
device manapun di jaringan yang sama.

**Status: beta8.1f.** Jalur RTSP menggunakan PyAV/UDP, telemetri ROV masuk
melalui TCP 6666, dan kendali web mendukung joystick virtual, keyboard,
controller Gamepad API, serta pemetaan kustom per perangkat.

RTSP dan kendali gerak sudah dicoba pada ROV nyata. Uji regresi software dapat
dijalankan tanpa hardware; uji kolam menyeluruh dan validasi operasi berdurasi
panjang tetap harus dilakukan sebelum pemakaian lapangan.

---

## Arsitektur

```
ROV camera ──RTSP/PyAV──► CaptureWorker ──► HOP/YOLO ──► MJPEG /video
                                      └──► shared state ──► /api/state

ROV Titan T1 ◄──TCP 6666──► RovWorker
              telemetri       ├──► shared state + WebSocket
              kontrol         ├──► thro/lift/yaw heartbeat
                              └──► deadman server + force-stop

GPS/serial ──NMEA──► GpsWorker ──► shared state + WebSocket

Browser ──HTTP/JSON──► Django views
        ──WebSocket──► Channels telemetry
```

### Endpoint utama

| Metode | Endpoint | Fungsi |
|---|---|---|
| `GET` | `/` | Dashboard |
| `GET` | `/video` | Stream MJPEG |
| `GET` | `/api/state` | Snapshot status aplikasi, capture, GPS, dan ROV |
| `POST` | `/api/control` | Pengaturan HOP, YOLO, dan capture |
| `GET/POST` | `/api/waypoints`, `/api/waypoint` | Daftar dan penandaan waypoint |
| `POST` | `/api/waypoints/clear` | Menghapus waypoint |
| `GET` | `/api/screenshot`, `/api/export` | Unduh gambar dan GPX |
| `POST` | `/api/rov/unlock` | Buka/kunci kendali ROV |
| `POST` | `/api/rov/command` | Perintah non-gerak seperti lampu dan lock |
| `POST` | `/api/rov/move` | Vektor gerak `thro/lift/yaw`; heartbeat 10 Hz |
| `POST` | `/api/rov/estop` | STOP keselamatan, tidak memerlukan unlock |
| `GET` | `/api/rov/caps` | Daftar kemampuan/perintah yang tersedia |
| `POST` | `/api/rov/sim` | Aktif/nonaktif mode simulasi |
| `GET/POST/DELETE` | `/api/rov/mapping` | Profil pemetaan controller |
| `POST` | `/api/rov/prefs` | Preferensi heading dan depth |
| `GET/POST` | `/api/sources`, `/api/source` | Daftar dan ganti sumber kamera |
| WebSocket | `/ws/telemetry` | Push status GPS, ROV, dan aplikasi |

Sumbu gerak sengaja ditolak oleh `/api/rov/command`. Semua gerakan harus
melalui `/api/rov/move` agar pilot lock, pencatatan heartbeat, dan deadman
server tetap berlaku.

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
pip install "Django>=5.0,<5.2" channels daphne pyserial av
# Windows, bila membutuhkan enumerasi kamera DirectShow:
pip install pygrabber
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

Semua unit dan regression test dapat dijalankan dengan:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

Kelompok pengujian yang tersedia:

- `test_beta81_controller.py` — controller, pilot lock, deadman, simulasi,
  mapping, dan interlock.
- `test_beta81c_atomic_stop.py` — STOP atomik dan race MOVE/STOP.
- `test_beta81d_hold_heartbeat.py` — pengiriman gerak 10 Hz, pelepasan
  joystick, indikator TX, dan layout mobile.
- `test_rov_api.py`, `test_rov_telemetry.py`, `test_capture_rov.py` — API ROV,
  protokol TCP, RTSP/PyAV, dan capture.
- `test_controls.py`, `test_source_switch.py`, `test_batch_env.py` — kontrol
  UI, pergantian sumber, dan launcher Windows.
- `test_gps.py`, `test_debounce.py`, `test_harness.py` — GPS, debounce, dan
  test harness.

Dua pengujian berikut membutuhkan server yang sedang berjalan:

```bash
# Terminal 1 (Windows CMD)
set ROV_FAKE_WORKERS=1
python manage.py runserver 127.0.0.1:8768 --noreload

# Terminal 2
python tests/test_mjpeg_client.py http://127.0.0.1:8768/video 4
python tests/test_ws_broadcast.py http://127.0.0.1:8768
```

`ROV_FAKE_WORKERS=1` menjalankan frame, GPS, dan telemetri tiruan tanpa
hardware ROV/RTSP dan tanpa model YOLO. Hasil pengujian terakhir dicatat di
`TEST_RESULTS.md`.

## Struktur File

```
rov_sar_web/
├── manage.py
├── jalan.bat                    ← launcher Windows
├── config.example.bat           ← contoh konfigurasi lokal
├── requirements.txt
├── README.md
├── UPDATE_beta8.1c.md
├── UPDATE_beta8.1f.md
├── rov_sar_web/
│   ├── settings.py
│   ├── urls.py
│   ├── asgi.py
│   └── wsgi.py
├── detection/
│   ├── apps.py                  ← lifecycle background workers
│   ├── views.py                 ← HTTP/MJPEG/REST endpoints
│   ├── consumers.py             ← WebSocket consumer
│   ├── routing.py
│   ├── urls.py
│   ├── state.py                 ← shared state, pilot lock, move heartbeat
│   ├── capture.py               ← video, enhancement, dan YOLO
│   ├── gps_worker.py
│   ├── rov_camera.py            ← RTSPReader PyAV
│   ├── rov_telemetry.py         ← klien protokol TCP 6666
│   ├── rov_worker.py            ← telemetri, gerak, deadman, force-stop
│   ├── rov_caps.py              ← capability gate perintah ROV
│   ├── controller_profiles.py   ← profil mapping controller
│   └── enhancement_utils.py
├── templates/detection/
│   └── dashboard.html
├── static/detection/
│   ├── css/
│   │   ├── dashboard.css
│   │   └── controls.css
│   ├── js/
│   │   ├── dashboard.js
│   │   └── controls.js          ← joystick, keyboard, gamepad, heartbeat
│   └── vendor/leaflet/
└── tests/
    ├── test_beta81_controller.py
    ├── test_beta81c_atomic_stop.py
    ├── test_beta81d_hold_heartbeat.py
    └── test_*.py
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
| Dropdown kamera (DirectShow) | Daftar sumber dan pergantian kamera dari UI tanpa restart |
| Tombol "Mulai/Hentikan Kamera" | Auto-start dan indikator status streaming |
| Controller desktop | Joystick virtual, keyboard, Gamepad API, dan mapping kustom |
| Dropdown port GPS | Port diatur lewat konfigurasi; status tampil di UI |
| Tombol "Hubungkan/Putus GPS" | Auto-start dan indikator status GPS |
| Map render QPainter | Leaflet.js dengan tile OpenStreetMap |
| Zoom buttons di map | Zoom in/out dan center ke ROV |

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

5. **Tidak ada login pengguna.** Gunakan hanya pada LAN ROV tertutup. Dukungan
   token backend belum terhubung ke input dashboard pada beta8.1f; lihat bagian
   keamanan kendali di bawah.

---

## Keamanan kontrol ROV — BACA SEBELUM DIPAKAI DI LAPANGAN

Dashboard melayani seluruh LAN. Siapa pun yang dapat membuka halaman berpotensi
mengirim perintah, sehingga kendali harus tetap terkunci sampai operator siap.

### Jalur gerak yang berlaku pada beta8.1f

Gerakan **tersedia melalui HTTP** pada `POST /api/rov/move`. Browser mengirim
satu vektor `thro/lift/yaw` setiap 100 ms atau 10 Hz. Endpoint
`/api/rov/command` tidak menerima sumbu gerak; pemisahan ini memastikan semua
gerakan melewati pengaman yang sama.

Pengamannya:

1. **Server unlock.** MOVE ditolak dengan HTTP 409 selama kendali terkunci.
2. **Pilot lock.** Klien pertama yang mengirim MOVE menjadi pilot sementara,
   sehingga dua browser tidak dapat mengendalikan ROV bersamaan.
3. **Heartbeat gerak 10 Hz.** Sumbu aktif dikirim ulang selama stick ditahan.
   Saat dilepas, nilai nol dikirim berulang untuk mengurangi risiko paket STOP
   tunggal hilang.
4. **Deadman server 1,5 detik.** Jika vektor terakhir masih aktif tetapi
   heartbeat browser berhenti, `RovWorker` menjalankan `force_stop()`.
   Pemeriksaan dilakukan sekitar setiap 200 ms.
5. **E-STOP.** `POST /api/rov/estop` menolkan semua sumbu dan tidak
   memerlukan unlock. STOP tetap dikirim ke wahana meskipun mode simulasi
   sedang aktif.
6. **Deadman klien.** Tab tersembunyi, halaman ditutup, dan beberapa kondisi
   kehilangan fokus memicu penolakan input atau E-STOP dari browser.

Indikator `TX … Hz` pada panel kendali menunjukkan ACK nyata dari
`/api/rov/move`. Saat stick aktif, nilai normalnya mendekati 10 Hz. Jika
menjadi `TX macet` atau `TX gagal`, lepaskan stick dan tekan STOP.

### Token kendali

Backend mendukung `ROV_CONTROL_TOKEN` melalui header `X-ROV-Token`.
Namun dashboard beta8.1f **belum mempunyai input token dan belum menyertakan
header tersebut pada request kendali**. Jika token diaktifkan sekarang,
perintah dari dashboard akan ditolak HTTP 403. Sampai dukungan token pada UI
ditambahkan, gunakan jaringan ROV yang tertutup, jangan expose port 8000 ke
internet, dan biarkan kontrol terkunci ketika tidak digunakan.

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

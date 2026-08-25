# CHANGELOG — rov_sar_web v.beta (SAR)

Konteks: migrasi aplikasi ROV SAR (human body detection) dari PyQt5 ke
dashboard Django web.

Versi ini mengimplementasi 7 perbaikan reliability hasil review teknis
v.alpha, semuanya sudah diuji end-to-end (lihat TEST_RESULTS.md).

---

## Perubahan per file

### `detection/views.py`
**FIX #1 — Async MJPEG (blocker utama v.alpha).**
- `video_stream` diubah dari `def` sync generator + `time.sleep()` menjadi
  `async def` + async generator + `await asyncio.sleep()`.
- Alasan: Django ASGI (Daphne) tidak bisa konsumsi sync iterator tak
  berhingga — sebelumnya menghasilkan warning "must consume synchronous
  iterators" lalu request hang.
- Dedup frame: pakai `state.get_frame_with_id()` (counter monotonik),
  BUKAN `id(jpeg)`. `id()` bisa di-reuse GC atau identik untuk bytes yang
  sama → generator skip selamanya. Ini bug yang sempat muncul saat testing.

**FIX #7 — WebSocket broadcast (multi-client sync).**
- Import `broadcast` dari `state`.
- `api_control`: broadcast `control_updated` setelah update.
- `api_waypoint_mark`: broadcast `waypoint_added`.
- `api_waypoints_clear`: broadcast `waypoints_cleared`.
- Sebelumnya endpoint hanya update state + return JSON, tidak sync ke
  browser lain.

### `detection/state.py`
**FIX #1 pendukung** — tambah `get_frame_with_id()` return `(jpeg, counter)`.
**FIX #7 pendukung** — tambah fungsi `broadcast(event, payload)` module-level
yang dipakai views & workers untuk `group_send` ke group `telemetry`.
- Tambah `last_waypoint_pos()` helper.
- `_haversine` tetap module-level (dipakai capture.py untuk spatial dedup).

### `detection/capture.py`
**FIX #3 — Debounce waypoint deteksi.**
- Logic YOLO-detect dipisah ke method `_maybe_tag_waypoint(result)`.
- Anti-spam: cooldown `DETECT_WP_COOLDOWN_S=5.0` + jarak
  `DETECT_WP_MIN_DIST_M=3.0` + confidence `DETECT_MIN_CONFIDENCE=0.5`.
- State baru: `_last_detect_wp_time`, `_last_detect_wp_pos`.
- Sebelumnya: setiap frame dengan box > 0 langsung bikin waypoint (200
  frame = 200 waypoint).

**FIX #4 — RTSP robust.**
- RTSP URL dibuka dengan `cv2.VideoCapture(src, cv2.CAP_FFMPEG)` explicit
  (sebelumnya `cv2.VideoCapture(src)` tanpa backend).
- Kamera lokal: `cv2.CAP_DSHOW` hanya di Windows (`os.name == "nt"`),
  default backend di Linux.
- Reconnect loop: kalau stream putus > `CONSECUTIVE_FAIL_LIMIT=30` frame,
  release + re-init. Kalau init awal gagal, retry dengan backoff (tidak
  langsung mati).
- **Komentar "drain thread" yang misleading DIHAPUS** — sebelumnya komentar
  menyebut ada drain thread padahal tidak ada implementasinya.
- Catatan: opsi `stimeout` + `max_delay` di-set via env var di settings.py.

**FIX #7 pendukung** — pakai `broadcast()` dari state (bukan channel layer
manual yang di-init di worker).

### `detection/gps_worker.py`
**FIX #5 — GPS watchdog + reconnect.**
- Serial: `_run_serial_with_reconnect()` — loop reconnect dengan backoff
  `RECONNECT_BACKOFF_S=3.0`. Sebelumnya thread langsung mati kalau serial
  gagal open.
- `_read_serial_loop()`: kalau readline exception, keluar loop → reconnect
  (bukan `continue` yang bisa jadi busy-loop).
- Watchdog thread `_run_watchdog()`: cek tiap 1s, kalau tidak ada fix >
  `STALE_TIMEOUT_S=10.0` → set `gps_connected=False` + broadcast
  `gps_status`. Sebelumnya `gps_connected` sekali True tidak pernah False.
- Broadcast `gps_status` on connect/disconnect/stale/serial_error.
- Semua broadcast pakai helper `broadcast()` dari state.

### `rov_sar_web/settings.py`
**FIX #4 pendukung** — `OPENCV_FFMPEG_CAPTURE_OPTIONS` diperluas:
`rtsp_transport;tcp|stimeout;5000000|max_delay;500000` (sebelumnya hanya
`rtsp_transport;tcp`).

### `templates/detection/dashboard.html`
**FIX #2 — Offline: Leaflet lokal.**
- Leaflet JS/CSS dari `{% static 'detection/vendor/leaflet/...' %}`
  (sebelumnya `https://unpkg.com/leaflet...`).

**FIX #6 — Rename honest.**
- Map title: "Peta GPS — Posisi ROV" → "Peta GPS — Posisi Buoy Permukaan".
- Tombol "⌖ Ke ROV" → "⌖ Ke Buoy".
- Alasan: GPS surface buoy bukan posisi presisi ROV di bawah air.

### `static/detection/js/dashboard.js`
**FIX #2 — Offline: fallback tile + guard.**
- Guard `typeof L === 'undefined'` → kalau Leaflet gagal load, tampilkan
  pesan, jangan crash (yang tadinya bikin binding kontrol & WS ikut gagal).
- `tileerror` handler: kalau tile OSM gagal (offline), remove tile layer
  setelah 4 error, tampilkan "OFFLINE MODE" overlay (peta jadi kanvas
  dark navy dengan trail + waypoint tetap tampil).
- Guard `if (!map) return` di semua fungsi map (updateRovMarker,
  addWaypointMarker, clearWaypointMarkers).

**FIX #7 — Handle broadcast events.**
- `handleTelemetryEvent`: tambah handler untuk `waypoints_cleared`,
  `control_updated`, `gps_status`.
- `applyControlToUI()`: silent update checkbox saat control_updated dari
  client lain (tanpa memicu event listener → tidak infinite loop).
- Dedup `waypoint_added`: skip kalau timestamp sudah ada di table.
- Tombol clear: cukup POST ke server, biarkan broadcast yang clear UI
  (juga di client lain).

### `static/detection/vendor/leaflet/` (BARU)
**FIX #2** — Leaflet 1.9.4 lokal: `leaflet.js` (147KB), `leaflet.css`
(14KB), `images/` (5 file: marker + layers icons). Di-download dari npm
registry, disimpan lokal supaya jalan tanpa internet.

### `detection/apps.py`
- Tambah `ROV_FAKE_WORKERS` mode untuk testing async MJPEG + broadcast
  tanpa cv2/torch/hardware (fake frame + fake GPS injector).

---

## Yang TIDAK berubah (tetap seperti v.alpha/PyQt5)

- `enhancement_utils.py` — HOP/CLAHE/DCP/WB copy apa adanya dari PyQt5.
- Layout UI 3-kolom, palette warna, label Indonesia.
- Model YOLO default: `rov_small_yolo11s_720_datatrain_hopv2`.
- Endpoint screenshot, source label display, heartbeat status.

## Catatan HOP GPU (belum dikerjakan — perlu profiling)

Pipeline HOP saat ini: `torch.from_numpy → .to(device) → dot → .cpu().numpy()`
per frame, lalu YOLO push lagi ke GPU. Ini melibatkan alokasi tensor + type
conversion + GPU sync — kandidat optimasi TAPI belum terbukti sebagai
bottleneck. Keputusan: profiling dulu sebelum optimasi. Opsi kalau terbukti
lambat: cache faktor HOP per depth 1-7, atau pipeline full-GPU tanpa
bolak-balik CPU. Tidak dikerjakan di v.beta.

---

# v.beta3 — Koreksi bug dari review v.beta2

Enam bug dibetulkan. Sebagian di kode test/support (fake JPEG, test_harness,
test_mjpeg_client, test_gps), sebagian lagi DI KODE PRODUKSI: offline grid
(`dashboard.js`), worker gating (`apps.py`), dan komentar SAR (`capture.py`).

### `detection/apps.py`
- **Fake JPEG valid**: `_start_fake_workers` sekarang generate JPEG via
  `cv2.imencode()` (bisa di-decode), bukan byte hex manual yang cuma punya
  marker SOI. Tiap frame ada counter text supaya beda.
- **Worker gating whitelist** (FIX #4 review): worker HANYA nyala untuk
  `runserver`/`daphne`/`ROV_FORCE_WORKERS`/`ROV_FAKE_WORKERS`. Sebelumnya
  blacklist yang lupa memasukkan `check` → `manage.py check` berpotensi buka
  kamera/GPS/YOLO. Sekarang command apapun selain server tidak start worker.

### `static/detection/js/dashboard.js`
- **Grid offline beneran** (FIX #3 review): `addOfflineGridOverlay()` sekarang
  pakai custom `L.GridLayer` yang menggambar garis grid + label tile di canvas,
  bukan cuma teks "OFFLINE MODE".
- **Guard map menyeluruh**: `map.invalidateSize/zoomIn/zoomOut/setView` semua
  dibungkus `if (map)`. Kalau Leaflet lokal gagal load, tombol tidak error.

### `tests/test_harness.py`
- Hex string rusak (`bytes.fromhex` dengan spasi di tengah angka) diganti
  generator cv2 → JPEG valid. Ada self-test decode di `__main__`.

### `tests/test_mjpeg_client.py`
- Sekarang PARSE multipart boundary + `cv2.imdecode()` tiap part → membuktikan
  frame VALID & ter-decode. Sebelumnya cuma hitung marker `\xff\xd8\xff`
  (membuktikan transport, bukan gambar). PASS syarat: >= 5 frame decoded DAN
  0 gagal decode.

### `tests/test_gps.py`
- Tambah `test_watchdog_runtime()`: jalankan `_run_watchdog()` di thread
  beneran, bikin fix jadi stale, verify `gps_connected` OTOMATIS jadi `False`.
  Sebelumnya cuma cek aritmatika `age > timeout` (logic check).

### `tests/` (semua)
- Tambah `sys.path.insert` supaya bisa di-run dari root maupun dari `tests/`.

---

# v.beta3.1 — Patch startup-state + GPS freshness (review v.beta3)

Empat patch, tanpa rombak arsitektur. Semua diuji (tests/test_beta31_patches.py).

### `detection/state.py`
- **PATCH #1** — `frame_age()` return `None` (bukan `float("inf")`) saat belum
  ada frame. `Infinity` bukan JSON standar → `JSON.parse()` di browser gagal →
  `fetchInitialState()` masuk catch → "Gagal load state awal" saat startup
  (RTSP/YOLO masih loading, frame pertama belum masuk). Sekarang JSON valid.
- **PATCH #2** — tambah helper `gps_fix_is_fresh(gps, max_age_s=10)`: True hanya
  kalau connected + lat/lon ada + fix belum stale. Dipakai bersama oleh manual
  waypoint & deteksi YOLO.

### `detection/views.py`
- **PATCH #2** — `api_waypoint_mark` pakai `gps_fix_is_fresh()`, bukan cuma cek
  `lat is None`. Waypoint manual ditolak (400) saat GPS disconnected/stale,
  supaya tidak mencatat koordinat lama yang terlihat sah.

### `detection/capture.py`
- **PATCH #2** — `_maybe_tag_waypoint` pakai `gps_fix_is_fresh()`. Deteksi YOLO
  tidak menandai waypoint pakai koordinat stale.

### `detection/gps_worker.py`
- **PATCH #3** — saat serial error / readline error, langsung
  `state.gps_connected = False` (thread-safe) + broadcast, tidak menunggu
  watchdog timeout 10s. Menghindari window di mana `/api/state` masih bilang
  connected=True padahal serial sudah putus. Watchdog tetap jadi pengaman untuk
  kasus device diam tanpa exception.

### `detection/apps.py`
- **PATCH #4** — worker gating dukung `runserver --noreload`. Sebelumnya cek
  `RUN_MAIN != "true"` memblokir startup di mode --noreload (RUN_MAIN tidak
  di-set saat --noreload, tidak ada child process). Sekarang: kalau --noreload
  ada di argv, start langsung. Deployment lapangan sering pakai --noreload.

### `static/detection/js/dashboard.js`
- **PATCH #1** — `refreshStatus()` & `fetchInitialState()` handle
  `frame_age_s === null`: `streaming = frame_age_s !== null &&
  Number.isFinite(frame_age_s) && frame_age_s < 5`.

### Dokumentasi (koreksi review)
- README judul: v.beta → v.beta3.1.
- README: klaim "tanpa cv2" diperbaiki — fake worker memang pakai cv2.imencode()
  untuk JPEG valid (cv2 dependency aplikasi).
- CHANGELOG: klaim "semua 6 bug di test/support" dikoreksi — sebagian di
  produksi (dashboard.js, apps.py, capture.py).

---

# v.beta5 — Paritas dengan `yolo_hop_v10_gps.py` (RTSP PyAV + telemetri ROV)

Versi sebelumnya (folder bernama "beta4", isinya v.beta3.1) setara dengan
`yolo_hop_v9_gps.py`: GPS + enhancement + YOLO + waypoint, TANPA telemetri ROV,
dan dengan jalur RTSP yang sudah terbukti keliru. Rilis ini menutup dua blocker
itu.

## BLOCKER #1 — Jalur RTSP dipindah ke PyAV

**Masalah.** `capture.py` membuka RTSP dengan `cv2.VideoCapture(src, CAP_FFMPEG)`
dan `settings.py` memaksa `rtsp_transport;tcp` lewat `OPENCV_FFMPEG_CAPTURE_OPTIONS`.
Dua-duanya salah untuk ROV ini:

- OpenCV/FFmpeg salah menangani timing stream Titan T1 dan membuang hampir
  semua frame → gambar beku. PyAV (binding langsung ke libav*, mesin yang sama
  dengan ffplay) mendekode stream yang sama dengan stabil.
- Server RTSP ROV **tidak men-deliver via TCP sama sekali**. Transport yang
  benar adalah UDP.
- Endpoint default juga salah: `rtsp://192.168.8.8:554/live`. Yang benar,
  terkonfirmasi dari PCAP aplikasi vendor (OPTIONS/DESCRIBE/PLAY),
  adalah `rtsp://192.168.8.9:8554/stream`.

Gejalanya tidak kelihatan sampai ROV dicelupkan — di webcam semuanya normal.

**Perbaikan.**
- `detection/rov_camera.py` (BARU) — port dari project desktop. `RTSPReader`
  berbasis PyAV: thread demux sendiri, keyframe-gating sebelum decode,
  reconnect backoff eksponensial, `frame_id` monotonik, interface ala
  `cv2.VideoCapture`. Opsi FFmpeg yang teruji lapangan (29.6 fps stabil,
  88 detik): `rtsp_transport=udp`, `fflags=nobuffer`, `flags=low_delay`,
  `probesize=2000000`, `analyzeduration=1000000`.
- Ditambahkan di atas versi desktop: `parse_spec()` supaya `ROV_RTSP_URL` di
  settings tetap string sederhana ("0" / URL / path file) tapi tetap dirutekan
  ke kelas yang benar; `open_source()` sekarang lintas-OS (DirectShow hanya
  di Windows) supaya modul bisa dites di Linux/CI.
- `capture.py` memanggil `rov_camera.open_source()`, bukan cv2 langsung.
- `settings.py`: `OPENCV_FFMPEG_CAPTURE_OPTIONS` **dihapus** (dengan komentar
  kenapa — variabel itu tidak berpengaruh ke PyAV dan menyisakannya cuma
  membingungkan). Endpoint default dibetulkan.

**Dedup frame.** RTSPReader menyimpan frame TERBARU (overwrite, bukan antrian),
jadi `read()` bisa mengembalikan frame yang sama berkali-kali kalau loop worker
lebih cepat dari laju kedatangan packet. Meng-enhance dan meng-inferensi ulang
frame identik membakar GPU tanpa menghasilkan gambar baru. Worker sekarang
melewati frame yang `frame_id`-nya belum berubah. Terukur: 197 read → 1 proses.

**Reconnect sadar-RTSP.** RTSPReader sudah reconnect sendiri di thread
internalnya. Membuka ulang dari CaptureWorker justru memutus percobaan yang
sedang berjalan, jadi re-open otomatis sekarang hanya untuk sumber non-RTSP
(webcam, yang memang tidak punya reconnect internal).

## BLOCKER #2 — Telemetri & kontrol ROV

Seluruh panel ROV versi desktop sebelumnya tidak ada di web. Akibatnya field
`Depth/Alt` di dashboard **tidak pernah terisi** — nilainya memang datang dari
telemetri ROV, bukan GPS (sinyal GPS tidak menembus air).

- `detection/rov_telemetry.py` (BARU) — port apa adanya, blok `__main__` dibuang.
- `detection/rov_worker.py` (BARU) — poll `RovTelemetry` @5 Hz → state →
  broadcast WebSocket. Perannya sama dengan QTimer 200ms di versi PyQt5.
  Laju dibatasi 5 Hz: telemetri masuk jauh lebih cepat, tapi mata manusia tidak
  butuh 30 update/detik untuk membaca angka kedalaman, dan tiap broadcast
  menempuh channel layer ke semua browser.
- `state.py` — state telemetri ROV (`rov_data`, `rov_connected`,
  `rov_updated_at`), flag `rov_use_heading` & `rov_auto_depth`, dan
  `active_heading()`.
- `apps.py` — start `RovWorker`, **opt-in** lewat `ROV_TELEMETRY_ENABLED=1`.
  Default mati supaya uji tanpa ROV tidak dibanjiri "connection refused" tiap
  2 detik.

**Heading.** COG dari NMEA RMC adalah arah GERAK buoy permukaan — buoy bisa
hanyut ke timur sementara ROV menghadap utara, dan saat buoy diam nilainya
sampah. `active_heading()` mengutamakan yaw ROV saat telemetri fresh, dan
jatuh kembali ke COG GPS saat telemetri putus. Sumbernya ditampilkan di UI
sebagai "(ROV)" / "(GPS)" — operator perlu tahu yang mana yang sedang dibaca.

**Auto-depth.** Kedalaman ROV (field "D") mengisi slider depth HOP, di-CLAMP
ke 1–7 m. Kurva HOP adalah interpolasi eksak 7 titik berderajat 6; di luar
rentang itu polinomialnya berosilasi dan koreksi warnanya justru rusak.

## Keamanan kontrol ROV — beda mendasar dengan versi desktop

Di PyQt5, kontrol ROV aman karena hanya bisa disentuh orang yang duduk di depan
laptop. Begitu dashboard ini melayani LAN, **siapa pun di WiFi ROV bisa POST ke
endpoint dan menggerakkan wahana**. Dua lapis penjagaan:

1. **Unlock = state SERVER**, bukan checkbox browser. Checkbox hanya mengubah
   flag lewat endpoint; perintah ditolak (HTTP 409) selama flag False, tak
   peduli klien mengirim apa. Perubahan di-broadcast supaya semua klien sinkron.
2. **Token opsional** — kalau `ROV_CONTROL_TOKEN` di-set, tiap perintah harus
   membawa header `X-ROV-Token`. Kosong = tanpa token; cukup untuk uji kolam
   tertutup, JANGAN untuk lapangan terbuka.

**Perintah gerak (`lift`/`thro`/`yaw`) SENGAJA TIDAK diekspos lewat HTTP.**
Kendali gerak butuh laju tinggi dan dead-man switch: kalau browser mati di
tengah gerakan, ROV menahan perintah terakhir dan terus bergerak.
Request/response HTTP bukan transport yang tepat untuk itu. Yang diizinkan
hanya `light`, `holdd`, `holdy` — masing-masing divalidasi rentangnya.

### Endpoint baru
```
POST /api/rov/unlock    {"unlocked": bool}
POST /api/rov/command   {"key": "light|holdd|holdy", "value": 0|1}
POST /api/rov/prefs     {"use_heading": bool, "auto_depth": bool}
GET  /api/sources       daftar sumber video terdeteksi
```
`GET /api/state` kini memuat blok `rov` + `heading` + `heading_source`.

## UI

- **Panel telemetri 8 field** di kolom kanan (roll, pitch, yaw, kedalaman,
  suhu air, suhu internal, baterai, waktu operasi), grid 2 kolom meniru
  `tele_grid` PyQt5. Redup saat telemetri stale.
- **Field Kedalaman akhirnya hidup** — label "Depth/Alt" diganti "Kedalaman"
  karena tidak pernah ada komponen altitude di sini.
- **Tombol lampu / depth hold / heading hold**. Status ON dibaca dari
  telemetri (`L`/`HD`/`HH`), **bukan dari hitungan klik di browser** — kalau
  perintah tidak sampai ke ROV, tombol tidak boleh terlihat menyala.
- **Checkbox unlock berwarna peringatan** + dialog konfirmasi. Mencentangnya
  membuat tombol di bawahnya benar-benar menggerakkan perangkat keras.
- Peringatan suhu internal > 45 °C (ROV didinginkan air; di darat naik terus).
- Label sumber ROV konsisten dengan indikator status di bawahnya — di mode uji
  tertulis "Mode uji — telemetri simulasi", bukan "nonaktif" yang bertentangan
  dengan "Telemetri aktif" tepat di bawahnya.

## Testing

Tiga suite baru, semuanya dijalankan sungguhan (bukan static check):

- `tests/test_rov_telemetry.py` — melawan ROV TIRUAN yang membuka socket TCP
  asli dan sengaja **memotong paket di tengah field**, karena stream TCP tidak
  menghormati batas pesan. Menguji parser, buffer sambungan, handshake
  `apptime`, keepalive ping, jalur perintah, deteksi stale.
- `tests/test_rov_api.py` — gerbang keamanan lewat HTTP sungguhan: perintah
  ditolak saat terkunci, `thro` ditolak, nilai di luar rentang ditolak, token
  divalidasi, body rusak ditolak.
- `tests/test_capture_rov.py` — membuktikan RTSP masuk ke `RTSPReader` dan
  BUKAN cv2; CaptureWorker memproses video sungguhan sampai jadi JPEG yang
  bisa di-decode ulang; dedup frame_id; clamp depth; pemilihan heading
  termasuk fallback saat telemetri putus.

Plus verifikasi browser (Playwright): tidak ada pageerror, tombol tidak bisa
diklik saat terkunci, dan tombol **tidak menyala** saat perintah ditolak.

Test lama (`test_debounce`, `test_gps`, `test_beta31_patches`,
`test_mjpeg_client`) tetap lolos — tidak ada regresi.

## Yang MASIH menjadi gap (belum dikerjakan)

- **Tile peta offline** — masih OSM online dengan fallback grid. Di lapangan
  laptop tersambung AP ROV tanpa internet, jadi peta akan jadi grid kosong.
  Rencana: pre-download tile zoom 14–18.
- **Parser NMEA** masih RMC saja, tanpa validasi checksum dan tanpa
  GGA/VTG/GSA (jumlah satelit, HDOP, fix quality, altitude). Rencana:
  modul `gps_reader.py` yang dipakai bersama desktop & web.
- **`nav_utils.py`** belum dipisah — haversine masih menempel di `state.py`.
- **Ganti sumber video saat runtime** — `/api/sources` baru read-only.
  Mengganti sumber di tengah jalan berarti menghentikan CaptureWorker di
  tengah inferensi YOLO; butuh penanganan lifecycle tersendiri.
- **Kontrol gamepad** — di luar scope, sesuai kesepakatan.

---

# v.beta6 — Pilihan kamera, logging, dan statistik performa

Rilis ini lahir dari satu pertanyaan yang ternyata tidak bisa dijawab:
"ini pakai CUDA atau tidak?" Untuk menjawabnya kami harus bolak-balik membaca
`nvidia-smi`, dan itu gejala dari masalah sebenarnya — aplikasi tidak punya
cara memberi tahu apa yang sedang terjadi di dalamnya.

## BUG — model YOLO tidak pernah dimuat kalau kamera gagal

`run()` memanggil `_init_model()` **setelah** `_init_capture()` berhasil.
Kalau sumber video gagal dibuka, worker berputar di loop retry dan model tidak
pernah masuk GPU sama sekali.

Gejalanya sangat menyesatkan: `nvidia-smi` menunjukkan tidak ada `python.exe`
di daftar proses, seolah CUDA yang bermasalah — padahal yang gagal cuma
kameranya. Urutan sekarang dibalik: model dan koefisien HOP dimuat lebih dulu,
capture menyusul.

## BUG — semua log INFO hilang tanpa jejak

`settings.py` tidak punya konfigurasi `LOGGING`. Django hanya mengarahkan
logger bernama `django.*` ke konsol; logger `detection.*` jatuh ke root yang
tidak punya handler, sehingga Python memakai `lastResort` yang **hanya
meloloskan WARNING ke atas**.

Akibatnya `logger.info` yang memberi tahu sumber video apa yang dibuka, apakah
berhasil, dan model berjalan di device mana — semuanya lenyap. Versi PyQt5
menampilkan itu di UI lewat `_check_device()`; versi web kehilangan jalur itu
saat porting dan tidak ada yang menggantikannya.

Sekarang `detection.*` punya handler konsol sendiri dengan format ringkas
(`14:43:46 INFO    ✅ Video source terbuka`). Level bisa diatur lewat
`ROV_LOG_LEVEL`.

Selain itu `logger.debug(f"YOLO error: {e}")` dinaikkan jadi `warning` —
kegagalan inferensi yang diam-diam persis jenis masalah yang membingungkan:
video tetap jalan, deteksi tidak pernah muncul, dan tidak ada satu baris pun
yang menjelaskan kenapa.

## BARU — pilih sumber kamera saat runtime

Sebelumnya ganti sumber harus lewat env var + restart server.

Yang membuat ini rumit di web (dan alasan ditunda di v.beta5): `CaptureWorker`
adalah thread terpisah yang bisa sedang di tengah inferensi YOLO, dan ada
klien MJPEG yang sedang membaca dari `state`.

Pendekatan yang dipakai: **worker tidak dihentikan.** `request_source()` cuma
menitipkan spec; thread worker sendiri yang menutup capture lama dan membuka
yang baru di awal iterasi berikutnya. Dua konsekuensi penting:

- **Model YOLO tetap di memori** — memuat ulang butuh beberapa detik dan akan
  memutus deteksi jauh lebih lama dari perlunya.
- **Capture tidak pernah dilepas dari thread lain** — melepasnya saat frame
  sedang di-decode bisa membuat PyAV/cv2 crash.

**Fallback saat salah pilih.** Kalau sumber baru gagal dibuka, worker kembali
ke sumber sebelumnya. Operator di lapangan bisa salah pilih, dan kehilangan
video karena itu tidak bisa diterima.

### Keamanan
`file` **tidak** diizinkan lewat `/api/source`. Menerima path sembarang dari
jaringan berarti siapa pun di LAN bisa menyuruh server membuka file apa pun di
laptop dan menyiarkan isinya. Untuk uji dengan file video, pakai `ROV_RTSP_URL`
saat start. Index kamera dan URL RTSP divalidasi bentuknya.

### UI
Field "Pilih Sumber Kamera" yang tadinya read-only jadi dropdown sungguhan.
Entri tanpa spec (misal `(gagal deteksi webcam: …)`) tetap ditampilkan supaya
operator tahu kenapa daftarnya pendek, tapi tidak bisa dipilih. Sumber aktif
yang tidak muncul di hasil enumerasi (file video, atau webcam yang gagal
di-enumerate) disisipkan sebagai `[aktif] …` — tanpa itu dropdown akan tampak
kosong padahal videonya jelas jalan.

## BARU — statistik performa di UI

Baris kecil di bawah status stream: **device**, fps proses, cap fps, waktu
YOLO, waktu enhancement, waktu encode JPEG. Device diberi warna — hijau untuk
`cuda:0`, oranye untuk `cpu`. Error model dan error sumber ikut ditampilkan
di situ.

Ini menjawab dua pertanyaan sekaligus tanpa `nvidia-smi`: "pakai GPU atau
tidak" dan "kenapa terasa lambat" (bottleneck-nya YOLO, HOP, atau encode).

## BARU — `ROV_PROCESS_FPS` (default 30)

Versi PyQt5 memakai `QTimer.start(30)` — plafon ~33 fps, satu frame per tick.
Loop di web sebelumnya tanpa pacing sama sekali: memproses secepat GPU sanggup,
yang terdengar bagus tapi justru membuat browser di laptop yang sama tersendat
karena berebut CPU, dan mengaburkan perilaku debounce waypoint yang berbasis
waktu. Set `0` untuk kembali tanpa batas.

## Testing — 72/72 PASS

`tests/test_source_switch.py` (BARU, 19 test). Ganti sumber dibuktikan dengan
**membaca warna frame yang keluar** (video biru → video merah), bukan sekadar
percaya nilai `worker.source`. Juga: fallback saat sumber gagal, worker tetap
hidup tanpa restart, pacing terukur 19.9 fps dari target 20, dan penolakan
`file` / index non-integer / URL non-RTSP di endpoint.

Test yang menangkap bug urutan init: worker dijalankan dengan sumber yang
sengaja tidak ada, lalu diperiksa bahwa `model_error` tetap terisi — bukti
`_init_model()` dipanggil walau capture gagal.

Semua suite v.beta5 tetap lolos tanpa perubahan.

## Tambahan v.beta6 — launcher batch

`config.bat` (diedit sekali) + `jalan.bat` (menu 8 pilihan). Menggantikan
hafalan `set VAR=...` yang panjang dan mudah salah.

Detail yang gampang terlewat tapi penting:

- **Subroutine `:BERSIH`** mengosongkan semua env var sebelum tiap menu.
  Tanpa ini, berpindah dari menu [4] ke [2] akan mewarisi
  `ROV_TELEMETRY_ENABLED=1` dan aplikasi terus mencoba menyambung ke ROV yang
  tidak ada. `tests/test_batch_env.py` memverifikasi daftarnya lengkap dengan
  membandingkan var yang di-set tiap menu terhadap yang dibersihkan.
- **Verifikasi conda yang sungguhan.** `conda activate` tidak selalu memberi
  errorlevel yang bisa dipercaya, jadi yang diperiksa adalah apakah paketnya
  benar-benar bisa di-import. `exit /b 1` di dalam subroutine juga hanya
  keluar dari subroutine — pemanggil memeriksa flag `OK` secara eksplisit.
- **File ditulis ASCII murni + CRLF.** Karakter garis panjang jadi mojibake
  di code page CMD default.

`tests/test_batch_env.py` (BARU, 18 test) memverifikasi tiap menu menghasilkan
konfigurasi Django yang benar, karena batch file tidak bisa dijalankan di CI.

## Perbaikan v.beta6a - dropdown kamera menggantung di "memuat..."

Laporan lapangan: dropdown sumber kamera tidak pernah terisi.

**Penyebab.** `/api/sources` memanggil `list_sources()` di setiap request, dan
enumerasi itu memakai pygrabber -> COM -> DirectShow. Dua masalah sekaligus:

1. **COM butuh inisialisasi per-thread.** Di aplikasi PyQt5 ini tidak pernah
   terasa karena `list_sources()` selalu dipanggil dari thread utama yang
   COM-nya sudah siap. View Django berjalan di thread pool, tempat COM belum
   di-inisialisasi - pembuatan `FilterGraph` bisa gagal atau menggantung, dan
   request-nya tidak pernah kembali.
2. **Enumerasinya lambat.** Beberapa detik itu wajar, apalagi kalau ada
   virtual camera seperti OBS atau kalau salah satu device sedang dipakai
   capture worker.

Ini kelalaian porting: kode dipindahkan apa adanya tanpa memperhitungkan bahwa
konteks eksekusinya berubah dari thread utama Qt menjadi thread pool web.

**Perbaikan.**
- Enumerasi di-cache di level modul, dan **dihangatkan saat startup dari
  thread utama** (`apps.py`), bukan saat request pertama.
- `_enumerate_webcams()` memanggil `CoInitialize()`/`CoUninitialize()`
  sendiri, jadi tetap aman kalau dipanggil dari thread mana pun.
- `/api/sources?refresh=1` untuk enumerasi ulang.
- **Timeout 12 detik di sisi browser.** Tanpa ini, request yang menggantung
  meninggalkan dropdown di "memuat..." selamanya tanpa memberi tahu operator
  apa yang salah.
- Opsi **"Deteksi ulang kamera"** di dasar dropdown. Karena daftarnya di-cache,
  OBS Virtual Camera yang baru dinyalakan SETELAH server hidup tidak akan
  muncul sampai di-refresh.

`tests/test_source_switch.py` bertambah 5 test, termasuk pemanggilan dari
thread lain - kondisi yang persis memicu bug ini.

## v.beta7 — Lapisan kendali wahana (2026-08-24)

Menambahkan kendali gerak dari browser: pad sentuh, keyboard, dan gamepad
(Gamepad API) mengisi satu vektor yang sama. Pemetaan tombol mengikuti tata
letak aplikasi vendor Geneinno, bukan konvensi gamepad umum — supaya operator
yang terlatih di app bawaan tidak perlu belajar ulang.

### Baru
- `detection/rov_caps.py` — tabel kapabilitas, sumber tunggal untuk "perintah
  apa yang boleh dikirim". Aksi yang belum terverifikasi PCAP (gear, tilt,
  posture) tetap dirender di UI tapi mati; mengaktifkannya nanti cukup satu
  baris di file ini, tanpa menyentuh view, template, atau JavaScript.
  Override sementara lewat `ROV_CAPS_ENABLE=gear:S:0,1,2`.
- `static/detection/js/controls.js` — lapisan masukan browser. Kuantisasi
  -1..1 → -2..2 identik dengan `quantize()` di `controller_mapper.py`
  (diverifikasi baris demi baris). Penggabungan sumber memakai magnitudo
  terbesar per sumbu, bukan penjumlahan.
- `static/detection/css/controls.css` — pad sentuh, bar sumbu, tombol aksi.
  Target sentuh ≥44 px; tombol mati membedakan "menunggu PCAP" (biru putus)
  dari "tidak ada perangkat" (abu-abu coret).
- `POST /api/rov/move` — vektor tiga sumbu sekaligus, dengan pilot lock.
- `POST /api/rov/estop` — nolkan semua sumbu; tidak butuh unlock maupun
  status pilot.
- `GET /api/rov/caps` — daftar kapabilitas untuk membangun tombol.
- `tests/test_controls.py` — 44 uji, termasuk paritas pemetaan tombol
  terhadap `controller_mapper.py`.

### Keselamatan
- **Deadman sisi server** (`RovWorker._check_deadman`): ROV mengunci perintah
  terakhir, jadi klien yang mati saat wahana bergerak berarti wahana terus
  bergerak. Watchdog menolkan gerak setelah 1,5 detik tanpa perintah. Harus di
  server, karena mode kegagalan yang paling mungkin adalah browser-nya sendiri
  yang berhenti mengirim.
- **Pilot lock**: satu klien memegang kendali gerak pada satu waktu, klaim
  kedaluwarsa 3 detik setelah perintah terakhir. Tanpa ini, dua HP di LAN yang
  sama mengirim vektor berselisih 10× per detik.
- **Sumbu gerak ditolak di `/api/rov/command`** — satu-satunya jalan adalah
  `/api/rov/move`, supaya pilot lock dan pencatatan deadman tidak bisa
  dilewati.
- Nilai di luar -2..2 ditolak, bukan di-clamp: clamp menyembunyikan bug
  kalibrasi klien sampai wahana sudah di air.
- Deadman sisi klien: tab disembunyikan, gamepad tercabut, atau halaman
  ditutup (`sendBeacon`) → stop segera.

### Catatan perangkat
- Controller fisik bermerek GENEINNO (kemungkinan OEM iPega), baterai internal
  3.7V 400mAh, port micro-USB kemungkinan besar charge-only — pairing lewat
  Bluetooth.
- LT/RT diduga digital, belum diverifikasi. `calibratePad()` membaca nilai
  istirahat tiap sumbu untuk memisahkan stick dari trigger, jadi kesimpulan
  itu tidak perlu ditebak dari nama perangkat.

### Belum
Gear (field `S`), tilt kamera, posture recovery, dan lampu bertingkat menunggu
PCAP. Rekam video belum ada; LB diikat ke tangkap frame.

### v.beta7a — Perbaikan paritas pemetaan

`controller_mapper.py` (acuan, dibaca dari manual vendor) menetapkan tekan
stick KIRI = auto-heading dan stick KANAN = auto-depth. Versi web pertama
menukarnya. Sudah diperbaiki, dan `TestMappingParity` kini menjaganya:
operator yang melatih memori otot di alat pemetaan lalu menemukan L3/R3
tertukar akan menekan yang salah persis saat sedang tidak sempat berpikir.

Selisih yang memang disengaja (LB rekam→foto, RB lampu bertingkat→toggle)
sekarang tertulis eksplisit di `controls.js`, bukan senyap.

## v.beta8 — Mode simulasi & pemetaan kustom (2026-08-24)

Dipicu temuan lapangan: controller Geneinno tidak terdeteksi sama sekali
(pygame maupun Gamepad API), sementara Xbox terdeteksi tapi pemetaannya
tidak cocok.

### Mode simulasi — `POST /api/rov/sim`
Perintah divalidasi dan disiarkan seperti biasa, tapi tidak pernah menyentuh
soket TCP. Sebelumnya menguji tombol memaksa ROV menyala dan bergerak di
lantai; sekarang seluruh pemetaan bisa diuji tanpa wahana.

- `record_move()` tetap dipanggil, supaya perilaku deadman ikut teruji —
  watchdog yang cuma diuji dengan ROV nyala tidak pernah benar-benar diuji.
- Validasi rentang tetap berlaku; simulasi justru tempat binding salah
  seharusnya ketahuan.
- Keluar dari simulasi menolkan gerak lebih dulu: kalau tidak, perintah nyata
  pertama bisa menggerakkan wahana pada nilai yang tadinya cuma pura-pura.
- Tidak disimpan permanen — restart kembali ke mode nyata. Mode simulasi yang
  tertinggal menyala berarti operator menekan STOP dan tidak terjadi apa-apa.
- Statusnya pita penuh selebar panel, bukan centang kecil: kesalahan yang
  berbahaya adalah mengira sedang simulasi padahal perintahnya sungguhan.

### Pemetaan kustom — `detection/controller_profiles.py`
Mode belajar: pilih aksi, tekan tombolnya. Selama memetakan, tidak ada input
yang diteruskan sebagai perintah — mengikat tombol ke "maju" tidak boleh
sekaligus membuat wahana maju.

- Profil disimpan di SERVER, bukan localStorage: profil menggambarkan
  perangkat keras, bukan preferensi orang. Pad yang sama dipindah ke tablet
  cadangan tetap terpetakan.
- Kunci dari `Vendor:xxxx Product:xxxx` kalau ada — nama perangkat berubah
  antar browser dan OS, pasangan vendor/product tidak.
- Slot "b<n>" tombol, "a<n>" sumbu, akhiran "-" membalik arah.
- Satu slot = satu aksi, satu aksi = satu slot; ikatan lama dilepas otomatis.
- Aksi tak dikenal dibuang saat simpan; berkas rusak = kehilangan kustomisasi,
  bukan kehilangan kendali.

### Uji
61 uji (dari 44). Termasuk pembacaan slot dan mode belajar di sisi JS.

### Catatan perangkat
Controller Geneinno tidak enumerate sebagai HID gamepad di Windows, sementara
Xbox bisa — jadi jalur HID-nya sehat dan masalahnya ada di pad. Dua hipotesis:
pad sedang di mode yang tidak enumerate di Windows (kombo HOME + A/B/X/Y),
atau ia periferal BLE dengan servis proprietary yang hanya dimengerti aplikasi
vendor. Kalau hipotesis kedua benar, pad itu tidak akan pernah terbaca Gamepad
API di laptop mana pun — tapi arsitekturnya memang membaca gamepad di browser
HP, bukan laptop. Uji berikutnya: pair ke HP Android, buka penguji gamepad di
Chrome HP.

---

## v.beta8.1 — Perbaikan lapisan controller (2026-08-24)

Rilis perbaikan saja. Tidak ada fitur baru, tidak ada perubahan GPS, tidak
ada perubahan protokol ROV. Cakupannya sengaja dipersempit ke satu lapisan
supaya kalau uji kolam berikutnya masih bermasalah, sumbernya bisa
dipastikan bukan akibat perubahan di tempat lain.

Temuan awalnya satu keluhan: **pemetaan controller tidak bisa diubah.**
Ternyata di bawahnya ada empat bug yang saling menumpuk, dan semuanya
senyap — tidak ada yang melempar kesalahan, tidak ada yang muncul di log.

### BUG 1 — Konfigurasi controller ikut terkunci bersama kendali gerak

`.pad-panel.locked` memasang `pointer-events: none` pada **seluruh** panel,
sementara kalibrasi, deadzone, mode simulasi, dan seluruh kotak pemetaan
kustom berada di dalamnya. Satu-satunya pengecualian adalah tombol STOP.

Akibatnya tombol "petakan" terlihat tapi mati selama kendali ROV masih
terkunci — dan petunjuk di UI-nya sendiri ("nyalakan mode simulasi dulu")
menyuruh operator menekan sesuatu yang juga ikut terkunci. Deadlock murni.

Ini juga salah secara konsep. Memetakan tombol tidak menggerakkan apa pun;
memaksa operator membuka kunci kendali fisik hanya untuk menyiapkan pad
adalah menyuruhnya mengambil risiko tanpa alasan. `controller_mapper.py`
versi desktop memang dirancang dengan asumsi sebaliknya.

**Bonus temuan.** Aturan yang mencoba menjaga STOP tetap terang:

```css
.pad-panel.locked .estop-btn { pointer-events: auto; filter: none; opacity: 1; }
```

`pointer-events` memang bisa dibatalkan anak, tapi `opacity` dan `filter`
tidak: keduanya di elemen induk membentuk satu grup rendering — subtree
dikomposit dulu, baru diberi transparansi. Jadi sejak panel ini ada, tombol
STOP **selalu** ikut redup 42% + grayscale setiap kali terkunci, meski ada
komentar di atasnya yang berbunyi "tidak pernah ikut meredup". Aturannya ada
sejak awal dan tidak pernah bekerja sekali pun.

Sekarang peredupan dipindah ke anak yang memang harus redup:

- `.pad-panel.locked` → hanya `pointer-events: none`
- `.axis-bars` + `.pad-body` → yang diredupkan
- `.estop-btn` + `.pad-foot` + `.learn-box` → terang penuh, bisa diklik

Aman karena penguncian yang sebenarnya tidak pernah ada di CSS: gerbang
gerak ada di `tick()`, di jalur keyboard, di aksi gamepad, dan di server.

### BUG 2 — `padIndex` tidak pernah terisi kalau event terlewat

`padIndex` hanya diisi oleh `gamepadconnected`. Event itu menyala sekali,
pada dokumen yang sedang hidup saat pad memberi input pertamanya. Kalau
halaman di-refresh dengan pad sudah menyala, atau tab dibuka di perangkat
kedua, event-nya tidak pernah datang — dan `pollGamepad()` berhenti di
baris pertama:

```js
if (padIndex === null || !navigator.getGamepads) return;
```

Gejalanya persis keluhan awal: klik "petakan", baris berubah jadi "tekan…",
lalu tidak ada tombol fisik apa pun yang bisa mengakhirinya. Tanpa pesan.

- `syncGamepad()` memindai `navigator.getGamepads()` tiap tick.
- `registerPad()` / `forgetPad()` memisahkan pemasangan dari pemindaian:
  `loadProfile()` menembak jaringan dan `calibratePad()` mengambil nilai
  istirahat — keduanya tidak boleh jalan 10x/detik.
- Event `gamepadconnected` / `gamepaddisconnected` tetap dipasang: ia datang
  lebih cepat, dan pencabutan harus langsung memicu STOP tanpa menunggu tick.
- `getGamepads()` melempar `SecurityError` di non-secure context — yaitu saat
  dashboard dibuka dari tablet lewat `http://192.168.x.x`. Sekarang ditangkap
  dan dijelaskan, bukan dibiarkan tampak seperti pad rusak.
- Kotak pemetaan menampilkan peringatan eksplisit saat belum ada pad,
  berikut apa yang harus dilakukan.

### BUG 3 — Satu binding kustom mematikan seluruh pemetaan bawaan

Blok pemetaan kustom di `pollGamepad()` diakhiri `return`, melewati fallback
sumbu **dan** seluruh loop `PAD_BUTTONS`. Jadi begitu operator menyimpan satu
binding saja, stick maju/naik/putar dan semua tombol bawaan mati sekaligus.

Sekadar membuang `return` juga salah. Kalau bawaan dan kustom berjalan
berdampingan, memetakan lampu ke B membuat RB **dan** B sama-sama menyalakan
lampu — dan tidak ada cara melihatnya di UI.

`effectiveBindings()` menggabungkan keduanya **per aksi**: aksi yang muncul di
profil memakai slot kustom dan binding bawaannya dilepas; aksi yang tidak
disebut tetap memakai bawaan. Override parsial, bukan pengganti total.

Jalur sumbu dan jalur tombol sekarang juga memakai daftar yang sama. Deteksi
tepi-tekan berkunci pada slot, bukan indeks tombol, jadi tidak mungkin lagi
satu tombol punya dua penghitung status yang bergerak sendiri-sendiri.

### BUG 4 — `tick()` memakai nilai gamepad dari tick sebelumnya

`mergeSources()` dipanggil sebelum `pollGamepad()`, jadi vektor yang dikirim
selalu hasil poll satu tick sebelumnya — 100 ms pada loop 10 Hz. Kecil di
meja, tapi ia menumpuk di atas latensi jaringan, TCP, respons thruster, dan
RTSP yang dilihat operator. Urutannya ditukar.

### BUG 5 — Dua pemilik untuk satu checkbox unlock

`controls.js` dan `dashboard.js` sama-sama memasang listener `change` pada
`ctrl-rov-unlock`. Milik `controls.js` berjalan sinkron dan langsung menyetel
`unlocked = true` dari nilai checkbox; milik `dashboard.js` baru menyelesaikan
POST-nya setelah `await`.

Jalur *cancel* aman — `confirm()` ditolak mengembalikan checkbox sebelum
`await` mana pun. Yang bocor jalur **POST gagal**: server menolak unlock,
`dashboard.js` mengembalikan checkbox, tapi menyetel `.checked` lewat script
tidak memicu `change`, jadi `controls.js` tidak pernah tahu. Panel tetap
terbuka, perintah gerak tetap terkirim, dan ditolak server satu per satu.

Listener di `controls.js` dihapus. Satu-satunya jalan masuk sekarang
`dashboard.js` → `applyUnlockToUI()` → `RovControls.setUnlocked()`, dan jalur
gagal memanggil `applyUnlockToUI(!want)` — mengembalikan seluruh state, bukan
cuma centangnya. Status server yang authoritative, bukan posisi checkbox di
satu browser.

### Uji — `tests/test_beta81_controller.py`, 31 uji

Diverifikasi dua arah: 31/31 lulus di beta8.1a. Dijalankan terhadap beta8,
21 uji terkumpul dan hasilnya **13 failure + 4 error** — keenam bug
tertangkap. (Angka "12 gagal" di draf changelog sebelumnya salah: yang benar
waktu itu 11 failure + 1 setup error.) Uji lama tetap hijau
(`test_controls` 61, `test_rov_api` 23/23, `test_rov_telemetry` 12/12,
`test_gps`, `test_debounce`, `test_beta31_patches`).

Sebagian besar membaca berkas sumber, bukan menjalankannya — yang rusak
adalah CSS dan struktur pemasangan listener, dan keduanya tidak punya jalur
Python untuk diuji. `TestEffectiveBindings` adalah pengecualian: ia menyalin
`effectiveBindings()` yang asli ke node dan benar-benar menjalankannya, jadi
aturan override per-aksi diuji sebagai perilaku, bukan sebagai teks. Kelas
itu otomatis di-skip kalau node tidak terpasang.

Satu uji sengaja memeriksa markup: kalau `pad-foot` atau `learn-table` suatu
saat dipindah keluar dari `#pad-panel`, aturan pengecualian di CSS berubah
jadi sampah yang menyesatkan — dan uji itu yang memberi tahu.

### Yang TIDAK berubah

Protokol ROV, `rov_caps.py`, endpoint `/api/rov/*`, pilot lock, deadman,
mode simulasi, format `controller_profiles.json`, dan seluruh jalur GPS.
Profil controller yang sudah tersimpan tetap terbaca.

### Catatan untuk uji berikutnya

Bug 1 dan Bug 2 berdiri sendiri. Setelah CSS diperbaiki, tombol "petakan"
bisa diklik — tapi kalau pad memang tidak enumerate sebagai HID gamepad,
tabelnya akan menampilkan peringatan "belum ada gamepad terdeteksi", bukan
diam. Jangan simpulkan CSS-nya gagal kalau itu yang muncul; itu Bug 2 yang
sedang bekerja sebagaimana mestinya, dan artinya masalahnya di perangkat.

GPS ditunda ke beta9: baud BU-353N5 (manual perangkat menyebut 4800,
sementara `config.bat` masih 9600 — belum menggigit karena defaultnya masih
`DUMMY`), `gps_status` yang menyiarkan `connected: True` sebelum fix RMC
pertama, dan parsing GGA untuk jumlah satelit / HDOP.

---

## v.beta8.1a — Koreksi hasil review beta8.1 (2026-08-24)

Beta8.1 tidak lolos review. Dua temuan, satu di antaranya blocker.

### BLOCKER — aturan override baru berjalan satu arah

Changelog beta8.1 mengklaim `effectiveBindings()` melepas binding bawaan
kalau **aksinya** dipetakan ulang **atau** kalau **slot fisiknya** dipakai
aksi lain. Yang benar-benar ada di kode hanya syarat pertama:

```js
const overridden = new Set(Object.values(custom));
...
for (const [idx, act] of Object.entries(PAD_BUTTONS)) {
    if (overridden.has(act)) continue;      // hanya aksi
    buttons.push(['b' + idx, act]);
}
```

Akibatnya, dengan bawaan `b5 → light`:

| Profil operator | Hasil beta8.1 | Seharusnya |
|---|---|---|
| `{"b5":"mark"}` | `b5→mark` **dan** `b5→light` | `b5→mark` |
| `{"b5":"thro"}` | `b5→thro` **dan** `b5→light` | `b5→thro` |
| `{"a1-":"light"}` | `a1-→light` **dan** `thro` fallback di sumbu 1 | hanya lampu |

Baris pertama masih tertolong kebetulan: kedua binding ada di daftar tombol
yang sama, jadi `padPrevSlots` membuat yang diproses lebih dulu menang. Itu
efek samping urutan loop, bukan aturan.

Baris kedua tidak tertolong apa pun. `thro` dibaca di jalur sumbu dan
`light` di jalur tombol — dua pembacaan terpisah dari slot yang sama. **Satu
tombol menggerakkan wahana sekaligus menyalakan lampu.** Untuk perangkat
lunak kendali, itu blocker.

Baris ketiga muncul karena perbandingan slot memakai string mentah, padahal
`a1` dan `a1-` adalah sumbu fisik yang sama; akhiran itu hanya arah baca.

Diperbaiki dengan `usedSlots` yang menyimpan bentuk telanjang tiap slot
kustom, dipakai untuk membuang binding tombol bawaan **dan** fallback sumbu
gerak yang slotnya sudah diambil.

### BUG — profil pad lama menempel ke pad baru

`registerPad()` membersihkan `padRest` dan `padPrevSlots` tapi tidak
`customMap`, sementara `loadProfile()` hanya menimpa saat profil yang datang
tidak kosong:

```js
if (m && Object.keys(m).length) { customMap = m; }
```

Pad tanpa profil karena itu mewarisi pemetaan pad sebelumnya. Bug ini lahir
di beta8 dan nyaris tak terlihat selama `padIndex` cuma terisi sekali per
muat halaman — rescan panas di beta8.1 justru menaikkan kelasnya, karena
ganti pad di tengah operasi jadi hal yang wajar.

- `registerPad()` mereset `customMap`, `learnSlot`, `padAxisMap`, `padRest`,
  `padPrevSlots`.
- `loadProfile(id)` menerima id eksplisit dan mengecek `padId !== wanted`
  sebelum memasang hasilnya: fetch pad lama bisa tiba setelah pad baru
  terdaftar, dan tanpa penjaga itu ia memasang pemetaan perangkat yang sudah
  dicabut.
- Profil kosong sekarang **menulis** `customMap = null`, bukan dilewati.
  Melewatinya persis cara pemetaan lama bertahan.

### Minor

`padApiBlocked` sekarang langsung merender ulang kotak pemetaan, tapi hanya
pada transisi — `syncGamepad()` jalan 10x/detik dan render tiap tick akan
membangun ulang DOM terus-menerus.

### Uji

31 uji (dari 22): 9 uji ditambahkan, 7 di antaranya mereproduksi kegagalan
pada beta8.1 dan 2 sisanya guard regression yang memang sudah lolos di sana.

### Catatan

Kesalahan beta8.1 bukan di kodenya saja: changelog-nya menjelaskan aturan
yang lebih lengkap daripada yang diimplementasikan, dan uji yang menyertainya
hanya menguji kasus `{"b1":"light"}` — satu-satunya bentuk yang aturan
setengah jadi itu memang tangani dengan benar. Uji yang dipilih mengikuti
implementasi, bukan spesifikasi, jadi 22/22 hijau tanpa arti.

---

## v.beta8.1b — Patch keselamatan lapisan kendali (2026-08-24)

Beta8.1a lolos review untuk semua yang direview sebelumnya. Yang di bawah ini
bukan regresi dari patch itu — ini lapisan lama yang baru terlihat ketika alur
controller + simulasi ditelusuri ujung ke ujung. Dua di antaranya blocker.

### BLOCKER — masuk mode simulasi tidak menolkan gerak fisik

`api_rov_sim()` hanya memanggil `force_stop()` pada arah SIM → REAL. Arah
sebaliknya justru yang berbahaya:

```
1. wahana nyata sedang thro:2 — firmware menahannya
2. operator mencentang Mode Simulasi
3. server berhenti meneruskan /move ke soket
4. thro:2 tetap tertahan di wahana, tanpa apa pun yang mencabutnya
```

Deadman tidak menolong. `/api/rov/move` di mode simulasi **tetap** memanggil
`record_move()` — memang disengaja sejak beta8, supaya perilaku watchdog ikut
teruji — jadi `rov_last_move_at` terus diperbarui dan watchdog menyimpulkan
browser masih sehat. Begitu operator melepas stick, snapshot-nya nol dan
watchdog juga tidak punya alasan bertindak. State perangkat lunak nol dan
tenang; wahana fisik masih berjalan.

Sekarang **kedua arah** transisi menolkan lebih dulu, dan urutannya penting:

```
force_stop() → record_move(0,0,0) → lepas pilot → baru rov_sim_mode = sim
```

Flag dipasang paling akhir. Kalau dibalik, `force_stop()` berjalan saat mode
sudah simulasi dan perintahnya tidak akan pernah menemukan jalan ke soket.
Klaim kendali ikut dilepas: mode berganti berarti aturan mainnya berganti.

### BLOCKER — STOP ikut disimulasikan

`api_rov_estop()` punya cabang yang langsung `return` sebelum menyentuh soket
kalau `rov_sim_mode` menyala. Digabung dengan bug di atas, hasilnya: wahana
yang sempat bergerak lalu modenya dipindah ke simulasi masih menahan perintah
terakhirnya — dan tombol merah besar di layar hanya menghentikan simulasinya.

Gerak normal boleh dialihkan ke simulasi; itu memang gunanya. STOP tidak. Ia
perintah keselamatan out-of-band dan tidak ikut mode. Sekarang `force_stop()`
selalu dijalankan selama ada worker, apa pun modenya. Tanpa worker: di
simulasi itu keadaan normal (`ok: true`), di mode nyata itu kegagalan yang
harus terlihat (409).

Ini melengkapi tiga pengecualian yang sudah ada di endpoint itu — tanpa pilot
lock, tanpa unlock, dan sekarang tanpa mode.

### Mode belajar sekarang benar-benar inert

Komentarnya berjanji "tidak ada satu pun input yang boleh diteruskan sebagai
perintah". Implementasinya tidak menepati:

- cabang `if (learnSlot) { ... return; }` keluar tanpa menyentuh `src.pad`,
  jadi nilai stick dari tick SEBELUM pemetaan dinyalakan tertinggal di sana
  dan terus terkirim;
- `src.keys` dan `src.touch` tidak lewat `pollGamepad()` sama sekali, jadi
  keyboard dan stick sentuh tetap menggerakkan wahana selagi tabel menunggu
  "tekan…".

Tiga lapis sekarang:

1. **Interlock di titik masuk.** Tombol "petakan" menolak kalau kendali ROV
   terbuka dan mode simulasi mati, dengan pesan yang menyebutkan jalan
   keluarnya. Petunjuk di UI sudah menyuruh menyalakan simulasi dulu — kode
   tidak boleh cuma menyarankan.
2. **`zeroSources()` saat masuk**, termasuk mengembalikan knob ke tengah.
3. **Penegakan tiap tick** di `tick()`, menolkan ketiga sumber. Ini yang
   menutup kasus kunci ROV dibuka dari dashboard SELAGI pemetaan berlangsung
   — penjagaan di titik masuk saja tidak melihat itu.

### Mode simulasi disinkronkan dari state awal

`/api/state` sudah membawa `rov.sim`, tapi `fetchInitialState()` tidak pernah
memasangnya ke UI. Halaman yang di-refresh — atau browser kedua yang baru
dibuka — selagi server berada di SIM menampilkan centang kosong dan tanpa
pita peringatan. Broadcast hanya menyusulkan PERUBAHAN, bukan keadaan yang
sudah berjalan. Operator membaca "mode nyata" padahal perintahnya tidak
sampai ke wahana sama sekali; ini kebalikan persis dari kesalahan yang pita
peringatan itu dibuat untuk mencegah.

`applySim()` dipanggil dari state awal dan dari heartbeat, dan dibuat
idempoten — pesan status hanya keluar saat nilainya berubah. Tanpa itu ia
akan mengumumkan "mode nyata" tiap beberapa detik sampai peringatan yang
sungguhan ikut tenggelam.

### `forgetPad()` membersihkan identitas perangkat

`padId`, `customMap`, `learnSlot`, dan `padAxisMap` tidak ikut dibuang saat
pad dicabut. Akibatnya "Simpan Profil" masih bisa menulis profil untuk pad
yang sudah tidak ada, dan penjaga `padId !== wanted` di `loadProfile()` masih
meloloskan respons pad lama karena `padId`-nya belum berubah. Menutup sisa
bug isolasi profil dari beta8.1a.

### Uji

50 uji (dari 31). Sembilan belas uji baru; **11 di antaranya gagal** kalau
dijalankan terhadap beta8.1a, jadi kelima temuan benar-benar terjaga.

`TestSimTransitionSafety`, `TestEstopIsNeverSimulated`, dan
`TestSimExposedInState` menjalankan endpoint sungguhan lewat Django test
client dengan worker palsu, jadi yang diuji perilaku, bukan teks. Ketiganya
di-skip kalau Django tidak terpasang, supaya uji sisi berkas tetap bisa
dijalankan di lingkungan telanjang.

Satu uji lama disesuaikan: `test_leaving_sim_zeroes_movement` sekarang
mengharapkan **dua** stop, karena masuk simulasi pun menolkan gerak.
Perubahan ekspektasi ini disengaja dan itulah inti patchnya.

---

## v.beta8.1c — STOP atomic + failure-aware (2026-08-24)

Patch ini **hanya** menyentuh tiga bug keselamatan yang ditemukan saat review
v.beta8.1b. Tidak ada perubahan GPS, mapping controller, layout UI, protokol,
kapabilitas tombol, atau perilaku LOCK/E-STOP latching. JS hanya diubah agar
kegagalan STOP/SIM dari backend tidak salah ditampilkan sebagai sukses.

### FIX 1 — STOP gagal tidak lagi memalsukan state nol

Sebelumnya `RovWorker.force_stop()` selalu mengubah cache dan
`state.rov_last_move` menjadi `0,0,0` walaupun satu atau lebih kiriman
`thro:0 / lift:0 / yaw:0` gagal. Akibatnya software bisa terlihat berhenti
padahal ROV mungkin masih menahan perintah terakhir.

Sekarang:

- ketiga sumbu STOP tetap dicoba;
- cache + state baru di-commit ke nol **hanya jika ketiganya sukses**;
- jika salah satu gagal, state gerak sebelumnya dipertahankan secara
  konservatif;
- `/api/rov/estop` mengembalikan HTTP 409 jika STOP fisik gagal;
- transisi REAL↔SIM dibatalkan jika worker ada tetapi STOP fisik gagal,
  sehingga server tidak berpindah ke SIM sambil meninggalkan wahana nyata
  mungkin masih bergerak.
- frontend memeriksa `response.ok` + `json.ok`, dan broadcast
  `rov_estop` membawa `ok/error`; jadi HTTP 409 tidak lagi tampil sebagai
  “STOP diterima ROV”.

### FIX 2 — STOP dan MOVE sekarang satu transaksi terhadap soket

Sebelumnya `send_move()` memakai `_move_lock`, tetapi `force_stop()` mengirim
nol di luar lock. MOVE dapat menyelip di tengah tiga command STOP.

Sekarang:

- MOVE dan STOP memakai `_move_lock` yang sama;
- state MOVE ditulis selagi lock masih dipegang, sehingga tidak bisa menimpa
  state nol setelah STOP;
- `_force_stop_active` dipasang **sebelum** STOP menunggu lock;
- MOVE baru maupun MOVE yang sedang menunggu lock ditolak selama STOP aktif;
- `_force_stop_gate` mencegah dua STOP paralel saling menghapus barrier.

Perilaku tetap sengaja **non-latching**: MOVE baru yang benar-benar datang
setelah transaksi STOP selesai masih mengikuti aturan v.beta8.1b. Patch ini
hanya menutup race yang overlap dengan STOP.

### FIX 3 — Deadman retry bila STOP gagal

Sebelumnya `_deadman_tripped=True` dipasang sebelum hasil STOP diketahui.
Kalau kiriman nol gagal, watchdog bisa berhenti mencoba, apalagi state sudah
terlanjur ditulis nol.

Sekarang:

- `_deadman_tripped` hanya dipasang setelah STOP sukses;
- STOP gagal mempertahankan state gerak sehingga alasan retry tetap ada;
- retry dibatasi `DEADMAN_STOP_RETRY_S = 0.5` detik supaya tidak membanjiri
  soket/log;
- event `rov_deadman` baru dibroadcast setelah STOP berhasil.

### Scope yang sengaja TIDAK diubah

- LOCK belum diubah menjadi "LOCK + immediate physical STOP".
- E-STOP belum dibuat latching/auto-lock.
- GPS tetap persis v.beta8.1b.
- Layout UI dan controller mapping tetap persis v.beta8.1b; JS hanya
  menampilkan kegagalan STOP/SIM dengan benar.
- Tidak ada perubahan format `controller_profiles.json`.

Dua butir pertama tetap dicatat sebagai opsi safety policy untuk trial ROV,
bukan bagian patch bug v.beta8.1c ini.

## v.beta8.1d — Hold gerak 10 Hz + UI HP (2026-08-25)

Patch ini hanya menyentuh jalur kendali web dan tampilannya.

- `detection/rov_worker.py`: selama salah satu sumbu gerak aktif, vektor
  lengkap diteruskan ke Titan pada setiap heartbeat browser (10 Hz). Vektor
  nol identik tetap dideduplikasi agar soket tidak dibanjiri saat diam.
- `MOVE_DEADMAN_S` tetap `1.5` detik. Transaksi STOP atomik beta8.1c tidak
  diubah.
- `static/detection/js/controls.js`: `pointerleave` tidak lagi melepas stick.
  Pelepasan hanya terjadi pada `pointerup`, `pointercancel`, atau
  `lostpointercapture`.
- Ditambahkan indikator `TX n Hz`, `TX menunggu`, `TX macet`, dan `TX gagal`
  berdasarkan respons nyata `/api/rov/move`.
- Tampilan HP: panel video memakai rasio 16:9 tanpa minimum tinggi desktop;
  judul, indikator TX, STOP, dan pembacaan vektor ditata ulang agar ringkas.
- Tes regresi baru: `tests/test_beta81d_hold_heartbeat.py`.

## v.beta8.1e — Hotfix pengulangan per sumbu (2026-08-25)

Beta8.1d salah mengulang vektor lengkap pada setiap heartbeat. Contohnya,
maju menghasilkan urutan `thro:2; lift:0; yaw:0;`. Pada perangkat nyata,
perintah nol setelah perintah aktif dapat membatalkan gerak sehingga maju,
mundur, naik, dan turun tidak bekerja.

Hotfix ini mengubah aturan menjadi per sumbu:

- sumbu aktif diulang 10 Hz, misalnya `thro:2; thro:2; ...`;
- sumbu lain yang nol tidak ikut dikirim;
- ketika sumbu aktif dilepas, nol dikirim tepat sekali pada sumbu tersebut;
- vektor diam identik tetap dideduplikasi;
- deadman server tetap 1,5 detik dan transaksi STOP tidak diubah.

Regresi ditambah untuk memastikan sumbu nol tidak menimpa sumbu aktif.

## v.beta8.1f — Enam arah mapping + kalibrasi Xbox (2026-08-25)

Versi ini mempertahankan hotfix gerak per-sumbu beta8.1e dan memperbaiki dua
celah di pemetaan gamepad:

- tabel pemetaan sekarang menampilkan enam arah eksplisit: Maju, Mundur,
  Naik, Turun, Putar kanan, dan Putar kiri;
- tombol digital/D-pad untuk Mundur, Turun, dan Putar kiri dibalik menjadi
  nilai sumbu negatif sebelum dikirim;
- backend profil menerima aksi `thro_neg`, `lift_neg`, dan `yaw_neg`;
- profil lama tetap kompatibel;
- gamepad ber-mapping standar (termasuk Xbox di Chrome/Edge) selalu memakai
  sumbu baku LX, LY, RX, RY dengan netral nol;
- gerakan stick yang pertama kali memicu event koneksi tidak lagi salah
  dianggap sebagai posisi netral/trigger dan membuang sumbu vertikal.

Untuk stick analog, satu sumbu tetap menghasilkan kedua arah. Enam baris
terpisah diperlukan agar controller yang memakai tombol digital terpisah bisa
memetakan arah positif dan negatif secara eksplisit.

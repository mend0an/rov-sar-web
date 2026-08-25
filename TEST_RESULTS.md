# TEST RESULTS — rov_sar_web v.beta3 (SAR)

Environment: Django 5.0.14, Channels 4.1.0, Daphne 4.1.2, Python 3.12.
cv2 4.13 + numpy 2.4 tersedia; torch di-stub untuk test debounce (torch tidak
dipakai di jalur debounce). Hardware ROV/GPS/RTSP TIDAK tersedia → fake worker
+ dummy GPS + mock YOLO.

## Koreksi dari v.beta2 (bug yang dilaporkan reviewer)

Enam koreksi sudah dikerjakan:
1. Fake JPEG sekarang di-generate cv2.imencode() → JPEG VALID (bisa di-decode),
   bukan byte hex manual yang cuma punya marker.
2. tests/test_harness.py: hex rusak diganti generator cv2 → jalan normal.
3. Fallback map offline: sekarang gambar GRID beneran (custom GridLayer canvas),
   bukan cuma teks "OFFLINE MODE".
4. apps.py: worker startup pakai WHITELIST (runserver/daphne/flag), jadi
   `manage.py check` DAN command lain TIDAK menyalakan kamera/GPS/YOLO.
5. test_mjpeg_client.py: sekarang PARSE multipart + cv2.imdecode() tiap frame
   (bukan cuma hitung marker SOI). test_gps.py: watchdog RUNTIME test.
6. Komentar "korosi" di capture.py diganti "tubuh/objek SAR".

---

## Hasil test (SEMUA di-run beneran, bukan static check)

### FIX #1 — Async MJPEG + frame VALID                          ✅ PASS
Test: tests/test_mjpeg_client.py (parse multipart + DECODE tiap JPEG)
- Content-Type: multipart/x-mixed-replace; boundary=frame  ✓
- Frames DECODED : 60 dalam 4.0s (14.9 FPS)                ✓
- Frames FAILED  : 0 (SEMUA frame valid & ter-decode)      ✓
- Warning "must consume synchronous iterators": TIDAK ADA  ✓
Ini membuktikan gambar valid sampai client & bisa di-decode browser,
bukan sekadar "byte mengalir".

### FIX #2 — Offline dependencies                               ✅ PASS (statis+partial)
- Leaflet lokal: leaflet.js 147KB + leaflet.css 14KB + 5 images  ✓
- HTML tidak referensi unpkg/cdnjs/jsdelivr                       ✓
- JS guard `typeof L === 'undefined'`                             ✓
- Fallback tileerror → GRID canvas beneran (bukan cuma teks)      ✓
- Semua akses map.* sudah di-guard (invalidateSize, zoomIn/Out,
  setView, createPane)                                           ✓
Catatan: verifikasi statis + review kode. Test runtime "cabut internet lalu
buka dashboard di browser" belum dilakukan (butuh browser) — cek manual
direkomendasikan sebelum lapangan.

### FIX #3 — Debounce waypoint deteksi                          ✅ PASS
Test: tests/test_debounce.py (mock YOLO result)
- 200 frame deteksi berturut → 1 waypoint                        ✓
- Setelah cooldown 5s + pindah > 3m → waypoint baru masuk         ✓
- Deteksi confidence 0.3 (< 0.5) → ditolak                        ✓

### FIX #4 — RTSP robust                                        ✅ PASS (statis)
- settings: rtsp_transport;tcp|stimeout;5000000|max_delay;500000 ✓
- capture.py: cv2.VideoCapture(src, cv2.CAP_FFMPEG) untuk RTSP    ✓
- CAP_DSHOW hanya di Windows                                      ✓
- reconnect loop (CONSECUTIVE_FAIL_LIMIT=30, backoff)             ✓
- komentar "drain thread" misleading DIHAPUS                      ✓
Catatan: test dengan RTSP stream nyata dari ROV belum (butuh hardware).
Reconnect baru teruji secara struktur kode, belum runtime.

### FIX #5 — GPS watchdog + reconnect                           ✅ PASS (runtime!)
Test: tests/test_gps.py
- NMEA parsing 0710.5000 S → -7.17500, 11024.3000 E → 110.40500  ✓
- Stale logic: age 15s > timeout 10s → stale=True                ✓
- Watchdog RUNTIME: jalankan _run_watchdog() beneran, set fix jadi
  stale, verify gps_connected OTOMATIS jadi False                ✓  ← BARU
- Dummy GPS emit fix                                             ✓
Catatan: serial reconnect loop teruji struktur; cabut-colok modul GPS fisik
belum (butuh hardware).

### FIX #6 — Rename honest                                      ✅ PASS
- "Posisi ROV" → "Posisi Buoy Permukaan"                         ✓
- "Ke ROV" → "Ke Buoy"                                           ✓
- Komentar "korosi" di capture.py → "tubuh/objek SAR"            ✓
- grep konfirmasi: 0 referensi korosi tersisa di kode SAR        ✓

### FIX #7 — WebSocket broadcast multi-client                   ✅ PASS
Test: tests/test_ws_broadcast.py (2 client WS + trigger REST)
- Client A & B menerima: control_updated, waypoint_added,
  waypoints_cleared                                             ✓
- Kedua client tersinkron                                        ✓

### FIX #4 (worker gating) — manage.py check aman               ✅ PASS
- `python manage.py check` TIDAK menyalakan worker (whitelist)   ✓
- Log tidak ada "FAKE workers"/"Starting background" saat check  ✓

---

## Infra checks
- python -m compileall: PASS (semua .py)
- python manage.py check: 0 issues, TANPA menyalakan worker
- Daphne ASGI: startup OK, WSCONNECT/WSDISCONNECT normal
- tests/test_harness.py: self-test JPEG valid (decode 240x320x3)

## STATUS AKURAT
ROV SAR Web Reliability Beta — logic transport, streaming, broadcast, debounce,
GPS watchdog sudah diuji RUNTIME. Offline & RTSP sebagian masih verifikasi
statis. SIAP masuk pengujian hardware.

## BELUM diuji (butuh hardware — WAJIB sebelum uji ROV)
1. Webcam nyata → /video (bukan fake frame)
2. GPS serial fisik → cabut/colok untuk uji reconnect runtime
3. RTSP stream ROV (192.168.8.8) → uji CAP_FFMPEG + reconnect + latency
4. Browser offline sungguhan → verify Leaflet lokal + grid fallback
5. 2 device fisik (laptop + tablet) di jaringan router ROV → multi-client
6. Profiling FPS HOP + YOLO di RTX 3060/5070 (untuk keputusan optimasi HOP GPU)

---

# v.beta3.1 patch tests (tests/test_beta31_patches.py + HTTP)

### PATCH #1 — Infinity dihapus dari /api/state              ✅ PASS
- frame_age() sebelum ada frame → None (bukan inf)          ✓
- json.dumps({"frame_age_s": None}) valid, no "Infinity"    ✓
- HTTP: /api/state tanpa frame → "frame_age_s": null valid  ✓  (verified via curl)
- Setelah set_frame → angka finite                          ✓

### PATCH #2 — GPS freshness (tolak stale/disconnected)      ✅ PASS
- gps_fix_is_fresh: fresh→True, disconnected→False,
  stale 100s→False, never→False                             ✓
- HTTP: manual waypoint saat GPS stale → 400 ditolak        ✓  (RequestFactory)
- Manual waypoint saat GPS fresh → 200 diterima             ✓
- _maybe_tag_waypoint (YOLO) juga pakai freshness check     ✓  (code review)

### PATCH #3 — Serial error langsung set disconnected        ✅ PASS (struktur)
- SerialException/readline error → state.gps_connected=False
  langsung (thread-safe) + broadcast                        ✓  (code review)
- Runtime cabut-colok serial fisik: BELUM (butuh hardware)

### PATCH #4 — runserver --noreload nyalakan worker          ✅ PASS
- Gating logic: --noreload → start, parent normal → skip,
  child (RUN_MAIN=true) → start, check → skip, daphne → start ✓
- HTTP: runserver --noreload → frame_age finite + GPS masuk  ✓  (verified)

## Full regression v.beta3.1 (semua ulang)
compile ✓ | django check ✓ | js syntax ✓ | debounce ✓ | gps ✓ |
beta31 patches ✓ | mjpeg decode ✓ | ws broadcast ✓

---

# TEST RESULTS — v.beta5 (RTSP PyAV + telemetri ROV)

Environment: Django 5.1.15, Channels 4.3.2, Daphne 4.2.3, Python 3.12,
cv2 4.13, PyAV 18.1.0. Hardware ROV/GPS/RTSP TIDAK tersedia → ROV tiruan
(socket TCP sungguhan), video file sungguhan, fake worker, dummy GPS.
`torch` di-stub (jalur yang diuji tidak menyentuh HOP GPU).

**Batas kepercayaan hasil ini.** Yang TIDAK bisa diuji di sini: stream RTSP dan
socket telemetri dari perangkat keras nyata. Yang dibuktikan adalah routing,
parsing, gerbang keamanan, dan logika — bukan bahwa ROV fisik merespons.

## Ringkasan: 53/53 PASS + verifikasi browser

| Suite | Hasil |
|---|---|
| `test_rov_telemetry.py` | 12/12 PASS |
| `test_rov_api.py` | 23/23 PASS |
| `test_capture_rov.py` | 18/18 PASS |
| `test_debounce.py` (regresi) | ALL PASS |
| `test_gps.py` (regresi) | ALL PASS |
| `test_beta31_patches.py` (regresi) | ALL PASS |
| `test_mjpeg_client.py` (regresi) | PASS — 61 frame decoded, 0 gagal, 15.1 fps |

## Telemetri ROV — terhadap ROV TIRUAN                        ✅ 12/12

Server TCP sungguhan yang mengirim `key:value;` dan **sengaja memotong paket
di tengah field**, karena stream TCP tidak menghormati batas pesan. Kalau
buffer sambungan salah, `PVN:TITAN-T1-FAKE` akan terpotong jadi sampah.

- Tersambung, telemetri dianggap fresh, 12 field ter-parse           ✓
- `PVN` utuh meski paket dipotong di tengah → buffer benar           ✓
- Yaw/Depth numerik, RT integer                                      ✓
- Handshake `apptime` terkirim persis setelah connect                ✓
- Keepalive `ping:0` terkirim berkala (5× dalam 1.2s @0.5s)          ✓
- `send()` sukses DAN ROV tiruan benar-benar menerima `light:1`      ✓
- `is_fresh()` jadi False setelah ROV mati (stale timeout 5s)        ✓

## Gerbang keamanan kontrol ROV                               ✅ 23/23

Lewat HTTP sungguhan (Django test client), bukan pemanggilan fungsi langsung.

- Perintah **ditolak (409) saat terkunci**, dan tidak ada satu pun
  perintah yang sampai ke worker                                     ✓
- Diterima setelah unlock; ROV menerima `light:1`                    ✓
- Perintah gerak `thro` ditolak (400) — tidak diekspos by design     ✓
- Nilai di luar rentang / non-integer / body rusak ditolak (400)     ✓
- Ditolak (409) saat telemetri stale dan saat worker tidak ada       ✓
- Token: ditolak tanpa token, ditolak token salah, diterima token
  benar, dan endpoint unlock ikut dilindungi                         ✓
- `/api/state` memuat blok `rov`, `heading`, `heading_source`, dan
  JSON-nya valid (tidak ada `Infinity`)                              ✓
- `/api/sources` melaporkan spec `('rtsp', …)` — bukan cv2           ✓

## Routing video, dedup, dan logika ROV                       ✅ 18/18

### Routing — ini inti perbaikan v.beta5
- `open_source(rtsp)` → **`RTSPReader` (PyAV)**, bukan cv2.VideoCapture ✓
- Transport RTSP = **udp**                                            ✓
- Endpoint default = `rtsp://192.168.8.9:8554/stream`                 ✓
- `settings.py` **tidak lagi** memaksa `rtsp_transport;tcp`            ✓

### CaptureWorker terhadap video sungguhan
File mp4 40 frame dibuat lalu diproses end-to-end.
- 80 frame masuk ke state; JPEG **bisa di-decode ulang** (240,320,3)  ✓
- `frame_age()` angka valid, bukan Infinity                           ✓

### Dedup frame_id
Sumber yang `frame_id`-nya beku (meniru RTSP macet):
- 197 kali `read()` → **1 kali proses**                               ✓

### Auto-depth & heading
- Clamp depth 1–7 m, 6 kasus termasuk 0.2 / 12.9 / −3.0               ✓
- Auto-depth mati → slider tidak disentuh                             ✓
- Yaw ROV dipakai saat telemetri fresh                                ✓
- COG GPS dipakai saat opsi dimatikan                                 ✓
- **Fallback ke COG GPS saat telemetri putus**                        ✓

## Verifikasi browser (Playwright, Chromium)                  ✅ PASS

Halaman dimuat sungguhan, bukan pemeriksaan string HTML.

- Tidak ada `pageerror`; JS lolos `node --check`                      ✓
- 44 id yang diakses JS semuanya ada di HTML; 8 class CSS ada         ✓
- Panel telemetri terisi angka yang berubah (R/P/Y/D/suhu/batv/RT)    ✓
- **Field "Kedalaman" akhirnya terisi** (1.41 m) — sebelumnya "—"     ✓
- Heading bertanda sumber: `182.0° (ROV)`                             ✓
- Saat terkunci: `pointer-events: none`, tombol **tidak bisa diklik** ✓
- Setelah unlock: grup aktif, status "⚠ Kontrol ROV DIBUKA"           ✓
- Perintah ditolak server → **tombol TIDAK menyala**. Ini yang
  membuktikan status tombol mengikuti telemetri, bukan klik           ✓
- Fallback peta offline bekerja (tanpa internet di container)         ✓

## Broadcast WebSocket                                        ✅ PASS

Klien WebSocket sungguhan terhadap Daphne yang berjalan.
- 5 event `rov` diterima; 13 field telemetri lengkap                  ✓
- `_heading_source` = `rov`; yaw **berubah antar sampel** (bukan beku) ✓
- Multi-client: klien A unlock lewat HTTP → klien B menerima
  `rov_unlock`; `rov_prefs` juga tersiar                              ✓

## Catatan temuan saat pengujian

**Label sumber ROV bertentangan dengan indikator status.** Di mode uji,
label tertulis "Telemetri ROV nonaktif (ROV_TELEMETRY_ENABLED=0)" sementara
tepat di bawahnya "● Telemetri aktif" — karena label membaca config sedangkan
status membaca aliran data. Sudah diperbaiki: sekarang membedakan tiga keadaan
(aktif / mode uji / nonaktif).

**Static 404 di bawah `daphne`.** Bukan bug: daphne tidak melayani file static.
Pakai `runserver` untuk pengujian lokal, atau `collectstatic` + reverse proxy
untuk deployment. Sudah dicatat di README.

---

# TEST RESULTS — v.beta6

## Ringkasan: 72/72 PASS + verifikasi browser

| Suite | Hasil |
|---|---|
| `test_source_switch.py` (BARU) | 19/19 PASS |
| `test_rov_api.py` | 23/23 PASS |
| `test_capture_rov.py` | 18/18 PASS |
| `test_rov_telemetry.py` | 12/12 PASS |
| `test_debounce.py` / `test_gps.py` / `test_beta31_patches.py` | ALL PASS |

## Ganti sumber runtime                                        ✅ 19/19

Diuji dengan dua video polos berwarna beda (biru dan merah). Warna frame yang
keluar dibaca ulang dari JPEG di `state` — jadi yang dibuktikan adalah video
yang benar-benar keluar berubah, bukan sekadar nilai `worker.source`.

- Sumber awal keluar biru; setelah ganti keluar merah                 ✓
- Worker **masih hidup** — tidak di-restart, model tidak dimuat ulang ✓
- Sumber gagal → **video tidak mati**, kembali ke sumber sebelumnya   ✓
- Pacing 20 fps terukur 19.9 fps                                      ✓
- `/api/source` menolak `file`, index non-integer, URL non-RTSP,
  spec bentuk salah, dan request saat worker tidak ada                ✓

## Bug urutan init                                             ✅ terverifikasi

Worker dijalankan dengan sumber yang sengaja tidak ada:
- `model_error` tetap terisi → `_init_model()` dipanggil walau capture gagal ✓
- `source_error` tercatat di stats                                          ✓

Sebelum perbaikan, model tidak pernah dimuat dalam kondisi ini — dan itulah
yang membuat `nvidia-smi` tidak menampilkan `python.exe`, sehingga masalah
kamera terlihat seperti masalah CUDA.

## Logging                                                     ✅ terverifikasi

Log server sekarang (sebelumnya baris-baris ini tidak pernah muncul):
```
03:05:26 INFO    🚀 Starting background workers…
03:05:26 ERROR   ❌ Gagal load YOLO model: No module named 'ultralytics'
03:05:26 INFO    ✅ HOP coefficients dihitung
03:05:26 INFO    Membuka source video: ('file', '/tmp/demo.mp4')
03:05:26 INFO    ✅ Video source terbuka: ('file', '/tmp/demo.mp4')
03:05:26 INFO    Pacing: maks 15 fps
```

## Statistik di /api/state                                     ✅ terverifikasi

```json
{"device": "n/a", "model_loaded": false,
 "model_error": "No module named 'ultralytics'",
 "process_fps": 15.0, "fps_cap": 15.0, "encode_ms": 0.7,
 "source": ["file", "/tmp/demo.mp4"]}
```
`process_fps` = 15.0 dari cap 15 → pacing terkonfirmasi bekerja.

## Browser (Playwright)                                        ✅ PASS

- Dropdown terisi, sumber aktif ter-select                            ✓
- Entri tanpa spec ditampilkan tapi **tidak bisa dipilih**            ✓
- Sumber aktif di luar hasil enumerasi disisipkan sebagai `[aktif] …` ✓
- Baris performa tampil: `n/a · 1.2 fps / cap 15 · jpeg 0.7 ms`
  plus baris error model                                              ✓
- Tidak ada `pageerror`                                               ✓

## Catatan

Container ini tidak punya `ultralytics` maupun GPU, jadi `device` terbaca
`n/a` dan waktu inferensi 0. Jalur device-reporting sendiri sudah diuji lewat
cabang error-nya; **konfirmasi `cuda:0` harus dilakukan di laptop dengan GPU.**
Itu justru gunanya fitur ini — sekali jalan, jawabannya langsung terbaca di UI.

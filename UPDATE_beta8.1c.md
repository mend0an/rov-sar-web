# Update v.beta8.1c — Control Safety Atomicity

Basis: **rov_sar_web_v_beta8.1b**  
Tanggal patch: **24 Agustus 2026**

## Tujuan

Patch minimal untuk tiga bug pada jalur penghentian gerak ROV. Tidak mengubah
fitur lain.

## File produksi yang berubah

1. `detection/rov_worker.py`
   - STOP hanya commit state nol bila semua axis berhasil dinolkan.
   - MOVE/STOP diserialisasi dan STOP diberi barrier terhadap MOVE overlap.
   - deadman retry setiap 0,5 s bila STOP gagal.

2. `detection/views.py`
   - E-STOP gagal tidak lagi menulis state nol palsu dan sekarang return 409.
   - pergantian REAL↔SIM dibatalkan bila STOP fisik gagal.
   - broadcast `rov_estop` membawa status gagal/sukses secara eksplisit.

3. `static/detection/js/controls.js`
   - tombol E-STOP memeriksa HTTP/JSON result, bukan menganggap setiap `fetch()`
     sebagai sukses.
   - kegagalan transisi SIM mengembalikan checkbox ke state server dan
     menampilkan pesan gagal.

4. `static/detection/js/dashboard.js`
   - event WebSocket `rov_estop` tidak lagi selalu menampilkan STOP sukses;
     `payload.ok=false` ditampilkan sebagai kegagalan.

## File test/dokumentasi

- `tests/test_beta81c_atomic_stop.py` — 6 regression test tanpa Django/hardware.
- `tests/test_beta81_controller.py` — tambah 2 test endpoint failure
  (auto-skip bila Django tidak tersedia).
- `CHANGELOG.md`
- `TEST_RESULTS.md`
- `UPDATE_beta8.1c.md`

## Invariant setelah patch

```text
STOP sukses
→ thro:0 + lift:0 + yaw:0 semua berhasil
→ baru cache/state software = 0

STOP gagal
→ state gerak lama dipertahankan
→ deadman tetap punya alasan retry
→ mode SIM tidak berubah bila transisi membutuhkan STOP yang gagal

MOVE overlap dengan STOP
→ tidak boleh menyelip di tengah transaksi STOP
→ request MOVE yang overlap ditolak / STOP menjadi transaksi terakhir
```

## Yang belum divalidasi

Patch ini belum membuktikan respons firmware Geneinno Titan T1 terhadap
`thro:0/lift:0/yaw:0` pada hardware nyata. Setelah patch lolos software test,
trial ROV tetap diperlukan dalam kondisi terkendali untuk memverifikasi:

- command nol benar-benar menetralkan thruster;
- telemetri `mtL/mtR/mt1..mt4` kembali ke sekitar nilai netral;
- deadman dan E-STOP bekerja pada gangguan jaringan nyata.

## Sengaja tidak diubah

GPS, controller mapping, CSS/layout dashboard, LOCK behavior, E-STOP latching,
protokol telemetri, dan kapabilitas command. Perubahan JS hanya pada pelaporan
status kegagalan STOP/SIM; tidak mengubah mapping atau layout.

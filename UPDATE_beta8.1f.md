# UPDATE beta8.1f — Mapping Enam Arah dan Kalibrasi Xbox

Jangan gunakan beta8.1d untuk trial gerak. Gunakan beta8.1f, yang sudah
mencakup hotfix pengiriman per-sumbu beta8.1e.

## Perbaikan

- Menambahkan Maju, Mundur, Naik, Turun, Putar kanan, dan Putar kiri ke tabel
  Pemetaan Kustom Controller.
- Tombol digital/D-pad dapat dipetakan terpisah untuk keenam arah.
- Stick analog tetap dua arah; mendorong sumbu ke arah berlawanan otomatis
  menghasilkan nilai negatif.
- Kalibrasi gamepad standar/Xbox tidak lagi mengambil posisi stick yang sedang
  digerakkan sebagai titik netral pada saat controller pertama terdeteksi.
- Maju/mundur hanya mengulang `thro`, naik/turun hanya mengulang `lift`, dan
  putar hanya mengulang `yaw` pada 10 Hz. Sumbu nol lain tidak ikut dikirim.
- Deadman server tetap 1,5 detik dan STOP atomik tetap dipertahankan.

## Cara memperbarui

1. Hentikan server lama.
2. Ekstrak beta8.1f ke folder proyek. Pertahankan `config.bat` lokal Anda.
3. Jalankan dari Anaconda Prompt yang benar:

   ```bat
   conda activate yolo_sar
   cd /d C:\RISET\Human_Body_Detection\rov_sar_web
   jalan.bat
   ```

4. Lakukan hard refresh (`Ctrl+F5`). Di HP, tutup tab lama lalu buka lagi.
5. Tekan **Kembali ke Bawaan**, lalu uji stick Xbox. Jika ingin tombol/D-pad
   kustom, petakan keenam arah yang diperlukan dan tekan **Simpan Profil**.

## Trial aman

Nyalakan mode simulasi saat memetakan. Untuk trial fisik, amankan wahana,
kosongkan area baling-baling, gunakan daya rendah, dan uji satu sumbu per
waktu. Ketika stick ditahan, indikator normal adalah sekitar `TX 8–10 Hz`.

Hasil otomatis: **74 test, 58 PASS, 16 SKIP** karena Django tidak tersedia di
environment review. Sintaks JavaScript dan kompilasi Python lulus. Konfirmasi
perangkat keras tetap diperlukan.

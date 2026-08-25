@echo off
REM ===========================================================================
REM  CONTOH KONFIGURASI
REM
REM  Salin file ini menjadi config.bat, lalu sesuaikan nilainya untuk laptop
REM  operator. config.bat tidak masuk Git agar path lokal dan token tidak bocor.
REM ===========================================================================

REM --- Nama conda environment -----------------------------------------------
set CONDA_ENV=yolo_sar

REM --- Path model YOLO ------------------------------------------------------
set MODEL_PATH=C:\path\to\best.pt

REM --- Video untuk trial deteksi --------------------------------------------
set VIDEO_UJI=C:\path\to\underwater.mp4

REM --- Index kamera / OBS Virtual Camera ------------------------------------
set KAMERA_INDEX=0

REM --- Port GPS -------------------------------------------------------------
REM  Gunakan AUTO untuk deteksi otomatis USB GPS (BU-353N5 dll), DUMMY untuk simulasi,
REM  atau COM4 dan seterusnya untuk port spesifik.
set GPS_PORT=AUTO
set GPS_BAUD=4800

REM --- ROV ------------------------------------------------------------------
set ROV_RTSP=rtsp://192.168.8.9:8554/stream
set ROV_IP=192.168.8.9

REM  Isi dengan token acak yang kuat sebelum dipakai di jaringan lapangan.
set ROV_TOKEN=

REM --- Performa -------------------------------------------------------------
set FPS_CAP=30

REM --- Port web -------------------------------------------------------------
set PORT=8000


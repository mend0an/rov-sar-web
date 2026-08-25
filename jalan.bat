@echo off
setlocal EnableDelayedExpansion
title ROV SAR Detection - Web

REM ===========================================================================
REM  ROV SAR Detection - launcher
REM
REM  Edit config.bat dulu, lalu jalankan file ini dengan klik dua kali.
REM ===========================================================================

cd /d "%~dp0"

if not exist "manage.py" (
    echo.
    echo  [X] manage.py tidak ketemu.
    echo      File jalan.bat harus berada di folder rov_sar_web.
    echo.
    pause
    exit /b 1
)

if not exist "config.bat" (
    echo.
    echo  [X] config.bat tidak ketemu di folder ini.
    echo.
    pause
    exit /b 1
)

call config.bat

:MENU
cls
echo ===========================================================================
echo   ROV SAR Detection - Web Edition
echo ===========================================================================
echo.
echo   [1]  Uji tanpa hardware        (mode FAKE - cek Django sehat)
echo   [2]  Trial deteksi dari VIDEO  (%VIDEO_UJI%)
echo   [3]  Trial deteksi dari KAMERA (index %KAMERA_INDEX% / OBS)
echo   [4]  Operasi penuh dengan ROV  (RTSP + telemetri)
echo.
echo   [5]  Cek environment           (GPU, model, paket)
echo   [6]  Lihat daftar kamera       (cari index OBS)
echo   [7]  Uji koneksi ROV           (RTSP dan telemetri saja)
echo   [8]  Buka firewall port %PORT%    (perlu klik kanan Run as Administrator)
echo.
echo   [0]  Keluar
echo.
echo ---------------------------------------------------------------------------
echo   Setelah server jalan, buka:  http://localhost:%PORT%
echo   Dari HP (satu WiFi)        :  http://IP-LAPTOP:%PORT%
echo   Tekan CTRL+C di jendela ini untuk menghentikan server.
echo ---------------------------------------------------------------------------
echo.

set PILIH=
set /p PILIH="  Pilih menu: "

if "%PILIH%"=="1" goto FAKE
if "%PILIH%"=="2" goto VIDEO
if "%PILIH%"=="3" goto KAMERA
if "%PILIH%"=="4" goto ROV
if "%PILIH%"=="5" goto CEK
if "%PILIH%"=="6" goto LISTCAM
if "%PILIH%"=="7" goto UJIROV
if "%PILIH%"=="8" goto FIREWALL
if "%PILIH%"=="0" exit /b 0
goto MENU


REM --- Aktifkan conda --------------------------------------------------------
REM  Set OK=0 kalau gagal. Pemanggil WAJIB memeriksa OK lalu goto MENU -
REM  `exit /b 1` di sini cuma keluar dari subroutine, bukan dari script,
REM  jadi tanpa pemeriksaan itu script akan lanjut dan errornya membingungkan.
:AKTIFKAN
set OK=1
call conda activate %CONDA_ENV% 2>nul

REM  Verifikasi sungguhan: conda activate tidak selalu memberi errorlevel yang
REM  bisa dipercaya, jadi yang diperiksa adalah apakah paketnya benar-benar
REM  bisa di-import dari environment yang aktif sekarang.
python -c "import django, cv2" 2>nul
if errorlevel 1 (
    set OK=0
    echo.
    echo  [X] Environment "%CONDA_ENV%" tidak aktif atau paketnya belum lengkap.
    echo.
    echo      Coba:
    echo        1. Jalankan jalan.bat dari Anaconda Prompt, atau
    echo        2. Betulkan CONDA_ENV di config.bat, atau
    echo        3. conda activate %CONDA_ENV%
    echo           pip install "Django^>=5.0,^<5.2" channels daphne
    echo.
    pause
)
exit /b 0


REM --- Bersihkan env var dari sesi sebelumnya --------------------------------
REM  Ini penting: env var yang tersisa dari menu sebelumnya akan MENANG
REM  melawan settings.py, dan itu bikin bingung ("kok masih pakai webcam?").
:BERSIH
set ROV_FAKE_WORKERS=
set ROV_RTSP_URL=
set ROV_MODEL_PATH=
set ROV_GPS_PORT=
set ROV_GPS_BAUD=
set ROV_TELEMETRY_ENABLED=
set ROV_HOST=
set ROV_CONTROL_TOKEN=
set ROV_PROCESS_FPS=
exit /b 0


REM --- Cek model ada atau tidak ----------------------------------------------
:CEKMODEL
if not exist "%MODEL_PATH%" (
    echo.
    echo  [!] Model tidak ketemu di:
    echo      %MODEL_PATH%
    echo.
    echo      Video akan tetap jalan, tapi TIDAK ADA DETEKSI.
    echo      Betulkan MODEL_PATH di config.bat.
    echo.
    pause
)
exit /b 0


REM --- [1] Mode FAKE ---------------------------------------------------------
:FAKE
cls
call :AKTIFKAN
if "%OK%"=="0" goto MENU
call :BERSIH
echo.
echo  Mode uji tanpa hardware. Yang harus terlihat di browser:
echo    - gambar "FAKE" dengan angka yang berubah
echo    - titik di peta bergerak sendiri
echo    - panel telemetri ROV terisi angka
echo.
echo  Kalau ini jalan, Django dan WebSocket-nya sehat.
echo.
set ROV_FAKE_WORKERS=1
python manage.py runserver 0.0.0.0:%PORT% --noreload
echo.
pause
goto MENU


REM --- [2] Trial dari file video ---------------------------------------------
:VIDEO
cls
call :AKTIFKAN
if "%OK%"=="0" goto MENU
call :BERSIH
if not exist "%VIDEO_UJI%" (
    echo.
    echo  [X] Video tidak ketemu di:
    echo      %VIDEO_UJI%
    echo.
    echo      Betulkan VIDEO_UJI di config.bat.
    echo.
    pause
    goto MENU
)
call :CEKMODEL
echo.
echo  Trial deteksi dari file video.
echo.
echo  CATATAN: model ..._hopv2 dilatih di atas data yang SUDAH di-HOP.
echo  Biarkan checkbox "Aktifkan HOP" tetap tercentang, dan atur slider
echo  Depth manual (mulai 2-3 m). Kalau HOP dimatikan, deteksi akan jelek
echo  bukan karena modelnya buruk, tapi karena input tidak cocok dengan
echo  distribusi training.
echo.
set ROV_RTSP_URL=%VIDEO_UJI%
set ROV_MODEL_PATH=%MODEL_PATH%
set ROV_GPS_PORT=%GPS_PORT%
set ROV_GPS_BAUD=%GPS_BAUD%
set ROV_PROCESS_FPS=%FPS_CAP%
python manage.py runserver 0.0.0.0:%PORT% --noreload
echo.
pause
goto MENU


REM --- [3] Trial dari kamera / OBS -------------------------------------------
:KAMERA
cls
call :AKTIFKAN
if "%OK%"=="0" goto MENU
call :BERSIH
call :CEKMODEL
echo.
echo  Trial deteksi dari kamera index %KAMERA_INDEX%.
echo  Kalau ini OBS Virtual Camera, nyalakan dulu "Start Virtual Camera" di OBS.
echo.
echo  Sumber bisa diganti dari dropdown di halaman web tanpa restart.
echo.
set ROV_RTSP_URL=%KAMERA_INDEX%
set ROV_MODEL_PATH=%MODEL_PATH%
set ROV_GPS_PORT=%GPS_PORT%
set ROV_GPS_BAUD=%GPS_BAUD%
set ROV_PROCESS_FPS=%FPS_CAP%
python manage.py runserver 0.0.0.0:%PORT% --noreload
echo.
pause
goto MENU


REM --- [4] Operasi penuh dengan ROV ------------------------------------------
:ROV
cls
call :AKTIFKAN
if "%OK%"=="0" goto MENU
call :BERSIH
call :CEKMODEL
echo.
echo  Operasi penuh: RTSP + telemetri ROV.
echo.
echo  Pastikan laptop sudah tersambung ke WiFi ROV.
echo  ROV butuh waktu boot lama - pada uji tercatat sekitar 146 detik.
echo  Sabar sebelum menyimpulkan gagal.
echo.
if "%ROV_TOKEN%"=="" (
    echo  [!] ROV_TOKEN kosong. Kontrol ROV tidak dilindungi token.
    echo      Siapa pun di WiFi ROV bisa menyalakan lampu / depth hold.
    echo      Aman untuk kolam tertutup, TIDAK untuk lapangan terbuka.
    echo.
)
set ROV_RTSP_URL=%ROV_RTSP%
set ROV_MODEL_PATH=%MODEL_PATH%
set ROV_GPS_PORT=%GPS_PORT%
set ROV_GPS_BAUD=%GPS_BAUD%
set ROV_TELEMETRY_ENABLED=1
set ROV_HOST=%ROV_IP%
set ROV_CONTROL_TOKEN=%ROV_TOKEN%
set ROV_PROCESS_FPS=%FPS_CAP%
python manage.py runserver 0.0.0.0:%PORT% --noreload
echo.
pause
goto MENU


REM --- [5] Cek environment ---------------------------------------------------
:CEK
cls
call :AKTIFKAN
if "%OK%"=="0" goto MENU
echo.
echo  === Paket ===
python -c "import django, channels, daphne, cv2, torch; print(' Django    :', django.get_version()); print(' OpenCV    :', cv2.__version__); print(' PyTorch   :', torch.__version__)"
python -c "import av; print(' PyAV      :', av.__version__)" 2>nul || echo  PyAV      : TIDAK ADA (RTSP ROV tidak akan jalan)
python -c "import ultralytics; print(' Ultralytics:', ultralytics.__version__)" 2>nul || echo  Ultralytics: TIDAK ADA (deteksi tidak akan jalan)
echo.
echo  === GPU ===
python -c "import torch; ok=torch.cuda.is_available(); print(' CUDA      :', ok); print(' GPU       :', torch.cuda.get_device_name(0) if ok else '-')"
echo.
echo  === Model ===
if exist "%MODEL_PATH%" (
    echo  Model     : ADA
    echo  Path      : %MODEL_PATH%
    echo.
    echo  Menguji inferensi sungguhan di GPU, tunggu sebentar...
    python -c "from ultralytics import YOLO; import numpy as np, torch; m=YOLO(r'%MODEL_PATH%'); m(np.zeros((640,640,3),dtype=np.uint8), verbose=False); print(' Device    :', next(m.model.parameters()).device); print(' VRAM      : %%.0f MB' %% (torch.cuda.memory_allocated()/1e6))"
) else (
    echo  Model     : TIDAK KETEMU
    echo  Path      : %MODEL_PATH%
)
echo.
pause
goto MENU


REM --- [6] Daftar kamera -----------------------------------------------------
:LISTCAM
cls
call :AKTIFKAN
if "%OK%"=="0" goto MENU
echo.
echo  Mendeteksi sumber video yang tersedia...
echo  Angka di kolom kanan adalah index yang dipakai KAMERA_INDEX di config.bat.
echo.
python -c "from detection import rov_camera; [print(' ', l, '->', s) for l, s in rov_camera.list_sources()]"
echo.
pause
goto MENU


REM --- [7] Uji koneksi ROV ---------------------------------------------------
:UJIROV
cls
call :AKTIFKAN
if "%OK%"=="0" goto MENU
echo.
echo  Uji ini menjalankan modul secara mandiri, TANPA Django.
echo  Gunanya memisahkan "hardware bermasalah" dari "aplikasi bermasalah".
echo.
echo  [a] Uji RTSP      (video)
echo  [b] Uji telemetri (TCP 6666)
echo.
set SUB=
set /p SUB="  Pilih: "
if /i "%SUB%"=="a" (
    echo.
    echo  Ctrl+C untuk berhenti.
    echo.
    python -m detection.rov_camera %ROV_RTSP%
)
if /i "%SUB%"=="b" (
    echo.
    echo  ROV butuh waktu boot sekitar 146 detik. Ctrl+C untuk berhenti.
    echo.
    python -m detection.rov_telemetry %ROV_IP%
)
echo.
pause
goto MENU


REM --- [8] Firewall ----------------------------------------------------------
:FIREWALL
cls
echo.
net session >nul 2>&1
if errorlevel 1 (
    echo  [X] Perlu hak Administrator.
    echo.
    echo      Tutup jendela ini, klik kanan jalan.bat,
    echo      pilih "Run as administrator", lalu ulangi menu [8].
    echo.
    pause
    goto MENU
)
echo  Membuka port %PORT% supaya bisa diakses dari HP...
netsh advfirewall firewall delete rule name="ROV SAR Web %PORT%" >nul 2>&1
netsh advfirewall firewall add rule name="ROV SAR Web %PORT%" dir=in action=allow protocol=TCP localport=%PORT%
echo.
echo  Selesai. IP laptop ini:
ipconfig | findstr /C:"IPv4"
echo.
echo  Buka dari HP: http://IP-DI-ATAS:%PORT%
echo.
pause
goto MENU

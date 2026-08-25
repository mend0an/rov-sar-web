"""
Test client MJPEG — baca stream, PARSE multipart, DECODE tiap JPEG.

Beda dengan versi lama yang cuma hitung marker \xff\xd8\xff:
versi ini benar-benar mem-parse boundary multipart, ekstrak tiap part JPEG,
lalu cv2.imdecode() untuk MEMBUKTIKAN frame valid (bukan cuma byte mengalir).
"""
import sys
import time
import urllib.request

import numpy as np
import cv2

url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8766/video"
duration = float(sys.argv[2]) if len(sys.argv) > 2 else 4.0

req = urllib.request.urlopen(url, timeout=10)
ctype = req.headers.get("Content-Type", "")
print(f"Connected. Content-Type: {ctype}")

# Ekstrak boundary dari header
boundary = None
if "boundary=" in ctype:
    boundary = ctype.split("boundary=")[1].strip()
    boundary_bytes = ("--" + boundary).encode()
    print(f"Boundary: {boundary}")

start = time.time()
total_bytes = 0
buffer = b""
decoded_ok = 0
decoded_fail = 0

while time.time() - start < duration:
    chunk = req.read(4096)
    if not chunk:
        break
    total_bytes += len(chunk)
    buffer += chunk

    # Parse multipart: cari boundary, ekstrak part JPEG, decode
    while boundary_bytes in buffer:
        idx = buffer.index(boundary_bytes)
        # Butuh boundary berikutnya untuk tahu batas part
        next_idx = buffer.find(boundary_bytes, idx + len(boundary_bytes))
        if next_idx == -1:
            break  # part belum lengkap, tunggu chunk berikut
        part = buffer[idx:next_idx]
        buffer = buffer[next_idx:]

        # Ekstrak body JPEG (setelah header \r\n\r\n)
        sep = part.find(b"\r\n\r\n")
        if sep != -1:
            jpeg = part[sep + 4:].rstrip(b"\r\n")
            if jpeg.startswith(b"\xff\xd8"):
                img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                if img is not None:
                    decoded_ok += 1
                else:
                    decoded_fail += 1

elapsed = time.time() - start
print(f"Duration       : {elapsed:.1f}s")
print(f"Total bytes    : {total_bytes}")
print(f"Frames DECODED : {decoded_ok}")
print(f"Frames FAILED  : {decoded_fail}")
print(f"Effective FPS  : {decoded_ok / elapsed:.1f}")

# PASS syarat: minimal 5 frame BERHASIL di-decode, TIDAK ADA yang gagal decode
if decoded_ok >= 5 and decoded_fail == 0:
    print("RESULT: PASS (semua frame valid & ter-decode)")
    sys.exit(0)
else:
    print(f"RESULT: FAIL (decoded={decoded_ok}, failed={decoded_fail})")
    sys.exit(1)

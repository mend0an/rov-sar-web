"""
Uji RovTelemetry + RovWorker terhadap ROV TIRUAN yang bicara protokol asli.

Ini bukan mock: server di bawah benar-benar membuka socket TCP, mengirim
telemetri dalam format "key:value;" yang dipotong sembarangan di tengah field
(persis seperti stream TCP sungguhan), dan mencatat perintah yang masuk. Yang
diuji karena itu adalah parser, buffer sambungan, keepalive, dan jalur
perintah — bukan sekadar apakah fungsinya bisa dipanggil.

Jalankan: python3 tests/test_rov_telemetry.py
"""
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "rov_sar_web.settings")
os.environ.setdefault("ROV_AUTOSTART_WORKERS", "0")

import django  # noqa: E402
django.setup()

from detection import rov_telemetry  # noqa: E402


class FakeRov:
    """
    Server TCP yang meniru ROV Titan T1: kirim telemetri berkala, balas ping,
    dan catat semua perintah yang diterima.
    """

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.received = []
        self._running = True
        self._conn = None
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        conn, _ = self.sock.accept()
        self._conn = conn
        conn.settimeout(0.2)
        threading.Thread(target=self._send_loop, args=(conn,), daemon=True).start()
        buf = ""
        while self._running:
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk.decode("ascii", "replace")
            parts = buf.split(";")
            buf = parts.pop()
            for item in parts:
                if item:
                    self.received.append(item)

    def _send_loop(self, conn):
        t0 = time.time()
        n = 0
        while self._running:
            t = time.time() - t0
            msg = (
                f"R:{t:.2f};P:-1.50;Y:{(t * 10) % 360:.2f};D:{1.0 + t * 0.5:.2f};"
                f"to:29.3;ti:38.1;batv:16.05;RT:{int(t)};PVN:TITAN-T1-FAKE;"
                f"L:0;HD:0;HH:0;"
            )
            # Potong di tengah supaya buffer sambungan benar-benar diuji:
            # stream TCP tidak menghormati batas pesan.
            cut = len(msg) // 3 + (n % 5)
            try:
                conn.sendall(msg[:cut].encode())
                time.sleep(0.02)
                conn.sendall(msg[cut:].encode())
            except OSError:
                return
            n += 1
            time.sleep(0.2)

    def stop(self):
        self._running = False
        for s in (self._conn, self.sock):
            try:
                if s:
                    s.close()
            except OSError:
                pass


def main():
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))

    fake = FakeRov()
    print(f"ROV tiruan mendengarkan di 127.0.0.1:{fake.port}\n")

    rov = rov_telemetry.RovTelemetry(host="127.0.0.1", port=fake.port,
                                     ping_interval=0.5)
    try:
        print("── Koneksi & parsing ──")
        connected = rov.wait_connected(timeout=5)
        check("Tersambung ke ROV", connected)

        time.sleep(1.5)
        check("Telemetri dianggap fresh", rov.is_fresh())

        snap = rov.snapshot()
        check("Field ter-parse", len(snap) >= 10, f"{len(snap)} field")
        check("PVN utuh meski paket terpotong",
              snap.get("PVN") == "TITAN-T1-FAKE", repr(snap.get("PVN")))
        check("Yaw numerik", rov.yaw is not None, f"Y={rov.yaw}")
        check("Depth numerik", rov.depth is not None, f"D={rov.depth}")
        check("Runtime integer", isinstance(rov.runtime_s, int),
              f"RT={rov.runtime_s}")

        print("\n── Handshake & keepalive ──")
        time.sleep(1.2)
        joined = ";".join(fake.received)
        check("Handshake apptime terkirim", "apptime:" in joined)
        check("Ping keepalive terkirim", "ping:0" in joined,
              f"{joined.count('ping:0')}x")

        print("\n── Perintah ──")
        before = len(fake.received)
        ok = rov.set_light(True)
        time.sleep(0.4)
        check("send() melaporkan sukses", ok)
        check("ROV menerima light:1",
              "light:1" in ";".join(fake.received[before:]))

        print("\n── Deteksi putus ──")
        fake.stop()
        time.sleep(6.5)   # stale_timeout default 5 detik
        check("is_fresh() jadi False setelah ROV mati", not rov.is_fresh())
    finally:
        rov.close()
        fake.stop()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'='*52}\n{passed}/{len(results)} PASS")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

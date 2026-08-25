"""
rov_telemetry.py — Klien telemetri & kontrol Geneinno Titan T1.

Protokol hasil pembedahan PCAP (09 Agustus 2026):
    Transport : TCP ke 192.168.8.9:6666
    Format    : ASCII polos, "key:value;" beruntun tanpa pembatas paket
    Enkripsi  : TIDAK ADA
    Autentikasi: TIDAK ADA

Catatan penting soal koneksi:
    Tidak ada "perintah pembuka pintu". Aplikasi vendor hanya mencoba connect
    berulang-ulang (ditolak RST) sampai ROV selesai boot dan port 6666 terbuka
    sendiri — pada rekaman uji butuh ~146 detik sejak aplikasi dibuka. Karena
    itu retry loop di sini BUKAN penanganan error, melainkan mekanisme koneksi
    yang normal. Jangan diperpendek timeout-nya.

Parsing:
    TCP adalah stream, bukan pesan. Satu paket bisa memuat 40 field sekaligus
    atau memotong "batv:9.8" di tengah. Karena itu data ditumpuk di buffer dan
    hanya dipotong pada ';' — sisa tanpa ';' disimpan untuk paket berikutnya.

Dependensi: hanya pustaka standar. Tidak mengimpor PyQt agar bisa dipakai
ulang di Django maupun skrip CLI.
"""

import socket
import threading
import time

ROV_HOST = "192.168.8.9"
ROV_PORT = 6666

# Perintah yang terkonfirmasi dikirim aplikasi vendor.
# Rentang gerak -2..2 adalah nilai yang teramati; belum diuji di luar itu.
CMD_LIFT  = "lift"    # -2..2  naik / turun
CMD_THRO  = "thro"    # -2..2  maju / mundur
CMD_YAW   = "yaw"     # -2..2  putar kiri / kanan
CMD_LIGHT = "light"   # 0 / 1  lampu
CMD_HOLDD = "holdd"   # 0 / 1  depth hold
CMD_HOLDY = "holdy"   # 0 / 1  heading hold

# Field telemetri yang sudah teridentifikasi artinya.
FIELD_INFO = {
    "R":     ("Roll",              "derajat"),
    "P":     ("Pitch",             "derajat"),
    "Y":     ("Yaw / heading",     "derajat"),
    "D":     ("Kedalaman",         "m"),
    "PS":    ("Tekanan",           "hPa"),
    "ti":    ("Suhu internal",     "C"),
    "to":    ("Suhu air",          "C"),
    "hum":   ("Kelembapan",        "%"),
    "batv":  ("Tegangan baterai",  "V"),
    "batc":  ("Arus baterai",      "A"),
    "batt":  ("Suhu baterai",      "C"),
    "batl":  ("Level baterai",     "bar"),
    "sysC":  ("Arus sistem",       "A"),
    # KOREKSI (uji lapangan 09-08-2026): RT naik tepat 1.001 per detik pada
    # dua sesi terpisah -> ini penghitung waktu operasi, BUKAN latency.
    # Angka "43ms" di UI vendor dihitung aplikasi dari selisih ping/pong.
    "RT":    ("Waktu operasi",     "detik"),
    "L":     ("Lampu",             ""),
    "S":     ("Kecepatan/gear",    ""),
    "HD":    ("Depth hold aktif",  ""),
    "HH":    ("Heading hold aktif",""),
    "mtL":   ("PWM thruster kiri", "us"),
    "mtR":   ("PWM thruster kanan","us"),
    "mt1":   ("PWM thruster 1",    "us"),
    "mt2":   ("PWM thruster 2",    "us"),
    "mt3":   ("PWM thruster 3",    "us"),
    "mt4":   ("PWM thruster 4",    "us"),
    "err":   ("Bitmask error",     ""),
    "PVN":   ("Model / firmware",  ""),
    "MAC":   ("MAC address",       ""),
    "sonar": ("Sonar",             ""),
    "DVL_COOR_REALTIME": ("Posisi DVL (x,y,z)", "m"),
    "DVL_BI":            ("DVL beam instrument", ""),
    "DVL_BD":            ("DVL beam bumi", ""),
}

# Field yang layak dikonversi ke float untuk dipakai numerik.
_FLOAT_FIELDS = {
    "R", "P", "Y", "D", "PS", "ti", "to", "hum",
    "batv", "batc", "batt", "sysC",
}


class RovTelemetry:
    """
    Klien telemetri + kontrol. Thread internal menjaga koneksi, mengirim
    keepalive, dan memperbarui dict telemetri terbaru.

    Pemakaian:
        rov = RovTelemetry()
        rov.wait_connected(timeout=180)
        print(rov.yaw, rov.depth)
        rov.set_light(True)
        rov.move(thro=2)
        rov.close()
    """

    def __init__(self, host=ROV_HOST, port=ROV_PORT,
                 ping_interval=2.0, connect_retry=2.0, stale_timeout=5.0):
        self.host          = host
        self.port          = port
        self.ping_interval = ping_interval
        self.connect_retry = connect_retry
        self.stale_timeout = stale_timeout

        self.data       = {}      # field -> nilai mentah (string)
        self.updated_at = 0.0     # waktu telemetri terakhir masuk
        self.last_error = None
        self.n_fields   = 0       # total field diterima sejak start

        self._sock      = None
        self._buffer    = ""
        self._lock      = threading.Lock()
        self._send_lock = threading.Lock()
        self._running   = True
        self._connected = threading.Event()
        # Dipakai sebagai pengganti time.sleep() supaya close() langsung
        # membangunkan thread yang sedang menunggu, bukan menanti sleep habis.
        self._wake      = threading.Event()

        self._rx = threading.Thread(target=self._rx_worker,
                                    name="RovTelemetryRX", daemon=True)
        self._ka = threading.Thread(target=self._keepalive_worker,
                                    name="RovKeepalive", daemon=True)
        self._rx.start()
        self._ka.start()

    # ── status ──────────────────────────────────────────────────────────────
    def is_connected(self):
        return self._connected.is_set()

    def is_fresh(self):
        """True kalau telemetri masih mengalir (bukan sekadar socket terbuka)."""
        return (self._connected.is_set()
                and time.time() - self.updated_at < self.stale_timeout)

    def wait_connected(self, timeout=180):
        """
        Tunggu sampai tersambung. Default 180 detik karena ROV butuh waktu
        boot yang lama — pada uji tercatat ~146 detik.
        """
        return self._connected.wait(timeout=timeout)

    # ── pembacaan telemetri ─────────────────────────────────────────────────
    def get(self, key, default=None):
        with self._lock:
            return self.data.get(key, default)

    def get_float(self, key, default=None):
        v = self.get(key)
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def snapshot(self):
        """Salinan seluruh telemetri saat ini."""
        with self._lock:
            return dict(self.data)

    @property
    def roll(self):    return self.get_float("R")

    @property
    def pitch(self):   return self.get_float("P")

    @property
    def yaw(self):     return self.get_float("Y")

    @property
    def depth(self):   return self.get_float("D")

    @property
    def water_temp(self): return self.get_float("to")

    @property
    def battery_v(self):  return self.get_float("batv")

    @property
    def runtime_s(self):
        """Waktu operasi ROV dalam detik sejak menyala (bukan latency)."""
        try:
            return int(self.get("RT"))
        except (TypeError, ValueError):
            return None

    @property
    def internal_temp(self):
        """Suhu internal. Naik cepat kalau ROV dinyalakan di darat."""
        return self.get_float("ti")

    # ── pengiriman perintah ─────────────────────────────────────────────────
    def send(self, key, value):
        """Kirim satu perintah 'key:value;'. True kalau terkirim."""
        if not self._connected.is_set() or self._sock is None:
            return False
        msg = f"{key}:{value};".encode("ascii")
        try:
            with self._send_lock:
                self._sock.sendall(msg)
            return True
        except OSError as e:
            self.last_error = f"send: {e}"
            self._drop()
            return False

    def move(self, thro=None, lift=None, yaw=None):
        """
        Kirim perintah gerak. Nilai teramati -2..2; None berarti tidak dikirim.
        Perhatikan: ROV kemungkinan menahan perintah terakhir sampai diganti,
        jadi kirim 0 secara eksplisit untuk berhenti.
        """
        ok = True
        if thro is not None: ok &= self.send(CMD_THRO, int(thro))
        if lift is not None: ok &= self.send(CMD_LIFT, int(lift))
        if yaw  is not None: ok &= self.send(CMD_YAW,  int(yaw))
        return ok

    def stop(self):
        return self.move(thro=0, lift=0, yaw=0)

    def set_light(self, on):
        return self.send(CMD_LIGHT, 1 if on else 0)

    def set_depth_hold(self, on):
        return self.send(CMD_HOLDD, 1 if on else 0)

    def set_heading_hold(self, on):
        return self.send(CMD_HOLDY, 1 if on else 0)

    # ── siklus hidup ────────────────────────────────────────────────────────
    def close(self):
        self._running = False
        self._wake.set()          # bangunkan thread yang sedang menunggu
        self._drop()
        for t in (self._rx, self._ka):
            if t.is_alive():
                t.join(timeout=2.0)

    def _drop(self):
        self._connected.clear()
        s, self._sock = self._sock, None
        if s:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass

    # ── thread penerima ─────────────────────────────────────────────────────
    def _rx_worker(self):
        while self._running:
            if not self._connect():
                self._wake.wait(self.connect_retry)
                continue
            try:
                self._read_loop()
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
            finally:
                self._drop()
                self._buffer = ""
            if self._running:
                self._wake.wait(self.connect_retry)

    def _connect(self):
        try:
            s = socket.create_connection((self.host, self.port), timeout=3.0)
            s.settimeout(1.0)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as e:
            self.last_error = f"connect: {e}"
            return False

        self._sock = s
        self._connected.set()
        self.last_error = None

        # Aplikasi vendor mengirim ini persis setelah handshake.
        self.send("apptime", int(time.time()))
        self.send("apptimez", "7.0")
        return True

    def _read_loop(self):
        while self._running:
            try:
                chunk = self._sock.recv(8192)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("koneksi ditutup ROV")
            self._feed(chunk.decode("ascii", "replace"))

    def _feed(self, text):
        """
        Potong stream pada ';'. Sisa terakhir (belum lengkap) disimpan di
        buffer untuk digabung dengan chunk berikutnya.
        """
        self._buffer += text
        parts = self._buffer.split(";")
        self._buffer = parts.pop()      # potongan terakhir belum tentu utuh

        fresh = {}
        for item in parts:
            if ":" not in item:
                continue
            key, _, val = item.partition(":")
            key = key.strip()
            if key:
                fresh[key] = val.strip()

        if fresh:
            with self._lock:
                self.data.update(fresh)
                self.n_fields += len(fresh)
            self.updated_at = time.time()

    # ── thread keepalive ────────────────────────────────────────────────────
    def _keepalive_worker(self):
        """
        Aplikasi vendor mengirim 'ping:0;' tiap 2 detik dan ROV membalas
        'pong:0;'. Belum diuji apakah ROV memutus koneksi tanpa ping, jadi
        keepalive tetap dikirim untuk meniru perilaku vendor.
        """
        while self._running:
            if self._connected.is_set():
                self.send("ping", 0)
            self._wake.wait(self.ping_interval)


# ─────────────────────────────────────────────────────────────────────────────
#  Uji mandiri: python -m detection.rov_telemetry
#
#  Dipertahankan setelah porting supaya koneksi TCP 6666 bisa diuji tanpa
#  menyalakan Django. ROV butuh waktu boot lama — pada uji tercatat ~146 detik.
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    host = sys.argv[1] if len(sys.argv) > 1 else ROV_HOST
    print(f"Menyambung ke {host}:{ROV_PORT} ...")
    print("(ROV butuh waktu boot; pada uji tercatat ~146 detik)")

    rov = RovTelemetry(host=host)
    if not rov.wait_connected(timeout=180):
        print(f"GAGAL tersambung. Error terakhir: {rov.last_error}")
        raise SystemExit(1)

    print("Tersambung. Ctrl-C untuk keluar.\n")
    try:
        while True:
            time.sleep(1.0)
            if not rov.is_fresh():
                print(f"  (telemetri stale — {rov.last_error})")
                continue

            def f(x, n=2):
                return "--" if x is None else f"{x:.{n}f}"

            print(f"R {f(rov.roll):>7} | P {f(rov.pitch):>7} | "
                  f"Y {f(rov.yaw):>7} | D {f(rov.depth)} m | "
                  f"air {f(rov.water_temp,1)} C | dalam {f(rov.internal_temp,1)} C | "
                  f"{f(rov.battery_v,2)} V | t+{rov.runtime_s}s")

            # ROV didinginkan oleh air. Di darat suhu internal naik terus.
            it = rov.internal_temp
            if it is not None and it > 45:
                print(f"  !! SUHU INTERNAL {it:.1f} C - matikan kalau di darat")
    except KeyboardInterrupt:
        pass
    finally:
        print("\n--- Semua field yang pernah diterima ---")
        for k, v in sorted(rov.snapshot().items()):
            label, unit = FIELD_INFO.get(k, ("", ""))
            print(f"  {k:20s} = {v:<28s} {label} {unit}".rstrip())
        print(f"\nTotal field diterima: {rov.n_fields}")
        rov.close()

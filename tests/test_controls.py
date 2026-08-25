"""
Uji lapisan kendali: tabel kapabilitas, endpoint gerak, pilot lock, deadman.

Dijalankan tanpa ROV. Yang diuji adalah logika yang menentukan apakah sebuah
perintah BOLEH sampai ke soket — dan justru bagian itulah yang tidak boleh
diuji dengan wahana di air.

    python tests/test_controls.py
"""
import json
import os
import pathlib
import sys
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "rov_sar_web.settings")

import django  # noqa: E402

django.setup()

from django.test import Client  # noqa: E402

from detection import rov_caps  # noqa: E402
from detection.state import state  # noqa: E402


# ═════════════════════════════════════════════════════════════════════════
class TestCapabilities(unittest.TestCase):

    def test_verified_commands_enabled(self):
        allowed = rov_caps.allowed_commands()
        for key in ("thro", "lift", "yaw", "light", "holdd", "holdy"):
            self.assertIn(key, allowed, f"'{key}' seharusnya aktif")

    def test_unverified_commands_absent(self):
        """Yang belum dikonfirmasi PCAP tidak boleh punya jalur ke soket."""
        allowed = rov_caps.allowed_commands()
        for cap_id in ("gear", "tilt", "posture", "lateral"):
            cap = rov_caps.CAPABILITIES[cap_id]
            self.assertFalse(cap.usable, f"'{cap_id}' seharusnya mati")
            self.assertNotIn(cap.key, allowed)

    def test_enabled_without_key_is_not_usable(self):
        """enabled=True tanpa nama field adalah salah tulis, dan harus mati."""
        broken = rov_caps.Capability(
            id="x", label="X", kind="toggle", key=None, values=(0, 1),
            enabled=True,
        )
        self.assertFalse(broken.usable)

    def test_move_range_is_five_levels(self):
        self.assertEqual(rov_caps.MOVE_VALUES, (-2, -1, 0, 1, 2))

    def test_env_override_parses(self):
        parsed = rov_caps._parse_env_override("gear:S:0,1,2;tilt:CT:-1,0,1")
        self.assertEqual(parsed["gear"], ("S", (0, 1, 2)))
        self.assertEqual(parsed["tilt"], ("CT", (-1, 0, 1)))

    def test_env_override_rejects_malformed(self):
        parsed = rov_caps._parse_env_override("gear;tilt:CT;bogus:X:1")
        self.assertNotIn("gear", parsed)
        self.assertNotIn("tilt", parsed)
        self.assertNotIn("bogus", parsed)

    def test_caps_payload_hides_values_when_disabled(self):
        d = rov_caps.CAPABILITIES["gear"].to_dict()
        self.assertFalse(d["enabled"])
        self.assertEqual(d["values"], [])
        self.assertEqual(d["reason"], rov_caps.REASON_PENDING_PCAP)

    def test_lateral_reason_is_hardware_not_pcap(self):
        """Beda alasan = beda jalan keluar. UI menampilkannya berbeda."""
        self.assertEqual(
            rov_caps.CAPABILITIES["lateral"].reason,
            rov_caps.REASON_NO_HARDWARE,
        )


# ═════════════════════════════════════════════════════════════════════════
class FakeWorker:
    """Pengganti RovWorker — mencatat, tidak mengirim ke mana pun."""

    def __init__(self, fresh=True):
        self.fresh = fresh
        self.moves = []
        self.stops = []
        self.commands = []

    def send(self, key, value):
        if not self.fresh:
            return False
        self.commands.append((key, value))
        return True

    def send_move(self, thro, lift, yaw):
        if not self.fresh:
            return False
        self.moves.append((thro, lift, yaw))
        return True

    def force_stop(self, reason=""):
        self.stops.append(reason)
        return True


class RovApiBase(unittest.TestCase):

    def setUp(self):
        self.client = Client()
        self.worker = FakeWorker()
        state.rov_worker = self.worker
        state.rov_control_unlocked = True
        state.rov_pilot_id = None
        state.rov_pilot_at = 0.0
        state.rov_last_move = {"thro": 0, "lift": 0, "yaw": 0}
        state.rov_last_move_at = 0.0

    def tearDown(self):
        state.rov_worker = None
        state.rov_control_unlocked = False
        state.rov_pilot_id = None

    def move(self, thro=0, lift=0, yaw=0, client_id="tester"):
        return self.client.post(
            "/api/rov/move",
            data=json.dumps({"thro": thro, "lift": lift, "yaw": yaw,
                             "client_id": client_id}),
            content_type="application/json",
        )


class TestMoveEndpoint(RovApiBase):

    def test_accepts_valid_vector(self):
        r = self.move(thro=2, lift=-1, yaw=1)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.worker.moves[-1], (2, -1, 1))

    def test_rejects_out_of_range_instead_of_clamping(self):
        """Clamp diam-diam menyembunyikan bug kalibrasi klien."""
        r = self.move(thro=3)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.worker.moves, [])

    def test_rejects_non_integer(self):
        r = self.client.post(
            "/api/rov/move",
            data=json.dumps({"thro": "maju", "client_id": "t"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)

    def test_blocked_when_locked(self):
        state.rov_control_unlocked = False
        r = self.move(thro=1)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.worker.moves, [])

    def test_requires_client_id(self):
        r = self.move(thro=1, client_id="")
        self.assertEqual(r.status_code, 400)

    def test_fails_when_telemetry_stale(self):
        self.worker.fresh = False
        r = self.move(thro=1)
        self.assertEqual(r.status_code, 409)

    def test_missing_axis_defaults_to_zero(self):
        r = self.client.post(
            "/api/rov/move",
            data=json.dumps({"thro": 1, "client_id": "t"}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.worker.moves[-1], (1, 0, 0))


class TestPilotLock(RovApiBase):

    def test_first_client_becomes_pilot(self):
        self.assertEqual(self.move(thro=1, client_id="hp-a").status_code, 200)
        self.assertEqual(state.rov_pilot_id, "hp-a")

    def test_second_client_rejected_while_first_active(self):
        self.move(thro=1, client_id="hp-a")
        r = self.move(thro=-2, client_id="hp-b")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(json.loads(r.content)["pilot"], "hp-a")
        # Vektor klien kedua tidak boleh ikut terkirim
        self.assertEqual(self.worker.moves, [(1, 0, 0)])

    def test_same_client_may_continue(self):
        for _ in range(5):
            self.assertEqual(self.move(thro=1, client_id="hp-a").status_code, 200)

    def test_claim_expires_after_ttl(self):
        self.move(thro=1, client_id="hp-a")
        state.rov_pilot_at = time.time() - (state.PILOT_TTL_S + 0.5)
        r = self.move(thro=1, client_id="hp-b")
        self.assertEqual(r.status_code, 200,
                         "klien lain harus bisa ambil alih setelah pilot diam")

    def test_estop_releases_pilot(self):
        self.move(thro=2, client_id="hp-a")
        self.client.post("/api/rov/estop",
                         data=json.dumps({"client_id": "hp-b"}),
                         content_type="application/json")
        self.assertIsNone(state.rov_pilot_id)
        r = self.move(thro=1, client_id="hp-b")
        self.assertEqual(r.status_code, 200)


class TestEstop(RovApiBase):

    def test_works_even_when_locked(self):
        """Berhenti tidak pernah termasuk 'gerakan tak disengaja'."""
        state.rov_control_unlocked = False
        r = self.client.post("/api/rov/estop",
                             data=json.dumps({"client_id": "x"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.worker.stops), 1)

    def test_works_from_non_pilot(self):
        self.move(thro=2, client_id="hp-a")
        r = self.client.post("/api/rov/estop",
                             data=json.dumps({"client_id": "penolong"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)

    def test_tolerates_empty_body(self):
        r = self.client.post("/api/rov/estop", data="",
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)


class TestCommandEndpointUsesCaps(RovApiBase):

    def _cmd(self, key, value):
        return self.client.post(
            "/api/rov/command",
            data=json.dumps({"key": key, "value": value}),
            content_type="application/json",
        )

    def test_verified_toggle_accepted(self):
        self.assertEqual(self._cmd("light", 1).status_code, 200)

    def test_unverified_gear_rejected_with_explanation(self):
        r = self._cmd("S", 1)
        self.assertEqual(r.status_code, 400)

    def test_unknown_key_rejected(self):
        self.assertEqual(self._cmd("nonsense", 1).status_code, 400)

    def test_movement_axes_cannot_bypass_move_endpoint(self):
        """
        Regresi. Endpoint ini pernah menerima 'thro' setelah sumbu gerak masuk
        tabel kapabilitas — jalan pintas yang melewati pilot lock DAN
        pencatatan yang menjadi denyut deadman. Gerak lewat sini berarti
        watchdog tidak akan pernah memicu.
        """
        for axis in ("thro", "lift", "yaw"):
            r = self._cmd(axis, 2)
            self.assertEqual(r.status_code, 400, f"'{axis}' harus ditolak")
            self.assertEqual(self.worker.commands, [],
                             "tidak boleh ada yang sampai ke soket")
            self.assertEqual(state.rov_last_move,
                             {"thro": 0, "lift": 0, "yaw": 0})

    def test_out_of_range_value_rejected(self):
        self.assertEqual(self._cmd("light", 7).status_code, 400)


class TestCapsEndpoint(RovApiBase):

    def test_payload_shape(self):
        r = self.client.get("/api/rov/caps")
        self.assertEqual(r.status_code, 200)
        d = json.loads(r.content)
        self.assertIn("capabilities", d)
        ids = {c["id"] for c in d["capabilities"]}
        self.assertTrue({"thro", "gear", "tilt", "lateral"} <= ids)
        gear = next(c for c in d["capabilities"] if c["id"] == "gear")
        self.assertFalse(gear["enabled"])
        self.assertTrue(gear["note"], "aksi mati harus menjelaskan alasannya")


# ═════════════════════════════════════════════════════════════════════════
class TestDeadman(unittest.TestCase):
    """
    Watchdog server. Ini pengaman yang paling penting di seluruh berkas ini:
    ROV mengunci perintah terakhir, jadi klien yang mati saat wahana bergerak
    berarti wahana terus bergerak.
    """

    def setUp(self):
        from detection.rov_worker import RovWorker
        self.w = RovWorker(host="127.0.0.1")
        self.w._rov = mock.Mock()
        self.w._rov.is_fresh.return_value = True
        self.w._rov.send.return_value = True
        state.rov_last_move = {"thro": 0, "lift": 0, "yaw": 0}
        state.rov_last_move_at = 0.0

    def test_trips_when_moving_and_silent(self):
        state.record_move(2, 0, 0)
        state.rov_last_move_at = time.time() - 2.0     # > MOVE_DEADMAN_S
        with mock.patch("detection.rov_worker.broadcast"):
            self.w._check_deadman()
        sent = [c.args for c in self.w._rov.send.call_args_list]
        self.assertIn(("thro", 0), sent)
        self.assertIn(("lift", 0), sent)
        self.assertIn(("yaw", 0), sent)

    def test_silent_but_already_stopped_does_nothing(self):
        state.record_move(0, 0, 0)
        state.rov_last_move_at = time.time() - 30.0
        with mock.patch("detection.rov_worker.broadcast"):
            self.w._check_deadman()
        self.assertEqual(self.w._rov.send.call_count, 0)

    def test_recent_command_does_not_trip(self):
        state.record_move(2, 0, 0)
        with mock.patch("detection.rov_worker.broadcast"):
            self.w._check_deadman()
        self.assertEqual(self.w._rov.send.call_count, 0)

    def test_does_not_repeat_after_tripping(self):
        state.record_move(2, 0, 0)
        state.rov_last_move_at = time.time() - 2.0
        with mock.patch("detection.rov_worker.broadcast"):
            self.w._check_deadman()
            n = self.w._rov.send.call_count
            self.w._check_deadman()
            self.w._check_deadman()
        self.assertEqual(self.w._rov.send.call_count, n,
                         "nol sudah nol — jangan dibanjiri")

    def test_rearms_after_new_movement(self):
        state.record_move(2, 0, 0)
        state.rov_last_move_at = time.time() - 2.0
        with mock.patch("detection.rov_worker.broadcast"):
            self.w._check_deadman()
            state.record_move(1, 0, 0)          # operator kembali
            self.w._check_deadman()             # segar, tidak memicu
            state.rov_last_move_at = time.time() - 2.0
            self.w._rov.send.reset_mock()
            self.w._check_deadman()             # diam lagi → memicu lagi
        self.assertGreater(self.w._rov.send.call_count, 0)


class TestSendMoveDedupe(unittest.TestCase):

    def setUp(self):
        from detection.rov_worker import RovWorker
        self.w = RovWorker(host="127.0.0.1")
        self.w._rov = mock.Mock()
        self.w._rov.is_fresh.return_value = True
        self.w._rov.send.return_value = True

    def test_repeated_identical_vector_is_deduped(self):
        self.w.send_move(1, 0, 0)
        n1 = self.w._rov.send.call_count
        self.w.send_move(1, 0, 0)               # sama persis, dalam MOVE_REPEAT_S
        self.assertEqual(self.w._rov.send.call_count, n1)

    def test_changed_axis_is_sent(self):
        self.w.send_move(1, 0, 0)
        self.w._rov.send.reset_mock()
        self.w.send_move(2, 0, 0)
        sent = [c.args for c in self.w._rov.send.call_args_list]
        self.assertIn(("thro", 2), sent)

    def test_resent_after_repeat_interval(self):
        from detection import rov_worker as rw
        self.w.send_move(1, 0, 0)
        self.w._last_move_sent_at = time.time() - (rw.MOVE_REPEAT_S + 0.1)
        self.w._rov.send.reset_mock()
        self.w.send_move(1, 0, 0)
        self.assertGreater(self.w._rov.send.call_count, 0,
                           "latch perlu disegarkan berkala")

    def test_stale_telemetry_sends_nothing(self):
        self.w._rov.is_fresh.return_value = False
        self.assertFalse(self.w.send_move(2, 0, 0))
        self.assertEqual(self.w._rov.send.call_count, 0)


# ═════════════════════════════════════════════════════════════════════════
class TestMappingParity(unittest.TestCase):
    """
    Web dan `controller_mapper.py` harus setuju soal AKSI tiap tombol.

    Indeks tombolnya memang berbeda — Gamepad API memakai "standard mapping"
    (Back di 8) sementara SDL/pygame memakai penomorannya sendiri (Back di 6).
    Yang tidak boleh berbeda adalah tombol fisik mana melakukan apa. Operator
    yang melatih memori otot di alat pemetaan lalu menemukan L3 dan R3 tertukar
    di web akan menekan yang salah persis saat sedang tidak sempat berpikir.

    Uji ini pernah gagal sekali dan menemukan tepat kesalahan itu.
    """

    WEB = pathlib.Path(__file__).resolve().parents[1] / \
        "static/detection/js/controls.js"

    # Selisih yang disengaja, beserta alasannya. Menambah baris di sini adalah
    # keputusan sadar; itulah gunanya daftar ini eksplisit.
    INTENTIONAL = {
        "LB": ("record", "photo", "web belum bisa merekam video"),
        "RB": ("light_down", "light", "lampu masih 0/1, belum bertingkat"),
    }

    def _web_buttons(self):
        """Urai literal PAD_BUTTONS dari controls.js."""
        src = self.WEB.read_text(encoding="utf-8")
        start = src.index("const PAD_BUTTONS = {")
        body = src[start:src.index("};", start)]
        out = {}
        for line in body.splitlines():
            line = line.split("//")[0].strip().rstrip(",")
            if ":" not in line or not line.split(":")[0].strip().isdigit():
                continue
            idx, act = line.split(":", 1)
            out[int(idx)] = act.strip().strip("'\"")
        return out

    def test_stick_press_actions_not_swapped(self):
        """
        Regresi. Manual vendor: tekan stick KIRI = auto-heading, stick KANAN =
        auto-depth. Versi web pertama menukarnya.
        """
        b = self._web_buttons()
        self.assertEqual(b.get(10), "holdy", "L3 harus heading lock")
        self.assertEqual(b.get(11), "holdd", "R3 harus depth lock")

    def test_gear_follows_vendor_x_y(self):
        b = self._web_buttons()
        self.assertEqual(b.get(2), "gear_down", "X = gear -1")
        self.assertEqual(b.get(3), "gear_up", "Y = gear +1")

    def test_dpad_follows_vendor(self):
        b = self._web_buttons()
        self.assertEqual(b.get(12), "tilt_up")
        self.assertEqual(b.get(13), "tilt_down")
        self.assertEqual(b.get(14), "posture")
        self.assertNotIn(15, b, "D-pad kanan hanya toggle UI di app vendor, "
                                "tidak ada paket ke ROV")

    def test_estop_present_and_reachable(self):
        b = self._web_buttons()
        self.assertIn("estop", b.values())

    def test_every_disabled_action_has_a_button(self):
        """
        Aksi yang menunggu PCAP tetap harus punya rumah di controller.
        Kalau tidak, tata letak berubah lagi setelah PCAP selesai dan operator
        harus belajar ulang — persis yang ingin dihindari.
        """
        acts = set(self._web_buttons().values())
        for a in ("gear_up", "gear_down", "tilt_up", "tilt_down", "posture"):
            self.assertIn(a, acts, f"'{a}' belum punya tombol")

    def test_intentional_divergences_are_documented(self):
        src = self.WEB.read_text(encoding="utf-8")
        self.assertIn("Selisih yang DISENGAJA", src,
                      "selisih dari alat desktop harus tertulis, bukan senyap")
        for _, (desktop, web, _) in self.INTENTIONAL.items():
            self.assertIn(desktop, src)
            self.assertIn(web, src)




# ═════════════════════════════════════════════════════════════════════════
class TestSimMode(RovApiBase):
    """
    Mode simulasi. Ada karena memetakan tombol butuh menekan tiap tombol
    berkali-kali, dan satu-satunya cara aman melakukannya adalah tanpa wahana
    di ujung kabel.
    """

    def tearDown(self):
        state.rov_sim_mode = False
        super().tearDown()

    def _sim(self, on):
        return self.client.post("/api/rov/sim",
                                data=json.dumps({"sim": on}),
                                content_type="application/json")

    def test_toggle(self):
        self.assertEqual(self._sim(True).status_code, 200)
        self.assertTrue(state.rov_sim_mode)
        self._sim(False)
        self.assertFalse(state.rov_sim_mode)

    def test_move_accepted_without_worker(self):
        """Inti fiturnya: tombol bisa diuji tanpa ROV sama sekali."""
        state.rov_worker = None
        self._sim(True)
        r = self.move(thro=2, yaw=-1)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(json.loads(r.content)["sim"])

    def test_move_never_reaches_socket(self):
        self._sim(True)
        self.move(thro=2, lift=2, yaw=2)
        self.assertEqual(self.worker.moves, [],
                         "mode simulasi tidak boleh menyentuh soket")

    def test_command_never_reaches_socket(self):
        self._sim(True)
        r = self.client.post("/api/rov/command",
                             data=json.dumps({"key": "light", "value": 1}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.worker.commands, [])

    def test_validation_still_applies(self):
        """Simulasi bukan alasan melonggarkan validasi — justru di sinilah
        binding yang salah harus ketahuan."""
        self._sim(True)
        self.assertEqual(self.move(thro=9).status_code, 400)

    def test_deadman_still_tracked(self):
        """record_move tetap jalan, supaya perilaku deadman ikut teruji."""
        self._sim(True)
        self.move(thro=2)
        self.assertEqual(state.rov_last_move["thro"], 2)

    def test_leaving_sim_zeroes_movement(self):
        """
        Keluar dari simulasi sementara vektor terakhir bukan nol berarti
        perintah nyata pertama bisa langsung menggerakkan wahana pada nilai
        yang tadinya cuma pura-pura.
        """
        self._sim(True)
        self.move(thro=2)
        self._sim(False)
        self.assertEqual(state.rov_last_move, {"thro": 0, "lift": 0, "yaw": 0})
        # Dua stop: satu saat MASUK simulasi, satu saat keluar. Sejak
        # beta8.1b kedua arah transisi menolkan gerak fisik — lihat
        # TestSimTransitionSafety di test_beta81_controller.py.
        self.assertEqual(len(self.worker.stops), 2)


class TestControllerProfiles(unittest.TestCase):

    XBOX = "Xbox 360 Controller (STANDARD GAMEPAD Vendor: 045e Product: 028e)"

    def setUp(self):
        from detection import controller_profiles as cp
        self.cp = cp
        self.path = cp._store_path()
        self._backup = self.path.read_bytes() if self.path.exists() else None

    def tearDown(self):
        if self._backup is not None:
            self.path.write_bytes(self._backup)
        elif self.path.exists():
            self.path.unlink()

    def test_key_prefers_vendor_product(self):
        """Nama berubah antar browser; vendor/product tidak."""
        self.assertEqual(self.cp.device_key(self.XBOX), "vp:045e:028e")

    def test_key_survives_browser_name_differences(self):
        a = "Xbox 360 Controller (STANDARD GAMEPAD Vendor: 045e Product: 028e)"
        b = "045e-028e-Xbox 360 Wired (Vendor: 045e Product: 028e)"
        self.assertEqual(self.cp.device_key(a), self.cp.device_key(b))

    def test_key_falls_back_to_name(self):
        k = self.cp.device_key("Geneinno Gamepad")
        self.assertTrue(k.startswith("name:"))
        self.assertNotIn(" ", k)

    def test_empty_id_does_not_crash(self):
        self.assertEqual(self.cp.device_key(""), "name:unknown")

    def test_roundtrip(self):
        self.cp.save(self.XBOX, {"b0": "light", "a1-": "thro"}, label="Xbox")
        got = self.cp.get(self.XBOX)
        self.assertEqual(got["mapping"]["b0"], "light")
        self.assertEqual(got["mapping"]["a1-"], "thro")

    def test_unknown_action_dropped(self):
        """Salah ketik di profil harus hilang, bukan diam-diam ikut."""
        self.cp.save(self.XBOX, {"b0": "light", "b1": "hapus_semua"})
        m = self.cp.get(self.XBOX)["mapping"]
        self.assertIn("b0", m)
        self.assertNotIn("b1", m)

    def test_delete(self):
        self.cp.save(self.XBOX, {"b0": "light"})
        self.assertTrue(self.cp.delete(self.XBOX))
        self.assertEqual(self.cp.get(self.XBOX), {})

    def test_corrupt_file_does_not_crash(self):
        """Berkas rusak = kehilangan kustomisasi, bukan kehilangan kendali."""
        self.path.write_text("{ bukan json", encoding="utf-8")
        self.assertEqual(self.cp.load_all(), {})

    def test_api_roundtrip(self):
        c = Client()
        r = c.post("/api/rov/mapping",
                   data=json.dumps({"id": self.XBOX,
                                    "mapping": {"b3": "estop"}}),
                   content_type="application/json")
        self.assertEqual(r.status_code, 200)
        r = c.get("/api/rov/mapping?id=" + self.XBOX)
        d = json.loads(r.content)
        self.assertEqual(d["profile"]["mapping"]["b3"], "estop")
        self.assertIn("estop", d["actions"])

    def test_api_requires_id(self):
        c = Client()
        r = c.post("/api/rov/mapping", data=json.dumps({"mapping": {}}),
                   content_type="application/json")
        self.assertEqual(r.status_code, 400)



if __name__ == "__main__":
    unittest.main(verbosity=2)

"""
Regresi beta8.1 — empat bug controller yang saling menumpuk.

Semuanya ditemukan pada beta8 dan semuanya SENYAP: tidak ada yang melempar
kesalahan, tidak ada yang muncul di log. Gejalanya hanya "tombolnya tidak
bisa diklik" atau "stick-nya mati" — persis kelas kegagalan yang tidak akan
ketahuan lagi kalau tidak dijaga di sini.

    python tests/test_beta81_controller.py

Sebagian besar uji ini membaca berkas sumber, bukan menjalankannya. Itu
disengaja: yang rusak di beta8 adalah CSS dan struktur pemasangan listener,
dan keduanya tidak punya jalur Python untuk diuji. Satu kelas terakhir
BENAR-BENAR mengeksekusi effectiveBindings() lewat node kalau node ada.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "rov_sar_web.settings")

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSS = ROOT / "static/detection/css/controls.css"
JS_CONTROLS = ROOT / "static/detection/js/controls.js"
JS_DASHBOARD = ROOT / "static/detection/js/dashboard.js"
HTML = ROOT / "templates/detection/dashboard.html"


def css_block(selector_line):
    """Isi blok CSS untuk selector persis, tanpa komentar."""
    src = CSS.read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", src):
        selectors = [s.strip() for s in m.group(1).split(",")]
        if selector_line in selectors:
            return m.group(2)
    return None


def css_blocks_containing(selector_line):
    """Semua blok yang selector-nya memuat baris ini (boleh dalam daftar)."""
    src = CSS.read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    out = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", src):
        selectors = [s.strip() for s in m.group(1).split(",")]
        if selector_line in selectors:
            out.append(m.group(2))
    return out


# ═════════════════════════════════════════════════════════════════════════
class TestLockScope(unittest.TestCase):
    """
    Bug 1. `.pad-panel.locked` memasang pointer-events:none pada SELURUH
    panel, sementara kalibrasi, deadzone, mode simulasi, dan seluruh kotak
    pemetaan kustom ada di dalamnya. Akibatnya tombol "petakan" terlihat
    tapi mati, dan petunjuk di UI-nya sendiri ("nyalakan mode simulasi
    dulu") menyuruh operator menekan sesuatu yang juga ikut terkunci.
    """

    def test_panel_still_blocks_pointer_events(self):
        """Yang dikunci tetap dikunci — ini bukan pelonggaran menyeluruh."""
        blk = css_block(".pad-panel.locked")
        self.assertIsNotNone(blk, "aturan .pad-panel.locked hilang")
        self.assertIn("pointer-events", blk)
        self.assertIn("none", blk)

    def test_dimming_not_on_parent(self):
        """
        `opacity` dan `filter` di induk membentuk grup rendering yang tidak
        bisa dibatalkan anak. Selama keduanya ada di sini, setiap aturan
        yang mencoba mengembalikan anak ke terang penuh adalah aturan mati.
        """
        blk = css_block(".pad-panel.locked")
        self.assertNotIn("opacity", blk,
                         "opacity di induk membuat STOP ikut redup dan tidak "
                         "bisa dibatalkan dari anak")
        self.assertNotIn("filter", blk,
                         "filter di induk berlaku untuk seluruh subtree")

    def test_movement_area_is_dimmed(self):
        """Peredupan tidak dihapus — hanya dipindah ke area gerak."""
        for sel in (".pad-panel.locked .axis-bars", ".pad-panel.locked .pad-body"):
            blks = css_blocks_containing(sel)
            self.assertTrue(blks, f"{sel} harus tetap diredupkan saat terkunci")
            self.assertTrue(any("opacity" in b for b in blks))

    def test_config_areas_clickable_while_locked(self):
        """Kalibrasi, deadzone, sim, dan pemetaan harus hidup saat terkunci."""
        for sel in (".pad-panel.locked .pad-foot", ".pad-panel.locked .learn-box"):
            blks = css_blocks_containing(sel)
            self.assertTrue(blks, f"{sel} tidak pernah dibuka kuncinya")
            self.assertTrue(any("auto" in b for b in blks),
                            f"{sel} harus pointer-events: auto")

    def test_estop_clickable_while_locked(self):
        blks = css_blocks_containing(".pad-panel.locked .estop-btn")
        self.assertTrue(blks, "STOP wajib tetap bisa ditekan saat terkunci")
        self.assertTrue(any("auto" in b for b in blks))

    def test_config_controls_really_live_inside_the_panel(self):
        """
        Uji di atas tidak ada artinya kalau markup-nya pindah. Kalau suatu
        saat pad-foot dikeluarkan dari #pad-panel, aturan pengecualiannya
        jadi sampah yang menyesatkan dan uji ini yang memberi tahu.
        """
        html = HTML.read_text(encoding="utf-8")
        start = html.index('id="pad-panel"')
        panel = html[start:html.index("<!-- ─── MIDDLE", start)]
        for needle in ('id="ctrl-sim"', 'id="btn-pad-calib"', 'id="ctrl-deadzone"',
                       'id="learn-table"', 'id="btn-map-save"'):
            self.assertIn(needle, panel,
                          f"{needle} tidak lagi di dalam #pad-panel — "
                          "aturan lock/unlock di CSS perlu ditinjau ulang")


# ═════════════════════════════════════════════════════════════════════════
class TestGamepadRescan(unittest.TestCase):
    """
    Bug 2. `padIndex` hanya diisi event `gamepadconnected`. Event itu tidak
    datang kalau halaman di-refresh dengan pad sudah menyala, jadi
    pollGamepad() berhenti di baris pertama dan mode belajar diam di
    "tekan…" selamanya — tanpa satu pun pesan.
    """

    def setUp(self):
        self.src = JS_CONTROLS.read_text(encoding="utf-8")

    def test_scanner_exists(self):
        self.assertIn("function syncGamepad(", self.src)
        self.assertIn("navigator.getGamepads()", self.src)

    def test_poll_does_not_bail_on_null_index(self):
        """Penjaga lama inilah yang membuat pemindaian tidak pernah terjadi."""
        self.assertNotIn("if (padIndex === null || !navigator.getGamepads) return;",
                         self.src)

    def test_registration_is_factored_out(self):
        """
        loadProfile() menembak jaringan dan calibratePad() mengambil nilai
        istirahat. Kalau pemindai memanggilnya langsung, keduanya jalan
        10x/detik. Pemasangan harus terkumpul di satu fungsi.
        """
        self.assertIn("function registerPad(", self.src)
        self.assertIn("function forgetPad(", self.src)

    def test_insecure_context_is_explained(self):
        """
        getGamepads() melempar di non-secure context — persis yang terjadi
        saat dashboard dibuka dari tablet lewat http://192.168.x.x. Diamnya
        harus dijelaskan, bukan tampak seperti pad rusak.
        """
        self.assertIn("padApiBlocked", self.src)
        self.assertIn("secure context", self.src)

    def test_learn_table_warns_when_no_pad(self):
        self.assertIn("learn-warn", self.src)
        self.assertIn("learn-warn", CSS.read_text(encoding="utf-8"))


# ═════════════════════════════════════════════════════════════════════════
class TestPollOrder(unittest.TestCase):
    """
    Bug 3. tick() membaca mergeSources() sebelum pollGamepad(), jadi nilai
    pad yang dipakai selalu hasil poll tick sebelumnya — tertinggal 100 ms
    pada loop 10 Hz, menumpuk di atas latensi jaringan/TCP/thruster/RTSP.
    """

    def test_poll_precedes_merge(self):
        src = JS_CONTROLS.read_text(encoding="utf-8")
        start = src.index("function tick()")
        body = src[start:src.index("\n    }", start)]
        # Komentar di dalam tick() menyebut kedua nama itu justru untuk
        # menjelaskan bug lamanya. Yang diuji urutan PANGGILAN, bukan teks.
        body = re.sub(r"//.*", "", body)
        self.assertLess(body.index("pollGamepad()"), body.index("mergeSources()"),
                        "pollGamepad() harus dipanggil sebelum mergeSources()")


# ═════════════════════════════════════════════════════════════════════════
class TestUnlockSingleOwner(unittest.TestCase):
    """
    Bug 4. Dua listener pada checkbox yang sama. Milik controls.js berjalan
    sinkron dan langsung membuka kunci dari nilai checkbox; milik
    dashboard.js baru selesai setelah await. Kalau server MENOLAK, checkbox
    dikembalikan lewat script — yang tidak memicu 'change' — sehingga
    controls.js tetap menganggap dirinya terbuka.
    """

    def test_controls_js_does_not_listen(self):
        src = JS_CONTROLS.read_text(encoding="utf-8")
        src_nc = re.sub(r"//.*", "", src)
        self.assertNotIn("$('ctrl-rov-unlock')", src_nc,
                         "controls.js tidak boleh lagi menyentuh checkbox ini")

    def test_controls_js_still_exposes_setter(self):
        """dashboard.js tetap butuh jalannya masuk."""
        src = JS_CONTROLS.read_text(encoding="utf-8")
        self.assertIn("setUnlocked", src)

    def test_dashboard_rolls_back_full_state_on_failure(self):
        """
        Mengembalikan `.checked` saja tidak cukup: itu tidak memicu 'change'
        dan tidak pernah sampai ke RovControls.
        """
        src = JS_DASHBOARD.read_text(encoding="utf-8")
        start = src.index("'/api/rov/unlock'")
        window = src[start:start + 700]
        self.assertIn("applyUnlockToUI", window,
                      "jalur gagal harus mengembalikan state, bukan cuma centang")

    def test_apply_unlock_reaches_control_layer(self):
        src = JS_DASHBOARD.read_text(encoding="utf-8")
        start = src.index("function applyUnlockToUI(")
        body = src[start:src.index("\n}", start)]
        self.assertIn("RovControls", body)
        self.assertIn("setUnlocked", body)


# ═════════════════════════════════════════════════════════════════════════
def _slice_function(src, name):
    start = src.index("function " + name + "(")
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise ValueError(name)


def _slice_const(src, name, open_ch, close_ch):
    start = src.index("const " + name + " =")
    i = src.index(open_ch, start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == open_ch:
            depth += 1
        elif src[j] == close_ch:
            depth -= 1
            if depth == 0:
                return src[start:j + 1] + ";"
    raise ValueError(name)


@unittest.skipUnless(shutil.which("node"), "node tidak tersedia — uji dilewati")
class TestEffectiveBindings(unittest.TestCase):
    """
    Bug 5 (yang paling halus). Pada beta8 blok pemetaan kustom diakhiri
    `return`, jadi SATU binding tersimpan mematikan stick dan seluruh tombol
    bawaan sekaligus. Tapi sekadar membuang `return` juga salah: kalau
    bawaan dan kustom berjalan berdampingan, memetakan lampu ke B membuat RB
    *dan* B sama-sama menyalakan lampu, tanpa cara melihatnya di UI.

    Aturan yang benar adalah override PER AKSI. Kelas ini menjalankan
    effectiveBindings() yang asli di node untuk membuktikannya, bukan
    mencocokkan teks.
    """

    @classmethod
    def setUpClass(cls):
        src = JS_CONTROLS.read_text(encoding="utf-8")
        cls.harness = "\n".join([
            _slice_const(src, "MOVE_ACTIONS", "[", "]"),
            _slice_const(src, "PAD_BUTTONS", "{", "}"),
            _slice_function(src, "effectiveBindings"),
        ])

    def run_js(self, custom_map, pad_axis_map):
        script = (
            f"let customMap = {json.dumps(custom_map)};\n"
            f"let padAxisMap = {json.dumps(pad_axis_map)};\n"
            f"{self.harness}\n"
            "console.log(JSON.stringify(effectiveBindings()));\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(script)
            path = f.name
        try:
            out = subprocess.run(["node", path], capture_output=True, text=True,
                                 timeout=20)
            self.assertEqual(out.returncode, 0, out.stderr)
            return json.loads(out.stdout)
        finally:
            os.unlink(path)

    DEFAULT_AXES = {
        "yaw":  {"axis": 0, "invert": False},
        "thro": {"axis": 1, "invert": True},
        "lift": {"axis": 3, "invert": True},
    }

    def test_no_profile_keeps_every_default(self):
        r = self.run_js(None, self.DEFAULT_AXES)
        acts = [a for _, a in r["buttons"]]
        for a in ("light", "photo", "mark", "estop", "holdy", "holdd"):
            self.assertIn(a, acts, f"'{a}' hilang padahal belum ada profil")
        self.assertEqual(r["axes"]["thro"]["fallback"]["axis"], 1)

    def test_one_binding_does_not_kill_the_rest(self):
        """Regresi langsung dari beta8: ini yang dulu mematikan semuanya."""
        r = self.run_js({"b1": "light"}, self.DEFAULT_AXES)
        acts = [a for _, a in r["buttons"]]
        for a in ("photo", "mark", "estop", "holdy", "holdd", "gear_up"):
            self.assertIn(a, acts, f"'{a}' ikut mati karena satu binding kustom")
        self.assertIsNotNone(r["axes"]["thro"]["fallback"],
                             "stick ikut mati karena satu binding tombol")

    def test_override_replaces_the_default_slot(self):
        """Lampu di B berarti RB TIDAK lagi menyalakan lampu."""
        r = self.run_js({"b1": "light"}, self.DEFAULT_AXES)
        light_slots = sorted(sl for sl, a in r["buttons"] if a == "light")
        self.assertEqual(light_slots, ["b1"],
                         "aksi yang dipetakan ulang tidak boleh punya dua tombol")

    def test_axis_override_is_per_action(self):
        r = self.run_js({"a2-": "lift"}, self.DEFAULT_AXES)
        self.assertEqual(r["axes"]["lift"]["slots"], ["a2-"])
        self.assertIsNotNone(r["axes"]["thro"]["fallback"],
                             "thro tidak disebut di profil, harus tetap bawaan")
        self.assertIsNotNone(r["axes"]["yaw"]["fallback"])

    def test_digital_buttons_can_map_both_directions(self):
        r = self.run_js({"b7": "thro", "b6": "thro_neg"},
                        self.DEFAULT_AXES)
        self.assertEqual(r["axes"]["thro"]["slots"], ["b7", "b6"])
        self.assertEqual(r["axes"]["thro"]["negative"], ["b6"])
        self.assertEqual([a for sl, a in r["buttons"] if sl in ("b6", "b7")],
                         [], "tombol arah tidak boleh ikut jalur aksi biasa")

    def test_all_reverse_directions_are_learnable(self):
        src = JS_CONTROLS.read_text(encoding="utf-8")
        profile_src = (ROOT / "detection" / "controller_profiles.py").read_text(
            encoding="utf-8")
        for action, label in (("thro_neg", "Mundur"),
                              ("lift_neg", "Turun"),
                              ("yaw_neg", "Putar kiri")):
            self.assertIn(f"['{action}',", src)
            self.assertIn(label, src)
            self.assertIn(f'"{action}"', profile_src,
                          "backend harus menerima aksi arah negatif")

    # ── Arah kedua: SLOT fisik yang bentrok ──────────────────────────────
    # Ketiganya lolos di beta8.1 awal. Aturan override waktu itu hanya
    # melihat aksi, jadi binding bawaan pada slot yang sudah diambil tetap
    # ikut terpasang dan satu tombol fisik punya dua rumah.

    def test_taking_a_default_button_releases_its_default_action(self):
        """b5 bawaannya lampu. Kalau operator mengambilnya untuk 'mark',
        lampu harus melepaskan b5 — bukan menumpang di sana."""
        r = self.run_js({"b5": "mark"}, self.DEFAULT_AXES)
        on_b5 = sorted(a for sl, a in r["buttons"] if sl == "b5")
        self.assertEqual(on_b5, ["mark"],
                         "satu tombol tidak boleh memicu dua aksi")

    def test_button_mapped_to_axis_does_not_also_fire_its_default(self):
        """
        Yang paling berbahaya dari ketiganya. Sumbu dibaca di jalur gerak
        dan tombol di jalur tombol, jadi b5→thro yang berdampingan dengan
        b5→light bawaan membuat satu tombol MENGGERAKKAN WAHANA sekaligus
        menyalakan lampu. Penjagaan tepi-tekan tidak menolong: kedua jalur
        membaca slot yang sama secara terpisah.
        """
        r = self.run_js({"b5": "thro"}, self.DEFAULT_AXES)
        self.assertEqual(r["axes"]["thro"]["slots"], ["b5"])
        self.assertEqual([a for sl, a in r["buttons"] if sl == "b5"], [],
                         "b5 sudah jadi sumbu gerak; binding tombolnya wajib lepas")

    def test_axis_taken_by_a_button_action_releases_movement_fallback(self):
        """
        'a1' dan 'a1-' sumbu fisik yang sama — akhiran itu cuma arah baca.
        Kalau perbandingannya memakai string mentah, a1- untuk lampu akan
        lolos berdampingan dengan fallback thro di sumbu 1.
        """
        r = self.run_js({"a1-": "light"}, self.DEFAULT_AXES)
        self.assertIsNone(r["axes"]["thro"]["fallback"],
                          "sumbu 1 sudah dipakai lampu; thro tidak boleh "
                          "diam-diam ikut membacanya")
        self.assertIn(["a1-", "light"], r["buttons"])
        self.assertIsNotNone(r["axes"]["lift"]["fallback"],
                             "sumbu lain tidak ikut terdampak")

    def test_collision_rules_do_not_fire_on_unrelated_slots(self):
        """Penjagaan slot tidak boleh jadi terlalu rakus."""
        r = self.run_js({"b1": "light"}, self.DEFAULT_AXES)
        acts = [a for _, a in r["buttons"]]
        for a in ("photo", "mark", "estop", "gear_up", "tilt_up"):
            self.assertIn(a, acts, f"'{a}' ikut terbuang tanpa alasan")
        self.assertIsNotNone(r["axes"]["thro"]["fallback"])

    def test_estop_survives_unrelated_overrides(self):
        r = self.run_js({"b1": "light", "a2-": "lift"}, self.DEFAULT_AXES)
        self.assertIn("estop", [a for _, a in r["buttons"]],
                      "STOP tidak boleh hilang gara-gara profil kustom")

    def test_uncalibrated_pad_does_not_crash(self):
        r = self.run_js(None, None)
        self.assertIsNone(r["axes"]["thro"]["fallback"])


@unittest.skipUnless(shutil.which("node"), "node tidak tersedia — uji dilewati")
class TestStandardGamepadCalibration(unittest.TestCase):
    """Pad standar tidak boleh salah dikalibrasi dari gerakan pemicu koneksi."""

    def test_first_vertical_motion_does_not_remove_xbox_axes(self):
        src = JS_CONTROLS.read_text(encoding="utf-8")
        fn = _slice_function(src, "calibratePad")
        script = "\n".join([
            "const gp = {mapping:'standard', axes:[0,-1,0,0]};",
            "const navigator = {getGamepads: () => [gp]};",
            "const $ = () => null;",
            "let padIndex=0, padRest=null, padAxisMap=null;",
            fn,
            "calibratePad();",
            "console.log(JSON.stringify({padRest, padAxisMap}));",
        ])
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(script)
            path = f.name
        try:
            out = subprocess.run(["node", path], capture_output=True, text=True,
                                 timeout=20)
            self.assertEqual(out.returncode, 0, out.stderr)
            result = json.loads(out.stdout)
        finally:
            os.unlink(path)

        self.assertEqual(result["padRest"][:4], [0, 0, 0, 0])
        self.assertEqual(result["padAxisMap"]["yaw"]["axis"], 0)
        self.assertEqual(result["padAxisMap"]["thro"]["axis"], 1)
        self.assertEqual(result["padAxisMap"]["lift"]["axis"], 3)


# ═════════════════════════════════════════════════════════════════════════
class TestProfileIsolation(unittest.TestCase):
    """
    Bug 6. Profil pad lama menempel ke pad baru.

    `registerPad()` tidak membersihkan `customMap`, dan `loadProfile()`
    hanya menimpanya kalau profil yang datang TIDAK kosong. Pad tanpa
    profil karena itu mewarisi pemetaan pad sebelumnya. Selama padIndex
    cuma terisi sekali per muat halaman ini nyaris tak terlihat — tapi
    rescan panas di beta8.1 membuat ganti pad di tengah operasi jadi hal
    yang wajar, jadi bug lama ini justru naik kelas.
    """

    def setUp(self):
        self.src = JS_CONTROLS.read_text(encoding="utf-8")

    def _fn(self, name):
        return _slice_function(self.src, name)

    def test_register_resets_device_state(self):
        body = self._fn("registerPad")
        for var in ("customMap", "padAxisMap", "padRest", "padPrevSlots", "learnSlot"):
            self.assertRegex(body, var + r"\s*=",
                             f"{var} tidak direset saat pad berganti")

    def test_empty_profile_clears_instead_of_being_skipped(self):
        """
        Profil kosong berarti "pakai bawaan", dan itu harus DITULIS.
        Melewatinya adalah persis cara pemetaan lama bertahan.
        """
        body = self._fn("loadProfile")
        self.assertNotIn("if (m && Object.keys(m).length) {", body,
                         "profil kosong masih dilewati, bukan menghapus")
        self.assertRegex(body, r"customMap\s*=\s*n\s*\?")

    def test_late_response_cannot_overwrite_a_newer_pad(self):
        """
        Fetch pad lama bisa tiba setelah pad baru terdaftar. Tanpa penjaga,
        ia memasang pemetaan milik perangkat yang sudah tidak terhubung.
        """
        body = self._fn("loadProfile")
        self.assertIn("padId !== wanted", body,
                      "tidak ada penjagaan terhadap respons yang datang telat")

    def test_load_profile_is_called_with_explicit_id(self):
        self.assertIn("loadProfile(padId)", self._fn("registerPad"))


# ═════════════════════════════════════════════════════════════════════════
class TestRenderThrottle(unittest.TestCase):
    """
    syncGamepad() berjalan 10x/detik. Setiap render ulang membangun ulang
    DOM tabel pemetaan, jadi penanda status hanya boleh dirender saat
    keadaannya BERUBAH — bukan tiap tick.
    """

    def test_blocked_state_renders_only_on_transition(self):
        body = _slice_function(JS_CONTROLS.read_text(encoding="utf-8"),
                               "syncGamepad")
        for m in re.finditer(r"renderLearnTable\(\)", body):
            before = body[:m.start()]
            self.assertIn("if (!padApiBlocked)", before[-260:],
                          "render dipanggil tanpa penjaga transisi")


# ═════════════════════════════════════════════════════════════════════════
#  Lapisan keselamatan — butuh Django. Dilewati kalau Django tidak terpasang,
#  supaya uji sisi berkas tetap bisa dijalankan di lingkungan telanjang.
# ═════════════════════════════════════════════════════════════════════════
try:
    import django
    django.setup()
    from django.test import Client
    from detection.state import state
    _DJANGO = True
except Exception:
    _DJANGO = False


class _FakeWorker:
    def __init__(self, stop_ok=True):
        self.moves, self.stops, self.commands = [], [], []
        self.stop_ok = stop_ok

    def send(self, key, value):
        self.commands.append((key, value))
        return True

    def send_move(self, thro, lift, yaw):
        self.moves.append((thro, lift, yaw))
        return True

    def force_stop(self, reason=""):
        self.stops.append(reason)
        return self.stop_ok


@unittest.skipUnless(_DJANGO, "Django tidak terpasang — uji dilewati")
class SafetyBase(unittest.TestCase):

    def setUp(self):
        self.client = Client()
        self.worker = _FakeWorker()
        state.rov_worker = self.worker
        state.rov_control_unlocked = True
        state.rov_sim_mode = False
        state.rov_pilot_id = None
        state.rov_pilot_at = 0.0
        state.rov_last_move = {"thro": 0, "lift": 0, "yaw": 0}
        state.rov_last_move_at = 0.0

    def tearDown(self):
        state.rov_worker = None
        state.rov_control_unlocked = False
        state.rov_sim_mode = False
        state.rov_pilot_id = None

    def sim(self, on):
        return self.client.post("/api/rov/sim", data=json.dumps({"sim": on}),
                                content_type="application/json")

    def move(self, thro=0, lift=0, yaw=0, client_id="tester"):
        return self.client.post(
            "/api/rov/move",
            data=json.dumps({"thro": thro, "lift": lift, "yaw": yaw,
                             "client_id": client_id}),
            content_type="application/json")

    def estop(self):
        return self.client.post("/api/rov/estop",
                                data=json.dumps({"client_id": "tester"}),
                                content_type="application/json")


class TestSimTransitionSafety(SafetyBase):
    """
    Bug 7 (blocker). Masuk ke mode simulasi tidak menolkan gerak fisik.

    Sampai beta8.1a `api_rov_sim()` hanya memanggil force_stop() pada arah
    SIM → REAL. Arah sebaliknya justru yang berbahaya: wahana yang sedang
    thro:2 tetap menahan perintah itu di firmware, sementara server berhenti
    meneruskan /move ke soket. Tidak ada apa pun yang mencabutnya.

    Deadman tidak menolong. /move di mode simulasi tetap memanggil
    record_move(), jadi rov_last_move_at terus diperbarui dan watchdog
    menyimpulkan browser masih sehat. Begitu operator melepas stick,
    snapshot-nya nol dan watchdog juga tidak punya alasan bertindak.
    """

    def test_entering_sim_stops_hardware(self):
        self.move(thro=2)
        self.assertEqual(self.worker.moves[-1], (2, 0, 0))
        self.sim(True)
        self.assertTrue(self.worker.stops,
                        "masuk simulasi tanpa menolkan gerak fisik — "
                        "wahana tetap menjalankan perintah terakhirnya")

    def test_leaving_sim_stops_hardware(self):
        """Arah yang sudah dijaga sejak beta8 — jangan sampai hilang."""
        self.sim(True)
        self.move(thro=2)
        before = len(self.worker.stops)
        self.sim(False)
        self.assertGreater(len(self.worker.stops), before)

    def test_stop_precedes_the_flag(self):
        """
        Urutannya penting: kalau flag dipasang lebih dulu, force_stop()
        berjalan saat mode sudah simulasi dan perintahnya tidak akan pernah
        menemukan jalan ke soket.
        """
        self.move(thro=2)
        self.sim(True)
        self.assertTrue(state.rov_sim_mode)
        self.assertTrue(self.worker.stops)

    def test_transition_releases_pilot(self):
        self.move(thro=1, client_id="hp-pilot")
        self.assertEqual(state.rov_pilot_id, "hp-pilot")
        self.sim(True)
        self.assertIsNone(state.rov_pilot_id,
                          "klaim kendali harus lepas saat aturan mainnya ganti")

    def test_state_vector_zeroed_on_entry(self):
        self.move(thro=2)
        self.sim(True)
        self.assertEqual(state.rov_last_move,
                         {"thro": 0, "lift": 0, "yaw": 0})

    def test_no_stop_when_mode_unchanged(self):
        """Menyetel ulang nilai yang sama bukan transisi."""
        self.sim(True)
        n = len(self.worker.stops)
        self.sim(True)
        self.assertEqual(len(self.worker.stops), n)


class TestStopFailureEndpoint(SafetyBase):
    """
    v.beta8.1c: endpoint tidak boleh menulis state nol atau berganti mode
    jika force_stop perangkat keras mengembalikan gagal.
    """

    def test_estop_failure_preserves_move_state(self):
        self.worker.stop_ok = False
        state.record_move(2, 0, 0)
        r = self.estop()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(state.rov_last_move,
                         {"thro": 2, "lift": 0, "yaw": 0})

    def test_sim_transition_aborts_when_physical_stop_fails(self):
        self.worker.stop_ok = False
        state.record_move(2, 0, 0)
        r = self.sim(True)
        self.assertEqual(r.status_code, 409)
        self.assertFalse(state.rov_sim_mode)
        self.assertEqual(state.rov_last_move,
                         {"thro": 2, "lift": 0, "yaw": 0})



class TestStopFailureUI(unittest.TestCase):
    """Frontend tidak boleh menampilkan STOP sukses untuk HTTP 409."""

    def setUp(self):
        self.src = JS_CONTROLS.read_text(encoding="utf-8")

    def test_estop_checks_http_and_json_result(self):
        body = _slice_function(self.src, "estop")
        self.assertIn("r.ok", body)
        self.assertIn("j.ok", body)
        self.assertIn("STOP GAGAL", body)

    def test_sim_failure_rolls_back_and_reports_error(self):
        body = _slice_function(self.src, "setSim")
        self.assertIn("applySim(j.sim)", body)
        self.assertIn("r.ok", body)
        self.assertIn("j.ok", body)
        self.assertIn("tidak diubah", body)

    def test_websocket_estop_failure_is_not_reported_as_success(self):
        src = JS_DASHBOARD.read_text(encoding="utf-8")
        pos = src.index("data.event === 'rov_estop'")
        body = src[pos:pos + 500]
        self.assertIn("data.payload.ok", body)
        self.assertIn("STOP GAGAL", body)



class TestEstopIsNeverSimulated(SafetyBase):
    """
    Bug 8 (blocker). STOP ikut disimulasikan.

    Sampai beta8.1a cabang simulasi di `api_rov_estop()` langsung `return`
    sebelum menyentuh soket. Digabung dengan Bug 7, hasilnya: wahana yang
    sempat bergerak lalu modenya dipindah ke simulasi masih menahan perintah
    terakhirnya — dan tombol merah besar di layar hanya menghentikan
    simulasinya. Gerak normal boleh dialihkan ke simulasi; STOP tidak.
    """

    def test_estop_reaches_hardware_in_sim_mode(self):
        self.sim(True)
        n = len(self.worker.stops)
        self.estop()
        self.assertGreater(len(self.worker.stops), n,
                           "STOP di mode simulasi tidak menyentuh perangkat "
                           "keras sama sekali")

    def test_estop_reaches_hardware_in_real_mode(self):
        n = len(self.worker.stops)
        self.estop()
        self.assertGreater(len(self.worker.stops), n)

    def test_estop_zeroes_state_and_releases_pilot(self):
        self.sim(True)
        self.move(thro=2, client_id="hp-pilot")
        self.estop()
        self.assertEqual(state.rov_last_move,
                         {"thro": 0, "lift": 0, "yaw": 0})
        self.assertIsNone(state.rov_pilot_id)

    def test_estop_without_worker_still_ok_in_sim(self):
        """Tanpa worker memang tidak ada perangkat keras untuk dihentikan."""
        state.rov_worker = None
        self.sim(True)
        r = self.estop()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(json.loads(r.content)["ok"])

    def test_estop_without_worker_fails_loudly_in_real(self):
        state.rov_worker = None
        r = self.estop()
        self.assertEqual(r.status_code, 409,
                         "tidak ada jalur ke wahana di mode nyata adalah "
                         "kegagalan yang harus terlihat")


@unittest.skipUnless(_DJANGO, "Django tidak terpasang — uji dilewati")
class TestSimExposedInState(unittest.TestCase):
    """
    Bug 9. Halaman yang dimuat saat server sudah SIM menampilkan centang
    kosong dan tanpa pita peringatan — broadcast hanya menyusulkan
    PERUBAHAN, bukan keadaan yang sudah berjalan. Operator membaca "mode
    nyata" padahal perintahnya tidak sampai ke wahana sama sekali.
    """

    def test_api_state_carries_sim(self):
        c = Client()
        state.rov_sim_mode = True
        try:
            s = json.loads(c.get("/api/state").content)
            self.assertTrue(s["rov"]["sim"])
        finally:
            state.rov_sim_mode = False

    def test_dashboard_syncs_sim_on_initial_load(self):
        src = JS_DASHBOARD.read_text(encoding="utf-8")
        start = src.index("async function fetchInitialState(")
        body = src[start:start + 4000]
        self.assertIn("applySim", body,
                      "state awal tidak pernah memasang mode simulasi ke UI")

    def test_apply_sim_is_idempotent(self):
        """
        Dipanggil dari heartbeat, jadi tidak boleh mengumumkan status tiap
        beberapa detik — pesan yang membanjir membuat peringatan sungguhan
        ikut tenggelam.
        """
        body = _slice_function(JS_CONTROLS.read_text(encoding="utf-8"),
                               "applySim")
        self.assertIn("changed", body)


# ═════════════════════════════════════════════════════════════════════════
class TestLearnModeInterlock(unittest.TestCase):
    """
    Bug 10. Mode belajar hanya menghentikan pembacaan gamepad.

    Cabang `if (learnSlot) { ... return; }` keluar tanpa menyentuh src.pad,
    jadi nilai stick dari tick SEBELUM pemetaan dinyalakan tertinggal di
    sana dan terus dikirim. Keyboard dan stick sentuh bahkan tidak lewat
    pollGamepad() sama sekali, jadi keduanya tetap menggerakkan wahana
    selagi tabel menunggu "tekan…".

    Komentar di kodenya sudah menjanjikan "tidak ada satu pun input yang
    boleh diteruskan sebagai perintah". Sekarang janjinya ditegakkan.
    """

    def setUp(self):
        self.src = JS_CONTROLS.read_text(encoding="utf-8")

    def test_learn_branch_zeroes_pad(self):
        body = _slice_function(self.src, "pollGamepad")
        branch = body[body.index("if (learnSlot)"):]
        branch = branch[:branch.index("return;")]
        self.assertIn("src.pad.thro", branch,
                      "cabang mode belajar keluar tanpa menolkan src.pad")

    def test_tick_zeroes_every_source_while_learning(self):
        body = _slice_function(self.src, "tick")
        self.assertIn("learnSlot", body,
                      "tick() tidak menegakkan apa pun saat mode belajar; "
                      "keyboard dan sentuh tetap lolos")
        for k in ("'touch'", "'keys'", "'pad'"):
            self.assertIn(k, body)

    def test_entry_is_refused_while_live(self):
        """
        Interlock di titik masuk. Petunjuk di UI sudah menyuruh menyalakan
        simulasi dulu; kode tidak boleh cuma menyarankan.
        """
        body = _slice_function(self.src, "renderLearnTable")
        self.assertIn("unlocked && !simMode", body,
                      "mode belajar bisa dimasuki sementara wahana hidup")

    def test_entry_zeroes_sources(self):
        body = _slice_function(self.src, "renderLearnTable")
        self.assertIn("zeroSources()", body)


class TestForgetPadCleanup(unittest.TestCase):
    """
    Bug 11 (minor). `forgetPad()` tidak membersihkan identitas perangkat,
    jadi "Simpan Profil" masih bisa menulis profil untuk pad yang sudah
    dicabut, dan penjaga `padId !== wanted` di loadProfile() masih
    meloloskan respons pad lama karena padId-nya belum berubah.
    """

    def test_device_state_cleared_on_disconnect(self):
        body = _slice_function(JS_CONTROLS.read_text(encoding="utf-8"),
                               "forgetPad")
        for var in ("padId", "customMap", "learnSlot", "padAxisMap"):
            self.assertRegex(body, var + r"\s*=",
                             f"{var} tidak dibersihkan saat pad dicabut")


if __name__ == "__main__":
    unittest.main(verbosity=2)

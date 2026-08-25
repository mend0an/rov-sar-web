/**
 * controls.js — Lapisan masukan kendali ROV.
 *
 * ARSITEKTUR
 * ══════════
 * Tiga sumber masukan mengisi SATU vektor yang sama:
 *
 *     pad sentuh  ┐
 *     keyboard    ├──► gabung ──► kuantisasi ──► pengirim 10 Hz ──► server
 *     gamepad     ┘      (-1..1)      (-2..2)
 *
 * Bukan tiga jalur terpisah. Konsekuensinya operator bisa berpindah dari
 * jempol ke gamepad di tengah manuver tanpa apa pun perlu di-"aktifkan", dan
 * menambah sumber keempat nanti (misal pedal) tidak menyentuh sisa berkas ini.
 *
 * Penggabungan memakai magnitudo terbesar per sumbu, bukan penjumlahan.
 * Menjumlahkan berarti menahan W di keyboard sambil mendorong stick maju
 * menghasilkan nilai dua kali lipat — perilaku yang tidak diminta siapa pun
 * dan mengejutkan tepat pada saat paling tidak tepat.
 *
 * KENAPA MENGIRIM TERUS-MENERUS
 * ═════════════════════════════
 * ROV mengunci perintah terakhir: lepas stick ≠ berhenti. Server punya
 * watchdog deadman yang menolkan gerak kalau tidak mendengar apa-apa selama
 * 1,5 detik. Itu artinya browser TIDAK BOLEH mengirim hanya saat nilai
 * berubah — menahan stick di posisi tetap selama tiga detik akan dibaca
 * server sebagai klien mati, dan wahana berhenti di tengah manuver.
 *
 * Jadi: selama vektornya bukan nol, kirim 10× per detik. Saat kembali nol,
 * kirim nol beberapa kali lalu diam. Deduplikasi ke soket TCP dilakukan di
 * server, di mana ia berlaku sama untuk semua klien.
 *
 * KUANTISASI
 * ══════════
 * -1..1 → -2..2, lima level diskrit, meniru `quantize()` di controller_mapper.py.
 * Bukan proporsional penuh karena rentang itulah yang teramati di PCAP.
 * Kehalusan sebenarnya datang dari gear (field S), yang belum aktif.
 */
(function () {
    'use strict';

    // ─── Konstanta ───────────────────────────────────────────────────
    const SEND_HZ = 10;
    const SEND_INTERVAL_MS = 1000 / SEND_HZ;

    // Setelah vektor kembali nol, tetap kirim nol sebanyak ini sebelum diam.
    // Satu paket nol bisa hilang di WiFi dermaga; kalau itu paket terakhir,
    // wahana terus bergerak. Sepuluh paket berlebihan secara statistik dan
    // itu memang inti dari perancangan untuk kegagalan.
    const ZERO_REPEAT = 10;

    const DEADZONE_DEFAULT = 0.15;

    // Identitas klien untuk pilot lock. Sengaja dibuat baru tiap muat halaman,
    // tidak disimpan: muat ulang berarti operator memang ingin memulai lagi,
    // dan klaim lama akan kedaluwarsa sendiri di server dalam 3 detik.
    const CLIENT_ID = 'c' + Math.random().toString(36).slice(2, 10);

    // Tiga aksi yang bernilai analog. Sisanya diperlakukan sebagai tombol.
    const MOVE_ACTIONS = ['thro', 'lift', 'yaw'];

    // ─── State ───────────────────────────────────────────────────────
    let caps = null;                 // hasil /api/rov/caps
    let unlocked = false;
    let deadzone = DEADZONE_DEFAULT;
    let keyboardEnabled = true;

    const src = {
        touch: { thro: 0, lift: 0, yaw: 0 },
        keys:  { thro: 0, lift: 0, yaw: 0 },
        pad:   { thro: 0, lift: 0, yaw: 0 },
    };

    let lastSent = { thro: 0, lift: 0, yaw: 0 };
    let zeroBudget = 0;
    let padIndex = null;
    let padRest = null;              // nilai istirahat tiap axis (kalibrasi)
    let padAxisMap = null;           // {thro:{axis,invert}, ...}
    let padPrevSlots = {};           // tepi-tekan per SLOT ("b5", "a4-"), bukan per index
    let padApiBlocked = false;       // getGamepads() dilarang (non-secure context)
    let sendInFlight = false;
    let padId = '';
    let customMap = null;        // {slot: aksi} dari server; null = pakai bawaan
    let learnSlot = null;        // aksi yang sedang menunggu tombol ditekan
    let simMode = false;

    // ═════════════════════════════════════════════════════════════════
    //  Util
    // ═════════════════════════════════════════════════════════════════
    const $ = (id) => document.getElementById(id);

    function status(msg) {
        if (typeof setStatus === 'function') setStatus(msg);
    }

    /** -1..1 → -2..2 diskrit. Cermin dari quantize() di controller_mapper.py. */
    function quantize(v, dz) {
        const a = Math.abs(v);
        if (a < dz) return 0;
        const span = 1.0 - dz;
        const s = span > 1e-6 ? (a - dz) / span : 1.0;
        const lvl = s <= 0.5 ? 1 : 2;
        return v > 0 ? lvl : -lvl;
    }

    /** Gabung sumber: magnitudo terbesar menang, per sumbu. */
    function mergeSources() {
        const out = { thro: 0, lift: 0, yaw: 0 };
        for (const axis of ['thro', 'lift', 'yaw']) {
            let best = 0;
            for (const k of ['touch', 'keys', 'pad']) {
                const v = src[k][axis] || 0;
                if (Math.abs(v) > Math.abs(best)) best = v;
            }
            out[axis] = best;
        }
        return out;
    }

    function capEnabled(id) {
        if (!caps) return false;
        const c = caps.capabilities.find((x) => x.id === id);
        return !!(c && c.enabled);
    }

    // ═════════════════════════════════════════════════════════════════
    //  Pengiriman
    // ═════════════════════════════════════════════════════════════════
    async function pushVector(vec) {
        // Anti-tumpukan: kalau request sebelumnya belum kembali (WiFi tersendat),
        // lewati siklus ini. Menumpuk request 10 Hz di atas jaringan yang sudah
        // lambat hanya memperparah keterlambatan, dan perintah lama yang akhirnya
        // sampai belakangan justru lebih berbahaya daripada tidak sampai.
        if (sendInFlight) return;
        sendInFlight = true;
        try {
            const r = await fetch('/api/rov/move', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ...vec, client_id: CLIENT_ID }),
            });
            const j = await r.json();
            if (!j.ok) {
                if (r.status === 409 && j.pilot) {
                    status('⚠ Klien lain sedang memegang kendali gerak');
                } else {
                    status('❌ ' + (j.error || 'gerak ditolak'));
                }
                zeroSources();
            }
        } catch (e) {
            status('❌ Gerak: koneksi gagal');
        } finally {
            sendInFlight = false;
        }
    }

    function tick() {
        // Urutan ini penting. Sebelum beta8.1 `mergeSources()` dipanggil
        // duluan, jadi nilai pad yang dipakai selalu hasil poll tick
        // SEBELUMNYA — tertinggal 100 ms pada 10 Hz. Itu menumpuk di atas
        // latensi jaringan, TCP, thruster, dan RTSP yang sudah ada.
        pollGamepad();

        // Menolkan pad saja tidak cukup: keyboard dan stick sentuh punya
        // sumbernya sendiri dan tidak lewat pollGamepad() sama sekali.
        // Dijalankan tiap tick, bukan sekali saat masuk mode belajar, karena
        // kunci ROV bisa dibuka dari dashboard SELAGI pemetaan berlangsung.
        if (learnSlot) {
            for (const k of ['touch', 'keys', 'pad']) {
                src[k].thro = src[k].lift = src[k].yaw = 0;
            }
        }

        const raw = mergeSources();
        const vec = {
            thro: quantize(raw.thro, deadzone),
            lift: quantize(raw.lift, deadzone),
            yaw:  quantize(raw.yaw,  deadzone),
        };

        paintVector(vec);

        if (!unlocked) return;

        const moving = vec.thro !== 0 || vec.lift !== 0 || vec.yaw !== 0;
        if (moving) {
            zeroBudget = ZERO_REPEAT;
            lastSent = vec;
            pushVector(vec);
        } else if (zeroBudget > 0) {
            zeroBudget--;
            lastSent = vec;
            pushVector(vec);
        }
    }

    function zeroSources() {
        for (const k of ['touch', 'keys', 'pad']) {
            src[k].thro = src[k].lift = src[k].yaw = 0;
        }
        resetSticks();
    }

    async function estop(reason) {
        zeroSources();
        zeroBudget = 0;
        try {
            const r = await fetch('/api/rov/estop', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ client_id: CLIENT_ID, reason: reason || '' }),
            });
            const j = await r.json();
            if (!r.ok || !j.ok) {
                status('❌ STOP GAGAL — ' +
                       (j.error || 'perintah nol tidak terkonfirmasi'));
                return false;
            }
            status('🛑 STOP — semua sumbu dinolkan');
            return true;
        } catch (e) {
            status('❌ STOP gagal terkirim — putuskan daya kalau perlu');
            return false;
        }
    }

    // ═════════════════════════════════════════════════════════════════
    //  Pad sentuh (pointer events — jempol di HP, mouse di laptop)
    // ═════════════════════════════════════════════════════════════════
    function initStick(padId, knobId, onMove, opts) {
        const pad = $(padId);
        const knob = $(knobId);
        if (!pad || !knob) return;

        const lockX = !!(opts && opts.lockX);
        const lockY = !!(opts && opts.lockY);
        let active = null;

        function place(x, y) {
            const r = pad.getBoundingClientRect();
            const cx = r.width / 2;
            const cy = r.height / 2;
            let dx = lockX ? 0 : (x - r.left - cx) / cx;
            let dy = lockY ? 0 : (y - r.top - cy) / cy;
            const m = Math.hypot(dx, dy);
            if (m > 1) { dx /= m; dy /= m; }
            knob.style.transform =
                `translate(calc(-50% + ${dx * cx * 0.62}px), calc(-50% + ${dy * cy * 0.62}px))`;
            // Sumbu Y layar tumbuh ke bawah, sedangkan "maju" dan "naik" ada
            // di atas. Pembalikan dilakukan di sini, satu kali, bukan
            // disebar ke pemanggil.
            onMove(dx, -dy);
        }

        function release() {
            active = null;
            knob.style.transform = 'translate(-50%, -50%)';
            onMove(0, 0);
        }

        pad.addEventListener('pointerdown', (e) => {
            if (!unlocked) return;
            active = e.pointerId;
            pad.setPointerCapture(e.pointerId);
            place(e.clientX, e.clientY);
            e.preventDefault();
        });
        pad.addEventListener('pointermove', (e) => {
            if (active !== e.pointerId) return;
            place(e.clientX, e.clientY);
            e.preventDefault();
        });
        for (const ev of ['pointerup', 'pointercancel', 'pointerleave']) {
            pad.addEventListener(ev, (e) => {
                if (active !== e.pointerId) return;
                release();
            });
        }

        pad._release = release;
    }

    function resetSticks() {
        for (const id of ['stick-left', 'stick-right']) {
            const pad = $(id);
            if (pad && pad._release) pad._release();
        }
    }

    // ═════════════════════════════════════════════════════════════════
    //  Keyboard
    // ═════════════════════════════════════════════════════════════════
    // Cermin dari profil keyboard di controller_mapper.py, supaya operator
    // yang berlatih di alat pemetaan menemukan tata letak yang sama di sini.
    //
    // Tombol itu digital — tidak ada "setengah didorong". Default memberi
    // level penuh (2); menahan Shift menurunkannya ke level 1. Tanpa itu,
    // kendali keyboard cuma punya dua pilihan: diam atau kencang.
    const KEYMAP = {
        KeyW: ['thro',  1], KeyS: ['thro', -1],
        KeyD: ['yaw',   1], KeyA: ['yaw',  -1],
        ArrowUp: ['lift', 1], ArrowDown: ['lift', -1],
    };
    const KEY_ACTIONS = {
        KeyL: 'light',
        KeyH: 'holdd',
        KeyJ: 'holdy',
        ArrowRight: 'gear_up',
        ArrowLeft: 'gear_down',
        KeyR: 'tilt_up',
        KeyF: 'tilt_down',
        KeyG: 'posture',
        KeyB: 'photo',
        KeyM: 'mark',
    };
    const held = new Set();
    let shiftHeld = false;

    function typingInField() {
        const el = document.activeElement;
        if (!el) return false;
        const tag = el.tagName;
        return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT'
            || el.isContentEditable;
    }

    function recomputeKeys() {
        const mag = shiftHeld ? 0.45 : 1.0;   // 0.45 → kuantisasi ke level 1
        src.keys.thro = src.keys.lift = src.keys.yaw = 0;
        for (const code of held) {
            const m = KEYMAP[code];
            if (m) src.keys[m[0]] = m[1] * mag;
        }
    }

    document.addEventListener('keydown', (e) => {
        if (e.repeat) return;
        shiftHeld = e.shiftKey;

        // Spasi = STOP, dan itu berlaku bahkan saat keyboard dimatikan atau
        // fokus sedang di suatu tempat yang tidak diduga. Satu-satunya
        // pengecualian: saat operator benar-benar sedang mengetik di kolom
        // isian, karena di situ spasi berarti spasi.
        if (e.code === 'Space' && !typingInField()) {
            e.preventDefault();
            estop('spasi');
            return;
        }

        if (!keyboardEnabled || typingInField() || !unlocked) return;

        if (KEYMAP[e.code]) {
            held.add(e.code);
            recomputeKeys();
            e.preventDefault();
            return;
        }
        const act = KEY_ACTIONS[e.code];
        if (act) {
            triggerAction(act);
            e.preventDefault();
        }
    });

    document.addEventListener('keyup', (e) => {
        shiftHeld = e.shiftKey;
        if (held.delete(e.code)) recomputeKeys();
        else recomputeKeys();
    });

    // ═════════════════════════════════════════════════════════════════
    //  Gamepad API
    // ═════════════════════════════════════════════════════════════════
    // Pemetaan tombol mengikuti tata letak aplikasi vendor Geneinno, bukan
    // konvensi gamepad umum. Alasannya keselamatan, bukan estetika: operator
    // yang sudah terlatih di app bawaan tidak boleh harus belajar ulang, dan
    // memori otot yang konsisten itu penting justru saat ada insiden di air.
    // Indeks di bawah memakai "standard gamepad mapping" milik Gamepad API,
    // yang penomorannya BERBEDA dari SDL/pygame yang dipakai
    // controller_mapper.py — di SDL, Back ada di 6, di sini di 8. Yang harus
    // sama antara kedua alat adalah AKSINYA, bukan angkanya.
    const PAD_BUTTONS = {
        10: 'holdy',      // tekan stick kiri  — heading lock
        11: 'holdd',      // tekan stick kanan — depth lock
        2:  'gear_down',  // X
        3:  'gear_up',    // Y
        12: 'tilt_up',    // D-pad ↑
        13: 'tilt_down',  // D-pad ↓
        14: 'posture',    // D-pad ←
        5:  'light',      // RB
        4:  'photo',      // LB
        0:  'mark',       // A
        8:  'estop',      // SELECT
    };

    // ── Selisih yang DISENGAJA dari controller_mapper.py ──────────────
    // Alat pemetaan desktop adalah acuan; di mana web menyimpang, itu karena
    // kapabilitasnya memang berbeda, bukan karena lupa:
    //
    //   LB : desktop 'record'      → web 'photo'
    //        Aplikasi web belum bisa merekam video; hanya ada tangkap frame.
    //   RB : desktop 'light_down'  → web 'light' (toggle)
    //        Lampu masih terverifikasi 0/1, belum bertingkat. Begitu PCAP
    //        membuktikan lampu punya level, ubah kind di rov_caps.py menjadi
    //        'step' dan pisahkan RB/RT seperti di desktop.
    //   LT/RT: desktop mengikatnya sebagai axis_btn (foto / lampu +1)
    //        Diduga digital di pad ini, dan keduanya sudah punya rumah lain.
    //        Dibiarkan kosong sampai kalibrasi memastikan jenisnya.
    //   A  : 'mark' tidak ada di layout vendor sama sekali — vendor tidak
    //        punya konsep waypoint. Ini murni tambahan untuk kerja SAR.

    /**
     * Daftarkan pad — SEKALI per perangkat, bukan tiap tick.
     *
     * `loadProfile()` menembak jaringan dan `calibratePad()` mengambil nilai
     * istirahat; keduanya tidak boleh diulang 10x/detik. Karena itu semua
     * pemasangan ada di sini, dan pemindai di `syncGamepad()` hanya memanggil
     * fungsi ini saat index-nya benar-benar berubah.
     */
    function registerPad(gp) {
        padIndex = gp.index;
        padId = gp.id || '';
        // Seluruh state yang menggambarkan PERANGKAT dibuang di sini.
        // Sampai beta8.1 awal `customMap` tidak ikut dibersihkan, jadi pad
        // yang tidak punya profil mewarisi pemetaan pad sebelumnya —
        // `loadProfile()` hanya menimpa saat profilnya tidak kosong, dan
        // profil kosong tidak menimpa apa pun. Selama padIndex cuma diisi
        // sekali per muat halaman itu jarang kelihatan; setelah rescan
        // panas ada, ganti pad di tengah operasi jadi hal yang wajar.
        customMap = null;
        learnSlot = null;
        padAxisMap = null;
        padRest = null;
        padPrevSlots = {};
        calibratePad();
        loadProfile(padId);
        const std = gp.mapping === 'standard';
        status(`🎮 Gamepad: ${gp.id}` +
               (std ? '' : ' — pemetaan non-standar, verifikasi tombolnya'));
        paintPadStatus(gp.id, std);
        renderLearnTable();
    }

    function forgetPad(reason) {
        if (padIndex === null) return;
        padIndex = null;
        // padId ikut dikosongkan. Kalau tidak, "Simpan Profil" masih bisa
        // menulis profil untuk pad yang sudah dicabut, dan penjaga
        // `padId !== wanted` di loadProfile() masih meloloskan respons pad
        // lama karena padId-nya belum berubah.
        padId = '';
        customMap = null;
        learnSlot = null;
        padAxisMap = null;
        padRest = null;
        padPrevSlots = {};
        src.pad.thro = src.pad.lift = src.pad.yaw = 0;
        // Gamepad tercabut saat wahana sedang bergerak adalah persis situasi
        // yang membuat latch berbahaya. Jangan menunggu deadman server.
        estop(reason || 'gamepad terputus');
        paintPadStatus(null, false);
        renderLearnTable();
    }

    /**
     * Cari pad yang aktif — dipanggil tiap tick.
     *
     * Kenapa tidak cukup mengandalkan event `gamepadconnected`: event itu
     * hanya menyala sekali, pada dokumen yang sedang hidup saat pad memberi
     * input pertamanya. Kalau halaman di-refresh dengan pad sudah menyala,
     * atau tab dibuka di perangkat kedua, event-nya tidak pernah datang —
     * dan sebelum beta8.1 `padIndex` tinggal `null` selamanya, membuat
     * `pollGamepad()` berhenti di baris pertama. Gejalanya: mode belajar
     * diam di "tekan…" tanpa satu pun pesan kesalahan.
     */
    function syncGamepad() {
        if (!navigator.getGamepads) {
            if (!padApiBlocked) { padApiBlocked = true; renderLearnTable(); }
            return null;
        }

        let pads;
        try {
            pads = navigator.getGamepads();
        } catch (e) {
            // Sebagian browser melempar SecurityError kalau halaman bukan
            // secure context — yaitu saat dashboard dibuka dari tablet lewat
            // http://192.168.x.x, bukan https atau localhost. Diamnya harus
            // dijelaskan, bukan dibiarkan tampak seperti pad rusak.
            // Hanya saat BERUBAH. syncGamepad() jalan 10x/detik; merender
            // ulang tabel tiap kali akan membangun ulang DOM terus-menerus.
            if (!padApiBlocked) {
                padApiBlocked = true;
                paintPadStatus(null, false);
                renderLearnTable();
            }
            return null;
        }
        if (!pads) return null;

        let gp = padIndex !== null ? pads[padIndex] : null;
        if (gp && gp.connected === false) gp = null;
        if (gp) return gp;

        for (const p of pads) {
            if (p && p.connected !== false) {
                registerPad(p);
                return p;
            }
        }
        forgetPad('gamepad tidak lagi terlihat');
        return null;
    }

    // Event tetap dipasang: ia datang lebih cepat daripada pemindai, dan
    // pencabutan perlu langsung memicu STOP tanpa menunggu tick berikutnya.
    window.addEventListener('gamepadconnected', (e) => {
        padApiBlocked = false;
        registerPad(e.gamepad);
    });

    window.addEventListener('gamepaddisconnected', (e) => {
        if (e.gamepad.index !== padIndex) return;
        forgetPad('gamepad terputus');
    });

    /**
     * Baca nilai istirahat tiap sumbu untuk memisahkan stick dari trigger.
     *
     * Stick analog beristirahat di ~0.00; trigger analog beristirahat di -1.00.
     * Perbedaan ini TIDAK bisa ditebak dari nama controller — penomoran sumbu
     * berbeda antara mode XInput dan HID langsung, dan pad Geneinno ini pernah
     * terbaca di mode HID (stick 0-3, trigger 4-5).
     *
     * Ini bukan kosmetik. Trigger yang salah terikat ke sumbu gerak berarti
     * perintah penuh terkirim sejak detik pertama tanpa ada yang menyentuh
     * apa pun — kelas kegagalan yang senyap dan tidak terlihat.
     */
    function calibratePad() {
        const gp = navigator.getGamepads ? navigator.getGamepads()[padIndex] : null;
        if (!gp) return;
        padRest = Array.from(gp.axes);
        const sticks = [];
        padRest.forEach((v, i) => { if (Math.abs(v) <= 0.5) sticks.push(i); });

        // Urutan stick yang tersisa setelah trigger disingkirkan hampir selalu
        // LX, LY, RX, RY. Kalau ternyata tidak, tab Monitor di alat pemetaan
        // desktop adalah tempat memastikannya.
        padAxisMap = {
            yaw:  sticks.length > 0 ? { axis: sticks[0], invert: false } : null,
            thro: sticks.length > 1 ? { axis: sticks[1], invert: true  } : null,
            lift: sticks.length > 3 ? { axis: sticks[3], invert: true  } : null,
        };

        const el = $('pad-calib-note');
        if (el) {
            const trig = padRest.map((v, i) => (Math.abs(v) > 0.5 ? i : null))
                                .filter((x) => x !== null);
            el.textContent = `sumbu stick: ${sticks.join(', ') || '—'}` +
                             (trig.length ? ` · trigger: ${trig.join(', ')}` : '');
        }
    }

    /**
     * Terjemahkan slot ("b5", "a1", "a1-") jadi pembacaan sekarang.
     * Akhiran "-" membalik arah sumbu — perlu karena tidak ada kesepakatan
     * antar pad soal apakah stick ke atas itu positif atau negatif.
     */
    function readSlot(gp, slot) {
        if (!slot) return 0;
        const inv = slot.endsWith('-');
        const core = inv ? slot.slice(0, -1) : slot;
        const n = parseInt(core.slice(1), 10);
        if (core[0] === 'b') {
            const b = gp.buttons[n];
            return b ? (b.value !== undefined ? b.value : (b.pressed ? 1 : 0)) : 0;
        }
        if (core[0] === 'a') {
            let v = gp.axes[n];
            if (v === undefined) return 0;
            if (padRest && padRest[n] !== undefined) v -= padRest[n];
            return inv ? -v : v;
        }
        return 0;
    }

    /** Slot mana yang sedang paling menonjol — dipakai mode belajar. */
    function detectPressedSlot(gp) {
        for (let i = 0; i < gp.buttons.length; i++) {
            if (gp.buttons[i] && gp.buttons[i].pressed) return 'b' + i;
        }
        for (let i = 0; i < gp.axes.length; i++) {
            let v = gp.axes[i];
            if (padRest && padRest[i] !== undefined) v -= padRest[i];
            // Ambang tinggi: mode belajar harus menangkap gerakan yang
            // DISENGAJA, bukan stick yang sedikit melenceng dari netral.
            if (Math.abs(v) > 0.7) return 'a' + i + (v < 0 ? '-' : '');
        }
        return null;
    }

    /**
     * Gabungkan pemetaan bawaan dengan override kustom — DUA ARAH.
     *
     * Sebelum beta8.1 blok custom diakhiri `return`, jadi satu binding
     * tersimpan mematikan stick DAN seluruh tombol bawaan sekaligus.
     * Sekadar membuang `return` juga salah: kalau bawaan dan kustom
     * dijalankan berdampingan, memetakan lampu ke B menghasilkan RB *dan* B
     * sama-sama menyalakan lampu, dan operator tidak punya cara melihatnya.
     *
     * Binding bawaan dilepas kalau salah satu dari dua hal ini terjadi:
     *
     *   1. AKSInya dipetakan ulang.  {"b1":"light"} melepas RB→light,
     *      supaya satu aksi tidak punya dua tombol.
     *
     *   2. SLOT FISIKnya dipakai aksi lain.  {"b5":"mark"} melepas
     *      b5→light, supaya satu tombol tidak memicu dua aksi.
     *
     * Arah kedua sempat tertinggal di beta8.1 awal, dan bukan cuma soal
     * kerapian. {"b5":"thro"} membuat b5 masuk ke jalur sumbu SEKALIGUS
     * tetap ada sebagai b5→light di jalur tombol — satu tombol fisik
     * menggerakkan wahana dan menyalakan lampu berbarengan. Penjagaan
     * tepi-tekan `padPrevSlots` tidak menolong di situ karena kedua jalur
     * dibaca terpisah.
     *
     * "a1" dan "a1-" adalah SUMBU FISIK YANG SAMA — akhiran itu cuma arah
     * baca. Jadi perbandingan slot selalu memakai bentuk telanjangnya,
     * kalau tidak {"a1-":"light"} akan lolos berdampingan dengan fallback
     * thro di sumbu 1.
     */
    function effectiveBindings() {
        const physicalSlot = (sl) => (sl.endsWith('-') ? sl.slice(0, -1) : sl);
        const custom = customMap || {};
        const overridden = new Set(Object.values(custom));
        const usedSlots = new Set(Object.keys(custom).map(physicalSlot));

        const axes = {};
        for (const act of MOVE_ACTIONS) {
            const slots = Object.keys(custom).filter((sl) => custom[sl] === act);
            if (slots.length) { axes[act] = { slots }; continue; }
            const fb = padAxisMap ? padAxisMap[act] : null;
            // Sumbu bawaannya sudah diambil aksi lain: lepaskan, jangan
            // dibiarkan membaca sumbu yang sama diam-diam.
            axes[act] = { fallback: (fb && usedSlots.has('a' + fb.axis)) ? null : fb };
        }

        // Satu daftar tombol untuk keduanya. Ini juga yang membuat deteksi
        // tepi-tekan aman: kuncinya slot, jadi tidak mungkin satu tombol
        // punya dua penghitung status yang bergerak sendiri-sendiri.
        const buttons = [];
        for (const [slot, act] of Object.entries(custom)) {
            if (!MOVE_ACTIONS.includes(act) && act !== 'none') buttons.push([slot, act]);
        }
        for (const [idx, act] of Object.entries(PAD_BUTTONS)) {
            if (overridden.has(act)) continue;
            if (usedSlots.has('b' + idx)) continue;
            buttons.push(['b' + idx, act]);
        }
        return { axes, buttons };
    }

    function pollGamepad() {
        const gp = syncGamepad();
        if (!gp) return;

        // ── Mode belajar ──────────────────────────────────────────────
        // Mendahului segalanya: selama memetakan, tidak ada satu pun input
        // yang boleh diteruskan sebagai perintah. Menekan tombol untuk
        // mengikatnya ke "maju" tidak boleh sekaligus membuat wahana maju.
        if (learnSlot) {
            // Menolkan, bukan sekadar `return`. Sampai beta8.1a jalur ini
            // keluar tanpa menyentuh src.pad, jadi nilai stick dari tick
            // SEBELUM mode belajar dinyalakan tetap tertinggal di sana dan
            // terus dikirim ke /api/rov/move selama pemetaan berlangsung.
            src.pad.thro = src.pad.lift = src.pad.yaw = 0;
            const slot = detectPressedSlot(gp);
            if (slot) bindSlot(slot);
            return;
        }

        const bind = effectiveBindings();

        for (const act of MOVE_ACTIONS) {
            const b = bind.axes[act];
            let v = 0;
            if (b.slots) {
                for (const sl of b.slots) {
                    const r = readSlot(gp, sl);
                    if (Math.abs(r) > Math.abs(v)) v = r;
                }
            } else if (b.fallback && gp.axes[b.fallback.axis] !== undefined) {
                v = gp.axes[b.fallback.axis];
                if (padRest && padRest[b.fallback.axis] !== undefined) {
                    v -= padRest[b.fallback.axis];
                }
                if (b.fallback.invert) v = -v;
            }
            src.pad[act] = v;
        }

        for (const [slot, act] of bind.buttons) {
            const pressed = readSlot(gp, slot) > 0.5;
            const was = !!padPrevSlots[slot];
            padPrevSlots[slot] = pressed;
            if (!pressed || was) continue;          // hanya tepi tekan
            if (act === 'estop') { estop('tombol STOP di gamepad'); continue; }
            if (!unlocked) continue;
            triggerAction(act);
        }
    }

    // ═════════════════════════════════════════════════════════════════
    //  Aksi non-gerak
    // ═════════════════════════════════════════════════════════════════
    const toggleState = { light: 0, holdd: 0, holdy: 0 };

    async function triggerAction(act) {
        if (act === 'photo') {
            window.open('/api/screenshot', '_blank');
            return;
        }
        if (act === 'mark') {
            const btn = $('btn-mark');
            if (btn) btn.click();
            return;
        }

        // Aksi yang belum terverifikasi PCAP: jangan diam-diam tidak terjadi
        // apa-apa. Operator yang menekan tombol dan tidak melihat reaksi akan
        // menekannya lagi, lebih keras, dan menyimpulkan alatnya rusak.
        const capId = act.startsWith('gear') ? 'gear'
                    : act.startsWith('tilt') ? 'tilt'
                    : act === 'posture' ? 'posture'
                    : act;
        if (!capEnabled(capId)) {
            const c = caps && caps.capabilities.find((x) => x.id === capId);
            status('⚠ ' + (c ? c.label : act) + ' belum aktif — menunggu PCAP');
            return;
        }

        if (act in toggleState) {
            const next = toggleState[act] ? 0 : 1;
            if (typeof sendRovCommand === 'function') {
                const ok = await sendRovCommand(act, next);
                if (ok) toggleState[act] = next;
            }
            return;
        }

        status('⚠ Aksi "' + act + '" belum punya penanganan');
    }


    // ═════════════════════════════════════════════════════════════════
    //  Pemetaan kustom (mode belajar)
    // ═════════════════════════════════════════════════════════════════
    // Pemetaan bawaan mengasumsikan "standard gamepad mapping". Pad yang
    // tidak melaporkan dirinya standar — dan pad Xbox pun bisa begitu di
    // sebagian browser — akan memberi penomoran yang tidak cocok, dengan
    // gejala tombol yang melakukan hal yang salah, bukan tidak melakukan
    // apa-apa. Menebak-nebak indeks yang benar bukan pekerjaan operator.
    // Mode belajar menggantinya: pilih aksi, tekan tombolnya, selesai.

    const LEARNABLE = [
        ['thro',      'Maju (dorong stick maju)'],
        ['yaw',       'Putar kanan'],
        ['lift',      'Naik'],
        ['holdy',     'Heading lock'],
        ['holdd',     'Depth lock'],
        ['light',     'Lampu'],
        ['gear_up',   'Gear +'],
        ['gear_down', 'Gear −'],
        ['tilt_up',   'Tilt naik'],
        ['tilt_down', 'Tilt turun'],
        ['posture',   'Posture recovery'],
        ['photo',     'Ambil foto'],
        ['mark',      'Tandai waypoint'],
        ['estop',     'STOP'],
    ];

    function bindSlot(slot) {
        if (!learnSlot) return;
        customMap = customMap || {};
        // Satu slot hanya boleh punya satu aksi; kalau tombol ini sudah
        // dipakai, lepaskan ikatan lamanya. Tanpa ini, satu tombol bisa
        // memicu dua hal sekaligus dan tidak ada cara melihatnya di UI.
        delete customMap[slot];
        for (const s of Object.keys(customMap)) {
            if (customMap[s] === learnSlot) delete customMap[s];
        }
        customMap[slot] = learnSlot;
        status(`✓ ${learnSlot} → ${slot}`);
        learnSlot = null;
        renderLearnTable();
    }

    function renderLearnTable() {
        const wrap = $('learn-table');
        if (!wrap) return;
        const rev = {};
        for (const [slot, act] of Object.entries(customMap || {})) rev[act] = slot;

        wrap.innerHTML = '';

        // Mode belajar tanpa pad adalah jalan buntu yang senyap: tombol
        // "petakan" bisa ditekan, barisnya berubah jadi "tekan…", dan tidak
        // ada satu pun tombol fisik yang bisa mengakhirinya. Katakan sejak
        // awal, dan katakan APA yang harus dilakukan.
        if (padIndex === null) {
            const warn = document.createElement('div');
            warn.className = 'learn-warn';
            warn.textContent = padApiBlocked
                ? '⚠ Browser menolak akses gamepad. Halaman ini bukan secure ' +
                  'context — buka lewat http://localhost di laptop, atau ' +
                  'pakai Chrome kalau mengakses dari tablet via alamat LAN.'
                : '⚠ Belum ada gamepad terdeteksi. Nyalakan pad, lalu tekan ' +
                  'satu tombol apa saja supaya browser mengenalinya.';
            wrap.appendChild(warn);
        }

        for (const [act, label] of LEARNABLE) {
            const row = document.createElement('div');
            row.className = 'learn-row';
            const waiting = learnSlot === act;
            row.innerHTML =
                `<span class="lr-label">${label}</span>` +
                `<span class="lr-slot${rev[act] ? '' : ' unset'}">` +
                `${waiting ? 'tekan…' : (rev[act] || '—')}</span>`;
            const b = document.createElement('button');
            b.className = 'lr-btn' + (waiting ? ' waiting' : '');
            b.textContent = waiting ? 'batal' : 'petakan';
            b.addEventListener('click', () => {
                if (waiting) { learnSlot = null; renderLearnTable(); return; }
                // Interlock. Memetakan berarti menekan tiap tombol berkali-kali
                // dengan sengaja, dan itu tidak boleh dilakukan sementara ada
                // jalur hidup ke wahana. Petunjuk di UI sudah menyuruh
                // menyalakan simulasi dulu — sekarang aturannya ditegakkan,
                // bukan sekadar disarankan.
                if (unlocked && !simMode) {
                    status('⚠ Kunci kendali ROV dulu, atau nyalakan mode ' +
                           'simulasi, sebelum memetakan tombol');
                    return;
                }
                zeroSources();
                learnSlot = act;
                renderLearnTable();
            });
            row.appendChild(b);
            wrap.appendChild(row);
        }
    }

    /**
     * Ambil profil untuk SATU perangkat tertentu.
     *
     * `requestedId` dibawa eksplisit, bukan dibaca dari `padId` saat respons
     * tiba. Kalau pad dicabut dan diganti selagi fetch berjalan, respons pad
     * lama bisa datang belakangan dan memasang pemetaan milik perangkat yang
     * sudah tidak terhubung. Dengan rescan panas, urutan itu bukan hal
     * teoretis lagi.
     *
     * Profil kosong berarti "pakai bawaan" — dan itu harus DITULIS, bukan
     * dilewati. Melewatinya berarti mempertahankan pemetaan pad sebelumnya.
     */
    async function loadProfile(requestedId) {
        const wanted = requestedId || padId;
        if (!wanted) return;
        try {
            const r = await fetch('/api/rov/mapping?id=' + encodeURIComponent(wanted));
            const j = await r.json();
            if (padId !== wanted) return;      // pad sudah berganti
            const m = j.profile && j.profile.mapping;
            const n = m ? Object.keys(m).length : 0;
            customMap = n ? m : null;
            if (n) status('🎮 Profil tersimpan dimuat — ' + n + ' binding');
            renderLearnTable();
        } catch (e) { /* profil opsional; bawaan tetap jalan */ }
    }

    async function saveProfile() {
        if (!padId) { status('❌ Tidak ada gamepad untuk disimpan'); return; }
        try {
            const r = await fetch('/api/rov/mapping', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id: padId, label: padId,
                                       mapping: customMap || {} }),
            });
            const j = await r.json();
            status(j.ok ? '💾 Profil controller tersimpan'
                        : '❌ ' + (j.error || 'gagal menyimpan'));
        } catch (e) { status('❌ Gagal menyimpan profil'); }
    }

    async function resetProfile() {
        customMap = null;
        learnSlot = null;
        renderLearnTable();
        if (padId) {
            try {
                await fetch('/api/rov/mapping?id=' + encodeURIComponent(padId),
                            { method: 'DELETE' });
            } catch (e) { /* diabaikan */ }
        }
        status('↺ Kembali ke pemetaan bawaan');
    }

    // ═════════════════════════════════════════════════════════════════
    //  Mode simulasi
    // ═════════════════════════════════════════════════════════════════
    async function setSim(on) {
        try {
            const r = await fetch('/api/rov/sim', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ sim: !!on }),
            });
            const j = await r.json();
            // Backend mengembalikan state lama saat STOP transisi gagal;
            // pasang itu kembali ke checkbox agar UI tidak berbohong.
            applySim(j.sim);
            if (!r.ok || !j.ok) {
                status('❌ Mode simulasi tidak diubah — ' +
                       (j.error || 'STOP ROV gagal'));
                return false;
            }
            return true;
        } catch (e) {
            status('❌ Gagal mengubah mode simulasi');
            return false;
        }
    }

    function applySim(on) {
        const changed = simMode !== !!on;
        simMode = !!on;
        const cb = $('ctrl-sim');
        if (cb && cb.checked !== simMode) cb.checked = simMode;
        const banner = $('sim-banner');
        if (banner) banner.style.display = simMode ? 'block' : 'none';
        const panel = $('pad-panel');
        if (panel) panel.classList.toggle('sim', simMode);
        // Pesan status hanya saat BERUBAH. Fungsi ini sekarang juga dipanggil
        // dari sinkronisasi state awal dan dari heartbeat, dan mengumumkan
        // "mode nyata" tiap beberapa detik akan menenggelamkan pesan lain
        // sampai peringatan yang sungguhan pun tidak terbaca lagi.
        if (changed) {
            status(simMode
                ? '🧪 MODE SIMULASI — perintah tidak dikirim ke wahana'
                : '⚠ Mode nyata — perintah SUNGGUHAN ke wahana');
        }
    }

    // ═════════════════════════════════════════════════════════════════
    //  Render UI
    // ═════════════════════════════════════════════════════════════════
    function paintVector(vec) {
        const el = $('vec-readout');
        if (el) {
            el.textContent = `thro:${vec.thro >= 0 ? ' ' : ''}${vec.thro}  ` +
                             `lift:${vec.lift >= 0 ? ' ' : ''}${vec.lift}  ` +
                             `yaw:${vec.yaw >= 0 ? ' ' : ''}${vec.yaw}`;
            el.classList.toggle('moving',
                vec.thro !== 0 || vec.lift !== 0 || vec.yaw !== 0);
        }
        for (const axis of ['thro', 'lift', 'yaw']) {
            const bar = $('bar-' + axis);
            if (!bar) continue;
            const pct = (vec[axis] / 2) * 50;
            bar.style.left = (pct >= 0 ? 50 : 50 + pct) + '%';
            bar.style.width = Math.abs(pct) + '%';
        }
    }

    function paintPadStatus(name, standard) {
        const el = $('pad-status');
        if (!el) return;
        if (!name) {
            el.textContent = '○ Gamepad tidak terhubung';
            el.className = 'pad-status';
            return;
        }
        el.textContent = '● ' + name.slice(0, 42) +
                         (standard ? '' : ' (non-standar)');
        el.className = 'pad-status on';
    }

    /**
     * Bangun deretan tombol DARI /api/rov/caps.
     *
     * Inilah yang membuat "auto-enable" bekerja: tombol yang belum terverifikasi
     * tetap dirender, tapi mati dan disertai alasannya. Saat satu baris di
     * rov_caps.py diubah setelah PCAP menjawab, tombolnya hidup di sini tanpa
     * ada HTML yang perlu disunting.
     */
    function buildActionButtons() {
        const wrap = $('pad-actions');
        if (!wrap || !caps) return;
        wrap.innerHTML = '';

        const layout = [
            { id: 'light',   act: 'light',     icon: '💡', hint: 'RB / L' },
            { id: 'holdd',   act: 'holdd',     icon: '⚓', hint: 'L3 / H' },
            { id: 'holdy',   act: 'holdy',     icon: '🧭', hint: 'R3 / J' },
            { id: 'gear',    act: 'gear_down', icon: '🐢', hint: 'X / ←', label: 'Gear −' },
            { id: 'gear',    act: 'gear_up',   icon: '🐇', hint: 'Y / →', label: 'Gear +' },
            { id: 'tilt',    act: 'tilt_up',   icon: '⬆', hint: 'D↑ / R', label: 'Tilt +' },
            { id: 'tilt',    act: 'tilt_down', icon: '⬇', hint: 'D↓ / F', label: 'Tilt −' },
            { id: 'posture', act: 'posture',   icon: '⚖', hint: 'D← / G' },
            { id: 'lateral', act: null,        icon: '↔', hint: '—' },
        ];

        for (const item of layout) {
            const cap = caps.capabilities.find((c) => c.id === item.id);
            if (!cap) continue;
            const btn = document.createElement('button');
            btn.className = 'pad-act-btn';
            btn.innerHTML =
                `<span class="pa-icon">${item.icon}</span>` +
                `<span class="pa-label">${item.label || cap.label}</span>` +
                `<span class="pa-hint">${item.hint}</span>`;
            if (!cap.enabled || !item.act) {
                btn.disabled = true;
                btn.classList.add('pending-' + (cap.reason || 'pcap'));
                btn.title = cap.note || 'Belum aktif';
            } else {
                btn.dataset.act = item.act;
                btn.dataset.cap = cap.id;
                btn.addEventListener('click', () => triggerAction(item.act));
            }
            wrap.appendChild(btn);
        }
    }

    /** Sinkronkan kilau tombol toggle dengan telemetri, bukan dengan tebakan lokal. */
    function syncToggles(p) {
        const map = { light: 'L', holdd: 'HD', holdy: 'HH' };
        for (const [act, field] of Object.entries(map)) {
            if (p[field] === undefined) continue;
            const on = String(p[field]) !== '0' && String(p[field]) !== '';
            toggleState[act] = on ? 1 : 0;
            const btn = document.querySelector(`.pad-act-btn[data-act="${act}"]`);
            if (btn) btn.classList.toggle('on', on);
        }
    }

    function setUnlocked(v) {
        unlocked = !!v;
        const panel = $('pad-panel');
        if (panel) panel.classList.toggle('locked', !unlocked);
        if (!unlocked) zeroSources();
    }

    // ═════════════════════════════════════════════════════════════════
    //  Deadman sisi klien
    // ═════════════════════════════════════════════════════════════════
    // Ini SELAIN watchdog server, bukan penggantinya. Yang di sini menangkap
    // kasus yang browser masih hidup untuk melaporkannya sendiri — tab
    // disembunyikan, jendela kehilangan fokus, halaman ditutup. Yang di server
    // menangkap kasus yang browser tidak akan pernah bisa melaporkan.
    document.addEventListener('visibilitychange', () => {
        if (document.hidden && unlocked) estop('tab disembunyikan');
    });
    window.addEventListener('blur', () => {
        held.clear();
        recomputeKeys();
    });
    window.addEventListener('pagehide', () => {
        if (!unlocked) return;
        // fetch biasa sering dibatalkan saat halaman dibongkar; sendBeacon
        // dirancang persis untuk pesan terakhir semacam ini.
        if (navigator.sendBeacon) {
            navigator.sendBeacon('/api/rov/estop',
                new Blob([JSON.stringify({ client_id: CLIENT_ID })],
                         { type: 'application/json' }));
        }
    });

    // ═════════════════════════════════════════════════════════════════
    //  Boot
    // ═════════════════════════════════════════════════════════════════
    async function loadCaps() {
        try {
            const r = await fetch('/api/rov/caps');
            caps = await r.json();
            buildActionButtons();
            const pending = caps.capabilities.filter((c) => !c.enabled);
            const note = $('caps-note');
            if (note) {
                const pcap = pending.filter((c) => c.reason === 'pcap');
                note.textContent = pcap.length
                    ? `${pcap.length} aksi menunggu verifikasi PCAP: ` +
                      pcap.map((c) => c.label).join(', ')
                    : 'Semua aksi terverifikasi.';
            }
        } catch (e) {
            status('❌ Gagal memuat kapabilitas ROV');
        }
    }

    function init() {
        initStick('stick-left', 'knob-left', (x, y) => {
            src.touch.yaw = x;
            src.touch.thro = y;
        });
        initStick('stick-right', 'knob-right', (x, y) => {
            // Sumbu X pad kanan sengaja dikunci: itu `lateral`, dan wahana ini
            // tidak punya thruster samping. Membiarkannya bergerak akan
            // mengajari operator gerakan yang tidak pernah menghasilkan apa pun.
            src.touch.lift = y;
        }, { lockX: true });

        const dz = $('ctrl-deadzone');
        if (dz) {
            dz.addEventListener('input', () => {
                deadzone = parseInt(dz.value, 10) / 100;
                const lbl = $('deadzone-label');
                if (lbl) lbl.textContent = dz.value + '%';
            });
        }

        const kb = $('ctrl-keyboard');
        if (kb) {
            keyboardEnabled = kb.checked;
            kb.addEventListener('change', () => {
                keyboardEnabled = kb.checked;
                held.clear();
                recomputeKeys();
            });
        }

        const cal = $('btn-pad-calib');
        if (cal) cal.addEventListener('click', () => {
            calibratePad();
            status('🎯 Kalibrasi netral — pastikan tangan lepas dari stick');
        });

        const stop = $('btn-estop');
        if (stop) stop.addEventListener('click', () => estop('tombol STOP'));

        // Checkbox unlock SENGAJA tidak didengarkan di sini.
        //
        // Sampai beta8, controls.js dan dashboard.js sama-sama memasang
        // listener 'change'. Milik controls.js berjalan sinkron dan langsung
        // menyetel unlocked=true dari nilai checkbox; milik dashboard.js baru
        // menyelesaikan POST-nya setelah `await`. Kalau server MENOLAK unlock,
        // dashboard.js mengembalikan checkbox ke posisi semula — tapi menyetel
        // `.checked` lewat script tidak memicu 'change', jadi controls.js tidak
        // pernah tahu dan tetap menganggap dirinya terbuka. Panel tetap
        // terbuka, perintah gerak tetap terkirim, dan semuanya ditolak server
        // satu per satu.
        //
        // Sekarang satu-satunya jalan masuk adalah dashboard.js →
        // applyUnlockToUI() → RovControls.setUnlocked(). Status server yang
        // authoritative, bukan posisi checkbox di satu browser.

        const simCb = $('ctrl-sim');
        if (simCb) simCb.addEventListener('change', () => setSim(simCb.checked));

        const bSave = $('btn-map-save');
        if (bSave) bSave.addEventListener('click', saveProfile);
        const bReset = $('btn-map-reset');
        if (bReset) bReset.addEventListener('click', resetProfile);

        renderLearnTable();
        loadCaps();
        setInterval(tick, SEND_INTERVAL_MS);
    }

    // Dipanggil dashboard.js saat status unlock / telemetri berubah.
    window.RovControls = {
        setUnlocked,
        syncToggles,
        applySim,
        _readSlot: readSlot,
        _bindSlot: bindSlot,
        _setLearn: (a) => { learnSlot = a; },
        _map: () => customMap,
        estop,
        clientId: CLIENT_ID,
        _quantize: quantize,        // diekspos untuk uji
        _merge: mergeSources,
        _src: src,
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();

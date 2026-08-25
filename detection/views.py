"""
HTTP views — endpoint yang diakses langsung lewat browser/fetch.

Endpoint:
    GET  /              dashboard HTML
    GET  /video         MJPEG stream (multipart/x-mixed-replace) — ASYNC
    GET  /api/state     full state snapshot (control + GPS + frame_age)
    POST /api/control   update control flags (HOP, YOLO, dst)
    GET  /api/waypoints list semua waypoint
    POST /api/waypoint  manual mark waypoint sekarang
    POST /api/waypoints/clear  hapus semua waypoint
    GET  /api/screenshot download frame terakhir sebagai JPEG
    GET  /api/export    download GPX file
"""
import asyncio
import io
import json
import time
from datetime import datetime

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.http import (
    HttpResponse, JsonResponse, StreamingHttpResponse,
    HttpResponseBadRequest, HttpResponseNotAllowed,
)
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import controller_profiles, rov_caps
from .state import state, broadcast, gps_fix_is_fresh


# ─── Dashboard ───────────────────────────────────────────────────────────
def dashboard(request):
    return render(request, "detection/dashboard.html")


# ─── MJPEG stream (ASYNC) ────────────────────────────────────────────────
async def video_stream(request):
    """
    MJPEG multipart stream. Browser tinggal pasang di:
        <img src="/video">

    IMPORTANT: harus async generator karena Django ASGI (Daphne) tidak bisa
    konsumsi sync iterator tak berhingga — akan hang/timeout dengan warning:
        "StreamingHttpResponse must consume synchronous iterators in order
         to serve them asynchronously."

    Pattern: async generator + await asyncio.sleep() untuk rate limit,
    bukan time.sleep() (blocking event loop).
    """
    boundary = b"frame"

    async def generator():
        target_interval = 1.0 / 30   # max 30fps ke client
        last_sent_count = -1

        while True:
            jpeg, count = state.get_frame_with_id()
            if jpeg is None:
                # Belum ada frame — sleep async, jangan block event loop
                await asyncio.sleep(0.1)
                continue

            # Dedup: skip kalau frame ini sudah dikirim (counter sama).
            # Pakai counter monotonik, bukan id(jpeg) yang bisa reuse GC.
            if count == last_sent_count:
                await asyncio.sleep(target_interval)
                continue
            last_sent_count = count

            yield (
                b"--" + boundary + b"\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                + jpeg + b"\r\n"
            )
            await asyncio.sleep(target_interval)

    response = StreamingHttpResponse(
        generator(),
        content_type=f"multipart/x-mixed-replace; boundary={boundary.decode()}",
    )
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Pragma"] = "no-cache"
    return response


# ─── State snapshot ──────────────────────────────────────────────────────
def api_state(request):
    """Snapshot lengkap untuk inisialisasi UI saat halaman load pertama kali."""
    from django.conf import settings
    heading_source, heading = state.active_heading()
    worker = state.capture_worker
    capture_stats = (worker.get_stats()
                     if worker is not None and hasattr(worker, "get_stats")
                     else None)
    return JsonResponse({
        "control": state.get_control(),
        "gps": state.get_gps(),
        "rov": state.get_rov(),
        "capture": capture_stats,
        "heading": heading,
        "heading_source": heading_source,
        "waypoints": state.get_waypoints(),
        "frame_age_s": state.frame_age(),
        "auto_waypoint_enabled": state.auto_waypoint_enabled,
        "mark_on_detect_enabled": state.mark_on_detect_enabled,
        "config": {
            "rtsp_label": _format_rtsp_label(settings.ROV_RTSP_URL),
            "gps_label": _format_gps_label(
                settings.ROV_GPS_PORT, settings.ROV_GPS_BAUD,
            ),
            "rov_label": f"{settings.ROV_HOST}:{settings.ROV_PORT}",
            "rov_enabled": settings.ROV_TELEMETRY_ENABLED,
            "control_requires_token": bool(settings.ROV_CONTROL_TOKEN),
        },
    })


def _format_rtsp_label(src: str) -> str:
    """Tampilan ringkas sumber video untuk UI."""
    if src.isdigit():
        return f"Kamera lokal index {src}"
    if src.startswith("rtsp://"):
        return src
    return src


def _format_gps_label(port: str, baud: int) -> str:
    if port.upper() == "DUMMY":
        return "DUMMY (simulasi)"
    return f"{port} @ {baud}bps"


# ─── Screenshot ──────────────────────────────────────────────────────────
def api_screenshot(request):
    """Download frame terakhir sebagai JPEG."""
    jpeg = state.get_frame()
    if jpeg is None:
        return JsonResponse(
            {"ok": False, "error": "Belum ada frame"},
            status=400,
        )
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"ROVSS_{timestamp}.jpg"
    response = HttpResponse(jpeg, content_type="image/jpeg")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# ─── Control ─────────────────────────────────────────────────────────────
@csrf_exempt
@require_http_methods(["POST"])
def api_control(request):
    """
    Update control flags. Body JSON: {"hop_enabled": true, ...}
    Hanya field yang dikirim yang di-update.
    """
    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON")

    allowed = {
        "hop_enabled", "clahe_enabled", "dehaze_enabled",
        "wb_enabled", "yolo_enabled", "hop_depth",
        "auto_waypoint_enabled", "mark_on_detect_enabled",
    }
    update = {k: v for k, v in data.items() if k in allowed}

    # Validasi hop_depth
    if "hop_depth" in update:
        try:
            d = int(update["hop_depth"])
            if not 1 <= d <= 7:
                return HttpResponseBadRequest("hop_depth must be 1-7")
            update["hop_depth"] = d
        except (TypeError, ValueError):
            return HttpResponseBadRequest("hop_depth must be int")

    state.update_control(**update)
    current = state.get_control()
    # Broadcast supaya semua browser tetap in-sync
    broadcast("control_updated", {
        **current,
        "auto_waypoint_enabled": state.auto_waypoint_enabled,
        "mark_on_detect_enabled": state.mark_on_detect_enabled,
    })
    return JsonResponse({"ok": True, "control": current})


# ─── Waypoints ───────────────────────────────────────────────────────────
def api_waypoints_list(request):
    return JsonResponse({"waypoints": state.get_waypoints()})


@csrf_exempt
@require_http_methods(["POST"])
def api_waypoint_mark(request):
    """Tandai waypoint manual di posisi GPS saat ini (harus fix fresh)."""
    gps = state.get_gps()
    if not gps_fix_is_fresh(gps):
        return JsonResponse(
            {"ok": False,
             "error": "GPS tidak aktif atau fix sudah kedaluwarsa — "
                      "waypoint tidak dicatat untuk menghindari koordinat lama"},
            status=400,
        )
    wp = state.add_waypoint(gps["lat"], gps["lon"], is_detect=False)
    broadcast("waypoint_added", wp.to_dict())
    return JsonResponse({"ok": True, "waypoint": wp.to_dict()})


@csrf_exempt
@require_http_methods(["POST"])
def api_waypoints_clear(request):
    state.clear_waypoints()
    broadcast("waypoints_cleared", {})
    return JsonResponse({"ok": True})


# ─── Kontrol & telemetri ROV ─────────────────────────────────────────────
#
# CATATAN KEAMANAN — ini beda mendasar dengan versi desktop.
#
# Di PyQt5, kontrol ROV cuma bisa disentuh orang yang duduk di depan laptop.
# Begitu dashboard ini melayani LAN, SIAPA PUN di WiFi ROV bisa POST ke
# endpoint ini dan menggerakkan wahana. Karena itu ada dua lapis:
#
#   1. Unlock — state SERVER (bukan checkbox browser). Checkbox di klien
#      hanya mengubah flag ini lewat endpoint; perintah gerak ditolak selama
#      flag-nya False, tak peduli klien mengirim apa.
#   2. Token — kalau ROV_CONTROL_TOKEN di-set, tiap perintah harus membawa
#      header X-ROV-Token yang cocok. Kosong = tanpa token (cukup untuk uji
#      kolam tertutup, JANGAN untuk lapangan terbuka).
#
# Perintah gerak (lift/thro/yaw) sengaja TIDAK diekspos lewat HTTP di versi
# ini. Kendali gerak butuh laju tinggi dan dead-man switch — request/response
# HTTP bukan transport yang tepat, dan tanpa dead-man switch ROV akan menahan
# perintah terakhir kalau browser mati di tengah gerakan.

# Daftar perintah TIDAK lagi ditulis tangan di sini. Diturunkan dari
# detection/rov_caps.py supaya tidak mungkin ada aksi yang hidup di UI tapi
# ditolak backend (atau sebaliknya) — dua daftar terpisah pasti akan berselisih
# suatu saat, dan selisihnya baru ketahuan di dermaga.
def _rov_allowed_commands() -> dict:
    return rov_caps.allowed_commands()


def _rov_token_ok(request) -> bool:
    from django.conf import settings
    expected = settings.ROV_CONTROL_TOKEN
    if not expected:
        return True
    return request.headers.get("X-ROV-Token", "") == expected


@csrf_exempt
@require_http_methods(["POST"])
def api_rov_unlock(request):
    """Buka / kunci kontrol ROV. Body: {"unlocked": true}."""
    if not _rov_token_ok(request):
        return JsonResponse({"ok": False, "error": "Token salah"}, status=403)
    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON")

    unlocked = bool(data.get("unlocked", False))
    with state._rov_lock:
        state.rov_control_unlocked = unlocked
    broadcast("rov_unlock", {"unlocked": unlocked})
    return JsonResponse({"ok": True, "unlocked": unlocked})


@csrf_exempt
@require_http_methods(["POST"])
def api_rov_command(request):
    """
    Kirim satu perintah ke ROV. Body: {"key": "light", "value": 1}.
    Hanya perintah di _ROV_ALLOWED_COMMANDS yang diterima.
    """
    if not _rov_token_ok(request):
        return JsonResponse({"ok": False, "error": "Token salah"}, status=403)

    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON")

    allowed = _rov_allowed_commands()
    key = str(data.get("key", ""))

    # Sumbu gerak TIDAK boleh lewat sini, meski ada di daftar kapabilitas.
    #
    # Endpoint ini mengirim satu pasang kunci-nilai langsung ke soket. Untuk
    # toggle itu tepat, tapi untuk gerak berarti melewati dua pengaman
    # sekaligus: pilot lock (dua klien bisa menyetir bersamaan) dan pencatatan
    # `state.record_move()` yang menjadi denyut bagi watchdog deadman. Tanpa
    # denyut itu, deadman tidak akan pernah memicu — wahana bisa terus
    # bergerak selamanya setelah kliennya mati.
    #
    # Jadi satu-satunya jalan menuju thro/lift/yaw adalah /api/rov/move.
    if key in rov_caps.move_keys().values():
        return JsonResponse(
            {"ok": False,
             "error": f"'{key}' adalah sumbu gerak — gunakan /api/rov/move "
                      f"supaya pilot lock dan deadman ikut berlaku"},
            status=400,
        )

    if key not in allowed:
        cap = None
        for c in rov_caps.CAPABILITIES.values():
            if c.key == key or c.id == key:
                cap = c
                break
        if cap is not None and not cap.usable:
            # Bedakan "belum diverifikasi" dari "tidak dikenal sama sekali".
            # Yang pertama punya jalan keluar yang jelas (jalankan PCAP);
            # pesan generik menyembunyikan itu.
            return JsonResponse(
                {"ok": False,
                 "error": f"'{cap.label}' belum aktif — {cap.note or cap.reason}",
                 "reason": cap.reason},
                status=400,
            )
        return JsonResponse(
            {"ok": False, "error": f"Perintah '{key}' tidak diizinkan"},
            status=400,
        )

    try:
        value = int(data.get("value"))
    except (TypeError, ValueError):
        return JsonResponse(
            {"ok": False, "error": "value harus integer"}, status=400,
        )
    if value not in allowed[key]:
        return JsonResponse(
            {"ok": False,
             "error": f"value {value} di luar rentang untuk '{key}'"},
            status=400,
        )

    if not state.rov_control_unlocked:
        return JsonResponse(
            {"ok": False, "error": "Kontrol ROV terkunci"}, status=409,
        )

    if state.rov_sim_mode:
        broadcast("rov_sim_command", {"key": key, "value": value})
        return JsonResponse({"ok": True, "sim": True, "key": key, "value": value})

    worker = state.rov_worker
    if worker is None:
        return JsonResponse(
            {"ok": False, "error": "Telemetri ROV tidak aktif"}, status=409,
        )
    if not worker.send(key, value):
        return JsonResponse(
            {"ok": False, "error": "ROV tidak tersambung / telemetri stale"},
            status=409,
        )

    broadcast("rov_command", {"key": key, "value": value})
    return JsonResponse({"ok": True, "key": key, "value": value})


@csrf_exempt
@require_http_methods(["POST"])
def api_rov_move(request):
    """
    Kirim vektor gerak sekaligus. Body:
        {"thro": 1, "lift": 0, "yaw": -2, "client_id": "abc123"}

    Kenapa satu endpoint untuk tiga sumbu, bukan tiga panggilan /api/rov/command
        Browser mengirim 10× per detik. Tiga POST terpisah berarti 30 request
        per detik, tiga kali overhead TCP+JSON, dan — yang lebih buruk — ketiga
        sumbu bisa sampai di waktu berbeda. Operator memutar sambil maju akan
        menghasilkan wahana yang sesaat cuma berputar, lalu sesaat cuma maju.
        Vektor adalah satu keputusan; kirimkan sebagai satu keputusan.

    Nilai di luar -2..2 DITOLAK, tidak di-clamp. Clamp diam-diam menyembunyikan
    bug kalibrasi di sisi klien; penolakan eksplisit memunculkannya saat uji
    di darat, bukan saat wahana sudah di air.
    """
    if not _rov_token_ok(request):
        return JsonResponse({"ok": False, "error": "Token salah"}, status=403)

    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON")

    axes = rov_caps.move_keys()          # {"thro": "thro", "lift": ..., ...}
    vec = {}
    for axis in ("thro", "lift", "yaw"):
        if axis not in axes:
            vec[axis] = 0                # sumbu dimatikan di tabel kapabilitas
            continue
        try:
            v = int(data.get(axis, 0))
        except (TypeError, ValueError):
            return JsonResponse(
                {"ok": False, "error": f"'{axis}' harus integer"}, status=400)
        if v not in rov_caps.MOVE_VALUES:
            return JsonResponse(
                {"ok": False,
                 "error": f"'{axis}'={v} di luar rentang "
                          f"{rov_caps.MOVE_VALUES}"},
                status=400)
        vec[axis] = v

    if not state.rov_control_unlocked:
        return JsonResponse(
            {"ok": False, "error": "Kontrol ROV terkunci"}, status=409)

    client_id = str(data.get("client_id", "")).strip()[:64]
    ok, holder = state.claim_pilot(client_id)
    if not ok:
        if holder is None:
            return JsonResponse(
                {"ok": False, "error": "client_id wajib untuk perintah gerak"},
                status=400)
        return JsonResponse(
            {"ok": False,
             "error": "Klien lain sedang memegang kendali gerak",
             "pilot": holder},
            status=409)

    # Mode simulasi: catat dan siarkan, tapi jangan sentuh soket.
    # record_move() TETAP dipanggil supaya perilaku deadman ikut terlihat
    # saat uji — kalau watchdog cuma diuji dengan ROV nyala, ia tidak
    # pernah benar-benar diuji.
    if state.rov_sim_mode:
        state.record_move(**vec)
        broadcast("rov_sim_move", vec)
        return JsonResponse({"ok": True, "sim": True, **vec})

    worker = state.rov_worker
    if worker is None:
        return JsonResponse(
            {"ok": False, "error": "Telemetri ROV tidak aktif"}, status=409)
    if not worker.send_move(**vec):
        return JsonResponse(
            {"ok": False, "error": "ROV tidak tersambung / telemetri stale"},
            status=409)

    return JsonResponse({"ok": True, **vec})


@csrf_exempt
@require_http_methods(["POST"])
def api_rov_estop(request):
    """
    Nolkan semua sumbu segera.

    Tiga hal yang sengaja berbeda dari endpoint lain:

    1. TIDAK butuh pilot lock. Kalau HP pilot membeku sementara wahana masih
       maju, siapa pun yang bisa meraih layar harus bisa menghentikannya.
       Klaim kendali dilepas paksa, supaya penolong bisa langsung mengambil
       alih setelahnya.
    2. TIDAK butuh unlock. Kunci itu untuk mencegah gerakan yang tidak
       disengaja; berhenti tidak pernah termasuk kategori itu.
    3. Tetap butuh token kalau token dipasang — pembatas jaringan, bukan
       pembatas operator.
    4. TIDAK pernah disimulasikan. Gerak normal boleh dialihkan ke simulasi
       — itu memang gunanya. STOP tidak. Kalau ada worker, force_stop()
       selalu dijalankan, apa pun mode yang sedang aktif. Alasannya kasus
       nyata: wahana yang sempat bergerak lalu modenya dipindah ke simulasi
       masih menahan perintah terakhirnya di firmware, dan sampai beta8.1a
       cabang simulasi di sini langsung `return` sebelum menyentuh soket —
       tombol merah besar itu hanya menghentikan simulasinya. STOP adalah
       perintah keselamatan out-of-band; ia tidak ikut mode.
    """
    if not _rov_token_ok(request):
        return JsonResponse({"ok": False, "error": "Token salah"}, status=403)

    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        data = {}

    sim = state.rov_sim_mode
    worker = state.rov_worker

    state.record_move(0, 0, 0)
    with state._rov_lock:
        state.rov_pilot_id = None
        state.rov_pilot_at = 0.0

    if worker is None:
        # Tanpa worker tidak ada perangkat keras untuk dihentikan. Di
        # simulasi itu keadaan normal; di mode nyata itu kegagalan yang
        # harus terlihat.
        broadcast("rov_estop", {"ok": sim, "sim": sim})
        if sim:
            return JsonResponse({"ok": True, "sim": True})
        return JsonResponse(
            {"ok": False, "error": "Telemetri ROV tidak aktif"}, status=409)

    ok = worker.force_stop(f"e-stop dari klien {data.get('client_id', '?')}")
    broadcast("rov_estop", {"ok": bool(ok), "sim": sim})
    return JsonResponse({"ok": bool(ok), "sim": sim})


@csrf_exempt
@require_http_methods(["POST"])
def api_rov_sim(request):
    """
    Nyalakan / matikan mode simulasi. Body: {"sim": true}.

    Tidak butuh unlock dan tidak butuh token: mode ini justru MENGURANGI
    apa yang bisa terjadi pada wahana. Yang berbahaya adalah kebalikannya —
    mengira sedang simulasi padahal tidak — dan itu ditangani dengan membuat
    statusnya sangat terlihat di UI, bukan dengan mempersulit menyalakannya.
    """
    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON")

    sim = bool(data.get("sim", False))

    # Transisi ke ARAH MANA PUN menolkan gerak fisik lebih dulu. Sampai
    # beta8.1a hanya arah keluar yang dijaga, dan arah masuk justru yang
    # lebih berbahaya:
    #
    #   1. wahana nyata sedang thro:2 — firmware menahannya
    #   2. operator mencentang Mode Simulasi
    #   3. server berhenti meneruskan /move ke soket
    #   4. thro:2 tetap tertahan di wahana, tanpa apa pun yang mencabutnya
    #
    # Deadman tidak menolong di situ: /move simulasi tetap memanggil
    # record_move(), jadi `rov_last_move_at` terus diperbarui dan watchdog
    # menyimpulkan browser masih sehat. Begitu operator melepas stick,
    # snapshot-nya nol dan watchdog juga tidak punya alasan bertindak —
    # sementara wahana fisik masih menjalankan perintah terakhirnya.
    #
    # force_stop() dipanggil SEBELUM flag dipasang, supaya perintahnya masih
    # menemukan jalan ke soket.
    with state._rov_lock:
        was = state.rov_sim_mode

    if was != sim:
        worker = state.rov_worker
        if worker is not None:
            worker.force_stop("berpindah mode simulasi" if sim
                              else "keluar dari mode simulasi")
        state.record_move(0, 0, 0)
        # Klaim kendali ikut dilepas: mode berganti berarti aturan mainnya
        # berganti, dan pilot yang sedang memegang stick tidak boleh
        # meneruskan tekanannya melewati batas itu.
        with state._rov_lock:
            state.rov_pilot_id = None
            state.rov_pilot_at = 0.0

    with state._rov_lock:
        state.rov_sim_mode = sim

    broadcast("rov_sim", {"sim": sim})
    return JsonResponse({"ok": True, "sim": sim})


@csrf_exempt
@require_http_methods(["GET", "POST", "DELETE"])
def api_rov_mapping(request):
    """
    Profil pemetaan controller, per perangkat.

        GET    /api/rov/mapping?id=<gamepad id>
        POST   {"id": "...", "mapping": {"b0": "light", "a1": "thro"}}
        DELETE ?id=<gamepad id>

    Slot ditulis "b<n>" untuk tombol dan "a<n>" untuk sumbu; sumbu boleh
    diakhiri "-" untuk arah terbalik (misal "a1-").
    """
    gid = request.GET.get("id", "")

    if request.method == "GET":
        return JsonResponse({
            "ok": True,
            "key": controller_profiles.device_key(gid),
            "profile": controller_profiles.get(gid),
            "actions": sorted(controller_profiles.VALID_ACTIONS),
        })

    if request.method == "DELETE":
        return JsonResponse({"ok": controller_profiles.delete(gid)})

    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON")

    gid = str(data.get("id", "")) or gid
    if not gid:
        return JsonResponse(
            {"ok": False, "error": "id controller wajib"}, status=400)

    entry = controller_profiles.save(
        gid, data.get("mapping") or {}, label=str(data.get("label", "")))
    return JsonResponse({"ok": True,
                         "key": controller_profiles.device_key(gid),
                         "profile": entry})


def api_rov_caps(request):
    """
    Daftar kapabilitas + status aktifnya.

    Browser membangun tombolnya DARI SINI, bukan dari daftar yang ditulis
    tangan di template. Konsekuensinya: begitu satu baris di rov_caps.py
    diubah setelah PCAP menjawab, tombolnya hidup di semua klien pada muat
    ulang berikutnya — tidak ada HTML atau JavaScript yang perlu disentuh.
    """
    return JsonResponse(rov_caps.to_dict())


@csrf_exempt
@require_http_methods(["POST"])
def api_rov_prefs(request):
    """
    Preferensi integrasi ROV. Body: {"use_heading": true, "auto_depth": false}.
    Bukan perintah ke wahana, jadi tidak butuh unlock.
    """
    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON")

    with state._rov_lock:
        if "use_heading" in data:
            state.rov_use_heading = bool(data["use_heading"])
        if "auto_depth" in data:
            state.rov_auto_depth = bool(data["auto_depth"])
        prefs = {
            "use_heading": state.rov_use_heading,
            "auto_depth": state.rov_auto_depth,
        }

    broadcast("rov_prefs", prefs)
    return JsonResponse({"ok": True, **prefs})


def api_sources(request):
    """
    Daftar sumber video yang terdeteksi (RTSP ROV + webcam USB), ditandai
    mana yang sedang aktif.
    """
    from . import rov_camera

    worker = state.capture_worker
    active = None
    if worker is not None:
        try:
            active = list(rov_camera.parse_spec(worker.source))
        except Exception:
            active = None

    # refresh=1 memaksa enumerasi ulang (operator menancapkan kamera baru,
    # atau baru menyalakan OBS Virtual Camera setelah server hidup).
    refresh = request.GET.get("refresh") == "1"
    try:
        found = rov_camera.list_sources(refresh=refresh)
    except Exception as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=500)

    sources = []
    for label, spec in found:
        s = list(spec) if spec else None
        sources.append({
            "label": label,
            "spec": s,
            "active": s is not None and s == active,
            "selectable": s is not None,
        })

    # Sumber yang sedang aktif belum tentu ada di hasil deteksi — misalnya
    # file video, atau webcam yang enumerasinya gagal. Tanpa ini, dropdown
    # akan tampil kosong padahal videonya jelas jalan.
    if active is not None and not any(x["active"] for x in sources):
        sources.insert(0, {
            "label": f"[aktif] {active[1]}",
            "spec": active,
            "active": True,
            "selectable": True,
        })

    return JsonResponse({"ok": True, "sources": sources, "active": active})


# Sumber yang boleh dipilih lewat API. File video sengaja TIDAK diizinkan:
# menerima path sembarang dari jaringan berarti siapa pun di LAN bisa menyuruh
# server membuka file apa pun di laptop dan menyiarkan isinya. Untuk uji dengan
# file video, pakai env var ROV_RTSP_URL saat start.
_ALLOWED_SOURCE_KINDS = {"dshow", "rtsp"}


@csrf_exempt
@require_http_methods(["POST"])
def api_set_source(request):
    """
    Ganti sumber video saat runtime. Body: {"spec": ["dshow", 1]}.

    Worker TIDAK dihentikan — model YOLO tetap di memori (memuat ulang butuh
    beberapa detik dan akan memutus deteksi lebih lama dari perlunya). Yang
    ditutup dan dibuka ulang hanya capture-nya, dan itu dikerjakan oleh thread
    worker sendiri, bukan dari sini: melepas capture saat frame-nya sedang
    di-decode bisa membuat PyAV/cv2 crash.
    """
    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON")

    spec = data.get("spec")
    if not isinstance(spec, (list, tuple)) or len(spec) != 2:
        return JsonResponse(
            {"ok": False, "error": "spec harus [kind, value]"}, status=400,
        )

    kind, value = spec[0], spec[1]
    if kind not in _ALLOWED_SOURCE_KINDS:
        return JsonResponse(
            {"ok": False,
             "error": f"Jenis sumber '{kind}' tidak diizinkan lewat API"},
            status=400,
        )
    if kind == "dshow":
        try:
            value = int(value)
        except (TypeError, ValueError):
            return JsonResponse(
                {"ok": False, "error": "Index kamera harus integer"},
                status=400,
            )
    else:
        value = str(value)
        if not value.lower().startswith(("rtsp://", "rtsps://")):
            return JsonResponse(
                {"ok": False, "error": "URL RTSP tidak valid"}, status=400,
            )

    worker = state.capture_worker
    if worker is None or not hasattr(worker, "request_source"):
        return JsonResponse(
            {"ok": False, "error": "Capture worker tidak aktif"}, status=409,
        )

    worker.request_source((kind, value))
    return JsonResponse({"ok": True, "spec": [kind, value]})


# ─── Export GPX ──────────────────────────────────────────────────────────
def api_export_gpx(request):
    waypoints = state.get_waypoints()
    if not waypoints:
        return JsonResponse(
            {"ok": False, "error": "Tidak ada waypoint"},
            status=400,
        )

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="ROV SAR Detection Web">',
    ]
    for wp in waypoints:
        lines.append(
            f'  <wpt lat="{wp["lat"]:.8f}" lon="{wp["lon"]:.8f}">'
            f'<name>{wp["label"]}</name></wpt>'
        )
    lines.append("</gpx>")

    gpx_content = "\n".join(lines)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"ROV_waypoints_{timestamp}.gpx"

    response = HttpResponse(gpx_content, content_type="application/gpx+xml")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response

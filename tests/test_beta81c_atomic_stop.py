"""
Regresi v.beta8.1c — STOP atomic + failure-aware.

Tidak butuh Django, Channels, torch, atau hardware. Yang diuji langsung adalah
RovWorker dengan ROV tiruan in-memory supaya tiga bug keselamatan dari review
beta8.1b tidak bisa muncul lagi:

1. STOP gagal tidak boleh membuat software mengaku vektor sudah nol.
2. STOP dan MOVE tidak boleh saling menyelip; STOP menang terhadap MOVE yang
   overlap dengannya.
3. Deadman harus mencoba STOP lagi kalau percobaan pertama gagal.

Jalankan:
    python tests/test_beta81c_atomic_stop.py
"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from detection import rov_worker as rw  # noqa: E402
from detection.state import state  # noqa: E402

RovWorker = rw.RovWorker
# Fallback membuat suite ini tetap bisa dijalankan terhadap beta8.1b untuk
# membuktikan regresinya; versi lama memang belum punya konstanta ini.
DEADMAN_STOP_RETRY_S = getattr(rw, "DEADMAN_STOP_RETRY_S", 0.5)


class FakeRov:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []

    def is_fresh(self):
        return True

    def send(self, axis, value):
        self.calls.append((axis, value))
        if self.results:
            return bool(self.results.pop(0))
        return True


class BlockingRov(FakeRov):
    """Bisa menahan satu send tertentu untuk membuat race deterministic."""

    def __init__(self, block_axis, block_value):
        super().__init__()
        self.block_axis = block_axis
        self.block_value = block_value
        self.entered = threading.Event()
        self.release = threading.Event()
        self._blocked_once = False
        self._lock = threading.Lock()

    def send(self, axis, value):
        with self._lock:
            should_block = (
                not self._blocked_once and
                axis == self.block_axis and value == self.block_value
            )
            if should_block:
                self._blocked_once = True
                self.entered.set()
        if should_block:
            if not self.release.wait(timeout=2.0):
                raise RuntimeError("test timeout menunggu release")
        return super().send(axis, value)


class WorkerBase(unittest.TestCase):
    def setUp(self):
        self.w = RovWorker(host="127.0.0.1")
        state.rov_last_move = {"thro": 0, "lift": 0, "yaw": 0}
        state.rov_last_move_at = 0.0

    def set_motion(self, thro=2, lift=0, yaw=0):
        state.record_move(thro, lift, yaw)
        self.w._last_move_vec = {"thro": thro, "lift": lift, "yaw": yaw}


class TestStopFailureState(WorkerBase):
    def test_failed_stop_preserves_state_and_cache(self):
        self.set_motion(2, 1, 0)
        # thro=0 sukses, lift=0 gagal, yaw=0 sukses
        self.w._rov = FakeRov([True, False, True])

        ok = self.w.force_stop("uji failure")

        self.assertFalse(ok)
        self.assertEqual(state.rov_last_move, {"thro": 2, "lift": 1, "yaw": 0})
        self.assertEqual(self.w._last_move_vec,
                         {"thro": 2, "lift": 1, "yaw": 0})
        self.assertEqual(self.w._rov.calls,
                         [("thro", 0), ("lift", 0), ("yaw", 0)])

    def test_successful_stop_commits_zero(self):
        self.set_motion(2, -1, 1)
        self.w._rov = FakeRov([True, True, True])

        self.assertTrue(self.w.force_stop("uji sukses"))
        self.assertEqual(state.rov_last_move, {"thro": 0, "lift": 0, "yaw": 0})
        self.assertEqual(self.w._last_move_vec,
                         {"thro": 0, "lift": 0, "yaw": 0})


class TestStopMoveAtomicity(WorkerBase):
    def test_move_already_running_finishes_then_stop_is_last(self):
        rov = BlockingRov("thro", 2)
        self.w._rov = rov
        out = {}

        t_move = threading.Thread(
            target=lambda: out.setdefault("move", self.w.send_move(2, 0, 0))
        )
        t_move.start()
        self.assertTrue(rov.entered.wait(1.0), "MOVE tidak masuk titik block")

        t_stop = threading.Thread(
            target=lambda: out.setdefault("stop", self.w.force_stop("race"))
        )
        t_stop.start()
        time.sleep(0.05)  # beri STOP waktu memasang barrier lalu menunggu lock
        rov.release.set()

        t_move.join(2.0)
        t_stop.join(2.0)
        self.assertFalse(t_move.is_alive())
        self.assertFalse(t_stop.is_alive())
        self.assertTrue(out.get("move"))
        self.assertTrue(out.get("stop"))

        last = {}
        for axis, value in rov.calls:
            last[axis] = value
        self.assertEqual(last, {"thro": 0, "lift": 0, "yaw": 0})
        self.assertEqual(state.rov_last_move,
                         {"thro": 0, "lift": 0, "yaw": 0})

    def test_move_started_while_stop_active_is_rejected(self):
        rov = BlockingRov("thro", 0)
        self.w._rov = rov
        self.set_motion(2, 0, 0)
        out = {}

        t_stop = threading.Thread(
            target=lambda: out.setdefault("stop", self.w.force_stop("race"))
        )
        t_stop.start()
        self.assertTrue(rov.entered.wait(1.0), "STOP tidak masuk titik block")

        # STOP sudah memasang barrier tetapi masih tertahan di kiriman pertama.
        out["move"] = self.w.send_move(2, 0, 0)

        # Lepaskan thread STOP sebelum assertion supaya suite juga bisa
        # dijalankan dengan aman terhadap beta8.1b yang memang gagal di sini.
        rov.release.set()
        t_stop.join(2.0)
        self.assertFalse(out["move"],
                         "MOVE overlap dengan STOP harus ditolak")
        self.assertTrue(out.get("stop"))
        self.assertNotIn(("thro", 2), rov.calls)
        self.assertEqual(state.rov_last_move,
                         {"thro": 0, "lift": 0, "yaw": 0})


class TestDeadmanRetry(WorkerBase):
    def test_deadman_retries_after_failed_stop(self):
        self.set_motion(2, 0, 0)
        state.rov_last_move_at = time.time() - 5.0
        # Percobaan STOP #1: salah satu axis gagal. #2: semua sukses.
        self.w._rov = FakeRov([
            True, False, True,
            True, True, True,
        ])

        with mock.patch("detection.rov_worker.broadcast") as bc:
            self.w._check_deadman()
            self.assertEqual(state.rov_last_move["thro"], 2,
                             "STOP gagal tidak boleh menghapus alasan retry")
            self.assertFalse(self.w._deadman_tripped)
            self.assertEqual(len(self.w._rov.calls), 3)
            bc.assert_not_called()

            # Lewati throttle retry tanpa sleep nyata.
            self.w._last_deadman_stop_attempt = (
                time.time() - DEADMAN_STOP_RETRY_S - 0.1
            )
            self.w._check_deadman()

        self.assertEqual(len(self.w._rov.calls), 6)
        self.assertEqual(state.rov_last_move,
                         {"thro": 0, "lift": 0, "yaw": 0})
        self.assertTrue(self.w._deadman_tripped)
        bc.assert_called_once()

    def test_deadman_does_not_busy_loop_between_retries(self):
        self.set_motion(2, 0, 0)
        state.rov_last_move_at = time.time() - 5.0
        self.w._rov = FakeRov([False, False, False])

        with mock.patch("detection.rov_worker.broadcast"):
            self.w._check_deadman()
            n = len(self.w._rov.calls)
            self.w._check_deadman()

        self.assertEqual(len(self.w._rov.calls), n,
                         "retry harus ditahan oleh interval, bukan 5x/detik")


if __name__ == "__main__":
    unittest.main(verbosity=2)

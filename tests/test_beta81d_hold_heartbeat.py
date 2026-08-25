"""Regresi beta8.1d: hold 10 Hz, joystick pointer, heartbeat, dan mobile UI.

Tidak membutuhkan Django, browser, jaringan, atau perangkat keras.
"""
import pathlib
import re
import unittest

from detection import rov_worker as rw


ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = (ROOT / "static/detection/js/controls.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/detection/css/controls.css").read_text(encoding="utf-8")
DASH_CSS = (ROOT / "static/detection/css/dashboard.css").read_text(encoding="utf-8")
HTML = (ROOT / "templates/detection/dashboard.html").read_text(encoding="utf-8")


class FakeRov:
    def __init__(self):
        self.calls = []

    def is_fresh(self):
        return True

    def send(self, axis, value):
        self.calls.append((axis, value))
        return True


class TestHeldMovementForwarding(unittest.TestCase):
    def setUp(self):
        self.worker = rw.RovWorker(host="127.0.0.1")
        self.worker._rov = FakeRov()

    def test_identical_active_vector_is_forwarded_every_heartbeat(self):
        self.assertTrue(self.worker.send_move(2, 0, 0))
        self.assertTrue(self.worker.send_move(2, 0, 0))
        self.assertEqual(
            self.worker._rov.calls,
            [("thro", 2), ("thro", 2)],
        )

    def test_inactive_axes_do_not_overwrite_active_axis(self):
        self.worker.send_move(2, 0, 0)
        self.assertNotIn(("lift", 0), self.worker._rov.calls)
        self.assertNotIn(("yaw", 0), self.worker._rov.calls)

    def test_released_axis_gets_one_zero(self):
        self.worker.send_move(0, -2, 0)
        self.worker._rov.calls.clear()
        self.worker.send_move(0, 0, 0)
        self.assertEqual(self.worker._rov.calls, [("lift", 0)])

    def test_identical_zero_vector_is_deduplicated(self):
        self.worker.send_move(2, 0, 0)
        self.worker.send_move(0, 0, 0)
        count = len(self.worker._rov.calls)
        self.worker.send_move(0, 0, 0)
        self.assertEqual(len(self.worker._rov.calls), count)

    def test_server_deadman_stays_1_5_seconds(self):
        self.assertEqual(rw.MOVE_DEADMAN_S, 1.5)


class TestBrowserRegression(unittest.TestCase):
    def test_browser_transmits_at_10_hz(self):
        self.assertRegex(JS, r"const\s+SEND_HZ\s*=\s*10\s*;")

    def test_pointerleave_does_not_release_stick(self):
        release_events = re.search(
            r"for \(const ev of \[(.*?)\]\)\s*\{", JS, re.S
        )
        self.assertIsNotNone(release_events)
        events = release_events.group(1)
        self.assertNotIn("pointerleave", events)
        self.assertIn("pointerup", events)
        self.assertIn("pointercancel", events)
        self.assertIn("lostpointercapture", events)

    def test_move_heartbeat_is_visible_and_updated_from_ack(self):
        self.assertIn('id="move-heartbeat"', HTML)
        self.assertIn("recordMoveAck();", JS)
        self.assertIn("TX ${moveAckTimes.length} Hz", JS)
        self.assertIn(".move-heartbeat.error", CSS)

    def test_mobile_video_removes_desktop_minimum(self):
        self.assertIsNotNone(re.search(
            r"@media \(max-width: 1100px\).*?\.video-panel\s*\{"
            r".*?min-height:\s*0;.*?aspect-ratio:\s*16\s*/\s*9;",
            DASH_CSS,
            re.S,
        ))

    def test_mobile_stop_is_not_forced_full_width(self):
        self.assertIsNotNone(re.search(
            r"@media \(max-width: 620px\).*?\.estop-btn\s*\{"
            r".*?width:\s*auto;",
            CSS,
            re.S,
        ))


if __name__ == "__main__":
    unittest.main(verbosity=2)

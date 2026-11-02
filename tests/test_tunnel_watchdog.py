import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from start_quick_tunnel import RESTART, TunnelWatchdog, endpoint_ready, main


class TunnelWatchdogTests(unittest.TestCase):
    def test_short_outage_does_not_rotate_working_address(self):
        watch = TunnelWatchdog()
        self.assertFalse(watch.observe(True, 0))
        self.assertFalse(watch.observe(False, 10))
        self.assertFalse(watch.observe(False, 54))
        self.assertTrue(watch.observe(False, 55))

    def test_reconnected_edge_resets_failure_window(self):
        watch = TunnelWatchdog()
        self.assertFalse(watch.observe(False, 0))
        self.assertFalse(watch.observe(True, 40))
        self.assertFalse(watch.observe(False, 44))
        self.assertFalse(watch.observe(False, 88))
        self.assertTrue(watch.observe(False, 89))

    def test_ready_503_counts_as_outage_instead_of_crashing_watchdog(self):
        with patch(
            "start_quick_tunnel.urllib.request.urlopen",
            side_effect=HTTPError("http://127.0.0.1:20241/ready", 503, "No edge", {}, None),
        ):
            self.assertFalse(endpoint_ready("http://127.0.0.1:20241/ready"))

    def test_lost_tunnel_restarts_but_user_interrupt_does_not(self):
        with (
            patch("sys.argv", ["start_quick_tunnel", "--port", "COM16"]),
            patch("start_quick_tunnel.run_tunnel", side_effect=[RESTART, 0]) as run,
            patch("start_quick_tunnel.time.sleep") as sleep,
        ):
            self.assertEqual(main(), 0)
            self.assertEqual(run.call_count, 2)
            sleep.assert_called_once_with(5)
        with (
            patch("sys.argv", ["start_quick_tunnel"]),
            patch("start_quick_tunnel.run_tunnel", side_effect=KeyboardInterrupt) as run,
        ):
            self.assertEqual(main(), 0)
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()

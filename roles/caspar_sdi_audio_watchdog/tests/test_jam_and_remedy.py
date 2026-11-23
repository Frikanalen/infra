"""Unit tests for the jam condition and the remedy policy.

Run: python3 -m unittest discover -s roles/caspar_sdi_audio_watchdog/tests
"""
import importlib.util
import os
import unittest

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location(
    "watchdog_jr", os.path.join(HERE, "..", "files", "caspar-sdi-audio-watchdog.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
w.log = lambda *a, **k: None

FLOOR = (-60.0, -60.0)
MUSIC = (-22.0, -24.0)
QUIET = (-58.5, -58.0)  # between floor and alive: proves nothing


class Jam(unittest.TestCase):
    def setUp(self):
        self.j = w.JamTracker()

    def run_ticks(self, n, atem, pre, post=None):
        return [self.j.tick(atem, pre, post) for _ in range(n)]

    def test_floor_with_any_multicast_audio_counts(self):
        # Quiet graphics music at -55 dBFS: below the old -50 threshold.
        self.run_ticks(30, FLOOR, -55.0)
        self.assertEqual(self.j.seconds, 30)

    def test_quiet_music_does_not_reset(self):
        self.run_ticks(20, FLOOR, -45.0)
        self.run_ticks(10, FLOOR, -70.0)   # very quiet passage, not digital silence
        self.assertEqual(self.j.seconds, 30)

    def test_digital_silence_on_the_multicast_holds(self):
        self.run_ticks(10, FLOOR, -40.0)
        self.assertEqual(self.run_ticks(5, FLOOR, -120.0), ["hold"] * 5)
        self.run_ticks(5, FLOOR, None)     # multicast unreadable (CasparCG restarting)
        self.assertEqual(self.j.seconds, 10)

    def test_only_real_audio_on_the_atem_clears(self):
        self.run_ticks(25, FLOOR, -40.0)
        self.run_ticks(3, QUIET, -40.0)
        self.assertEqual(self.j.seconds, 25)
        self.assertEqual(self.j.tick(MUSIC, -40.0, None), "alive")
        self.assertEqual(self.j.seconds, 0)

    def test_falls_back_to_the_program_return_without_atem_levels(self):
        self.run_ticks(10, None, -40.0, -85.0)
        self.assertEqual(self.j.seconds, 10)
        self.assertEqual(self.j.signal, "return")
        self.assertEqual(self.j.tick(None, -40.0, -30.0), "alive")
        self.assertEqual(self.j.seconds, 0)

    def test_return_fallback_needs_pre_above_min_and_respects_grace(self):
        self.assertEqual(self.j.tick(None, -55.0, -85.0), "hold")
        self.assertEqual(self.j.tick(None, -40.0, -85.0, in_grace=True), "hold")
        self.assertEqual(self.j.seconds, 0)

    def test_switching_signal_restarts_the_count(self):
        self.run_ticks(10, None, -40.0, -85.0)
        self.run_ticks(3, FLOOR, -40.0)
        self.assertEqual((self.j.seconds, self.j.signal), (3, "atem"))

    def test_nothing_readable_holds(self):
        self.assertEqual(self.j.tick(None, None, None), "hold")


class Remedy(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(w.CFG)
        w.CFG.update(COOLDOWN_SECONDS=600, VERIFY_SECONDS=60, MAX_BACKOFF_SECONDS=3600,
                     FAILURE_RESET_SECONDS=300, MAX_RESTARTS=0, WINDOW_SECONDS=21600,
                     SOFT_REMEDY_COMMAND="")
        self.p = w.RemedyPolicy({"restarts": []})

    def tearDown(self):
        w.CFG.clear()
        w.CFG.update(self.cfg)

    def test_first_jam_restarts(self):
        self.assertEqual(self.p.decide(1000)[0], "restart")

    def test_a_new_jam_after_a_working_restart_waits_ten_minutes_not_six_hours(self):
        self.p.acted("restart", 1000)
        self.p.saw("alive", 1010)
        self.assertEqual(self.p.decide(1300), (None, "cooldown"))
        self.assertEqual(self.p.decide(1600)[0], "restart")
        for t in (1600, 2200, 2800, 3400, 4000):   # one jam every 10 min, each cleared
            self.p.acted("restart", t)
            self.p.saw("alive", t + 10)
        self.assertEqual(self.p.decide(4600)[0], "restart")  # no 6 h cap by default

    def test_a_restart_that_does_not_clear_backs_off_exponentially(self):
        self.p.acted("restart", 1000)
        self.assertEqual(self.p.decide(1030), (None, "settling"))
        self.assertEqual(self.p.decide(1100), (None, "backoff"))   # failed: wait 1200 s
        self.assertEqual(self.p.state["failures"], 1)
        self.assertEqual(self.p.decide(2200)[0], "restart")
        self.p.acted("restart", 2200)
        self.p.decide(2300)                                         # failed again: 2400 s
        self.assertEqual(self.p.state["failures"], 2)
        self.assertEqual(self.p.decide(4500), (None, "backoff"))
        self.assertEqual(self.p.decide(4600)[0], "restart")

    def test_backoff_is_capped(self):
        self.p.state["failures"] = 10
        self.assertEqual(self.p.wait_seconds(), 3600)

    def test_failures_reset_after_audio_for_a_while(self):
        self.p.acted("restart", 1000)
        self.p.decide(1100)
        self.assertEqual(self.p.state["failures"], 1)
        self.p.saw("alive", 1200)
        self.p.saw("alive", 1400)
        self.assertEqual(self.p.state["failures"], 1)
        self.p.saw("alive", 1500)
        self.assertEqual(self.p.state["failures"], 0)

    def test_soft_remedy_first_then_restart_at_once_if_it_fails(self):
        w.CFG["SOFT_REMEDY_COMMAND"] = "true"
        self.assertEqual(self.p.decide(1000)[0], "soft")
        self.p.acted("soft", 1000)
        self.assertEqual(self.p.decide(1070), ("restart", "soft remedy failed"))

    def test_soft_remedy_that_works_is_used_again(self):
        w.CFG["SOFT_REMEDY_COMMAND"] = "true"
        self.p.acted("soft", 1000)
        self.p.saw("alive", 1005)
        self.assertEqual(self.p.decide(1700)[0], "soft")

    def test_optional_hard_cap(self):
        w.CFG.update(MAX_RESTARTS=2)
        for t in (1000, 2000):
            self.p.acted("restart", t)
            self.p.saw("alive", t + 5)
        self.assertEqual(self.p.decide(3000), (None, "limit"))

    def test_state_from_the_old_watchdog_loads(self):
        p = w.RemedyPolicy({"restarts": [900.0, 950.0]})
        self.assertEqual(p.state["last_remedy"], 950.0)
        self.assertEqual(p.state["failures"], 0)


if __name__ == "__main__":
    unittest.main()

"""Audio lead detector: diag log parsing, tailing across logrotate, and the
starved-window decision."""
import importlib.util
import os
import tempfile
import unittest

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location(
    "watchdog_lead", os.path.join(HERE, "..", "files", "caspar-sdi-audio-watchdog.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
w.log = lambda *a, **k: None


def s_line(ba_min, ba_max, sched_acc=0, t="2026-11-25T14:48:00.000Z"):
    return (f"{t} dl1/ch1 S fr=134 n=50 res=ok:50,late:0,drop:0,flush:0,other:0 "
            f"ba={ba_min}..{ba_max} ba_post=3820..4788 bv=5..5 a_unacc=972..1940 "
            f"av_skew=0..0 s_lead=4416..4416 ia=20.0..20.0ms ph=133000 ph_jump=0 cft_step=0 "
            f"sched_acc={sched_acc} ns_cad=0 short=0 oncb=25 ovf=0 sup=0")


class Parse(unittest.TestCase):
    def test_s_line(self):
        self.assertEqual(w.parse_diag_line(s_line(2860, 3828, 3)), ("s", 2860, 3828, 3))

    def test_start_resets(self):
        self.assertEqual(w.parse_diag_line(
            "2026-11-23T18:37:10.100Z dl1/ch1 I START 720p5000 buffer_depth=5 audio_cadence=960"),
            ("reset",))

    def test_events_and_context_ignored(self):
        self.assertIsNone(w.parse_diag_line(
            "2026-11-25T14:48:00.508Z dl1/ch1 E fr=160 SCHED_ACCEPT_304_OF_960"))
        self.assertIsNone(w.parse_diag_line(
            "  f=156 t=18:03:39.428Z r=ok cft=154000 ba=2860/2860/3820/3820 bv=5"))


class Tail(unittest.TestCase):
    def test_follows_from_end_and_across_rotation(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "diag.log")
            with open(p, "w") as f:
                f.write("old line\n")
            t = w.DiagTail(p)
            self.assertEqual(t.lines(), [])
            with open(p, "a") as f:
                f.write("one\ntw")
            self.assertEqual(t.lines(), ["one"])
            with open(p, "a") as f:
                f.write("o\n")
            self.assertEqual(t.lines(), ["two"])
            # copytruncate: same inode, shorter file
            with open(p, "w") as f:
                f.write("after truncate\n")
            self.assertEqual(t.lines(), ["after truncate"])
            # new file (create mode): new inode, read from the start
            os.rename(p, p + ".1")
            with open(p, "w") as f:
                f.write("new file\n")
            self.assertEqual(t.lines(), ["new file"])

    def test_missing_file_is_quiet(self):
        t = w.DiagTail("/nonexistent/diag.log")
        self.assertEqual(t.lines(), [])


class Lead(unittest.TestCase):
    def setUp(self):
        self.m = w.LeadMonitor()
        self.now = 1000.0

    def second(self, ba_min, ba_max, acc=0):
        self.now += 1
        self.m.feed([("s", ba_min, ba_max, acc)], self.now)
        return self.m.tick(self.now)[0]

    def test_healthy_lead_is_alive(self):
        self.assertEqual(self.second(3819, 3831), "alive")

    def test_one_frame_of_lead_holds(self):
        # 25 Nov 08:40-14:40: max ~1124, min 390-980, no audible gaps yet
        verdicts = [self.second(400, 1124) for _ in range(120)]
        self.assertEqual(set(verdicts), {"hold"})

    def test_partial_accepts_trigger_after_30_of_60(self):
        verdicts = [self.second(0, 1200, acc=2) for _ in range(30)]
        self.assertEqual(verdicts[:29], ["hold"] * 29)
        self.assertEqual(verdicts[29], "jam")

    def test_hovering_still_triggers(self):
        # alternate starved and not: 30 starved seconds within 60
        verdicts = []
        for i in range(60):
            verdicts.append(self.second(0, 1200, acc=1 if i % 2 else 0))
        self.assertEqual(verdicts[-1], "jam")

    def test_zero_lead_mute_triggers(self):
        verdicts = [self.second(0, 0) for _ in range(30)]
        self.assertEqual(verdicts[-1], "jam")

    def test_restart_line_clears_window(self):
        for _ in range(29):
            self.second(0, 0)
        self.m.feed([("reset",)], self.now)
        self.assertEqual(self.second(0, 0), "hold")

    def test_stale_log_holds_and_clears(self):
        for _ in range(40):
            self.second(0, 0)
        self.now += 10
        self.assertEqual(self.m.tick(self.now)[0], "hold")
        self.assertEqual(self.second(0, 0), "hold")

    def test_old_seconds_leave_the_window(self):
        for _ in range(29):
            self.second(0, 0)
        for _ in range(60):
            self.second(3800, 3830)
        self.assertEqual(self.second(0, 0), "hold")

    def test_remedy_policy_sees_alive_after_recreate(self):
        st = {"restarts": []}
        p = w.RemedyPolicy(st)
        verdicts = [self.second(0, 0) for _ in range(30)]
        p.saw(verdicts[-1], self.now)
        kind, _ = p.decide(self.now)
        self.assertEqual(kind, "soft" if w.CFG["SOFT_REMEDY_COMMAND"] else "restart")
        p.acted(kind, self.now)
        self.m.reset()
        p.saw(self.second(3820, 3830), self.now)
        self.assertTrue(st["alive_since_remedy"])


if __name__ == "__main__":
    unittest.main()

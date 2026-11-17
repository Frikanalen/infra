"""Unit tests for the atemkontroll /state parsing.

Run: python3 -m unittest discover -s roles/caspar_sdi_audio_watchdog/tests
The sample is atemkontroll's mock mixer (src/mock.ts) /state document.
"""
import copy
import importlib.util
import os
import unittest

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location(
    "watchdog", os.path.join(HERE, "..", "files", "caspar-sdi-audio-watchdog.py"))
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)

MOCK_STATE = {
    "connected": True,
    "model": "ATEM 1 M/E Production Studio 4K (mock)",
    "inputs": [{"id": 1, "shortName": "CAM1", "longName": "Camera 1"}],
    "mixEffects": [{"index": 0, "program": 1, "preview": 2, "inTransition": False,
                    "fadeToBlack": {"fullyBlack": False, "inTransition": False}}],
    "downstreamKeyers": [{"index": 0, "onAir": False, "tie": False, "inTransition": False}],
    "auxes": [{"index": 0, "source": 10010}],
    "macros": [],
}


def with_me(**changes):
    s = copy.deepcopy(MOCK_STATE)
    s["mixEffects"][0].update(changes)
    return s


class ProgramDecision(unittest.TestCase):
    def test_caspar_on_program(self):
        self.assertEqual(watchdog.program_decision(MOCK_STATE, 1)[0], "on_air")

    def test_other_source_inhibits(self):
        v, d = watchdog.program_decision(with_me(program=3), 1)
        self.assertEqual(v, "inhibit")
        self.assertIn("program=3", d)

    def test_transition_inhibits(self):
        self.assertEqual(watchdog.program_decision(with_me(inTransition=True), 1)[0], "inhibit")

    def test_fade_to_black_inhibits(self):
        s = with_me(fadeToBlack={"fullyBlack": True, "inTransition": False})
        self.assertEqual(watchdog.program_decision(s, 1)[0], "inhibit")
        s = with_me(fadeToBlack={"fullyBlack": False, "inTransition": True})
        self.assertEqual(watchdog.program_decision(s, 1)[0], "inhibit")

    def test_disconnected_is_unknown(self):
        s = copy.deepcopy(MOCK_STATE)
        s["connected"] = False
        self.assertEqual(watchdog.program_decision(s, 1)[0], "unknown")

    def test_empty_state_is_unknown(self):
        empty = {"connected": True, "inputs": [], "mixEffects": [], "downstreamKeyers": [],
                 "auxes": [], "macros": []}
        self.assertEqual(watchdog.program_decision(empty, 1)[0], "unknown")
        self.assertEqual(watchdog.program_decision(None, 1)[0], "unknown")
        self.assertEqual(watchdog.program_decision(with_me(program=None), 1)[0], "unknown")

    def test_unconfigured_url_is_unknown(self):
        watchdog.CFG["ATEM_URL"] = ""
        self.assertEqual(watchdog.ask_atem()[0], "unknown")

    def test_unreachable_is_unknown(self):
        watchdog.CFG["ATEM_URL"] = "http://127.0.0.1:9"
        self.assertEqual(watchdog.ask_atem()[0], "unknown")


class InputName(unittest.TestCase):
    def test_resolves_by_long_or_short_name(self):
        s = copy.deepcopy(MOCK_STATE)
        s["inputs"].append({"id": 4, "shortName": "CCG1", "longName": "caspar1"})
        self.assertEqual(watchdog.resolve_input(s, "caspar1", 1), 4)
        self.assertEqual(watchdog.resolve_input(s, "ccg1", 1), 4)
        s["mixEffects"][0]["program"] = 4
        self.assertEqual(watchdog.program_decision(s, 1, input_name="caspar1")[0], "on_air")
        self.assertEqual(watchdog.program_decision(s, 1)[0], "inhibit")

    def test_unknown_name_falls_back(self):
        self.assertEqual(watchdog.resolve_input(MOCK_STATE, "nope", 1), 1)
        self.assertEqual(watchdog.resolve_input(MOCK_STATE, "", 1), 1)


class Grace(unittest.TestCase):
    def setUp(self):
        watchdog.CFG["ATEM_URL"] = "http://atem.invalid"
        watchdog.CFG["ATEM_INPUT"] = 1
        watchdog.CFG["ATEM_INPUT_NAME"] = ""
        watchdog.CFG["PROGRAM_GRACE_SECONDS"] = 15
        self.states = []
        self.real_fetch = watchdog.fetch_atem_state
        watchdog.fetch_atem_state = lambda: self.states.pop(0)

    def tearDown(self):
        watchdog.fetch_atem_state = self.real_fetch

    def test_cut_to_caspar_delays_the_jam(self):
        w = watchdog.ProgramWatch()
        w.started = 0
        self.states = [with_me(program=3), with_me(program=1)]
        w.poll(100)           # someone else on program
        self.assertIsNone(w.on_air_since)
        w.poll(110)           # cut to caspar at 110
        self.assertEqual(w.on_air_since, 110)
        self.assertEqual(w.jam_start(105), 125)  # jam seen at 105 counts from 110+15
        self.assertEqual(w.jam_start(200), 200)

    def test_on_program_at_start_has_no_grace(self):
        w = watchdog.ProgramWatch()
        self.states = [with_me(program=1)]
        w.poll(w.started + 1)
        self.assertEqual(w.jam_start(50), 50)

    def test_unreachable_keeps_previous(self):
        w = watchdog.ProgramWatch()
        w.started = 0
        w.on_air_since = 110
        def boom():
            raise OSError("down")
        watchdog.fetch_atem_state = boom
        w.poll(200)
        self.assertEqual(w.on_air_since, 110)


if __name__ == "__main__":
    unittest.main()

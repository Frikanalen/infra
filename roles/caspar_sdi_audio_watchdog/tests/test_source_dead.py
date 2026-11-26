"""Source-dead detector: a file plays on but its audio is gone everywhere
(26 Nov 2026, CasparCG's ffmpeg producer on a VP9/Opus .webm)."""
import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location(
    "watchdog_sd", os.path.join(HERE, "..", "files", "caspar-sdi-audio-watchdog.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
w.log = lambda *a, **k: None

FILE = "/mnt/media/628845.webm"


class Fake:
    """INFO 1 stand-in: one file layer whose time advances one second a tick."""

    def __init__(self, path=FILE, layer=50, producer="ffmpeg", start=1060.0, advance=True):
        self.path, self.layer, self.producer = path, layer, producer
        self.t, self.advance = start, advance
        self.audio_calls = []
        self.audio = -12.0
        self.fail = False

    def layers(self):
        if self.fail:
            raise OSError("AMCP down")
        return [{"layer": 20, "producer": "html", "path": "http://graphics/x", "time": None, "length": None},
                {"layer": self.layer, "producer": self.producer, "path": self.path,
                 "time": self.t, "length": 1800.0}]

    def audio_fn(self, path, start, seconds):
        self.audio_calls.append((path, start, seconds))
        return self.audio

    def step(self):
        if self.advance:
            self.t += 1.0


class Monitor(unittest.TestCase):
    def setUp(self):
        self.saved = dict(w.CFG)
        w.CFG.update(SOURCE_DEAD_SECONDS=30, SOURCE_DEAD_DBFS=-100.0, SOURCE_DEAD_FILE_MIN_DB=-60.0,
                     SOURCE_DEAD_MEDIA_DIR="/mnt/media", SOURCE_DEAD_LAYERS="",
                     SOURCE_DEAD_POLL_SECONDS=5.0)
        self.f = Fake()
        self.m = w.SourceDeadMonitor(self.f.layers, self.f.audio_fn)

    def tearDown(self):
        w.CFG.clear()
        w.CFG.update(self.saved)

    def run_ticks(self, n, pre=-120.0, sdi_alive=False, t0=1000.0):
        out = []
        for i in range(n):
            out.append(self.m.tick(t0 + i, pre, sdi_alive))
            self.f.step()
        return out

    def test_the_26_nov_case_acts_after_30_s(self):
        out = self.run_ticks(31)
        self.assertEqual([v for v, _, _ in out[:29]], ["hold"] * 29)
        verdict, held, info = out[29]
        self.assertEqual((verdict, held), ("dead", 30))
        self.assertEqual(info["path"], FILE)
        self.assertEqual(info["layer"], 50)
        # The file is checked once, from where the silence began.
        self.assertEqual(len(self.f.audio_calls), 1)
        path, start, seconds = self.f.audio_calls[0]
        self.assertEqual((path, start), (FILE, 1060.0))
        self.assertGreaterEqual(seconds, 1.0)
        self.assertLessEqual(seconds, 10.0)
        self.assertEqual(out[30][0], "dead")

    def test_audio_resets(self):
        self.run_ticks(20)
        self.assertEqual(self.m.tick(1020, -30.0, False)[0], "alive")
        self.assertEqual([v for v, _, _ in self.run_ticks(29, t0=1021)], ["hold"] * 29)

    def test_silence_in_the_file_never_acts(self):
        self.f.audio = -91.0  # volumedetect on digital zero
        self.assertNotIn("dead", [v for v, _, _ in self.run_ticks(60)])
        self.assertEqual(len(self.f.audio_calls), 1)

    def test_file_without_audio_or_unreadable_never_acts(self):
        for result in ("noaudio", None):
            self.setUp()
            self.f.audio = result
            self.assertNotIn("dead", [v for v, _, _ in self.run_ticks(60)])

    def test_sdi_alive_means_multicast_consumer_not_source(self):
        out = self.run_ticks(60, sdi_alive=True)
        self.assertEqual({v for v, _, _ in out}, {"hold"})
        self.assertEqual(self.f.audio_calls, [])

    def test_stopped_layer_never_acts(self):
        self.f.advance = False  # paused or frozen: not this case
        self.assertNotIn("dead", [v for v, _, _ in self.run_ticks(60)])

    def test_live_and_non_ffmpeg_layers_ignored(self):
        self.f.path = "srt://contrib/live/x"
        self.assertNotIn("dead", [v for v, _, _ in self.run_ticks(60)])
        self.setUp()
        self.f.producer = "html"
        self.assertNotIn("dead", [v for v, _, _ in self.run_ticks(60)])

    def test_layer_filter(self):
        w.CFG["SOURCE_DEAD_LAYERS"] = "10"
        self.assertNotIn("dead", [v for v, _, _ in self.run_ticks(60)])
        w.CFG["SOURCE_DEAD_LAYERS"] = "10,50"
        self.m.reset()
        self.assertIn("dead", [v for v, _, _ in self.run_ticks(31, t0=2000)])

    def test_relative_path_joined_with_media_dir(self):
        self.f.path = "628845.webm"
        self.run_ticks(31)
        self.assertEqual(self.f.audio_calls[0][0], "/mnt/media/628845.webm")

    def test_new_item_is_checked_afresh(self):
        self.f.audio = -91.0
        self.run_ticks(40)
        self.f.path, self.f.t, self.f.audio = "/mnt/media/next.mp4", 0.0, -10.0
        self.assertIn("dead", [v for v, _, _ in self.run_ticks(10, t0=1040)])
        self.assertEqual(self.f.audio_calls[-1][0], "/mnt/media/next.mp4")

    def test_amcp_down_holds(self):
        self.f.fail = True
        self.assertEqual({v for v, _, _ in self.run_ticks(60)}, {"hold"})

    def test_off_and_stale(self):
        self.assertEqual(self.m.tick(0, None, False)[0], "hold")
        w.CFG["SOURCE_DEAD_SECONDS"] = 0
        self.assertEqual({v for v, _, _ in self.run_ticks(60)}, {"hold"})


class Volumedetect(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(w.parse_volumedetect(
            "[Parsed_volumedetect_0 @ 0x1] mean_volume: -27.4 dB\n"
            "[Parsed_volumedetect_0 @ 0x1] max_volume: -8.1 dB\n"), -8.1)
        self.assertEqual(w.parse_volumedetect("max_volume: -inf dB"), -120.0)
        self.assertEqual(w.parse_volumedetect("Output file is empty, nothing was encoded", 0), "noaudio")
        self.assertIsNone(w.parse_volumedetect("No such file or directory", 1))

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_real_files(self):
        with tempfile.TemporaryDirectory() as d:
            tone = os.path.join(d, "tone.mkv")
            silent = os.path.join(d, "silent.mkv")
            video = os.path.join(d, "video.mkv")
            run = lambda *a: subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", *a],
                                            check=True)
            run("-f", "lavfi", "-i", "sine=f=440:d=8", "-c:a", "libopus", tone)
            run("-f", "lavfi", "-i", "anullsrc=d=8", "-t", "8", "-c:a", "libopus", silent)
            run("-f", "lavfi", "-i", "testsrc=d=3:s=64x64", video)
            self.assertGreater(w.file_audio_max_db(tone, 2.0, 3.0), -30)
            self.assertLess(w.file_audio_max_db(silent, 2.0, 3.0), -60)
            self.assertEqual(w.file_audio_max_db(video, 0.0, 2.0), "noaudio")
            self.assertIsNone(w.file_audio_max_db(os.path.join(d, "missing.mkv"), 0.0, 2.0))


if __name__ == "__main__":
    unittest.main()

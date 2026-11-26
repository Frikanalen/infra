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
    """INFO 1 stand-in: file layers whose time advances one second a tick."""

    def __init__(self):
        self.layers_ = {50: {"path": FILE, "producer": "ffmpeg", "t": 1060.0, "advance": True}}
        self.audio_calls = []
        self.audio = {}           # path -> result; default -12 dB
        self.fail = False

    @property
    def main(self):
        return self.layers_[50]

    def layers(self):
        if self.fail:
            raise OSError("AMCP down")
        out = [{"layer": 20, "producer": "html", "path": "http://graphics/x", "time": None, "length": None}]
        for n, l in sorted(self.layers_.items()):
            out.append({"layer": n, "producer": l["producer"], "path": l["path"],
                        "time": l["t"], "length": 1800.0})
        return out

    def audio_fn(self, path, start, seconds):
        self.audio_calls.append((path, start, seconds))
        return self.audio.get(path, -12.0)

    def step(self):
        for l in self.layers_.values():
            if l["advance"]:
                l["t"] += 1.0


class Base(unittest.TestCase):
    def setUp(self):
        self.saved = dict(w.CFG)
        w.CFG.update(SOURCE_DEAD_SECONDS=30, SOURCE_DEAD_DBFS=-100.0, SOURCE_DEAD_FILE_MIN_DB=-60.0,
                     SOURCE_DEAD_MEDIA_DIR="/mnt/media", SOURCE_DEAD_LAYERS="",
                     SOURCE_DEAD_POLL_SECONDS=5.0, SOURCE_DEAD_MARGIN_SECONDS=3.0,
                     SOURCE_DEAD_RECHECK_SECONDS=60.0)
        self.f = Fake()
        self.m = w.SourceDeadMonitor(self.f.layers, self.f.audio_fn, run_async=False)
        self.t = 1000.0

    def tearDown(self):
        w.CFG.clear()
        w.CFG.update(self.saved)

    def run_ticks(self, n, pre=-120.0, sdi="silent"):
        out = []
        for _ in range(n):
            out.append(self.m.tick(self.t, pre, sdi))
            self.f.step()
            self.t += 1
        return out

    def verdicts(self, n, **kw):
        return [v for v, _, _ in self.run_ticks(n, **kw)]


class Monitor(Base):
    def test_the_26_nov_case_acts_after_30_s(self):
        v = self.verdicts(40)
        self.assertNotIn("dead", v[:30])
        self.assertIn("dead", v)
        first = v.index("dead")
        self.assertLessEqual(first, 36)  # within a poll of 30 s
        _, held, info = self.run_ticks(1)[0]
        self.assertEqual(info["path"], FILE)
        self.assertEqual(info["layer"], 50)
        # One check, over a stretch the multicast has heard: ends 3 s before
        # the polled position, inside the silence.
        self.assertEqual(len(self.f.audio_calls), 1)
        path, start, seconds = self.f.audio_calls[0]
        self.assertEqual(path, FILE)
        self.assertGreaterEqual(start, 1060.0)
        self.assertLessEqual(seconds, 20.0)
        self.assertGreaterEqual(seconds, 1.0)

    def test_audio_resets(self):
        self.run_ticks(25)
        self.assertEqual(self.m.tick(self.t, -30.0, "silent")[0], "alive")
        self.t += 1
        self.assertNotIn("dead", self.verdicts(29))

    def test_silence_in_the_file_rechecked_each_minute_then_acts(self):
        self.f.audio[FILE] = -91.0  # a quiet passage at first
        v = self.verdicts(70)
        self.assertNotIn("dead", v)
        self.assertEqual(len(self.f.audio_calls), 1)
        self.f.audio[FILE] = -15.0  # the programme has audio again, the multicast doesn't
        v = self.verdicts(40)
        self.assertIn("dead", v)
        self.assertEqual(len(self.f.audio_calls), 2)
        # The recheck looked at a newer stretch.
        self.assertGreater(self.f.audio_calls[1][1], self.f.audio_calls[0][1])

    def test_unreadable_is_retried_slowly_not_every_second(self):
        self.f.audio[FILE] = None
        self.assertNotIn("dead", self.verdicts(100))
        self.assertLessEqual(len(self.f.audio_calls), 2)

    def test_file_without_audio_is_final(self):
        self.f.audio[FILE] = "noaudio"
        self.assertNotIn("dead", self.verdicts(200))
        self.assertEqual(len(self.f.audio_calls), 1)

    def test_sdi_alive_means_multicast_consumer_not_source(self):
        self.assertEqual(set(self.verdicts(60, sdi="alive")), {"hold"})
        self.assertEqual(self.f.audio_calls, [])

    def test_sdi_unknown_never_acts(self):
        self.assertEqual(set(self.verdicts(60, sdi="unknown")), {"hold"})
        self.assertEqual(self.f.audio_calls, [])

    def test_stopped_layer_never_acts(self):
        self.f.main["advance"] = False  # paused or frozen: not this case
        self.assertNotIn("dead", self.verdicts(60))

    def test_live_and_non_ffmpeg_layers_ignored(self):
        self.f.main["path"] = "srt://contrib/live/x"
        self.assertNotIn("dead", self.verdicts(60))
        self.setUp()
        self.f.main["producer"] = "html"
        self.assertNotIn("dead", self.verdicts(60))

    def test_layer_filter(self):
        w.CFG["SOURCE_DEAD_LAYERS"] = "10"
        self.assertNotIn("dead", self.verdicts(60))
        w.CFG["SOURCE_DEAD_LAYERS"] = "10,50"
        self.m.reset()
        self.assertIn("dead", self.verdicts(40))

    def test_relative_path_joined_with_media_dir(self):
        self.f.main["path"] = "628845.webm"
        self.run_ticks(40)
        self.assertEqual(self.f.audio_calls[0][0], "/mnt/media/628845.webm")

    def test_silent_background_layer_does_not_hide_dead_programme(self):
        self.f.layers_[10] = {"path": "/mnt/media/loop.mov", "producer": "ffmpeg", "t": 0.0,
                              "advance": True}
        self.f.audio["/mnt/media/loop.mov"] = "noaudio"
        v = self.verdicts(40)
        self.assertIn("dead", v)
        self.assertEqual({c[0] for c in self.f.audio_calls}, {"/mnt/media/loop.mov", FILE})

    def test_programme_after_silent_gap_gets_its_own_30_s(self):
        # 40 s of silence with no file layer (graphics only), then the next
        # programme starts with a silent leader.
        del self.f.layers_[50]
        self.assertNotIn("dead", self.verdicts(40))
        self.f.layers_[50] = {"path": "/mnt/media/next.mp4", "producer": "ffmpeg", "t": 0.0,
                              "advance": True}
        v = self.verdicts(25)
        self.assertNotIn("dead", v)
        self.assertEqual(self.f.audio_calls, [])
        self.assertIn("dead", self.verdicts(20))
        # The check never reaches past what the multicast has heard.
        _, start, seconds = self.f.audio_calls[0]
        self.assertGreaterEqual(start, 0.0)

    def test_new_item_is_checked_afresh(self):
        self.f.audio[FILE] = -91.0
        self.run_ticks(40)
        self.f.main.update(path="/mnt/media/next.mp4", t=0.0)
        self.assertIn("dead", self.verdicts(40))
        self.assertEqual(self.f.audio_calls[-1][0], "/mnt/media/next.mp4")

    def test_recue_starts_layer_over(self):
        self.run_ticks(20)
        self.f.main["t"] = 0.0  # playout re-cued the item from the top
        v = self.verdicts(20)
        self.assertNotIn("dead", v)

    def test_amcp_down_holds(self):
        self.f.fail = True
        self.assertEqual(set(self.verdicts(60)), {"hold"})

    def test_off_and_stale(self):
        self.assertEqual(self.m.tick(0, None, "silent")[0], "hold")
        w.CFG["SOURCE_DEAD_SECONDS"] = 0
        self.assertEqual(set(self.verdicts(60)), {"hold"})

    def test_check_errors_do_not_escape(self):
        def boom(*a):
            raise RuntimeError("x")
        self.m.audio_fn = boom
        self.assertNotIn("dead", self.verdicts(60))

    def test_async_check_does_not_block(self):
        import threading
        gate = threading.Event()

        def slow(path, start, seconds):
            gate.wait(5)
            return -12.0
        m = w.SourceDeadMonitor(self.f.layers, slow)  # threaded
        out = []
        for _ in range(40):
            out.append(m.tick(self.t, -120.0, "silent")[0])
            self.f.step()
            self.t += 1
        self.assertNotIn("dead", out)  # the check is still running
        gate.set()
        for _ in range(50):
            if m.tick(self.t, -120.0, "silent")[0] == "dead":
                break
            import time
            time.sleep(0.01)
            self.t += 1
        else:
            self.fail("never acted once the check finished")


class SdiAndMute(Base):
    def test_seconds_of_sdi_silence_after_unknown_do_not_act(self):
        # 40 s with no SDI evidence (CasparCG off program, ATEM levels
        # stale), then a cut: the lagging return shows silence briefly.
        self.verdicts(40, sdi="unknown")
        self.assertNotIn("dead", self.verdicts(5))
        # Held silent for the full time, it acts.
        self.assertIn("dead", self.verdicts(35))

    def test_muted_layer_never_counts(self):
        vols = {None: 1.0, 50: 0.0}
        self.m = w.SourceDeadMonitor(self.f.layers, self.f.audio_fn, run_async=False,
                                     volume_fn=lambda l: vols.get(l))
        self.assertNotIn("dead", self.verdicts(60))
        self.assertEqual(self.f.audio_calls, [])
        vols[50] = 1.0  # unmuted: counts from then on
        self.assertIn("dead", self.verdicts(40))

    def test_master_muted_and_unknown_volume(self):
        vols = {None: 0.0, 50: 1.0}
        self.m = w.SourceDeadMonitor(self.f.layers, self.f.audio_fn, run_async=False,
                                     volume_fn=lambda l: vols.get(l))
        self.assertNotIn("dead", self.verdicts(60))
        self.m = w.SourceDeadMonitor(self.f.layers, self.f.audio_fn, run_async=False,
                                     volume_fn=lambda l: None)
        self.assertIn("dead", self.verdicts(40))

    def test_parse_mixer_reply(self):
        self.assertEqual(w.parse_mixer_reply("201 MIXER OK\r\n0.5\r\n"), 0.5)
        self.assertEqual(w.parse_mixer_reply("201 MIXER OK\r\n1 0 linear\r\n"), 1.0)
        self.assertIsNone(w.parse_mixer_reply("401 MIXER ERROR\r\n"))
        self.assertIsNone(w.parse_mixer_reply(""))


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

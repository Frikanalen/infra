"""Dropout detector: parsing, classification, and the real ffmpeg graph on
synthetic audio (skipped without ffmpeg)."""
import importlib.util
import math
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
import wave

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location(
    "watchdog_do", os.path.join(HERE, "..", "files", "caspar-sdi-audio-watchdog.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
w.log = lambda *a, **k: None

RATE = 48000


def tone(seconds, freq=440.0, amp=0.25, right=True):
    n = int(seconds * RATE)
    out = []
    for i in range(n):
        v = int(amp * 32767 * math.sin(2 * math.pi * freq * i / RATE))
        out.append((v, v if right else 0))
    return out


def write_wav(path, frames):
    with wave.open(path, "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(RATE)
        f.writeframes(b"".join(struct.pack("<hh", l, r) for l, r in frames))


def count_dropouts(frames):
    """Run the watchdog's own filter graph over frames; count dropouts the
    way the reader does (gap length from silencedetect, context level from
    the 1 s RMS of the mono level stream)."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.wav")
        write_wav(path, frames)
        cmd = w.reader_command(path)
        cmd[cmd.index("-analyzeduration"):cmd.index("-analyzeduration") + 4] = []
        p = subprocess.run(cmd, capture_output=True)
        lvl = p.stdout
        rms_by_second = []
        for i in range(0, len(lvl) - 1, 2 * 8000):
            chunk = struct.unpack(f"<{len(lvl[i:i + 16000]) // 2}h", lvl[i:i + 16000])
            r = math.sqrt(sum(x * x for x in chunk) / max(1, len(chunk))) / 32768
            rms_by_second.append(20 * math.log10(r) if r > 0 else -120)
        n = 0
        g = w.GapFilter()
        last_end = 0.0

        def count(durs):
            nonlocal n
            for dur in durs:
                sec = min(int(last_end), len(rms_by_second) - 1)
                if w.is_dropout(dur, rms_by_second[sec]):
                    n += 1

        for line in p.stderr.decode(errors="replace").splitlines():
            if "silence_end:" in line:
                last_end = float(line.split("silence_end:")[1].split("|")[0])
            count(g.feed(line, 0.0))
        count(g.flush(10.0))
        return n


class Pure(unittest.TestCase):
    def test_parse(self):
        line = "[Parsed_silencedetect_4 @ 0x1] silence_end: 12.5 | silence_duration: 0.0042"
        self.assertEqual(w.parse_silence_end(line), 0.0042)
        self.assertIsNone(w.parse_silence_end("[Parsed_silencedetect_4 @ 0x1] silence_start: 3"))

    def test_is_dropout(self):
        self.assertTrue(w.is_dropout(0.004, -25))
        self.assertFalse(w.is_dropout(0.5, -25))     # a real pause
        self.assertFalse(w.is_dropout(0.004, -70))   # quiet context
        self.assertFalse(w.is_dropout(0.004, None))

    def test_classify(self):
        self.assertEqual(w.classify_dropouts(0, 40), "sdi")
        self.assertEqual(w.classify_dropouts(30, 40), "shared")
        self.assertEqual(w.classify_dropouts(30, 2), "multicast")
        self.assertEqual(w.classify_dropouts(8, 40), None)   # mixed, below the line
        self.assertIsNone(w.classify_dropouts(3, 5))

    def test_monitor_sustain(self):
        m = w.DropoutMonitor()
        self.assertEqual(m.tick(100, 0, 40), ("sdi", 0, True))
        self.assertEqual(m.tick(220, 0, 40), ("sdi", 120, False))
        self.assertEqual(m.tick(221, 0, 0)[0], None)

    def test_layers_summary_strips_credentials(self):
        xml = ("<channel><stage><layer><layer_55><foreground><producer>ffmpeg</producer><file>"
               "<path>srt://key:pw@192.0.2.1:8890?x</path></file></foreground></layer_55>"
               "<layer_50><foreground><producer>empty</producer></foreground></layer_50>"
               "<layer_601><foreground><file><path>https://frikanalen.tv/graphics/?d=1</path></file>"
               "<producer>html</producer></foreground></layer_601></layer></stage></channel>")
        out = w.summarize_layers(xml)
        self.assertIn("55:ffmpeg:srt://192.0.2.1", out)
        self.assertIn("601:html:", out)
        self.assertNotIn("pw@", out)
        self.assertNotIn("50:", out)


class Filter(unittest.TestCase):
    def ev(self, g, line):
        return g.feed("[Parsed_silencedetect_4 @ 0x1] " + line, 0.0)

    def test_isolated_short_gap_is_confirmed_when_no_pause_follows(self):
        g = w.GapFilter()
        self.ev(g, "silence_start: 1.0")
        self.ev(g, "silence_end: 1.002 | silence_duration: 0.002")
        self.assertEqual(self.ev(g, "silence_start: 2.0"), [0.002])

    def test_fade_tail_right_before_a_pause_is_dropped(self):
        g = w.GapFilter()
        self.ev(g, "silence_end: 3.499 | silence_duration: 0.000229")
        self.assertEqual(self.ev(g, "silence_start: 3.4996"), [])
        self.assertEqual(g.flush(10.0), [])

    def test_short_run_right_after_a_pause_is_dropped(self):
        g = w.GapFilter()
        self.ev(g, "silence_end: 5.0 | silence_duration: 0.8")
        self.ev(g, "silence_end: 5.01 | silence_duration: 0.002")
        self.assertEqual(g.flush(10.0), [])

    def test_flush_confirms_after_a_while(self):
        g = w.GapFilter()
        g.feed("silence_end: 1.002 | silence_duration: 0.002", 100.0)
        self.assertEqual(g.flush(100.2), [])
        self.assertEqual(g.flush(100.6), [0.002])


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class WithFfmpeg(unittest.TestCase):
    def test_injected_zero_runs_are_counted(self):
        frames = tone(3.0)
        for k in range(10):          # ten 1-10 ms holes, both channels
            at = 4800 + k * 12000
            frames[at:at + 48 * (k + 1)] = [(0, 0)] * (48 * (k + 1))
        self.assertEqual(count_dropouts(frames), 10)

    def test_speech_like_pauses_and_fades_do_not_count(self):
        frames = tone(1.0)
        frames += [(0, 0)] * int(0.5 * RATE)  # a half-second pause: too long to be a dropout
        frames += tone(1.0)
        # A fade-out into silence, then the next segment.
        frames += [(int(l * (1 - i / RATE)), int(r * (1 - i / RATE))) for i, (l, r) in enumerate(tone(1.0))]
        frames += [(0, 0)] * int(0.4 * RATE)
        frames += tone(1.0)
        frames += tone(1.0, amp=0.0005)      # very quiet passage, not zero
        self.assertEqual(count_dropouts(frames), 0)

    def test_mono_programme_with_silent_right_channel_does_not_count(self):
        self.assertEqual(count_dropouts(tone(2.0, right=False)), 0)


if __name__ == "__main__":
    unittest.main()


class Capture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = dict(w.CFG)
        w.CFG.update(CAPTURE_DIR=self.tmp, CAPTURE_INTERVAL_SECONDS=900, CAPTURE_KEEP=3,
                     CAPTURE_MAX_BYTES=10 ** 9, CAPTURE_SECONDS=60)
        self.calls = []
        xml = ("<channel><stage><layer><layer_50><foreground><producer>ffmpeg</producer><file>"
               "<path>/mnt/media/1/original/a.mp4</path><time>308.5</time><time>1200</time></file>"
               "</foreground></layer_50></layer></stage></channel>")
        self.c = w.Capturer({"multicast": "udp://m", "return": "http://r"},
                            popen=lambda cmd, **k: self.calls.append(cmd), info=lambda: xml)

    def tearDown(self):
        shutil.rmtree(self.tmp)
        w.CFG.clear()
        w.CFG.update(self.cfg)

    def test_captures_both_streams_with_a_sidecar(self):
        import json
        d = self.c.capture("dropouts-shared", 1_800_000_000.0, multicast_per_min=40, return_per_min=35)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all("-c" in c and "copy" in c and "60" in c for c in self.calls))
        side = json.load(open(os.path.join(d, "episode.json")))
        self.assertEqual(side["reason"], "dropouts-shared")
        self.assertEqual(side["layers"][0], {"layer": 50, "producer": "ffmpeg",
                                             "path": "/mnt/media/1/original/a.mp4",
                                             "time": 308.5, "length": 1200.0})
        self.assertEqual(side["multicast_per_min"], 40)

    def test_rate_limited(self):
        self.assertIsNotNone(self.c.capture("jam", 1000.0))
        self.assertIsNone(self.c.capture("jam", 1500.0))
        self.assertIsNotNone(self.c.capture("jam", 1901.0))

    def test_keeps_the_newest(self):
        for k in range(5):
            self.c.capture("jam", 1_800_000_000.0 + 1000 * k)
        self.assertEqual(len(os.listdir(self.tmp)), 3)
        self.assertTrue(sorted(os.listdir(self.tmp))[-1].startswith("20270115T090640Z"))

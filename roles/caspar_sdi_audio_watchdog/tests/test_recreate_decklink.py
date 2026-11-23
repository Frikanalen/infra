"""Unit tests for the DeckLink consumer re-create script, with a fake AMCP."""
import importlib.util
import os
import unittest

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location(
    "recreate", os.path.join(HERE, "..", "files", "caspar-recreate-decklink.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

DECKLINK_OK = ("<port_301><consumer>decklink</consumer><decklink><buffer-depth>3</buffer-depth>"
               "<embedded-audio>true</embedded-audio><index>1</index><key-only>false</key-only>"
               "<keyer>internal</keyer><latency>normal</latency></decklink></port_301>")
DECKLINK_EXTERNAL = DECKLINK_OK.replace("internal", "external")
FFMPEG = "<port_126375><consumer>ffmpeg</consumer></port_126375>"


def info(*ports):
    return ('<?xml version="1.0" encoding="utf-8"?>\n<channel><output><port>'
            + "".join(ports) + "</port></output><stage/></channel>")


class FakeAmcp:
    """Scripted replies: infos is the sequence of INFO 1 documents."""

    def __init__(self, infos, remove=202, add=202):
        self.infos = list(infos)
        self.remove, self.add = remove, add
        self.sent = []

    def command(self, cmd):
        self.sent.append(cmd)
        if cmd.startswith("REMOVE"):
            return self.remove, ""
        if cmd.startswith("ADD"):
            return self.add, ""
        if cmd.startswith("INFO"):
            doc = self.infos.pop(0) if len(self.infos) > 1 else self.infos[0]
            return 201, doc
        raise AssertionError(cmd)


class Clock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, s):
        self.t += s

    def __call__(self):
        return self.t


def run(amcp):
    c = Clock()
    return r.recreate(amcp, sleep=c.sleep, clock=c, out=lambda *a: None)


class Recreate(unittest.TestCase):
    def test_remove_wait_add_verify(self):
        amcp = FakeAmcp([info(DECKLINK_OK, FFMPEG), info(FFMPEG), info(FFMPEG), info(DECKLINK_OK, FFMPEG)])
        self.assertTrue(run(amcp))
        self.assertEqual(amcp.sent[0], "REMOVE 1-301")
        add = [c for c in amcp.sent if c.startswith("ADD")]
        self.assertEqual(add, ["ADD 1-301 DECKLINK 1 EMBEDDED_AUDIO INTERNAL_KEY"])
        # ADD only after INFO showed the port gone.
        i_add = amcp.sent.index(add[0])
        self.assertTrue(any(c.startswith("INFO") for c in amcp.sent[1:i_add]))

    def test_port_never_goes_away(self):
        amcp = FakeAmcp([info(DECKLINK_OK, FFMPEG)])
        self.assertFalse(run(amcp))
        self.assertFalse(any(c.startswith("ADD") for c in amcp.sent))

    def test_add_refused(self):
        amcp = FakeAmcp([info(FFMPEG)], add=501)
        self.assertFalse(run(amcp))

    def test_remove_refused(self):
        self.assertFalse(run(FakeAmcp([info(FFMPEG)], remove=404)))

    def test_wrong_keyer_after_add_fails(self):
        amcp = FakeAmcp([info(FFMPEG), info(DECKLINK_EXTERNAL, FFMPEG)])
        self.assertFalse(run(amcp))

    def test_consumer_must_come_back(self):
        self.assertFalse(run(FakeAmcp([info(FFMPEG)])))

    def test_port_parsing(self):
        self.assertIsNone(r.consumer_on_port(info(FFMPEG), "301"))
        self.assertTrue(r.healthy(r.consumer_on_port(info(DECKLINK_OK), "301")))
        self.assertFalse(r.healthy(None))


class FakeSock:
    def __init__(self, data):
        self.data = data
        self.sent = b""

    def sendall(self, b):
        self.sent += b

    def recv(self, n):
        out, self.data = self.data[:7], self.data[7:]  # small reads on purpose
        return out


class AmcpParsing(unittest.TestCase):
    def client(self, data):
        a = r.Amcp.__new__(r.Amcp)
        a.sock, a.buf = FakeSock(data), b""
        return a

    def test_info_xml_as_casparcg_25_sends_it_then_next_reply(self):
        doc = info(DECKLINK_OK).replace("><", ">\n<")
        a = self.client(b"201 INFO OK\r\n" + doc.encode() + b"\n\r\n" + b"202 REMOVE OK\r\n")
        code, body = a.command("INFO 1")
        self.assertEqual(code, 201)
        self.assertTrue(r.healthy(r.consumer_on_port(body, "301")))
        self.assertEqual(a.command("REMOVE 1-301"), (202, ""))

    def test_error_reply(self):
        self.assertEqual(self.client(b"501 ADD FAILED\r\n").command("ADD 1-301 DECKLINK 1")[0], 501)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Re-create CasparCG's DeckLink consumer over AMCP: the soft remedy for a
jammed SDI embedder (it resets the card's audio lead, like a restart, with a
~1-2 s SDI break instead of a re-cue).

  REMOVE 1-301, wait until port 301 is gone from INFO 1 (5 s at most),
  ADD 1-301 DECKLINK 1 EMBEDDED_AUDIO INTERNAL_KEY, and check within 5 s
  that INFO 1 lists port 301 as a decklink with the internal keyer and
  embedded audio.

REMOVE first and wait: an ADD to a port still in use tears the old consumer
down asynchronously, and the new one fails to open the device the old one
still holds (the 501 on 16 Nov). The flags are case-insensitive, but a
misspelled one is silently ignored (external keyer, no audio), so the result
is checked. buffer-depth can't be set over AMCP: the re-added consumer gets
the default (3) until the next CasparCG restart.

Exit 0 on success, 1 on any failure (the watchdog then restarts CasparCG).
"""

import os
import re
import socket
import sys
import time

HOST = os.environ.get("CASPAR_AMCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("CASPAR_AMCP_PORT", "5250"))
CHANNEL = os.environ.get("CASPAR_CHANNEL", "1")
DEVICE = os.environ.get("DECKLINK_DEVICE", "1")
CONSUMER_PORT = os.environ.get("DECKLINK_CONSUMER_PORT", "301")
TIMEOUT = float(os.environ.get("RECREATE_TIMEOUT", "5"))


class Amcp:
    """A minimal AMCP client: one command, one reply."""

    def __init__(self, host=HOST, port=PORT):
        self.sock = socket.create_connection((host, port), 5)
        self.sock.settimeout(5)
        self.buf = b""

    def _line(self):
        while b"\r\n" not in self.buf:
            data = self.sock.recv(65536)
            if not data:
                raise ConnectionError("CasparCG closed the connection")
            self.buf += data
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line.decode(errors="replace")

    def command(self, cmd):
        """Returns (code, body). Raises on a connection problem."""
        self.sock.sendall((cmd + "\r\n").encode())
        status = self._line()
        code = int(status[:3])
        if code == 201:
            body = self._line()
            # CasparCG 2.5 sends INFO as a whole XML document after 201, with
            # bare \n line ends, ended by "\n\r\n".
            if body.startswith("<?xml"):
                while not body.rstrip().endswith("</channel>"):
                    body += "\n" + self._line()
            return code, body
        if code == 200:
            lines = []
            while True:
                line = self._line()
                if line == "":
                    return code, "\n".join(lines)
                lines.append(line)
        return code, ""


def consumer_on_port(info_xml, port):
    """The <port_N> block of INFO 1, or None."""
    m = re.search(rf"<port_{port}>(.*?)</port_{port}>", info_xml or "", re.S)
    return m.group(1) if m else None


def healthy(block):
    return (block is not None
            and "<consumer>decklink</consumer>" in block
            and "<keyer>internal</keyer>" in block
            and "<embedded-audio>true</embedded-audio>" in block)


def recreate(amcp, sleep=time.sleep, clock=time.monotonic, out=print):
    target = f"{CHANNEL}-{CONSUMER_PORT}"
    code, _ = amcp.command(f"REMOVE {target}")
    out(f"REMOVE {target}: {code}")
    if code >= 400:
        out("REMOVE failed")
        return False
    deadline = clock() + TIMEOUT
    while True:
        sleep(0.25)
        _, info = amcp.command(f"INFO {CHANNEL}")
        if consumer_on_port(info, CONSUMER_PORT) is None:
            break
        if clock() > deadline:
            out(f"port {CONSUMER_PORT} still there after {TIMEOUT:.0f} s")
            return False
    sleep(1.0)  # let the old consumer release the device
    cmd = f"ADD {target} DECKLINK {DEVICE} EMBEDDED_AUDIO INTERNAL_KEY"
    code, _ = amcp.command(cmd)
    out(f"{cmd}: {code}")
    if code >= 400:
        out("ADD failed")
        return False
    deadline = clock() + TIMEOUT
    while True:
        _, info = amcp.command(f"INFO {CHANNEL}")
        block = consumer_on_port(info, CONSUMER_PORT)
        if healthy(block):
            out(f"port {CONSUMER_PORT}: decklink, internal keyer, embedded audio")
            return True
        if clock() > deadline:
            out(f"port {CONSUMER_PORT} not healthy after {TIMEOUT:.0f} s: {block!r}")
            return False
        sleep(0.25)


def main():
    try:
        ok = recreate(Amcp())
    except (OSError, ValueError) as e:
        print(f"AMCP error: {e!r}")
        ok = False
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

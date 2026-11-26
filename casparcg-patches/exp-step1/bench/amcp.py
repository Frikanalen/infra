#!/usr/bin/env python3
"""Tiny AMCP client for CasparCG 2.5 (stdlib only).

Usage:
  amcp.py [-H HOST] [-p PORT] [-t TIMEOUT] [-q] 'CMD 1' ['CMD 2' ...]
  echo 'INFO 1' | amcp.py -          # read commands from stdin, one per line

Each command is sent on one connection, in order. For every command the full
response is printed to stdout, prefixed by '>> CMD' unless -q is given.
Exit status: 0 if every response code was < 400, 1 if any was >= 400,
2 on connection/protocol errors.

Env defaults: CASPAR_HOST (127.0.0.1), CASPAR_PORT (5250).
"""
import argparse
import os
import socket
import sys
import time


def read_response(sock, buf, timeout):
    """Return (code, text, remaining_buf).

    AMCP: 202/4xx/5xx = status line only; 201 = status + one data line
    (INFO <ch> in 2.5 sends 201 + a multi-line XML document); 200 = status +
    lines until an empty line. For 200/201 we additionally stop when the
    socket has been idle for a short while, which copes with XML replies.
    """
    deadline = time.monotonic() + timeout

    def fill(idle=None):
        nonlocal buf
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("AMCP response timeout")
        sock.settimeout(min(remaining, idle) if idle else remaining)
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            return False
        if not chunk:
            raise ConnectionError("AMCP connection closed")
        buf += chunk
        return True

    while b"\r\n" not in buf:
        if not fill():
            raise TimeoutError("AMCP response timeout")
    line, buf = buf.split(b"\r\n", 1)
    status = line.decode("utf-8", "replace")
    try:
        code = int(status.split(" ", 1)[0])
    except ValueError:
        code = 0
    lines = [status]
    if code in (200, 201):
        xml = False
        while True:
            if b"\r\n" in buf or (b"\n" in buf and xml):
                sep = b"\r\n" if b"\r\n" in buf else b"\n"
                l, buf = buf.split(sep, 1)
                s = l.decode("utf-8", "replace")
                if code == 200 and s == "":
                    break
                lines.append(s)
                if s.startswith("<?xml"):
                    xml = True
                if code == 201 and not xml:
                    break
                if xml and s.strip() == "</channel>":
                    # swallow the trailing blank line if it is already there
                    if buf.startswith(b"\r\n"):
                        buf = buf[2:]
                    break
                continue
            if not fill(idle=0.4):
                break  # idle: assume the reply is complete
    return code, "\n".join(lines), buf


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-H", "--host", default=os.environ.get("CASPAR_HOST", "127.0.0.1"))
    ap.add_argument("-p", "--port", type=int, default=int(os.environ.get("CASPAR_PORT", "5250")))
    ap.add_argument("-t", "--timeout", type=float, default=float(os.environ.get("AMCP_TIMEOUT", "10")))
    ap.add_argument("-q", "--quiet", action="store_true", help="do not echo the command")
    ap.add_argument("commands", nargs="+")
    a = ap.parse_args()

    cmds = a.commands
    if cmds == ["-"]:
        cmds = [l.rstrip("\r\n") for l in sys.stdin if l.strip()]

    try:
        sock = socket.create_connection((a.host, a.port), timeout=a.timeout)
    except OSError as e:
        print(f"amcp: cannot connect to {a.host}:{a.port}: {e}", file=sys.stderr)
        return 2
    rc = 0
    buf = b""
    try:
        for c in cmds:
            sock.sendall(c.encode("utf-8") + b"\r\n")
            code, text, buf = read_response(sock, buf, a.timeout)
            if not a.quiet:
                print(f">> {c}")
            print(text)
            sys.stdout.flush()
            if code == 0 or code >= 400:
                rc = 1
    except (OSError, TimeoutError, ConnectionError) as e:
        print(f"amcp: {e}", file=sys.stderr)
        return 2
    finally:
        sock.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())

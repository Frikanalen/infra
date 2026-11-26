#!/usr/bin/env python3
"""Minimal fake CasparCG AMCP server for dry-running the bench scripts on any
machine (no CasparCG, no DeckLink). Not a CasparCG emulator: it only tracks
which consumer ports exist and answers the commands the kit sends.

  mock_caspar.py [--port 5250] [--diag-log FILE] [--exp-file FILE]
                 [--remove-delay 1.0] [--diag-hz 1]

ADD <ch> DECKLINK <n> ... -> port 300+n; ADD <ch> STREAM/FILE ... -> port 100000+ch.
REMOVE <ch>-<port> drops the port after --remove-delay seconds (like the async
consumer teardown). With --diag-log it appends an 'I START' line (including the
experiment file's first line) on ADD DECKLINK and one fake 'S' line per second.
"""
import argparse
import datetime
import random
import socketserver
import threading
import time

STATE = {"ports": {}, "lock": threading.Lock()}
ARGS = None


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def diag(line):
    if ARGS.diag_log:
        with open(ARGS.diag_log, "a") as fh:
            fh.write(f"{now_iso()} dl1/ch1 {line}\n")


def info_xml(ch):
    with STATE["lock"]:
        ports = {p: n for (c, p), n in STATE["ports"].items() if c == ch}
    body = "".join(f"<port_{p}><consumer>{n}</consumer></port_{p}>" for p, n in sorted(ports.items()))
    return ('201 INFO OK\r\n<?xml version="1.0" encoding="utf-8"?>\r\n<channel>\r\n'
            f"   <output><port>{body}</port></output>\r\n</channel>\r\n\r\n")


def handle(cmd):
    w = cmd.split()
    if not w:
        return "400 ERROR\r\n"
    verb = w[0].upper()
    if verb == "VERSION":
        return "201 VERSION OK\r\n2.5.0 mock\r\n"
    if verb == "INFO" and len(w) == 2:
        return info_xml(int(w[1].split("-")[0]))
    if verb == "ADD" and len(w) >= 3:
        ch = int(w[1].split("-")[0])
        kind = w[2].upper()
        if kind == "DECKLINK":
            port = 300 + int(w[3]) if len(w) > 3 and w[3].isdigit() else 301
            exp = ""
            if ARGS.exp_file:
                try:
                    exp = open(ARGS.exp_file).readline().strip()
                except OSError:
                    exp = "(no file)"
            diag(f"I START mock 720p50 buffer_depth=3 exp={exp}")
        elif kind in ("STREAM", "FILE"):
            port = 100000 + ch
        else:
            return "404 ADD ERROR\r\n"
        with STATE["lock"]:
            STATE["ports"][(ch, port)] = kind.lower()
        return "202 ADD OK\r\n"
    if verb == "REMOVE" and len(w) >= 2 and "-" in w[1]:
        ch, port = (int(x) for x in w[1].split("-", 1))
        with STATE["lock"]:
            if (ch, port) not in STATE["ports"]:
                return "404 REMOVE FAILED\r\n"

        def later():
            time.sleep(ARGS.remove_delay)
            with STATE["lock"]:
                STATE["ports"].pop((ch, port), None)
            if port < 1000:
                diag("I STOP")
        threading.Thread(target=later, daemon=True).start()
        return "202 REMOVE OK\r\n"
    if verb in ("PLAY", "CLEAR", "MIXER", "STOP", "LOAD", "LOADBG"):
        return f"202 {verb} OK\r\n"
    return "400 ERROR\r\n"


class H(socketserver.StreamRequestHandler):
    def handle(self):
        for raw in self.rfile:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self.wfile.write(handle(line).encode())


def s_lines():
    fr = 0
    while True:
        time.sleep(1.0 / ARGS.diag_hz)
        with STATE["lock"]:
            on = any(p < 1000 for (_, p) in STATE["ports"])
        if on:
            fr += 50
            cb = random.choice([1.7] * 50 + [35.0])
            late = 1 if cb > 30 else 0
            diag(f"S fr={fr} n=50 res=ok:{50 - late},late:{late},drop:0,flush:0,other:0 ba=2860..3828 "
                 f"pop_max={random.uniform(0.1, 0.5):.1f}ms sv_max=0.1ms sa_max=0.0ms cb_max={cb:.1f}ms "
                 f"ia=20.0..{20 + cb:.1f}ms ovf=0 sup=0")


def main():
    global ARGS
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=5250)
    ap.add_argument("--diag-log")
    ap.add_argument("--exp-file")
    ap.add_argument("--remove-delay", type=float, default=1.0)
    ap.add_argument("--diag-hz", type=float, default=1.0)
    ARGS = ap.parse_args()
    threading.Thread(target=s_lines, daemon=True).start()
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", ARGS.port), H) as srv:
        srv.serve_forever()


if __name__ == "__main__":
    main()

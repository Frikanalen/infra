#!/usr/bin/env python3
"""Restart CasparCG when its DeckLink SDI audio is jammed.

CasparCG 2.5's DeckLink consumer can lose embedded audio for good after a
short output timing hiccup, while the mixer and the ffmpeg (multicast)
consumer keep good audio. Only re-creating the consumer brings it back.

This compares two audio levels once a second:
  * pre: the channel's own multicast (before the DeckLink card)
  * SDI: the studio ATEM's audio meter for CasparCG's input, from
    atemkontroll's /state, when fresh; otherwise the encoder appliance's
    program return

With the ATEM meter, "jammed" means the meter sits at its floor (at or below
ATEM_FLOOR_DBFS on both channels: a dead embedder reads exactly -60, while
programme audio never went below -54 in a week of data) while the multicast
is not digital silence (above PRE_SILENCE_DBFS). How loud the multicast is
doesn't matter, so quiet music or speech can't reset the timer: only real
audio on the ATEM input (above ATEM_ALIVE_DBFS) clears it. Seconds that
prove nothing either way (the multicast silent or unreadable, the meter
between floor and alive) neither count nor reset.

Without fresh ATEM levels it falls back to the program return: jammed means
pre above PRE_MIN_DBFS while the return is below POST_MAX_DBFS.

Once jammed for JAM_SECONDS it acts: SOFT_REMEDY_COMMAND first if set (e.g.
re-creating the DeckLink consumer over AMCP), else, or if that didn't clear
it within VERIFY_SECONDS, RESTART_COMMAND (nodm, and with it CasparCG).
Playout re-cues by itself on reconnect.
Air check before restarting, from atemkontroll (the studio ATEM's HTTP API):
  * jam seen on the ATEM meter: restart whether or not CasparCG is on program
    (RESTART_OFF_PROGRAM), except during a transition involving its input.
  * jam seen on the program return: restart only if CasparCG's input is on
    program; another source, a transition or fade-to-black inhibits.
  * atemkontroll unset, unreachable or unclear: ON_UNKNOWN_PROGRAM decides.
After a cut to CasparCG, a jam counts only after PROGRAM_GRACE_SECONDS.
Dropouts: a second detector counts short gaps in both decoded streams, per
rolling minute: runs of (near) digital zero on all channels, 8 samples to
DROPOUT_MAX_SECONDS long, inside audio that is otherwise not silent (the
second's RMS above DROPOUT_CONTEXT_DBFS). Audio PTS holes count too: the
reader zero-fills them (aresample async), so an ffmpeg consumer dropping
frames shows up as zero runs on the multicast. Sustained for
DROPOUT_SUSTAIN_SECONDS at DROPOUT_PER_MINUTE or more:
  * on the program return only: the DeckLink path (the 16 Nov case, cured by
    re-creating the consumer) -> the same remedies as a jam;
  * on both outputs: upstream of the consumers (or the source material
    itself) -> CRIT only, or a restart with RESTART_ON_SHARED_DROPOUTS;
  * on the multicast only: the ffmpeg consumer -> CRIT only.
The layers on air (INFO 1) are logged at the start of each episode.

Rate limits: COOLDOWN_SECONDS between remedies for new jams. A remedy that
leaves the jam in place counts as failed; each failure in a row doubles the
wait before the next (up to MAX_BACKOFF_SECONDS) and logs CRIT. The failure
count resets after FAILURE_RESET_SECONDS of audio. MAX_RESTARTS per
WINDOW_SECONDS is an optional hard cap (0 = none). State survives restarts
of the watchdog.

Config comes from the environment (see the systemd unit). Stdlib only;
needs the ffmpeg CLI.
"""

import collections
import json
import math
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from urllib.parse import urlsplit
from datetime import datetime

CFG = {
    "PRE_URL": os.environ.get("PRE_URL", "udp://239.1.1.51:5004?overrun_nonfatal=1&fifo_size=1000000"),
    "POST_URL": os.environ.get("POST_URL", "http://simula.frikanalen.tv:9094/frikanalen.ts"),
    "PRE_MIN_DBFS": float(os.environ.get("PRE_MIN_DBFS", "-50")),
    "POST_MAX_DBFS": float(os.environ.get("POST_MAX_DBFS", "-70")),
    "JAM_SECONDS": int(os.environ.get("JAM_SECONDS", "30")),
    "STALE_SECONDS": float(os.environ.get("STALE_SECONDS", "5")),
    "COOLDOWN_SECONDS": int(os.environ.get("COOLDOWN_SECONDS", "600")),
    "MAX_RESTARTS": int(os.environ.get("MAX_RESTARTS", "0")),
    "WINDOW_SECONDS": int(os.environ.get("WINDOW_SECONDS", "21600")),
    "RESTART_COMMAND": os.environ.get("RESTART_COMMAND", "systemctl restart nodm"),
    "SOFT_REMEDY_COMMAND": os.environ.get("SOFT_REMEDY_COMMAND", ""),
    "VERIFY_SECONDS": int(os.environ.get("VERIFY_SECONDS", "60")),
    "MAX_BACKOFF_SECONDS": int(os.environ.get("MAX_BACKOFF_SECONDS", "3600")),
    "FAILURE_RESET_SECONDS": int(os.environ.get("FAILURE_RESET_SECONDS", "300")),
    "DRY_RUN": os.environ.get("DRY_RUN", "true").lower() in ("1", "true", "yes"),
    "STATE_FILE": os.environ.get("STATE_FILE", "/var/lib/caspar-sdi-audio-watchdog/state.json"),
    "METRICS_FILE": os.environ.get("METRICS_FILE", ""),
    "ATEM_URL": os.environ.get("ATEM_URL", "").rstrip("/"),
    "ATEM_TOKEN": os.environ.get("ATEM_TOKEN", ""),
    "ATEM_INPUT": int(os.environ.get("ATEM_INPUT", "1")),
    "ATEM_ME": int(os.environ.get("ATEM_ME", "0")),
    "ATEM_INPUT_NAME": os.environ.get("ATEM_INPUT_NAME", ""),
    "PROGRAM_GRACE_SECONDS": int(os.environ.get("PROGRAM_GRACE_SECONDS", "15")),
    "ATEM_FLOOR_DBFS": float(os.environ.get("ATEM_FLOOR_DBFS", "-59.5")),
    "ATEM_ALIVE_DBFS": float(os.environ.get("ATEM_ALIVE_DBFS", "-57")),
    "PRE_SILENCE_DBFS": float(os.environ.get("PRE_SILENCE_DBFS", "-90")),
    "LEVELS_STALE_SECONDS": float(os.environ.get("LEVELS_STALE_SECONDS", "3")),
    "RESTART_OFF_PROGRAM": os.environ.get("RESTART_OFF_PROGRAM", "true").lower() in ("1", "true", "yes"),
    "ON_UNKNOWN_PROGRAM": os.environ.get("ON_UNKNOWN_PROGRAM", "restart"),
    "DROPOUT_NOISE_DBFS": float(os.environ.get("DROPOUT_NOISE_DBFS", "-80")),
    "DROPOUT_MIN_SAMPLES": int(os.environ.get("DROPOUT_MIN_SAMPLES", "8")),
    "DROPOUT_MAX_SECONDS": float(os.environ.get("DROPOUT_MAX_SECONDS", "0.2")),
    "DROPOUT_CONTEXT_DBFS": float(os.environ.get("DROPOUT_CONTEXT_DBFS", "-60")),
    "DROPOUT_PER_MINUTE": int(os.environ.get("DROPOUT_PER_MINUTE", "20")),
    "DROPOUT_SUSTAIN_SECONDS": int(os.environ.get("DROPOUT_SUSTAIN_SECONDS", "120")),
    "RESTART_ON_SHARED_DROPOUTS": os.environ.get("RESTART_ON_SHARED_DROPOUTS", "false").lower() in ("1", "true", "yes"),
    "AMCP_HOST": os.environ.get("AMCP_HOST", "127.0.0.1"),
}

RATE = 8000  # Hz, mono s16le; plenty for an RMS level
BYTES_PER_SECOND = RATE * 2


def log(level, msg, **fields):
    """One line per event; journald → Alloy → Loki. key=value for LogQL."""
    extra = " ".join(f"{k}={v}" for k, v in fields.items())
    print(f"{level} {msg} {extra}".rstrip(), flush=True)


class Level:
    """Reads one source with ffmpeg; keeps the last 1-second RMS in dBFS."""

    def __init__(self, name, url):
        self.name = name
        self.url = url
        self.dbfs = None
        self.updated = 0.0
        self.proc = None
        self.dropouts = collections.deque()  # wall-clock times of counted gaps
        self.lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True, name=name).start()

    def dropouts_last(self, now, seconds=60):
        gaps = getattr(self, "gaps", None)
        if gaps is not None:
            with self.lock:
                confirmed = gaps.flush(now)
            for d in confirmed:
                self.on_gap(d, now)
        with self.lock:
            while self.dropouts and now - self.dropouts[0] > seconds:
                self.dropouts.popleft()
            return len(self.dropouts)

    def clear_dropouts(self):
        with self.lock:
            self.dropouts.clear()

    def on_gap(self, duration, now):
        """A run of near-zero audio ended; count it if it's a dropout."""
        if is_dropout(duration, self.dbfs if self.fresh() else None):
            with self.lock:
                self.dropouts.append(now)

    def _stderr(self, stream):
        gaps = GapFilter()
        self.gaps = gaps
        for raw in iter(stream.readline, b""):
            now = time.time()
            with self.lock:
                confirmed = gaps.feed(raw.decode(errors="replace"), now) + gaps.flush(now)
            for d in confirmed:
                self.on_gap(d, now)

    def fresh(self):
        return self.dbfs is not None and time.monotonic() - self.updated < CFG["STALE_SECONDS"]

    def _run(self):
        backoff = 2
        while True:
            cmd = reader_command(self.url)
            started = time.monotonic()
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                threading.Thread(target=self._stderr, args=(self.proc.stderr,), daemon=True,
                                 name=self.name + "-gaps").start()
                while True:
                    buf = self.proc.stdout.read(BYTES_PER_SECOND)
                    if len(buf) < BYTES_PER_SECOND:
                        break
                    n = len(buf) // 2
                    acc = 0
                    mv = memoryview(buf).cast("h")
                    for s in mv:
                        acc += s * s
                    rms = math.sqrt(acc / n) / 32768.0
                    self.dbfs = 20 * math.log10(rms) if rms > 0 else -120.0
                    self.updated = time.monotonic()
            except Exception as e:  # keep going; fail-safe is "no fresh level"
                log("WARN", "reader error", source=self.name, error=repr(e))
            finally:
                if self.proc and self.proc.poll() is None:
                    self.proc.kill()
                if self.proc:
                    self.proc.wait()
            log("WARN", "reader stopped, restarting", source=self.name)
            if time.monotonic() - started > 60:
                backoff = 2
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


def reader_command(url):
    """ffmpeg reading one source: 1 s levels on stdout (8 kHz mono s16le), and
    on stderr silencedetect events for gaps at 48 kHz on all channels, after
    aresample async has zero-filled any PTS holes."""
    min_d = CFG["DROPOUT_MIN_SAMPLES"] / 48000
    graph = (
        "[0:a:0]asplit=2[a][b];"
        f"[a]aresample={RATE},aformat=sample_fmts=s16:channel_layouts=mono[lvl];"
        "[b]aresample=48000:async=48000,"
        f"silencedetect=n={CFG['DROPOUT_NOISE_DBFS']}dB:d={min_d:.6f}[sd]"
    )
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-loglevel", "info",
        "-analyzeduration", "2000000", "-probesize", "2000000",
        "-i", url, "-filter_complex", graph,
        "-map", "[lvl]", "-f", "s16le", "pipe:1",
        "-map", "[sd]", "-f", "null", "-",
    ]


def parse_silence_end(line):
    """silence_duration from an ffmpeg silencedetect "silence_end" line."""
    if "silence_end:" not in line or "silence_duration:" not in line:
        return None
    try:
        return float(line.rsplit("silence_duration:", 1)[1].split()[0])
    except (IndexError, ValueError):
        return None


class GapFilter:
    """Turns silencedetect events into dropout candidates, leaving out the
    short sub-threshold run a fade leaves right before or after a real pause
    (within EDGE seconds of a gap longer than DROPOUT_MAX_SECONDS)."""

    EDGE = 0.05

    def __init__(self):
        self.pending = None  # (end, duration, wall time)
        self.last_long_end = float("-inf")

    def feed(self, line, wall):
        """Returns the durations confirmed by this line."""
        out = []
        if "silence_start:" in line:
            try:
                start = float(line.rsplit("silence_start:", 1)[1].split()[0])
            except (IndexError, ValueError):
                return out
            if self.pending and start - self.pending[0] >= self.EDGE:
                out.append(self.pending[1])
            # else: a short run right before the next pause: part of a fade
            self.pending = None
            return out
        dur = parse_silence_end(line)
        if dur is None:
            return out
        try:
            end = float(line.split("silence_end:", 1)[1].split("|")[0])
        except (IndexError, ValueError):
            return out
        if dur > CFG["DROPOUT_MAX_SECONDS"]:
            self.last_long_end = end
        elif end - dur - self.last_long_end >= self.EDGE:
            self.pending = (end, dur, wall)
        return out

    def flush(self, wall, after=0.5):
        """Confirm a candidate no pause has followed for a while."""
        if self.pending and wall - self.pending[2] >= after:
            dur, self.pending = self.pending[1], None
            return [dur]
        return []


def is_dropout(duration, context_dbfs):
    """A short gap inside otherwise non-silent audio."""
    return (duration <= CFG["DROPOUT_MAX_SECONDS"]
            and context_dbfs is not None and context_dbfs > CFG["DROPOUT_CONTEXT_DBFS"])


def classify_dropouts(multicast_per_min, return_per_min):
    """Which path the dropouts are on, from the two rolling-minute counts."""
    thr = CFG["DROPOUT_PER_MINUTE"]
    mc, ret = multicast_per_min >= thr, return_per_min >= thr
    if mc and ret:
        return "shared"
    if ret and multicast_per_min < thr / 4:
        return "sdi"
    if mc:
        return "multicast"
    return None


def layers_on_air(host=None, timeout=3):
    """A short summary of INFO 1's foreground layers (producer and path), for
    the log at an episode's start. Read-only; '?' if CasparCG can't be read."""
    try:
        with socket.create_connection((host or CFG["AMCP_HOST"], 5250), timeout) as s:
            s.settimeout(timeout)
            s.sendall(b"INFO 1\r\n")
            buf = b""
            while not buf.rstrip().endswith(b"</channel>"):
                data = s.recv(65536)
                if not data:
                    break
                buf += data
    except OSError as e:
        return f"? ({e!r})"
    return summarize_layers(buf.decode(errors="replace"))


def summarize_layers(xml):
    out = []
    for m in re.finditer(r"<layer_(\d+)>(.*?)</layer_\1>", xml, re.S):
        fg = re.search(r"<foreground>(.*?)</foreground>", m.group(2), re.S)
        if not fg:
            continue
        prods = re.findall(r"<producer>([^<]+)</producer>", fg.group(1))
        prod = next((p for p in prods if p not in ("empty", "transition")), None)
        if prod is None:
            continue
        path = re.search(r"<path>([^<]*)</path>", fg.group(1))
        name = path.group(1) if path else ""
        if "://" in name:
            # Live sources: scheme, host and path only. Credentials sit in the
            # userinfo (rtsp) or the query (srt streamid).
            u = urlsplit(name)
            name = f"{u.scheme}://{u.hostname or ''}{u.path}"
        else:
            name = name.rsplit("/", 1)[-1]
        out.append(f"{m.group(1)}:{prod}:{name[:40]}")
    return ",".join(out) or "none"


def resolve_input(state, name, fallback):
    """The id of the ATEM input whose longName or shortName is name, else fallback."""
    if name and isinstance(state, dict):
        for inp in state.get("inputs") or []:
            if not isinstance(inp, dict):
                continue
            names = {str(inp.get("longName", "")).lower(), str(inp.get("shortName", "")).lower()}
            if name.lower() in names and isinstance(inp.get("id"), int):
                return inp["id"]
    return fallback


def program_of(state, me=0):
    """The program input on M/E me, or None if the state can't be trusted."""
    if not isinstance(state, dict) or not state.get("connected"):
        return None
    for m in state.get("mixEffects") or []:
        if isinstance(m, dict) and m.get("index") == me and isinstance(m.get("program"), int):
            return m["program"]
    return None


def program_decision(state, input_id, me=0, input_name=""):
    """Read an atemkontroll /state document.

    Returns (verdict, detail), verdict one of:
      "on_air"  - our input is on program, no transition: restarting is right
      "inhibit" - another source is on program, or a transition or
                  fade-to-black is under way
      "unknown" - the mixer state can't be trusted
    """
    if not isinstance(state, dict) or not state.get("connected"):
        return "unknown", "atemkontroll not connected to the mixer"
    input_id = resolve_input(state, input_name, input_id)
    mes = [m for m in state.get("mixEffects") or [] if isinstance(m, dict) and m.get("index") == me]
    if not mes:
        return "unknown", f"no M/E {me} in state"
    m = mes[0]
    program = m.get("program")
    if not isinstance(program, int):
        return "unknown", f"program={program!r}"
    ftb = m.get("fadeToBlack") or {}
    if m.get("inTransition") or ftb.get("inTransition"):
        return "inhibit", f"program={program} transition in progress"
    if ftb.get("fullyBlack"):
        return "inhibit", f"program={program} faded to black"
    if program != input_id:
        return "inhibit", f"program={program} (caspar is input {input_id})"
    return "on_air", f"program={program}"


def fetch_atem_state():
    """atemkontroll's /state, or raises."""
    req = urllib.request.Request(CFG["ATEM_URL"] + "/state")
    if CFG["ATEM_TOKEN"]:
        req.add_header("Authorization", "Bearer " + CFG["ATEM_TOKEN"])
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.load(r)


def ask_atem():
    if not CFG["ATEM_URL"]:
        return "unknown", "atemkontroll URL not configured"
    try:
        state = fetch_atem_state()
    except Exception as e:
        return "unknown", f"atemkontroll unreachable: {e!r}"
    return program_decision(state, CFG["ATEM_INPUT"], CFG["ATEM_ME"], CFG["ATEM_INPUT_NAME"])


def input_levels(state, input_id, now, stale_seconds):
    """(left, right) dBFS of the ATEM audio input, or None if not fresh.

    atemkontroll's /state "audio" block (atemkontroll#3): levels arrive ~10
    times a second, floored at -60; an old updatedAt means unknown, not silent.
    """
    if not isinstance(state, dict) or not state.get("connected"):
        return None
    audio = state.get("audio") or {}
    updated = audio.get("updatedAt")
    if not updated:
        return None
    try:
        t = datetime.fromisoformat(updated.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None
    if abs(now - t) > stale_seconds:
        return None
    for inp in audio.get("inputs") or []:
        if isinstance(inp, dict) and inp.get("input") == input_id:
            left, right = inp.get("leftDbfs"), inp.get("rightDbfs")
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return float(left), float(right)
    return None


def atem_signal_decision(state, input_id, me, restart_off_program):
    """Restart decision when the jam was seen on the ATEM's own input meter.

    The meter measures caspar1's SDI directly, so it doesn't matter what is on
    program: off program a restart is harmless and fixes it before it airs.
    Only a transition involving the input inhibits (the grace after a cut is
    applied to the jam timer).
    """
    m = next((m for m in state.get("mixEffects") or []
              if isinstance(m, dict) and m.get("index") == me), None)
    if m is None:
        return "on_air", f"no M/E {me} in state; ATEM meter shows the jam"
    program, preview = m.get("program"), m.get("preview")
    if m.get("inTransition") and input_id in (program, preview):
        return "inhibit", f"transition involving input {input_id} (program={program} preview={preview})"
    if program == input_id:
        return "on_air", f"program={program}"
    if restart_off_program:
        return "on_air", f"program={program}, input {input_id} off program (restart_off_program)"
    return "inhibit", f"program={program} (caspar is input {input_id}; restart_off_program=false)"


class AtemWatch:
    """Polls atemkontroll's /state once a second in the background. Tracks the
    program source (for the grace after a cut to CasparCG) and the ATEM's
    audio meter for CasparCG's input."""

    def __init__(self, every=1.0, start=True):
        self.every = every
        self.state = None
        self.fetched = 0.0
        self.on_air_since = None  # when our input last went to program
        self.started = time.time()
        if start and CFG["ATEM_URL"]:
            threading.Thread(target=self._run, daemon=True, name="atem").start()

    def _run(self):
        while True:
            self.poll(time.time())
            time.sleep(self.every)

    def poll(self, now):
        try:
            state = fetch_atem_state()
        except Exception:
            return  # unknown: keep what we had, it goes stale by itself
        self.state, self.fetched = state, now
        program = program_of(state, CFG["ATEM_ME"])
        if program is None:
            return
        if program != self.ours():
            self.on_air_since = None
        elif self.on_air_since is None:
            # Already on program when we started: no grace needed.
            first = now - self.started < 6
            self.on_air_since = 0.0 if first else now
            if not first:
                log("INFO", "CasparCG cut to program", program=program,
                    grace_s=CFG["PROGRAM_GRACE_SECONDS"])

    def fresh_state(self, now):
        return self.state if now - self.fetched < 3 else None

    def ours(self):
        return resolve_input(self.state, CFG["ATEM_INPUT_NAME"], CFG["ATEM_INPUT"])

    def sdi_levels(self, now):
        return input_levels(self.fresh_state(now), self.ours(), now, CFG["LEVELS_STALE_SECONDS"])

    def jam_start(self, jam_since):
        """When the jam starts to count: not before the grace after a cut."""
        if self.on_air_since:
            return max(jam_since, self.on_air_since + CFG["PROGRAM_GRACE_SECONDS"])
        return jam_since

    def decide(self, signal, now):
        if not CFG["ATEM_URL"]:
            return "unknown", "atemkontroll URL not configured"
        state = self.fresh_state(now)
        if state is None:
            return "unknown", "atemkontroll unreachable or stale"
        if signal == "atem":
            return atem_signal_decision(state, self.ours(), CFG["ATEM_ME"], CFG["RESTART_OFF_PROGRAM"])
        return program_decision(state, CFG["ATEM_INPUT"], CFG["ATEM_ME"], CFG["ATEM_INPUT_NAME"])


class JamTracker:
    """Counts the seconds the SDI audio has been jammed, one tick a second.

    tick() returns what the second showed: "jam", "alive" (clears the
    count) or "hold" (proves nothing: neither counts nor clears).
    """

    def __init__(self):
        self.seconds = 0
        self.signal = None

    def tick(self, atem_levels, pre_dbfs, post_dbfs, in_grace=False):
        """atem_levels: (left, right) if fresh, else None; *_dbfs None if stale.

        in_grace: CasparCG was just cut to program. The program return lags
        the cut, so a return-based jam doesn't count yet; the ATEM's own
        input meter doesn't lag.
        """
        if atem_levels is not None:
            loudest = max(atem_levels)
            if loudest > CFG["ATEM_ALIVE_DBFS"]:
                verdict = "alive"
            elif (loudest <= CFG["ATEM_FLOOR_DBFS"] and pre_dbfs is not None
                  and pre_dbfs > CFG["PRE_SILENCE_DBFS"]):
                verdict = "jam"
            else:
                verdict = "hold"
            signal_ = "atem"
        elif post_dbfs is not None and pre_dbfs is not None:
            signal_ = "return"
            if post_dbfs >= CFG["POST_MAX_DBFS"]:
                verdict = "alive"
            elif pre_dbfs > CFG["PRE_MIN_DBFS"] and not in_grace:
                verdict = "jam"
            else:
                verdict = "hold"
        else:
            signal_, verdict = None, "hold"

        if verdict == "alive":
            self.seconds = 0
        elif verdict == "jam":
            if self.signal is not None and self.signal != signal_:
                self.seconds = 0  # the two signals don't add up
            self.seconds += 1
            self.signal = signal_
        return verdict

    def clear(self):
        self.seconds = 0


class RemedyPolicy:
    """Decides when to act on a jam, and how, from persisted state.

    state: {"restarts": [timestamps of remedies], "failures": int,
            "last_remedy": float, "last_kind": "soft"|"restart"|None,
            "alive_since_remedy": bool, "alive_since": float|None}
    """

    def __init__(self, state):
        self.state = state
        state.setdefault("restarts", [])
        state.setdefault("failures", 0)
        state.setdefault("last_remedy", max(state["restarts"], default=0.0))
        state.setdefault("last_kind", None)
        state.setdefault("alive_since_remedy", True)
        state.setdefault("alive_since", None)

    def saw(self, verdict, now):
        """Feed every second's verdict: audio after a remedy proves it worked."""
        st = self.state
        if verdict == "alive":
            st["alive_since_remedy"] = True
            if st["alive_since"] is None:
                st["alive_since"] = now
            if st["failures"] and now - st["alive_since"] >= CFG["FAILURE_RESET_SECONDS"]:
                log("INFO", "audio back for a while; remedy failure count reset", failures=st["failures"])
                st["failures"] = 0
        elif verdict == "jam":
            st["alive_since"] = None

    def wait_seconds(self):
        """How long after the last remedy the next may come."""
        return min(CFG["COOLDOWN_SECONDS"] * 2 ** self.state["failures"], CFG["MAX_BACKOFF_SECONDS"])

    def decide(self, now):
        """With the jam held long enough: (kind or None, reason)."""
        st = self.state
        st["restarts"] = [t for t in st["restarts"] if now - t < max(CFG["WINDOW_SECONDS"], 86400)]
        since = now - st["last_remedy"]
        if st["last_remedy"] and since < CFG["VERIFY_SECONDS"]:
            return None, "settling"
        failed = st["last_remedy"] and not st["alive_since_remedy"]
        if failed and not st.get("failure_counted"):
            st["failures"] += 1
            st["failure_counted"] = True
            log("CRIT", "remedy did not clear the SDI audio jam", kind=st["last_kind"],
                failures=st["failures"], next_wait_s=self.wait_seconds())
            if st["last_kind"] == "soft":
                # The cheap fix didn't take: go straight to the restart.
                return "restart", "soft remedy failed"
        if since < self.wait_seconds():
            return None, "backoff" if st["failures"] else "cooldown"
        if CFG["MAX_RESTARTS"]:
            recent = [t for t in st["restarts"] if now - t < CFG["WINDOW_SECONDS"]]
            if len(recent) >= CFG["MAX_RESTARTS"]:
                return None, "limit"
        if CFG["SOFT_REMEDY_COMMAND"] and not failed:
            return "soft", "new jam"
        return "restart", "jam persists after a remedy" if failed else "new jam"

    def acted(self, kind, now):
        st = self.state
        st["restarts"].append(now)
        st["last_remedy"] = now
        st["last_kind"] = kind
        st["alive_since_remedy"] = False
        st["alive_since"] = None
        st["failure_counted"] = False


class DropoutMonitor:
    """Tracks dropout episodes from the per-minute counts, once a second."""

    def __init__(self):
        self.kind = None
        self.since = None

    def tick(self, now, multicast_per_min, return_per_min):
        """Returns (kind or None, seconds the kind has held, started now?)."""
        kind = classify_dropouts(multicast_per_min, return_per_min)
        started = False
        if kind != self.kind:
            self.kind, self.since = kind, now
            started = kind is not None
        return kind, (now - self.since if kind else 0), started

    def reset(self):
        self.kind = self.since = None


def run_remedy(kind, why, detail, signal_, pre_dbfs, sdi_desc, policy, extra=None):
    """Run a remedy (soft, falling back to restart) and record it on policy."""
    now = time.time()
    command = CFG["SOFT_REMEDY_COMMAND"] if kind == "soft" else CFG["RESTART_COMMAND"]
    what = "re-creating the DeckLink consumer" if kind == "soft" else "restarting CasparCG"
    fields = dict(reason=why, command=repr(command), air=repr(detail), signal=signal_,
                  pre_dbfs=f"{pre_dbfs:.1f}" if pre_dbfs is not None else "stale", sdi_dbfs=sdi_desc)
    fields.update(extra or {})
    if CFG["DRY_RUN"]:
        log("CRIT", f"DRY RUN, would be {what}", **fields)
    else:
        log("CRIT", what, failures=policy.state["failures"],
            remedies_24h=len(policy.state["restarts"]) + 1, **fields)
        rc = subprocess.call(command, shell=True)
        log("INFO", "remedy command finished", kind=kind, rc=rc)
        if kind == "soft" and rc != 0:
            # The consumer couldn't be re-created cleanly: restart now.
            log("CRIT", "soft remedy failed; restarting CasparCG", rc=rc,
                command=repr(CFG["RESTART_COMMAND"]))
            policy.acted("soft", now)
            kind = "restart"
            rc = subprocess.call(CFG["RESTART_COMMAND"], shell=True)
            log("INFO", "remedy command finished", kind=kind, rc=rc)
    # A dry run also counts, so it logs at the same rate a real run would act.
    policy.acted(kind, now)


def load_state():
    try:
        with open(CFG["STATE_FILE"]) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"restarts": []}


def save_state(state):
    tmp = CFG["STATE_FILE"] + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, CFG["STATE_FILE"])


def write_metrics(pre, post, jammed_for, restarts_in_window, last_restart,
                  dropouts_pre=0, dropouts_post=0):
    path = CFG["METRICS_FILE"]
    if not path or not os.path.isdir(os.path.dirname(path)):
        return
    lines = [
        "# HELP caspar_sdi_audio_pre_dbfs Audio RMS of the channel multicast (before DeckLink).",
        "# TYPE caspar_sdi_audio_pre_dbfs gauge",
        f"caspar_sdi_audio_pre_dbfs {pre.dbfs if pre.fresh() else 'NaN'}",
        "# HELP caspar_sdi_audio_post_dbfs Audio RMS of the program return (after SDI).",
        "# TYPE caspar_sdi_audio_post_dbfs gauge",
        f"caspar_sdi_audio_post_dbfs {post.dbfs if post.fresh() else 'NaN'}",
        "# HELP caspar_sdi_audio_jammed_seconds How long the jammed condition has held.",
        "# TYPE caspar_sdi_audio_jammed_seconds gauge",
        f"caspar_sdi_audio_jammed_seconds {jammed_for}",
        "# HELP caspar_sdi_audio_restarts_window Watchdog restarts in the rate-limit window.",
        "# TYPE caspar_sdi_audio_restarts_window gauge",
        f"caspar_sdi_audio_restarts_window {restarts_in_window}",
        "# HELP caspar_sdi_audio_last_restart_timestamp_seconds Time of the last watchdog restart.",
        "# TYPE caspar_sdi_audio_last_restart_timestamp_seconds gauge",
        f"caspar_sdi_audio_last_restart_timestamp_seconds {last_restart}",
        "# HELP caspar_sdi_audio_dropouts_per_minute Short gaps in the last minute, by output.",
        "# TYPE caspar_sdi_audio_dropouts_per_minute gauge",
        f'caspar_sdi_audio_dropouts_per_minute{{output="multicast"}} {dropouts_pre}',
        f'caspar_sdi_audio_dropouts_per_minute{{output="return"}} {dropouts_post}',
    ]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log("INFO", "starting", **{k.lower(): v for k, v in CFG.items()
                               if not k.endswith("_URL") and k != "ATEM_TOKEN"}, atem_url=CFG["ATEM_URL"] or "unset")
    pre = Level("pre", CFG["PRE_URL"])
    post = Level("post", CFG["POST_URL"])
    state = load_state()
    policy = RemedyPolicy(state)
    dpolicy = RemedyPolicy(state.setdefault("dropouts", {"restarts": []}))
    drops = DropoutMonitor()
    last_drop_crit = 0.0
    jam = JamTracker()
    last_status = 0.0
    last_hold_log = 0.0
    last_inhibit_log = 0.0
    watch = AtemWatch()
    previous = None

    while True:
        time.sleep(1)
        now = time.time()

        levels = watch.sdi_levels(now)
        pre_dbfs = pre.dbfs if pre.fresh() else None
        post_dbfs = post.dbfs if post.fresh() else None
        in_grace = bool(watch.on_air_since) and now < watch.on_air_since + CFG["PROGRAM_GRACE_SECONDS"]
        verdict = jam.tick(levels, pre_dbfs, post_dbfs, in_grace)
        policy.saw(verdict, now)
        sdi_desc = (f"{levels[0]:.1f}/{levels[1]:.1f}" if levels is not None
                    else f"{post_dbfs:.1f}" if post_dbfs is not None else "stale")
        if verdict == "jam" and previous != "jam" and jam.seconds == 1:
            log("WARN", "possible SDI audio jam", signal=jam.signal,
                pre_dbfs=f"{pre_dbfs:.1f}", sdi_dbfs=sdi_desc)
        previous = verdict

        jammed_for = jam.seconds
        write_metrics(pre, post, jammed_for, len(policy.state["restarts"]), policy.state["last_remedy"],
                      pre.dropouts_last(now), post.dropouts_last(now))

        if now - last_status >= 60:
            last_status = now
            save_state(state)  # keep the failure bookkeeping current on disk
            log("INFO", "status",
                pre_dbfs=f"{pre_dbfs:.1f}" if pre_dbfs is not None else "stale",
                post_dbfs=f"{post_dbfs:.1f}" if post_dbfs is not None else "stale",
                sdi_signal="atem" if levels is not None else "return" if post_dbfs is not None else None,
                sdi_dbfs=sdi_desc, last=verdict,
                program=program_of(watch.fresh_state(now), CFG["ATEM_ME"]) if CFG["ATEM_URL"] else "n/a",
                jammed_s=jammed_for, failures=policy.state["failures"],
                remedies_24h=len(policy.state["restarts"]),
                dropouts_multicast_per_min=pre.dropouts_last(now),
                dropouts_return_per_min=post.dropouts_last(now))

        mc_drops, ret_drops = pre.dropouts_last(now), post.dropouts_last(now)
        dkind, dheld, dstarted = drops.tick(now, mc_drops, ret_drops)
        dpolicy.saw("jam" if dkind == "sdi" else "alive" if dkind is None else "hold", now)
        if dstarted:
            log("WARN", "audio dropout episode", path=dkind, multicast_per_min=mc_drops,
                return_per_min=ret_drops, layers=repr(layers_on_air()))
        if dkind and dheld >= CFG["DROPOUT_SUSTAIN_SECONDS"] and jammed_for < CFG["JAM_SECONDS"]:
            act = dkind == "sdi" or (dkind == "shared" and CFG["RESTART_ON_SHARED_DROPOUTS"])
            if not act:
                if now - last_drop_crit >= 300:
                    last_drop_crit = now
                    log("CRIT", "audio dropouts; not acting", path=dkind,
                        multicast_per_min=mc_drops, return_per_min=ret_drops,
                        hint=repr("ffmpeg consumer" if dkind == "multicast"
                                  else "upstream of both consumers, or the source"))
            elif now - policy.state["last_remedy"] >= CFG["COOLDOWN_SECONDS"]:
                kind, why = dpolicy.decide(now)
                if kind is not None:
                    if dkind == "shared":
                        kind = "restart"
                    air_verdict, detail = watch.decide("atem", now)
                    if air_verdict == "inhibit":
                        if now - last_inhibit_log >= 300:
                            last_inhibit_log = now
                            log("WARN", "audio dropouts; remedy inhibited", reason=repr(detail), path=dkind)
                    else:
                        run_remedy(kind, f"{dkind} dropouts ({why})", detail, "dropouts", pre_dbfs,
                                   sdi_desc, dpolicy,
                                   extra=dict(multicast_per_min=mc_drops, return_per_min=ret_drops))
                        pre.clear_dropouts()
                        post.clear_dropouts()
                        drops.reset()
                save_state(state)

        if jammed_for < CFG["JAM_SECONDS"]:
            continue

        kind, why = policy.decide(now)
        if kind is not None and now - dpolicy.state["last_remedy"] < CFG["VERIFY_SECONDS"]:
            kind, why = None, "settling"  # a dropout remedy just ran
        save_state(state)
        if kind is None:
            if why == "limit" and now - last_hold_log >= 300:
                last_hold_log = now
                log("CRIT", "SDI audio jammed but restart limit reached; not acting",
                    jammed_s=jammed_for, window_s=CFG["WINDOW_SECONDS"])
            elif why == "backoff" and now - last_hold_log >= 300:
                last_hold_log = now
                log("CRIT", "SDI audio still jammed after a remedy; backing off",
                    jammed_s=jammed_for, failures=policy.state["failures"],
                    next_in_s=int(policy.wait_seconds() - (now - policy.state["last_remedy"])))
            continue

        air_verdict, detail = watch.decide(jam.signal, now)
        if air_verdict == "unknown":
            air_verdict = "on_air" if CFG["ON_UNKNOWN_PROGRAM"] == "restart" else "inhibit"
            detail = f"program unknown ({detail}); on_unknown_program={CFG['ON_UNKNOWN_PROGRAM']}"
        if air_verdict == "inhibit":
            if now - last_inhibit_log >= 300:
                last_inhibit_log = now
                log("WARN", "SDI audio jammed; remedy inhibited", reason=repr(detail),
                    signal=jam.signal, jammed_s=jammed_for)
            continue

        run_remedy(kind, why, detail, jam.signal, pre_dbfs, sdi_desc, policy,
                   extra=dict(jammed_s=jammed_for))
        save_state(state)
        jam.clear()


if __name__ == "__main__":
    main()

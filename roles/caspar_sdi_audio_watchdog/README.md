# Ansible Role: caspar_sdi_audio_watchdog

Stopgap for a CasparCG 2.5 bug: the DeckLink consumer can lose its embedded SDI
audio for good after a short output timing hiccup, while the mixer and the ffmpeg
(multicast) consumer still have audio. It happened four times on caspar1 on
2026-11-16/17 and 40 times on 19-23 Nov. Each time only re-creating the
consumer (REMOVE/ADD) or restarting CasparCG brought the audio back. The probe
data shows why: after a restart the card holds about 4 frames of audio, and
that lead drops one frame (960 samples) at a time over tens of minutes, mostly
without any late or dropped frame. At 0 the embedder goes silent until the
consumer is re-created.

## What it runs

**caspar-sdi-audio-watchdog**
- Compares two audio levels once a second:
  - pre: the channel's own multicast, from before the DeckLink card, read
    with the ffmpeg CLI.
  - SDI, from one of two sources, and every log line says which (`signal=`):
    - `atem`: the studio ATEM's audio meter for caspar1's input, from
      atemkontroll's `/state` `audio` block (atemkontroll#3), polled every
      second. This is preferred: it measures caspar1's SDI directly,
      whatever is on program.
    - `return`: the encoder appliance's program return, read with ffmpeg.
      Used whenever the ATEM levels are missing or older than 3 s.
- Counts as jammed for 30 s:
  - `atem`: the meter at its floor (at or below -59.5 dBFS on both channels;
    a dead input reads exactly -60, programme audio never went below -54 in a
    week of data) while the multicast is not digital silence (above -90
    dBFS). Its loudness doesn't matter, so quiet music or speech can't reset
    the timer. Only audio above -57 dBFS on the ATEM input clears it. A
    second that proves nothing (multicast silent or unreadable, the meter
    between -59.5 and -57) neither counts nor clears.
  - `return` (fallback): pre above -50 dBFS while the return is below -70 dBFS.
    A dead embedder measures about -83 dBFS there.
- When jammed it acts in two steps:
  1. The soft remedy (default): `files/caspar-recreate-decklink.py`
     re-creates the DeckLink consumer over AMCP. It sends `REMOVE 1-301`,
     waits until port 301 is gone from `INFO 1` (5 s at most), then
     `ADD 1-301 DECKLINK 1 EMBEDDED_AUDIO INTERNAL_KEY`. It checks within 5 s
     that the port is back as a decklink with the internal keyer and embedded
     audio. This is about a 1-2 s SDI break, with no re-cue. The re-added
     consumer gets the default buffer-depth (3) until the next restart.
  2. If the script fails, or the jam isn't cleared within 60 s:
     `systemctl restart nodm`, the same restart as by hand. Playout re-cues
     by itself.
- Also counts **dropouts**: short gaps of near-digital silence (all channels
  below -80 dBFS for 8 samples to 200 ms) inside otherwise non-silent audio.
  It counts them on both the multicast and the program return, per rolling
  minute. Audio PTS holes are zero-filled by the reader, so frames the
  ffmpeg consumer drops count too. A fade's sub-threshold tail right before
  or after a real pause is left out.
  - The counts go in the status line (`dropouts_multicast_per_min`,
    `dropouts_return_per_min`) and the textfile metric
    `caspar_sdi_audio_dropouts_per_minute{output=...}`.
  - At 20/min or more for 2 min, an episode starts. The watchdog logs it,
    with the layers on air from `INFO 1`.
  - On the return only (the DeckLink path, like 16 Nov), it applies the same
    remedies as a jam.
  - On both outputs, or the multicast only, it only logs `CRIT`.
    `_restart_on_shared_dropouts` turns on a restart for dropouts on both
    outputs once there's data to trust it.
- At the start of every jam and every dropout episode, it records 60 s of
  both streams' audio (stream copy) to
  `/var/lib/caspar-sdi-audio-watchdog/captures/<UTC>-<reason>/`. Next to the
  audio it writes `episode.json`: the time, the class, the counts, and the
  layers on air from `INFO 1`, with file paths and positions, so the source
  file can be compared afterwards.
  - At most one capture per 15 min, keeping the newest 50 and at most 2 GB.
- Before restarting, checks what the studio ATEM has on program (M/E 0;
  caspar1 is input 1).
  - Jam seen on the `atem` signal: it restarts whether or not caspar1 is on
    program (`_restart_off_program`, default true), because off program a
    restart is harmless and fixes the audio before it airs. A transition
    involving input 1 inhibits.
  - Jam seen on the `return` signal: it restarts only if input 1 is on
    program. Another source, a transition or fade-to-black inhibits.
  - While inhibited it logs `restart inhibited reason='...'` every 5 min.
  - An unset URL, an unreachable API or an unclear state follow
    `caspar_sdi_audio_watchdog_on_unknown_program`: `restart` (the default) or
    `inhibit`.
  - `caspar_sdi_audio_watchdog_atem_input_name` (optional) finds the input by
    its ATEM name instead of the number.
  - It polls the program every 5 s. After a cut to CasparCG, a jam only starts
    to count 15 s later (`_program_grace_seconds`), because the return lags
    the cut.
  - `tests/` holds unit tests for the `/state` parsing:
    `python3 -m unittest discover -s roles/caspar_sdi_audio_watchdog/tests`
    (also the jam condition and the remedy policy).
- Fails safe: if either input has had no audio for 5 s, it does nothing.
- Rate limits:
  - 10 minutes between remedies for new jams. No cap per window by default:
    the old 3-per-6-h cap left jams on air for hours.
  - A remedy after which the ATEM input shows no audio within 60 s counts as
    failed. It logs `CRIT remedy did not clear the SDI audio jam`, and each
    failure in a row doubles the wait (20, 40, 60 min max). After 5 min of
    audio the count resets.
  - The state is kept in `/var/lib/caspar-sdi-audio-watchdog/state.json`, so
    it survives a watchdog restart.
- `caspar_sdi_audio_watchdog_dry_run: true` (the role default) logs
  `DRY RUN, would restart` instead of restarting.
  - `host_vars/caspar1.yml` sets it to false.
- Logs a status line every minute and one line per event. Lines go to the
  journal, and from there through Alloy to Loki, e.g.
  `{unit="caspar-sdi-audio-watchdog.service"} |= "CRIT"`.
- If a node_exporter textfile directory exists, it also writes
  `caspar_sdi_audio_*` gauges there. caspar1 has no node_exporter today.

**caspar-decklink-audio-probe**
- Keeps `files/decklink-audio-probe.bt` attached to CasparCG with bpftrace,
  and re-attaches it after every CasparCG restart.
- The probe uses uprobes at fixed offsets in the stripped CasparCG 2.5.0 binary.
- Once a second it logs:
  - frame completion results (ok/late/dropped/flushed);
  - the card's buffered video frames and audio samples;
  - the skew between the consumer's audio and video schedules.
- It also logs every non-OK completion.
- It refuses to attach unless the binary's BuildID is
  `caspar_sdi_audio_probe_build_id`. Turn it off with
  `caspar_sdi_audio_probe_enabled: false`.

## Variables

See `defaults/main.yml`.

# Ansible Role: caspar_sdi_audio_watchdog

Stopgap for a CasparCG 2.5 bug: the DeckLink consumer can lose its embedded SDI
audio for good after a short output timing hiccup, while the mixer and the ffmpeg
(multicast) consumer still have audio. It happened four times on caspar1 on
2026-11-16/17. Each time only re-creating the consumer (REMOVE/ADD) or
restarting CasparCG brought the audio back.

## What it runs

**caspar-sdi-audio-watchdog**
- Reads one-second audio RMS from two sources with the ffmpeg CLI:
  - pre: the channel's own multicast, from before the DeckLink card.
  - post: the encoder appliance's program return, from after SDI.
- Counts as jammed when pre is above -50 dBFS while post is below -70 dBFS for 45 s.
  - A dead embedder measures about -83 dBFS on the return.
- When jammed, it runs `systemctl restart nodm`, the same restart as by hand.
  Playout re-cues by itself.
- Fails safe: if either input has had no audio for 5 s, it does nothing.
- Rate-limited:
  - 10 minutes between restarts.
  - At most 3 restarts in 6 h. The count is kept in
    `/var/lib/caspar-sdi-audio-watchdog/state.json`, so it survives a watchdog
    restart.
  - Past the limit it only logs `CRIT ... restart limit reached`, every 5 min.
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

# DeckLink consumer experiment bench kit

Scripts for comparing the runtime-switchable experiments in the patched DeckLink
consumer (`CASPAR_DECKLINK_EXP`, also read from `/run/casparcg/decklink-exp`) under air-like
load. They use only bash, ffmpeg and python3 (stdlib). `pidstat` (sysstat) and `stress-ng`
are used if installed.

> **Safety: never run this on an on-air machine, or on any machine whose SDI, multicast or
> audio feeds anything on air.** `run-matrix.sh` re-creates the DeckLink consumer for every
> mode, which drops SDI for several seconds each time. `load.sh` pushes a 4 Mbit/s multicast
> to `TEST_MCAST`. The scripts that change state refuse to run unless `BENCH_ACK_NOT_ON_AIR=1`
> is set. They also refuse on hosts listed in `BENCH_FORBIDDEN_HOSTS` (default `caspar1 caspar2`),
> on a machine that has `/etc/casparcg-onair`, and against a non-local `CASPAR_HOST` unless
> `BENCH_ALLOW_REMOTE=1`. If the test machine is monitored, silence its alerts for the length of
> the matrix first (see `infra/docs/alerting.md`, `scripts/alert-silence`).

## Files

| File | Purpose |
|---|---|
| `gen-sync-clip.sh` | Makes the 720p50, 10 min, loopable sync clip: a 1 kHz tone at -20 dBFS, plus a 2-frame white flash and a 20 ms 2 kHz beep once per second, with frame number and timecode burned in |
| `load.sh start\|stop\|status` | Builds the load: the sync clip on 1-10, a busy HTML page on 1-20, an x264 veryfast 4 Mbit/s ffmpeg STREAM consumer to a test multicast, an optional 1080p decode on 1-30 and optional `stress-ng` |
| `run-matrix.sh` | Runs a warm-up, then the modes × repeats in alternating order. Re-creates 1-301 for each mode and records runs.csv, captures, pidstat and diag-log slices |
| `capture-analyse.sh` | Analyses one capture: audio gaps per minute, beep and flash interval slips, and A/V offset (median/p95) |
| `summarize.py` | Builds per-run and per-mode tables from a matrix output directory. Writes `summary.csv` |
| `amcp.py` | Tiny AMCP client: `./amcp.py 'INFO 1'` |
| `mock_caspar.py` | Fake AMCP server for dry-running the kit on a laptop. It is not a CasparCG emulator |
| `busy.html` | Local "heavy template" page (canvas particles, blurred CSS, scrolling text). It is the default `HTML_URL`, so no network is needed |
| `analyse_capture.py`, `lib.sh` | Helpers |

Every script takes `--help` and is configured through environment variables. The common ones
are `CASPAR_HOST` (127.0.0.1) and `CASPAR_PORT` (5250).

## Prerequisites on the test machine

* The experiment build of CasparCG 2.5 (diag + keeper + exp patches). Configure channel 1 as 720p50
  with the DeckLink as in production: buffer depth 3, embedded audio, internal key, BGRA. Run the diag
  logger with `CASPAR_DECKLINK_DIAG_CSV=1`, because the per-callback CSV is what gives `cb_us`
  percentiles.
* `/run/casparcg/` must be writable by the user that runs the bench. For example, add
  `RuntimeDirectory=casparcg` and `RuntimeDirectoryMode=0775` to the service, with a shared group.
  Alternatively, point `EXP_FILE` somewhere both sides can reach.
* A way to see the SDI output:
  * a return encoder, set as `CAPTURE_URL=srt://…`, `udp://@239.x.x.x:port` or `http://…`. The capture is
    stream-copied into `.ts`. If the stream's codecs do not fit in TS, set `CAPTURE_EXT` and
    `CAPTURE_CODEC_ARGS`.
  * or a local DeckLink input, set as `CAPTURE_URL="decklink:DeckLink SDI 4K (2)"`. This needs ffmpeg built
    with `--enable-decklink`. Video is scaled to 320x180 ultrafast x264, which is enough for flash
    detection, and audio is kept as PCM.

  Capturing on the test machine itself adds load. Where possible, capture on a second machine:
  run only the capture part there, and keep the timestamps in UTC with NTP.
* NTP on every machine involved, because all logs use UTC.

## Protocol

1. **Prepare**
   ```sh
   export BENCH_ACK_NOT_ON_AIR=1 MEDIA_DIR=/path/to/caspar/media
   ./gen-sync-clip.sh                     # BENCH_SYNC.mov, 10 min, H.264 + PCM
   ./load.sh start                        # add DECODE_CLIP=… STRESS_CPU=4 to taste
   ./load.sh status
   ```
   Check the output on a monitor: the flash appears in the top-left quadrant, the HTML layer is
   bottom-right and the decode clip is top-right. Check the multicast with
   `ffprobe udp://@239.255.0.99:5004`. Then make a 60 s test capture and run
   `capture-analyse.sh` on it. It must report about 60 beeps, about 60 flashes and 0 gaps before you
   continue. If it does not, adjust `FLASH_CROP`, `FLASH_YAVG` or `BEEP_THRESH`.

2. **Calibrate the load.** The goal is to reproduce the air stalls in **base** mode: seconds with
   `cb_max` above 30 ms, `INTER_ARRIVAL` events, and if possible `A_UNACC_STEP` or BA steps. Run base
   for 15 to 20 min (`MODES=base REPEAT=1 RUN_MINUTES=20 WARMUP_MINUTES=0`), then `summarize.py`. If
   there are no stalls, raise the load: `STRESS_CPU`, a second HTML page, `DECODE_CLIP`. Do not change
   the load once the matrix has started, and record the final settings (load.sh writes them to
   `$STATE_DIR/load.env`; copy that file into the output dir).

3. **Run the matrix**
   ```sh
   CAPTURE_URL=srt://returnbox:9000 OUT_DIR=$HOME/bench/$(date -u +%F) ./run-matrix.sh
   ```
   The defaults are a 10 min warm-up in `base` (marked `warmup`; summarize.py leaves it out of the mode
   averages), then 2 rounds over `base ahead ahead,pool=heap ahead,pool=sdk nokeeper`, 45 min each. The
   second round runs in reverse order, so drift such as thermal state, memory fragmentation or HTML
   cache growth does not always favour the same mode. Total: about 8 h. Run it in `tmux`. Ctrl-C
   or SIGTERM closes the current run as `aborted`, removes the exp file and re-creates 1-301 in
   env or base mode.

   Mode strings are written verbatim as the first line of `EXP_FILE`. The consumer build has to treat
   `base` (and an empty line) as "all experiments off". Check that each run's
   `runs/<id>/consumer-start.txt` shows the flags you expect (the experiment build is expected to log
   them on the `I START` line or on an `I EXP` line).

4. **Analyse** (after the matrix, or on another machine. The analysis is CPU heavy, so never run it
   while a run is being measured)
   ```sh
   ./capture-analyse.sh $OUT_DIR/runs/*/cap-*.ts     # writes <capture>.analysis.json
   ./summarize.py $OUT_DIR                            # prints tables, writes summary.csv
   ```

5. **Clean up:** `./load.sh stop`, then remove `/run/casparcg/decklink-exp` if it is still there.

## What to record

Record these per matrix, in `OUT_DIR` next to `matrix.env` (some are written for you):

* the CasparCG build (git describe plus patch hashes), DeckLink driver/firmware version, kernel, CPU governor
* the final load settings (`load.env`, `STRESS_CPU`, HTML URL, decode clip)
* anything unusual: who touched the machine, other jobs, thermal warnings

Per run (written automatically):

* `runs.csv`: id, mode, round, warm-up/measure, start/end UTC, status, casparcg pid, diag log and CSV byte
  offsets, capture count
* `runs/<id>/run.env`, `consumer-start.txt` (diag START line after ADD), `info-*.xml`, `amcp.log`
* `runs/<id>/diag.log`, `diag.csv`: the slice of the diag log and CSV written during the run
* `runs/<id>/pidstat.txt`: `pidstat -h -t -u -w -p <pid> 1`, per-thread CPU and context switches
* `runs/<id>/cap-NN-HHMMSS.*` and `captures.csv`: 60 s of SDI return every 10 min (first at +60 s)
* `runs/<id>/health.log`: only present if the consumer disappeared during the run

## Reading the results

The main metrics, per hour of run (from `summarize.py`):

| Metric | Source | Expectation if an experiment works |
|---|---|---|
| `stall_s_per_h` | diag S lines, seconds with `cb_max` > 30 ms | down (the main target) |
| `cb_p99_us`, `cb_p999_us`, `cb_over_per_h` | diag CSV `cb_us` | down. Other `*_us` columns, such as a new `conv_us`, are listed automatically |
| `ia_stall_s_per_h` | S lines, max inter-arrival > 30 ms | down |
| `late_per_h`, `drop_per_h` | S lines `res=` | ≤ base |
| E events (`A_UNACC_STEP`, `BA_MIN_STEP`, `BA_ZERO`, `INTER_ARRIVAL`) | diag E headers | down or unchanged, and never up |
| `gaps_per_min`, `beep_slips` | captures | 0. Any gap is audible. A beep slip of n × 20 ms means n frames of audio were lost or added |
| `av_offset_med_ms` | captures | constant across modes and runs. A shift means the experiment changed the audio lead |
| `cpu_pct` | pidstat | report it, but it is not the goal |

Treat a mode as better only if it wins in **both** rounds. With 2 × 45 min per mode, you can see a
change in stall rate from tens per hour to a few per hour. A change of less than a factor of about 2
needs more repeats (`REPEAT=4`).

## Dry run without CasparCG

```sh
python3 mock_caspar.py --port 15250 --diag-log /tmp/d.log --exp-file /tmp/exp &
export CASPAR_PORT=15250 BENCH_ACK_NOT_ON_AIR=1
MEDIA_DIR=/tmp/m DURATION_S=20 ./gen-sync-clip.sh
./load.sh start
CASPAR_PID=$! EXP_FILE=/tmp/exp DIAG_LOG=/tmp/d.log OUT_DIR=/tmp/out MODES="base ahead" \
  RUN_SECONDS=15 WARMUP_SECONDS=5 SETTLE_S=1 POST_ADD_SETTLE_S=1 \
  CAPTURE_URL=/tmp/m/BENCH_SYNC.mov CAPTURE_EXT=mkv CAPTURE_FIRST_S=2 CAPTURE_SECONDS=4 CAPTURE_EVERY_S=7 \
  ./run-matrix.sh
./capture-analyse.sh /tmp/out/runs/*/cap-*.mkv && ./summarize.py /tmp/out
```

## Notes and limits

* AMCP syntax used (CasparCG 2.5): `ADD 1 STREAM "<url>" -format mpegts -codec:v libx264 -preset:v veryfast …`.
  The ffmpeg consumer takes `-name value` pairs with no spaces inside a value. Options ending in `:v`
  or `:a` go to that encoder, and the rest go to the muxer/protocol. Its port is `100000 + channel`, so
  `load.sh stop` uses `REMOVE 1-100001`. The DeckLink consumer port is `300 + device` (301). `INFO 1`
  lists consumers as `<port_301>` XML nodes, and the scripts poll for that.
* The consumer is torn down asynchronously after `REMOVE`, so run-matrix polls `INFO 1` until `port_301`
  is gone, then waits `SETTLE_S` more before the `ADD`, so the old and new instances never overlap on
  the card.
* The A/V offset has one-frame (20 ms) resolution on the video side. Compare medians, and treat a
  change of a whole frame as real.
* `silencedetect` at -80 dB / 0.2 ms counts every dropout, including very short ones. Gaps in the first
  second of a capture are ignored (`GAP_SKIP_S`), because that is capture start-up.
* With an AAC return stream, the AAC encoder and decoder can add or hide gaps of a few ms. Prefer a PCM
  return path (DeckLink input, or an SRT encoder with PCM or high-bitrate audio).

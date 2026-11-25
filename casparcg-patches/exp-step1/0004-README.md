# 0004: DeckLink consumer experiments, step 1 (CasparCG 2.5.0)

Patch: `0004-decklink-exp-step1.patch`. Apply with `patch -p1` on top of v2.5.0-stable (69e8ad5) plus
`decklink-diag-v250.patch` (0002) and `decklink-lead-keeper-on-diag-v250.patch` (0003). It changes:

* `src/modules/decklink/consumer/decklink_consumer.cpp`
* `src/modules/decklink/consumer/format_strategy.h`
* `src/modules/decklink/consumer/sdr_bgra_strategy.cpp`
* `src/modules/decklink/consumer/v210_strategies.cpp`

It is a test build. Every experiment is switched at runtime, so one binary covers every configuration. With
`CASPAR_DECKLINK_EXP` unset or empty, the consumer behaves exactly as 0002+0003. The only additions are the new
timing fields and one `EXP` info line at start.

## Why

On the air server, the completion callback (`ScheduledFrameCompleted`) does all of the per-frame work. It pops
from a 1-slot queue, converts the frame (allocating about 3.7 MB and copying it), builds the wrapper, and
schedules video and audio. Under load the callback often takes about 35 ms instead of 1.7 ms. 73% of the
stalls over 30 ms were the callback body itself, mostly the conversion, which was not measured.
Step 1 measures the conversion (E0) and adds two switchable changes: convert ahead of the callback (E1) and
reuse buffers (E2).

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `CASPAR_DECKLINK_EXP` | empty | Comma-separated flags, case-insensitive, spaces ignored. Read once when the consumer is created, so changes apply on the next consumer re-create or restart. Unknown tokens are ignored and logged as a warning. |
| `CASPAR_DECKLINK_EXP_QUEUE` | `3` | Depth of the ready queue with `ahead`, clamped to 1..16. Ignored without `ahead`, where the queue capacity stays at the channel's field_count, as today. A `queue=N` flag overrides it. |
| `CASPAR_DECKLINK_EXP_FILE` | `/run/casparcg/decklink-exp` | If this file exists when the consumer is created, its first line (trailing CR/LF stripped) replaces `CASPAR_DECKLINK_EXP`. An empty first line, or an empty file, means all experiments off. If the file is missing, the environment value is used. This is how `bench/run-matrix.sh` switches modes with REMOVE/ADD and no restart. Remove the file after benching, or it keeps overriding the environment. |

The existing `CASPAR_DECKLINK_DIAG`, `CASPAR_DECKLINK_DIAG_LOG` and `CASPAR_DECKLINK_DIAG_CSV` variables work as before.

### Flags

| Flag | Experiment | What changes |
|---|---|---|
| (none) | E0 only | Nothing functional. The conversion is timed (`cv_us`). |
| `ahead` | E1 | `send()` runs on the consumer proxy's executor thread. It converts the frame right away and pushes a *ready item* into a queue of depth `CASPAR_DECKLINK_EXP_QUEUE`. A ready item holds the image buffer or SDK frame, the audio vector and nb_samples, plus frame1/frame2, which are kept only when there are secondary ports. The callback pops a ready item, blocking if the queue is empty, as today. It then only wraps and schedules video, and schedules audio (with the audio lead keeper, unchanged) and the secondary ports (unchanged). Progressive: one item per frame. Interlaced: `send()` holds the first field and builds one item from both fields when the second arrives, the same pairing as the two `pop()`s today. `send()` now blocks only when the ready queue is full, and it blocks *before* converting. |
| `pool` or `pool=heap` | E2 | The primary port converts into reused 128-byte-aligned heap buffers instead of allocating a new one per frame. A buffer goes back to the free list when the last `shared_ptr` to it is released. That happens in the `decklink_frame` destructor, after the driver's final `Release()`. The free-list state is held by `shared_ptr`, so frames the driver still holds after teardown are safe. The pool keeps at most `ring` idle buffers. Works with every format strategy (BGRA, SDR v210, HDR v210). |
| `queue=N` | E1 | Ready queue depth for `ahead` (1..16), overriding `CASPAR_DECKLINK_EXP_QUEUE`. |
| `nokeeper` | - | Turns off the audio lead keeper (0003): `check_audio_lead()` is not called, so `take_audio_correction()` always returns 0 and no `AUDIO_LEAD_CORRECTED` lines appear. Everything else is as with no flags. Used as the bench's control for the keeper. |
| `base`, `none`, `off`, `-` | - | No-op tokens. A line of just `base` means all experiments off. |
| `pool=sdk` | E2 | The primary port converts straight into frames from `IDeckLinkOutput::CreateVideoFrame` (the strategy's pixel format and row bytes), and those SDK frames are scheduled directly. There is no `decklink_frame` wrapper, so the driver can use its own buffers. A ring of `ring` frames is created at start. A frame is handed out again only when (a) the completion callback has reported it and (b) `AddRef()`/`Release()` shows that only the pool still holds a reference. If no frame is free, that frame falls back to the normal allocate-and-convert path and counts as `pool_miss`. The completed frame's display time, audio time and sample count, which `decklink_frame` stores for the diagnostics and for the late-frame adjustment, are kept per ring slot and found by pointer in the callback. Not available with HDR (the metadata lives on our wrapper), or if `CreateVideoFrame` fails or returns a buffer that is not 16-byte aligned. In those cases it logs a warning and falls back to `pool=heap`. |

Flags combine: `ahead,pool`, `ahead,pool=sdk`, `pool` alone (pooled buffers, still converted in the callback).
`ring` = buffer depth + (queue with `ahead`, else 1) + 4. With buffer depth 3 that is 10 with `ahead` and the
default queue, and 8 without it. At 720p BGRA that is about 37 MB.

## New diagnostic output

### Info line (log file, and also `CASPAR_LOG(info)`)

Written once at start, after `START` and `LEGEND`:

```
... I EXP flags=ahead,pool src=env ahead=1 nokeeper=0 queue=3 capacity=3 pool=heap ring=10 frame_bytes=3686400 secondaries=0
```

`src=` is `env` or `file:<path>`; whitespace is removed from the shown flags. It may also carry `unknown=` and
`note="..."` (the reason for a pool=sdk fallback). With no flags it reads `flags=- src=... ahead=0 nokeeper=0 queue=- capacity=<field_count> pool=off ring=-`. The `STOP` line now ends with
`pool_hits= pool_misses= sdk_ref_busy=` (totals since start). `sdk_ref_busy` counts free-looking SDK frames
skipped because the SDK still held a reference.

### Per-frame fields (frame_sample; E context line and CSV)

They describe the primary-port frame scheduled by *this* callback, wherever it was converted.

| Field (E line / CSV) | Meaning |
|---|---|
| `cv` / `cv_us` | Conversion time in µs, including buffer acquisition. Baseline and `pool`: measured in the callback's primary task. `ahead`: measured in `send()` for the item scheduled now. -1 = no conversion (abort). |
| `al` / `al_us` | Buffer acquisition only (pool free list or SDK ring lookup, plus allocation on a miss). **-1 without `pool`**: in the baseline the allocation happens inside `convert_frame_for_port` and cannot be separated without changing the code path. |
| `cvw` / `cv_where` | Where the conversion ran: `cb`/0 = completion callback (TBB primary task), `snd`/1 = send thread. |
| `swt` / `swait_us` | `ahead` only: how long `send()` waited for queue space before converting this item. -1 otherwise. |
| `age` / `age_us` | `ahead` only: time from the end of the conversion to the callback popping the item. -1 otherwise. |
| `phit`, `pmiss` / `pool_hit`, `pool_miss` | 1 if the destination buffer was reused, or if the pool had none free (fresh allocation or fallback). Both 0 without `pool`. |

`q` (and CSV `q`) is the ready-queue size after the pop when `ahead` is on, and the frame-queue size otherwise.
`pop` is still the time the callback waited for its input. With `ahead`, that is the wait for a converted item.

The CSV columns are **appended at the end**, after `cb_us`:
`...,cb_us,cv_us,al_us,cv_where,swait_us,age_us,pool_hit,pool_miss`.
The CSV header is written only when the file is new. Point `CASPAR_DECKLINK_DIAG_LOG` at a new path, or move the
old `.csv` away, so that the header matches the rows.

### Per-second summary (S)

Appended to the end of the line:

```
... sup=0 cv_max=1.6ms cv_p95=1.4ms al_max=0.0ms swait_max=19.8ms age_max=40.2ms cv_cb=0 pool_hit=50 pool_miss=0
```

| Field | Meaning |
|---|---|
| `cv_max`, `cv_p95` | Maximum and approximate 95th percentile (nearest rank over that second's callbacks, up to 256) of `cv_us` |
| `al_max` | Maximum `al_us` (0 without pool) |
| `swait_max`, `age_max` | Maximum send wait and item age (0 without `ahead`) |
| `cv_cb` | How many of that second's conversions ran on the callback (so 0 with `ahead`) |
| `pool_hit`, `pool_miss` | Pool hits and misses that second. After start, expect `ring` misses (heap: first fills) and then only hits |

## Suggested runs

Run each configuration for at least one busy period, with the CSV on and the same load:

1. `CASPAR_DECKLINK_EXP=` (baseline, now with `cv_us`)
2. `pool`
3. `ahead` (queue 3), then `CASPAR_DECKLINK_EXP_QUEUE=2` and `=4`
4. `ahead,pool`
5. `ahead,pool=sdk`

Compare: `cb_max` and the `INTER_ARRIVAL_*ms` event count, `cv_p95` and `cv_max`, late, dropped and flushed
results, `ba` minimum and `A_UNACC_STEP` / `AUDIO_LEAD_CORRECTED` counts, and, with `ahead`, `pop_max` (the
callback starving) against `swait_max` (the producer blocked).

## Known limitations

* **Latency.** With `ahead`, up to `queue` converted frames wait between the mixer and the driver. Each one adds
  a frame (20 ms at 50p) of output delay compared with consumers that do not use it. Video and audio move
  together, so SDI lip-sync is unchanged, but sync against other outputs on the same channel changes by up to
  that amount. `queue=1` is closest to today.
* **Back-pressure moves.** With `ahead`, `send()`, and so the channel tick, now carries the conversion time
  (normally about 1.5 ms) and blocks when the queue is full. A slow conversion now delays the producer instead
  of the callback. Look at `swait_max` and `pop_max`.
* **Secondary ports** still convert and schedule in the callback (inside `parallel_for`, as today), and their
  conversion is not timed. Pools apply to the primary port only.
* **pool=sdk has not been tested on hardware** (no card in the sandbox). It relies on `CreateVideoFrame` frames
  being schedulable many times, and on `Release()` returning a meaningful count. If the SDK keeps an extra
  internal reference, every acquire is skipped. That shows as `pool_miss` on every frame and a growing
  `sdk_ref_busy` in STOP, and output still works through the fallback path. It is allowed for every SDR
  strategy, not only BGRA, as long as the buffer is 16-byte aligned. HDR falls back to `pool=heap`.
* **Key-only with pool:** the BGRA key-only shuffle runs in place on the pooled buffer (`aligned_memshfl(dst,
  dst, ...)`) instead of into a second fresh buffer. The output is the same and one allocation is saved.
* **VANC:** the 2.5.0 consumer in this tree has no VANC/`vanc_` code, so there was nothing to keep at schedule time.
* Abort or teardown with items still queued: their SDK ring slots stay marked busy until the consumer is
  destroyed. That is harmless, since the pool goes away with the consumer.
* `al_us` is not available for the baseline (see above). The baseline allocation cost is roughly
  `cv_us(baseline) - cv_us(pool, steady state)`.
* Flags are read once per consumer instance, not live.

## Build check

Compiled in the sandbox build (`build-diag`, gcc 13, `-O3 -Werror`, `make decklink`) by temporarily copying the
four files into the configured source tree. The build was clean and the tree was restored afterwards. An extra
`-Wall -Wextra` pass reports no new warnings in the touched files. The patch was checked with `patch -p1
--dry-run` on 69e8ad5 + 0002 + 0003, and the result is identical to the working tree. The env parser and the
heap pool lifetime, including the pool being destroyed before outstanding buffers, were exercised in a small
ASan test. There is no runtime test against a DeckLink device.

## Review fixes (second pass)

* Added the `/run/casparcg/decklink-exp` override (`CASPAR_DECKLINK_EXP_FILE`) that `bench/run-matrix.sh` relies on;
  before, the file was ignored and every bench mode would have run with the environment's flags.
* Added the `nokeeper` flag and the `base`/`none`/`off`/`-` no-op tokens; `nokeeper` was in the bench's default
  MODES but was an unknown (ignored) token, so that run would have silently been `base`.
* Added the `queue=N` flag.
* `ahead`: a conversion exception in `send()` is now also stored in `exception_`, so every later `send()` rethrows
  it, as happens today when the callback's conversion throws.
* `pool=sdk`: the completion callback matches a completed frame to its ring slot by pointer *or* by `GetBytes()`
  buffer. If the driver ever handed back a different interface pointer, the old code would have treated a driver
  frame as a `decklink_frame` (reading garbage, or crashing in the late-frame branch) and leaked the slot.
* Destructor: `abort_request_` is now set while holding `buffer_mutex_`. Before, a callback that had just checked
  its wait predicate could miss the notify and block forever, hanging `StopScheduledPlayback` on REMOVE. This was
  already the case in 0002/0003 (narrow window); the fix doesn't change the no-flags data path. The completion
  callback is also cleared before teardown when `pool=sdk` is active, as it already was with the diag logger on.

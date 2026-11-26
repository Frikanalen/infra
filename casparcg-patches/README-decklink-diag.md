# DeckLink consumer diagnostic build (CasparCG 2.5.0)

Patch: `decklink-diag-v250.patch`. It applies to `v2.5.0-stable` (69e8ad5) and touches only
`src/modules/decklink/consumer/decklink_consumer.cpp`.

## What it is for

On caspar1 the card's buffered embedded audio (`GetBufferedAudioSampleFrameCount`) starts at about
3820 samples. It then drops in exact 960-sample (one-frame) steps at irregular intervals, usually with
no late or dropped completion, and sometimes steps back up by 960. When it reaches 0, SDI audio stays
silent until the consumer is re-created. CasparCG's own `audio_scheduled_` and `video_scheduled_` stay
locked, and buffered video stays at 5.

This build records enough per frame to tell which mechanism causes each step:

| Hypothesis | Signature in the log |
|---|---|
| Completion callback skipped or doubled | `CFT_STEP_n` (completed frame's display time does not advance by exactly 1000), `INTER_ARRIVAL_*`, `INTER_ARRIVAL_SHORT_*` |
| Video playhead jumped | `PLAYHEAD_STEP_<ph step>_HW_<hw delta>` (ph moved differently from the hardware clock), `PH_CFT_SHIFT_*` |
| Driver discarded an audio block it had accepted | `SCHED_ACCEPT_<got>_OF_<ns>`: buffered audio right after `ScheduleAudioSamples` minus right before differs from the block size by 48 samples or more, while `sw == ns` and `hr == S_OK` |
| Driver rejected or shortened a block | `SAMPLES_WRITTEN_x_OF_y`, `AUDIO_HR_FAIL` |
| Frame carried the wrong number of samples | `NS_CADENCE_n`, `AV_SKEW_n` |
| Audio-only slip inside the driver | `A_UNACC_STEP_n` while ph, hw and bv stay regular and none of the above fire |
| Late scheduling relative to the hardware frame | `s_lead`, `s_inf` (deadline margin when `ScheduleAudioSamples` is called), `q`, `cpu`, `oncb` |

No API exposes the **audio** playhead in timestamped mode. `ph` is the **video** playhead, so an
audio-only slip shows up only indirectly, as `a_unacc` stepping.

## Enabling and configuring

The diagnostics are **on by default** in this build. Set them with environment variables in the
casparcg service environment:

| Variable | Default | Effect |
|---|---|---|
| `CASPAR_DECKLINK_DIAG` | on | `0` turns off the logger and the extra driver queries completely |
| `CASPAR_DECKLINK_DIAG_LOG` | `/var/log/casparcg/decklink-diag.log` | Log file, opened in append mode. The directory is created if needed. Keep it on **local disk**, not NFS: a hung filesystem can make consumer removal hang (the live callback is not affected) |
| `CASPAR_DECKLINK_DIAG_CSV` | off | `1` also writes one CSV line per callback to `<log>.csv` (header written once when the file is new) |

If the log file cannot be opened, or a write fails later (disk full, file removed), output falls back
to `CASPAR_LOG(info)` at about one line per second, with a single warning.

## Line format

Every line starts with `<UTC ISO-8601 ms> dl<device>/ch<channel> <type>`.

* `I`: info (START with format, cadence and buffer depth, LEGEND, PREROLL, STOP, SCHEDULED_PLAYBACK_HAS_STOPPED, ticksPerFrame warning).
* `S`: per-second summary.
* `E`: event header, followed by indented per-frame context lines. The context runs from 5 frames before
  the trigger to 4 frames after it, or 50 after for `BA_LOW` and `BA_ZERO`.

### Per-second summary (S)

```
2026-11-23T18:03:38.988Z dl1/ch1 S fr=134 n=50 res=ok:50,late:0,drop:0,flush:0,other:0 ba=2860..3828
  ba_post=3820..4788 bv=5..5 a_unacc=972..1940 av_skew=0..0 s_lead=4416..4416 ia=20.0..20.0ms pop_max=0.3ms
  sv_max=0.1ms sa_max=0.0ms cb_max=2.0ms ph=133000 spd=1.00 hw=134123/400/1000 a_sched=132480 v_sched=138000
  a_lead=4800 v_lead=5000 late_adj=0 ph_jump=0 cft_step=0 sched_acc=0 ns_cad=0 short=0 ahr_fail=0 vhr_fail=0
  oncb=25 ovf=0 sup=0
```
(This is one line in the file, wrapped here. The values are synthetic, from the test harness.)

`x..y` fields give the min..max over that second. The others are counts for the second or the value at
its last frame. `ovf` counts samples lost because the ring buffer was full. `sup` counts events
suppressed by the 20-events-per-second cap.

### Per-frame context line (inside E)

```
2026-11-23T18:03:39.508Z dl1/ch1 E fr=160 SCHED_ACCEPT_0_OF_960
  f=156 t=18:03:39.428Z r=ok cft=154000 cat=148800 cns=960 ba=2860/2860/3820/3820 bv=5 ph=155000 spd=1.00
  hw=156123/400/1000 hwhr=0x00000000 vt=160000 at=153600 a_lead=4800 v_lead=5000 a_unacc=1940 av_skew=0
  s_ph=155400 s_inf=400 s_lead=4416 ns=960 sw=960 ahr=0x00000000 vhr=0x00000000 shr=0x00000000 q=1 cpu=3
  ia=20000 pop=300 sv=50 sa=30 cb=2000
```

| Field | Meaning (time units: 50000/s, so 1 frame = 1000; audio in samples at 48 kHz) |
|---|---|
| `f`, `t`, `r` | Completion counter, wall time, result (`ok`, `late`, `drop`, `flush`). Also `LATE_ADJ` (caspar skipped one frame of timeline) and `ABORT` |
| `cft`, `cat`, `cns` | Display time, audio stream time and sample count that the **completed** frame was scheduled with |
| `ba=a/b/c/d` | Buffered audio at callback entry / right before `ScheduleAudioSamples` / right after it / after all scheduling joined |
| `bv` | Buffered video frames |
| `ph`, `spd`, `shr` | `GetScheduledStreamTime` (video playhead), speed, HRESULT |
| `hw=t/in/per`, `hwhr` | `GetHardwareReferenceClock` time / timeInFrame / ticksPerFrame (should be 1000), HRESULT. Only deltas of `t` matter |
| `vt`, `at` | Display time and audio stream time passed to `ScheduleVideoFrame` / `ScheduleAudioSamples` this callback |
| `a_lead`, `v_lead` | `audio_scheduled_ - ph_in_samples`, `video_scheduled_ - ph` |
| `a_unacc` | `a_lead - ba`: samples caspar believes the driver holds but the driver does not report. **Constant in normal operation; the key discriminator** |
| `av_skew` | `audio_scheduled_ - video_scheduled_` in samples. Constant at 50p |
| `s_ph`, `s_inf`, `s_lead` | ph and hw timeInFrame read right before `ScheduleAudioSamples`, and `at - s_ph_in_samples` (audio deadline margin) |
| `ns`, `sw`, `ahr`, `vhr` | Samples in the block, samplesWritten, audio and video schedule HRESULTs |
| `q`, `cpu`, `oncb` | Caspar frame queue size after pop, CPU the primary scheduling task ran on, whether that task ran on the callback thread |
| `ia`, `pop`, `sv`, `sa`, `cb` | Microseconds: inter-arrival, wait in pop, ScheduleVideoFrame, ScheduleAudioSamples, whole callback |

### Event triggers

`RESULT_*`, `INTER_ARRIVAL_<ms>ms` (>30 ms), `INTER_ARRIVAL_SHORT_<us>us` (<10 ms), `CFT_STEP_n`,
`PH_CFT_SHIFT_n`, `PLAYHEAD_STEP_<ph>_HW_<hw>`, `SCHED_ACCEPT_x_OF_y`, `SAMPLES_WRITTEN_x_OF_y`,
`AUDIO_HR_FAIL`, `VIDEO_HR_FAIL`, `NS_CADENCE_n`, `AV_SKEW_n`, `A_UNACC_STEP_n` (480 samples or more frame
to frame), `BA_LOW_n` (falls below 1920), `BA_ZERO`, `BA_MIN_STEP` (per-second ba minimum moved by 480 or
more; shows the two largest frame ba deltas and the largest a_unacc delta as `frame:delta`, and is
centred on the a_unacc frame when that delta is 480 or more), `NO_CALLBACKS_FOR_1S`, `CALLBACKS_RESUMED`.

### CSV (optional)

Columns: `wall_ms,consumer,frame_no,result,late_adj,cft,cat,cns,ph,hw,hw_in_frame,sched_ph,bv,ba,
ba_pre_sched,ba_post_sched,ba_post,a_sched,v_sched,at,vt,sched_in_frame,ns,sw,ahr,vhr,a_unacc,av_skew,q,cpu,
oncb,ia_us,pop_us,sv_us,sa_us,cb_us`. `wall_ms` is the Unix epoch in ms (UTC).

## Correlating with audio drops or resets

1. Note the wall time of each audible drop, reset or silence. Make sure caspar1 runs NTP; log times are UTC.
2. `grep -E ' E .*(BA_MIN_STEP|A_UNACC_STEP|BA_ZERO|BA_LOW)' decklink-diag.log`. Each one-frame step should
   show up as one of these.
3. Read the context lines around the step frame and see which other trigger fired on the same frame
   or next to it:
   * `CFT_STEP` or `INTER_ARRIVAL_SHORT` on the same frame: the driver skipped or doubled a completion.
   * `PLAYHEAD_STEP` with a ph step different from the hw delta: the video playhead jumped, and audio
     probably followed it.
   * `SCHED_ACCEPT_x_OF_960` with `sw=960`: the driver accepted the block and then discarded part of it.
     Look at `s_lead` and `s_inf` to see whether the block was close to the deadline.
   * `NS_CADENCE` or `AV_SKEW`: the mixer delivered a frame with the wrong amount of audio.
   * Only `A_UNACC_STEP` with ph, hw, cft and bv all regular: the slip happened inside the driver's audio path,
     with nothing visible at the API. That is evidence for a driver or firmware report to Blackmagic.
4. When `ba` reaches 0, the `BA_ZERO` event shows the 50 frames that follow it, so you can see whether
   later blocks are accepted (`ba_post_sched` grows) or dropped (it stays at 0).
5. For a full offline timeline, enable the CSV and plot `ba`, `a_unacc` and `ph - cft` against `wall_ms`.

## Overhead and safety

* Callback path: a few clock reads and five extra cheap driver queries per frame. These are
  `GetScheduledStreamTime` and `GetHardwareReferenceClock`, each read twice (at callback entry and again
  before audio is scheduled), plus `GetBufferedAudioSampleFrameCount` three more times. The callback also
  copies a ~350-byte struct into a preallocated lock-free ring. It takes no locks, allocates nothing and
  does no I/O. If the ring is full, the sample is counted in `ovf` and dropped.
* Scheduling behaviour is unchanged. The only functional difference is that `ScheduleAudioSamples` gets a
  real `samplesWritten` pointer. The destructor also clears the completion callback before
  `DisableVideoOutput`, which only happens at teardown.
* Logging runs in a separate thread at SCHED_OTHER, nice 10.
* Disk usage: about 35-50 MB/day for summaries and occasional events, and more during event storms (capped
  at 20 events/s). The CSV adds about 300-400 MB/day. There is no built-in rotation. If you use logrotate,
  use `copytruncate`. Watch free space on `/var/log` over multi-day runs.

## Build

Apply to a clean v2.5.0-stable tree (`git apply decklink-diag-v250.patch`) and build caspar1 the usual
way. No new dependencies: it uses boost::filesystem (already linked) and pthread/setpriority/sched_getcpu
on Linux. Verified in a sandbox (Ubuntu 24.04, gcc 13.3, -Werror, HTML module off): `make casparcg` builds
and links cleanly, and the diag block passes a standalone synthetic harness. Rebuild on caspar1 rather
than copying the sandbox binary.

## +fk3: 0004 (convert ahead) and 0005 (realtime callback)

`exp-step1/0004-decklink-exp-step1.patch` and `exp-step2/0005-decklink-exp-rt.patch`, applied on top of 0002 and 0003, in that order. Both are inert unless `CASPAR_DECKLINK_EXP` (comma
separated) or the first line of `/run/casparcg/decklink-exp` (wins when it exists) asks for them;
the file is read at each consumer ADD.

* `ahead`, `queue=N`: convert the frame in `send()` (consumer executor thread) and hand ready frames
  to the completion callback through an N-deep queue, so the callback only pops and schedules.
  Adds N frames of latency.
* `rt`, `rtprio=N`: SCHED_FIFO N (1..49) for the completion callback thread only. Needs
  RLIMIT_RTPRIO >= N for the casparcg user; every change or failure is logged as an `RT thread=cb`
  line (also in the diag log). Without `rt` a leftover FIFO is reset to SCHED_OTHER.

caspar1 runs `ahead,queue=2,rt,rtprio=2` (host_vars/caspar1.yml). Upstream commented out the same
realtime call in the callback in a July 2022 WIP commit (3ec1aab27, merged with #1433 in Dec 2022),
with no stated reason or regression; the documented starvation upstream was at SCHED_RR 99/49, not
FIFO 2. Bench numbers: tx3, 26 Nov 2026, late callbacks ~80/h -> 0-3/h, callback p99 13 -> ~1 ms.

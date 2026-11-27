# libswresample4 with FFmpeg be8a4f80b9 (Ubuntu noble 6.1.1)

## What

A rebuild of Ubuntu noble's `ffmpeg` 7:6.1.1-3ubuntu5 with one backported upstream
commit, as version **7:6.1.1-3ubuntu5+fk1**. Only `libswresample4` (and
`libswresample-dev`) are shipped.

The patch is `swr-avoid-reapplication-of-firstpts.patch`: FFmpeg commit
[be8a4f80b9](https://github.com/FFmpeg/FFmpeg/commit/be8a4f80b97222d99b4262c9230ca8a1db28973a),
"swr/swresample: avoid reapplication of firstpts" (Gyan Doshi, Dec 2023, fixes
[trac #4131](https://trac.ffmpeg.org/ticket/4131)). It first shipped in FFmpeg 7.0. It is a
three-line change in `swr_init()`.

## Why

CasparCG 2.5's ffmpeg producer resamples audio through
`aresample=async=1000:first_pts=…:min_comp=0.01`. With `first_pts` set, the first
time swresample needs a soft compensation, it re-runs `swr_init()`, which resets
the output pts to `first_pts`. It then inserts silence from there up to the real
position.

On air, 26 Nov 2026 at 11:17Z, video 628845 (VP9/Opus WebM) went silent on SDI and the
multicast at file position 1060.7 s, while the picture kept running. That is the
file's one 10 ms audio timestamp step (WebM ms rounding). CasparCG logged `Latency`,
and the layer time jumped back to 0. Left alone, the audio would have stayed silent
for about 1060 s.

## Evidence (tx3 bench, 26–27 Nov 2026)

- **Stock lib:** 3 of 3 plays from 0 lost audio at 1060.7 s (caspar1 on air, and twice
  on tx3). Plain `ffmpeg` 6.1.1 with CasparCG's chain reproduces it (output pts
  1060.71 → 0.0025). From a 950 s start with `-copyts` it also resets at 1060.72 s.
  FFmpeg 7.1.1 does not reset.
- **Patched lib** (bench image `fk-caspar-bench:fk2noavx2-swr`):
  - 628845 from 0 to 1140 s: no reset and no `Latency` at 1060.7 s, audio present.
  - With SEEK 950: no reset.
  - Sanity plays (AAC MP4, PCM DV, MP2 MPEG-PS): normal.

## Build

```sh
cp swr-avoid-reapplication-of-firstpts.patch build-swr.sh ~/Frikanalen/caspar-build/swr/
~/Frikanalen/caspar-build/swr/build-swr.sh   # docker, ubuntu:24.04, apt-get source ffmpeg=7:6.1.1-3ubuntu5
ls ~/Frikanalen/caspar-build/swr/out/        # libswresample4_6.1.1-3ubuntu5+fk1_amd64.deb, libswresample-dev_…
```

The build adds the patch to `debian/patches/series`, bumps the changelog to
+fk1, and runs `dpkg-buildpackage -B` with `nocheck nodoc`. It takes about 10 minutes
on 32 cores. CasparCG links `libswresample.so.4` dynamically, so the server deb
does not need a rebuild.

## Rollback

Unhold and reinstall the stock library (and empty `casparcg_patched_debs` in host_vars/caspar1.yml), then restart CasparCG so it reloads the library:

```sh
apt-mark unhold libswresample4
apt-get install --allow-downgrades libswresample4=7:6.1.1-3ubuntu5
```

Do the restart (`systemctl restart nodm`) in a cue window.

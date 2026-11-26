# 0006: DeckLink consumer experiments, step 3: zero-copy BGRA (CasparCG 2.5.0)

The patch is `0006-decklink-exp-zerocopy.patch`. Apply it with `patch -p1` on top of v2.5.0 + 0002 + 0003 + 0004 + 0005. It changes `src/modules/decklink/consumer/decklink_consumer.cpp` only, and compiles with -Werror. **For the bench (tx3) only.**

## Why

With 8-bit BGRA output (caspar1, and anything using the internal keyer), the "conversion" is only a memcpy. It copies 3.6 MB per frame at 720p out of the mixer's GL read-back buffer into a freshly allocated card buffer. It costs about 1.6–2 ms at idle and 13–20 ms when preempted under load. Zero-copy hands the driver the read-back buffer itself, so nothing on our side touches the pixels.

## Flag

`zerocopy` (or `zc`), combined with the 0004/0005 flags, for example `ahead,zerocopy,rt,rtprio=2`.

- **When it applies:** only where the copy would be a plain copy:
  - BGRA strategy;
  - progressive;
  - channel format equal to the output format;
  - no region or offset;
  - not key-only;
  - a full-size, 16-byte-aligned image.

  Any other frame takes the normal copy, and the first refusal logs `ZEROCOPY_FALLBACK reason=…`.
- **Lifetime:** the card frame keeps the frame's image array alive until the driver releases it, and the pooled GL buffer then goes back to the mixer. It keeps only the image array, not the whole frame, so the mixer's render texture is freed as usual.
- **It overrides `pool`.** `pool_hit`/`pool_miss` in the diag then count zero-copy frames and fallbacks.
- **EXP and STOP lines:** the EXP line shows `zerocopy=1 zc_static=ok|<reason>`. The STOP line adds `zc_ptr_changes`, the number of times the buffer pointer changed.

## What to check before reading timings (from review)

1. **Picture correct?** Watch the SDI output (or a capture) for black, torn or stale frames, and check `dmesg` for blackmagic or page-pinning errors. If the DeckLink library tries to DMA-pin the NVIDIA GL mapping (a VM_PFNMAP mmap), it may fail. In that case zero-copy needs SDK-allocated or pinned memory instead, and this experiment says "not like this".
2. **Real GL path?** A channel with no layers makes the mixer return a static zeroed buffer, which passes every check and flatters the numbers. Play real content (the sync clip plus the invisible page). `zc_ptr_changes` should be about the frame count, since the buffers rotate through the pool, not near 1.
3. **Warm-up:** holding read-back buffers longer grows the GL host pool by a few buffers. Each new buffer is created inline on the GL thread, which costs one mixer stall per buffer. Drop the first seconds after each ADD. Poll `INFO GL` about once a second: the host buffer count should rise by about buffer_depth+queue and then stay flat.
4. **Moved, or removed?** For an app-owned frame the DeckLink library may copy into its own buffers, inside `ScheduleVideoFrame` or on an internal thread. Compare `sv_us` (ScheduleVideoFrame time) and per-thread CPU (`pidstat -t`) across modes, not only `cv_us`.

## Bench arms (rtprio container, as in step 2)

| Arm | Flags | Isolates |
|---|---|---|
| H | `ahead,pool=heap,rt,rtprio=2` | the copy, without allocation (control) |
| Z | `ahead,zerocopy,rt,rtprio=2` | no copy |
| C2 | `ahead,rt,rtprio=2` | step 2's best (copy + allocation) |
| Zb | `base,zerocopy` | zero-copy without ahead/RT, inside the callback |

Use the same load as steps 1 and 2. For each arm report:
- `cv_us`, `sv_us`, callback p50/p99/max;
- stalls and gaps between callbacks;
- late/dropped frames;
- CPU per thread, including DeckLink library threads;
- host pool growth.

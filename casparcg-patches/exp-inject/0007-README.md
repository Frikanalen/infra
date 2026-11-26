# 0007: fault injection for testing the audio lead keeper (CasparCG 2.5.0, bench only)

The patch is `0007-decklink-exp-inject.patch`. Apply it with `patch -p1` on v2.5.0 + 0002 + 0003 + 0004 + 0005. Do not apply 0006 (zero-copy, dropped). It changes `decklink_consumer.cpp` only, and compiles with -Werror.

## Why

The keeper (0003) has made 0 corrections on caspar1 and on tx3, because real slips were rare there or got absorbed. To prove it repairs a slip, we cause slips on purpose.

## Flags (in `CASPAR_DECKLINK_EXP` / `/run/casparcg/decklink-exp`)

| Flag | Effect |
|---|---|
| `inject=drop:S` | Every S seconds, one audio block is not scheduled, but the audio timeline advances as if it were. The card ends up exactly one frame short of audio against video. This is the slip the keeper must repair. |
| `inject=stall:MS:S` | Every S seconds, one completion callback sleeps MS ms first. This reproduces the real trigger, and leaves it to the driver whether lead is lost. caspar1's slips came with 31–54 ms callback gaps. |

- **Timing:** the first injection comes S seconds after the consumer starts. Use S ≥ 10, so the keeper's 1 s baseline is taken first.
- **Logging:** each injection writes `INJECT kind=drop|stall … n=…` to the diag log, and the EXP line shows `inject=…`.
- **Combining:** `nokeeper` turns the keeper off for the control arm.

## Bench arms (tx3, no rtprio, sync clip playing, no extra load)

| Arm | Flags | Expect |
|---|---|---|
| K-drop | `inject=drop:60` | Lead drops by 960 at each INJECT, then one AUDIO_LEAD_CORRECTED about 0.5 s later. Lead back to about 3820, and never a staircase. |
| N-drop | `nokeeper,inject=drop:60` | A staircase: about 3820, 2860, 1900, 940, then partial accepts and a mute after about 4 minutes. That's caspar1's failure. |
| K-stall | `inject=stall:45:60` | Shows whether a 45 ms callback alone costs a frame on this driver. If it does, the keeper corrects it. |
| N-stall | `nokeeper,inject=stall:45:60` | Control for K-stall. |
| KA-drop | `ahead,inject=drop:60` | The keeper together with ahead. |

20 minutes per arm.

**Measure:**
- buffered audio (ba) at each INJECT and 1–2 s later;
- the time from INJECT to AUDIO_LEAD_CORRECTED;
- SCHED_ACCEPT partial accepts and BA_ZERO;
- the A/V offset and audible gaps, if an SDI loop capture (Duo 2 SDI 2 → SDI 3) is available.

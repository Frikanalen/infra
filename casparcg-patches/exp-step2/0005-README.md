# 0005: DeckLink consumer experiments, step 2: realtime priority (CasparCG 2.5.0)

The patch is `0005-decklink-exp-rt.patch`. Apply it with `patch -p1` on top of v2.5.0 + 0002 (diag) + 0003 (keeper) + 0004 (exp-step1). It changes `src/modules/decklink/consumer/decklink_consumer.cpp` only, and compiles with -Werror. **For the bench (tx3) only, not for air.**

## Why

On tx3, `ahead` cut the callback to 0.6 ms (p50), but gaps of over 30 ms between callbacks remained, at about 85 seconds per hour. That is the driver's callback thread not being scheduled in time on a busy CPU, so realtime priority is the remaining lever. With `ahead` the callback is now short enough to run as SCHED_FIFO safely.

## Flags (in `CASPAR_DECKLINK_EXP` or `/run/casparcg/decklink-exp`, combined with the 0004 flags)

| Flag | Effect |
|---|---|
| `rt` or `rt=cb` | SCHED_FIFO for the driver's completion callback thread. |
| `rt=conv` | SCHED_FIFO for the consumer executor thread (`send()`, and the conversion with `ahead`). |
| `rt=all` | Both. |
| `rtprio=N` | Priority 1..49, default 10. Upstream's channel thread uses 2. Values of 50 and above are rejected, because they would compete with threaded IRQ handlers, including the DeckLink driver's. |

- **Without `rt` flags,** a thread still left SCHED_FIFO by an earlier consumer is put back to SCHED_OTHER. The driver may reuse its callback thread across REMOVE/ADD.
- **The policy is checked again** whenever the driver delivers on a different thread.
- **Every change or failure is logged** as a diag line:
  `RT thread=cb tid=… want=FIFO:10 before=OTHER:0 result=ok rtlimit=99 thread_changes=0`
- **The EXP line gains** `rt= rtprio= rtlimit= chan_policy=`. `chan_policy` is the policy of the `channel-N` thread.
- **Not covered:** TBB worker threads (v210 chunks, secondary ports) stay SCHED_OTHER.

## Prerequisite: allowing RT at all

Without `RLIMIT_RTPRIO` (or CAP_SYS_NICE), the RT line says `result=failed:EPERM`. That is the state on caspar1 today, which is why upstream's own `set_thread_realtime_priority()` calls silently do nothing there. In docker, start the container with `--ulimit rtprio=49`. On cgroup v1 kernels with RT group scheduling, also pass `--cpu-rt-runtime=<us>`.

**Caveat:** once rtprio is allowed, upstream `video_channel.cpp` puts the `channel-N` (mixer) thread on FIFO:2 in *every* arm, including runs without `rt` flags. Granting rtprio is therefore an experiment of its own, and the arms below separate the two effects.

## Bench arms (restart the container between A and the rest)

| Arm | Container | Flags | Compare with |
|---|---|---|---|
| A | no rtprio | `ahead` | step 1 (repeat of the baseline) |
| B | `--ulimit rtprio=49` | `ahead` | A: the upstream channel thread on FIFO:2 alone |
| C | rtprio | `ahead,rt` | B: the callback on FIFO:10 |
| C2 | rtprio | `ahead,rt,rtprio=2` | C: no preemption between the callback and the channel thread |
| D | rtprio | `ahead,rt=all` | C: the conversion thread on FIFO too |
| E | rtprio | `base,rt` | B with `base`: RT without `ahead` (the long callback on FIFO) |

**Before using a run, check:**
- the RT lines say `result=ok`;
- `chan_policy` is `OTHER:0` in A and `FIFO:2` in B–E;
- every sample's `cb_tid` matches the RT line's tid (`thread_changes=0`).

**Record with each run:**
- the container's CPU count and cpuset;
- `/proc/sys/kernel/sched_rt_runtime_us`;
- whether the kernel uses threadirqs (`ps -eLo cls,rtprio,comm | grep irq`);
- per-thread context switches (`pidstat -t -w 1`).

**Report:**
- `swait_us` and the ready-queue age alongside callback timings, so producer starvation can be told apart from callback wake-up latency;
- with `rt=cb`, watch for tails caused by `buffer_mutex_` (not priority-inheriting) under a full CPU.

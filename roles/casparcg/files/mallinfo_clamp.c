/*
 * mallinfo() that saturates instead of wrapping.
 *
 * CasparCG 2.5's embedded Chromium (CEF 142) reports the process heap with
 * glibc's old mallinfo(), whose fields are 32-bit ints, then checked_cast()s
 * arena + hblkhd and uordblks to size_t. Past 2 GiB of heap the int wraps
 * negative, the cast fails and Chromium traps (ud2, "Illegal instruction")
 * on its MemoryInfra thread, taking the whole server down. That is the crash
 * caspar1 had ten times from 2026-11-01 to 11-14 (libcef.so+0x77270aa,
 * MallocDumpProvider::OnMemoryDump).
 *
 * Preloaded into casparcg-server (LD_PRELOAD), this answers mallinfo() from
 * mallinfo2(), whose fields are size_t, clamping each to INT_MAX, and keeps
 * arena + hblkhd from overflowing too. Nothing in CasparCG needs the exact
 * figures; Chromium only uses them for memory metrics.
 */
#define _GNU_SOURCE
#include <limits.h>
#include <malloc.h>

static int clamp(size_t value, size_t limit)
{
    return value > limit ? (int)limit : (int)value;
}

struct mallinfo mallinfo(void)
{
    struct mallinfo2 m = mallinfo2();
    struct mallinfo out;

    /* Chromium adds these two, so share INT_MAX between them. */
    out.arena    = clamp(m.arena, INT_MAX / 2);
    out.hblkhd   = clamp(m.hblkhd, INT_MAX / 2);
    out.ordblks  = clamp(m.ordblks, INT_MAX);
    out.smblks   = clamp(m.smblks, INT_MAX);
    out.hblks    = clamp(m.hblks, INT_MAX);
    out.usmblks  = clamp(m.usmblks, INT_MAX);
    out.fsmblks  = clamp(m.fsmblks, INT_MAX);
    out.uordblks = clamp(m.uordblks, INT_MAX);
    out.fordblks = clamp(m.fordblks, INT_MAX);
    out.keepcost = clamp(m.keepcost, INT_MAX);
    return out;
}

#!/usr/bin/env python3
"""Does an igb_main.c have the "strip the Rx timestamp header only on the
first buffer" fix, i.e. is the TSIP check in igb_clean_rx_irq() guarded by
!skb? Checks the code, not a commit id, so stable backports and reworded
versions of the fix count too.

As a script: igb_fix_present.py <igb_main.c | -> ; exit 0 fixed, 1 not
fixed, 2 unknown (code moved or refactored: needs a human).
"""
import re
import sys


def verdict(src):
    f = re.search(r"static int igb_clean_rx_irq\([^;)]*\)\s*\{.*?\n}\n", src, re.S)
    if not f:
        return "unknown: igb_clean_rx_irq not found"
    m = re.search(r"if\s*\(([^{;]*?)igb_test_staterr\(\s*rx_desc\s*,"
                  r"\s*E1000_RXDADV_STAT_TSIP\s*\)", f.group(0))
    if not m:
        return "unknown: TSIP check not found"
    return "fixed" if re.search(r"!\s*skb\s*&&", m.group(1)) else "not fixed"


if __name__ == "__main__":
    src = sys.stdin.read() if sys.argv[1] == "-" else open(sys.argv[1]).read()
    v = verdict(src)
    print(v)
    sys.exit({"fixed": 0, "not fixed": 1}.get(v, 2))

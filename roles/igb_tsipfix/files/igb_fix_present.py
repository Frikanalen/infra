#!/usr/bin/env python3
"""Exit 0 if the given igb_main.c already has the "strip the Rx timestamp
header only on the first buffer" fix (TSIP check guarded by !skb), 1 if not,
2 if the code can't be found at all (refactored: needs a human)."""
import re, sys
src = open(sys.argv[1]).read() if sys.argv[1] != "-" else sys.stdin.read()
f = re.search(r"static int igb_clean_rx_irq\([^;)]*\)\s*\{.*?\n}\n", src, re.S)
if not f:
    print("unknown: igb_clean_rx_irq not found"); sys.exit(2)
src = f.group(0)
m = re.search(r"if\s*\(([^{;]*?)igb_test_staterr\(\s*rx_desc\s*,\s*E1000_RXDADV_STAT_TSIP\s*\)", src)
if not m:
    print("unknown: TSIP check not found"); sys.exit(2)
cond = m.group(1)
if re.search(r"!\s*skb\s*&&", cond):
    print("fixed"); sys.exit(0)
print("not fixed"); sys.exit(1)

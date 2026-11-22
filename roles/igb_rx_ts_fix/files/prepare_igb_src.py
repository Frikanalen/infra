#!/usr/bin/env python3
"""Stage a patched igb source tree for one stable kernel version.

usage: prepare_igb_src.py <stable-version> <dest-dir> <patch-dir> <key-patch>

Downloads drivers/net/ethernet/intel/igb at tag v<stable-version> from
github.com/gregkh/linux (the stable tree) into <dest-dir>, then for each
patch in <patch-dir> (sorted by name):
  - already in the source (applies in reverse): note it, skip it
  - applies cleanly: apply it
  - neither: skip it, unless it is <key-patch>, in which case fail

Writes <dest-dir>/fk-status.json last, so its presence means "done":
  {"stable": ..., "applied": [...], "already_upstream": [...],
   "skipped": [...], "intree_fixed": bool}
intree_fixed is true when the stock source already has the fix (checked in
the code by igb_fix_present.py, so backports count), i.e. the kernel's own
igb is fixed and no DKMS module is needed.
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from igb_fix_present import verdict  # noqa: E402

REPO = "gregkh/linux"
PATH = "drivers/net/ethernet/intel/igb"
RAW = "https://raw.githubusercontent.com"
UA = {"User-Agent": "ansible-igb-rx-ts-fix"}
# Headers the driver includes. Sources come from the Makefile's igb-y list.
HEADERS = ["e1000_82575.h", "e1000_defines.h", "e1000_hw.h", "e1000_i210.h",
           "e1000_mac.h", "e1000_mbx.h", "e1000_nvm.h", "e1000_phy.h",
           "e1000_regs.h", "igb.h"]


def fetch(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                timeout=30) as r:
        return r.read()


def patch(args, src, pfile):
    with open(pfile, "rb") as f:
        return subprocess.run(["patch", "-p6", "-s", "-f", "--no-backup-if-mismatch", *args],
                              cwd=src, stdin=f,
                              capture_output=True).returncode == 0


def main(stable, dest, patchdir, key):
    base = f"{RAW}/{REPO}/v{stable}/{PATH}"
    makefile = fetch(f"{base}/Makefile").decode()
    # igb-y := a.o b.o \ ... (continuation lines), plus any igb-$(CONFIG_..)
    joined = re.sub(r"\\\n", " ", makefile)
    objs = re.findall(r"(\w+)\.o", " ".join(
        line.split("=", 1)[1] for line in joined.splitlines()
        if re.match(r"\s*igb-\S*\s*[:+]?=", line)))
    os.makedirs(dest, exist_ok=True)
    with open(os.path.join(dest, "Makefile"), "w") as f:
        f.write(makefile)
    for name in [o + ".c" for o in objs] + HEADERS:
        try:
            data = fetch(f"{base}/{name}")
        except urllib.error.HTTPError as e:
            if e.code == 404 and name.endswith(".h"):
                continue
            raise
        with open(os.path.join(dest, name), "wb") as f:
            f.write(data)

    with open(os.path.join(dest, "igb_main.c")) as f:
        stock = verdict(f.read())
    status = {"stable": stable, "stock": stock, "applied": [],
              "already_upstream": [], "skipped": [],
              "intree_fixed": stock == "fixed"}
    if stock.startswith("unknown"):
        sys.exit(f"v{stable}: {stock}; the code has moved, look at the patch by hand")
    for name in sorted(os.listdir(patchdir)):
        p = os.path.join(patchdir, name)
        if patch(["-R", "--dry-run"], dest, p):
            status["already_upstream"].append(name)
            if name == key:
                status["intree_fixed"] = True
        elif patch(["--dry-run"], dest, p):
            patch([], dest, p)
            status["applied"].append(name)
        elif name == key:
            sys.exit(f"{key} neither applies nor is already in v{stable}")
        else:
            status["skipped"].append(name)

    with open(os.path.join(dest, "fk-status.json"), "w") as f:
        json.dump(status, f)
    print(json.dumps(status))


if __name__ == "__main__":
    main(*sys.argv[1:5])

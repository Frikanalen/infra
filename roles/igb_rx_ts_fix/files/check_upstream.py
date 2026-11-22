#!/usr/bin/env python3
"""Is the igb Rx-timestamp fix in mainline yet? Prints one JSON object.

Run on the Ansible controller by roles/igb_rx_ts_fix. Reads igb_main.c from
torvalds/linux master and checks the code itself (igb_fix_present.py). If it
is fixed, it also asks the GitHub search API for the commit, best effort.
Never fails the play: on any error it prints {"error": ...}.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from igb_fix_present import verdict  # noqa: E402

UA = {"User-Agent": "ansible-igb-rx-ts-fix"}
MAIN = ("https://raw.githubusercontent.com/torvalds/linux/master/"
        "drivers/net/ethernet/intel/igb/igb_main.c")


def fetch(url, accept=None):
    h = dict(UA, **({"Accept": accept} if accept else {}))
    with urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                timeout=30) as r:
        return r.read().decode()


def main(subject):
    out = {"mainline": verdict(fetch(MAIN))}
    if out["mainline"] == "fixed":
        try:
            q = urllib.parse.quote(f'repo:torvalds/linux "{subject}"')
            hits = json.loads(fetch(
                f"https://api.github.com/search/commits?q={q}",
                "application/vnd.github+json"))
            if hits.get("items"):
                out["url"] = hits["items"][0]["html_url"]
        except Exception:  # noqa: BLE001 - the URL is a nicety
            pass
    m = out["mainline"]
    if m == "fixed":
        out["message"] = ("FIX IS IN MAINLINE " + out.get("url", "") +
                          ". Once every host reports intree-fixed, delete"
                          " roles/igb_rx_ts_fix.").replace("  ", " ")
    elif m == "not fixed":
        out["message"] = f'Not in mainline yet ("{subject}").'
    else:
        out["message"] = (f"Mainline igb_clean_rx_irq() changed shape ({m});"
                          " check by hand whether the fix landed.")
    return out


if __name__ == "__main__":
    try:
        print(json.dumps(main(sys.argv[1])))
    except Exception as e:  # noqa: BLE001 - report, never break the play
        print(json.dumps({"error": f"Could not check mainline: {type(e).__name__}: {e}"}))

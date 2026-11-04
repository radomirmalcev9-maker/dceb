#!/usr/bin/env python3
"""Score Capri against evals/scenarios.py.

    .venv/bin/python evals/run_eval.py              # deterministic offline pass
    .venv/bin/python evals/run_eval.py --live       # also query capricorn
    .venv/bin/python evals/run_eval.py --verbose    # show every answer

The offline pass scores the deterministic analyzer, so it is stable in CI and
needs no model session. The live pass shows how much the real model adds.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_bridge import OfflineAnalyzer, bridge          # noqa: E402
from scenarios import SCENARIOS                        # noqa: E402


def check(resp, scenario) -> tuple:
    """Return (action_ok, content_ok, detail)."""
    action_ok = resp.action.value == scenario["expect"]
    needle = scenario.get("must_contain")
    blob = resp.explanation + str(resp.params)
    content_ok = True if not needle else (needle.lower() in blob.lower())
    detail = f"{resp.action.value} | {resp.explanation[:70]}"
    return action_ok, content_ok, detail


def run(offline: bool = True, live: bool = False, verbose: bool = False) -> int:
    rows = []
    for sc in SCENARIOS:
        row = {"name": sc["name"], "mode": sc["mode"]}
        if offline:
            resp = OfflineAnalyzer.analyze(sc["prompt"], sc["mode"])
            a, c, detail = check(resp, sc)
            row["offline_action"] = a
            row["offline_content"] = c
            row["detail"] = detail
        if live:
            if not bridge.available:
                row["live_action"] = None
            else:
                try:
                    lresp = bridge.ask(sc["prompt"], mode=sc["mode"])
                    la, lc, ldetail = check(lresp, sc)
                    row["live_action"] = la
                    row["live_content"] = lc
                    row["detail"] = ldetail
                except Exception as exc:                     # noqa: BLE001
                    row["live_action"] = False
                    row["live_content"] = False
                    row["detail"] = f"live error: {exc}"
        rows.append(row)

    width = max(len(r["name"]) for r in rows)
    print(f"{'scenario'.ljust(width)}  {'mode'.ljust(12)}  offline  live")
    print("-" * (width + 30))
    for r in rows:
        off = ("ok" if (r.get("offline_action") and r.get("offline_content", True))
               else "FAIL")
        if "live_action" in r:
            live = "skip" if r["live_action"] is None else (
                "ok" if (r["live_action"] and r.get("live_content", True)) else "FAIL")
        else:
            live = "-"
        print(f"{r['name'].ljust(width)}  {r['mode'].ljust(12)}  {off.ljust(7)}  {live}")
        if verbose or off == "FAIL":
            print(f"{' ' * (width + 2)}expected={''}  got: {r['detail']}")

    off_ok = sum(1 for r in rows
                 if r.get("offline_action") and r.get("offline_content", True))
    total = len(rows)
    print("-" * (width + 30))
    print(f"offline: {off_ok}/{total} ({off_ok / total:.0%})")
    if live and any("live_action" in r for r in rows):
        live_rows = [r for r in rows if r.get("live_action") is not None]
        if live_rows:
            live_ok = sum(1 for r in live_rows
                          if r["live_action"] and r.get("live_content", True))
            print(f"live:    {live_ok}/{len(live_rows)} "
                  f"({live_ok / len(live_rows):.0%})"
                  if live_rows else "live: skipped (no session)")
    return 0 if off_ok == total else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="also query the capricorn model")
    ap.add_argument("--verbose", action="store_true", help="print every answer")
    args = ap.parse_args()
    return run(offline=True, live=args.live, verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())

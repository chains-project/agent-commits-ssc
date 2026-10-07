"""
[IN]: latest summary_*.csv in scripts/pilot/24h/ and scripts/pilot/alltime/
[OUT]: stdout diagnostic table + scripts/pilot/comparison.csv
[POS]: Joins the two pilot windows so we can label each channel as
       alive / outdated / dead / weak / cap-bound for the supervisor meeting.
[SYNC]: When CHANNELS in collect-commits.py changes, schemas auto-track via join keys.

Decision logic for the "diagnosis" column:
    alltime_err or 24h_err          -> error               (need to re-run, not a finding)
    alltime_total == 0              -> wrong_identifier    (channel never returned anything)
    alltime_total > 0, 24h == 0     -> outdated_identifier (was used historically, no longer)
    alltime_total > 0, 24h > 0      -> alive               (currently active)
Additional flags:
    alltime_total > 1000            -> +cap_bound (Search API caps at 1000 results per query)
"""

import csv
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
PILOT_24H = SCRIPTS_ROOT / "pilot" / "24h"
PILOT_ALL = SCRIPTS_ROOT / "pilot" / "alltime"


def latest_summary(d):
    """Return the most recent summary_*.csv under directory d."""
    files = sorted(d.glob("summary_*.csv"))
    if not files:
        raise FileNotFoundError(f"no summary_*.csv in {d}")
    return files[-1]


def load_summary(path):
    """Index rows by (agent, channel) for join."""
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return {(r["agent"], r["channel"]): r for r in rows}


def diagnose(r_24, r_all):
    """Classify a (channel, mode, query) by comparing 24h and alltime measurements."""
    err_24 = (r_24.get("error") or "") if r_24 else ""
    err_all = (r_all.get("error") or "") if r_all else ""
    if err_24 or err_all:
        return "error"

    # window_total_count: -1 = error, 0 = genuine no hits, >0 = hits
    tot_24 = int(r_24["window_total_count"]) if r_24 else -1
    tot_all = int(r_all["window_total_count"]) if r_all else -1
    if tot_all < 0 or tot_24 < 0:
        return "error"

    if tot_all == 0:
        return "wrong_identifier"
    if tot_24 == 0:
        return "outdated_identifier"
    return "alive"


def cap_flag(tot_all):
    return "+cap_bound" if tot_all > 1000 else ""


def main():
    s24 = load_summary(latest_summary(PILOT_24H))
    sall = load_summary(latest_summary(PILOT_ALL))

    # Join on (agent, channel). The two runs share the same CHANNELS config so
    # the keysets should match; we still union to surface any drift.
    keys = sorted(set(s24.keys()) | set(sall.keys()))

    out_rows = []
    for k in keys:
        r24 = s24.get(k)
        rall = sall.get(k)
        agent, channel = k
        # Pull canonical fields from whichever side exists.
        ref = rall or r24
        mode = ref.get("mode", "") if ref else ""
        qtype = ref.get("query_type", "") if ref else ""
        qval = ref.get("query_value", "") if ref else ""

        tot_24 = int(r24["window_total_count"]) if r24 else -1
        tot_all = int(rall["window_total_count"]) if rall else -1
        diag = diagnose(r24, rall)
        flag = cap_flag(tot_all)

        out_rows.append({
            "agent": agent,
            "channel": channel,
            "mode": mode,
            "query_type": qtype,
            "query_value": qval,
            "alltime_total": tot_all,
            "h24_total": tot_24,
            "diagnosis": diag + flag,
        })

    # Sort: alive first, then outdated, then wrong, then errors; within group by agent.
    order = {"alive": 0, "outdated_identifier": 1, "wrong_identifier": 2, "error": 3}
    out_rows.sort(key=lambda r: (order.get(r["diagnosis"].split("+")[0], 9), r["agent"], r["channel"]))

    out_csv = SCRIPTS_ROOT / "pilot" / "comparison.csv"
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)
    print(f"wrote {out_csv}\n")

    # Pretty-print diagnostic table.
    print("=" * 125)
    print(f"{'agent':<8} {'channel':<22} {'mode':<9} {'alltime':>10} {'24h':>8}  diagnosis")
    print("-" * 125)
    for r in out_rows:
        tot_all = "ERR" if r["alltime_total"] < 0 else str(r["alltime_total"])
        tot_24 = "ERR" if r["h24_total"] < 0 else str(r["h24_total"])
        print(
            f"{r['agent']:<8} {r['channel']:<22} {r['mode']:<9} "
            f"{tot_all:>10} {tot_24:>8}  {r['diagnosis']}"
        )
    print("=" * 125)

    # Group counts.
    from collections import Counter
    cats = Counter(r["diagnosis"].split("+")[0] for r in out_rows)
    print()
    for k, v in cats.most_common():
        print(f"  {k:<22} {v}")


if __name__ == "__main__":
    main()

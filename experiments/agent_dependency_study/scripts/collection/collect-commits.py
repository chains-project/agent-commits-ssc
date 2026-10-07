"""
[IN]: GITHUB_TOKEN (from .env), CHANNELS config from audit, last-24h window
[OUT]: scripts/pilot/<window>/<agent>_<UTC-timestamp>.csv with channel-tagged commit metadata; summary_<timestamp>.csv with per-channel totals
[POS]: RQ1 pilot data collector. Multi-channel identity per agent based on exploratory-commit-identity-audit.md.
[SYNC]: When changing CHANNELS, update scripts/CLAUDE.md and reference the audit doc.

PURPOSE
-------
For RQ1 of the thesis, we need to characterize AI-coding-agent commits on GitHub.
A naive single-email query (as in Monperrus's original collect-data.sh) undercounts
or misses agents whose identity is split across multiple channels:
  - GitHub App "bot" accounts with noreply emails like "<id>+<login>@users.noreply.github.com"
  - Custom CLI author emails (e.g., "codex@openai.com")
  - User-account logins
  - Co-author trailers in commit message text

This script queries every validated channel separately and tags each fetched commit
with the channel that found it, so we can later (a) dedupe across channels and
(b) report author-mode vs co-author-mode footprints separately.

Reference: docs/exploratory-commit-identity-audit.md in ai-agent-commit-data-quality.
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# -----------------------------------------------------------------------------
# Paths and constants
# -----------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
ENV_FILE = SCRIPTS_ROOT / ".env"           # holds GITHUB_TOKEN=...
PILOT_ROOT = SCRIPTS_ROOT / "pilot"        # all CSV output goes under here
PILOT_ROOT.mkdir(exist_ok=True)

PER_PAGE = 100                            # GitHub Search API max page size
SEARCH_CAP_PAGES = 10                     # Search API hard cap = 1000 results total
API_BASE = "https://api.github.com/search/commits"

# Search API is rate-limited to 30 requests/minute on top of the regular core
# limit; secondary rate limits hit even sooner if requests burst. These sleeps
# stay safely under both. Tweak with --sleep-between-channels if needed.
DEFAULT_PAGE_SLEEP = 2.5
DEFAULT_CHANNEL_SLEEP = 5.0
RATE_LIMIT_BACKOFF = 60.0                 # seconds to wait after a 403 before retrying

# All-time lower bound matches Monperrus's collect-data.sh convention: 2020-01-01.
# Using a pinned date rather than no filter avoids the bogus future-dated commits
# (e.g., 2037, 2089) that GitHub's index returns when no upper bound is enforced.
ALLTIME_SINCE = datetime(2020, 1, 1, tzinfo=timezone.utc)

# -----------------------------------------------------------------------------
# Channel configuration
# -----------------------------------------------------------------------------
# Each tuple = (agent, channel_id, mode, query_type, value, audit_lifetime_total).
#
#   agent        - one of claude/codex/copilot/cursor (the 4 study agents)
#   channel_id   - short label for this identity channel (used in CSV "channel" column)
#   mode         - "author" : the agent appears in the Git author field directly
#                  "coauthor": the agent appears only in commit message text
#                              (Co-authored-by trailer or other reference)
#   query_type   - "author-email": GitHub qualifier `author-email:<value>`
#                  "author"      : GitHub qualifier `author:<value>` (user/app slug)
#                  "fulltext"    : quoted full-text match `"<value>"`
#   value        - the literal identifier/string to search
#   audit_total  - lifetime count from the March-2026 audit, for reference only
#                  (not used at query time; printed in the summary CSV)
#
# Rationale per agent is detailed in exploratory-commit-identity-audit.md.
CHANNELS = [
    # ---- Claude: CLI identity + GitHub App bot + message-text references ----
    ("claude", "cli_email",        "author",   "author-email", "noreply@anthropic.com",                                 3_300_000),
    ("claude", "app_bot_email",    "author",   "author-email", "242468646+Claude@users.noreply.github.com",                13_355),
    ("claude", "app_bot_slug",     "author",   "author",       "anthropic-code-agent[bot]",                                13_355),
    ("claude", "cli_user",         "author",   "author",       "Claude",                                                3_300_000),
    ("claude", "msg_cli_email",    "coauthor", "fulltext",     "noreply@anthropic.com",                                11_600_000),

    # ---- Codex: CLI + new bot + legacy connector + user account + trailers ----
    ("codex",  "cli_email",        "author",   "author-email", "codex@openai.com",                                         11_200),
    ("codex",  "new_bot_email",    "author",   "author-email", "242516109+Codex@users.noreply.github.com",                  3_960),
    ("codex",  "legacy_connector", "author",   "author",       "chatgpt-codex-connector[bot]",                                529),
    ("codex",  "user_login",       "author",   "author",       "codex",                                                     6_900),
    ("codex",  "msg_noreply",      "coauthor", "fulltext",     "noreply@openai.com",                                       17_900),
    ("codex",  "msg_coauthored",   "coauthor", "fulltext",     "Co-authored-by: Codex",                                    39_600),

    # ---- Copilot: SWE Agent bot is the only validated author channel ----
    ("copilot","swe_agent_bot",    "author",   "author-email", "198982749+Copilot@users.noreply.github.com",            2_887_035),
    ("copilot","msg_noreply",      "coauthor", "fulltext",     "copilot@users.noreply.github.com",                        950_000),
    ("copilot","msg_bot_slug",     "coauthor", "fulltext",     "copilot-swe-agent[bot]",                                  204_000),

    # ---- Cursor: cursoragent@cursor.com is the real one (NOT cursor@anysphere.io) ----
    ("cursor", "cursoragent_email","author",   "author-email", "cursoragent@cursor.com",                                  400_000),
    ("cursor", "bg_agent_email",   "author",   "author-email", "agent@cursor.com",                                            198),
    ("cursor", "bot_noreply",      "author",   "author-email", "206951365+cursor[bot]@users.noreply.github.com",           2_700),
    ("cursor", "msg_cursoragent",  "coauthor", "fulltext",     "cursoragent@cursor.com",                                  766_000),
]


# -----------------------------------------------------------------------------
# Token loading: prefer scripts/.env, fall back to environment variable
# -----------------------------------------------------------------------------
def load_token():
    """Read GITHUB_TOKEN from scripts/.env or from the process environment."""
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == "GITHUB_TOKEN":
                    # Strip optional surrounding quotes
                    return v.strip().strip('"').strip("'")
    return os.environ.get("GITHUB_TOKEN")


# -----------------------------------------------------------------------------
# Query construction
# -----------------------------------------------------------------------------
def build_query(query_type, value, since_iso, until_iso):
    """
    Build the GitHub Search Commits 'q' parameter string for the given channel.

    All channels are scoped to a [since, until] author-date range so the request
    only returns commits whose original author date falls inside the window.
    """
    date_clause = f"author-date:{since_iso}..{until_iso}"
    if query_type == "author-email":
        # Exact match on the Git commit author email field
        return f"author-email:{value} {date_clause}"
    if query_type == "author":
        # Match by user login or GitHub App slug (incl. "<slug>[bot]")
        return f"author:{value} {date_clause}"
    if query_type == "fulltext":
        # Quoted full-text match across the commit message body
        return f'"{value}" {date_clause}'
    raise ValueError(f"unknown query_type: {query_type}")


# -----------------------------------------------------------------------------
# Single-page fetch
# -----------------------------------------------------------------------------
def fetch_page(token, query, page):
    """Call /search/commits for one page. Returns the parsed JSON dict.

    Raises urllib.error.HTTPError on non-2xx (caller handles 403 specially).
    """
    params = {
        "q": query,
        "per_page": str(PER_PAGE),
        "page": str(page),
        "sort": "author-date",
        "order": "desc",
    }
    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        # 'cloak-preview' is the historical media type required for commit search;
        # still honored even though commit search is now generally available.
        "Accept": "application/vnd.github.cloak-preview+json",
        "Authorization": f"token {token}",
        "User-Agent": "agent-commits-pilot",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_page_with_retry(token, query, page):
    """Call fetch_page; on 403 (rate-limit), wait and retry once."""
    try:
        return fetch_page(token, query, page)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            # Secondary rate limit hit. Back off and try once more.
            print(f"   403 rate-limited; sleeping {RATE_LIMIT_BACKOFF:.0f}s then retrying", file=sys.stderr)
            time.sleep(RATE_LIMIT_BACKOFF)
            return fetch_page(token, query, page)
        raise


# -----------------------------------------------------------------------------
# Per-channel collection: paginate up to 1000 results, dedupe by SHA within the channel
# -----------------------------------------------------------------------------
def collect_one(token, agent, channel_id, mode, query_type, value, since_iso, until_iso, max_pages, page_sleep):
    """
    Paginate through one (agent, channel) query and return:
      total      - the API-reported total_count for the window, or None on error
      rows       - list of normalized row dicts (one per unique commit found)
      incomplete - True if GitHub reported incomplete_results for this query
      error      - None on success, or a short error string for the summary

    max_pages bounds how many pages we actually fetch. The Search API itself
    caps at SEARCH_CAP_PAGES (10), so max_pages should be <= 10.
    """
    seen_shas = set()
    rows = []
    total = None
    incomplete = False
    error = None

    for page in range(1, max_pages + 1):
        q = build_query(query_type, value, since_iso, until_iso)
        try:
            data = fetch_page_with_retry(token, q, page)
        except Exception as e:
            # Network or HTTP error: log, record it, stop paginating this channel
            err_str = f"{type(e).__name__}: {e}"
            print(f"  [{agent}/{channel_id}] page {page} error: {err_str}", file=sys.stderr)
            error = err_str
            break

        # Capture total_count and incomplete flag only on the first page
        if total is None:
            total = data.get("total_count", 0)
            incomplete = bool(data.get("incomplete_results"))

        items = data.get("items", [])
        if not items:
            break

        # Normalize each commit record into our flat CSV schema
        for it in items:
            sha = it.get("sha")
            if not sha or sha in seen_shas:
                continue  # dedupe within this channel
            seen_shas.add(sha)
            commit = it.get("commit", {}) or {}
            author = commit.get("author", {}) or {}
            committer = commit.get("committer", {}) or {}
            repo = (it.get("repository") or {}).get("full_name", "")
            message_full = commit.get("message") or ""
            # Keep only the first line of the message; the full body is often huge
            message_first = message_full.splitlines()[0] if message_full else ""
            rows.append({
                "agent": agent,
                "channel": channel_id,
                "mode": mode,
                "query_type": query_type,
                "query_value": value,
                "sha": sha,
                "repo": repo,
                "author_name": author.get("name", ""),
                "author_email": author.get("email", ""),
                "author_date": author.get("date", ""),
                "committer_email": committer.get("email", ""),
                "committer_date": committer.get("date", ""),
                "message_first_line": message_first,
                "html_url": it.get("html_url", ""),
            })

        # If this page was not full, there are no more results to fetch
        if len(items) < PER_PAGE:
            break
        # Be polite: short sleep between pages to avoid secondary rate limits
        time.sleep(page_sleep)

    return total, rows, incomplete, error


# -----------------------------------------------------------------------------
# Main driver
# -----------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description="Multi-channel agent-commit collector.")
    p.add_argument(
        "--window", choices=["24h", "alltime"], default="24h",
        help="Time window. '24h' = last 24 hours (default). "
             "'alltime' = 2020-01-01..now, to test whether a channel identifier is alive at all.",
    )
    p.add_argument(
        "--max-pages", type=int, default=None,
        help="Override pages per channel. Default: 10 for 24h, 1 for alltime "
             "(alltime mostly cares about total_count + a small sample, since the 1000-cap binds).",
    )
    p.add_argument(
        "--page-sleep", type=float, default=DEFAULT_PAGE_SLEEP,
        help=f"Sleep between pages within a channel (default {DEFAULT_PAGE_SLEEP}s).",
    )
    p.add_argument(
        "--channel-sleep", type=float, default=DEFAULT_CHANNEL_SLEEP,
        help=f"Sleep between channels (default {DEFAULT_CHANNEL_SLEEP}s). "
             "Prevents Search API secondary rate-limit (403) bursts.",
    )
    return p.parse_args()


def main():
    args = parse_args()
    token = load_token()
    if not token:
        print("ERROR: GITHUB_TOKEN not found.", file=sys.stderr)
        sys.exit(1)

    # Build the time window based on --window.
    now = datetime.now(timezone.utc).replace(microsecond=0)
    if args.window == "24h":
        since = now - timedelta(hours=24)
        default_max_pages = SEARCH_CAP_PAGES   # fetch as much as the API allows
    else:
        since = ALLTIME_SINCE
        # For alltime, almost every channel exceeds the 1000-result cap, so
        # paginating to 10 pages just wastes time. 1 page = total_count + a 100-row sample.
        default_max_pages = 1

    max_pages = args.max_pages if args.max_pages is not None else default_max_pages
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    until_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    # Filename-safe timestamp (colons replaced) used in output filenames
    run_stamp = now.strftime("%Y-%m-%dT%H-%M-%SZ")

    # Each window writes into its own subdir so 24h and alltime never collide.
    out_dir = PILOT_ROOT / args.window
    out_dir.mkdir(exist_ok=True)

    print(f"Window mode: {args.window}")
    print(f"Window:      {since_iso}  to  {until_iso}")
    print(f"Output dir:  {out_dir}")
    print(f"Channels:    {len(CHANNELS)}")
    print(f"Max pages:   {max_pages} (per channel)")
    print()

    # Collect per-agent rows (combined across that agent's channels) plus a summary
    rows_by_agent = {}
    summary = []
    fieldnames = [
        "agent", "channel", "mode", "query_type", "query_value",
        "sha", "repo", "author_name", "author_email", "author_date",
        "committer_email", "committer_date", "message_first_line", "html_url",
    ]

    for idx, (agent, channel_id, mode, query_type, value, audit_total) in enumerate(CHANNELS):
        print(f"[{agent}/{channel_id}] mode={mode} q_type={query_type} value={value!r}")
        total, rows, incomplete, error = collect_one(
            token, agent, channel_id, mode, query_type, value,
            since_iso, until_iso, max_pages, args.page_sleep,
        )
        print(f"   total_count={total} fetched={len(rows)} incomplete={incomplete} error={error}")
        summary.append({
            "agent": agent,
            "channel": channel_id,
            "mode": mode,
            "query_type": query_type,
            "query_value": value,
            "audit_total_lifetime": audit_total,    # reference snapshot from March 2026
            # window_total_count = -1 means "not measured due to error";
            # 0 means "successfully measured, genuinely no hits in this window".
            "window_total_count": total if total is not None else -1,
            "fetched_rows": len(rows),
            "incomplete_results": incomplete,
            "error": error or "",
        })
        rows_by_agent.setdefault(agent, []).extend(rows)
        # Sleep between channels to avoid secondary rate limits.
        if idx < len(CHANNELS) - 1:
            time.sleep(args.channel_sleep)

    # Write one CSV per agent (rows across all channels for that agent are concatenated)
    for agent, rows in rows_by_agent.items():
        out_file = out_dir / f"{agent}_{run_stamp}.csv"
        with open(out_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"wrote {len(rows):>6} rows -> {out_file.name}")

    # Write the per-channel summary table (totals + fetched counts)
    summary_file = out_dir / f"summary_{run_stamp}.csv"
    with open(summary_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(summary[0].keys()))
        writer.writeheader()
        writer.writerows(summary)
    print(f"wrote summary -> {summary_file.name}")
    print()

    # Pretty-print the summary to stdout for meeting-ready screenshots
    total_label = "total" if args.window == "alltime" else "24h_total"
    print("=" * 115)
    print(f"{'agent':<8} {'channel':<22} {'mode':<9} {'q_type':<13} {total_label:>12} {'fetched':>8}  status")
    print("-" * 115)
    for r in summary:
        tot = r["window_total_count"]
        tot_str = "ERR" if tot < 0 else str(tot)
        status = "ERROR" if r["error"] else ("OK" if tot > 0 else "0-hits")
        print(
            f"{r['agent']:<8} {r['channel']:<22} {r['mode']:<9} {r['query_type']:<13} "
            f"{tot_str:>12} {r['fetched_rows']:>8}  {status}"
        )
    print("=" * 115)

    # Flag channels that hit the 1000-result search cap; these need window slicing
    over_cap = [r for r in summary if r["window_total_count"] > SEARCH_CAP_PAGES * PER_PAGE]
    if over_cap:
        print(f"\n{len(over_cap)} channel(s) exceed the 1000-result Search API cap in this window.")
        print("   For full corpus, slice the window into smaller chunks (e.g., hourly).")

    # Errored channels need re-running, not interpretation.
    errored = [r for r in summary if r["error"]]
    if errored:
        print(f"\n{len(errored)} channel(s) errored (treat as 'unknown', not as '0 hits'):")
        for r in errored:
            print(f"   - {r['agent']}/{r['channel']:<22} error={r['error']}")

    # Genuinely-zero channels: successfully queried, no results found.
    # Compare 24h vs alltime to distinguish wrong-identifier vs outdated-identifier:
    #   - 0 in alltime          -> identifier is wrong (channel never worked)
    #   - 0 in 24h, >0 alltime  -> identifier is outdated (agent stopped using it)
    dead = [r for r in summary if r["window_total_count"] == 0]
    if dead:
        print(f"\n{len(dead)} channel(s) returned genuine 0 hits in this window:")
        for r in dead:
            print(f"   - {r['agent']}/{r['channel']:<22} value={r['query_value']!r}")


if __name__ == "__main__":
    main()

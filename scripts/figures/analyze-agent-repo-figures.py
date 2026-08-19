"""Generate detailed figures for AI agent commit and repository analysis.
[IN]: stable data_products commit population, repo metadata, and selected channel-count CSV files.
[OUT]: figures/agent_repo_analysis/*.png, figures/agent_repo_analysis/tables/*.csv, and generated README.md.
[POS]: Generates report-ready figures and backing tables for agent/repository analysis.
[SYNC]: If figure/table names or input assumptions change, update scripts/CLAUDE.md, scripts/OUTPUTS.md, and figures/agent_repo_analysis/README.md.

Inputs are local CSV products created by the collection pipeline. The script
does not modify raw data; it writes derived tables and PNG figures under
figures/agent_repo_analysis by default.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns


AGENTS = ["claude", "codex", "copilot", "cursor"]
TARGET_LANGUAGES = ["Python", "Java", "JavaScript", "TypeScript"]
STAR_BUCKETS = ["0", "1-4", "5-9", "10-99", "100+"]


def parse_args() -> argparse.Namespace:
    root_default = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root_default)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--top-n", type=int, default=20)
    return parser.parse_args()


def paths(root: Path) -> dict[str, Path]:
    return {
        "four": root / "data_products" / "agent_commit_population_corrected_v1" / "agent_commit_population.csv",
        "merged": root / "data_products" / "agent_commit_population_merged_1y_v1" / "merged_agent_commit_population.csv",
        "repo": root / "data_products" / "repo_metadata_merged_1y_v1" / "repo_metadata.csv",
        "four_channels": root / "scripts" / "pilot" / "annual_sample_2026-06-10T11-06-53Z_corrected_v1" / "channel_counts.csv",
        "nonclaude_raw": root / "scripts" / "pilot" / "nonclaude_optimized_2026-06-12T12-32-47Z" / "sha_channels_raw.csv",
        "nonclaude_channels": root / "data_products" / "nonclaude_optimized_cleaned_v1" / "channel_counts_clean.csv",
    }


def mkdirs(out: Path) -> Path:
    (out / "tables").mkdir(parents=True, exist_ok=True)
    return out / "tables"


def setup_style() -> None:
    sns.set_theme(style="whitegrid", context="paper")
    plt.rcParams.update(
        {
            "figure.dpi": 130,
            "savefig.dpi": 220,
            "axes.titlesize": 12,
            "axes.labelsize": 10,
            "xtick.labelsize": 8,
            "ytick.labelsize": 8,
            "legend.fontsize": 8,
        }
    )


def savefig(out: Path, name: str) -> None:
    plt.tight_layout()
    plt.savefig(out / name, bbox_inches="tight")
    plt.close()


def read_existing(path: Path, **kwargs) -> pd.DataFrame:
    if not path.exists():
        print(f"missing={path}")
        return pd.DataFrame()
    return pd.read_csv(path, **kwargs)


def add_month(df: pd.DataFrame, date_col: str = "author_date") -> pd.DataFrame:
    df = df.copy()
    dt = pd.to_datetime(df[date_col], errors="coerce", utc=True)
    df["month"] = dt.dt.tz_convert(None).dt.to_period("M").astype(str)
    return df[df["month"].notna()]


def star_bucket(stars: pd.Series) -> pd.Series:
    bins = [-1, 0, 4, 9, 99, np.inf]
    return pd.cut(stars.fillna(0), bins=bins, labels=STAR_BUCKETS).astype(str)


def clean_bool(series: pd.Series) -> pd.Series:
    return series.astype(str).str.lower().isin(["true", "1", "yes"])


def load_four_monthly(p: Path, table_dir: Path) -> pd.DataFrame:
    df = read_existing(p, usecols=["agent", "repo_sha", "author_date"])
    if df.empty:
        return df
    df = add_month(df).drop_duplicates(["agent", "repo_sha", "month"])
    monthly = df.groupby(["month", "agent"], observed=True)["repo_sha"].nunique().reset_index(name="unique_repo_sha")
    monthly.to_csv(table_dir / "monthly_4agent_10min.csv", index=False)
    return monthly


def load_nonclaude_monthly(p: Path, table_dir: Path) -> pd.DataFrame:
    cols = ["agent", "repo_sha", "author_date"]
    df = read_existing(p, usecols=cols)
    if df.empty:
        return df
    df = df[df["agent"].isin(["codex", "copilot", "cursor"])]
    df = add_month(df).drop_duplicates(["agent", "repo_sha", "month"])
    monthly = df.groupby(["month", "agent"], observed=True)["repo_sha"].nunique().reset_index(name="unique_repo_sha")
    monthly.to_csv(table_dir / "monthly_3agent_4h.csv", index=False)
    return monthly


def plot_monthly_lines(monthly: pd.DataFrame, out: Path, name: str, title: str, logy: bool = False) -> None:
    if monthly.empty:
        return
    pivot = monthly.pivot(index="month", columns="agent", values="unique_repo_sha").fillna(0)
    pivot = pivot.reindex(columns=[a for a in AGENTS if a in pivot.columns])
    ax = pivot.plot(marker="o", figsize=(10.8, 5.2), linewidth=2)
    ax.set_title(title)
    ax.set_xlabel("Month")
    ax.set_ylabel("Unique repo_sha")
    if logy:
        ax.set_yscale("log")
    ax.tick_params(axis="x", rotation=45)
    ax.legend(title="Agent", ncol=4)
    savefig(out, name)


def plot_monthly_stacked(monthly: pd.DataFrame, out: Path, name: str, title: str) -> None:
    if monthly.empty:
        return
    pivot = monthly.pivot(index="month", columns="agent", values="unique_repo_sha").fillna(0)
    pivot = pivot.reindex(columns=[a for a in AGENTS if a in pivot.columns])
    ax = pivot.plot(kind="bar", stacked=True, figsize=(11, 5.4), width=0.78)
    ax.set_title(title)
    ax.set_xlabel("Month")
    ax.set_ylabel("Unique repo_sha")
    ax.tick_params(axis="x", rotation=45)
    ax.legend(title="Agent", ncol=4)
    savefig(out, name)


def load_channel_counts(path: Path, table_name: str, table_dir: Path) -> pd.DataFrame:
    df = read_existing(path)
    if df.empty:
        return df
    df = df[df["agent"].astype(str).isin(AGENTS)].copy()
    df["unique_repo_sha_after_dedup"] = pd.to_numeric(df["unique_repo_sha_after_dedup"], errors="coerce").fillna(0)
    df["raw_rows_before_dedup"] = pd.to_numeric(df["raw_rows_before_dedup"], errors="coerce").fillna(0)
    df = df[df["unique_repo_sha_after_dedup"] > 0]
    df.to_csv(table_dir / table_name, index=False)
    return df


def plot_channel_share(df: pd.DataFrame, out: Path, name: str, title: str) -> None:
    if df.empty:
        return
    tmp = df.copy()
    tmp["channel_label"] = tmp["agent"] + "/" + tmp["channel"]
    tmp = tmp.sort_values(["agent", "unique_repo_sha_after_dedup"], ascending=[True, False])
    plt.figure(figsize=(11, max(4.8, len(tmp) * 0.24)))
    sns.barplot(data=tmp, y="channel_label", x="unique_repo_sha_after_dedup", hue="mode", dodge=False)
    plt.title(title)
    plt.xlabel("Unique repo_sha after per-channel dedup")
    plt.ylabel("Channel")
    plt.xscale("log")
    plt.legend(title="Mode", loc="lower right")
    savefig(out, name)


def load_repo_metadata(path: Path, table_dir: Path) -> pd.DataFrame:
    usecols = [
        "repo",
        "status",
        "agent_commit_rows",
        "agents",
        "owner_type",
        "fork",
        "archived",
        "size",
        "stargazers_count",
        "forks_count",
        "open_issues_count",
        "language",
        "created_at",
        "updated_at",
        "pushed_at",
        "license_key",
    ]
    df = read_existing(path, usecols=usecols, low_memory=False)
    if df.empty:
        return df
    for col in ["agent_commit_rows", "size", "stargazers_count", "forks_count", "open_issues_count"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    df["language"] = df["language"].fillna("blank").replace("", "blank")
    df["fork_bool"] = clean_bool(df["fork"])
    df["archived_bool"] = clean_bool(df["archived"])
    df["star_bucket"] = star_bucket(df["stargazers_count"])
    df["repo_count"] = 1
    summary = df.groupby("status", observed=True).size().reset_index(name="repos")
    summary.to_csv(table_dir / "repo_status_summary.csv", index=False)
    return df


def repo_language_tables(repo: pd.DataFrame, table_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    ok = repo[repo["status"] == "ok"].copy()
    lang = ok.groupby("language", observed=True).agg(
        repos=("repo", "count"),
        agent_commit_rows=("agent_commit_rows", "sum"),
        median_stars=("stargazers_count", "median"),
    )
    lang = lang.sort_values("repos", ascending=False).reset_index()
    lang.to_csv(table_dir / "repo_language_distribution.csv", index=False)
    combo = ok.groupby("agents", observed=True).agg(repos=("repo", "count"), agent_commit_rows=("agent_commit_rows", "sum"))
    combo = combo.sort_values("repos", ascending=False).reset_index()
    combo.to_csv(table_dir / "repo_agent_combo_distribution.csv", index=False)
    return lang, combo


def plot_repo_languages(lang: pd.DataFrame, out: Path, top_n: int) -> None:
    if lang.empty:
        return
    top = lang.head(top_n).sort_values("repos")
    plt.figure(figsize=(9.5, 6.4))
    sns.barplot(data=top, y="language", x="repos", color="#4C78A8")
    plt.title(f"Top {top_n} repository languages")
    plt.xlabel("Accessible repositories")
    plt.ylabel("Language")
    savefig(out, "06_repo_language_top.png")
    top2 = lang.sort_values("agent_commit_rows", ascending=False).head(top_n).sort_values("agent_commit_rows")
    plt.figure(figsize=(9.5, 6.4))
    sns.barplot(data=top2, y="language", x="agent_commit_rows", color="#F58518")
    plt.title(f"Top {top_n} languages by agent-attributed commit rows")
    plt.xlabel("Agent-attributed rows in repos")
    plt.ylabel("Language")
    savefig(out, "07_language_by_agent_rows.png")


def repo_feature_tables(repo: pd.DataFrame, table_dir: Path) -> pd.DataFrame:
    ok = repo[repo["status"] == "ok"].copy()
    rows = [
        ("all_ok", len(ok)),
        ("non_fork", int((~ok["fork_bool"]).sum())),
        ("non_archived", int((~ok["archived_bool"]).sum())),
        ("target_language", int(ok["language"].isin(TARGET_LANGUAGES).sum())),
        ("target_nonfork_nonarchived", int((ok["language"].isin(TARGET_LANGUAGES) & ~ok["fork_bool"] & ~ok["archived_bool"]).sum())),
        ("target_nonfork_nonarchived_stars_ge_1", int((ok["language"].isin(TARGET_LANGUAGES) & ~ok["fork_bool"] & ~ok["archived_bool"] & (ok["stargazers_count"] >= 1)).sum())),
        ("target_nonfork_nonarchived_stars_ge_5", int((ok["language"].isin(TARGET_LANGUAGES) & ~ok["fork_bool"] & ~ok["archived_bool"] & (ok["stargazers_count"] >= 5)).sum())),
        ("target_nonfork_nonarchived_stars_ge_10", int((ok["language"].isin(TARGET_LANGUAGES) & ~ok["fork_bool"] & ~ok["archived_bool"] & (ok["stargazers_count"] >= 10)).sum())),
    ]
    funnel = pd.DataFrame(rows, columns=["filter", "repos"])
    funnel.to_csv(table_dir / "repo_filter_funnel.csv", index=False)
    return funnel


def plot_repo_features(repo: pd.DataFrame, funnel: pd.DataFrame, out: Path) -> None:
    ok = repo[repo["status"] == "ok"].copy()
    fig, axes = plt.subplots(1, 3, figsize=(13, 4.3))
    ok["star_bucket"].value_counts().reindex(STAR_BUCKETS).plot(kind="bar", ax=axes[0], color="#4C78A8")
    axes[0].set_title("Stars")
    axes[0].set_xlabel("Star bucket")
    axes[0].set_ylabel("Repositories")
    ok["owner_type"].fillna("blank").value_counts().plot(kind="bar", ax=axes[1], color="#54A24B")
    axes[1].set_title("Owner type")
    axes[1].set_xlabel("")
    pd.Series({"non-fork": (~ok["fork_bool"]).sum(), "fork": ok["fork_bool"].sum(), "archived": ok["archived_bool"].sum()}).plot(kind="bar", ax=axes[2], color="#E45756")
    axes[2].set_title("Fork / archived")
    axes[2].set_xlabel("")
    savefig(out, "08_repo_feature_overview.png")
    plt.figure(figsize=(10.5, 4.8))
    sns.barplot(data=funnel, x="filter", y="repos", color="#72B7B2")
    plt.title("Repository filtering funnel")
    plt.xlabel("")
    plt.ylabel("Repositories")
    plt.xticks(rotation=35, ha="right")
    plt.yscale("log")
    savefig(out, "09_repo_filter_funnel.png")


def plot_agent_combos(combo: pd.DataFrame, out: Path, top_n: int) -> None:
    if combo.empty:
        return
    top = combo.head(top_n).sort_values("repos")
    plt.figure(figsize=(9.8, 6))
    sns.barplot(data=top, y="agents", x="repos", color="#B279A2")
    plt.title(f"Top {top_n} agent combinations by repository count")
    plt.xlabel("Repositories")
    plt.ylabel("Agent combination")
    savefig(out, "10_agent_combo_top.png")


def plot_top_repos(repo: pd.DataFrame, out: Path, table_dir: Path, top_n: int) -> None:
    ok = repo[repo["status"] == "ok"].copy()
    cols = ["repo", "agent_commit_rows", "agents", "language", "stargazers_count"]
    top = ok.sort_values("agent_commit_rows", ascending=False).head(top_n)[cols]
    top.to_csv(table_dir / "top_repositories_by_agent_rows.csv", index=False)
    plt.figure(figsize=(10, 6.5))
    plot = top.sort_values("agent_commit_rows")
    labels = plot["repo"].str.slice(0, 42)
    sns.barplot(x=plot["agent_commit_rows"], y=labels, hue=plot["agents"], dodge=False)
    plt.title(f"Top {top_n} repositories by agent-attributed commit rows")
    plt.xlabel("Agent-attributed rows")
    plt.ylabel("Repository")
    plt.legend(title="Agents", loc="lower right")
    savefig(out, "11_top_repos_agent_rows.png")


def plot_repo_scatter(repo: pd.DataFrame, out: Path) -> None:
    ok = repo[(repo["status"] == "ok") & (repo["agent_commit_rows"] > 0)].copy()
    if ok.empty:
        return
    sample = ok.sort_values("agent_commit_rows", ascending=False).head(25000)
    plt.figure(figsize=(8.5, 5.8))
    plt.scatter(sample["stargazers_count"] + 1, sample["agent_commit_rows"], s=8, alpha=0.28)
    plt.xscale("log")
    plt.yscale("log")
    plt.title("Repository popularity vs agent-attributed activity")
    plt.xlabel("Stars + 1, log scale")
    plt.ylabel("Agent-attributed rows, log scale")
    savefig(out, "12_repo_stars_vs_agent_rows.png")
    plt.figure(figsize=(8.5, 5.8))
    plt.scatter(sample["size"] + 1, sample["agent_commit_rows"], s=8, alpha=0.25, color="#F58518")
    plt.xscale("log")
    plt.yscale("log")
    plt.title("Repository size vs agent-attributed activity")
    plt.xlabel("Repo size + 1, log scale")
    plt.ylabel("Agent-attributed rows, log scale")
    savefig(out, "13_repo_size_vs_agent_rows.png")


def aggregate_agent_language(merged_path: Path, repo: pd.DataFrame, table_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    ok = repo[repo["status"] == "ok"][["repo", "language", "star_bucket"]].copy()
    repo_to_lang = ok.set_index("repo")["language"].to_dict()
    repo_to_stars = ok.set_index("repo")["star_bucket"].to_dict()
    lang_parts = []
    star_parts = []
    cols = ["agent", "repo", "repo_sha"]
    for chunk in pd.read_csv(merged_path, usecols=cols, chunksize=250000):
        chunk = chunk[chunk["agent"].isin(AGENTS)]
        chunk["language"] = chunk["repo"].map(repo_to_lang).fillna("unknown")
        chunk["star_bucket"] = chunk["repo"].map(repo_to_stars).fillna("unknown")
        lang_parts.append(chunk.groupby(["agent", "language"], observed=True)["repo_sha"].nunique())
        star_parts.append(chunk.groupby(["agent", "star_bucket"], observed=True)["repo_sha"].nunique())
    lang = pd.concat(lang_parts).groupby(level=[0, 1]).sum().reset_index(name="unique_repo_sha")
    stars = pd.concat(star_parts).groupby(level=[0, 1]).sum().reset_index(name="unique_repo_sha")
    lang.to_csv(table_dir / "agent_language_unique_repo_sha.csv", index=False)
    stars.to_csv(table_dir / "agent_star_bucket_unique_repo_sha.csv", index=False)
    return lang, stars


def plot_agent_language(lang: pd.DataFrame, out: Path, top_n: int) -> None:
    if lang.empty:
        return
    top_langs = lang.groupby("language")["unique_repo_sha"].sum().sort_values(ascending=False).head(top_n).index
    data = lang[lang["language"].isin(top_langs)]
    pivot = data.pivot_table(index="language", columns="agent", values="unique_repo_sha", aggfunc="sum", fill_value=0)
    pivot = pivot.reindex(pivot.sum(axis=1).sort_values(ascending=False).index)
    plt.figure(figsize=(8, max(5, len(pivot) * 0.28)))
    sns.heatmap(np.log10(pivot + 1), annot=pivot, fmt=".0f", cmap="YlGnBu", cbar_kws={"label": "log10(count+1)"})
    plt.title(f"Agent commits by repository language, top {top_n}")
    plt.xlabel("Agent")
    plt.ylabel("Language")
    savefig(out, "14_agent_language_heatmap.png")


def plot_agent_star_bucket(stars: pd.DataFrame, out: Path) -> None:
    if stars.empty:
        return
    data = stars[stars["star_bucket"].isin(STAR_BUCKETS)]
    pivot = data.pivot_table(index="agent", columns="star_bucket", values="unique_repo_sha", aggfunc="sum", fill_value=0)
    pivot = pivot.reindex(index=[a for a in AGENTS if a in pivot.index], columns=STAR_BUCKETS)
    plt.figure(figsize=(8.5, 4.5))
    sns.heatmap(np.log10(pivot + 1), annot=pivot, fmt=".0f", cmap="PuBuGn", cbar_kws={"label": "log10(count+1)"})
    plt.title("Agent commits by repository star bucket")
    plt.xlabel("Star bucket")
    plt.ylabel("Agent")
    savefig(out, "15_agent_star_bucket_heatmap.png")


def write_index(out: Path, table_dir: Path) -> None:
    figures = sorted(p.name for p in out.glob("*.png"))
    tables = sorted(p.name for p in table_dir.glob("*.csv"))
    lines = ["# Agent Repository Analysis Figures", "", "## Figures", ""]
    lines += [f"- `{name}`" for name in figures]
    lines += ["", "## Tables", ""]
    lines += [f"- `tables/{name}`" for name in tables]
    lines += ["", "Generated by `scripts/figures/analyze-agent-repo-figures.py`.", ""]
    (out / "README.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    root = args.root.resolve()
    out = (args.out or (root / "figures" / "agent_repo_analysis")).resolve()
    table_dir = mkdirs(out)
    setup_style()
    p = paths(root)

    four_monthly = load_four_monthly(p["four"], table_dir)
    plot_monthly_lines(four_monthly, out, "01_agent_monthly_trend_4agent_10min.png", "4-agent monthly trend, 10 min/day baseline", logy=False)
    plot_monthly_lines(four_monthly, out, "02_agent_monthly_trend_4agent_10min_log.png", "4-agent monthly trend, 10 min/day baseline, log scale", logy=True)
    plot_monthly_stacked(four_monthly, out, "03_agent_monthly_stacked_4agent_10min.png", "4-agent monthly volume composition, 10 min/day baseline")

    nonclaude_monthly = load_nonclaude_monthly(p["nonclaude_raw"], table_dir)
    plot_monthly_lines(nonclaude_monthly, out, "04_nonclaude_monthly_trend_4h.png", "Non-Claude monthly trend, 4 h/day sample", logy=False)
    plot_monthly_stacked(nonclaude_monthly, out, "05_nonclaude_monthly_stacked_4h.png", "Non-Claude monthly volume composition, 4 h/day sample")

    four_channels = load_channel_counts(p["four_channels"], "channel_counts_4agent_10min.csv", table_dir)
    nonclaude_channels = load_channel_counts(p["nonclaude_channels"], "channel_counts_3agent_4h.csv", table_dir)
    plot_channel_share(four_channels, out, "16_channel_share_4agent_10min.png", "4-agent channel contribution, 10 min/day baseline")
    plot_channel_share(nonclaude_channels, out, "17_channel_share_3agent_4h.png", "Non-Claude channel contribution, 4 h/day sample")

    repo = load_repo_metadata(p["repo"], table_dir)
    lang, combo = repo_language_tables(repo, table_dir)
    funnel = repo_feature_tables(repo, table_dir)
    plot_repo_languages(lang, out, args.top_n)
    plot_repo_features(repo, funnel, out)
    plot_agent_combos(combo, out, args.top_n)
    plot_top_repos(repo, out, table_dir, args.top_n)
    plot_repo_scatter(repo, out)

    agent_lang, agent_stars = aggregate_agent_language(p["merged"], repo, table_dir)
    plot_agent_language(agent_lang, out, args.top_n)
    plot_agent_star_bucket(agent_stars, out)

    write_index(out, table_dir)
    print(f"output_dir={out}")
    print(f"figures={len(list(out.glob('*.png')))}")
    print(f"tables={len(list(table_dir.glob('*.csv')))}")


if __name__ == "__main__":
    main()

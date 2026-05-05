"""
NBA playoff totals report for 2025-26 second-round matchups.

Data source: Basketball-Reference team game-log pages
(stats.nba.com is unreachable from this network, so we scrape BR instead).

For each matchup prints:
  - Each team's regular-season avg PPG / OPP / TOTAL
  - Each team's playoff (R1) avg PPG / OPP / TOTAL
  - Last-10 regular-season totals trend
  - Head-to-head regular-season games + avg total
"""
from __future__ import annotations

import io
import time
import requests
import pandas as pd

SEASON_YEAR = 2026
HEADERS = {"User-Agent": "Mozilla/5.0 (research; nbaPlayoffsPred)"}

# Display name -> BR 3-letter abbr + full team name (as shown in opp_name column)
TEAMS = {
    "Knicks":       ("NYK", "New York Knicks"),
    "76ers":        ("PHI", "Philadelphia 76ers"),
    "Cavaliers":    ("CLE", "Cleveland Cavaliers"),
    "Pistons":      ("DET", "Detroit Pistons"),
    "Timberwolves": ("MIN", "Minnesota Timberwolves"),
    "Spurs":        ("SAS", "San Antonio Spurs"),
    "Lakers":       ("LAL", "Los Angeles Lakers"),
    "Thunder":      ("OKC", "Oklahoma City Thunder"),
}

MATCHUPS = [
    ("Knicks", "76ers"),
    ("Cavaliers", "Pistons"),
    ("Timberwolves", "Spurs"),
    ("Lakers", "Thunder"),
]


def fetch_team_games(abbr: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (regular_season_df, playoff_df) for one team."""
    url = f"https://www.basketball-reference.com/teams/{abbr}/{SEASON_YEAR}_games.html"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    html = r.text

    # pandas finds tables by id via attrs
    rs_tables = pd.read_html(io.StringIO(html), attrs={"id": "games"})
    po_tables = pd.read_html(io.StringIO(html), attrs={"id": "games_playoffs"})
    rs = clean(rs_tables[0]) if rs_tables else pd.DataFrame()
    po = clean(po_tables[0]) if po_tables else pd.DataFrame()
    return rs, po


def clean(df: pd.DataFrame) -> pd.DataFrame:
    # Strip the repeating header rows BR injects mid-table.
    df = df.copy()
    if "G" in df.columns:
        df = df[df["G"] != "G"]
    # Standardize key columns by position-independent names.
    rename = {
        "Date": "date",
        "Opponent": "opp",
        "Tm": "pts",
        "Opp": "opp_pts",
    }
    for k, v in rename.items():
        if k in df.columns:
            df = df.rename(columns={k: v})
    # Some BR tables have an unnamed home/away column ('@' for away, blank for home)
    for col in df.columns:
        if df[col].astype(str).isin(["@", "", "nan"]).all():
            df = df.rename(columns={col: "loc"})
            break
    if {"pts", "opp_pts"}.issubset(df.columns):
        df["pts"] = pd.to_numeric(df["pts"], errors="coerce")
        df["opp_pts"] = pd.to_numeric(df["opp_pts"], errors="coerce")
        df = df.dropna(subset=["pts", "opp_pts"])
        df["total"] = df["pts"] + df["opp_pts"]
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.sort_values("date").reset_index(drop=True)
    return df


def fmt_summary(df: pd.DataFrame, label: str) -> str:
    if df.empty:
        return f"  {label}: no games"
    return (
        f"  {label} ({len(df)} g): "
        f"PPG {df['pts'].mean():.1f} | "
        f"OPP {df['opp_pts'].mean():.1f} | "
        f"TOTAL {df['total'].mean():.1f} "
        f"(min {int(df['total'].min())}, max {int(df['total'].max())})"
    )


def fmt_last_n(df: pd.DataFrame, n: int = 10) -> str:
    if df.empty:
        return f"  last-{n}: n/a"
    g = df.tail(n)
    totals = [int(t) for t in g["total"].tolist()]
    return f"  last-{len(g)} totals avg {sum(totals)/len(totals):.1f} -> {totals}"


def head_to_head(rs: pd.DataFrame, opp_full_name: str) -> pd.DataFrame:
    if rs.empty or "opp" not in rs.columns:
        return pd.DataFrame()
    return rs[rs["opp"] == opp_full_name]


def main() -> None:
    print(f"Fetching {SEASON_YEAR-1}-{str(SEASON_YEAR)[-2:]} game logs from Basketball-Reference...\n")
    cache: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
    for name, (abbr, _) in TEAMS.items():
        print(f"  {name} ({abbr})...")
        cache[name] = fetch_team_games(abbr)
        time.sleep(3.5)  # be polite to BR (rate limit ~20/min)
    print()

    for a, b in MATCHUPS:
        rs_a, po_a = cache[a]
        rs_b, po_b = cache[b]
        print("=" * 72)
        print(f"{a}  vs  {b}")
        print("=" * 72)

        for name, rs, po in [(a, rs_a, po_a), (b, rs_b, po_b)]:
            print(f"\n{name}")
            print(fmt_summary(rs, "regular season"))
            print(fmt_summary(po, "playoffs (R1) "))
            print(fmt_last_n(rs, 10))

        full_a = TEAMS[a][1]
        full_b = TEAMS[b][1]
        h2h = head_to_head(rs_a, full_b)
        print(f"\nHead-to-head regular season ({a} pov): {len(h2h)} game(s)")
        if not h2h.empty:
            for _, r in h2h.iterrows():
                d = r["date"].strftime("%Y-%m-%d") if pd.notna(r["date"]) else "?"
                loc = r.get("loc", "")
                where = "@" if loc == "@" else "vs"
                print(
                    f"  {d}  {a} {where} {b}  "
                    f"{int(r['pts'])}-{int(r['opp_pts'])}  total {int(r['total'])}"
                )
            print(f"  -> avg total in H2H: {h2h['total'].mean():.1f}")

        # Combined recent-form total: mean of both teams' last-10 totals
        if not rs_a.empty and not rs_b.empty:
            la = rs_a.tail(10)["total"].mean()
            lb = rs_b.tail(10)["total"].mean()
            print(
                f"\nCombined recent-form signal: "
                f"{a} last-10 avg total {la:.1f}, {b} last-10 avg total {lb:.1f}, "
                f"midpoint {(la + lb) / 2:.1f}"
            )
        print()


if __name__ == "__main__":
    main()

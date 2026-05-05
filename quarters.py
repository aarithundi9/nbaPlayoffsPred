"""
Quarter-by-quarter scoring breakdown for the Knicks-76ers second-round matchup.

For each team: avg points per quarter (regular season last 15 + all R1 playoff
games), then combined matchup expectations and live cash-out thresholds.

Caches box-score HTML to ./cache/ so re-runs are fast.
"""
from __future__ import annotations

import os
import re
import time
import requests
import pandas as pd

SEASON_YEAR = 2026
HEADERS = {"User-Agent": "Mozilla/5.0 (research; nbaPlayoffsPred)"}
CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

TEAMS = {
    "Knicks": ("NYK", "New York Knicks"),
    "76ers":  ("PHI", "Philadelphia 76ers"),
}

# How many regular-season games to include (last N games of the season)
LAST_N_RS = 15


def get(url: str, sleep: float = 3.5) -> str:
    """Fetch with on-disk cache."""
    key = re.sub(r"[^a-zA-Z0-9]+", "_", url) + ".html"
    path = os.path.join(CACHE_DIR, key)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    with open(path, "w", encoding="utf-8") as f:
        f.write(r.text)
    time.sleep(sleep)
    return r.text


BOX_RE = re.compile(r'/boxscores/(\d{9}[A-Z]{3})\.html')


def list_box_scores(abbr: str) -> tuple[list[str], list[str]]:
    """Return (regular_season_box_ids, playoff_box_ids) for a team."""
    html = get(f"https://www.basketball-reference.com/teams/{abbr}/{SEASON_YEAR}_games.html")
    # Split HTML at the playoff table marker so we can separate them.
    parts = html.split('id="games_playoffs"', 1)
    rs_html = parts[0]
    po_html = parts[1] if len(parts) == 2 else ""
    rs_ids = list(dict.fromkeys(BOX_RE.findall(rs_html)))   # preserve order, dedupe
    po_ids = list(dict.fromkeys(BOX_RE.findall(po_html)))
    return rs_ids, po_ids


# Match one team row in the line score table.
# captures: abbr, q1, q2, q3, q4, total
LINE_ROW_RE = re.compile(
    r"<tr ><th scope=\"row\" class=\"center \" data-stat=\"team\" >"
    r"<a href='/teams/([A-Z]{3})/\d{4}\.html'>[A-Z]{3}</a></th>"
    r"<td class=\"center \" data-stat=\"1\" >(\d+)</td>"
    r"<td class=\"center \" data-stat=\"2\" >(\d+)</td>"
    r"<td class=\"center \" data-stat=\"3\" >(\d+)</td>"
    r"<td class=\"center \" data-stat=\"4\" >(\d+)</td>"
    r"<td class=\"center \" data-stat=\"T\" ><strong>(\d+)</strong>"
)
# OT-aware fallback: just grab the first 4 quarter columns and the team abbr,
# computing total from quarters in case the game went to OT.
OT_ROW_RE = re.compile(
    r"<tr ><th scope=\"row\" class=\"center \" data-stat=\"team\" >"
    r"<a href='/teams/([A-Z]{3})/\d{4}\.html'>[A-Z]{3}</a></th>"
    r"((?:<td class=\"center \" data-stat=\"\d+\" >\d+</td>)+)"
)
DATE_RE = re.compile(r'<meta property="og:url" content="https://www\.basketball-reference\.com/boxscores/(\d{4})(\d{2})(\d{2})')


def parse_box(box_id: str) -> list[dict]:
    """Return list of dicts: [{team, q1, q2, q3, q4, total, date, box_id}, ...] for the two teams."""
    html = get(f"https://www.basketball-reference.com/boxscores/{box_id}.html")
    date_m = DATE_RE.search(html)
    date_str = f"{date_m.group(1)}-{date_m.group(2)}-{date_m.group(3)}" if date_m else box_id[:8]

    rows: list[dict] = []
    for m in LINE_ROW_RE.finditer(html):
        rows.append({
            "team": m.group(1),
            "q1": int(m.group(2)),
            "q2": int(m.group(3)),
            "q3": int(m.group(4)),
            "q4": int(m.group(5)),
            "total": int(m.group(6)),
            "date": date_str,
            "box_id": box_id,
            "ot": False,
        })
    if len(rows) == 2:
        return rows

    # Fallback for OT games — re-scan with looser regex
    rows = []
    for m in OT_ROW_RE.finditer(html):
        cells = re.findall(r'data-stat="(\d+)" >(\d+)', m.group(2))
        q = {int(k): int(v) for k, v in cells}
        if not all(i in q for i in (1, 2, 3, 4)):
            continue
        ot_pts = sum(v for k, v in q.items() if k > 4)
        rows.append({
            "team": m.group(1),
            "q1": q[1],
            "q2": q[2],
            "q3": q[3],
            "q4": q[4],
            "ot": ot_pts > 0,
            "total": q[1] + q[2] + q[3] + q[4] + ot_pts,
            "date": date_str,
            "box_id": box_id,
        })
    return rows[:2]  # only the two team rows


def build_df(team_abbr: str, opp_abbr: str | None = None) -> pd.DataFrame:
    rs_ids, po_ids = list_box_scores(team_abbr)
    rs_ids = rs_ids[-LAST_N_RS:]
    all_rows: list[dict] = []
    for season_type, ids in [("regular", rs_ids), ("playoff", po_ids)]:
        for box_id in ids:
            try:
                team_rows = parse_box(box_id)
            except Exception as e:
                print(f"  warn: failed {box_id}: {e}")
                continue
            if len(team_rows) != 2:
                continue
            t = next((r for r in team_rows if r["team"] == team_abbr), None)
            o = next((r for r in team_rows if r["team"] != team_abbr), None)
            if t is None or o is None:
                continue
            row = {
                "season_type": season_type,
                "date": t["date"],
                "box_id": box_id,
                "team": team_abbr,
                "opp": o["team"],
                "ot": t.get("ot", False),
                **{f"team_{q}": t[q] for q in ("q1", "q2", "q3", "q4")},
                **{f"opp_{q}": o[q] for q in ("q1", "q2", "q3", "q4")},
                "team_total": t["total"],
                "opp_total": o["total"],
                "game_total": t["total"] + o["total"],
            }
            all_rows.append(row)
    return pd.DataFrame(all_rows)


def quarter_summary(df: pd.DataFrame, label: str, scope: str | None = None) -> None:
    if scope == "regular":
        df = df[df["season_type"] == "regular"]
    elif scope == "playoff":
        df = df[df["season_type"] == "playoff"]
    if df.empty:
        print(f"  {label}: no games")
        return
    parts = []
    for q in ("q1", "q2", "q3", "q4"):
        team_avg = df[f"team_{q}"].mean()
        opp_avg = df[f"opp_{q}"].mean()
        parts.append(f"{q.upper()}: {team_avg:.1f} (opp {opp_avg:.1f}, total {team_avg+opp_avg:.1f})")
    half_team = (df["team_q1"] + df["team_q2"]).mean()
    half_opp = (df["opp_q1"] + df["opp_q2"]).mean()
    q3_team = (df["team_q1"] + df["team_q2"] + df["team_q3"]).mean()
    q3_opp = (df["opp_q1"] + df["opp_q2"] + df["opp_q3"]).mean()
    print(f"  {label} ({len(df)} g):")
    for p in parts:
        print(f"    {p}")
    print(f"    HALF total: {half_team + half_opp:.1f}   (team {half_team:.1f}, opp {half_opp:.1f})")
    print(f"    end-Q3 total: {q3_team + q3_opp:.1f}   (team {q3_team:.1f}, opp {q3_opp:.1f})")
    print(f"    FINAL total: {df['game_total'].mean():.1f}")


def main() -> None:
    all_dfs: dict[str, pd.DataFrame] = {}
    for name, (abbr, _full) in TEAMS.items():
        print(f"\nFetching {name} ({abbr}) box scores...")
        all_dfs[name] = build_df(abbr)

    print("\n" + "=" * 72)
    print("PER-TEAM QUARTER AVERAGES")
    print("=" * 72)
    for name, df in all_dfs.items():
        print(f"\n{name}")
        quarter_summary(df, f"last {LAST_N_RS} regular-season", scope="regular")
        quarter_summary(df, "R1 playoffs", scope="playoff")
        quarter_summary(df, "combined", scope=None)

    # Head-to-head this season
    print("\n" + "=" * 72)
    print("KNICKS vs 76ers — head-to-head this season (regular)")
    print("=" * 72)
    nyk = all_dfs["Knicks"]
    h2h = nyk[nyk["opp"] == "PHI"].copy()
    if h2h.empty:
        # H2H may not appear in last-15; try fetching from full list
        rs_ids, _ = list_box_scores("NYK")
        rows = []
        for box_id in rs_ids:
            tr = parse_box(box_id)
            if len(tr) == 2 and {tr[0]["team"], tr[1]["team"]} == {"NYK", "PHI"}:
                t = next(r for r in tr if r["team"] == "NYK")
                o = next(r for r in tr if r["team"] == "PHI")
                rows.append({
                    "date": t["date"], "box_id": box_id,
                    "team_q1": t["q1"], "team_q2": t["q2"], "team_q3": t["q3"], "team_q4": t["q4"],
                    "opp_q1": o["q1"], "opp_q2": o["q2"], "opp_q3": o["q3"], "opp_q4": o["q4"],
                    "team_total": t["total"], "opp_total": o["total"],
                    "game_total": t["total"] + o["total"],
                })
        h2h = pd.DataFrame(rows)

    if not h2h.empty:
        print()
        for _, r in h2h.sort_values("date").iterrows():
            line = (
                f"  {r['date']}  NYK {r['team_q1']}-{r['team_q2']}-{r['team_q3']}-{r['team_q4']}={r['team_total']} | "
                f"PHI {r['opp_q1']}-{r['opp_q2']}-{r['opp_q3']}-{r['opp_q4']}={r['opp_total']} | "
                f"total {r['game_total']}"
            )
            print(line)
        # Aggregate
        avg_q = {
            "Q1": (h2h["team_q1"] + h2h["opp_q1"]).mean(),
            "Q2": (h2h["team_q2"] + h2h["opp_q2"]).mean(),
            "Q3": (h2h["team_q3"] + h2h["opp_q3"]).mean(),
            "Q4": (h2h["team_q4"] + h2h["opp_q4"]).mean(),
        }
        print(f"\n  H2H avg quarter totals (combined both teams):")
        for q, v in avg_q.items():
            print(f"    {q}: {v:.1f}")
        half = avg_q["Q1"] + avg_q["Q2"]
        eq3 = half + avg_q["Q3"]
        final = h2h["game_total"].mean()
        print(f"    HALF total: {half:.1f}")
        print(f"    end-Q3 total: {eq3:.1f}")
        print(f"    FINAL total: {final:.1f}")

    # Cash-out thresholds for over 219.5
    print("\n" + "=" * 72)
    print("CASH-OUT THRESHOLDS for OVER 219.5 (game-flow benchmarks)")
    print("=" * 72)

    # Use combined-history baselines: average of NYK's playoff + 76ers' playoff
    # because playoff pace is the most relevant. Fallback to combined if no playoff.
    nyk_po = all_dfs["Knicks"][all_dfs["Knicks"]["season_type"] == "playoff"]
    phi_po = all_dfs["76ers"][all_dfs["76ers"]["season_type"] == "playoff"]

    if not nyk_po.empty and not phi_po.empty:
        # Average per-team, per-quarter from playoff samples
        baseline = {
            "q1": (nyk_po["team_q1"].mean() + phi_po["team_q1"].mean()),
            "q2": (nyk_po["team_q2"].mean() + phi_po["team_q2"].mean()),
            "q3": (nyk_po["team_q3"].mean() + phi_po["team_q3"].mean()),
            "q4": (nyk_po["team_q4"].mean() + phi_po["team_q4"].mean()),
        }
        baseline_total = sum(baseline.values())
        target = 219.5
        ratio = target / baseline_total

        print(f"\n  Playoff-pace baseline (NYK+PHI avg, both teams combined):")
        print(f"    Q1: {baseline['q1']:.1f}  Q2: {baseline['q2']:.1f}  Q3: {baseline['q3']:.1f}  Q4: {baseline['q4']:.1f}")
        print(f"    baseline final: {baseline_total:.1f} (vs target 219.5)")
        print(f"    pace adjustment to hit 219.5: {ratio:.2f}x baseline")
        print()
        print("  If you want to be on pace for 219.5 you should see roughly:")
        end_q1 = baseline['q1'] * ratio
        end_h = (baseline['q1'] + baseline['q2']) * ratio
        end_q3 = (baseline['q1'] + baseline['q2'] + baseline['q3']) * ratio
        print(f"    end of Q1:  total >= {end_q1:.0f}  (combined both teams)")
        print(f"    halftime:   total >= {end_h:.0f}")
        print(f"    end of Q3:  total >= {end_q3:.0f}")
        print()
        print("  Cash-out warnings (if total falls clearly below these):")
        # Use 90% of pace as warning threshold
        print(f"    end of Q1 < {end_q1 * 0.9:.0f}  -> on pace for ~{end_q1*0.9/ratio:.0f}, way behind")
        print(f"    halftime < {end_h * 0.9:.0f}     -> on pace for ~{end_h*0.9/ratio:.0f}")
        print(f"    end of Q3 < {end_q3 * 0.93:.0f}    -> on pace for ~{end_q3*0.93/ratio:.0f}, very hard to get to 220")


if __name__ == "__main__":
    main()

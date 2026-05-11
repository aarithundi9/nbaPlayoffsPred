"""
Quarter-by-quarter cash-out thresholds for the two Game-1s tonight.

For each matchup:
  - Per-team Q1/Q2/Q3/Q4 averages from last 15 regular-season + all R1 playoff
  - H2H games this season with quarter-by-quarter splits
  - On-pace targets and warning thresholds for the user's chosen line
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

# (display_name, BR abbr, full team name as shown in BR opp_name column)
TEAMS = {
    "Cavaliers":    ("CLE", "Cleveland Cavaliers"),
    "Pistons":      ("DET", "Detroit Pistons"),
    "Lakers":       ("LAL", "Los Angeles Lakers"),
    "Thunder":      ("OKC", "Oklahoma City Thunder"),
}

# (team_a, team_b, target_total)
MATCHUPS = [
    ("Cavaliers", "Pistons", 219.5),
    ("Lakers", "Thunder", 216.5),
]

LAST_N_RS = 15


def get(url: str, sleep: float = 3.5) -> str:
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


def parse_team_log(abbr: str) -> tuple[pd.DataFrame, pd.DataFrame, list[str], list[str]]:
    """Return (rs_df, po_df, rs_box_ids, po_box_ids)."""
    html = get(f"https://www.basketball-reference.com/teams/{abbr}/{SEASON_YEAR}_games.html")
    parts = html.split('id="games_playoffs"', 1)
    rs_html = parts[0]
    po_html = parts[1] if len(parts) == 2 else ""
    rs_ids = list(dict.fromkeys(BOX_RE.findall(rs_html)))
    po_ids = list(dict.fromkeys(BOX_RE.findall(po_html)))
    # Map ids to (date, opponent) by extracting from game_log table HTML
    rs_df = _parse_log_table(html, "games")
    po_df = _parse_log_table(html, "games_playoffs")
    return rs_df, po_df, rs_ids, po_ids


def _parse_log_table(html: str, table_id: str) -> pd.DataFrame:
    """Pull (box_id, date, opp_full) from a games table by id."""
    # Extract just the table HTML
    start = html.find(f'id="{table_id}"')
    if start < 0:
        return pd.DataFrame(columns=["box_id", "date", "opp"])
    end = html.find("</table>", start)
    section = html[start:end]
    rows = []
    # Each row has /boxscores/<id>.html and an opponent link /teams/<abbr>/year.html
    # plus a date csk attr like csk="2026-04-18"
    for m in re.finditer(
        r'csk="(\d{4}-\d{2}-\d{2})"[^"]*"[^"]*"[^"]*</a></td>.*?'
        r'/boxscores/(\d{9}[A-Z]{3})\.html.*?'
        r'<a href="/teams/[A-Z]{3}/\d{4}\.html">([^<]+)</a>',
        section, re.DOTALL,
    ):
        rows.append({"date": m.group(1), "box_id": m.group(2), "opp": m.group(3)})
    return pd.DataFrame(rows)


LINE_ROW_RE = re.compile(
    r"<tr ><th scope=\"row\" class=\"center \" data-stat=\"team\" >"
    r"<a href='/teams/([A-Z]{3})/\d{4}\.html'>[A-Z]{3}</a></th>"
    r"<td class=\"center \" data-stat=\"1\" >(\d+)</td>"
    r"<td class=\"center \" data-stat=\"2\" >(\d+)</td>"
    r"<td class=\"center \" data-stat=\"3\" >(\d+)</td>"
    r"<td class=\"center \" data-stat=\"4\" >(\d+)</td>"
    r"<td class=\"center \" data-stat=\"T\" ><strong>(\d+)</strong>"
)


def parse_box(box_id: str) -> dict[str, dict] | None:
    """Return {team_abbr: {q1,q2,q3,q4,total}} or None."""
    try:
        html = get(f"https://www.basketball-reference.com/boxscores/{box_id}.html")
    except Exception as e:
        print(f"  warn: {box_id}: {e}")
        return None
    out = {}
    for m in LINE_ROW_RE.finditer(html):
        out[m.group(1)] = {
            "q1": int(m.group(2)),
            "q2": int(m.group(3)),
            "q3": int(m.group(4)),
            "q4": int(m.group(5)),
            "total": int(m.group(6)),
        }
    return out if len(out) == 2 else None


def find_h2h(rs_df: pd.DataFrame, opp_full: str) -> pd.DataFrame:
    if rs_df.empty:
        return pd.DataFrame()
    return rs_df[rs_df["opp"] == opp_full].sort_values("date")


def quarter_avg(games: list[dict], team_abbr: str) -> dict[str, float]:
    """Average Q1-Q4 + total for a list of parsed games."""
    if not games:
        return {}
    pts = {q: [] for q in ("q1", "q2", "q3", "q4")}
    opps = {q: [] for q in ("q1", "q2", "q3", "q4")}
    totals = []
    for g in games:
        t = g.get(team_abbr)
        if not t:
            continue
        opp = next((v for k, v in g.items() if k != team_abbr), None)
        if not opp:
            continue
        for q in pts:
            pts[q].append(t[q])
            opps[q].append(opp[q])
        totals.append(t["total"] + opp["total"])
    out = {}
    for q in pts:
        out[f"team_{q}"] = sum(pts[q]) / len(pts[q]) if pts[q] else 0
        out[f"opp_{q}"] = sum(opps[q]) / len(opps[q]) if opps[q] else 0
        out[f"total_{q}"] = out[f"team_{q}"] + out[f"opp_{q}"]
    out["final"] = sum(totals) / len(totals) if totals else 0
    out["n"] = len(totals)
    return out


def cashout_thresholds(per_team_avg_a: dict, per_team_avg_b: dict, target: float) -> None:
    """Print on-pace and warning thresholds to hit `target` total."""
    if not per_team_avg_a or not per_team_avg_b:
        print("  (no playoff data — using regular-season fallback)")
        return
    # Combined per-quarter: each team's "team_q" averages summed
    combined = {}
    for q in ("q1", "q2", "q3", "q4"):
        combined[q] = per_team_avg_a[f"team_{q}"] + per_team_avg_b[f"team_{q}"]
    baseline = sum(combined.values())
    ratio = target / baseline if baseline else 1.0

    end_q1 = combined["q1"] * ratio
    end_h = (combined["q1"] + combined["q2"]) * ratio
    end_q3 = (combined["q1"] + combined["q2"] + combined["q3"]) * ratio
    print(f"  Playoff-pace baseline (sum of both teams' R1 quarter averages):")
    print(f"    Q1 {combined['q1']:.1f} | Q2 {combined['q2']:.1f} | Q3 {combined['q3']:.1f} | Q4 {combined['q4']:.1f}  ->  final {baseline:.1f}")
    print(f"    pace ratio to hit {target}: {ratio:.3f}x")
    print()
    print(f"  ON-PACE targets to clear {target}:")
    print(f"    end of Q1:  total >= {end_q1:.0f}")
    print(f"    halftime:   total >= {end_h:.0f}")
    print(f"    end of Q3:  total >= {end_q3:.0f}")
    print()
    print(f"  CASH-OUT WARNINGS (game falling behind {target}):")
    print(f"    Q1 < {end_q1*0.85:.0f}   -> on pace for ~{end_q1*0.85/ratio:.0f} (well short)")
    print(f"    half < {end_h*0.90:.0f}  -> on pace for ~{end_h*0.90/ratio:.0f}")
    print(f"    end Q3 < {end_q3*0.93:.0f} -> on pace for ~{end_q3*0.93/ratio:.0f} (very tough Q4 needed)")


def main() -> None:
    # Build the universe of box scores to fetch: last_15 RS + all PO per team + H2H games
    needed_box_ids: set[str] = set()
    team_data: dict[str, dict] = {}

    for name, (abbr, full) in TEAMS.items():
        print(f"[{abbr}] fetching team game log...")
        rs_df, po_df, rs_ids, po_ids = parse_team_log(abbr)
        team_data[name] = {
            "abbr": abbr,
            "full": full,
            "rs_df": rs_df,
            "po_df": po_df,
            "rs_ids_last15": rs_ids[-LAST_N_RS:],
            "po_ids": po_ids,
        }
        needed_box_ids.update(rs_ids[-LAST_N_RS:])
        needed_box_ids.update(po_ids)

    # Add H2H box ids
    h2h_by_matchup: dict[tuple, list[dict]] = {}
    for a, b, _ in MATCHUPS:
        full_b = TEAMS[b][1]
        h2h_df = find_h2h(team_data[a]["rs_df"], full_b)
        h2h_by_matchup[(a, b)] = h2h_df.to_dict("records")
        for r in h2h_by_matchup[(a, b)]:
            needed_box_ids.add(r["box_id"])

    print(f"\nFetching {len(needed_box_ids)} unique box scores (cached when possible)...")
    parsed: dict[str, dict] = {}
    for i, box_id in enumerate(sorted(needed_box_ids), 1):
        if i % 10 == 0:
            print(f"  {i}/{len(needed_box_ids)} ...")
        parsed[box_id] = parse_box(box_id) or {}

    # Per-team quarter averages
    print("\n" + "=" * 78)
    print("PER-TEAM QUARTER AVERAGES")
    print("=" * 78)
    team_q_stats: dict[str, dict] = {}
    for name, (abbr, _) in TEAMS.items():
        td = team_data[name]
        rs_games = [parsed[bid] for bid in td["rs_ids_last15"] if parsed.get(bid)]
        po_games = [parsed[bid] for bid in td["po_ids"] if parsed.get(bid)]
        rs_avg = quarter_avg(rs_games, abbr)
        po_avg = quarter_avg(po_games, abbr)
        team_q_stats[name] = {"rs": rs_avg, "po": po_avg}
        print(f"\n{name} ({abbr})")
        if rs_avg:
            print(f"  last-{LAST_N_RS} reg ({rs_avg['n']} g):")
            print(f"    Q1 {rs_avg['team_q1']:.1f} (opp {rs_avg['opp_q1']:.1f}, total {rs_avg['total_q1']:.1f}) | "
                  f"Q2 {rs_avg['team_q2']:.1f} (opp {rs_avg['opp_q2']:.1f}, total {rs_avg['total_q2']:.1f})")
            print(f"    Q3 {rs_avg['team_q3']:.1f} (opp {rs_avg['opp_q3']:.1f}, total {rs_avg['total_q3']:.1f}) | "
                  f"Q4 {rs_avg['team_q4']:.1f} (opp {rs_avg['opp_q4']:.1f}, total {rs_avg['total_q4']:.1f})")
            print(f"    final {rs_avg['final']:.1f}")
        if po_avg:
            print(f"  R1 playoff ({po_avg['n']} g):")
            print(f"    Q1 {po_avg['team_q1']:.1f} (opp {po_avg['opp_q1']:.1f}, total {po_avg['total_q1']:.1f}) | "
                  f"Q2 {po_avg['team_q2']:.1f} (opp {po_avg['opp_q2']:.1f}, total {po_avg['total_q2']:.1f})")
            print(f"    Q3 {po_avg['team_q3']:.1f} (opp {po_avg['opp_q3']:.1f}, total {po_avg['total_q3']:.1f}) | "
                  f"Q4 {po_avg['team_q4']:.1f} (opp {po_avg['opp_q4']:.1f}, total {po_avg['total_q4']:.1f})")
            print(f"    final {po_avg['final']:.1f}")

    # Per-matchup analysis
    for a, b, target in MATCHUPS:
        abbr_a, _ = TEAMS[a][:2]
        abbr_b, _ = TEAMS[b][:2]
        print("\n" + "=" * 78)
        print(f"{a} vs {b}  -- target line: OVER {target}")
        print("=" * 78)

        # H2H quarter-by-quarter
        h2h = h2h_by_matchup[(a, b)]
        if h2h:
            print(f"\nHead-to-head this season ({len(h2h)} games):")
            print(f"  {'date':<12} {a[:5]:<5} {'1':>4} {'2':>4} {'3':>4} {'4':>4} = {'T':>4}    {b[:5]:<5} {'1':>4} {'2':>4} {'3':>4} {'4':>4} = {'T':>4}   total")
            qsums = {q: 0.0 for q in ("q1", "q2", "q3", "q4")}
            count = 0
            for g in h2h:
                box = parsed.get(g["box_id"])
                if not box:
                    continue
                ta = box.get(abbr_a)
                tb = box.get(abbr_b)
                if not ta or not tb:
                    continue
                count += 1
                for q in qsums:
                    qsums[q] += ta[q] + tb[q]
                total = ta["total"] + tb["total"]
                d = g["date"]
                print(f"  {d:<12} {abbr_a:<5} {ta['q1']:>4} {ta['q2']:>4} {ta['q3']:>4} {ta['q4']:>4} = {ta['total']:>4}    "
                      f"{abbr_b:<5} {tb['q1']:>4} {tb['q2']:>4} {tb['q3']:>4} {tb['q4']:>4} = {tb['total']:>4}   {total:>5}")
            if count:
                avgs = {q: qsums[q] / count for q in qsums}
                avg_total = sum(avgs.values())
                print()
                print(f"  H2H avg combined: Q1 {avgs['q1']:.1f} | Q2 {avgs['q2']:.1f} | Q3 {avgs['q3']:.1f} | Q4 {avgs['q4']:.1f}  ->  total {avg_total:.1f}")
                print(f"  H2H halftime avg: {avgs['q1'] + avgs['q2']:.1f}    end-Q3 avg: {avgs['q1'] + avgs['q2'] + avgs['q3']:.1f}")

        # Cash-out thresholds — use playoff R1 quarter averages
        print(f"\nCash-out thresholds for OVER {target}:")
        cashout_thresholds(team_q_stats[a]["po"], team_q_stats[b]["po"], target)


if __name__ == "__main__":
    main()

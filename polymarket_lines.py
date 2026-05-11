"""Pull current Polymarket US totals lines for given event slugs.

No authentication required for the league events endpoint.
"""
from __future__ import annotations

import sys
import requests

EVENTS_URL = "https://gateway.polymarket.us/v2/leagues/nba/events"


def fetch_nba_events() -> list[dict]:
    r = requests.get(EVENTS_URL, timeout=30)
    r.raise_for_status()
    return r.json().get("events", [])


def totals_for(event_slug: str) -> list[dict]:
    """Return totals markets for a given event slug, sorted by line."""
    events = fetch_nba_events()
    for e in events:
        if e.get("slug") != event_slug:
            continue
        rows = []
        for m in e.get("markets", []):
            if m.get("sportsMarketType") != "totals":
                continue
            sides = {s.get("description"): s for s in m.get("marketSides", [])}
            over = sides.get("Over") or {}
            under = sides.get("Under") or {}
            rows.append({
                "line": float(m.get("line") or 0),
                "slug": m.get("slug"),
                "over_last": float(over.get("price") or 0),
                "over_ask": float((over.get("quote") or {}).get("value") or 0),
                "under_last": float(under.get("price") or 0),
                "under_ask": float((under.get("quote") or {}).get("value") or 0),
            })
        return sorted(rows, key=lambda r: r["line"])
    return []


def main() -> None:
    targets = sys.argv[1:] or ["nba-cle-det-2026-05-05", "nba-lal-okc-2026-05-05"]
    events = fetch_nba_events()
    by_slug = {e.get("slug"): e for e in events}

    for slug in targets:
        e = by_slug.get(slug)
        if not e:
            print(f"\n!! event not found: {slug}")
            continue
        title = e.get("title") or e.get("name")
        print(f"\n=== {title}  ({slug}) ===")
        print(f"start: {e.get('startDate')}")

        # totals
        totals = [m for m in e.get("markets", []) if m.get("sportsMarketType") == "totals"]
        totals.sort(key=lambda m: float(m.get("line") or 0))
        if totals:
            print()
            print(f"  {'line':>6}  {'BUY OVER':>9}  {'BUY UNDER':>10}  {'over implied':>13}")
            for m in totals:
                line = float(m.get("line") or 0)
                sides = {s.get("description"): s for s in m.get("marketSides", [])}
                o_ask = float(((sides.get("Over") or {}).get("quote") or {}).get("value") or 0)
                u_ask = float(((sides.get("Under") or {}).get("quote") or {}).get("value") or 0)
                # Implied probability: ask is buy price; subtract vig (split equally)
                vig = (o_ask + u_ask) - 1.0
                over_implied = o_ask - vig / 2 if vig > 0 else o_ask
                print(f"  {line:>6}  {o_ask:>9.2f}  {u_ask:>10.2f}  {over_implied*100:>11.1f}%")


if __name__ == "__main__":
    main()

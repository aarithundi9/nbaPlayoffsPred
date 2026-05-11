"""
Polymarket US activity fetcher.

Pulls your trade history (and other activity) from Polymarket US and writes it
to bets.csv. Run periodically; new fills are appended, existing rows are kept
so any prob-estimate annotations you've added are preserved.

Auth: Polymarket US uses Ed25519-signed headers.
  X-PM-Access-Key   = your Key ID
  X-PM-Timestamp    = unix-millis (must be within 30s of server time)
  X-PM-Signature    = base64(Ed25519_sign("{ts}{METHOD}{path}", secret_key))

Set credentials via environment variables OR a local .env file:
  POLYMARKET_KEY_ID=pmk_...
  POLYMARKET_SECRET_KEY=base64-encoded-ed25519-private-key

(Both are issued at https://polymarket.us/developer when you create an API key.)
"""
from __future__ import annotations

import base64
import csv
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives.asymmetric import ed25519

BASE_URL = "https://api.polymarket.us"
ACTIVITIES_PATH = "/v1/portfolio/activities"
BETS_CSV = Path(__file__).parent / "bets.csv"
ENV_FILE = Path(__file__).parent / ".env"

CSV_FIELDS = [
    "trade_id",
    "create_time",
    "market_slug",
    "market_question",
    "my_side",        # Over/Under/Yes/No — pulled from resolution event
    "trade_action",   # BUY/SELL — what this trade was
    "price",
    "qty",
    "cost_basis",
    "realized_pnl_trade",  # P&L from this individual trade (only set when SELLing out)
    # Market resolution (same across all trades that touched this market)
    "result",         # WIN / LOSS / "" if still open
    "market_pnl",     # final realized P&L for the whole market position
    "market_payout",  # cash received at resolution (gross)
    "market_cost",    # total cost basis put into the market
    "resolved_at",
    # Manual annotations (preserved across re-fetches)
    "my_prob_estimate",
    "model_implied_total",
    "notes",
]


def load_env() -> None:
    """Load KEY=VAL pairs from .env into os.environ if present."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def get_creds() -> tuple[str, ed25519.Ed25519PrivateKey]:
    key_id = os.environ.get("POLYMARKET_KEY_ID")
    secret_b64 = os.environ.get("POLYMARKET_SECRET_KEY")
    if not key_id or not secret_b64:
        sys.exit(
            "ERROR: set POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY in env or .env\n"
            f"Expected location: {ENV_FILE}"
        )
    raw = base64.b64decode(secret_b64)
    # Ed25519 private keys are 32 bytes. The doc base64 may include a public-key
    # tail; trim defensively to the first 32 bytes.
    if len(raw) < 32:
        sys.exit(f"ERROR: decoded secret is {len(raw)} bytes; expected >=32")
    priv = ed25519.Ed25519PrivateKey.from_private_bytes(raw[:32])
    return key_id, priv


def auth_headers(method: str, path: str, key_id: str, priv: ed25519.Ed25519PrivateKey) -> dict[str, str]:
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}{method}{path}".encode()
    sig = base64.b64encode(priv.sign(message)).decode()
    return {
        "X-PM-Access-Key": key_id,
        "X-PM-Timestamp": timestamp,
        "X-PM-Signature": sig,
        "Content-Type": "application/json",
    }


def fetch_activities(
    key_id: str,
    priv: ed25519.Ed25519PrivateKey,
    activity_type: str | None = None,
    market_slug: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Page through activities of one type (or all if None) and return all rows."""
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, str | int] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if activity_type:
            params["types"] = activity_type
        if market_slug:
            params["marketSlug"] = market_slug

        headers = auth_headers("GET", ACTIVITIES_PATH, key_id, priv)
        r = requests.get(BASE_URL + ACTIVITIES_PATH, headers=headers, params=params, timeout=30)
        if r.status_code == 401:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            full_path = f"{ACTIVITIES_PATH}?{qs}"
            headers = auth_headers("GET", full_path, key_id, priv)
            r = requests.get(BASE_URL + ACTIVITIES_PATH, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        out.extend(data.get("activities", []))
        cursor = data.get("nextCursor")
        if data.get("eof") or not cursor:
            break
    return out


def flatten_trade(activity: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the fields we care about from a TRADE-type activity. Returns None for non-trades."""
    if activity.get("type") != "ACTIVITY_TYPE_TRADE":
        return None
    t = activity.get("trade") or {}
    if not t.get("id"):
        return None
    price = (t.get("price") or {}).get("value")
    cost_basis = (t.get("costBasis") or {}).get("value")
    pnl = (t.get("realizedPnl") or {}).get("value")
    qty_raw = t.get("qty", "")
    try:
        qty_f = float(qty_raw)
    except (TypeError, ValueError):
        qty_f = None
    action = ""
    if qty_f is not None:
        action = "BUY" if qty_f > 0 else ("SELL" if qty_f < 0 else "")
    return {
        "trade_id": t["id"],
        "create_time": t.get("createTime", ""),
        "market_slug": t.get("marketSlug", ""),
        "trade_action": action,
        "price": price or "",
        "qty": qty_raw or "",
        "cost_basis": cost_basis or "",
        "realized_pnl_trade": pnl or "",
        # Resolution-derived columns — filled later if a resolution event exists for this market
        "market_question": "",
        "my_side": "",
        "result": "",
        "market_pnl": "",
        "market_payout": "",
        "market_cost": "",
        "resolved_at": "",
        "my_prob_estimate": "",
        "model_implied_total": "",
        "notes": "",
    }


def index_resolutions(activities: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Return {market_slug: {result, market_pnl, market_payout, market_cost, my_side, market_question, resolved_at}}.

    Resolutions settle by market, so we key on slug and apply to every trade
    that touched the market.
    """
    import json as _json
    out: dict[str, dict[str, str]] = {}
    for a in activities:
        if a.get("type") != "ACTIVITY_TYPE_POSITION_RESOLUTION":
            continue
        r = a.get("positionResolution") or {}
        market = r.get("market") or {}
        before = r.get("beforePosition") or {}
        after = r.get("afterPosition") or {}
        meta = before.get("marketMetadata") or {}

        slug = r.get("marketSlug") or market.get("slug") or meta.get("slug") or ""
        if not slug:
            continue

        my_side = meta.get("outcome", "")  # "Over" / "Under" / "Yes" / "No"

        market_pnl = (after.get("realized") or {}).get("value") or ""

        # Determine WIN/LOSS from realized P&L sign — robust to outcome-array
        # ordering quirks (Polymarket sometimes returns ["Under","Over"] instead
        # of ["Over","Under"], so positional matching against outcomePrices is
        # unreliable). Realized P&L > 0 means net winnings on this market.
        result = ""
        try:
            pnl_f = float(market_pnl) if market_pnl else 0.0
            if pnl_f > 0.001:
                result = "WIN"
            elif pnl_f < -0.001:
                result = "LOSS"
        except (TypeError, ValueError):
            pass
        # Cash received at expiry = beforePosition.cashValue (it goes to 0 after settlement)
        market_payout = (before.get("cashValue") or {}).get("value") or ""
        market_cost = (before.get("cost") or {}).get("value") or ""

        out[slug] = {
            "market_question": market.get("question") or "",
            "my_side": my_side,
            "result": result,
            "market_pnl": market_pnl,
            "market_payout": market_payout,
            "market_cost": market_cost,
            "resolved_at": r.get("updateTime") or "",
        }
    return out


def load_existing() -> dict[str, dict[str, str]]:
    """Map trade_id -> row from existing bets.csv (preserve user annotations)."""
    if not BETS_CSV.exists():
        return {}
    out: dict[str, dict[str, str]] = {}
    with open(BETS_CSV, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            tid = row.get("trade_id", "")
            if tid:
                out[tid] = row
    return out


def write_csv(rows: list[dict[str, Any]]) -> None:
    rows_sorted = sorted(rows, key=lambda r: r.get("create_time", ""))
    with open(BETS_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for row in rows_sorted:
            w.writerow({k: row.get(k, "") for k in CSV_FIELDS})


def main() -> None:
    load_env()
    key_id, priv = get_creds()

    print(f"[{datetime.now():%H:%M:%S}] fetching trades + resolutions from {BASE_URL}...")
    trades = fetch_activities(key_id, priv, activity_type="ACTIVITY_TYPE_TRADE")
    resolutions_raw = fetch_activities(key_id, priv, activity_type="ACTIVITY_TYPE_POSITION_RESOLUTION")
    activities = trades + resolutions_raw
    print(f"  fetched {len(trades)} trades + {len(resolutions_raw)} resolutions")
    if os.environ.get("PM_DEBUG"):
        import json as _j
        for a in resolutions_raw:
            r = a.get("positionResolution") or {}
            m = r.get("market") or {}
            meta = (r.get("beforePosition") or {}).get("marketMetadata") or {}
            print(f"  RES {m.get('slug')} my_side={meta.get('outcome')} outcomes={m.get('outcomes')} prices={m.get('outcomePrices')} pnl={(r.get('afterPosition') or {}).get('realized', {}).get('value')}")

    new_rows = [r for r in (flatten_trade(a) for a in activities) if r]
    resolutions = index_resolutions(activities)

    existing = load_existing()
    merged: dict[str, dict[str, Any]] = dict(existing)
    added = 0
    for row in new_rows:
        tid = row["trade_id"]
        # Apply resolution by market_slug to every trade that touched it
        slug = row.get("market_slug", "")
        if slug in resolutions:
            row.update(resolutions[slug])
        if tid in merged:
            preserved = {
                k: merged[tid].get(k, "")
                for k in ("my_prob_estimate", "model_implied_total", "notes")
            }
            merged[tid] = {**row, **preserved}
        else:
            merged[tid] = row
            added += 1

    write_csv(list(merged.values()))
    rows = list(merged.values())

    def _f(v: Any) -> float:
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    # One row per market for resolved P&L
    by_market: dict[str, dict[str, Any]] = {}
    for r in rows:
        slug = r.get("market_slug", "")
        if r.get("result") and slug not in by_market:
            by_market[slug] = r

    win_markets = [r for r in by_market.values() if r.get("result") == "WIN"]
    loss_markets = [r for r in by_market.values() if r.get("result") == "LOSS"]
    market_pnl = sum(_f(r.get("market_pnl")) for r in by_market.values())

    # Roll up by market for "closed early vs still open" — a market is "closed
    # early" if its trades sum to a non-zero realized P&L (manual sell-out).
    unresolved_markets: dict[str, float] = {}
    for r in rows:
        if r.get("result"):
            continue
        slug = r.get("market_slug", "")
        unresolved_markets[slug] = unresolved_markets.get(slug, 0.0) + _f(r.get("realized_pnl_trade"))
    closed_early_markets = {s: p for s, p in unresolved_markets.items() if abs(p) > 1e-9}
    still_open_markets = [s for s, p in unresolved_markets.items() if abs(p) <= 1e-9]
    early_pnl = sum(closed_early_markets.values())

    net = market_pnl + early_pnl
    # Note: some resolved markets may also have a partial-close P&L from an
    # earlier sell. Add those too.
    partial_close_pnl = sum(
        _f(r.get("realized_pnl_trade")) for r in rows if r.get("result")
    )
    net += partial_close_pnl

    print(f"  wrote {len(merged)} rows ({added} new) to {BETS_CSV.name}")
    print()
    print(f"  Settled markets: {len(by_market)}  (W {len(win_markets)} / L {len(loss_markets)})  P&L ${market_pnl:+.2f}")
    print(f"  Closed early:    {len(closed_early_markets)} market(s)              P&L ${early_pnl:+.2f}")
    print(f"  Still open:      {len(still_open_markets)} market(s)")
    if abs(partial_close_pnl) > 1e-9:
        print(f"  Partial closes within resolved markets:        P&L ${partial_close_pnl:+.2f}")
    print(f"  TOTAL realized:  ${net:+.2f}")
    unannot = sum(1 for r in rows if not r.get("my_prob_estimate"))
    if unannot:
        print(f"  reminder: {unannot} rows still need my_prob_estimate filled in")


if __name__ == "__main__":
    main()

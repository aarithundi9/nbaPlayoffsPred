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
    "side",          # BUY/SELL inferred from intent / qty sign if available
    "price",
    "qty",
    "cost_basis",
    "realized_pnl",
    "state",
    # Resolution fields (filled when contract auto-settles at market close)
    "resolved",       # YES / NO / "" if not yet resolved
    "resolution_pnl", # net P&L from auto-settlement
    "resolved_at",
    # Annotation columns — you fill these manually
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
    side = ""
    if qty_f is not None:
        side = "BUY" if qty_f > 0 else ("SELL" if qty_f < 0 else "")
    return {
        "trade_id": t["id"],
        "create_time": t.get("createTime", ""),
        "market_slug": t.get("marketSlug", ""),
        "side": side,
        "price": price or "",
        "qty": qty_raw or "",
        "cost_basis": cost_basis or "",
        "realized_pnl": pnl or "",
        "state": t.get("state", ""),
        "resolved": "",
        "resolution_pnl": "",
        "resolved_at": "",
        "my_prob_estimate": "",
        "model_implied_total": "",
        "notes": "",
    }


def index_resolutions(activities: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Return {market_slug: {resolved, resolution_pnl, resolved_at}}.

    POSITION_RESOLUTION activities settle by market, not by trade_id, so we key
    on marketSlug and apply the resolution to every trade that touched that market.
    """
    out: dict[str, dict[str, str]] = {}
    for a in activities:
        if a.get("type") != "ACTIVITY_TYPE_POSITION_RESOLUTION":
            continue
        r = a.get("positionResolution") or {}
        slug = r.get("marketSlug") or ""
        if not slug:
            continue
        outcome = r.get("outcome") or r.get("resolvedOutcome") or ""
        # Common values: "RESOLUTION_OUTCOME_YES" / "_NO" / "_INVALID"
        resolved = ""
        if "YES" in outcome:
            resolved = "YES"
        elif "NO" in outcome:
            resolved = "NO"
        elif outcome:
            resolved = outcome
        pnl = ""
        for k in ("realizedPnl", "pnl", "netPayout"):
            v = (r.get(k) or {})
            if isinstance(v, dict) and v.get("value"):
                pnl = v["value"]
                break
        out[slug] = {
            "resolved": resolved,
            "resolution_pnl": pnl,
            "resolved_at": r.get("resolvedAt") or r.get("createTime") or "",
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
    if os.environ.get("PM_DEBUG") and resolutions_raw:
        import json as _json
        print("--- DEBUG: first raw resolution ---")
        print(_json.dumps(resolutions_raw[0], indent=2, default=str))
        print("--- end debug ---")

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
    n_resolved = sum(1 for r in merged.values() if r.get("resolved"))
    print(f"  wrote {len(merged)} rows ({added} new, {n_resolved} resolved) to {BETS_CSV.name}")
    unannot = sum(1 for r in merged.values() if not r.get("my_prob_estimate"))
    if unannot:
        print(f"  reminder: {unannot} rows still need my_prob_estimate filled in")


if __name__ == "__main__":
    main()

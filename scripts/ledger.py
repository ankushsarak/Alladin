#!/usr/bin/env python3
"""
Alladin forward record.

An append-only ledger of every call Alladin has actually published, settled
later against real prices. The original recommendation is frozen at publish
time and is never rewritten — performance is always measured against what was
said on the day, not against a revised version of it.

Lifecycle of one call:

    PENDING   published, entry zone not yet traded
    NO_FILL   entry never traded within the fill window — excluded from returns
    OPEN      filled, still inside the holding window
    TARGET_1  the real session high reached Target 1
    STOPPED   the real session low breached the stop
    CLOSED    holding window elapsed; exited at the close

Only settled calls (TARGET_1 / STOPPED / CLOSED) count toward the live record.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "docs" / "ledger.json"

FILL_WINDOW = 2      # sessions the entry zone stays valid
HOLD = 5             # sessions held once filled
SETTLED = ("TARGET_1", "STOPPED", "CLOSED")
FINAL = SETTLED + ("NO_FILL",)


def load():
    if LEDGER.exists():
        try:
            return json.loads(LEDGER.read_text())
        except Exception:                                       # noqa: BLE001
            pass
    return {"version": 1, "started": None, "calls": []}


def save(led):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(led, indent=1, ensure_ascii=False))


def record_key(symbol, date):
    return f"{symbol}|{date}"


def publish(led, tips, today, now_iso):
    """
    Append today's calls. Never touches an existing entry — if a symbol was
    already published today, the original stands.
    """
    have = {c["key"] for c in led["calls"]}
    added = 0
    for t in tips:
        key = record_key(t["symbol"], today)
        if key in have:
            continue
        led["calls"].append({
            "key": key,
            "published": today,
            "published_at": now_iso,
            "symbol": t["symbol"],
            "ticker": t["ticker"],
            "name": t["name"],
            "market": t["market"],
            "sector": t["sector"],
            "cur": t["cur"],
            # frozen at publication — never edited afterwards
            "original": {
                "price_at_publish": t["price"],
                "action": t["action"],
                "mode": t["mode"],
                "conviction": t["conviction"],
                "score": t["score"],
                "entry": list(t["entry"]),
                "stop": t["stop"],
                "targets": list(t["targets"]),
                "risk_pct": t["risk_pct"],
                "rr": t["rr"],
                "size": t["size"],
                "window": t["window"],
                "thesis": list(t.get("thesis", []))[:2],
            },
            "result": {
                "status": "PENDING",
                "filled_on": None, "fill_price": None,
                "exit_on": None, "exit_price": None,
                "ret": None, "mfe": None, "mae": None,
                "bars_held": 0,
                "note": None,
                "last_checked": now_iso,
            },
        })
        added += 1
    if added and not led.get("started"):
        led["started"] = today
    return added


def settle(led, rows_for, now_iso):
    """
    Score every unsettled call against real bars published after the call.

    `rows_for(symbol)` returns that symbol's OHLCV rows (or None). Only bars
    strictly after the publication date are ever consulted, so a call can
    never be scored against data that existed when it was made.
    """
    changed = 0
    for c in led["calls"]:
        r = c["result"]
        if r["status"] in FINAL:
            continue

        rows = rows_for(c["symbol"])
        if not rows:
            continue
        fwd = [b for b in rows if b["d"] > c["published"]]
        if not fwd:
            continue

        o = c["original"]
        lo, hi = min(o["entry"]), max(o["entry"])
        stop, t1 = o["stop"], o["targets"][0]

        # --- find the fill
        fill_i, fill_px, fill_on = None, None, None
        for i, b in enumerate(fwd[:FILL_WINDOW]):
            if b["l"] <= hi and b["h"] >= lo:
                # gap through the zone fills at the open, else mid-zone
                fill_px = b["o"] if (b["o"] < lo or b["o"] > hi) else (lo + hi) / 2
                fill_px = min(max(fill_px, b["l"]), b["h"])
                fill_i, fill_on = i, b["d"]
                break

        if fill_i is None:
            if len(fwd) >= FILL_WINDOW:
                r.update(status="NO_FILL", note="Entry zone never traded within "
                         f"{FILL_WINDOW} sessions. Excluded from returns.",
                         last_checked=now_iso)
                changed += 1
            continue

        r.update(filled_on=fill_on, fill_price=round(fill_px, 2))

        # --- walk the holding window on real highs and lows
        held = fwd[fill_i:fill_i + HOLD]
        mfe = mae = 0.0
        status, exit_px, exit_on, note = "OPEN", None, None, None

        for b in held:
            mfe = max(mfe, (b["h"] / fill_px - 1) * 100)
            mae = min(mae, (b["l"] / fill_px - 1) * 100)
            if b["l"] <= stop:
                status, exit_px, exit_on = "STOPPED", stop, b["d"]
                note = "The session low breached the stop."
                break
            if b["h"] >= t1:
                status, exit_px, exit_on = "TARGET_1", t1, b["d"]
                note = "The session high reached Target 1."
                break

        if status == "OPEN" and len(held) >= HOLD:
            status, exit_px, exit_on = "CLOSED", held[-1]["c"], held[-1]["d"]
            note = f"Holding window of {HOLD} sessions elapsed; exited at the close."

        r.update(
            status=status, exit_on=exit_on,
            exit_price=round(exit_px, 2) if exit_px else None,
            ret=round((exit_px / fill_px - 1) * 100, 2) if exit_px else
                round((held[-1]["c"] / fill_px - 1) * 100, 2),
            mfe=round(mfe, 2), mae=round(mae, 2),
            bars_held=len(held), note=note, last_checked=now_iso,
        )
        changed += 1
    return changed


def summarise(led):
    """Live statistics — settled calls only, plus what is still open."""
    calls = led["calls"]
    settled = [c for c in calls if c["result"]["status"] in SETTLED]
    open_ = [c for c in calls if c["result"]["status"] in ("PENDING", "OPEN")]
    nofill = [c for c in calls if c["result"]["status"] == "NO_FILL"]

    def agg(sel):
        rs = [c["result"]["ret"] for c in settled if sel(c) and c["result"]["ret"] is not None]
        if not rs:
            return None
        w = [x for x in rs if x > 0]
        return {"n": len(rs), "win": round(len(w) / len(rs) * 100, 1),
                "avg": round(sum(rs) / len(rs), 2), "sum": round(sum(rs), 2)}

    rets = [c["result"]["ret"] for c in settled if c["result"]["ret"] is not None]

    # equity path, ~4% of capital per call, ordered by exit date
    days, equity = [], 100000.0
    by_exit = {}
    for c in settled:
        if c["result"]["ret"] is None:
            continue
        by_exit.setdefault(c["result"]["exit_on"] or c["published"], []).append(c)
    for d in sorted(by_exit):
        group = by_exit[d]
        day_ret = sum(x["result"]["ret"] for x in group) / len(group) * 0.04
        equity *= 1 + day_ret / 100
        days.append({"date": d, "d": d, "all": round(day_ret, 3),
                     "ind": round(sum(x["result"]["ret"] for x in group
                                      if x["market"] == "INDIA") /
                                  max(1, sum(1 for x in group if x["market"] == "INDIA")), 3)
                     if any(x["market"] == "INDIA" for x in group) else 0.0,
                     "usa": round(sum(x["result"]["ret"] for x in group
                                      if x["market"] == "USA") /
                                  max(1, sum(1 for x in group if x["market"] == "USA")), 3)
                     if any(x["market"] == "USA" for x in group) else 0.0,
                     "eq": round(equity, 2), "n": len(group)})

    outcomes = {k: sum(1 for c in calls if c["result"]["status"] == k)
                for k in ("TARGET_1", "STOPPED", "CLOSED", "NO_FILL", "OPEN", "PENDING")}

    sectors = {}
    for c in settled:
        if c["result"]["ret"] is not None:
            sectors.setdefault(c["sector"], []).append(c["result"]["ret"])

    return {
        "started": led.get("started"),
        "published": len(calls),
        "settled": len(settled),
        "open": len(open_),
        "no_fill": len(nofill),
        "outcomes": outcomes,
        "winRate": round(len([r for r in rets if r > 0]) / len(rets) * 100, 1) if rets else None,
        "avg": round(sum(rets) / len(rets), 2) if rets else None,
        "total": round((equity / 100000 - 1) * 100, 2) if days else None,
        "days": days,
        "markets": {"india": agg(lambda c: c["market"] == "INDIA"),
                    "usa": agg(lambda c: c["market"] == "USA")},
        "conviction": {"high": agg(lambda c: c["original"]["conviction"] == "HIGH"),
                       "medium": agg(lambda c: c["original"]["conviction"] == "MEDIUM")},
        "sectors": sorted([[k, round(sum(v) / len(v), 2)] for k, v in sectors.items()],
                          key=lambda x: -x[1]),
        "best": max(settled, key=lambda c: c["result"]["ret"] or -99)["ticker"] if settled else None,
        "worst": min(settled, key=lambda c: c["result"]["ret"] or 99)["ticker"] if settled else None,
        "fill_window": FILL_WINDOW,
        "hold": HOLD,
        "method": (
            f"Every call Alladin publishes is written to an append-only ledger and never edited "
            f"afterwards. A call fills only if price actually traded into the published entry zone "
            f"within {FILL_WINDOW} sessions; otherwise it is marked no-fill and excluded from returns. "
            f"Once filled it is held up to {HOLD} sessions and exits on the real session low breaching "
            f"the stop, the real session high reaching Target 1, or the close at the end of the window. "
            f"Returns are measured from the fill against the originally published levels. Costs, "
            f"slippage and taxes are not modelled."
        ),
    }


def recent(led, n=12):
    """Most recent calls, newest first — for the post-market view."""
    out = sorted(led["calls"], key=lambda c: (c["published"], c["ticker"]), reverse=True)[:n]
    return [{
        "key": c["key"], "ticker": c["ticker"], "name": c["name"],
        "market": c["market"], "sector": c["sector"], "cur": c["cur"],
        "published": c["published"],
        "original": c["original"],
        "result": c["result"],
    } for c in out]

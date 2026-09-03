#!/usr/bin/env python3
"""
Alladin data builder.

Pulls real market data and news from free public sources, computes technical
indicators from actual OHLCV, scores setups with a deterministic rule engine,
backtests those same rules on real history, and writes docs/data.json.

Sources
  Prices / OHLCV : Yahoo Finance chart API (public, no key)
  News           : Google News RSS (public, no key)

Standard library only, so CI needs no pip install.
"""

import http.cookiejar
import json
import math
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data.json"
CACHE = ROOT / ".cache"
CACHE_TTL = int(os.environ.get("ALLADIN_CACHE_TTL", 21600))   # 6h; 0 disables

# Deliberately generic. A full Chrome UA makes Yahoo's chart API answer 429,
# because a real browser would be carrying consent cookies and a crumb.
UA = "Mozilla/5.0"
IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------- universe

INDIA = [
    ("RELIANCE.NS", "Reliance Industries", "Energy"),
    ("HDFCBANK.NS", "HDFC Bank", "Financials"),
    ("ICICIBANK.NS", "ICICI Bank", "Financials"),
    ("INFY.NS", "Infosys", "Technology"),
    ("TCS.NS", "Tata Consultancy Services", "Technology"),
    ("LT.NS", "Larsen & Toubro", "Industrials"),
    ("SUNPHARMA.NS", "Sun Pharmaceutical", "Healthcare"),
    ("KOTAKBANK.NS", "Kotak Mahindra Bank", "Financials"),
    ("HINDUNILVR.NS", "Hindustan Unilever", "Consumer"),
    ("ITC.NS", "ITC Limited", "Consumer"),
    ("BHARTIARTL.NS", "Bharti Airtel", "Communication"),
    ("AXISBANK.NS", "Axis Bank", "Financials"),
    ("MARUTI.NS", "Maruti Suzuki", "Consumer"),
    ("TITAN.NS", "Titan Company", "Consumer"),
]

USA = [
    ("NVDA", "NVIDIA Corp", "Technology"),
    ("MSFT", "Microsoft", "Technology"),
    ("AAPL", "Apple", "Technology"),
    ("GOOGL", "Alphabet", "Communication"),
    ("AMZN", "Amazon", "Consumer"),
    ("META", "Meta Platforms", "Communication"),
    ("JPM", "JPMorgan Chase", "Financials"),
    ("XOM", "Exxon Mobil", "Energy"),
    ("UNH", "UnitedHealth Group", "Healthcare"),
    ("CAT", "Caterpillar", "Industrials"),
    ("AMD", "Advanced Micro Devices", "Technology"),
    ("V", "Visa", "Financials"),
]

INDICES = [
    ("^NSEI", "NIFTY 50", "INDIA"),
    ("^NSEBANK", "BANK NIFTY", "INDIA"),
    ("^GSPC", "S&P 500", "USA"),
    ("^IXIC", "NASDAQ", "USA"),
]

SECTOR_ART = {
    "Technology": "tech",
    "Energy": "energy",
    "Financials": "finance",
    "Healthcare": "health",
    "Consumer": "tech",
    "Industrials": "finance",
    "Communication": "tech",
}


# ---------------------------------------------------------------- fetching

MIN_INTERVAL = float(os.environ.get("ALLADIN_MIN_INTERVAL", 2.5))
_last_call = [0.0]


def _pace():
    """Yahoo tolerates a steady trickle but blocks bursts. Space every call."""
    gap = time.time() - _last_call[0]
    if gap < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - gap)
    _last_call[0] = time.time()


def http_get(url, tries=2, timeout=25):
    """
    Paced, stateless GET. Deliberately carries NO cookies — Yahoo's consent
    cookies make its chart API answer 429 — and never retry-storms, because
    retrying into a 429 is what keeps the IP throttled.
    """
    ctx = ssl.create_default_context()
    last = None
    for attempt in range(tries):
        _pace()
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429 and attempt + 1 < tries:
                time.sleep(20)                 # cool off properly, once
                continue
            break
        except Exception as e:                                  # noqa: BLE001
            last = e
            if attempt + 1 < tries:
                time.sleep(3)
    print(f"  ! fetch failed {url.split('?')[0]}: {last}", file=sys.stderr)
    return None


def cache_path(key):
    CACHE.mkdir(exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", key)
    return CACHE / f"{safe}.json"


def fetch_ohlcv(symbol, rng="1y", interval="1d"):
    """
    Real OHLCV from Yahoo Finance. Tries both API hosts, falls back to a
    local cache so a transient rate-limit never blanks the site.
    """
    ck = cache_path(f"{symbol}_{rng}_{interval}")
    if CACHE_TTL and ck.exists() and (time.time() - ck.stat().st_mtime) < CACHE_TTL:
        try:
            return json.loads(ck.read_text())
        except Exception:                                       # noqa: BLE001
            pass

    raw = None
    for host in ("query1", "query2"):
        url = (
            f"https://{host}.finance.yahoo.com/v8/finance/chart/"
            f"{urllib.parse.quote(symbol)}?range={rng}&interval={interval}"
        )
        raw = http_get(url)
        if raw:
            break

    if not raw:
        if ck.exists():                       # stale cache beats no data
            try:
                print(f"  ~ {symbol}: using cached data", file=sys.stderr)
                return json.loads(ck.read_text())
            except Exception:                                   # noqa: BLE001
                pass
        return None
    try:
        data = json.loads(raw)
        res = data["chart"]["result"][0]
        meta = res["meta"]
        ts = res.get("timestamp") or []
        q = res["indicators"]["quote"][0]

        rows = []
        for i, t in enumerate(ts):
            o, h, l, c, v = (q.get(k, [None] * len(ts))[i] for k in
                             ("open", "high", "low", "close", "volume"))
            if None in (o, h, l, c):
                continue
            rows.append({
                "t": t,
                "d": datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"),
                "o": float(o), "h": float(h), "l": float(l), "c": float(c),
                "v": float(v or 0),
            })
        if len(rows) < 60:
            return None
        out = {
            "symbol": symbol,
            "currency": meta.get("currency", "USD"),
            "price": float(meta.get("regularMarketPrice") or rows[-1]["c"]),
            "chg": float(meta.get("regularMarketChangePercent") or 0.0),
            "prevClose": float(meta.get("chartPreviousClose") or rows[-2]["c"]),
            "exchange": meta.get("fullExchangeName", ""),
            "rows": rows,
        }
        try:
            ck.write_text(json.dumps(out))
        except Exception:                                       # noqa: BLE001
            pass
        return out
    except Exception as e:                                      # noqa: BLE001
        print(f"  ! parse failed {symbol}: {e}", file=sys.stderr)
        return None


def fetch_news(query, region="US", limit=3):
    """Real headlines from Google News RSS."""
    hl, gl, ceid = ("en-IN", "IN", "IN:en") if region == "IN" else ("en-US", "US", "US:en")
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(query)
           + f"&hl={hl}&gl={gl}&ceid={ceid}")
    raw = http_get(url)
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []

    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        src_el = item.find("source")
        source = (src_el.text or "").strip() if src_el is not None else "Google News"

        # Google News titles are "Headline - Publisher"; split the publisher off.
        if source and title.endswith(" - " + source):
            title = title[: -(len(source) + 3)].strip()

        when = ""
        try:
            dt = datetime.strptime(pub[:25].strip(), "%a, %d %b %Y %H:%M:%S")
            delta = datetime.now(timezone.utc).replace(tzinfo=None) - dt
            hrs = delta.total_seconds() / 3600
            when = ("just now" if hrs < 1 else
                    f"{int(hrs)}h ago" if hrs < 24 else
                    f"{int(hrs // 24)}d ago")
        except Exception:                                       # noqa: BLE001
            when = pub[:16]

        if title:
            out.append({"h": title, "src": source, "time": when, "url": link})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- indicators

def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(-n, 0):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100 - (100 / (1 + rs))


def ema_series(vals, n):
    if len(vals) < n:
        return []
    k = 2 / (n + 1)
    out = [sum(vals[:n]) / n]
    for v in vals[n:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def macd(closes):
    """Return (macd_line, signal, histogram) from real closes."""
    if len(closes) < 35:
        return None, None, None
    e12, e26 = ema_series(closes, 12), ema_series(closes, 26)
    n = min(len(e12), len(e26))
    line = [e12[-n + i] - e26[-n + i] for i in range(n)]
    if len(line) < 9:
        return line[-1], None, None
    sig = ema_series(line, 9)
    return line[-1], sig[-1], line[-1] - sig[-1]


def atr(rows, n=14):
    if len(rows) < n + 1:
        return None
    trs = []
    for i in range(-n, 0):
        h, l, pc = rows[i]["h"], rows[i]["l"], rows[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / n


def swing_levels(rows, lookback=60):
    """Real support/resistance from actual swing highs and lows."""
    window = rows[-lookback:] if len(rows) >= lookback else rows
    price = rows[-1]["c"]
    highs = sorted({round(r["h"], 2) for r in window if r["h"] > price})
    lows = sorted({round(r["l"], 2) for r in window if r["l"] < price}, reverse=True)
    resistance = highs[0] if highs else price * 1.03
    support = lows[0] if lows else price * 0.97
    # nearest meaningful pivot: max high / min low of the recent window
    recent = rows[-20:]
    return {
        "resistance": max(resistance, max(r["h"] for r in recent)) if highs else resistance,
        "support": min(support, min(r["l"] for r in recent)) if lows else support,
        "swing_low": min(r["l"] for r in rows[-20:]),
        "swing_high": max(r["h"] for r in rows[-20:]),
        "hi52": max(r["h"] for r in rows),
        "lo52": min(r["l"] for r in rows),
    }


def indicators(rows):
    """All indicators computed from real OHLCV. No synthesis."""
    closes = [r["c"] for r in rows]
    vols = [r["v"] for r in rows]
    price = closes[-1]

    m_line, m_sig, m_hist = macd(closes)
    a = atr(rows)
    lv = swing_levels(rows)

    v20 = sma(vols, 20) or 0
    v5 = sma(vols, 5) or 0

    return {
        "price": price,
        "sma20": sma(closes, 20),
        "sma50": sma(closes, 50),
        "sma200": sma(closes, 200),
        "rsi": rsi(closes),
        "macd": m_line,
        "macd_signal": m_sig,
        "macd_hist": m_hist,
        "atr": a,
        "atr_pct": (a / price * 100) if a else None,
        "vol_ratio": (v5 / v20) if v20 else None,
        "chg5": (price / closes[-6] - 1) * 100 if len(closes) > 6 else None,
        "chg20": (price / closes[-21] - 1) * 100 if len(closes) > 21 else None,
        **lv,
    }


# ---------------------------------------------------------------- rule engine

def score_setup(ind):
    """
    Deterministic score (0-100) from real indicators, with the reasons that
    produced it. No randomness, no model — auditable arithmetic.
    """
    price = ind["price"]
    s, reasons, against = 0, [], []

    # Trend structure — 30
    if ind["sma20"] and ind["sma50"]:
        if price > ind["sma20"] > ind["sma50"]:
            s += 30
            reasons.append(("Trend", "Price is above both the 20- and 50-day averages, which are stacked bullishly."))
        elif price > ind["sma50"]:
            s += 18
            reasons.append(("Trend", "Price holds above the 50-day average, though the short-term average has not confirmed."))
        elif price > ind["sma20"]:
            s += 10
            reasons.append(("Trend", "Price has reclaimed the 20-day average but remains under the 50-day."))
        else:
            against.append("Price trades below both the 20- and 50-day averages.")
    if ind["sma200"] and price > ind["sma200"]:
        s += 6
        reasons.append(("Structure", "The long-term 200-day trend is still up."))
    elif ind["sma200"]:
        against.append("Price is below its 200-day average — the primary trend is not supportive.")

    # Momentum — 26
    r = ind["rsi"]
    if r is not None:
        if 52 <= r <= 68:
            s += 16
            reasons.append(("Momentum", f"RSI at {r:.0f} — strength with room left before exhaustion."))
        elif 45 <= r < 52:
            s += 9
            reasons.append(("Momentum", f"RSI at {r:.0f} — momentum is recovering from neutral."))
        elif r > 72:
            s += 4
            against.append(f"RSI at {r:.0f} is overbought; entries here carry pullback risk.")
        else:
            against.append(f"RSI at {r:.0f} shows weak momentum.")
    if ind["macd_hist"] is not None:
        if ind["macd_hist"] > 0:
            s += 10
            reasons.append(("Momentum", "MACD sits above its signal line — the momentum cross is positive."))
        else:
            against.append("MACD remains below its signal line.")

    # Participation — 18
    vr = ind["vol_ratio"]
    if vr is not None:
        if vr >= 1.25:
            s += 18
            reasons.append(("Volume", f"Recent volume is running {vr:.2f}× the 20-day average — participation is confirming."))
        elif vr >= 1.0:
            s += 11
            reasons.append(("Volume", f"Volume is {vr:.2f}× its 20-day average — steady participation."))
        elif vr >= 0.8:
            s += 5
        else:
            against.append(f"Volume is only {vr:.2f}× average — conviction behind the move is thin.")

    # Entry location — 20
    if ind["sma20"]:
        ext = (price / ind["sma20"] - 1) * 100
        if -1.5 <= ext <= 3.0:
            s += 20
            reasons.append(("Location", f"Price sits {ext:+.1f}% from its 20-day average — a reasonable entry zone."))
        elif 3.0 < ext <= 6.0:
            s += 10
            against.append(f"Price is {ext:.1f}% extended above its 20-day average.")
        elif ext > 6.0:
            against.append(f"Price is {ext:.1f}% above its 20-day average — chasing here is poor location.")
        else:
            s += 6
            against.append(f"Price is {ext:.1f}% below its 20-day average — the trend is still repairing.")

    return max(0, min(100, s)), reasons, against


def build_levels(ind, score):
    """Entry / stop / targets derived from real ATR and real swing levels."""
    price, a = ind["price"], ind["atr"] or ind["price"] * 0.02
    sma20, res, swing_low = ind["sma20"], ind["resistance"], ind["swing_low"]
    ext = ((price / sma20 - 1) * 100) if sma20 else 0

    if ext > 4.5 and sma20:
        mode = "WAIT"
        entry = (round(sma20 * 0.995, 2), round(sma20 * 1.005, 2))
        window = "On a pullback to the 20-day average"
    elif res and price < res <= price * 1.025:
        mode = "BUY ON BREAKOUT"
        entry = (round(res * 1.001, 2), round(res * 1.008, 2))
        window = f"On a close above {res:,.2f}"
    else:
        mode = "BUY NOW"
        entry = (round(price * 0.997, 2), round(price * 1.005, 2))
        window = "First 60–90 minutes of the session"

    mid = (entry[0] + entry[1]) / 2
    stop = min(swing_low * 0.998, mid - 1.6 * a)
    if (mid - stop) / mid > 0.09:                # cap risk at a sane distance
        stop = mid * 0.93
    risk = mid - stop
    t1, t2, t3 = mid + risk * 1.3, mid + risk * 2.2, mid + risk * 3.4

    return {
        "mode": mode, "window": window,
        "entry": [round(entry[0], 2), round(entry[1], 2)],
        "stop": round(stop, 2),
        "targets": [round(t1, 2), round(t2, 2), round(t3, 2)],
        "risk_pct": round(risk / mid * 100, 2),
        "rr": round((t2 - mid) / risk, 1),
    }


def position_size(risk_pct, conviction):
    """Fixed-fractional sizing: risk ~1% of portfolio, scaled by conviction."""
    budget = {"HIGH": 1.0, "MEDIUM": 0.7, "LOW": 0.4}[conviction]
    if risk_pct <= 0:
        return 0.0
    return round(min(5.0, max(0.5, budget / (risk_pct / 100))), 1)


def conviction_of(score):
    return "HIGH" if score >= 72 else "MEDIUM" if score >= 58 else "LOW"


def stars(score, part):
    """Per-dimension 1-5 rating from the same real indicators."""
    return max(1, min(5, int(round(part / 100 * 5)))) if part else 1


# ---------------------------------------------------------------- backtest

def backtest(universe_data, hold=5, min_score=58):
    """
    Replay the SAME rule engine over real history. For each session we score
    using only bars up to that day, then measure the real forward return over
    the next `hold` sessions, honouring stop and first target on real highs
    and lows. Nothing here is simulated — every price is an actual close.
    """
    per_day, trades = {}, []

    for sym, meta in universe_data.items():
        rows = meta["rows"]
        if len(rows) < 220:
            continue
        for i in range(200, len(rows) - hold):
            hist = rows[: i + 1]
            ind = indicators(hist)
            if not ind["sma50"] or not ind["atr"]:
                continue
            score, _, _ = score_setup(ind)
            if score < min_score:
                continue

            lv = build_levels(ind, score)
            if lv["mode"] == "WAIT":
                continue
            entry = (lv["entry"][0] + lv["entry"][1]) / 2
            fwd = rows[i + 1: i + 1 + hold]
            if not fwd:
                continue

            # Fill only if the entry zone actually traded.
            if not any(b["l"] <= lv["entry"][1] and b["h"] >= lv["entry"][0] for b in fwd[:2]):
                continue

            ret, outcome = None, "CLOSED"
            for b in fwd:
                if b["l"] <= lv["stop"]:
                    ret, outcome = (lv["stop"] / entry - 1) * 100, "STOPPED"
                    break
                if b["h"] >= lv["targets"][0]:
                    ret, outcome = (lv["targets"][0] / entry - 1) * 100, "TARGET HIT"
                    break
            if ret is None:
                ret = (fwd[-1]["c"] / entry - 1) * 100

            d = rows[i]["d"]
            trades.append({
                "d": d, "sym": sym, "market": meta["market"], "sector": meta["sector"],
                "ret": ret, "outcome": outcome, "conviction": conviction_of(score),
            })
            per_day.setdefault(d, []).append((meta["market"], ret))

    if not trades:
        return None

    # Daily equity: mean return of that day's calls, capped to a sane exposure.
    days = []
    equity = 100000.0
    for d in sorted(per_day):
        picks = per_day[d]
        ind_r = [r for m, r in picks if m == "INDIA"]
        usa_r = [r for m, r in picks if m == "USA"]
        allr = sum(r for _, r in picks) / len(picks)
        # position-weighted: assume ~4% of capital per call
        day_ret = allr * 0.04
        equity *= 1 + day_ret / 100
        days.append({
            "date": d,
            "ind": round(sum(ind_r) / len(ind_r), 3) if ind_r else 0.0,
            "usa": round(sum(usa_r) / len(usa_r), 3) if usa_r else 0.0,
            "all": round(day_ret, 3),
            "eq": round(equity, 2),
            "n": len(picks),
        })

    def agg(sel):
        rs = [t["ret"] for t in trades if sel(t)]
        if not rs:
            return None
        wins = [r for r in rs if r > 0]
        return {
            "tips": len(rs),
            "win": round(len(wins) / len(rs) * 100, 1),
            "avg": round(sum(rs) / len(rs), 2),
            "ret": round(sum(rs) / len(rs) * len(rs) * 0.04, 2),
        }

    peak, mdd = 100000.0, 0.0
    for d in days:
        peak = max(peak, d["eq"])
        mdd = min(mdd, (d["eq"] / peak - 1) * 100)

    total = (days[-1]["eq"] / 100000 - 1) * 100 if days else 0.0
    n = len(days)
    ann = ((days[-1]["eq"] / 100000) ** (252 / n) - 1) * 100 if n > 20 else None

    all_r = [t["ret"] for t in trades]
    wins = [r for r in all_r if r > 0]
    best = max(trades, key=lambda t: t["ret"])
    worst = min(trades, key=lambda t: t["ret"])

    sectors = {}
    for t in trades:
        sectors.setdefault(t["sector"], []).append(t["ret"])
    sector_rows = sorted(
        [[k, round(sum(v) / len(v) * len(v) * 0.04, 2)] for k, v in sectors.items()],
        key=lambda x: -x[1])[:6]

    return {
        "days": days,
        "total": round(total, 2),
        "annualized": round(ann, 1) if ann else None,
        "maxdd": round(mdd, 1),
        "winRate": round(len(wins) / len(all_r) * 100, 1),
        "tips": len(all_r),
        "avg": round(sum(all_r) / len(all_r), 2),
        "rr": None,
        "markets": {
            "india": agg(lambda t: t["market"] == "INDIA"),
            "usa": agg(lambda t: t["market"] == "USA"),
        },
        "sectors": sector_rows,
        "conviction": {
            "high": agg(lambda t: t["conviction"] == "HIGH"),
            "medium": agg(lambda t: t["conviction"] == "MEDIUM"),
        },
        "best": {"sym": best["sym"], "ret": round(best["ret"], 2), "d": best["d"]},
        "worst": {"sym": worst["sym"], "ret": round(worst["ret"], 2), "d": worst["d"]},
        "assumptions": (
            f"Backtest, not a live track record. The same rule engine was replayed over "
            f"{len(days)} sessions of real Yahoo Finance history across a {len(universe_data)}-name "
            f"universe. A setup counts only if price actually traded into the entry zone within two "
            f"sessions; it exits on the real low breaching the stop, the real high reaching Target 1, "
            f"or the close after {hold} sessions. Sizing assumes ~4% of capital per call. No costs, "
            f"slippage, taxes or borrowing are modelled, and the universe is fixed today, so it carries "
            f"survivorship bias. Past behaviour does not predict future results."
        ),
        "hold": hold,
        "outcomes": {
            k: sum(1 for t in trades if t["outcome"] == k)
            for k in ("TARGET HIT", "STOPPED", "CLOSED")
        },
    }


# ---------------------------------------------------------------- tips

def make_thesis(ind, reasons):
    """Editorial lines built strictly from the computed indicators."""
    out = []
    by = {k: v for k, v in reasons}
    if "Trend" in by:
        out.append(by["Trend"])
    if "Momentum" in by:
        out.append(by["Momentum"])
    if "Volume" in by:
        out.append(by["Volume"])
    if "Location" in by:
        out.append(by["Location"])
    if "Structure" in by and len(out) < 4:
        out.append(by["Structure"])
    return out[:4]


def sections(ind, lv, name, sector):
    p = ind["price"]
    def f(x):
        return f"{x:,.2f}"

    chart = (
        f"Last price {f(p)}. The 20-day average sits at {f(ind['sma20'])} and the 50-day at "
        f"{f(ind['sma50'])}, putting price {((p/ind['sma20']-1)*100):+.1f}% from its short-term mean. "
        f"RSI(14) reads {ind['rsi']:.0f} and the MACD histogram is {ind['macd_hist']:+.2f}. "
        f"Nearest resistance from the last 60 sessions is {f(ind['resistance'])}; the 20-session swing low "
        f"is {f(ind['swing_low'])}. Average true range is {f(ind['atr'])} "
        f"({ind['atr_pct']:.1f}% of price), which is what sets the stop distance below."
    ) if ind["sma20"] and ind["sma50"] else "Insufficient history for a full technical read."

    business = (
        f"{name} sits in the {sector.lower()} sector. Alladin's engine scores price behaviour, not company "
        f"filings — fundamentals are not modelled here, so treat this as a technical read and pair it with "
        f"your own view of the business."
    )

    macro = (
        f"Over the last 20 sessions the stock has moved {ind['chg20']:+.1f}% against its own trend, and "
        f"{ind['chg5']:+.1f}% over the last five. Its 52-week range is {f(ind['lo52'])} to {f(ind['hi52'])}, "
        f"so price is currently {((p - ind['lo52'])/(ind['hi52']-ind['lo52'])*100):.0f}% of the way up that range."
    ) if ind["chg20"] is not None else "Range data unavailable."

    catalyst = (
        "Alladin does not model scheduled events. Check the headlines below and the company's own calendar "
        "before entering — an earnings date inside the holding window changes the risk materially."
    )

    risks = (
        f"The setup fails on a close below {f(lv['stop'])}, which is {lv['risk_pct']:.1f}% under the entry "
        f"midpoint. That level is placed beneath the recent swing low and roughly 1.6 ATR away, so normal "
        f"volatility should not trigger it — but a gap through it will fill worse than the stop price."
    )

    signal = (
        f"Composite score reflects trend, momentum, volume and entry location computed from real OHLCV. "
        f"The plan below is arithmetic on those levels: stop from swing low and ATR, targets at 1.3, 2.2 and "
        f"3.4 times the risk distance."
    )

    return {
        "THE CHART": chart,
        "THE BUSINESS": business,
        "THE MACRO": macro,
        "THE CATALYST": catalyst,
        "THE RISKS": risks,
        "THE SIGNAL": signal,
    }


def build_tip(sym, meta, ind, score, reasons, against, idx):
    lv = build_levels(ind, score)
    conv = conviction_of(score)
    cur = "₹" if meta["market"] == "INDIA" else "$"
    ticker = sym.replace(".NS", "")

    news = fetch_news(f"{meta['name']} stock", "IN" if meta["market"] == "INDIA" else "US")
    for n in news:
        n["why"] = ("Context only — Alladin's score is computed from price and volume, "
                    "not from this headline. Read it before you act.")

    trend_p = 100 if ind["sma20"] and ind["sma50"] and ind["price"] > ind["sma20"] > ind["sma50"] else 55
    mom_p = min(100, (ind["rsi"] or 50) * 1.3) if ind["macd_hist"] and ind["macd_hist"] > 0 else 45
    vol_p = min(100, (ind["vol_ratio"] or 0.8) * 65)
    loc_p = max(20, 100 - abs((ind["price"] / ind["sma20"] - 1) * 100) * 16) if ind["sma20"] else 50

    return {
        "id": ticker.lower(),
        "ticker": ticker,
        "name": meta["name"],
        "market": meta["market"],
        "sector": meta["sector"],
        "art": SECTOR_ART.get(meta["sector"], "tech"),
        "cur": cur,
        "action": "BUY",
        "mode": lv["mode"],
        "conviction": conv,
        "score": score,
        "price": round(ind["price"], 2),
        "chg": round(meta["chg"], 2),
        "entry": lv["entry"],
        "window": lv["window"],
        "stop": lv["stop"],
        "targets": lv["targets"],
        "rr": f"1 : {lv['rr']}",
        "risk_pct": lv["risk_pct"],
        "size": position_size(lv["risk_pct"], conv),
        "scores": {
            "Technical": stars(score, trend_p),
            "Momentum": stars(score, mom_p),
            "Volume": stars(score, vol_p),
            "Location": stars(score, loc_p),
        },
        "indicators": {
            "rsi": round(ind["rsi"], 1) if ind["rsi"] else None,
            "sma20": round(ind["sma20"], 2) if ind["sma20"] else None,
            "sma50": round(ind["sma50"], 2) if ind["sma50"] else None,
            "sma200": round(ind["sma200"], 2) if ind["sma200"] else None,
            "macd_hist": round(ind["macd_hist"], 3) if ind["macd_hist"] else None,
            "atr": round(ind["atr"], 2) if ind["atr"] else None,
            "vol_ratio": round(ind["vol_ratio"], 2) if ind["vol_ratio"] else None,
            "resistance": round(ind["resistance"], 2),
            "support": round(ind["support"], 2),
            "hi52": round(ind["hi52"], 2),
            "lo52": round(ind["lo52"], 2),
        },
        "thesis": make_thesis(ind, reasons),
        "sections": sections(ind, lv, meta["name"], meta["sector"]),
        "news": news,
        "workcase": [r[1] for r in reasons][:4] or ["Score met the threshold on trend and momentum."],
        "failcase": against[:4] or ["No disqualifying signal, but no setup is certain."],
        "invalidation": f"A daily close below {cur}{lv['stop']:,.2f}.",
        "chart": [],   # filled by caller
    }


# ---------------------------------------------------------------- main

def main():
    now_ist = datetime.now(IST)
    print(f"Alladin data build — {now_ist:%Y-%m-%d %H:%M} IST")

    # ---- indices
    print("Fetching indices…")
    indices = []
    for sym, label, market in INDICES:
        d = fetch_ohlcv(sym, rng="3mo")
        if not d:
            continue
        closes = [r["c"] for r in d["rows"]][-40:]
        indices.append({
            "symbol": sym, "label": label, "market": market,
            "price": round(d["price"], 2), "chg": round(d["chg"], 2),
            "spark": [round(c, 2) for c in closes],
        })
        print(f"  {label}: {d['price']:,.2f} ({d['chg']:+.2f}%)")
        pass

    # ---- universe
    universe = {}
    for market, lst in (("INDIA", INDIA), ("USA", USA)):
        print(f"Fetching {market} universe…")
        for sym, name, sector in lst:
            d = fetch_ohlcv(sym, rng="2y")
            if not d:
                continue
            universe[sym] = {
                "name": name, "sector": sector, "market": market,
                "rows": d["rows"], "price": d["price"], "chg": d["chg"],
                "currency": d["currency"],
            }
            print(f"  {sym}: {d['price']:,.2f} ({d['chg']:+.2f}%) · {len(d['rows'])} bars")
            pass

    if not universe:
        print("No market data retrieved — aborting.", file=sys.stderr)
        return 1

    # ---- score everything
    print("Scoring setups from real indicators…")
    scored = []
    for sym, meta in universe.items():
        ind = indicators(meta["rows"])
        if not ind["sma50"] or not ind["atr"]:
            continue
        score, reasons, against = score_setup(ind)
        scored.append((score, sym, meta, ind, reasons, against))
        print(f"  {sym:14s} score {score:3d}  rsi {ind['rsi']:.0f}  vol {ind['vol_ratio'] or 0:.2f}x")

    MIN = 58
    tips = {"india": [], "usa": []}
    for market, key in (("INDIA", "india"), ("USA", "usa")):
        picks = sorted([s for s in scored if s[2]["market"] == market],
                       key=lambda x: -x[0])[:3]
        for i, (score, sym, meta, ind, reasons, against) in enumerate(picks):
            if score < MIN:
                continue                      # honour "no trade" over forcing three
            tip = build_tip(sym, meta, ind, score, reasons, against, i)
            tip["chart"] = [{"d": r["d"], "c": round(r["c"], 2)}
                            for r in meta["rows"][-70:]]
            tips[key].append(tip)
            pass
        print(f"  {market}: {len(tips[key])} qualifying setup(s)")

    # ---- watchlist: everything scored, ranked
    watch = []
    for score, sym, meta, ind, _, _ in sorted(scored, key=lambda x: -x[0])[:8]:
        watch.append({
            "ticker": sym.replace(".NS", ""),
            "name": meta["name"],
            "cur": "₹" if meta["market"] == "INDIA" else "$",
            "px": round(ind["price"], 2),
            "chg": round(meta["chg"], 2),
            "score": round(score / 10, 1),
        })

    # ---- backtest the same rules on real history
    print("Backtesting the rule engine on real history…")
    bt = backtest(universe)
    if bt:
        print(f"  {bt['tips']} historical setups · win {bt['winRate']}% · total {bt['total']:+.2f}%")

    payload = {
        "generated": now_ist.isoformat(),
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "sources": {
            "prices": "Yahoo Finance chart API (public)",
            "news": "Google News RSS (public)",
            "engine": "Deterministic rule engine over computed indicators — no LLM, no random data",
        },
        "indices": indices,
        "tips": tips,
        "watchlist": watch,
        "performance": bt,
        "universe_size": len(universe),
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    print(f"Wrote {OUT} ({OUT.stat().st_size/1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

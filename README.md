# Alladin

**Your AI-powered market intelligence.**

Live: **https://ankushsarak.github.io/Alladin/**

A market intelligence dashboard for Indian and US equities. Every number on the
site comes from real public market data. There is no placeholder, sample, or
generated data anywhere in the app.

---

## Where the data comes from

| What | Source | Key needed |
|---|---|---|
| Prices, OHLCV, 52-week ranges | Yahoo Finance chart API | No |
| Headlines | Google News RSS | No |
| Indicators, scores, levels | Computed locally from the OHLCV above | — |

Google Finance has no public API — it exists only as a Google Sheets function —
so Yahoo Finance is used as the free equivalent.

## What is *not* here

**No LLM is involved.** Setups are chosen by a deterministic rule engine you can
read in [`scripts/build_data.py`](scripts/build_data.py) and verify by hand. The
same input always produces the same output. Nothing is inferred, predicted, or
written by a model.

## How a tip is produced

1. Fetch two years of daily OHLCV for each name in the universe (23 large caps
   across NSE and US markets).
2. Compute, from those bars only: SMA 20/50/200, RSI(14), MACD(12,26,9), ATR(14),
   5-day vs 20-day volume ratio, 20-session swing high/low, 60-session
   support/resistance, and the 52-week range.
3. Score 0–100 across four dimensions — trend structure (36), momentum (26),
   participation (18), entry location (20).
4. Below 58, there is no tip. Alladin publishes fewer than three ideas, or none,
   rather than forcing a list.
5. Derive levels arithmetically: entry from the setup type, stop below the swing
   low and ~1.6 ATR back, targets at 1.3× / 2.2× / 3.4× the risk distance.
6. Size the position so a stop-out costs roughly 1% of the portfolio.
7. Attach real headlines for the name from Google News.

## The performance page is a backtest

It is labelled as one throughout. The same rule engine is replayed over real
history: a setup counts only if price actually traded into the entry zone within
two sessions, and it exits on the real low breaching the stop, the real high
reaching Target 1, or the close after five sessions.

It does **not** model costs, slippage, taxes or spread, and the universe is
fixed as of today, so it carries survivorship bias. At the time of writing the
backtest is roughly break-even — that number is published exactly as computed,
not tuned to look good.

## Running it

```bash
python3 scripts/build_data.py
```

Writes `docs/data.json`. Then serve the folder:

```bash
python3 -m http.server 8000
```

Standard library only — no dependencies to install.

Two environment variables are available: `ALLADIN_CACHE_TTL` (seconds, `0`
disables the local response cache) and `ALLADIN_MIN_INTERVAL` (seconds between
requests; Yahoo blocks bursts, so keep this at 2 or more).

## Automation

[`.github/workflows/update-data.yml`](.github/workflows/update-data.yml) rebuilds
the feed on a schedule and commits `docs/data.json`; GitHub Pages redeploys on
push.

| IST | UTC cron | Purpose |
|---|---|---|
| 07:30 | `0 2 * * 1-5` | India pre-market briefing |
| 13:30 | `0 8 * * 1-5` | US pre-market briefing |
| 16:00 | `30 10 * * 1-5` | India post-market |
| 02:30 | `0 21 * * 1-5` | US post-market |

## Layout

```
index.html                     the whole front end — no build step
scripts/build_data.py          fetch, compute, score, backtest
docs/data.json                 generated feed the page reads
.github/workflows/             scheduled rebuild
```

## Disclaimer

Research and education only. Not investment advice, not a solicitation to trade.
A mechanical rule engine over public price data can and does get things wrong.
Markets carry risk and past behaviour does not predict future results. Verify
every number against your broker before acting on it.

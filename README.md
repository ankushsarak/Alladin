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

## Who decides what

The split is deliberate, and enforced by what each side is given.

| | Decided by |
|---|---|
| Which names qualify | Deterministic rule engine |
| Entry, stop, targets, position size | Deterministic rule engine |
| What the numbers mean | The LLM ([`scripts/analyst.py`](scripts/analyst.py)) |
| Whether the news backs the chart | The LLM |
| The strongest objection to the trade | The LLM |

The model is never asked for a price and never picks a stock, so it cannot
hallucinate a level into a trade plan — the levels are passed to it labelled as
fixed, and a test asserts they survive the round trip unchanged. Every number is
arithmetic you can reproduce from
[`scripts/build_data.py`](scripts/build_data.py); the same input always gives the
same output.

The analyst layer is optional and works with any of several LLM keys — **use a
free one**; there is no paid dependency. It needs no SDK (standard-library HTTP)
and is checked in this order:

| Env var | Provider | Cost | Get a key |
|---|---|---|---|
| `GEMINI_API_KEY` | Google Gemini | **Free tier** | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| `GROQ_API_KEY` | Groq (Llama 3.3) | **Free tier** | [console.groq.com/keys](https://console.groq.com/keys) |
| `OPENROUTER_API_KEY` | OpenRouter (free models) | **Free tier** | [openrouter.ai/keys](https://openrouter.ai/keys) |
| `ANTHROPIC_API_KEY` | Claude | Paid | [console.anthropic.com](https://console.anthropic.com) |

Set one as a repository secret (Settings → Secrets and variables → Actions) for
CI, or export it in your shell locally. Force a provider with
`ALLADIN_PROVIDER=gemini|groq|openrouter|anthropic` and override the model with
`ALLADIN_MODEL`. Without any key the build falls back to prose computed from the
indicators, and the site says which one you are reading.

## The forward record

[`docs/ledger.json`](docs/ledger.json) is an append-only log of every call
Alladin has actually published. The original recommendation is frozen at publish
time and never edited — performance is always measured against what was said on
the day.

Each call is settled against real prices by [`scripts/ledger.py`](scripts/ledger.py):

| Status | Meaning |
|---|---|
| `PENDING` | Published; entry zone has not traded yet |
| `NO_FILL` | Entry never traded within 2 sessions — **excluded from returns** |
| `OPEN` | Filled, still inside the 5-session window |
| `TARGET_1` | A real session high reached Target 1 |
| `STOPPED` | A real session low breached the stop |
| `CLOSED` | Window elapsed; exited at the close |

Only settled calls count toward the live win rate. A call that never filled is
not quietly counted as a win, and nothing is backfilled — the record starts on
the day tracking began and grows one session at a time.

This is the number worth trusting. The backtest below is the weaker evidence.

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
scripts/analyst.py             LLM writes the analysis (optional, free-tier)
scripts/ledger.py              append-only forward record + settlement
docs/data.json                 generated feed the page reads
docs/ledger.json               every call ever published, frozen
.github/workflows/             scheduled rebuild
```

## Disclaimer

Research and education only. Not investment advice, not a solicitation to trade.
A mechanical rule engine over public price data can and does get things wrong.
Markets carry risk and past behaviour does not predict future results. Verify
every number against your broker before acting on it.

#!/usr/bin/env python3
"""
Alladin's reasoning layer.

Claude reads the numbers the rule engine already computed — real indicators
from real OHLCV, plus real headlines — and writes the analysis around them.

The division of labour is deliberate and enforced by what this module is given:

  The rule engine decides    which names qualify, and every price level
                             (entry, stop, targets, size). Deterministic,
                             auditable, reproducible.

  Claude decides             what the numbers mean, whether the headlines
                             corroborate or contradict the technical read,
                             what would invalidate the setup, and where the
                             mechanical case is weak.

Claude is never asked for a price and never chooses a stock, so it cannot
hallucinate a level into the trade plan. If no API credentials are present the
build falls back to the computed prose and the site says so.
"""

import json
import os
import sys

MODEL = os.environ.get("ALLADIN_MODEL", "claude-opus-5")

SYSTEM = """You are the analyst voice of Alladin, a market intelligence product for Indian and US equities.

You are given indicators computed from real daily OHLCV, the levels a deterministic rule engine has already fixed, and real news headlines. Your job is to explain and to challenge — never to invent.

Hard rules:
- Never state a price, level, or target that was not given to you. Never revise the entry, stop, targets, or position size. They are fixed.
- Never predict what a stock will do. Describe what the evidence shows and what would falsify it.
- Never imply certainty or probability. No "will", no "should rally", no percentages of likelihood.
- If the headlines do not clearly bear on the setup, say so plainly rather than manufacturing a connection. Saying "the news is noise here" is a valid and valuable answer.
- The engine reads price and volume only. It is blind to earnings dates, guidance, litigation and regulation. Name that blindness when it matters.
- You may conclude the setup is weak. Alladin publishes the score whatever it is, and an honest bear case is more useful than a balanced-sounding one.

Voice: confident, concise, editorial. Short declarative sentences. No hedging filler, no hype, no exclamation marks, no emoji. Write for a reader who understands markets and resents being sold to."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["thesis", "chart", "macro", "catalyst", "risks", "signal",
                 "news_read", "workcase", "failcase", "invalidation", "critique"],
    "properties": {
        "thesis": {
            "type": "array", "minItems": 3, "maxItems": 4,
            "items": {"type": "string"},
            "description": "Short editorial statements, one sentence each, each grounded in a specific given number.",
        },
        "chart": {"type": "string",
                  "description": "What the technical picture shows, citing the given indicator values."},
        "macro": {"type": "string",
                  "description": "Where the stock sits in its own range and trend. No macroeconomic forecasting."},
        "catalyst": {"type": "string",
                     "description": "What in the headlines could move this, or an explicit statement that nothing dated is visible."},
        "risks": {"type": "string",
                  "description": "What breaks this setup, including what the price-only engine cannot see."},
        "signal": {"type": "string",
                   "description": "What the composite score is actually measuring here, and its limits."},
        "news_read": {
            "type": "object", "additionalProperties": False,
            "required": ["verdict", "detail"],
            "properties": {
                "verdict": {"type": "string",
                            "enum": ["CORROBORATES", "CONTRADICTS", "MIXED", "IRRELEVANT", "NO NEWS"]},
                "detail": {"type": "string"},
            },
            "description": "Whether the real headlines support the technical read.",
        },
        "workcase": {"type": "array", "minItems": 2, "maxItems": 4,
                     "items": {"type": "string"}},
        "failcase": {"type": "array", "minItems": 2, "maxItems": 4,
                     "items": {"type": "string"}},
        "invalidation": {"type": "string",
                         "description": "The observable condition that kills the thesis. May reference the given stop."},
        "critique": {"type": "string",
                     "description": "The strongest honest objection to taking this setup at all."},
    },
}


def _payload(t):
    """Exactly what Claude is allowed to see. Levels are labelled as fixed."""
    ind = t.get("indicators", {})
    return {
        "ticker": t["ticker"],
        "company": t["name"],
        "market": t["market"],
        "sector": t["sector"],
        "currency": t["cur"],
        "last_price": t["price"],
        "change_pct_today": t["chg"],
        "computed_indicators": {
            "rsi_14": ind.get("rsi"),
            "sma_20": ind.get("sma20"),
            "sma_50": ind.get("sma50"),
            "sma_200": ind.get("sma200"),
            "macd_histogram": ind.get("macd_hist"),
            "atr_14": ind.get("atr"),
            "volume_5d_vs_20d": ind.get("vol_ratio"),
            "nearest_resistance_60d": ind.get("resistance"),
            "nearest_support_60d": ind.get("support"),
            "high_52w": ind.get("hi52"),
            "low_52w": ind.get("lo52"),
        },
        "engine_output_FIXED_do_not_change": {
            "composite_score_0_100": t["score"],
            "conviction": t["conviction"],
            "action": t["mode"],
            "entry_zone": t["entry"],
            "stop": t["stop"],
            "targets": t["targets"],
            "risk_pct_to_stop": t["risk_pct"],
            "reward_to_risk_at_t2": t["rr"],
            "position_size_pct": t["size"],
            "entry_window": t["window"],
        },
        "engine_reasons_for": t.get("workcase", []),
        "engine_reasons_against": t.get("failcase", []),
        "headlines": [{"headline": n["h"], "source": n["src"], "age": n["time"]}
                      for n in t.get("news", [])],
    }


def analyse(tips, verbose=True):
    """
    Enrich each tip in place with Claude's analysis.

    Returns a dict describing what happened, which the site displays so a
    reader always knows which words came from a model and which are arithmetic.
    """
    if not tips:
        return {"enabled": False, "reason": "No qualifying setups to analyse."}

    have_creds = bool(os.environ.get("ANTHROPIC_API_KEY")
                      or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    try:
        import anthropic
    except ImportError:
        msg = "The anthropic SDK is not installed; showing computed analysis instead."
        if verbose:
            print(f"  ~ analyst skipped: {msg}", file=sys.stderr)
        return {"enabled": False, "reason": msg}

    try:
        client = anthropic.Anthropic()
    except Exception as e:                                      # noqa: BLE001
        msg = f"No Anthropic credentials available ({e.__class__.__name__}); showing computed analysis instead."
        if verbose:
            print(f"  ~ analyst skipped: {msg}", file=sys.stderr)
        return {"enabled": False, "reason": msg}

    if not have_creds:
        # The SDK also reads an `ant auth login` profile; let it try, but say so.
        if verbose:
            print("  ~ no ANTHROPIC_API_KEY; relying on a stored profile if present",
                  file=sys.stderr)

    done, failed = 0, []
    for t in tips:
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=8000,
                system=SYSTEM,
                thinking={"type": "adaptive"},
                output_config={
                    "effort": "high",
                    "format": {"type": "json_schema", "schema": SCHEMA},
                },
                messages=[{
                    "role": "user",
                    "content": (
                        "Analyse this setup. Every number below is already computed from real "
                        "market data — treat the engine output as fixed and unchangeable.\n\n"
                        + json.dumps(_payload(t), indent=1, ensure_ascii=False)
                    ),
                }],
            )

            if resp.stop_reason == "refusal":
                failed.append(f"{t['ticker']}: declined")
                continue

            text = "".join(b.text for b in resp.content if b.type == "text")
            a = json.loads(text)

            # Overwrite the computed prose with the analyst's, keeping every level.
            # Lengths are guidance in the schema descriptions, not enforced by it,
            # so trim here rather than trusting the response to fit the layout.
            def clip(s, n):
                s = " ".join(str(s).split())
                return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0] + "…"

            t["thesis"] = [clip(x, 150) for x in a["thesis"][:4]]
            t["sections"]["THE CHART"] = clip(a["chart"], 950)
            t["sections"]["THE MACRO"] = clip(a["macro"], 750)
            t["sections"]["THE CATALYST"] = clip(a["catalyst"], 750)
            t["sections"]["THE RISKS"] = clip(a["risks"], 850)
            t["sections"]["THE SIGNAL"] = clip(a["signal"], 750)
            t["workcase"] = [clip(x, 200) for x in a["workcase"][:4]]
            t["failcase"] = [clip(x, 200) for x in a["failcase"][:4]]
            t["invalidation"] = clip(a["invalidation"], 230)
            t["critique"] = clip(a["critique"], 520)
            t["news_read"] = {"verdict": a["news_read"]["verdict"],
                              "detail": clip(a["news_read"]["detail"], 620)}
            t["analyst"] = MODEL
            done += 1
            if verbose:
                print(f"  ✓ {t['ticker']} analysed ({a['news_read']['verdict'].lower()})")

        except Exception as e:                                  # noqa: BLE001
            name = e.__class__.__name__
            failed.append(f"{t['ticker']}: {name}")
            if verbose:
                print(f"  ! {t['ticker']} analysis failed: {e}", file=sys.stderr)
            # Auth and permission problems apply to every call — stop rather
            # than firing one doomed request per setup.
            if isinstance(e, (getattr(anthropic, "AuthenticationError", ()),
                              getattr(anthropic, "PermissionDeniedError", ()))) \
                    or name in ("AuthenticationError", "PermissionDeniedError", "TypeError"):
                return {"enabled": False,
                        "reason": ("No usable Anthropic credentials; showing computed analysis "
                                   "instead. Set ANTHROPIC_API_KEY to enable the analyst."),
                        "errors": failed}

    if not done:
        return {"enabled": False,
                "reason": "Every analysis attempt failed; showing computed analysis instead.",
                "errors": failed}

    return {
        "enabled": True,
        "model": MODEL,
        "analysed": done,
        "failed": failed,
        "scope": ("Claude wrote the thesis, the analysis sections, the bull and bear cases, "
                  "the news read and the critique. It never chose a stock and never set a "
                  "price — every level on this page is arithmetic from the rule engine."),
    }

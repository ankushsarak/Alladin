#!/usr/bin/env python3
"""
Alladin's reasoning layer — provider-agnostic, free-tier friendly.

A language model reads the numbers the rule engine already computed — real
indicators from real OHLCV, plus real headlines — and writes the analysis
around them. It never chooses a stock and is never asked for a price, so it
cannot put a hallucinated level into the trade plan.

Works with whichever free key is present, checked in this order:

    GEMINI_API_KEY      Google Gemini      free tier — aistudio.google.com/apikey
    GROQ_API_KEY        Groq (Llama)       free tier — console.groq.com/keys
    OPENROUTER_API_KEY  OpenRouter         free models — openrouter.ai/keys
    ANTHROPIC_API_KEY   Claude             paid

Force one with ALLADIN_PROVIDER=gemini|groq|openrouter|anthropic and override
the model with ALLADIN_MODEL. Standard library only — no SDK, no pip install.
If no key is present the build falls back to computed prose and the site says so.
"""

import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

# Sensible free defaults per provider; override with ALLADIN_MODEL.
DEFAULT_MODEL = {
    "gemini": "gemini-2.0-flash",
    "groq": "llama-3.3-70b-versatile",
    "openrouter": "meta-llama/llama-3.3-70b-instruct:free",
    "anthropic": "claude-opus-5",
}
DISPLAY = {
    "gemini": "Google Gemini",
    "groq": "Groq (Llama 3.3)",
    "openrouter": "OpenRouter",
    "anthropic": "Claude",
}

SYSTEM = """You are the analyst voice of Alladin, a market intelligence product for Indian and US equities.

You are given indicators computed from real daily OHLCV, the levels a deterministic rule engine has already fixed, and real news headlines. Your job is to explain and to challenge — never to invent.

Hard rules:
- Never state a price, level, or target that was not given to you. Never revise the entry, stop, targets, or position size. They are fixed.
- Never predict what a stock will do. Describe what the evidence shows and what would falsify it.
- Never imply certainty or probability. No "will", no "should rally", no percentages of likelihood.
- If the headlines do not clearly bear on the setup, say so plainly rather than manufacturing a connection. "The news is noise here" is a valid and valuable answer.
- The engine reads price and volume only. It is blind to earnings dates, guidance, litigation and regulation. Name that blindness when it matters.
- You may conclude the setup is weak. Alladin publishes the score whatever it is, and an honest bear case is more useful than a balanced-sounding one.

Voice: confident, concise, editorial. Short declarative sentences. No hedging filler, no hype, no exclamation marks, no emoji. Write for a reader who understands markets and resents being sold to."""

# The shape we ask for. Enforced by prompt + defensive parsing, not by the API,
# so it works identically across providers whose JSON modes differ.
SHAPE = {
    "thesis": ["3-4 one-sentence statements, each grounded in a specific given number"],
    "chart": "what the technical picture shows, citing the given indicator values",
    "macro": "where the stock sits in its own range and trend; no macro forecasting",
    "catalyst": "what in the headlines could move this, or a plain statement that nothing dated is visible",
    "risks": "what breaks this setup, including what the price-only engine cannot see",
    "signal": "what the composite score is actually measuring here, and its limits",
    "news_read": {
        "verdict": "one of CORROBORATES | CONTRADICTS | MIXED | IRRELEVANT | NO NEWS",
        "detail": "whether the real headlines support the technical read",
    },
    "workcase": ["2-4 short bull points"],
    "failcase": ["2-4 short bear points"],
    "invalidation": "the observable condition that kills the thesis; may reference the given stop",
    "critique": "the single strongest honest objection to taking this setup at all",
}
VERDICTS = {"CORROBORATES", "CONTRADICTS", "MIXED", "IRRELEVANT", "NO NEWS"}


def _payload(t):
    """Exactly what the model is allowed to see. Levels are labelled as fixed."""
    ind = t.get("indicators", {})
    return {
        "ticker": t["ticker"], "company": t["name"], "market": t["market"],
        "sector": t["sector"], "currency": t["cur"], "last_price": t["price"],
        "change_pct_today": t["chg"],
        "computed_indicators": {
            "rsi_14": ind.get("rsi"), "sma_20": ind.get("sma20"),
            "sma_50": ind.get("sma50"), "sma_200": ind.get("sma200"),
            "macd_histogram": ind.get("macd_hist"), "atr_14": ind.get("atr"),
            "volume_5d_vs_20d": ind.get("vol_ratio"),
            "nearest_resistance_60d": ind.get("resistance"),
            "nearest_support_60d": ind.get("support"),
            "high_52w": ind.get("hi52"), "low_52w": ind.get("lo52"),
        },
        "engine_output_FIXED_do_not_change": {
            "composite_score_0_100": t["score"], "conviction": t["conviction"],
            "action": t["mode"], "entry_zone": t["entry"], "stop": t["stop"],
            "targets": t["targets"], "risk_pct_to_stop": t["risk_pct"],
            "reward_to_risk_at_t2": t["rr"], "position_size_pct": t["size"],
            "entry_window": t["window"],
        },
        "engine_reasons_for": t.get("workcase", []),
        "engine_reasons_against": t.get("failcase", []),
        "headlines": [{"headline": n["h"], "source": n["src"], "age": n["time"]}
                      for n in t.get("news", [])],
    }


def _prompt(t):
    return (
        "Analyse this setup. Every number below is already computed from real market data — "
        "treat the engine output as fixed and unchangeable.\n\n"
        + json.dumps(_payload(t), indent=1, ensure_ascii=False)
        + "\n\nRespond with ONLY a JSON object of exactly this shape (values are instructions, "
        "replace them with your analysis):\n"
        + json.dumps(SHAPE, indent=1, ensure_ascii=False)
        + "\n\nNo markdown, no code fences, no text outside the JSON object."
    )


# ---------------------------------------------------------------- transport

_CTX = ssl.create_default_context()


def _post(url, headers, body, tries=3):
    data = json.dumps(body).encode("utf-8")
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data=data, method="POST",
                                         headers={"Content-Type": "application/json", **headers})
            with urllib.request.urlopen(req, timeout=90, context=_CTX) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503, 529) and attempt + 1 < tries:
                time.sleep(3 * (attempt + 1))
                continue
            try:
                last = f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}"
            except Exception:                                   # noqa: BLE001
                pass
            break
        except Exception as e:                                  # noqa: BLE001
            last = e
            if attempt + 1 < tries:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(str(last))


def _call_gemini(model, key, system, user):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4,
                             "maxOutputTokens": 2048},
    }
    d = _post(url, {"x-goog-api-key": key}, body)
    return d["candidates"][0]["content"]["parts"][0]["text"]


def _call_openai_compatible(base, model, key, system, user, extra_headers=None):
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "temperature": 0.4, "max_tokens": 2048,
    }
    d = _post(base, {"Authorization": f"Bearer {key}", **(extra_headers or {})}, body)
    return d["choices"][0]["message"]["content"]


def _call_anthropic(model, key, system, user):
    body = {
        "model": model, "max_tokens": 2048, "system": system,
        "messages": [{"role": "user", "content": user
                      + "\n\nReturn only the JSON object, nothing else."}],
    }
    d = _post("https://api.anthropic.com/v1/messages",
              {"x-api-key": key, "anthropic-version": "2023-06-01"}, body)
    return "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")


def _dispatch(provider, model, key, system, user):
    if provider == "gemini":
        return _call_gemini(model, key, system, user)
    if provider == "groq":
        return _call_openai_compatible(
            "https://api.groq.com/openai/v1/chat/completions", model, key, system, user)
    if provider == "openrouter":
        return _call_openai_compatible(
            "https://openrouter.ai/api/v1/chat/completions", model, key, system, user,
            {"HTTP-Referer": "https://ankushsarak.github.io/Alladin/", "X-Title": "Alladin"})
    if provider == "anthropic":
        return _call_anthropic(model, key, system, user)
    raise ValueError(provider)


# ---------------------------------------------------------------- parsing

def _extract(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    i, j = text.find("{"), text.rfind("}")
    if i == -1 or j == -1:
        raise ValueError("no JSON object in response")
    return json.loads(text[i:j + 1])


def _clip(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0] + "…"


def _apply(t, a, provider, model):
    """Overwrite the computed prose, keeping every level. Validates required keys."""
    for k in ("thesis", "chart", "macro", "catalyst", "risks", "signal",
              "news_read", "workcase", "failcase", "invalidation", "critique"):
        if k not in a:
            raise ValueError(f"missing key: {k}")

    thesis = [_clip(x, 150) for x in a["thesis"] if str(x).strip()][:4]
    work = [_clip(x, 200) for x in a["workcase"] if str(x).strip()][:4]
    fail = [_clip(x, 200) for x in a["failcase"] if str(x).strip()][:4]
    if len(thesis) < 2 or len(work) < 1 or len(fail) < 1:
        raise ValueError("too few list items")

    nr = a["news_read"]
    verdict = str(nr.get("verdict", "")).upper().strip()
    if verdict not in VERDICTS:
        verdict = "MIXED"

    t["thesis"] = thesis
    t["sections"]["THE CHART"] = _clip(a["chart"], 950)
    t["sections"]["THE MACRO"] = _clip(a["macro"], 750)
    t["sections"]["THE CATALYST"] = _clip(a["catalyst"], 750)
    t["sections"]["THE RISKS"] = _clip(a["risks"], 850)
    t["sections"]["THE SIGNAL"] = _clip(a["signal"], 750)
    t["workcase"] = work
    t["failcase"] = fail
    t["invalidation"] = _clip(a["invalidation"], 230)
    t["critique"] = _clip(a["critique"], 520)
    t["news_read"] = {"verdict": verdict, "detail": _clip(nr.get("detail", ""), 620)}
    t["analyst"] = DISPLAY.get(provider, provider)


# ---------------------------------------------------------------- entry point

def _select():
    """Return (provider, key) for the first available, honouring ALLADIN_PROVIDER."""
    keys = {
        "gemini": os.environ.get("GEMINI_API_KEY"),
        "groq": os.environ.get("GROQ_API_KEY"),
        "openrouter": os.environ.get("OPENROUTER_API_KEY"),
        "anthropic": os.environ.get("ANTHROPIC_API_KEY"),
    }
    forced = os.environ.get("ALLADIN_PROVIDER", "").strip().lower()
    if forced:
        return (forced, keys.get(forced)) if keys.get(forced) else (forced, None)
    for p in ("gemini", "groq", "openrouter", "anthropic"):
        if keys[p]:
            return p, keys[p]
    return None, None


def analyse(tips, verbose=True):
    """Enrich each tip in place. Returns a status dict the site displays."""
    if not tips:
        return {"enabled": False, "reason": "No qualifying setups to analyse."}

    provider, key = _select()
    if not provider or not key:
        msg = ("No free LLM key set; showing computed analysis instead. "
               "Set GEMINI_API_KEY (aistudio.google.com/apikey) or GROQ_API_KEY "
               "(console.groq.com/keys) to enable the analyst.")
        if verbose:
            print(f"  ~ analyst skipped: {msg}", file=sys.stderr)
        return {"enabled": False, "reason": msg}

    model = os.environ.get("ALLADIN_MODEL") or DEFAULT_MODEL.get(provider, "")
    if verbose:
        print(f"  using {DISPLAY.get(provider, provider)} · {model}")

    done, failed = 0, []
    for t in tips:
        try:
            raw = _dispatch(provider, model, key, SYSTEM, _prompt(t))
            _apply(t, _extract(raw), provider, model)
            done += 1
            if verbose:
                print(f"  ✓ {t['ticker']} ({t['news_read']['verdict'].lower()})")
        except Exception as e:                                  # noqa: BLE001
            failed.append(f"{t['ticker']}: {e.__class__.__name__}")
            if verbose:
                print(f"  ! {t['ticker']} failed: {e}", file=sys.stderr)
            # Auth / bad-model errors apply to every call — stop early.
            s = str(e).lower()
            if any(w in s for w in ("401", "403", "invalid api key", "api key not valid",
                                    "permission", "unauthor", "not found", "404")):
                return {"enabled": bool(done), "model": DISPLAY.get(provider, provider),
                        "provider": provider, "analysed": done, "failed": failed,
                        "reason": ("The LLM key was rejected; showing computed analysis for the "
                                   "rest. Check the key and its free-tier quota."),
                        "scope": _SCOPE.format(m=DISPLAY.get(provider, provider))}

    if not done:
        return {"enabled": False,
                "reason": "Every analysis attempt failed; showing computed analysis instead.",
                "errors": failed}

    return {"enabled": True, "model": DISPLAY.get(provider, provider),
            "provider": provider, "analysed": done, "failed": failed,
            "scope": _SCOPE.format(m=DISPLAY.get(provider, provider))}


_SCOPE = ("{m} wrote the thesis, the analysis sections, the bull and bear cases, the news read "
          "and the critique. It never chose a stock and never set a price — every level on this "
          "page is arithmetic from the rule engine.")

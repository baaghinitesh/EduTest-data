"""
gemini_client.py — 3 Gemini keys, correct secret names, full load-balancing.

GitHub Secret names (exact — verified from repo settings):
  GEMINI_API_KEY    → Flash K1  — primary researcher + gen last-resort fallback
  GEMINI_API_KEY_1  → Flash K2  — secondary researcher + gen fallback  (underscore before 1)
  GEMINI_API_KEY2   → Pro first, then Flash — PRIMARY question generator (no underscore before 2)

Key assignment per task:
  ┌─────────────────┬──────────────────────────────────────────────────────────┐
  │ Task            │ Key order (first available, first success wins)          │
  ├─────────────────┼──────────────────────────────────────────────────────────┤
  │ Research        │ K1 Flash → K2 Flash → empty string (never crashes)      │
  │ Generate        │ K3 Pro → K3 Flash → K1 Flash → K2 Flash → raise         │
  │ Structure(fall) │ K1 Flash → K2 Flash → raise                             │
  └─────────────────┴──────────────────────────────────────────────────────────┘

Free-tier daily budget (4 runs):
  Flash K1  : ~4-8 research + fallback gen calls  (1500 RPD limit) ✅
  Flash K2  : ~4-8 research + fallback gen calls  (1500 RPD limit) ✅
  Pro   K3  : ~4-12 generation calls              (25 RPD limit)   ✅
  Flash K3  : fallback only if Pro K3 quota hit   (1500 RPD limit) ✅
"""
import os, json, re, time
from google import genai
from google.genai import types

MODEL_PRO   = "gemini-2.5-pro"
MODEL_FLASH = "gemini-2.5-flash"

# Conservative RPM (official limit - 20% safety buffer)
RPM_PRO   = 4    # 5 RPM official → 4 safe
RPM_FLASH = 8    # 10 RPM official → 8 safe
MAX_RETRIES = 3

# ── Exact GitHub secret names ─────────────────────────────────────────────────
KEY_K1 = "GEMINI_API_KEY"    # Flash K1 — researcher + gen last-resort
KEY_K2 = "GEMINI_API_KEY_1"  # Flash K2 — researcher fallback + gen fallback (underscore_1)
KEY_K3 = "GEMINI_API_KEY2"   # Pro/Flash — primary generator (no underscore, just 2)

_clients   = {}
_last_call = {}   # tracker_key → float timestamp


class GeminiError(Exception):
    pass


class GeminiQuotaError(GeminiError):
    pass


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_client(key_env):
    if key_env not in _clients:
        api_key = os.environ.get(key_env, "").strip()
        if not api_key:
            raise GeminiError(f"Secret '{key_env}' not set or empty")
        _clients[key_env] = genai.Client(api_key=api_key)
    return _clients[key_env]


def _rate_limit(tracker, rpm):
    min_gap = 60.0 / rpm + 0.5
    elapsed = time.time() - _last_call.get(tracker, 0.0)
    if elapsed < min_gap:
        wait = min_gap - elapsed
        print(f"  [RateLimit/{tracker}] waiting {wait:.1f}s...")
        time.sleep(wait)
    _last_call[tracker] = time.time()


def _extract_json(text):
    """Robustly extract JSON array or object from model output."""
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for start, end in [("[", "]"), ("{", "}")]:
        idx = text.find(start)
        if idx == -1:
            continue
        depth = 0
        for i, ch in enumerate(text[idx:], idx):
            if ch == start:
                depth += 1
            elif ch == end:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[idx : i + 1])
                    except json.JSONDecodeError:
                        break
    raise GeminiError("No valid JSON found in Gemini response")


def _call(model, tracker, key_env, rpm, prompt, expect_json, max_tokens):
    """
    Call one key/model with retry.
    Raises GeminiQuotaError on 429/RESOURCE_EXHAUSTED.
    Raises GeminiError on other permanent failures.
    """
    client   = _get_client(key_env)
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _rate_limit(tracker, rpm)
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.7,
                    max_output_tokens=max_tokens,
                ),
            )
            text = (resp.text or "").strip()
            if not text:
                raise GeminiError("Empty response from model")
            return text if not expect_json else _extract_json(text)

        except GeminiError:
            raise
        except Exception as e:
            last_err = e
            s = str(e)
            print(f"  [Gemini/{tracker}] attempt {attempt}/{MAX_RETRIES}: {s[:120]}")
            if any(x in s for x in ["429", "RESOURCE_EXHAUSTED", "quota"]):
                raise GeminiQuotaError(f"Quota hit on {tracker} ({key_env})")
            if any(x in s for x in ["503", "UNAVAILABLE", "overloaded"]):
                wait = 25 * attempt
                print(f"  [Gemini/{tracker}] server busy — sleeping {wait}s")
                time.sleep(wait)
                continue
            if attempt < MAX_RETRIES:
                time.sleep(6 * attempt)
    raise GeminiError(f"{tracker} failed after {MAX_RETRIES} attempts: {last_err}")


def _available(key_env):
    return bool(os.environ.get(key_env, "").strip())


# ── Public API ────────────────────────────────────────────────────────────────

def research(prompt, max_tokens=1500):
    """
    Step 1 — Research.
    K1 Flash → K2 Flash → empty string (never raises, never crashes the run).
    """
    for tracker, key_env in [("research_k1", KEY_K1),
                              ("research_k2", KEY_K2)]:
        if not _available(key_env):
            print(f"  [Research] {key_env} not set — skipping")
            continue
        try:
            result = _call(MODEL_FLASH, tracker, key_env, RPM_FLASH,
                           prompt, False, max_tokens)
            if result and len(result) > 50:
                print(f"  [Research] ✅ {tracker} ({key_env})")
                return result
        except GeminiQuotaError:
            print(f"  [Research] {tracker} quota hit — trying next key")
        except GeminiError as e:
            print(f"  [Research] {tracker} failed ({e}) — trying next key")

    print("  [Research] All keys failed — using empty context")
    return ""


def generate_questions(prompt, max_tokens=8192):
    """
    Step 2 — Generate raw questions (plain text, NOT JSON).
    All 3 keys used in order:
      1. K3 Pro   — best quality, uses Pro quota (GEMINI_API_KEY2)
      2. K3 Flash — same key, Flash model if Pro quota hit
      3. K1 Flash — fallback (GEMINI_API_KEY)
      4. K2 Flash — last resort (GEMINI_API_KEY_1)
    Raises GeminiError only if ALL keys fail.
    """
    attempts = [
        # (model,       tracker,       key_env, rpm)
        (MODEL_PRO,   "gen_k3_pro",   KEY_K3,  RPM_PRO),    # K3 Pro — primary
        (MODEL_FLASH, "gen_k3_flash", KEY_K3,  RPM_FLASH),  # K3 Flash — if Pro quota hit
        (MODEL_FLASH, "gen_k1_flash", KEY_K1,  RPM_FLASH),  # K1 Flash — fallback
        (MODEL_FLASH, "gen_k2_flash", KEY_K2,  RPM_FLASH),  # K2 Flash — last resort
    ]
    last_err = "no keys available"
    for model, tracker, key_env, rpm in attempts:
        if not _available(key_env):
            print(f"  [Generate] {key_env} not set — skipping {tracker}")
            continue
        try:
            result = _call(model, tracker, key_env, rpm, prompt, False, max_tokens)
            if result and len(result) > 100:
                print(f"  [Generate] ✅ {tracker} ({model})")
                return result
        except GeminiQuotaError:
            print(f"  [Generate] {tracker} quota hit — trying next")
            last_err = f"{tracker} quota exhausted"
        except GeminiError as e:
            print(f"  [Generate] {tracker} failed ({e}) — trying next")
            last_err = str(e)

    raise GeminiError(f"All 3 Gemini keys exhausted for generation. Last: {last_err}")


def structure_with_flash(prompt, max_tokens=6144):
    """
    Fallback JSON structurer — called only when both Groq keys fail.
    K1 Flash → K2 Flash → raise.
    """
    for tracker, key_env in [("struct_k1", KEY_K1),
                              ("struct_k2", KEY_K2)]:
        if not _available(key_env):
            continue
        try:
            result = _call(MODEL_FLASH, tracker, key_env, RPM_FLASH,
                           prompt, True, max_tokens)
            if result:
                print(f"  [StructureFlash] ✅ {tracker} ({key_env})")
                return result
        except Exception as e:
            print(f"  [StructureFlash] {tracker} failed ({e}) — trying next")

    raise GeminiError("Flash structuring failed on all available keys")

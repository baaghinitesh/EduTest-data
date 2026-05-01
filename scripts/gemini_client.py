"""
gemini_client.py — 3 Gemini keys with round-robin load balancing.

GitHub Secret names (exact):
  GEMINI_API_KEY    → K1 Flash  — researcher + generation fallback
  GEMINI_API_KEY_1  → K2 Flash  — researcher fallback + generation fallback
  GEMINI_API_KEY2   → K3 Pro/Flash — primary generator

Generation key rotation (round-robin across chunks):
  Chunk 1 → K3 Pro
  Chunk 2 → K1 Flash
  Chunk 3 → K2 Flash
  Chunk 4 → K3 Flash (Pro quota hit fallback)
  Chunk 5 → K1 Flash  ... and so on

This spreads RPM/RPD load across all 3 keys so no single key
hits rate limits during a multi-chunk generation run.

Free-tier daily budget per key:
  Flash: 1,500 RPD, 10 RPM
  Pro:   25 RPD, 5 RPM
"""
import os, json, re, time
from google import genai
from google.genai import types

MODEL_PRO   = "gemini-2.5-pro"
MODEL_FLASH = "gemini-2.5-flash"

RPM_PRO   = 4    # 5 RPM official → 4 safe
RPM_FLASH = 8    # 10 RPM official → 8 safe
MAX_RETRIES = 3

# ── Exact GitHub secret names ─────────────────────────────────────────────────
KEY_K1 = "GEMINI_API_KEY"    # Flash K1
KEY_K2 = "GEMINI_API_KEY_1"  # Flash K2
KEY_K3 = "GEMINI_API_KEY2"   # Pro/Flash K3

_clients   = {}
_last_call = {}

# Round-robin counter for generation — shared across all calls in a run
_gen_round_robin = 0


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
    Research step — K1 Flash → K2 Flash → empty string (never raises).
    Returns full research text (not trimmed — quality matters here).
    """
    for tracker, key_env in [("research_k1", KEY_K1),
                              ("research_k2", KEY_K2)]:
        if not _available(key_env):
            continue
        try:
            result = _call(MODEL_FLASH, tracker, key_env, RPM_FLASH,
                           prompt, False, max_tokens)
            if result and len(result) > 50:
                print(f"  [Research] ✅ {tracker}")
                return result
        except GeminiQuotaError:
            print(f"  [Research] {tracker} quota hit — trying next")
        except GeminiError as e:
            print(f"  [Research] {tracker} failed ({e}) — trying next")

    print("  [Research] All keys failed — using empty context")
    return ""


def generate_questions_for_chunk(prompt, chunk_index=0, max_tokens=4096):
    """
    Generate raw questions for ONE chunk (10Q).
    Uses round-robin across all 3 keys to spread RPM/RPD load.

    Key rotation order per chunk_index:
      0 → K3 Pro (best quality)
      1 → K1 Flash
      2 → K2 Flash
      3 → K3 Flash (if Pro quota hit on K3)
      4 → K1 Flash ... repeats

    If the primary key for this chunk fails, falls back through all others.
    """
    # Build ordered attempt list based on chunk_index for load balancing
    # Primary key rotates: K3→K1→K2→K3→K1→K2...
    rotation = [
        (MODEL_PRO,   "gen_k3_pro",   KEY_K3, RPM_PRO),    # K3 Pro
        (MODEL_FLASH, "gen_k1_flash", KEY_K1, RPM_FLASH),  # K1 Flash
        (MODEL_FLASH, "gen_k2_flash", KEY_K2, RPM_FLASH),  # K2 Flash
    ]
    # Rotate starting point based on chunk_index
    start = chunk_index % len(rotation)
    ordered = rotation[start:] + rotation[:start]

    # Always add K3 Flash as final fallback (if K3 Pro was primary and failed)
    fallback = (MODEL_FLASH, "gen_k3_flash", KEY_K3, RPM_FLASH)

    attempts = ordered + [fallback]
    last_err = "no keys available"

    for model, tracker, key_env, rpm in attempts:
        if not _available(key_env):
            continue
        # Skip duplicate tracker (K3 Flash already tried if K3 Pro was in ordered)
        try:
            result = _call(model, tracker, key_env, rpm, prompt, False, max_tokens)
            if result and len(result) > 80:
                print(f"  [Generate] ✅ chunk via {tracker} ({model})")
                return result
        except GeminiQuotaError:
            print(f"  [Generate] {tracker} quota hit — trying next key")
            last_err = f"{tracker} quota exhausted"
        except GeminiError as e:
            print(f"  [Generate] {tracker} failed ({e}) — trying next key")
            last_err = str(e)

    raise GeminiError(f"All Gemini keys failed for chunk {chunk_index}. Last: {last_err}")


def structure_with_flash(prompt, max_tokens=4096):
    """
    Fallback JSON structurer when both Groq keys fail.
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
                print(f"  [StructureFlash] ✅ {tracker}")
                return result
        except Exception as e:
            print(f"  [StructureFlash] {tracker} failed ({e}) — trying next")

    raise GeminiError("Flash structuring failed on all keys")

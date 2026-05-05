"""
gemini_client.py — 3 Gemini Flash keys + Mistral fallback.

GitHub Secret names (exact):
  GEMINI_API_KEY    → K1 Flash 2.5 — primary researcher + generation fallback
  GEMINI_API_KEY_1  → K2 Flash 2.5 — researcher fallback + generation fallback
  GEMINI_API_KEY2   → K3 Flash 2.5 — generation primary (dedicated generator key)
  MISTRAL_API_KEY   → Mistral small — 4th fallback when ALL Gemini keys exhausted

Generation key rotation (round-robin across chunks):
  Chunk 0 → K3 Flash (dedicated generator)
  Chunk 1 → K1 Flash
  Chunk 2 → K2 Flash
  Chunk 3 → K3 Flash ... repeats

Quota handling (per key):
  RPM (429 with short wait ≤ 90s) → wait and retry on SAME key
  RPD (429 with long wait > 90s, or RESOURCE_EXHAUSTED) → mark key exhausted,
      try next key immediately
  A key marked RPD-exhausted is skipped for the rest of the run.

Research:
  K1 Flash → K2 Flash → K3 Flash → Mistral → empty string (never raises)

Structure fallback (when both Groq keys fail):
  Mistral → Flash 2.0 K1 → Flash 2.0 K2 → Flash 2.5 K1 → Flash 2.5 K2
"""
import os, json, re, time
from google import genai
from google.genai import types

MODEL_FLASH_25 = "gemini-2.5-flash"   # primary — best quality Flash
MODEL_FLASH_20 = "gemini-2.0-flash"   # fallback — higher TPM, good for structuring

RPM_FLASH   = 8    # 10 RPM official → 8 safe
MAX_RETRIES = 3

# ── Exact GitHub secret names ─────────────────────────────────────────────────
KEY_K1 = "GEMINI_API_KEY"    # Flash K1
KEY_K2 = "GEMINI_API_KEY_1"  # Flash K2
KEY_K3 = "GEMINI_API_KEY2"   # Flash K3 (dedicated generator)

_clients   = {}
_last_call = {}

# Per-key RPD exhaustion tracker — once a key hits daily quota it's skipped
# for the rest of this process run.
_rpd_exhausted: set = set()


class GeminiError(Exception):
    pass


class GeminiQuotaError(GeminiError):
    """Raised only when ALL keys (Gemini + Mistral) are truly exhausted."""
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


def _parse_retry_delay(err_str):
    """
    Parse retry-after seconds from a 429 error message.
    Returns (seconds, is_long_wait).
    is_long_wait=True means RPD exhausted (wait > 90s) → skip key.
    is_long_wait=False means RPM hit (short wait) → sleep and retry.
    """
    m = re.search(r"retry.{0,20}after[:\s]+(\d+\.?\d*)\s*s", err_str, re.IGNORECASE)
    if not m:
        m = re.search(r"try again in\s+(?:(\d+)h\s*)?(?:(\d+)m\s*)?(\d+\.?\d*)s",
                      err_str, re.IGNORECASE)
        if m:
            h    = int(m.group(1) or 0)
            mins = int(m.group(2) or 0)
            secs = float(m.group(3) or 0)
            total = h * 3600 + mins * 60 + secs
            return total, total > 90
    if m:
        secs = float(m.group(1))
        return secs, secs > 90
    # No delay found — assume short RPM hit
    return 30.0, False


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


def _call(model, tracker, key_env, prompt, expect_json, max_tokens):
    """
    Call one Gemini key.
    - RPM hit (short wait) → sleep and retry on same key (up to MAX_RETRIES)
    - RPD hit (long wait / RESOURCE_EXHAUSTED) → mark key exhausted, raise GeminiQuotaError
    - Other errors → retry up to MAX_RETRIES, then raise GeminiError
    """
    if key_env in _rpd_exhausted:
        raise GeminiQuotaError(f"{key_env} already RPD-exhausted this run")

    client   = _get_client(key_env)
    last_err = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _rate_limit(tracker, RPM_FLASH)
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
                wait_secs, is_rpd = _parse_retry_delay(s)
                if is_rpd or "RESOURCE_EXHAUSTED" in s:
                    # Daily quota exhausted — mark and skip this key
                    _rpd_exhausted.add(key_env)
                    print(f"  [Gemini/{tracker}] RPD exhausted — marking {key_env} as done for this run")
                    raise GeminiQuotaError(f"RPD exhausted on {tracker} ({key_env})")
                else:
                    # RPM hit — wait and retry on same key
                    wait_secs = min(wait_secs, 65.0)  # cap at 65s for RPM
                    print(f"  [Gemini/{tracker}] RPM hit — sleeping {wait_secs:.0f}s then retrying")
                    time.sleep(wait_secs)
                    continue  # retry same key

            if any(x in s for x in ["503", "UNAVAILABLE", "overloaded"]):
                wait = 20 * attempt
                print(f"  [Gemini/{tracker}] server busy — sleeping {wait}s")
                time.sleep(wait)
                continue
            if attempt < MAX_RETRIES:
                time.sleep(5 * attempt)

    raise GeminiError(f"{tracker} failed after {MAX_RETRIES} attempts: {last_err}")


def _available(key_env):
    """Key is available if set AND not RPD-exhausted this run."""
    return bool(os.environ.get(key_env, "").strip()) and key_env not in _rpd_exhausted


def _available_set():
    """Return True if set (ignoring exhaustion) — for reporting."""
    return bool(os.environ.get)


def all_gemini_exhausted():
    """True if every configured Gemini key is RPD-exhausted."""
    keys = [KEY_K1, KEY_K2, KEY_K3]
    configured = [k for k in keys if os.environ.get(k, "").strip()]
    return bool(configured) and all(k in _rpd_exhausted for k in configured)


# ── Public API ────────────────────────────────────────────────────────────────

def research(prompt, max_tokens=1500):
    """
    Research step — K1 → K2 → K3 Flash → Mistral → empty string (never raises).
    Uses all 3 Gemini keys before falling back to Mistral.
    """
    for tracker, key_env in [("research_k1", KEY_K1),
                              ("research_k2", KEY_K2),
                              ("research_k3", KEY_K3)]:
        if not _available(key_env):
            continue
        try:
            result = _call(MODEL_FLASH_25, tracker, key_env, prompt, False, max_tokens)
            if result and len(result) > 50:
                print(f"  [Research] ✅ {tracker}")
                return result
        except GeminiQuotaError:
            print(f"  [Research] {tracker} RPD exhausted — trying next key")
        except GeminiError as e:
            print(f"  [Research] {tracker} failed ({e}) — trying next key")

    # Mistral fallback
    try:
        import mistral_client
        result = mistral_client.research(prompt, max_tokens=max_tokens)
        if result:
            return result
    except Exception as e:
        print(f"  [Research] Mistral fallback failed ({e})")

    print("  [Research] All keys failed — using empty context")
    return ""


def generate_questions_for_chunk(prompt, chunk_index=0, max_tokens=4096):
    """
    Generate raw questions for ONE chunk (10Q).

    Key order per chunk (round-robin to spread RPM/RPD load):
      Chunk 0 → K3 → K1 → K2
      Chunk 1 → K1 → K2 → K3
      Chunk 2 → K2 → K3 → K1
      ...repeats

    RPM hit on a key → wait and retry on SAME key (handled inside _call).
    RPD hit on a key → skip to next key immediately.
    All 3 Gemini keys RPD-exhausted → try Mistral.
    Mistral also fails → raise GeminiQuotaError (run loop stops, draft saved).
    """
    rotation = [
        ("gen_k3", KEY_K3),
        ("gen_k1", KEY_K1),
        ("gen_k2", KEY_K2),
    ]
    start   = chunk_index % len(rotation)
    ordered = rotation[start:] + rotation[:start]

    last_err         = "no keys available"
    any_key_tried    = False

    for tracker, key_env in ordered:
        if not os.environ.get(key_env, "").strip():
            print(f"  [Generate] {key_env} not set — skipping {tracker}")
            continue
        if key_env in _rpd_exhausted:
            print(f"  [Generate] {tracker} RPD-exhausted — skipping")
            continue

        any_key_tried = True
        try:
            result = _call(MODEL_FLASH_25, tracker, key_env, prompt, False, max_tokens)
            if result and len(result) > 80:
                print(f"  [Generate] ✅ {tracker} (Flash 2.5)")
                return result
            print(f"  [Generate] {tracker} response too short — trying next key")
            last_err = "response too short"
        except GeminiQuotaError:
            print(f"  [Generate] {tracker} RPD exhausted — trying next Gemini key")
            last_err = f"{tracker} RPD exhausted"
        except GeminiError as e:
            print(f"  [Generate] {tracker} failed ({e}) — trying next key")
            last_err = str(e)

    # All available Gemini keys failed/exhausted — try Mistral as final fallback
    try:
        import mistral_client
        if mistral_client.available():
            print(f"  [Generate] All Gemini keys done — trying Mistral (mistral-small-latest)")
            result = mistral_client.generate_questions(prompt, max_tokens=max_tokens)
            if result and len(result) > 80:
                return result
            last_err = "Mistral response too short"
        else:
            print(f"  [Generate] Mistral not configured (MISTRAL_API_KEY not set)")
    except Exception as e:
        print(f"  [Generate] Mistral fallback failed ({e})")
        last_err = str(e)

    # Every key exhausted or failed
    if all_gemini_exhausted():
        raise GeminiQuotaError(
            f"All Gemini keys RPD-exhausted for chunk {chunk_index}. "
            f"Mistral also unavailable/failed. Last: {last_err}"
        )
    # Non-quota failure (all keys tried but got errors, not quota)
    raise GeminiError(f"All keys failed for chunk {chunk_index}. Last: {last_err}")


def structure_with_flash(prompt, max_tokens=4096):
    """
    Fallback JSON structurer when both Groq keys fail.
    Order: Mistral → Flash 2.0 K1 → Flash 2.0 K2 → Flash 2.5 K1 → Flash 2.5 K2
    Mistral is tried first because it has a separate quota from Gemini.
    """
    # Try Mistral first — separate quota from Gemini
    try:
        import mistral_client
        if mistral_client.available():
            result = mistral_client._call(
                "You are an expert educator. Output ONLY valid JSON — no markdown, no text outside the JSON array.",
                prompt,
                expect_json=True,
                max_tokens=max_tokens
            )
            if isinstance(result, list) and result:
                print(f"  [StructureFlash] ✅ mistral (mistral-small-latest)")
                return result
            if isinstance(result, dict):
                return [result]
    except Exception as e:
        print(f"  [StructureFlash] Mistral failed ({e}) — trying Flash")

    # Flash fallback — 2.0 first (higher TPM), then 2.5
    # Use all 3 keys (K1, K2, K3) for structuring to maximise chances
    attempts = [
        ("struct_k1_20", KEY_K1, MODEL_FLASH_20),
        ("struct_k2_20", KEY_K2, MODEL_FLASH_20),
        ("struct_k3_20", KEY_K3, MODEL_FLASH_20),
        ("struct_k1_25", KEY_K1, MODEL_FLASH_25),
        ("struct_k2_25", KEY_K2, MODEL_FLASH_25),
        ("struct_k3_25", KEY_K3, MODEL_FLASH_25),
    ]
    for tracker, key_env, model in attempts:
        if not os.environ.get(key_env, "").strip():
            continue
        # For structuring, try even RPD-exhausted keys on Flash 2.0
        # (different model = different quota bucket on some accounts)
        try:
            result = _call(model, tracker, key_env, prompt, True, max_tokens)
            if result:
                print(f"  [StructureFlash] ✅ {tracker} ({model})")
                return result
        except Exception as e:
            print(f"  [StructureFlash] {tracker} failed ({e}) — trying next")

    raise GeminiError("Flash structuring failed on all keys")

"""
gemini_client.py — 3 Gemini Flash keys + Mistral fallback.

GitHub Secret names (exact):
  GEMINI_API_KEY    → K1 Flash 2.5 — primary researcher + generation
  GEMINI_API_KEY_1  → K2 Flash 2.5 — researcher fallback + generation fallback
  GEMINI_API_KEY2   → K3 Flash 2.5 — generation primary (dedicated generator key)
  MISTRAL_API_KEY   → Mistral small — 4th fallback when ALL Gemini keys exhausted

Generation key rotation (round-robin across chunks):
  Chunk 0 → K3 Flash (dedicated generator)
  Chunk 1 → K1 Flash
  Chunk 2 → K2 Flash
  Chunk 3 → K3 Flash ... repeats
  If ALL Gemini keys exhausted → Mistral fallback

Research:
  K1 Flash → K2 Flash → Mistral → empty string (never crashes)

Structure fallback (when both Groq keys fail):
  Mistral → Flash 2.0 K1 → Flash 2.0 K2 → Flash 2.5 K1 → Flash 2.5 K2
"""
import os, json, re, time
from google import genai
from google.genai import types

MODEL_FLASH_25 = "gemini-2.5-flash"   # primary — best quality Flash
MODEL_FLASH_20 = "gemini-2.0-flash"   # fallback — higher TPM, good for structuring

RPM_FLASH = 8    # 10 RPM official → 8 safe
MAX_RETRIES = 3

# ── Exact GitHub secret names ─────────────────────────────────────────────────
KEY_K1 = "GEMINI_API_KEY"    # Flash K1
KEY_K2 = "GEMINI_API_KEY_1"  # Flash K2
KEY_K3 = "GEMINI_API_KEY2"   # Flash K3 (dedicated generator)

_clients   = {}
_last_call = {}


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


def _call(model, tracker, key_env, prompt, expect_json, max_tokens):
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
                raise GeminiQuotaError(f"Quota hit on {tracker} ({key_env})")
            if any(x in s for x in ["503", "UNAVAILABLE", "overloaded"]):
                wait = 20 * attempt
                print(f"  [Gemini/{tracker}] server busy — sleeping {wait}s")
                time.sleep(wait)
                continue
            if attempt < MAX_RETRIES:
                time.sleep(5 * attempt)
    raise GeminiError(f"{tracker} failed after {MAX_RETRIES} attempts: {last_err}")


def _available(key_env):
    return bool(os.environ.get(key_env, "").strip())


# ── Public API ────────────────────────────────────────────────────────────────

def research(prompt, max_tokens=1500):
    """
    Research step — K1 Flash → K2 Flash → Mistral → empty string (never raises).
    """
    for tracker, key_env in [("research_k1", KEY_K1),
                              ("research_k2", KEY_K2)]:
        if not _available(key_env):
            continue
        try:
            result = _call(MODEL_FLASH_25, tracker, key_env, prompt, False, max_tokens)
            if result and len(result) > 50:
                print(f"  [Research] ✅ {tracker}")
                return result
        except GeminiQuotaError:
            print(f"  [Research] {tracker} quota hit — trying next key")
        except GeminiError as e:
            print(f"  [Research] {tracker} failed ({e}) — trying next key")

    # Mistral fallback — only imported if needed to avoid startup cost
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
    Round-robin across K3 → K1 → K2 based on chunk_index.
    Falls back through all Gemini keys, then Mistral if all Gemini exhausted.
    """
    # Rotation: K3 → K1 → K2 → K3 → K1 → K2 ...
    rotation = [
        ("gen_k3", KEY_K3),
        ("gen_k1", KEY_K1),
        ("gen_k2", KEY_K2),
    ]
    start   = chunk_index % len(rotation)
    ordered = rotation[start:] + rotation[:start]

    last_err = "no keys available"
    gemini_all_quota = True   # track if ALL Gemini keys hit quota

    for tracker, key_env in ordered:
        if not _available(key_env):
            print(f"  [Generate] {key_env} not set — skipping {tracker}")
            continue
        try:
            result = _call(MODEL_FLASH_25, tracker, key_env, prompt, False, max_tokens)
            if result and len(result) > 80:
                print(f"  [Generate] ✅ {tracker} (Flash 2.5)")
                return result
            gemini_all_quota = False   # got a response, just too short
        except GeminiQuotaError:
            print(f"  [Generate] {tracker} quota hit — trying next key")
            last_err = f"{tracker} quota exhausted"
        except GeminiError as e:
            print(f"  [Generate] {tracker} failed ({e}) — trying next key")
            last_err = str(e)
            gemini_all_quota = False   # non-quota failure

    # All Gemini keys failed — try Mistral as 4th fallback
    try:
        import mistral_client
        if mistral_client.available():
            print(f"  [Generate] All Gemini keys failed — trying Mistral fallback")
            result = mistral_client.generate_questions(prompt, max_tokens=max_tokens)
            if result and len(result) > 80:
                return result
        else:
            print(f"  [Generate] Mistral not configured (MISTRAL_API_KEY not set)")
    except Exception as e:
        print(f"  [Generate] Mistral fallback failed ({e})")
        last_err = str(e)

    raise GeminiError(f"All Flash keys exhausted for chunk {chunk_index}. Last: {last_err}")


def structure_with_flash(prompt, max_tokens=4096):
    """
    Fallback JSON structurer when both Groq keys fail.
    Order: Mistral → Flash 2.0 K1 → Flash 2.0 K2 → Flash 2.5 K1 → Flash 2.5 K2
    Mistral is tried first because it's not rate-limited by the same quota as Gemini.
    """
    # Try Mistral first — separate quota from Gemini
    try:
        import mistral_client
        if mistral_client.available():
            # Extract the raw text and schema from the flash_prompt format
            # The prompt is: "Extract ALL questions...\nTEXT:\n{raw_text}"
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
    attempts = [
        ("struct_k1_20", KEY_K1, MODEL_FLASH_20),
        ("struct_k2_20", KEY_K2, MODEL_FLASH_20),
        ("struct_k1_25", KEY_K1, MODEL_FLASH_25),
        ("struct_k2_25", KEY_K2, MODEL_FLASH_25),
    ]
    for tracker, key_env, model in attempts:
        if not _available(key_env):
            continue
        try:
            result = _call(model, tracker, key_env, prompt, True, max_tokens)
            if result:
                print(f"  [StructureFlash] ✅ {tracker} ({model})")
                return result
        except Exception as e:
            print(f"  [StructureFlash] {tracker} failed ({e}) — trying next")

    raise GeminiError("Flash structuring failed on all keys")

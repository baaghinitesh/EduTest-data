"""
gemini_client.py — 3 Gemini Flash keys + Mistral fallback.

GitHub Secret names (exact):
  GEMINI_API_KEY    → K1 Flash 2.5 — primary researcher + generation fallback
  GEMINI_API_KEY_1  → K2 Flash 2.5 — researcher fallback + generation fallback
  GEMINI_API_KEY2   → K3 Flash 2.5 — generation primary (dedicated generator key)
  MISTRAL_API_KEY   → Mistral small — 4th key in generation rotation (every chunk)

Generation key rotation — ALL 6 keys × 2 models tried per chunk before giving up:
  For each chunk, the order is built as:
    [K3-2.5, K3-2.0, K1-2.5, K1-2.0, K2-2.5, K2-2.0, Mistral, GroqK1, GroqK2]
  The starting Gemini key rotates by chunk_index (0→K3, 1→K1, 2→K2, 3→Mistral-first, …)
  so load is spread across keys across chunks.

  Within each key: try Flash 2.5 first, then Flash 2.0 (lower model = different quota bucket).
  Between keys: short sleep (2s) to avoid hammering rate limits.
  Groq is used as last-resort generation fallback (it can generate MCQs too).

Quota handling (per key):
  RPM (429 with short wait ≤ 90s) → wait and retry on SAME key+model
  RPD (429 with long wait > 90s, or RESOURCE_EXHAUSTED) → mark key exhausted,
      try next key immediately (but still try lower model on same key first)
  A key marked RPD-exhausted is skipped for the rest of the run.

Research (all keys tried before returning empty):
  K1 Flash 2.5 → K2 Flash 2.5 → K3 Flash 2.5 → Mistral → Flash 2.0 K1/K2/K3

Structure fallback (when both Groq keys AND Mistral fail — called from content_builder):
  Flash 2.0 K1 → Flash 2.0 K2 → Flash 2.0 K3 → Flash 2.5 K1 → Flash 2.5 K2 → Flash 2.5 K3
  NOTE: Groq K1→K2→Mistral are handled BEFORE this function is called (in groq_client).
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

# Per-key+model exhaustion tracker — tracks Flash 2.5 vs Flash 2.0 separately
# key: (key_env, model) → True if exhausted
_model_exhausted: dict = {}

# Sleep between key switches to avoid hammering rate limits
_INTER_KEY_SLEEP = 2   # seconds between trying different keys
_MAX_SLEEP_CAP   = 65  # cap RPM sleep at 65s (never block for > 1 min on RPM)


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
    Call one Gemini key+model combination.
    - RPM hit (short wait) → sleep and retry on same key (up to MAX_RETRIES)
    - RPD hit (long wait / RESOURCE_EXHAUSTED) → mark key exhausted, raise GeminiQuotaError
    - Other errors → retry up to MAX_RETRIES, then raise GeminiError
    """
    if key_env in _rpd_exhausted:
        raise GeminiQuotaError(f"{key_env} already RPD-exhausted this run")

    # Check per-model exhaustion (Flash 2.5 exhausted but Flash 2.0 might still work)
    if _model_exhausted.get((key_env, model)):
        raise GeminiQuotaError(f"{key_env}/{model} already exhausted this run")

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
                    # Daily quota exhausted — mark this key+model as done
                    _model_exhausted[(key_env, model)] = True
                    # If both models for this key are exhausted, mark the key itself
                    if (_model_exhausted.get((key_env, MODEL_FLASH_25)) and
                            _model_exhausted.get((key_env, MODEL_FLASH_20))):
                        _rpd_exhausted.add(key_env)
                        print(f"  [Gemini/{tracker}] Both models exhausted — marking {key_env} fully done")
                    else:
                        print(f"  [Gemini/{tracker}] RPD exhausted on {model} — will try other model for {key_env}")
                    raise GeminiQuotaError(f"RPD exhausted on {tracker}/{model} ({key_env})")
                else:
                    # RPM hit — wait and retry on same key
                    wait_secs = min(wait_secs, _MAX_SLEEP_CAP)
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
    """Key is available if set AND not fully RPD-exhausted this run."""
    return bool(os.environ.get(key_env, "").strip()) and key_env not in _rpd_exhausted


def _model_available(key_env, model):
    """Key+model is available if key is set and this specific model not exhausted."""
    if not os.environ.get(key_env, "").strip():
        return False
    if key_env in _rpd_exhausted:
        return False
    return not _model_exhausted.get((key_env, model), False)


def all_gemini_exhausted():
    """True if every configured Gemini key is RPD-exhausted (both models)."""
    keys = [KEY_K1, KEY_K2, KEY_K3]
    configured = [k for k in keys if os.environ.get(k, "").strip()]
    if not configured:
        return True
    # A key is "fully exhausted" if it's in _rpd_exhausted OR both models are exhausted
    def _key_fully_exhausted(k):
        if k in _rpd_exhausted:
            return True
        return (_model_exhausted.get((k, MODEL_FLASH_25), False) and
                _model_exhausted.get((k, MODEL_FLASH_20), False))
    return all(_key_fully_exhausted(k) for k in configured)


def all_keys_exhausted():
    """
    True if ALL generation keys (Gemini K1/K2/K3 + Mistral + Groq K1/K2) are exhausted.
    Used to determine if the run should stop entirely.
    Groq is included because it's used as last-resort generation fallback.
    """
    import mistral_client as _mc
    import groq_client as _gq
    gemini_done  = all_gemini_exhausted()
    mistral_done = not _mc.available() or _mc._is_exhausted()
    groq_done    = _gq.all_groq_exhausted()
    return gemini_done and mistral_done and groq_done


# ── Unified Call Helpers ──────────────────────────────────────────────────────

def _call_gemini_unified(key_env, model, prompt, tracker, max_tokens):
    return _call(model, tracker, key_env, prompt, False, max_tokens)


def _call_mistral_unified(key_env, model, prompt, max_tokens):
    import mistral_client
    if not mistral_client.available():
        raise GeminiQuotaError("Mistral is not configured or already exhausted")
    return mistral_client.generate_questions(prompt, max_tokens=max_tokens, model=model)


def _call_groq_unified(key_env, model, prompt, max_tokens):
    import groq_client
    if key_env in groq_client._tpd_exhausted:
        raise GeminiQuotaError(f"Groq {key_env} already exhausted")
    system = "You are an expert MCQ question setter. Generate high-quality questions exactly as instructed. Do NOT output JSON — write in plain numbered text format."
    return groq_client._call_one(
        key_env,
        system,
        prompt,
        max_tokens=min(max_tokens, 4000),
        model=model
    )


def _try_key_model_unified(client_type, key_env, model, prompt, tracker_label, max_tokens):
    """
    Attempt to run the prompt with a specific key and model.
    Handles calls and records exhaustion.
    Returns (result_text, is_exhausted_error) or (None, is_exhausted_error).
    """
    import mistral_client
    import groq_client

    # 1. Check if already marked exhausted
    if client_type == "gemini":
        if not _model_available(key_env, model):
            return None, True
    elif client_type == "groq":
        if key_env in groq_client._tpd_exhausted:
            return None, True
    elif client_type == "mistral":
        if not mistral_client.available():
            return None, True

    print(f"  [UnifiedGen] Trying {client_type}/{key_env} model={model}...")

    # 2. Make the call
    try:
        if client_type == "gemini":
            res = _call_gemini_unified(key_env, model, prompt, tracker_label, max_tokens)
        elif client_type == "mistral":
            res = _call_mistral_unified(key_env, model, prompt, max_tokens)
        elif client_type == "groq":
            res = _call_groq_unified(key_env, model, prompt, max_tokens)
        else:
            raise ValueError(f"Unknown client type: {client_type}")

        if res and len(res) > 80:
            print(f"  [UnifiedGen] ✅ Success with {client_type}/{key_env} model={model}")
            return res, False
        else:
            print(f"  [UnifiedGen] ⚠️ Success but response too short with {client_type}/{key_env}")
            return None, False

    except (GeminiQuotaError, mistral_client.MistralQuotaError) as e:
        print(f"  [UnifiedGen] 🛑 Quota/RPD exhausted for {client_type}/{key_env} model={model}: {e}")
        # Mark as exhausted
        if client_type == "gemini":
            _model_exhausted[(key_env, model)] = True
            if (_model_exhausted.get((key_env, MODEL_FLASH_25)) and
                    _model_exhausted.get((key_env, MODEL_FLASH_20))):
                _rpd_exhausted.add(key_env)
                print(f"  [Gemini] Both models exhausted — marking {key_env} fully done")
        elif client_type == "mistral":
            mistral_client._exhausted = True
            print(f"  [Mistral] Marking Mistral exhausted")
        return None, True

    except Exception as e:
        s = str(e)
        print(f"  [UnifiedGen] ❌ Error with {client_type}/{key_env} model={model}: {s[:150]}")
        # Check if it looks like a daily quota exhaustion
        is_quota = False
        if "RESOURCE_EXHAUSTED" in s or "quota" in s.lower() or "limit" in s.lower() or "tpd exhausted" in s.lower():
            is_quota = True

        if is_quota:
            if client_type == "gemini":
                _model_exhausted[(key_env, model)] = True
                if (_model_exhausted.get((key_env, MODEL_FLASH_25)) and
                        _model_exhausted.get((key_env, MODEL_FLASH_20))):
                    _rpd_exhausted.add(key_env)
                    print(f"  [Gemini] Both models exhausted — marking {key_env} fully done")
            elif client_type == "groq":
                groq_client._tpd_exhausted.add(key_env)
                print(f"  [Groq] Marking {key_env} exhausted")
            elif client_type == "mistral":
                mistral_client._exhausted = True
                print(f"  [Mistral] Marking Mistral exhausted")
            return None, True
        
        return None, False


# ── Public API ────────────────────────────────────────────────────────────────

def research(prompt, max_tokens=1500):
    """
    Research step — tries ALL available keys before giving up.
    Uses the exact same unified primary and lower model tiers as generation.
    Returns text or empty string (never raises, falls back gracefully).
    """
    # Define Tiers for Research
    tier1 = [
        ("gemini", KEY_K1, MODEL_FLASH_25, "research_k1_25"),
        ("gemini", KEY_K2, MODEL_FLASH_25, "research_k2_25"),
        ("gemini", KEY_K3, MODEL_FLASH_25, "research_k3_25"),
        ("mistral", "MISTRAL_API_KEY", "mistral-small-latest", "research_mistral"),
        ("groq", "GROQ_API_KEY", "llama-3.3-70b-versatile", "research_groq1_33"),
        ("groq", "GROQ_API_KEY2", "llama-3.3-70b-versatile", "research_groq2_33"),
    ]
    
    tier2 = [
        ("gemini", KEY_K1, MODEL_FLASH_20, "research_k1_20"),
        ("gemini", KEY_K2, MODEL_FLASH_20, "research_k2_20"),
        ("gemini", KEY_K3, MODEL_FLASH_20, "research_k3_20"),
        ("groq", "GROQ_API_KEY", "llama-3.1-8b-instant", "research_groq1_31"),
        ("groq", "GROQ_API_KEY2", "llama-3.1-8b-instant", "research_groq2_31"),
    ]
    
    def is_set(key_env):
        return bool(os.environ.get(key_env, "").strip())
        
    t1_active = [item for item in tier1 if is_set(item[1])]
    t2_active = [item for item in tier2 if is_set(item[1])]
    
    attempts = t1_active + t2_active
    
    for client_type, key_env, model, label in attempts:
        # Sleep between switches to avoid hammering rate limits
        if _INTER_KEY_SLEEP > 0:
            time.sleep(_INTER_KEY_SLEEP)
            
        res, is_exhausted = _try_key_model_unified(
            client_type, key_env, model, prompt, label, max_tokens
        )
        
        if res and len(res) > 50:
            print(f"  [Research] ✅ Success with {client_type}/{key_env} model={model}")
            return res
            
    print("  [Research] All keys failed — using empty context")
    return ""


def generate_questions_for_chunk(prompt, chunk_index=0, max_tokens=4096):
    """
    Generate raw questions for ONE chunk (10Q).

    Unified 6 API Key × 2 Model Two-Tier Rotation Flow:
      1. Tier 1 (Primary Models across all keys)
      2. Tier 2 (Lower Models across all keys)
    
    Starting keys within each tier rotate by chunk_index to balance load.
    Handles per-minute rate limits by sleeping and retrying in up to 3 passes.
    Raises GeminiQuotaError ONLY when all keys are completely daily exhausted.
    """
    # Define Tiers
    tier1 = [
        ("gemini", KEY_K1, MODEL_FLASH_25, "gen_k1_25"),
        ("gemini", KEY_K2, MODEL_FLASH_25, "gen_k2_25"),
        ("gemini", KEY_K3, MODEL_FLASH_25, "gen_k3_25"),
        ("mistral", "MISTRAL_API_KEY", "mistral-small-latest", "gen_mistral"),
        ("groq", "GROQ_API_KEY", "llama-3.3-70b-versatile", "gen_groq1_33"),
        ("groq", "GROQ_API_KEY2", "llama-3.3-70b-versatile", "gen_groq2_33"),
    ]
    
    tier2 = [
        ("gemini", KEY_K1, MODEL_FLASH_20, "gen_k1_20"),
        ("gemini", KEY_K2, MODEL_FLASH_20, "gen_k2_20"),
        ("gemini", KEY_K3, MODEL_FLASH_20, "gen_k3_20"),
        ("groq", "GROQ_API_KEY", "llama-3.1-8b-instant", "gen_groq1_31"),
        ("groq", "GROQ_API_KEY2", "llama-3.1-8b-instant", "gen_groq2_31"),
    ]
    
    # Filter for configured keys
    def is_set(key_env):
        return bool(os.environ.get(key_env, "").strip())
        
    t1_active = [item for item in tier1 if is_set(item[1])]
    t2_active = [item for item in tier2 if is_set(item[1])]
    
    # Rotate active lists by chunk_index to balance load
    if t1_active:
        shift1 = chunk_index % len(t1_active)
        t1_active = t1_active[shift1:] + t1_active[:shift1]
    if t2_active:
        shift2 = chunk_index % len(t2_active)
        t2_active = t2_active[shift2:] + t2_active[:shift2]
        
    attempts = t1_active + t2_active
    
    if not attempts:
        raise GeminiQuotaError("No generation keys are configured in the environment!")
        
    # Attempt loop with transient retries (MAX_PASSES)
    MAX_PASSES = 3
    last_err = "No keys available"
    
    for pass_idx in range(MAX_PASSES):
        if pass_idx > 0:
            print(f"  [Generate] Pass {pass_idx + 1}/{MAX_PASSES} — retrying transient/non-exhausted keys...")
            
        transient_keys_available = False
        
        for client_type, key_env, model, label in attempts:
            # Check if this key/model is permanently exhausted
            if client_type == "gemini":
                if not _model_available(key_env, model):
                    continue
            elif client_type == "groq":
                import groq_client
                if key_env in groq_client._tpd_exhausted:
                    continue
            elif client_type == "mistral":
                import mistral_client
                if not mistral_client.available():
                    continue
                    
            transient_keys_available = True
            
            # Sleep between key switches to prevent hammering rate limits
            if _INTER_KEY_SLEEP > 0:
                time.sleep(_INTER_KEY_SLEEP)
                
            res, is_exhausted = _try_key_model_unified(
                client_type, key_env, model, prompt, label, max_tokens
            )
            
            if res:
                return res
            if not is_exhausted:
                last_err = f"Transient failure on {client_type}/{key_env}"
                
        # If no keys are even available for retry (all permanently exhausted), stop immediately
        if not transient_keys_available:
            print("  [Generate] All keys are permanently exhausted. Skipping further passes.")
            break
            
        # If we failed all keys but some are still transiently available, sleep and try next pass
        if pass_idx < MAX_PASSES - 1:
            wait_time = 15
            print(f"  [Generate] Pass {pass_idx + 1} failed. Sleeping {wait_time}s before next pass...")
            time.sleep(wait_time)
            
    # If we get here, everything failed
    if all_keys_exhausted():
        raise GeminiQuotaError("All 6 generation keys are fully exhausted (daily limits hit).")
    raise GeminiError(f"All keys/models failed in this run. Last error: {last_err}")


def structure_with_flash(prompt, max_tokens=4096):
    """
    Last-resort JSON structurer — called only after Groq K1, Groq K2, AND Mistral
    have all failed (those are handled in groq_client.format_questions).

    Order: Flash 2.0 K1 → Flash 2.0 K2 → Flash 2.0 K3 → Flash 2.5 K1 → Flash 2.5 K2 → Flash 2.5 K3
    Flash 2.0 is tried first (higher TPM quota bucket).
    All 3 Gemini keys are tried on both models = 6 attempts total.
    """
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
        if model == MODEL_FLASH_25 and key_env in _rpd_exhausted:
            continue
        try:
            result = _call(model, tracker, key_env, prompt, True, max_tokens)
            if result:
                print(f"  [StructureFlash] ✅ {tracker} ({model})")
                return result
        except Exception as e:
            print(f"  [StructureFlash] {tracker} failed ({e}) — trying next")

    raise GeminiError("Flash structuring failed on all keys (Flash 2.0 K1/K2/K3 + Flash 2.5 K1/K2/K3)")

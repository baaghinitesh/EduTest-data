"""
gemini_client.py — 3 Gemini Flash keys + Mistral/Groq fallback.

GitHub Secret names (exact):
  GEMINI_API_KEY    → K1 — primary researcher + generation fallback
  GEMINI_API_KEY_1  → K2 — researcher fallback + generation fallback
  GEMINI_API_KEY2   → K3 — generation primary (dedicated generator key)
  MISTRAL_API_KEY   → Mistral small — 4th key in generation rotation (optional)

Active Gemini models:
  MODEL_PRIMARY = "gemini-3.8-flash"       (Tier 1: highest quality MCQ generation & research)
  MODEL_LITE    = "gemini-3.5-flash-lite"  (Tier 2: high-throughput fallback + JSON structuring)

Generation key rotation:
  Tier 1: K_a-3.8 → K_b-3.8 → K_c-3.8 → Mistral → GroqK1(gpt-oss-120b) → GroqK2(gpt-oss-120b)
  Tier 2: K_a-3.5-lite → K_b-3.5-lite → K_c-3.5-lite → GroqK1(qwen3.8-27b) → GroqK2(qwen3.8-27b)
  Starting key rotates by chunk_index so load is spread across keys.

Circuit-breaking & quota handling:
  404 / NOT_FOUND / model unavailable → mark model dead for the run immediately (no retries)
  401 / 403 / API_KEY_INVALID         → mark key dead for the run immediately (no retries)
  RPM (429 with short wait ≤ 90s)     → wait and retry on SAME key+model
  RPD (429 with long wait > 90s, or RESOURCE_EXHAUSTED) → mark key+model exhausted,
      try next model/key immediately
"""
import os, time, re
from google import genai
from google.genai import types

from json_utils import extract_json

MODEL_PRIMARY = "gemini-3.8-flash"       # Tier 1 — best reasoning/quality Flash
MODEL_LITE    = "gemini-3.5-flash-lite"  # Tier 2 — high RPM/RPD + fast JSON structuring

# Backwards-compatible aliases for callers/reports
MODEL_FLASH_25 = MODEL_PRIMARY
MODEL_FLASH_20 = MODEL_LITE

GROQ_GEN_PRIMARY  = "openai/gpt-oss-120b"
GROQ_GEN_FALLBACK = "qwen/qwen3.8-27b"

RPM_FLASH   = 8
MAX_RETRIES = 3

# ── Exact GitHub secret names ─────────────────────────────────────────────────
KEY_K1 = "GEMINI_API_KEY"    # Flash K1
KEY_K2 = "GEMINI_API_KEY_1"  # Flash K2
KEY_K3 = "GEMINI_API_KEY2"   # Flash K3 (dedicated generator)

_clients   = {}
_last_call = {}

# Per-key exhaustion tracker (RPD exhausted on all models, or invalid key)
_rpd_exhausted: set = set()

# Per-key+model exhaustion tracker — tracks primary vs lite separately
# key: (key_env, model) → True if exhausted or permanently unavailable
_model_exhausted: dict = {}

_INTER_KEY_SLEEP = 2   # seconds between trying different keys
_MAX_SLEEP_CAP   = 65  # cap RPM sleep at 65s


class GeminiError(Exception):
    pass


class GeminiQuotaError(GeminiError):
    """Raised only when ALL keys (Gemini + Mistral + Groq) are truly exhausted."""
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
    return 30.0, False


def _extract_json(text):
    return extract_json(text, error_cls=GeminiError)


def _mark_model_dead(key_env, model):
    _model_exhausted[(key_env, model)] = True
    if (_model_exhausted.get((key_env, MODEL_PRIMARY)) and
            _model_exhausted.get((key_env, MODEL_LITE))):
        _rpd_exhausted.add(key_env)
        print(f"  [Gemini] Both models exhausted/unavailable — marking {key_env} done")


def _call(model, tracker, key_env, prompt, expect_json, max_tokens):
    """
    Call one Gemini key+model combination.
    - 404 / NOT_FOUND → mark model dead across all keys immediately (no retries)
    - 401 / 403 / invalid key → mark key dead immediately (no retries)
    - RPM hit (short wait) → sleep and retry on same key (up to MAX_RETRIES)
    - RPD hit (long wait / RESOURCE_EXHAUSTED) → mark key+model exhausted, raise GeminiQuotaError
    - 503 / transient / malformed JSON → retry up to MAX_RETRIES, then raise GeminiError
    """
    if key_env in _rpd_exhausted:
        raise GeminiQuotaError(f"{key_env} already exhausted this run")

    if _model_exhausted.get((key_env, model)):
        raise GeminiQuotaError(f"{key_env}/{model} already exhausted/unavailable this run")

    client   = _get_client(key_env)
    last_err = None

    config_kwargs = {
        "temperature": 0.1 if expect_json else 0.7,
        "max_output_tokens": max_tokens,
    }
    if expect_json:
        config_kwargs["response_mime_type"] = "application/json"
        # 'low' is supported on both gemini-3.8-flash and gemini-3.5-flash-lite
        # (note: 'minimal' returns 400 on gemini-3.8-flash)
        config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level="low")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _rate_limit(tracker, RPM_FLASH)
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(**config_kwargs),
            )
            text = (resp.text or "").strip()
            if not text:
                raise GeminiError("Empty response from model")
            return text if not expect_json else _extract_json(text)

        except GeminiQuotaError:
            raise
        except Exception as e:
            last_err = e
            s = str(e)
            s_lower = s.lower()
            print(f"  [Gemini/{tracker}] attempt {attempt}/{MAX_RETRIES}: {s[:120]}")

            # Circuit-break immediately on retired/missing model (404 NOT_FOUND)
            if "404" in s or "not_found" in s_lower or "no longer available" in s_lower:
                for k in (KEY_K1, KEY_K2, KEY_K3):
                    _mark_model_dead(k, model)
                raise GeminiQuotaError(f"Model {model} unavailable (404)")

            # Circuit-break immediately on auth/permission error
            if any(x in s_lower for x in ["401", "403", "api_key_invalid", "permission_denied"]):
                _rpd_exhausted.add(key_env)
                raise GeminiQuotaError(f"Auth/permission error on {key_env}")

            # If a model rejects thinking_config or response_mime_type, strip and retry once
            if "400" in s and "thinking" in s_lower and "thinking_config" in config_kwargs:
                config_kwargs.pop("thinking_config", None)
                continue

            if any(x in s for x in ["429", "RESOURCE_EXHAUSTED", "quota"]):
                wait_secs, is_rpd = _parse_retry_delay(s)
                if is_rpd or "RESOURCE_EXHAUSTED" in s:
                    _mark_model_dead(key_env, model)
                    raise GeminiQuotaError(f"RPD exhausted on {tracker}/{model} ({key_env})")
                wait_secs = min(wait_secs, _MAX_SLEEP_CAP)
                print(f"  [Gemini/{tracker}] RPM hit — sleeping {wait_secs:.0f}s then retrying")
                time.sleep(wait_secs)
                continue

            if any(x in s for x in ["503", "UNAVAILABLE", "overloaded"]):
                wait = 20 * attempt
                print(f"  [Gemini/{tracker}] server busy — sleeping {wait}s")
                time.sleep(wait)
                continue

            if attempt < MAX_RETRIES:
                time.sleep(3 * attempt)

    raise GeminiError(f"{tracker} failed after {MAX_RETRIES} attempts: {last_err}")


def _available(key_env):
    """Key is available if set AND not fully exhausted this run."""
    return bool(os.environ.get(key_env, "").strip()) and key_env not in _rpd_exhausted


def _model_available(key_env, model):
    """Key+model is available if key is set and this specific model not exhausted."""
    if not os.environ.get(key_env, "").strip():
        return False
    if key_env in _rpd_exhausted:
        return False
    return not _model_exhausted.get((key_env, model), False)


def all_gemini_exhausted():
    """True if every configured Gemini key is exhausted (both models)."""
    keys = [KEY_K1, KEY_K2, KEY_K3]
    configured = [k for k in keys if os.environ.get(k, "").strip()]
    if not configured:
        return True

    def _key_fully_exhausted(k):
        if k in _rpd_exhausted:
            return True
        return (_model_exhausted.get((k, MODEL_PRIMARY), False) and
                _model_exhausted.get((k, MODEL_LITE), False))

    return all(_key_fully_exhausted(k) for k in configured)


def all_keys_exhausted():
    """
    True if ALL generation keys (Gemini K1/K2/K3 + Mistral + Groq K1/K2) are exhausted.
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
    if not groq_client.is_model_available(key_env, model):
        raise GeminiQuotaError(f"Groq {key_env}/{model} unavailable or exhausted")
    system = "You are an expert MCQ question setter. Generate high-quality questions exactly as instructed. Do NOT output JSON — write in plain numbered text format."
    return groq_client._call_one(
        key_env,
        system,
        prompt,
        max_tokens=min(max_tokens, 6000),
        model=model,
        expect_json=False,
    )


def _try_key_model_unified(client_type, key_env, model, prompt, tracker_label, max_tokens):
    """
    Attempt to run the prompt with a specific key and model.
    Returns (result_text, is_exhausted_error) or (None, is_exhausted_error).
    """
    import mistral_client
    import groq_client

    if client_type == "gemini":
        if not _model_available(key_env, model):
            return None, True
    elif client_type == "groq":
        if not groq_client.is_model_available(key_env, model):
            return None, True
    elif client_type == "mistral":
        if not mistral_client.available():
            return None, True

    print(f"  [UnifiedGen] Trying {client_type}/{key_env} model={model}...")

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
        print(f"  [UnifiedGen] 🛑 Quota/unavailable for {client_type}/{key_env} model={model}: {e}")
        if client_type == "gemini":
            _mark_model_dead(key_env, model)
        elif client_type == "mistral":
            mistral_client._exhausted = True
        return None, True

    except Exception as e:
        s = str(e)
        s_lower = s.lower()
        print(f"  [UnifiedGen] ❌ Error with {client_type}/{key_env} model={model}: {s[:150]}")
        is_permanent = (
            "RESOURCE_EXHAUSTED" in s
            or "quota" in s_lower
            or "tpd exhausted" in s_lower
            or "402" in s
            or "404" in s
            or "401" in s
            or "403" in s
            or "not available" in s_lower
            or "does not exist" in s_lower
            or "decommissioned" in s_lower
        )

        if is_permanent:
            if client_type == "gemini":
                _mark_model_dead(key_env, model)
            elif client_type == "groq":
                if not groq_client.is_model_available(key_env, model):
                    pass
                else:
                    groq_client._tpd_exhausted.add(key_env)
            elif client_type == "mistral":
                mistral_client._exhausted = True
            return None, True

        return None, False


# ── Public API ────────────────────────────────────────────────────────────────

def research(prompt, max_tokens=1500):
    """
    Research step — tries ALL available keys before giving up.
    Returns text or empty string (never raises).
    """
    tier1 = [
        ("gemini", KEY_K1, MODEL_PRIMARY, "research_k1_38"),
        ("gemini", KEY_K2, MODEL_PRIMARY, "research_k2_38"),
        ("gemini", KEY_K3, MODEL_PRIMARY, "research_k3_38"),
        ("mistral", "MISTRAL_API_KEY", "mistral-small-latest", "research_mistral"),
        ("groq", "GROQ_API_KEY",  GROQ_GEN_PRIMARY, "research_groq1_oss"),
        ("groq", "GROQ_API_KEY2", GROQ_GEN_PRIMARY, "research_groq2_oss"),
    ]

    tier2 = [
        ("gemini", KEY_K1, MODEL_LITE, "research_k1_lite"),
        ("gemini", KEY_K2, MODEL_LITE, "research_k2_lite"),
        ("gemini", KEY_K3, MODEL_LITE, "research_k3_lite"),
        ("groq", "GROQ_API_KEY",  GROQ_GEN_FALLBACK, "research_groq1_qwen"),
        ("groq", "GROQ_API_KEY2", GROQ_GEN_FALLBACK, "research_groq2_qwen"),
    ]

    import mistral_client
    import groq_client

    def _can_try(client_type, key_env, model):
        if not os.environ.get(key_env, "").strip():
            return False
        if client_type == "gemini":
            return _model_available(key_env, model)
        if client_type == "groq":
            return groq_client.is_model_available(key_env, model)
        if client_type == "mistral":
            return mistral_client.available()
        return False

    for client_type, key_env, model, label in (tier1 + tier2):
        if not _can_try(client_type, key_env, model):
            continue
        if _INTER_KEY_SLEEP > 0:
            time.sleep(_INTER_KEY_SLEEP)

        res, _ = _try_key_model_unified(
            client_type, key_env, model, prompt, label, max_tokens
        )
        if res and len(res) > 50:
            print(f"  [Research] ✅ Success with {client_type}/{key_env} model={model}")
            return res

    print("  [Research] All keys failed — using empty context")
    return ""


def generate_questions_for_chunk(prompt, chunk_index=0, max_tokens=8192):
    """
    Generate raw questions for ONE chunk (10Q).
    Rotates Tier 1 (gemini-3.8-flash + Mistral + Groq gpt-oss-120b) then
    Tier 2 (gemini-3.5-flash-lite + Groq qwen3.8-27b).
    Raises GeminiQuotaError ONLY when all keys are exhausted/unavailable.
    """
    tier1 = [
        ("gemini", KEY_K1, MODEL_PRIMARY, "gen_k1_38"),
        ("gemini", KEY_K2, MODEL_PRIMARY, "gen_k2_38"),
        ("gemini", KEY_K3, MODEL_PRIMARY, "gen_k3_38"),
        ("mistral", "MISTRAL_API_KEY", "mistral-small-latest", "gen_mistral"),
        ("groq", "GROQ_API_KEY",  GROQ_GEN_PRIMARY, "gen_groq1_oss"),
        ("groq", "GROQ_API_KEY2", GROQ_GEN_PRIMARY, "gen_groq2_oss"),
    ]

    tier2 = [
        ("gemini", KEY_K1, MODEL_LITE, "gen_k1_lite"),
        ("gemini", KEY_K2, MODEL_LITE, "gen_k2_lite"),
        ("gemini", KEY_K3, MODEL_LITE, "gen_k3_lite"),
        ("groq", "GROQ_API_KEY",  GROQ_GEN_FALLBACK, "gen_groq1_qwen"),
        ("groq", "GROQ_API_KEY2", GROQ_GEN_FALLBACK, "gen_groq2_qwen"),
    ]

    def is_set(key_env):
        return bool(os.environ.get(key_env, "").strip())

    t1_active = [item for item in tier1 if is_set(item[1])]
    t2_active = [item for item in tier2 if is_set(item[1])]

    if t1_active:
        shift1 = chunk_index % len(t1_active)
        t1_active = t1_active[shift1:] + t1_active[:shift1]
    if t2_active:
        shift2 = chunk_index % len(t2_active)
        t2_active = t2_active[shift2:] + t2_active[:shift2]

    attempts = t1_active + t2_active

    if not attempts:
        raise GeminiQuotaError("No generation keys are configured in the environment!")

    MAX_PASSES = 3
    last_err = "No keys available"

    for pass_idx in range(MAX_PASSES):
        if pass_idx > 0:
            print(f"  [Generate] Pass {pass_idx + 1}/{MAX_PASSES} — retrying transient/non-exhausted keys...")

        transient_keys_available = False

        for client_type, key_env, model, label in attempts:
            if client_type == "gemini":
                if not _model_available(key_env, model):
                    continue
            elif client_type == "groq":
                import groq_client
                if not groq_client.is_model_available(key_env, model):
                    continue
            elif client_type == "mistral":
                import mistral_client
                if not mistral_client.available():
                    continue

            transient_keys_available = True

            if _INTER_KEY_SLEEP > 0:
                time.sleep(_INTER_KEY_SLEEP)

            res, is_exhausted = _try_key_model_unified(
                client_type, key_env, model, prompt, label, max_tokens
            )

            if res:
                return res
            if not is_exhausted:
                last_err = f"Transient failure on {client_type}/{key_env}"

        if not transient_keys_available:
            print("  [Generate] All keys are permanently exhausted/unavailable. Skipping further passes.")
            break

        if pass_idx < MAX_PASSES - 1:
            wait_time = 15
            print(f"  [Generate] Pass {pass_idx + 1} failed. Sleeping {wait_time}s before next pass...")
            time.sleep(wait_time)

    if all_keys_exhausted():
        raise GeminiQuotaError("All 6 generation keys are fully exhausted (daily limits hit).")
    raise GeminiError(f"All keys/models failed in this run. Last error: {last_err}")


def structure_with_flash(prompt, max_tokens=8192):
    """
    Fallback JSON structurer — called when Groq K1/K2 and Mistral fail.
    Tries gemini-3.5-flash-lite first (high RPM/RPD, fast JSON), then gemini-3.8-flash.
    """
    attempts = [
        ("struct_k1_lite", KEY_K1, MODEL_LITE),
        ("struct_k2_lite", KEY_K2, MODEL_LITE),
        ("struct_k3_lite", KEY_K3, MODEL_LITE),
        ("struct_k1_38",   KEY_K1, MODEL_PRIMARY),
        ("struct_k2_38",   KEY_K2, MODEL_PRIMARY),
        ("struct_k3_38",   KEY_K3, MODEL_PRIMARY),
    ]
    for tracker, key_env, model in attempts:
        if not _model_available(key_env, model):
            continue
        try:
            result = _call(model, tracker, key_env, prompt, True, max_tokens)
            if result:
                print(f"  [StructureFlash] ✅ {tracker} ({model})")
                return result
        except Exception as e:
            print(f"  [StructureFlash] {tracker} failed ({e}) — trying next")

    raise GeminiError("Flash structuring failed on all keys (3.5-Flash-Lite + 3.8-Flash)")

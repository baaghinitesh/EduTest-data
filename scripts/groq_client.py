"""
groq_client.py — 2 Groq keys with Mistral fallback + smart chunking.

GitHub Secret names (exact):
  GROQ_API_KEY   → Groq K1 — primary structurer
  GROQ_API_KEY2  → Groq K2 — fallback structurer
  MISTRAL_API_KEY → Mistral — 3rd fallback structurer (before Flash)

Active Groq production models:
  MODEL          = "qwen/qwen3.8-27b"    (primary structurer — fast, reasoning_effort="none")
  MODEL_FALLBACK = "openai/gpt-oss-20b"  (fallback structurer — reasoning_effort="low")

Structuring chain per 5Q sub-chunk:
  Groq K1 (qwen3.8-27b) → Groq K2 (qwen3.8-27b) → Mistral
  → Groq K1 (gpt-oss-20b) → Groq K2 (gpt-oss-20b)
  → caller falls back to Flash via structure_with_flash

Circuit-breaking:
  404 / model_decommissioned → disables that model for the rest of the run (no retries)
  401 / 403 / invalid key    → disables that key for the rest of the run (no retries)
  429 TPM (wait ≤ 90s)       → sleeps stated wait time, retries same key
  429 TPD (wait > 90s)       → marks key TPD-exhausted for the rest of the run
"""
import os, re, time
from groq import Groq

from json_utils import extract_json

MODEL          = "qwen/qwen3.8-27b"
MODEL_FALLBACK = "openai/gpt-oss-20b"
MAX_RETRIES    = 4
MIN_GAP        = 3.0  # seconds between calls — stay under 8,000 TPM free tier

# ── Exact GitHub secret names ─────────────────────────────────────────────────
KEY_G1 = "GROQ_API_KEY"   # Groq K1 — primary
KEY_G2 = "GROQ_API_KEY2"  # Groq K2 — fallback

_clients         = {}
_last_call       = {}
_tpd_exhausted: set = set()   # keys that hit daily token limit this run
_auth_failed: set   = set()   # keys with 401/403 auth failure this run
_disabled_models: set = set() # models returning 404/decommissioned this run


class GroqError(Exception):
    pass


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_client(key_env):
    if key_env not in _clients:
        api_key = os.environ.get(key_env, "").strip()
        if not api_key:
            raise GroqError(f"{key_env} not set or empty")
        _clients[key_env] = Groq(api_key=api_key)
    return _clients[key_env]


def _rate_limit(key_env):
    elapsed = time.time() - _last_call.get(key_env, 0.0)
    if elapsed < MIN_GAP:
        time.sleep(MIN_GAP - elapsed)
    _last_call[key_env] = time.time()


def _extract_json(text):
    return extract_json(text, error_cls=GroqError)


def _parse_wait_seconds(err_str):
    """
    Parse wait time from Groq rate-limit error.
    Returns (seconds: float, is_tpd: bool).
    """
    m = re.search(r"try again in\s+(?:(\d+)h\s*)?(?:(\d+)m\s*)?(\d+\.?\d*)s",
                  err_str, re.IGNORECASE)
    if m:
        h    = int(m.group(1) or 0)
        mins = int(m.group(2) or 0)
        secs = float(m.group(3) or 0)
        total = h * 3600 + mins * 60 + secs
        return total, total > 90
    m = re.search(r"try again in (\d+\.?\d*)s", err_str, re.IGNORECASE)
    if m:
        secs = float(m.group(1))
        return secs, secs > 90
    return 45.0, False


def is_model_available(key_env, model=None):
    """True if key is configured, not exhausted/auth-failed, and model is active."""
    if not os.environ.get(key_env, "").strip():
        return False
    if key_env in _tpd_exhausted or key_env in _auth_failed:
        return False
    if model and model in _disabled_models:
        return False
    return True


def _key_available(key_env):
    return is_model_available(key_env)


def _reasoning_effort_for(model_name, expect_json):
    if model_name.startswith("qwen/"):
        return "none"
    if model_name.startswith("openai/gpt-oss"):
        return "low" if expect_json else "medium"
    return None


def _call_one(key_env, system_prompt, user_prompt, max_tokens, model=None, expect_json=False):
    """
    Call one Groq key with circuit-breaking & retry.
    - 404 / decommissioned → disable model immediately (no retry)
    - 401 / 403 → disable key immediately (no retry)
    - 429 TPM (short wait) → sleep stated time, retry same key
    - 429 TPD (long wait)  → mark key TPD-exhausted, raise GroqError immediately
    """
    call_model = model if model is not None else MODEL
    if not is_model_available(key_env, call_model):
        raise GroqError(f"Groq {key_env}/{call_model} unavailable or exhausted this run")

    client   = _get_client(key_env)
    last_err = None

    create_kwargs = {
        "model": call_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        "temperature": 0.15 if expect_json else 0.4,
        "max_tokens": max_tokens,
    }
    if expect_json:
        create_kwargs["response_format"] = {"type": "json_object"}
    effort = _reasoning_effort_for(call_model, expect_json)
    if effort is not None:
        create_kwargs["reasoning_effort"] = effort

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _rate_limit(key_env)
            resp = client.chat.completions.create(**create_kwargs)
            text = (resp.choices[0].message.content or "").strip()
            if not text:
                raise GroqError("Empty response")
            return text

        except GroqError:
            raise
        except Exception as e:
            last_err = e
            s = str(e)
            s_lower = s.lower()
            print(f"  [Groq/{key_env}] attempt {attempt}/{MAX_RETRIES}: {s[:150]}")

            # Circuit-break immediately on missing/decommissioned model
            if ("404" in s or "model_decommissioned" in s_lower
                    or "does not exist" in s_lower or "do not have access" in s_lower):
                _disabled_models.add(call_model)
                print(f"  [Groq] Model '{call_model}' unavailable (404) — disabling for this run")
                raise GroqError(f"Groq model '{call_model}' unavailable (404)")

            # Circuit-break immediately on auth errors
            if "401" in s or "403" in s or "invalid_api_key" in s_lower:
                _auth_failed.add(key_env)
                print(f"  [Groq/{key_env}] Auth failed — disabling key for this run")
                raise GroqError(f"Groq auth failed on {key_env}")

            # If an optional param (reasoning_effort / response_format) is rejected with 400, strip it
            if "400" in s:
                if "reasoning_effort" in s_lower and "reasoning_effort" in create_kwargs:
                    create_kwargs.pop("reasoning_effort", None)
                    continue
                if "response_format" in s_lower and "response_format" in create_kwargs:
                    create_kwargs.pop("response_format", None)
                    continue

            if "413" in s:
                raise GroqError("Prompt too large (413) — chunk smaller")

            if "429" in s or "rate_limit" in s_lower:
                wait, is_tpd = _parse_wait_seconds(s)
                if is_tpd:
                    _tpd_exhausted.add(key_env)
                    print(f"  [Groq/{key_env}] TPD exhausted — marking as done for this run")
                    raise GroqError(f"TPD exhausted on {key_env}")
                wait = min(wait, 65.0)
                print(f"  [Groq/{key_env}] TPM hit — sleeping {wait:.0f}s then retrying")
                time.sleep(wait)
                continue

            if attempt < MAX_RETRIES:
                time.sleep(4 * attempt)

    raise GroqError(f"Groq/{key_env} failed after {MAX_RETRIES} attempts: {last_err}")


def _sys(exam_name, subject_name=""):
    ctx = f"{exam_name} exam"
    if subject_name:
        ctx += f", {subject_name}"
    return (
        f"You are an expert educator specialising in the {ctx}. "
        "Output ONLY valid JSON matching the requested schema — no markdown or extra text."
    )


# ── Compact schema (wrapped in object for json_object mode) ───────────────────
_COMPACT_SCHEMA = (
    '{"questions":[{"question":"...","difficulty":"medium","explanation":"...",'
    '"hint":"","options":['
    '{"letter":"A","text":"...","isCorrect":true,"explanation":"..."},'
    '{"letter":"B","text":"...","isCorrect":false,"explanation":"..."},'
    '{"letter":"C","text":"...","isCorrect":false,"explanation":"..."},'
    '{"letter":"D","text":"...","isCorrect":false,"explanation":"..."}]}]}'
)


def _structure_chunk(raw_chunk, exam_name, subject_name, key_env, model=None):
    """
    Structure one small chunk of raw text (≤5 questions) into JSON.
    Returns list of question dicts or raises GroqError.
    """
    sys_prompt = _sys(exam_name, subject_name)
    user_prompt = (
        f"Extract ALL questions from the text below into JSON.\n"
        f"Output ONLY a valid JSON object matching this schema:\n{_COMPACT_SCHEMA}\n\n"
        f"Rules: include every question from TEXT, exactly ONE isCorrect:true per question, "
        f"difficulty is 'easy'/'medium'/'hard'.\n\n"
        f"TEXT:\n{raw_chunk}"
    )
    text = _call_one(
        key_env, sys_prompt, user_prompt,
        max_tokens=4096, model=model, expect_json=True
    )
    result = _extract_json(text)
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return [result]
    raise GroqError("Unexpected JSON structure")


def _split_raw_text(raw_text, questions_per_chunk=5):
    """
    Split raw numbered question text into sub-chunks of N questions each.
    Using 5Q keeps each call well inside Groq's 8,000 TPM free-tier ceiling.
    """
    pattern = re.compile(r'(?:^|\n)\s*(\d+)\s*[.)]\s', re.MULTILINE)
    matches = list(pattern.finditer(raw_text))

    if len(matches) < 2:
        return [raw_text]

    chunks = []
    for i in range(0, len(matches), questions_per_chunk):
        start = matches[i].start()
        end   = (matches[i + questions_per_chunk].start()
                 if (i + questions_per_chunk) < len(matches)
                 else len(raw_text))
        chunk = raw_text[start:end].strip()
        if chunk:
            chunks.append(chunk)
    return chunks if chunks else [raw_text]


# ── Public API ────────────────────────────────────────────────────────────────

def format_questions(raw_text, schema_example, exam_name, subject_name=""):
    """
    Structure raw question text into a list of question dicts.
    Splits into 5Q sub-chunks so Groq TPM/output limits are respected.
    Raises GroqError only if ALL sub-chunks fail on ALL keys/models.
    """
    chunks = _split_raw_text(raw_text, questions_per_chunk=5)
    print(f"  [Groq] Structuring {len(chunks)} sub-chunk(s)...")

    all_questions = []

    for chunk_idx, chunk in enumerate(chunks, 1):
        chunk_done = False

        # ── Step 1: Try both Groq keys with Primary Model ────────────────────
        for key_env in [KEY_G1, KEY_G2]:
            if not is_model_available(key_env, MODEL):
                continue
            try:
                qs = _structure_chunk(chunk, exam_name, subject_name, key_env, model=MODEL)
                if qs:
                    all_questions.extend(qs)
                    print(f"  [Groq] ✅ sub-chunk {chunk_idx}/{len(chunks)} — {len(qs)}Q via {key_env} ({MODEL})")
                    chunk_done = True
                    break
            except GroqError as e:
                err = str(e)
                if "413" in err or "too large" in err.lower():
                    print(f"  [Groq] sub-chunk {chunk_idx} too large even after split — skipping")
                    chunk_done = True
                    break
                print(f"  [Groq] {key_env} ({MODEL}) failed on sub-chunk {chunk_idx} ({err[:80]}) — trying next")

        # ── Step 2: Mistral fallback for this sub-chunk ──────────────────────
        if not chunk_done:
            try:
                import mistral_client
                if mistral_client.available():
                    print(f"  [Groq] Trying Mistral for sub-chunk {chunk_idx}")
                    qs = mistral_client.structure_questions(
                        chunk, _COMPACT_SCHEMA, exam_name, subject_name
                    )
                    if qs:
                        all_questions.extend(qs)
                        print(f"  [Groq] ✅ sub-chunk {chunk_idx}/{len(chunks)} — {len(qs)}Q via Mistral")
                        chunk_done = True
            except Exception as e:
                print(f"  [Groq] Mistral fallback failed on sub-chunk {chunk_idx} ({str(e)[:80]})")

        # ── Step 3: Try both Groq keys with Fallback Model ───────────────────
        if not chunk_done:
            for key_env in [KEY_G1, KEY_G2]:
                if not is_model_available(key_env, MODEL_FALLBACK):
                    continue
                try:
                    print(f"  [Groq] Trying {key_env} fallback ({MODEL_FALLBACK}) for sub-chunk {chunk_idx}")
                    qs = _structure_chunk(chunk, exam_name, subject_name, key_env, model=MODEL_FALLBACK)
                    if qs:
                        all_questions.extend(qs)
                        print(f"  [Groq] ✅ sub-chunk {chunk_idx}/{len(chunks)} — {len(qs)}Q via {key_env} ({MODEL_FALLBACK})")
                        chunk_done = True
                        break
                except GroqError as e:
                    print(f"  [Groq] {key_env} ({MODEL_FALLBACK}) failed on sub-chunk {chunk_idx} ({str(e)[:80]})")

        if not chunk_done:
            print(f"  [Groq] sub-chunk {chunk_idx} failed on Groq+Mistral — skipping")

    if not all_questions:
        raise GroqError("All sub-chunks failed on Groq + Mistral")

    return all_questions


def all_groq_exhausted():
    """True if every configured Groq key is exhausted, auth-failed, or all models disabled."""
    keys = [KEY_G1, KEY_G2]
    configured = [k for k in keys if os.environ.get(k, "").strip()]
    if not configured:
        return True
    if MODEL in _disabled_models and MODEL_FALLBACK in _disabled_models:
        return True
    return all(k in _tpd_exhausted or k in _auth_failed for k in configured)

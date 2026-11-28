"""
groq_client.py — 2 Groq keys (rotated) with Mistral + Gemini Flash fallback.

GitHub Secret names (exact):
  GROQ_API_KEY   → Groq K1
  GROQ_API_KEY2  → Groq K2
  MISTRAL_API_KEY → Mistral (optional)

Active Groq production models:
  MODEL          = "qwen/qwen3.8-27b"    (primary structurer — fast, reasoning_effort="none")
  MODEL_FALLBACK = "openai/gpt-oss-20b"  (fallback structurer — reasoning_effort="low")

Key improvements:
  - Rotates GROQ_API_KEY and GROQ_API_KEY2 across sub-chunks (doubles effective TPM).
  - Caps 5Q structuring max_tokens at 2500 so Groq's TPM estimator (input + max_tokens)
    doesn't reject calls with "429 Request too large".
  - Fails over immediately on 429 TPM / "Request too large" to the other Groq key,
    Mistral, gpt-oss-20b, or per-sub-chunk Flash fallback instead of sleeping 4×45s=180s
    and dropping questions!
  - Respects run_budget.safe_sleep / check_budget at every step.
"""
import os, re, time
from groq import Groq

from json_utils import extract_json
from run_budget import RunTimeoutError, check_budget, safe_sleep

MODEL          = "qwen/qwen3.8-27b"
MODEL_FALLBACK = "openai/gpt-oss-20b"
MAX_RETRIES    = 3
MIN_GAP        = 4.0  # seconds between calls per key (with 2 keys rotating, ~8s per key)

KEY_G1 = "GROQ_API_KEY"
KEY_G2 = "GROQ_API_KEY2"

_clients          = {}
_last_call        = {}
_subchunk_seq     = 0
_tpd_exhausted: set   = set()
_auth_failed: set     = set()
_disabled_models: set = set()


class GroqError(Exception):
    pass


class GroqRateLimitError(GroqError):
    """Transient TPM / request-size 429 — caller should rotate key/model immediately."""
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
        safe_sleep(MIN_GAP - elapsed, f"Groq/{key_env} pacing")
    _last_call[key_env] = time.time()


def _extract_json(text):
    return extract_json(text, error_cls=GroqError)


def _parse_wait_seconds(err_str):
    """
    Parse wait time from Groq rate-limit error.
    Returns (seconds: float | None, is_tpd: bool).
    """
    s_lower = err_str.lower()
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
    is_tpd = ("tokens per day" in s_lower or "requests per day" in s_lower or "tpd" in s_lower)
    return None, is_tpd


def is_model_available(key_env, model=None):
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
    Call one Groq key with circuit-breaking & fast TPM failover.
    """
    check_budget(f"Groq/{key_env}")
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
        check_budget(f"Groq/{key_env} attempt {attempt}")
        try:
            _rate_limit(key_env)
            resp = client.chat.completions.create(**create_kwargs)
            text = (resp.choices[0].message.content or "").strip()
            if not text:
                raise GroqError("Empty response")
            return text

        except (GroqError, RunTimeoutError):
            raise
        except Exception as e:
            last_err = e
            s = str(e)
            s_lower = s.lower()
            print(f"  [Groq/{key_env}] attempt {attempt}/{MAX_RETRIES}: {s[:150]}")

            if ("404" in s or "model_decommissioned" in s_lower
                    or "does not exist" in s_lower or "do not have access" in s_lower):
                _disabled_models.add(call_model)
                print(f"  [Groq] Model '{call_model}' unavailable (404) — disabling for this run")
                raise GroqError(f"Groq model '{call_model}' unavailable (404)")

            if "401" in s or "403" in s or "invalid_api_key" in s_lower:
                _auth_failed.add(key_env)
                print(f"  [Groq/{key_env}] Auth failed — disabling key for this run")
                raise GroqError(f"Groq auth failed on {key_env}")

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

                # "Request too large for model ... TPM" means prompt + max_tokens exceeded
                # the current minute budget — fail fast so caller rotates key/model immediately.
                if "request too large" in s_lower or wait is None:
                    raise GroqRateLimitError(
                        f"Groq TPM/window limit on {key_env}/{call_model} — rotating"
                    )

                if wait <= 15.0 and attempt == 1:
                    print(f"  [Groq/{key_env}] TPM hit — sleeping {wait:.0f}s then retrying once")
                    safe_sleep(wait + 0.5, f"Groq/{key_env} TPM")
                    continue

                raise GroqRateLimitError(
                    f"Groq TPM wait {wait:.0f}s on {key_env}/{call_model} — rotating"
                )

            if attempt < MAX_RETRIES:
                safe_sleep(3 * attempt, f"Groq/{key_env} retry")

    raise GroqError(f"Groq/{key_env} failed after {MAX_RETRIES} attempts: {last_err}")


def _sys(exam_name, subject_name=""):
    ctx = f"{exam_name} exam"
    if subject_name:
        ctx += f", {subject_name}"
    return (
        f"You are an expert educator specialising in the {ctx}. "
        "Output ONLY valid JSON matching the requested schema — no markdown or extra text."
    )


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
    Structure one small sub-chunk of raw text (≤5 questions) into JSON.
    Uses max_tokens=2500 so Groq's TPM estimator stays well below 6k-8k TPM limits.
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
        max_tokens=2500, model=model, expect_json=True
    )
    result = _extract_json(text)
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return [result]
    raise GroqError("Unexpected JSON structure")


def _split_raw_text(raw_text, questions_per_chunk=5):
    """Split raw numbered question text into sub-chunks of N questions each."""
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


def _flash_structure_subchunk(raw_chunk):
    """Fallback to Gemini Flash for a single sub-chunk so questions aren't dropped."""
    import gemini_client
    flash_prompt = (
        f"Extract ALL questions from the text below.\n"
        f"Output ONLY a valid JSON object matching this schema:\n{_COMPACT_SCHEMA}\n"
        f"Rules: ONE isCorrect:true per question, difficulty is 'easy'/'medium'/'hard'.\n\n"
        f"TEXT:\n{raw_chunk}"
    )
    qs = gemini_client.structure_with_flash(flash_prompt, max_tokens=4096)
    if isinstance(qs, dict):
        return [qs]
    return qs if isinstance(qs, list) else []


def _try_structure_subchunk(chunk, chunk_idx, total_chunks, exam_name, subject_name, keys_order):
    """Try Groq primary (both keys) → Mistral → Groq fallback (both keys) for one sub-chunk."""
    # Step 1: Primary Groq model across rotated keys
    for key_env in keys_order:
        if not is_model_available(key_env, MODEL):
            continue
        try:
            qs = _structure_chunk(chunk, exam_name, subject_name, key_env, model=MODEL)
            if qs:
                print(f"  [Groq] ✅ sub-chunk {chunk_idx}/{total_chunks} — {len(qs)}Q via {key_env} ({MODEL})")
                return qs
        except RunTimeoutError:
            raise
        except GroqError as e:
            print(f"  [Groq] {key_env} ({MODEL}) failed on sub-chunk {chunk_idx} ({str(e)[:80]}) — trying next")

    # Step 2: Mistral fallback
    try:
        import mistral_client
        if mistral_client.available():
            print(f"  [Groq] Trying Mistral for sub-chunk {chunk_idx}")
            qs = mistral_client.structure_questions(
                chunk, _COMPACT_SCHEMA, exam_name, subject_name
            )
            if qs:
                print(f"  [Groq] ✅ sub-chunk {chunk_idx}/{total_chunks} — {len(qs)}Q via Mistral")
                return qs
    except RunTimeoutError:
        raise
    except Exception as e:
        print(f"  [Groq] Mistral fallback failed on sub-chunk {chunk_idx} ({str(e)[:80]})")

    # Step 3: Fallback Groq model across reversed keys
    for key_env in reversed(keys_order):
        if not is_model_available(key_env, MODEL_FALLBACK):
            continue
        try:
            print(f"  [Groq] Trying {key_env} fallback ({MODEL_FALLBACK}) for sub-chunk {chunk_idx}")
            qs = _structure_chunk(chunk, exam_name, subject_name, key_env, model=MODEL_FALLBACK)
            if qs:
                print(f"  [Groq] ✅ sub-chunk {chunk_idx}/{total_chunks} — {len(qs)}Q via {key_env} ({MODEL_FALLBACK})")
                return qs
        except RunTimeoutError:
            raise
        except GroqError as e:
            print(f"  [Groq] {key_env} ({MODEL_FALLBACK}) failed on sub-chunk {chunk_idx} ({str(e)[:80]})")

    return None


# ── Public API ────────────────────────────────────────────────────────────────

def format_questions(raw_text, schema_example, exam_name, subject_name=""):
    """
    Structure raw question text into a list of question dicts.
    Rotates GROQ_API_KEY and GROQ_API_KEY2 per sub-chunk, falls back per-sub-chunk
    to Mistral / gpt-oss-20b / Gemini Flash, and sleeps briefly before one final
    retry pass if TPM limits blocked all providers on a sub-chunk.
    """
    global _subchunk_seq
    check_budget("Groq format_questions")

    chunks = _split_raw_text(raw_text, questions_per_chunk=5)
    print(f"  [Groq] Structuring {len(chunks)} sub-chunk(s)...")

    all_questions = []

    for chunk_idx, chunk in enumerate(chunks, 1):
        check_budget(f"Groq sub-chunk {chunk_idx}")
        keys_order = [KEY_G1, KEY_G2] if (_subchunk_seq % 2 == 0) else [KEY_G2, KEY_G1]
        _subchunk_seq += 1

        qs = _try_structure_subchunk(
            chunk, chunk_idx, len(chunks), exam_name, subject_name, keys_order
        )

        # Step 4: Per-sub-chunk Flash fallback before giving up or sleeping
        if not qs:
            try:
                print(f"  [Groq] sub-chunk {chunk_idx} → trying Gemini Flash fallback...")
                qs = _flash_structure_subchunk(chunk)
                if qs:
                    print(f"  [Groq] ✅ sub-chunk {chunk_idx}/{len(chunks)} — {len(qs)}Q via Flash fallback")
            except RunTimeoutError:
                raise
            except Exception as e:
                print(f"  [Groq] Flash fallback failed on sub-chunk {chunk_idx} ({str(e)[:80]})")

        # Step 5: If TPM blocked everything, sleep 20s for Groq TPM window reset and retry once
        if not qs and not all_groq_exhausted():
            print(f"  [Groq] sub-chunk {chunk_idx} rate-limited everywhere — sleeping 20s then retrying...")
            safe_sleep(20, f"Groq sub-chunk {chunk_idx} TPM reset")
            qs = _try_structure_subchunk(
                chunk, chunk_idx, len(chunks), exam_name, subject_name, list(reversed(keys_order))
            )

        if qs:
            all_questions.extend(qs)
        else:
            print(f"  [Groq] sub-chunk {chunk_idx} failed on all keys & models — skipping")

    if not all_questions:
        raise GroqError("All sub-chunks failed on Groq + Mistral + Flash")

    return all_questions


def all_groq_exhausted():
    keys = [KEY_G1, KEY_G2]
    configured = [k for k in keys if os.environ.get(k, "").strip()]
    if not configured:
        return True
    if MODEL in _disabled_models and MODEL_FALLBACK in _disabled_models:
        return True
    return all(k in _tpd_exhausted or k in _auth_failed for k in configured)

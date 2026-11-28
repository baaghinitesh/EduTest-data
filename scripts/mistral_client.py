"""
mistral_client.py — Mistral AI fallback for generation and structuring.

GitHub Secret name (exact):
  MISTRAL_API_KEY  → Mistral API key — used in rotation / fallback

Circuit-breaking & time budget:
  402 (subscription/billing required), 401/403 (auth), 404 (model missing)
  → immediately disable Mistral for the rest of the run (no retries/sleeps).
  RunTimeoutError → propagated immediately.
"""
import os, time

try:
    from mistralai.client import Mistral  # v2.x / v3.x
except ImportError:
    from mistralai import Mistral         # v1.x

from json_utils import extract_json
from run_budget import RunTimeoutError, check_budget, safe_sleep

MODEL       = "mistral-small-latest"
MAX_RETRIES = 3
MIN_GAP     = 1.2

KEY_ENV = "MISTRAL_API_KEY"

_client    = None
_last_call = 0.0
_exhausted = False


class MistralError(Exception):
    pass


class MistralQuotaError(MistralError):
    pass


def _get_client():
    global _client
    if _client is None:
        api_key = os.environ.get(KEY_ENV, "").strip()
        if not api_key:
            raise MistralError(f"Secret '{KEY_ENV}' not set or empty")
        _client = Mistral(api_key=api_key)
    return _client


def _rate_limit():
    global _last_call
    elapsed = time.time() - _last_call
    if elapsed < MIN_GAP:
        safe_sleep(MIN_GAP - elapsed, "Mistral pacing")
    _last_call = time.time()


def _extract_json(text):
    return extract_json(text, error_cls=MistralError)


def _call(system_prompt, user_prompt, expect_json=False, max_tokens=8192, model=None):
    global _exhausted
    check_budget("Mistral")
    if _exhausted:
        raise MistralQuotaError("Mistral already disabled/exhausted this run")

    client     = _get_client()
    last_err   = None
    call_model = model if model is not None else MODEL

    call_temp = 0.15 if expect_json else 0.7
    extra_args = {}
    if expect_json:
        extra_args["response_format"] = {"type": "json_object"}

    for attempt in range(1, MAX_RETRIES + 1):
        check_budget(f"Mistral attempt {attempt}")
        try:
            _rate_limit()
            resp = client.chat.complete(
                model=call_model,
                messages=[
                    {"role": "system",  "content": system_prompt},
                    {"role": "user",    "content": user_prompt},
                ],
                temperature=call_temp,
                max_tokens=max_tokens,
                **extra_args
            )
            text = (resp.choices[0].message.content or "").strip()
            if not text:
                raise MistralError("Empty response from Mistral")
            return _extract_json(text) if expect_json else text

        except (MistralQuotaError, RunTimeoutError):
            raise
        except Exception as e:
            last_err = e
            s = str(e)
            s_lower = s.lower()
            print(f"  [Mistral] attempt {attempt}/{MAX_RETRIES}: {s[:120]}")

            if any(x in s_lower for x in [
                "status 402", "subscription", "401", "403", "unauthorized", "404"
            ]):
                _exhausted = True
                print("  [Mistral] Account/billing/auth error — disabling Mistral for this run")
                raise MistralQuotaError(f"Mistral unavailable: {s[:80]}")

            if any(x in s_lower for x in ["429", "rate_limit", "quota", "too many"]):
                _exhausted = True
                print("  [Mistral] Quota/rate-limit hit — marking Mistral exhausted for this run")
                raise MistralQuotaError(f"Mistral quota/rate-limit hit: {s[:80]}")

            if any(x in s_lower for x in ["503", "502", "unavailable"]):
                wait = 10 * attempt
                print(f"  [Mistral] server busy — sleeping {wait}s")
                safe_sleep(wait, "Mistral 503")
                continue

            if attempt < MAX_RETRIES:
                safe_sleep(3 * attempt, "Mistral retry")
    raise MistralError(f"Mistral failed after {MAX_RETRIES} attempts: {last_err}")


def available():
    return bool(os.environ.get(KEY_ENV, "").strip()) and not _exhausted


def _is_exhausted():
    return _exhausted


def generate_questions(prompt, max_tokens=8192, model=None):
    if not os.environ.get(KEY_ENV, "").strip():
        raise MistralError(f"{KEY_ENV} not set — Mistral unavailable")
    if _exhausted:
        raise MistralQuotaError("Mistral already exhausted this run")

    system = (
        "You are an expert MCQ question setter for competitive and school exams. "
        "Generate high-quality questions exactly as instructed. "
        "Do NOT output JSON — write in plain numbered text format."
    )
    result = _call(system, prompt, expect_json=False, max_tokens=max_tokens, model=model)
    if result and len(result) > 80:
        print(f"  [Generate] ✅ mistral ({model or MODEL})")
        return result
    raise MistralError("Mistral response too short")


def structure_questions(raw_text, schema, exam_name, subject_name="", model=None):
    if not os.environ.get(KEY_ENV, "").strip():
        raise MistralError(f"{KEY_ENV} not set — Mistral unavailable")
    if _exhausted:
        raise MistralQuotaError("Mistral already exhausted this run")

    ctx = f"{exam_name} exam" + (f", {subject_name}" if subject_name else "")
    system = (
        f"You are an expert educator specialising in the {ctx}. "
        "Output ONLY a valid JSON object matching the schema — no markdown or extra text."
    )
    user = (
        f"Extract ALL questions from the text below into JSON.\n"
        f"Output ONLY a JSON object with a 'questions' array. Schema:\n{schema}\n\n"
        f"Rules: exactly ONE isCorrect:true per question, "
        f"difficulty is 'easy'/'medium'/'hard'.\n\n"
        f"TEXT:\n{raw_text}"
    )
    result = _call(system, user, expect_json=True, max_tokens=4096, model=model)
    if isinstance(result, list) and result:
        print(f"  [Structure] ✅ Mistral fallback — {len(result)}Q ({model or MODEL})")
        return result
    if isinstance(result, dict):
        return [result]
    raise MistralError("Mistral returned unexpected JSON structure")


def research(prompt, max_tokens=1500, model=None):
    if not os.environ.get(KEY_ENV, "").strip():
        return ""
    if _exhausted:
        print("  [Research] Mistral already exhausted this run — skipping")
        return ""
    try:
        system = (
            "You are a senior exam expert. Provide concise, focused analysis "
            "for question setters. Be specific and exam-focused."
        )
        result = _call(system, prompt, expect_json=False, max_tokens=max_tokens, model=model)
        if result and len(result) > 50:
            print(f"  [Research] ✅ mistral fallback ({model or MODEL})")
            return result
    except RunTimeoutError:
        raise
    except Exception as e:
        print(f"  [Research] Mistral failed ({e}) — using empty context")
    return ""

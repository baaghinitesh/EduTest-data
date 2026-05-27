"""
mistral_client.py — Mistral AI fallback for generation and structuring.

GitHub Secret name (exact):
  MISTRAL_API_KEY  → Mistral API key — used when ALL Gemini keys are exhausted

Role in the pipeline:
  Generation  : 4th fallback after Gemini K3 → K1 → K2 all fail
  Structuring : 3rd fallback after Groq K1 → K2 both fail (before Flash)
  Research    : 3rd fallback after Gemini K1 → K2 both fail

Model used:
  mistral-small-latest  — fast, cheap, good quality for MCQ generation
  (free tier: 1 req/s, no hard daily limit on free API keys)

API: OpenAI-compatible — https://api.mistral.ai/v1/chat/completions
"""
import os, json, re, time

# Support both mistralai v1.x (from mistralai import Mistral)
# and v2.x (from mistralai.client import Mistral — same API, different import path)
try:
    from mistralai.client import Mistral  # v2.x
except ImportError:
    from mistralai import Mistral         # v1.x

MODEL       = "mistral-small-latest"   # fast + good quality
MAX_RETRIES = 3
MIN_GAP     = 1.2   # 1 req/s free tier → 1.2s gap to be safe

KEY_ENV = "MISTRAL_API_KEY"

_client    = None
_last_call = 0.0
_exhausted = False   # set True when Mistral hits quota/rate-limit this run


class MistralError(Exception):
    pass


class MistralQuotaError(MistralError):
    pass


# ── Internal helpers ──────────────────────────────────────────────────────────

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
        time.sleep(MIN_GAP - elapsed)
    _last_call = time.time()


def _extract_json(text):
    """Robustly extract JSON array or object from Mistral response."""
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text).strip()
    if not text.startswith("["):
        idx = text.find("[")
        if idx != -1:
            text = text[idx:]
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
    raise MistralError("No valid JSON found in Mistral response")


def _call(system_prompt, user_prompt, expect_json=False, max_tokens=4096, model=None):
    """Call Mistral with retry. Returns text or parsed JSON."""
    global _exhausted
    if _exhausted:
        raise MistralQuotaError("Mistral already exhausted this run")
    
    client   = _get_client()
    last_err = None
    call_model = model if model is not None else MODEL
    
    # Use deterministic low temperature for structuring/JSON tasks
    call_temp = 0.15 if expect_json else 0.7
    extra_args = {}
    if expect_json:
        extra_args["response_format"] = {"type": "json_object"}

    for attempt in range(1, MAX_RETRIES + 1):
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

        except MistralError:
            raise
        except Exception as e:
            last_err = e
            s = str(e)
            print(f"  [Mistral] attempt {attempt}/{MAX_RETRIES}: {s[:120]}")
            if any(x in s for x in ["429", "rate_limit", "quota", "too many"]):
                _exhausted = True
                print(f"  [Mistral] Quota/rate-limit hit — marking Mistral exhausted for this run")
                raise MistralQuotaError(f"Mistral quota/rate-limit hit: {s[:80]}")
            if any(x in s for x in ["503", "502", "unavailable"]):
                wait = 15 * attempt
                print(f"  [Mistral] server busy — sleeping {wait}s")
                time.sleep(wait)
                continue
            if attempt < MAX_RETRIES:
                time.sleep(5 * attempt)
    raise MistralError(f"Mistral failed after {MAX_RETRIES} attempts: {last_err}")


def available():
    """Return True if MISTRAL_API_KEY is set and not exhausted this run."""
    return bool(os.environ.get(KEY_ENV, "").strip()) and not _exhausted


def _is_exhausted():
    """Return True if Mistral hit quota/rate-limit this run."""
    return _exhausted


# ── Public API ────────────────────────────────────────────────────────────────

def generate_questions(prompt, max_tokens=4096, model=None):
    """
    Generate raw question text using Mistral.
    Called when all 3 Gemini keys are exhausted, or as part of rotation.
    Returns raw text string or raises MistralError.
    """
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
    """
    Structure raw question text into JSON array using Mistral.
    Called when both Groq keys fail, before Flash fallback.
    Returns list of question dicts or raises MistralError.
    """
    if not os.environ.get(KEY_ENV, "").strip():
        raise MistralError(f"{KEY_ENV} not set — Mistral unavailable")
    if _exhausted:
        raise MistralQuotaError("Mistral already exhausted this run")

    ctx = f"{exam_name} exam" + (f", {subject_name}" if subject_name else "")
    system = (
        f"You are an expert educator specialising in the {ctx}. "
        "Output ONLY valid JSON — no markdown, no text outside the JSON array."
    )
    user = (
        f"Extract questions from the text below. "
        f"Output ONLY a JSON array. Schema: {schema}\n\n"
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
    """
    Research step using Mistral — fallback when Gemini keys fail.
    Returns text or empty string (never raises).
    """
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
    except Exception as e:
        print(f"  [Research] Mistral failed ({e}) — using empty context")
    return ""

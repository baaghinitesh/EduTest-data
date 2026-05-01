"""
groq_client.py — 2 Groq keys with fallback.

GitHub Secret names (exact — verified from repo settings):
  GROQ_API_KEY   → Groq K1 — primary structurer
  GROQ_API_KEY2  → Groq K2 — fallback structurer  (no underscore before 2)

If both Groq keys fail, caller falls back to gemini_client.structure_with_flash().
Free tier: 14,400 RPD, 6,000 TPM — essentially unlimited for our use.
"""
import os, json, re, time
from groq import Groq

MODEL       = "llama-3.3-70b-versatile"
MAX_RETRIES = 3
MIN_GAP     = 2.0   # seconds between calls

# ── Exact GitHub secret names ─────────────────────────────────────────────────
KEY_G1 = "GROQ_API_KEY"   # Groq K1 — primary
KEY_G2 = "GROQ_API_KEY2"  # Groq K2 — fallback (no underscore before 2)

_clients   = {}
_last_call = {}   # key_env → float timestamp


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
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for start, end in [("{", "}"), ("[", "]")]:
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
    raise GroqError("No valid JSON in Groq response")


def _call_one(key_env, system_prompt, user_prompt, expect_json, max_tokens):
    """Call one Groq key with retry. Returns result or raises GroqError."""
    client   = _get_client(key_env)
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _rate_limit(key_env)
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
                ],
                temperature=0.5,
                max_tokens=max_tokens,
            )
            text = (resp.choices[0].message.content or "").strip()
            if not text:
                raise GroqError("Empty response")
            return text if not expect_json else _extract_json(text)

        except GroqError:
            raise
        except Exception as e:
            last_err = e
            s = str(e)
            print(f"  [Groq/{key_env}] attempt {attempt}/{MAX_RETRIES}: {s[:120]}")
            if "413" in s:
                raise GroqError("Prompt too large (413)")
            if "429" in s or "rate_limit" in s.lower():
                m = re.search(r"try again in (\d+\.?\d*)s", s, re.IGNORECASE)
                wait = float(m.group(1)) + 2 if m else 45
                print(f"  [Groq/{key_env}] rate limited — sleeping {wait:.0f}s")
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
        "Generate accurate, exam-standard content. "
        "ALWAYS respond with valid JSON only — no markdown fences, no text outside JSON."
    )


def _key_available(key_env):
    return bool(os.environ.get(key_env, "").strip())


# ── Public API ────────────────────────────────────────────────────────────────

def format_questions(raw_text, schema_example, exam_name, subject_name=""):
    """
    Structure raw question text into JSON array.
    GROQ_API_KEY → GROQ_API_KEY2 → raises GroqError (caller falls back to Flash).
    """
    prompt = (
        f"Extract ALL questions, options, correct answers, and explanations from the text below.\n"
        f"Output ONLY a valid JSON array matching this schema exactly:\n{schema_example}\n"
        f"Start your response with [ and end with ]. No other text.\n\n"
        f"{raw_text}\n["
    )
    sys = _sys(exam_name, subject_name)

    for key_env in [KEY_G1, KEY_G2]:
        if not _key_available(key_env):
            print(f"  [Groq] {key_env} not set — skipping")
            continue
        try:
            result = _call_one(key_env, sys, prompt, expect_json=True, max_tokens=8000)
            if result:
                print(f"  [Groq] ✅ {key_env} structured successfully")
                return result
        except GroqError as e:
            print(f"  [Groq] {key_env} failed ({e}) — trying next key")

    raise GroqError("Both Groq keys failed — caller should fall back to Flash")

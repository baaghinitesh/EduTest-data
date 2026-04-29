"""
groq_client.py — Groq API wrapper for notes and formulas.

Free tier: 14,400 RPD, 6,000 tokens/minute — essentially unlimited.
Models:
  llama-3.3-70b-versatile  → notes (high quality, detailed)
  llama-3.1-8b-instant     → formulas (fast, precise)

Graceful failure: raises GroqError so caller can skip item and continue.
"""
import os, json, re, time
from groq import Groq

MODEL_NOTES    = "llama-3.3-70b-versatile"
MODEL_FORMULAS = "llama-3.1-8b-instant"

MAX_RETRIES = 2
_client = None
_last_call = 0.0
TPM_LIMIT = 6000   # tokens per minute free tier
MIN_GAP = 2.0      # minimum 2s between calls to stay under TPM


class GroqError(Exception):
    """Raised when Groq call fails after retries."""
    pass


def _get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            raise GroqError("GROQ_API_KEY environment variable not set")
        _client = Groq(api_key=api_key)
    return _client


def _rate_limit_sleep():
    global _last_call
    elapsed = time.time() - _last_call
    if elapsed < MIN_GAP:
        time.sleep(MIN_GAP - elapsed)
    _last_call = time.time()


def _extract_json(text):
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        idx = text.find(start_char)
        if idx == -1:
            continue
        depth = 0
        for i, ch in enumerate(text[idx:], idx):
            if ch == start_char:
                depth += 1
            elif ch == end_char:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[idx:i+1])
                    except json.JSONDecodeError:
                        break
    raise GroqError("No valid JSON found in Groq response")


def _call(model, prompt, expect_json, max_tokens):
    client = _get_client()
    last_err = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _rate_limit_sleep()
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are an expert medical educator specializing in NORCET exam preparation. "
                            "You generate accurate, high-quality educational content. "
                            "Always respond with valid JSON only — no markdown fences, no extra text."
                        )
                    },
                    {"role": "user", "content": prompt}
                ],
                temperature=0.6,
                max_tokens=max_tokens,
            )
            text = response.choices[0].message.content
            if not expect_json:
                return text
            return _extract_json(text)

        except GroqError:
            raise

        except Exception as e:
            last_err = e
            err_str = str(e)
            print(f"  [Groq] Attempt {attempt} failed: {err_str[:120]}")

            if "rate_limit" in err_str.lower() or "429" in err_str:
                # Parse retry-after if available
                retry_match = re.search(r"try again in (\d+\.?\d*)s", err_str, re.IGNORECASE)
                wait = float(retry_match.group(1)) + 2 if retry_match else 30
                print(f"  [Groq] Rate limited — waiting {wait:.0f}s...")
                time.sleep(wait)
            elif attempt < MAX_RETRIES:
                time.sleep(3 * attempt)

    raise GroqError(f"Groq failed after {MAX_RETRIES} attempts: {last_err}")


def generate_note(prompt, expect_json=True):
    """
    Use llama-3.3-70b for high-quality notes.
    Raises GroqError on failure — caller should catch and skip.
    """
    return _call(MODEL_NOTES, prompt, expect_json, max_tokens=8000)


def generate_formula(prompt, expect_json=True):
    """
    Use llama-3.1-8b for fast, precise formula sheets.
    Raises GroqError on failure — caller should catch and skip.
    """
    return _call(MODEL_FORMULAS, prompt, expect_json, max_tokens=6000)

"""
groq_client.py — Groq API wrapper for notes and formulas.

Free tier: 14,400 RPD, 6,000 TPM per model.
Models:
  llama-3.3-70b-versatile → notes AND formulas
    (8B instant has only 6K TPM — our prompts exceed that limit)

Graceful failure: raises GroqError so caller can skip and continue.
"""
import os, json, re, time
from groq import Groq

MODEL_NOTES    = "llama-3.3-70b-versatile"
MODEL_FORMULAS = "llama-3.3-70b-versatile"  # 8B TPM too low for our prompts

MAX_RETRIES = 3
MIN_GAP     = 2.0   # minimal gap — external 60s delays handle rate limiting

_client    = None
_last_call = 0.0


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
                            "You are an expert medical educator specializing in NORCET nursing exam. "
                            "Generate accurate, comprehensive educational content. "
                            "ALWAYS respond with valid JSON only — no markdown fences, no extra text outside JSON."
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
            err_str  = str(e)
            print(f"  [Groq] Attempt {attempt}/{MAX_RETRIES} failed: {err_str[:120]}")

            # Prompt too large (413) — no point retrying
            if "413" in err_str:
                raise GroqError(f"Prompt too large for Groq (413): {err_str[:80]}")

            # Rate limited (429) — wait and retry
            if "429" in err_str or "rate_limit" in err_str.lower() or "rate limit" in err_str.lower():
                retry_match = re.search(r"try again in (\d+\.?\d*)s", err_str, re.IGNORECASE)
                wait = float(retry_match.group(1)) + 2 if retry_match else 45
                print(f"  [Groq] Rate limited (429) — waiting {wait:.0f}s...")
                time.sleep(wait)
                continue

            if attempt < MAX_RETRIES:
                time.sleep(4 * attempt)

    raise GroqError(f"Groq failed after {MAX_RETRIES} attempts: {last_err}")


def generate_note(prompt, expect_json=True):
    """llama-3.3-70b for comprehensive notes."""
    return _call(MODEL_NOTES, prompt, expect_json, max_tokens=8000)


def generate_formula(prompt, expect_json=True):
    """llama-3.3-70b for formula/quick-reference sheets."""
    return _call(MODEL_FORMULAS, prompt, expect_json, max_tokens=5000)


def expand_content(existing_html, topic, subj_name, content_type="note"):
    """
    Expand existing HTML content that is too short.
    Returns additional HTML to append to existing content.
    """
    prompt = f"""The following {content_type} about {topic} ({subj_name}) is incomplete and needs more content.

EXISTING CONTENT:
{existing_html[:2000]}

Please CONTINUE and EXPAND this content by adding:
- More detailed explanations for each section
- Additional clinical examples and case scenarios
- More specific drug doses and normal values in <code> tags
- Additional mnemonics and memory aids in <em> tags
- More exam-focused key points
- Any missing sections from: Classification, Pathophysiology, Clinical Features, Management, Nursing Care, Complications

Write ONLY the additional HTML to append (starting with <h3> or <p>).
Do NOT repeat what's already written. Just add new content.
Write at least 500 more words of detailed HTML."""

    return _call(MODEL_NOTES, prompt, expect_json=False, max_tokens=4000)

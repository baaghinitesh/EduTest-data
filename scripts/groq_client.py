"""
groq_client.py — Groq API wrapper. Dynamic system prompt (not NORCET-specific).

Free tier: 14,400 RPD, 6,000 TPM — essentially unlimited for our use.
Model: llama-3.3-70b-versatile for all content (notes, formulas, resources, formatter).
"""
import os, json, re, time
from groq import Groq

MODEL = "llama-3.3-70b-versatile"
MAX_RETRIES = 3
MIN_GAP     = 2.0

_client    = None
_last_call = 0.0


class GroqError(Exception):
    pass


def _get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            raise GroqError("GROQ_API_KEY not set")
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
    text = re.sub(r"```", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for s, e in [("{", "}"), ("[", "]")]:
        idx = text.find(s)
        if idx == -1:
            continue
        depth = 0
        for i, ch in enumerate(text[idx:], idx):
            if ch == s:
                depth += 1
            elif ch == e:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[idx:i+1])
                    except json.JSONDecodeError:
                        break
    raise GroqError("No valid JSON in Groq response")


def _call(prompt, expect_json, max_tokens, system_prompt):
    client   = _get_client()
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _rate_limit_sleep()
            response = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": prompt},
                ],
                temperature=0.6,
                max_tokens=max_tokens,
            )
            text = response.choices[0].message.content
            return text if not expect_json else _extract_json(text)

        except GroqError:
            raise
        except Exception as e:
            last_err = e
            err_str  = str(e)
            print(f"  [Groq] Attempt {attempt}/{MAX_RETRIES} failed: {err_str[:120]}")
            if "413" in err_str:
                raise GroqError(f"Prompt too large (413)")
            if "429" in err_str or "rate_limit" in err_str.lower():
                m = re.search(r"try again in (\d+\.?\d*)s", err_str, re.IGNORECASE)
                wait = float(m.group(1)) + 2 if m else 45
                print(f"  [Groq] Rate limited — waiting {wait:.0f}s...")
                time.sleep(wait)
                continue
            if attempt < MAX_RETRIES:
                time.sleep(4 * attempt)
    raise GroqError(f"Groq failed after {MAX_RETRIES} attempts: {last_err}")


def _sys(exam_name, subject_name=""):
    """Build a dynamic system prompt based on exam context."""
    ctx = f"{exam_name} exam"
    if subject_name:
        ctx += f", {subject_name} subject"
    return (
        f"You are an expert educator and content creator specializing in the {ctx}. "
        "Generate accurate, comprehensive educational content. "
        "ALWAYS respond with valid JSON only — no markdown fences, no extra text outside JSON."
    )


def format_questions(raw_text, schema_example, exam_name, subject_name=""):
    """Step 4 — Formatter: extract structured JSON from audited raw text."""
    prompt = (
        f"Extract questions, options, correct answers, and explanations from this text.\n"
        f"Output ONLY a valid JSON array matching this schema:\n{schema_example}\n"
        f"Output absolutely nothing but valid JSON array starting with [.\n\n"
        f"{raw_text}\n["
    )
    sys = _sys(exam_name, subject_name)
    return _call(prompt, expect_json=True, max_tokens=8000, system_prompt=sys)


def generate_note(prompt, exam_name, subject_name="", expect_json=False):
    return _call(prompt, expect_json, max_tokens=8000, system_prompt=_sys(exam_name, subject_name))


def generate_formula(prompt, exam_name, subject_name="", expect_json=True):
    return _call(prompt, expect_json, max_tokens=5000, system_prompt=_sys(exam_name, subject_name))


def generate_resource(prompt, exam_name, subject_name="", expect_json=True):
    return _call(prompt, expect_json, max_tokens=5000, system_prompt=_sys(exam_name, subject_name))


def expand_content(existing_html, topic, subj_name, exam_name, content_type="note"):
    prompt = (
        f"The following {content_type} about {topic} ({subj_name}) is incomplete.\n\n"
        f"EXISTING:\n{existing_html[:2000]}\n\n"
        "Add MORE content: detailed explanations, clinical examples, drug doses in <code>, "
        "mnemonics in <em>, key points. Write ONLY the additional HTML to append "
        "(start with <h3> or <p>). At least 500 more words. Do NOT repeat existing content."
    )
    return _call(prompt, expect_json=False, max_tokens=4000, system_prompt=_sys(exam_name, subj_name))

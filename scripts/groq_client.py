"""
groq_client.py — 2 Groq keys with fallback + smart chunking.

GitHub Secret names (exact):
  GROQ_API_KEY   → Groq K1 — primary structurer
  GROQ_API_KEY2  → Groq K2 — fallback structurer

Free tier limits:
  RPD: 14,400 requests/day  (not a concern)
  TPD: 100,000 tokens/day   ← THIS is the real limit
  TPM: 6,000 tokens/minute

Strategy: never send more than ~4,000 tokens per call.
Raw text for 10 questions ≈ 2,000-3,000 tokens → safe.
If raw text is too large, split it into 10Q segments first.
"""
import os, json, re, time
from groq import Groq

MODEL       = "llama-3.3-70b-versatile"
MAX_RETRIES = 3
MIN_GAP     = 3.0   # seconds between calls — stay under 6000 TPM

# ── Exact GitHub secret names ─────────────────────────────────────────────────
KEY_G1 = "GROQ_API_KEY"   # Groq K1 — primary
KEY_G2 = "GROQ_API_KEY2"  # Groq K2 — fallback

_clients   = {}
_last_call = {}


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
    """Robustly extract JSON array or object from text."""
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text).strip()
    # Ensure it starts with [
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
    raise GroqError("No valid JSON in Groq response")


def _parse_wait_seconds(err_str):
    """
    Parse wait time from Groq rate-limit error.
    Handles both "try again in 45s" and "try again in 1h49m48.864s" formats.
    """
    # Try "Xh Ym Zs" format
    m = re.search(r"try again in\s+(?:(\d+)h\s*)?(?:(\d+)m\s*)?(\d+\.?\d*)s",
                  err_str, re.IGNORECASE)
    if m:
        h = int(m.group(1) or 0)
        mins = int(m.group(2) or 0)
        secs = float(m.group(3) or 0)
        total = h * 3600 + mins * 60 + secs
        # Cap at 120s — if wait is longer, switch to other key immediately
        return min(total, 120.0) + 2
    # Try plain seconds
    m = re.search(r"try again in (\d+\.?\d*)s", err_str, re.IGNORECASE)
    if m:
        return min(float(m.group(1)), 120.0) + 2
    return 45.0  # default


def _call_one(key_env, system_prompt, user_prompt, max_tokens):
    """Call one Groq key with retry. Returns text or raises GroqError."""
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
                temperature=0.4,
                max_tokens=max_tokens,
            )
            text = (resp.choices[0].message.content or "").strip()
            if not text:
                raise GroqError("Empty response")
            return text

        except GroqError:
            raise
        except Exception as e:
            last_err = e
            s = str(e)
            print(f"  [Groq/{key_env}] attempt {attempt}/{MAX_RETRIES}: {s[:150]}")
            if "413" in s:
                raise GroqError("Prompt too large (413) — chunk smaller")
            if "429" in s or "rate_limit" in s.lower():
                wait = _parse_wait_seconds(s)
                # If wait > 90s, this key is exhausted for the day — switch immediately
                if wait > 90:
                    raise GroqError(f"TPD exhausted on {key_env} — switching key")
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
        "Output ONLY valid JSON — no markdown, no text outside the JSON array."
    )


def _key_available(key_env):
    return bool(os.environ.get(key_env, "").strip())


# ── Compact schema (fewer tokens) ─────────────────────────────────────────────
_COMPACT_SCHEMA = (
    '[{"question":"...","difficulty":"medium","explanation":"...",'
    '"hint":"","options":['
    '{"letter":"A","text":"...","isCorrect":true,"explanation":"..."},'
    '{"letter":"B","text":"...","isCorrect":false,"explanation":"..."},'
    '{"letter":"C","text":"...","isCorrect":false,"explanation":"..."},'
    '{"letter":"D","text":"...","isCorrect":false,"explanation":"..."}]}]'
)


def _structure_chunk(raw_chunk, exam_name, subject_name, key_env):
    """
    Structure one small chunk of raw text (≤10 questions) into JSON.
    Returns list of question dicts or raises GroqError.
    """
    sys_prompt = _sys(exam_name, subject_name)
    user_prompt = (
        f"Extract questions from the text below. "
        f"Output ONLY a JSON array. Schema: {_COMPACT_SCHEMA}\n\n"
        f"Rules: exactly ONE isCorrect:true per question, "
        f"difficulty is 'easy'/'medium'/'hard'.\n\n"
        f"TEXT:\n{raw_chunk}"
    )
    text = _call_one(key_env, sys_prompt, user_prompt, max_tokens=4000)
    result = _extract_json(text)
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return [result]
    raise GroqError("Unexpected JSON structure")


def _split_raw_text(raw_text, questions_per_chunk=10):
    """
    Split raw numbered question text into chunks of N questions each.
    Splits on question number boundaries like '1.' '2.' etc.
    """
    # Find all question start positions
    pattern = re.compile(r'(?:^|\n)\s*(\d+)\s*[.)]\s', re.MULTILINE)
    matches = list(pattern.finditer(raw_text))

    if len(matches) < 2:
        # Can't split — return as single chunk
        return [raw_text]

    chunks = []
    for i in range(0, len(matches), questions_per_chunk):
        start = matches[i].start()
        end   = matches[i + questions_per_chunk].start() if (i + questions_per_chunk) < len(matches) else len(raw_text)
        chunk = raw_text[start:end].strip()
        if chunk:
            chunks.append(chunk)
    return chunks if chunks else [raw_text]


# ── Public API ────────────────────────────────────────────────────────────────

def format_questions(raw_text, schema_example, exam_name, subject_name=""):
    """
    Structure raw question text into JSON array.
    - Splits raw text into ≤10Q chunks to stay under token limits
    - GROQ_API_KEY → GROQ_API_KEY2 → raises GroqError (caller falls back to Flash)
    """
    # Split into manageable chunks
    chunks = _split_raw_text(raw_text, questions_per_chunk=10)
    print(f"  [Groq] Structuring {len(chunks)} chunk(s)...")

    all_questions = []

    for chunk_idx, chunk in enumerate(chunks, 1):
        chunk_done = False
        for key_env in [KEY_G1, KEY_G2]:
            if not _key_available(key_env):
                continue
            try:
                qs = _structure_chunk(chunk, exam_name, subject_name, key_env)
                all_questions.extend(qs)
                print(f"  [Groq] ✅ chunk {chunk_idx}/{len(chunks)} — {len(qs)}Q via {key_env}")
                chunk_done = True
                break
            except GroqError as e:
                err = str(e)
                if "413" in err or "too large" in err.lower():
                    # This chunk is still too big — skip it, don't retry
                    print(f"  [Groq] chunk {chunk_idx} too large even after split — skipping")
                    chunk_done = True
                    break
                print(f"  [Groq] {key_env} failed on chunk {chunk_idx} ({err[:80]}) — trying next key")

        if not chunk_done:
            # Both Groq keys failed — try Mistral before giving up on this chunk
            try:
                import mistral_client
                if mistral_client.available():
                    qs = mistral_client.structure_questions(chunk, exam_name, subject_name, _COMPACT_SCHEMA)
                    if qs:
                        all_questions.extend(qs)
                        print(f"  [Groq] ✅ chunk {chunk_idx}/{len(chunks)} — {len(qs)}Q via Mistral fallback")
                        chunk_done = True
            except Exception as e:
                print(f"  [Groq] Mistral fallback also failed for chunk {chunk_idx} ({str(e)[:60]})")

        if not chunk_done:
            print(f"  [Groq] chunk {chunk_idx} failed on all keys — skipping")

    if not all_questions:
        raise GroqError("All chunks failed on all Groq keys")

    return all_questions

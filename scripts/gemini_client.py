"""
gemini_client.py — Balanced 2-key strategy.

  GEMINI_API_KEY   → Flash (researcher + subject tests)
                   → Pro  (auditor — uses spare Pro quota on this account)
  GEMINI_API_KEY_1 → Pro  (generator — full-length test chunks)

Per-day Pro budget (4 runs):
  GEMINI_API_KEY   Pro: ~4 calls/day (auditor)   out of 25 RPD free
  GEMINI_API_KEY_1 Pro: ~12-20 calls/day (gen)   out of 25 RPD free
  GEMINI_API_KEY   Flash: ~16 calls/day           out of 15 RPD free
"""
import os, json, re, time
from google import genai
from google.genai import types

MODEL_PRO   = "gemini-2.5-pro"
MODEL_FLASH = "gemini-2.5-flash"

RPM_PRO   = 4   # 5 RPM official → 4 for safety
RPM_FLASH = 8   # 10 RPM official → 8 for safety
MAX_RETRIES = 4

_clients   = {}
# Separate rate-limit trackers per (account × model) slot
_last_call = {
    "pro_gen":   0.0,   # GEMINI_API_KEY_1 + Pro  (generator)
    "pro_audit": 0.0,   # GEMINI_API_KEY   + Pro  (auditor)
    "flash":     0.0,   # GEMINI_API_KEY   + Flash (researcher / subject tests)
}


class GeminiError(Exception):
    pass


class GeminiQuotaError(GeminiError):
    pass


def _get_client(key_name):
    if key_name not in _clients:
        api_key = os.environ.get(key_name, "")
        if not api_key:
            raise GeminiError(f"Env var {key_name} not set")
        _clients[key_name] = genai.Client(api_key=api_key)
    return _clients[key_name]


def _rate_limit_sleep(tracker_key, rpm):
    min_gap = 60.0 / rpm
    elapsed = time.time() - _last_call[tracker_key]
    if elapsed < min_gap:
        wait = min_gap - elapsed + 0.5
        print(f"  [RateLimit/{tracker_key}] Waiting {wait:.1f}s...")
        time.sleep(wait)
    _last_call[tracker_key] = time.time()


def _extract_json(text):
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for s, e in [("[", "]"), ("{", "}")]:
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
    raise GeminiError("No valid JSON in Gemini response")


def _call(model_name, tracker_key, key_env, rpm, prompt, expect_json, max_tokens):
    client   = _get_client(key_env)
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _rate_limit_sleep(tracker_key, rpm)
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.65,
                    max_output_tokens=max_tokens,
                )
            )
            text = response.text
            return text if not expect_json else _extract_json(text)

        except GeminiError:
            raise
        except Exception as e:
            last_err = e
            err_str  = str(e)
            print(f"  [Gemini/{tracker_key}] Attempt {attempt}/{MAX_RETRIES}: {err_str[:100]}")
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
                raise GeminiQuotaError(f"Quota exhausted for {tracker_key}")
            if "503" in err_str or "UNAVAILABLE" in err_str:
                wait = 20 * attempt
                print(f"  [Gemini/{tracker_key}] Server busy — waiting {wait}s...")
                time.sleep(wait)
                continue
            if attempt < MAX_RETRIES:
                time.sleep(5 * attempt)
    raise GeminiError(f"Gemini/{tracker_key} failed after {MAX_RETRIES} attempts: {last_err}")


# ── Public API ─────────────────────────────────────────────────────────────────

def research(prompt, max_tokens=1024):
    """Step 1 — Researcher: Flash via GEMINI_API_KEY. Returns raw text."""
    try:
        return _call(MODEL_FLASH, "flash", "GEMINI_API_KEY", RPM_FLASH, prompt, False, max_tokens)
    except Exception as e:
        print(f"  [research] Failed ({e}) — returning empty context")
        return ""


def generate_pro(prompt, expect_json=True, max_tokens=8192):
    """Step 2 — Generator: Pro via GEMINI_API_KEY_1. Falls back to Flash."""
    try:
        return _call(MODEL_PRO, "pro_gen", "GEMINI_API_KEY_1", RPM_PRO, prompt, expect_json, max_tokens)
    except GeminiQuotaError:
        print("  [generate_pro] GEMINI_API_KEY_1 Pro quota hit — falling back to Flash")
        return _call(MODEL_FLASH, "flash", "GEMINI_API_KEY", RPM_FLASH, prompt, expect_json, min(max_tokens, 4096))


def audit(raw_text, exam_name, topic, max_tokens=6144):
    """Step 3 — Auditor: Pro via GEMINI_API_KEY (spare quota). Returns corrected text or None."""
    prompt = (
        f"You are a strict quality auditor for {exam_name} exam questions about {topic}.\n"
        "Review each question below:\n"
        "- Verify the correct answer is factually accurate\n"
        "- Recalculate any math; check lab values / drug doses\n"
        "- Ensure distractors are plausible but definitively wrong\n"
        "- Fix any hallucinated facts or ambiguous wording\n"
        "Return the corrected questions as raw text in the SAME format. "
        "Do NOT change questions that are already correct. Do NOT add JSON.\n\n"
        f"{raw_text}"
    )
    try:
        return _call(MODEL_PRO, "pro_audit", "GEMINI_API_KEY", RPM_PRO, prompt, False, max_tokens)
    except Exception as e:
        print(f"  [audit] Failed ({e}) — using generator output directly")
        return None


def generate_flash(prompt, expect_json=True, max_tokens=6144):
    """Flash via GEMINI_API_KEY — for subject-wise tests."""
    return _call(MODEL_FLASH, "flash", "GEMINI_API_KEY", RPM_FLASH, prompt, expect_json, max_tokens)

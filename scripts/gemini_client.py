"""
gemini_client.py — 3-API strategy with Pro→Flash fallback.

  GEMINI_API_KEY_1 → gemini-2.5-pro  (full-length tests, best quality)
  GEMINI_API_KEY   → gemini-2.5-flash (subject tests, fallback for Pro)

Logic:
  - Always try Pro first for full-length tests
  - If Pro quota exhausted (429) → automatically fall back to Flash
  - Flash used directly for subject-wise tests
  - Both keys independent accounts = separate quotas
"""
import os, json, re, time
from google import genai
from google.genai import types

MODEL_PRO   = "gemini-2.5-pro"
MODEL_FLASH = "gemini-2.5-flash"

RPM_PRO     = 4    # 5 RPM limit, use 4 for safety
RPM_FLASH   = 8    # 10 RPM limit, use 8 for safety
MAX_RETRIES = 4

_clients   = {}
_last_call = {"pro": 0.0, "flash": 0.0}


class GeminiError(Exception):
    """Raised when all retries exhausted or unrecoverable error."""
    pass


class GeminiQuotaError(GeminiError):
    """Raised specifically when quota is exhausted — triggers fallback."""
    pass


def _get_client(key_name):
    if key_name not in _clients:
        api_key = os.environ.get(key_name, "")
        if not api_key:
            raise GeminiError(f"Environment variable {key_name} not set")
        _clients[key_name] = genai.Client(api_key=api_key)
    return _clients[key_name]


def _rate_limit_sleep(model_key, rpm):
    min_gap = 60.0 / rpm
    elapsed = time.time() - _last_call[model_key]
    if elapsed < min_gap:
        wait = min_gap - elapsed + 0.5
        print(f"  [RateLimit/{model_key}] Waiting {wait:.1f}s...")
        time.sleep(wait)
    _last_call[model_key] = time.time()


def _extract_json(text):
    """Extract first valid JSON array or object from text."""
    # Strip markdown fences
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)
    text = text.strip()
    # Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find first balanced [ ] or { }
    for start_char, end_char in [("[", "]"), ("{", "}")]:
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
    raise GeminiError("No valid JSON found in Gemini response")


def _call(model_name, model_key, key_env, rpm, prompt, expect_json, max_tokens):
    """Call a specific Gemini model with retries."""
    client = _get_client(key_env)
    last_err = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _rate_limit_sleep(model_key, rpm)
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.65,
                    max_output_tokens=max_tokens,
                )
            )
            text = response.text
            if not expect_json:
                return text
            return _extract_json(text)

        except GeminiError:
            raise

        except Exception as e:
            last_err = e
            err_str  = str(e)
            print(f"  [Gemini/{model_key}] Attempt {attempt}/{MAX_RETRIES}: {err_str[:100]}")

            # Quota exhausted — raise special error to trigger fallback
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
                raise GeminiQuotaError(f"Quota exhausted for {model_key}")

            # Server busy (503) — wait and retry
            if "503" in err_str or "UNAVAILABLE" in err_str:
                wait = 20 * attempt
                print(f"  [Gemini/{model_key}] Server busy — waiting {wait}s...")
                time.sleep(wait)
                continue

            if attempt < MAX_RETRIES:
                time.sleep(5 * attempt)

    raise GeminiError(f"Gemini/{model_key} failed after {MAX_RETRIES} attempts: {last_err}")


def generate_pro(prompt, expect_json=True, max_tokens=8192):
    """
    Try gemini-2.5-pro (GEMINI_API_KEY_1) first.
    If quota exhausted, automatically falls back to gemini-2.5-flash (GEMINI_API_KEY).
    For large prompts (full-length tests), Flash fallback uses smaller chunks.
    """
    try:
        result = _call(MODEL_PRO, "pro", "GEMINI_API_KEY_1", RPM_PRO, prompt, expect_json, max_tokens)
        return result
    except GeminiQuotaError:
        print(f"  [Gemini/pro] Quota exhausted — falling back to Flash (smaller chunks)...")
        # Flash handles smaller requests better — reduce max_tokens
        flash_max = min(max_tokens, 4096)
        return _call(MODEL_FLASH, "flash", "GEMINI_API_KEY", RPM_FLASH, prompt, expect_json, flash_max)


def generate_flash(prompt, expect_json=True, max_tokens=6144):
    """
    Use gemini-2.5-flash (GEMINI_API_KEY) directly.
    For subject-wise tests.
    """
    return _call(MODEL_FLASH, "flash", "GEMINI_API_KEY", RPM_FLASH, prompt, expect_json, max_tokens)

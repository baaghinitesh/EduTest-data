"""
gemini_client.py — Dual Gemini key support.

  GEMINI_API_KEY     → gemini-2.5-flash  (Key 1, subject-wise tests)
  GEMINI_API_KEY_PRO → gemini-2.5-pro    (Key 2, full-length tests only)

Free tier:
  Flash: 10 RPM, 20 RPD
  Pro:   5 RPM,  25 RPD (preview)

Graceful failure: raises GeminiError on exhaustion so caller can skip item.
"""
import os, json, re, time
from google import genai
from google.genai import types

MODEL_FLASH = "gemini-2.5-flash"
MODEL_PRO   = "gemini-2.5-pro"

RPM_FLASH = 8    # stay under 10 RPM
RPM_PRO   = 4    # stay under 5 RPM
MAX_RETRIES = 2  # fewer retries — fail fast, skip item, move on

_clients = {}
_last_call = {"flash": 0.0, "pro": 0.0}


class GeminiError(Exception):
    """Raised when Gemini quota is exhausted or call fails after retries."""
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
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
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
            raise  # re-raise our own errors immediately

        except Exception as e:
            last_err = e
            err_str = str(e)
            print(f"  [Gemini/{model_key}] Attempt {attempt} failed: {err_str[:120]}")

            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
                # Quota exhausted — no point retrying, skip this item
                raise GeminiError(f"Quota exhausted for {model_key}: {err_str[:100]}")

            if attempt < MAX_RETRIES:
                time.sleep(5 * attempt)

    raise GeminiError(f"Gemini/{model_key} failed after {MAX_RETRIES} attempts: {last_err}")


def generate_flash(prompt, expect_json=True, max_tokens=6144):
    """
    gemini-2.5-flash via GEMINI_API_KEY.
    Use for: subject-wise tests (10-50Q).
    Raises GeminiError on failure — caller should catch and skip.
    """
    return _call(MODEL_FLASH, "flash", "GEMINI_API_KEY", RPM_FLASH, prompt, expect_json, max_tokens)


def generate_pro(prompt, expect_json=True, max_tokens=8192):
    """
    gemini-2.5-pro via GEMINI_API_KEY_PRO.
    Use for: full-length tests only (1 call per run).
    Raises GeminiError on failure — caller should catch and skip.
    """
    return _call(MODEL_PRO, "pro", "GEMINI_API_KEY_PRO", RPM_PRO, prompt, expect_json, max_tokens)

"""
gemini_client.py — Single model: gemini-2.5-pro only.

Free tier: 5 RPM, 100 RPD
Strategy: 4 runs/day x ~20 calls/run = ~80 calls/day (safe under 100 RPD)
All content uses Pro for maximum quality — medical accuracy is non-negotiable.
"""
import os, json, re, time
from google import genai
from google.genai import types

MODEL = "gemini-2.5-pro"
RPM   = 4        # actual limit 5, use 4 for safety (1 call per 15s)
MAX_RETRIES = 3

_client = None
_last_call = 0.0

def _get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise ValueError("GEMINI_API_KEY environment variable not set")
        _client = genai.Client(api_key=api_key)
    return _client

def _rate_limit_sleep():
    """Enforce minimum gap between calls to stay under 5 RPM."""
    global _last_call
    min_gap = 60.0 / RPM   # 15 seconds between calls
    elapsed = time.time() - _last_call
    if elapsed < min_gap:
        wait = min_gap - elapsed + 1.0   # 1s buffer
        print(f"  [RateLimit] Waiting {wait:.1f}s (Pro 5 RPM limit)...")
        time.sleep(wait)
    _last_call = time.time()

def _extract_json(text):
    """Strip markdown fences and extract first valid JSON object/array."""
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
    raise ValueError("No valid JSON found in Gemini response")

def generate(prompt, expect_json=True, max_tokens=8192):
    """
    Call gemini-2.5-pro with rate limiting and retries.
    Returns parsed JSON if expect_json=True, else raw text.
    """
    client = _get_client()
    last_err = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _rate_limit_sleep()
            response = client.models.generate_content(
                model=MODEL,
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

        except Exception as e:
            last_err = e
            err_str = str(e)
            print(f"  [Pro] Attempt {attempt} failed: {err_str[:150]}")

            if "429" in err_str or "quota" in err_str.lower() or "RESOURCE_EXHAUSTED" in err_str:
                retry_match = re.search(r"(\d+)\s*second", err_str)
                suggested = int(retry_match.group(1)) if retry_match else 60
                wait = max(suggested + 5, 60) * attempt
                print(f"  [Pro] Rate limited — waiting {wait}s...")
                time.sleep(wait)
            elif attempt < MAX_RETRIES:
                time.sleep(5 * attempt)

    raise RuntimeError(f"Gemini Pro failed after {MAX_RETRIES} attempts: {last_err}")

# Aliases so content_builder.py works without changes
generate_flash = generate
generate_pro   = generate

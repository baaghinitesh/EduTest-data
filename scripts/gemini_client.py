"""
gemini_client.py — Dual-model Gemini API wrapper.

Model assignment:
  FLASH (gemini-2.5-flash) — long content: full-length tests, long notes
      Free tier: 10 RPM, 250 RPD, 1M token context
  PRO   (gemini-2.5-pro)   — precision content: formulas, short/topic tests
      Free tier: 5 RPM, 100 RPD

Rate-limit aware: auto-sleeps between calls to stay within RPM limits.
"""
import os, json, re, time
import google.generativeai as genai

# ── Model names ───────────────────────────────────────────────────────────────
MODEL_FLASH = "gemini-2.5-flash"   # long content, full-length tests, long notes
MODEL_PRO   = "gemini-2.5-pro"     # precision content, formulas, short tests

# ── Rate limit guards (free tier) ─────────────────────────────────────────────
# We stay well under limits by enforcing minimum gaps between calls
RPM_FLASH = 10   # 10 RPM → 1 call per 6s minimum
RPM_PRO   = 5    # 5 RPM  → 1 call per 12s minimum
_last_call = {"flash": 0.0, "pro": 0.0}

MAX_RETRIES = 3
_initialized = False

def init():
    global _initialized
    if _initialized:
        return
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable not set")
    genai.configure(api_key=api_key)
    _initialized = True

def _rate_limit_sleep(model_key, rpm):
    """Sleep if needed to respect RPM limit."""
    min_gap = 60.0 / rpm
    elapsed = time.time() - _last_call[model_key]
    if elapsed < min_gap:
        sleep_time = min_gap - elapsed + 0.5  # 0.5s buffer
        print(f"  [RateLimit] Sleeping {sleep_time:.1f}s for {model_key} RPM limit...")
        time.sleep(sleep_time)
    _last_call[model_key] = time.time()

def _extract_json(text):
    """Strip markdown fences and extract the first valid JSON object/array."""
    # Remove ```json ... ``` or ``` ... ```
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)
    text = text.strip()
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find first [ or { and extract balanced block
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

def _call(model_name, model_key, rpm, prompt, expect_json, max_tokens):
    """Internal: call a specific model with rate limiting and retries."""
    init()
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _rate_limit_sleep(model_key, rpm)
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(
                prompt,
                generation_config=genai.types.GenerationConfig(
                    temperature=0.7,
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
            print(f"  [Gemini/{model_key}] Attempt {attempt} failed: {err_str[:120]}")
            # If rate limited (429), wait longer
            if "429" in err_str or "quota" in err_str.lower():
                wait = 30 * attempt
                print(f"  [Gemini/{model_key}] Rate limited — waiting {wait}s...")
                time.sleep(wait)
            elif attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Gemini/{model_key} failed after {MAX_RETRIES} attempts: {last_err}")

# ── Public API ────────────────────────────────────────────────────────────────

def generate_flash(prompt, expect_json=True, max_tokens=8192):
    """
    Use gemini-2.5-flash.
    Best for: full-length tests (100Q), long notes (1000+ words), multi-subject tests.
    Free: 10 RPM, 250 RPD, 1M token context.
    """
    return _call(MODEL_FLASH, "flash", RPM_FLASH, prompt, expect_json, max_tokens)

def generate_pro(prompt, expect_json=True, max_tokens=4096):
    """
    Use gemini-2.5-pro.
    Best for: formula sheets, short/topic tests (20-30Q), precision factual content.
    Free: 5 RPM, 100 RPD.
    """
    return _call(MODEL_PRO, "pro", RPM_PRO, prompt, expect_json, max_tokens)

def generate(prompt, expect_json=True, model="flash"):
    """Convenience wrapper — model='flash' or 'pro'."""
    if model == "pro":
        return generate_pro(prompt, expect_json)
    return generate_flash(prompt, expect_json)

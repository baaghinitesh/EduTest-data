"""
gemini_client.py — Gemini API wrapper with retry + JSON extraction
"""
import os, json, re, time
import google.generativeai as genai

MODEL = "gemini-1.5-flash"
MAX_RETRIES = 3

def init():
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable not set")
    genai.configure(api_key=api_key)

def _extract_json(text):
    """Strip markdown fences and extract the first valid JSON object/array."""
    # Remove ```json ... ``` or ``` ... ```
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)
    text = text.strip()
    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find first [ or { and try from there
    for start_char, end_char in [("[", "]"), ("{", "}")]:
        idx = text.find(start_char)
        if idx != -1:
            # Find matching end
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

def generate(prompt, expect_json=True):
    """Call Gemini and return parsed JSON (or raw text if expect_json=False)."""
    init()
    model = genai.GenerativeModel(MODEL)
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = model.generate_content(
                prompt,
                generation_config=genai.types.GenerationConfig(
                    temperature=0.7,
                    max_output_tokens=8192,
                )
            )
            text = response.text
            if not expect_json:
                return text
            return _extract_json(text)
        except Exception as e:
            last_err = e
            print(f"  [Gemini] Attempt {attempt} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)  # exponential backoff
    raise RuntimeError(f"Gemini failed after {MAX_RETRIES} attempts: {last_err}")
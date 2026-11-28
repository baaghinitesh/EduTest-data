"""
json_utils.py — Robust JSON extraction with truncated-array recovery.
"""
import json
import re


_WRAPPER_KEYS = ("questions", "items", "data", "results")


def _unwrap_dict(obj):
    """If obj is a wrapper dict like {'questions': [...]}, return the inner list."""
    if isinstance(obj, dict):
        for key in _WRAPPER_KEYS:
            val = obj.get(key)
            if isinstance(val, list):
                return val
    return obj


def _recover_array_objects(text, array_start):
    """
    Scan inside an unclosed JSON array starting at `array_start` ('[')
    and recover every complete JSON object before truncation.
    """
    decoder = json.JSONDecoder()
    pos = array_start + 1
    n = len(text)
    recovered = []

    while pos < n:
        while pos < n and text[pos] in " \t\r\n,":
            pos += 1
        if pos >= n or text[pos] == "]":
            break
        if text[pos] != "{":
            next_obj = text.find("{", pos)
            if next_obj == -1:
                break
            pos = next_obj
        try:
            obj, end_pos = decoder.raw_decode(text, pos)
            if isinstance(obj, dict):
                recovered.append(obj)
            pos = end_pos
        except json.JSONDecodeError:
            break

    return recovered


def extract_json(text, error_cls=ValueError):
    """
    Extract JSON array or object from raw LLM text.
    - Strips markdown code fences.
    - Unwraps {'questions': [...]} wrapper dicts used in json_object mode.
    - Uses json.JSONDecoder.raw_decode (string/escape aware).
    - Recovers complete question objects if a JSON array was truncated.
    """
    cleaned = re.sub(r"```(?:json)?\s*", "", text or "")
    cleaned = re.sub(r"```", "", cleaned).strip()
    if not cleaned:
        raise error_cls("Empty JSON response")

    try:
        return _unwrap_dict(json.loads(cleaned))
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    arr_idx = cleaned.find("[")
    obj_idx = cleaned.find("{")

    # If a JSON object appears before any '[', try decoding it first (e.g. {"questions": [...]}).
    if obj_idx != -1 and (arr_idx == -1 or obj_idx < arr_idx):
        try:
            obj, _ = decoder.raw_decode(cleaned, obj_idx)
            return _unwrap_dict(obj)
        except json.JSONDecodeError:
            pass

    # Try every '[' candidate (handles preamble notes like "[Note] ... [...]")
    for match in re.finditer(r"\[", cleaned):
        idx = match.start()
        try:
            obj, _ = decoder.raw_decode(cleaned, idx)
            unwrapped = _unwrap_dict(obj)
            if isinstance(unwrapped, list):
                return unwrapped
        except json.JSONDecodeError:
            recovered = _recover_array_objects(cleaned, idx)
            if recovered:
                return recovered

    if obj_idx != -1:
        try:
            obj, _ = decoder.raw_decode(cleaned, obj_idx)
            return _unwrap_dict(obj)
        except json.JSONDecodeError:
            pass

    raise error_cls("No valid JSON found in model response")

"""
research_cache.py — Persistent research cache per exam/class.

Stores research results in data/research/<class_slug>.json so that:
  - Repeated runs on the same exam reuse existing research
  - Research is only regenerated when stale (> CACHE_TTL_DAYS old)
  - Failed research from a previous run is retried next time

Cache file structure:
  {
    "class_slug": "neet-ug",
    "exam_name": "NEET UG",
    "subjects": {
      "neet-physics": {
        "research": "...text...",
        "cached_at": "2026-05-01T10:00:00Z",
        "chapters_covered": ["Mechanics", "Thermodynamics", ...]
      },
      ...
    },
    "full_exam": {
      "research": "...text...",
      "cached_at": "2026-05-01T10:00:00Z"
    }
  }
"""
import os, json
from datetime import datetime, timezone, timedelta

ROOT       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR  = os.path.join(ROOT, "data", "research")
CACHE_TTL_DAYS = 30   # regenerate research after 30 days


def _cache_path(class_slug):
    os.makedirs(CACHE_DIR, exist_ok=True)
    safe = class_slug.replace("/", "-").replace("\\", "-")
    return os.path.join(CACHE_DIR, f"{safe}.json")


def _load(class_slug):
    path = _cache_path(class_slug)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"class_slug": class_slug, "subjects": {}, "full_exam": {}}


def _save(class_slug, data):
    path = _cache_path(class_slug)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _is_fresh(cached_at_str):
    if not cached_at_str:
        return False
    try:
        cached_at = datetime.fromisoformat(cached_at_str.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) - cached_at < timedelta(days=CACHE_TTL_DAYS)
    except Exception:
        return False


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ── Public API ────────────────────────────────────────────────────────────────

def get_subject_research(class_slug, subject_slug, chapters_str):
    """
    Return cached research for a subject, or None if missing/stale.
    chapters_str is used to detect if the cache covers the same chapters.
    """
    data = _load(class_slug)
    entry = data.get("subjects", {}).get(subject_slug, {})
    if not entry:
        return None
    if not _is_fresh(entry.get("cached_at")):
        return None
    # If chapters changed significantly, invalidate
    cached_chapters = entry.get("chapters_covered", "")
    if cached_chapters and chapters_str and cached_chapters != chapters_str:
        return None
    research = entry.get("research", "")
    return research if len(research) > 50 else None


def store_subject_research(class_slug, subject_slug, research_text, chapters_str):
    """Persist research for a subject."""
    if not research_text or len(research_text) < 50:
        return
    data = _load(class_slug)
    data.setdefault("subjects", {})[subject_slug] = {
        "research":        research_text,
        "cached_at":       _now(),
        "chapters_covered": chapters_str,
    }
    _save(class_slug, data)
    print(f"  [ResearchCache] Stored subject research: {class_slug}/{subject_slug}")


def get_full_exam_research(class_slug):
    """Return cached full-exam research, or None if missing/stale."""
    data  = _load(class_slug)
    entry = data.get("full_exam", {})
    if not entry or not _is_fresh(entry.get("cached_at")):
        return None
    research = entry.get("research", "")
    return research if len(research) > 50 else None


def store_full_exam_research(class_slug, research_text):
    """Persist full-exam research."""
    if not research_text or len(research_text) < 50:
        return
    data = _load(class_slug)
    data["full_exam"] = {
        "research":  research_text,
        "cached_at": _now(),
    }
    _save(class_slug, data)
    print(f"  [ResearchCache] Stored full-exam research: {class_slug}")

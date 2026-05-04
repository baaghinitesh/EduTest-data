"""
draft_store.py — Manages partial (incomplete) test drafts across runs.

Design
======
When a test generation is interrupted mid-way (quota hit, API failure),
the partial questions collected so far are saved as a DRAFT:

  data/drafts/<class_slug>/<uid>.json

Drafts are:
  - Committed to git (so they survive across GitHub Actions runs)
  - NEVER published to MongoDB (app never sees them)
  - Resumed on the next run: generation continues from where it stopped
  - Deleted once the test reaches its target question count and is
    published to MongoDB as a complete test

Draft JSON format
=================
{
  "uid":             "uuid-string",
  "title":           "NEET UG — Physics | Thermodynamics",
  "class_slug":      "neet-ug",
  "test_type":       "subject-wise",   # or "full-length", "combined"
  "target_q":        90,               # total questions needed
  "collected_q":     60,               # questions collected so far
  "final_path":      "data/admin/neet-ug/tests/xxx.json",  # where to save when done
  "payload":         { ...full test payload with questions so far... },
  "resume_context":  {
    # For full-length: which subjects still need questions
    "remaining_subjects": [
      {"subjectSlug": "...", "subjectName": "...", "questionCount": 30, "chapters": [...]}
    ],
    # For subject/combined: how many more Q needed
    "remaining_q": 30
  },
  "created_at":      "2026-05-04T10:00:00Z",
  "updated_at":      "2026-05-04T10:05:00Z"
}
"""
import os, json
from datetime import datetime, timezone

ROOT        = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRAFTS_DIR  = os.path.join(ROOT, "data", "drafts")


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _draft_path(class_slug, uid):
    safe = class_slug.replace("/", "-")
    return os.path.join(DRAFTS_DIR, safe, f"{uid}.json")


def save_draft(uid, title, class_slug, test_type, target_q,
               payload, resume_context, final_path):
    """
    Save or update a partial test draft.
    Called when generation is interrupted before reaching target_q.
    """
    collected_q = payload.get("totalQuestions", len(payload.get("questions", [])))
    draft = {
        "uid":            uid,
        "title":          title,
        "class_slug":     class_slug,
        "test_type":      test_type,
        "target_q":       target_q,
        "collected_q":    collected_q,
        "final_path":     final_path,
        "payload":        payload,
        "resume_context": resume_context,
        "created_at":     _now(),
        "updated_at":     _now(),
    }
    path = _draft_path(class_slug, uid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(draft, f, indent=2, ensure_ascii=False)
    rel = os.path.relpath(path, ROOT)
    print(f"  [Draft] Saved {collected_q}/{target_q}Q → {rel}")
    return rel


def load_all_drafts():
    """
    Return list of all draft dicts, sorted oldest-first (resume in order).
    """
    drafts = []
    if not os.path.isdir(DRAFTS_DIR):
        return drafts
    for class_dir in os.listdir(DRAFTS_DIR):
        class_path = os.path.join(DRAFTS_DIR, class_dir)
        if not os.path.isdir(class_path):
            continue
        for fname in os.listdir(class_path):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(class_path, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    draft = json.load(f)
                draft["_file_path"] = fpath
                drafts.append(draft)
            except Exception as e:
                print(f"  [Draft] Warning: could not read {fpath}: {e}")
    drafts.sort(key=lambda d: d.get("created_at", ""))
    return drafts


def delete_draft(uid, class_slug):
    """Delete a draft after it has been completed and published."""
    path = _draft_path(class_slug, uid)
    if os.path.exists(path):
        os.remove(path)
        rel = os.path.relpath(path, ROOT)
        print(f"  [Draft] Deleted completed draft: {rel}")
        # Remove empty directory
        parent = os.path.dirname(path)
        try:
            if not os.listdir(parent):
                os.rmdir(parent)
        except Exception:
            pass
        return True
    return False


def has_drafts():
    """Quick check — any drafts pending?"""
    return bool(load_all_drafts())


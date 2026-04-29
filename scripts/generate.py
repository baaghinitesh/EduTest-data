"""
generate.py — EduTest content runner. 3-API strategy.

Per run generates up to 11 items:
  1 full-length test  (Gemini Pro,   GEMINI_API_KEY_PRO)
  2 subject tests     (Gemini Flash, GEMINI_API_KEY)
  4 notes             (Groq 70B,     GROQ_API_KEY)
  4 formulas          (Groq 8B,      GROQ_API_KEY)

GRACEFUL FAILURE POLICY:
  - Each item is wrapped in try/except
  - If an item fails (quota, network, bad JSON) → log warning, skip, continue
  - Run only fails if ZERO items were generated
  - Partial success (e.g. 7/11 items) → commits what was generated, exits 0
  - This prevents GitHub Actions from marking the run as failed
    when only 1-2 API calls hit quota limits
"""
import os, sys, random, traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import topic_picker
import content_builder
import catalog_updater
from gemini_client import GeminiError
from groq_client import GroqError

CLASS_SLUG = "norcet"

def safe_run(label, fn, *args, **kwargs):
    """
    Run fn(*args, **kwargs) safely.
    Returns (result, None) on success.
    Returns (None, error_str) on failure — never raises.
    """
    try:
        result = fn(*args, **kwargs)
        return result, None
    except (GeminiError, GroqError) as e:
        msg = f"API quota/error: {e}"
        print(f"  [SKIP] {label} — {msg}")
        return None, msg
    except Exception as e:
        msg = f"Unexpected error: {e}"
        print(f"  [SKIP] {label} — {msg}")
        traceback.print_exc()
        return None, msg

def run():
    mode = os.environ.get("GENERATE_MODE", "").strip()

    print(f"\n{'='*65}")
    print(f"  EduTest Generator  |  3-API Strategy")
    print(f"  Exam: {CLASS_SLUG.upper()}  |  Target: 11 items per run")
    print(f"  APIs: Gemini Pro (full-length) + Flash (tests) + Groq (notes/formulas)")
    print(f"{'='*65}\n")

    topics_data = topic_picker.load_topics(CLASS_SLUG)
    if not topics_data:
        print(f"ERROR: No topics for {CLASS_SLUG}")
        sys.exit(1)

    exam_config = topics_data.get("examConfig", {})
    board       = topics_data.get("board", "Competitive")
    index_entries = []
    errors = []
    item_num = 0

    # ── 1. Full-length test (Gemini Pro) ──────────────────────────────────────
    item_num += 1
    print(f"[{item_num}/11] Full-length test (Gemini Pro)...")
    dist = topic_picker.pick_subjects_for_full_length(CLASS_SLUG)
    result, err = safe_run(
        "full-length test",
        content_builder.build_full_length_test,
        CLASS_SLUG, exam_config, dist, board
    )
    if result:
        uid, path, entry = result
        index_entries.append(entry)
        print(f"  ✅ Done: {entry['title']}\n")
    else:
        errors.append(f"Full-length test: {err}")
        print(f"  ⚠️  Skipped — continuing with other items\n")

    # ── 2-3. Subject-wise tests (Gemini Flash) ────────────────────────────────
    test_topics, state = topic_picker.pick_multiple_topics("test", CLASS_SLUG, count=2, different_subjects=True)
    test_items_generated = []

    for i, t in enumerate(test_topics):
        item_num += 1
        print(f"[{item_num}/11] Subject test (Gemini Flash): {t['subjectName']}/{t['topic']}...")
        result, err = safe_run(
            f"subject test {i+1}",
            content_builder.build_subject_test,
            t, exam_config
        )
        if result:
            uid, path, entry = result
            index_entries.append(entry)
            test_items_generated.append((t, uid))
            print(f"  ✅ Done: {entry['title']}\n")
        else:
            errors.append(f"Subject test {i+1}: {err}")
            print(f"  ⚠️  Skipped\n")

    if test_items_generated:
        topic_picker.mark_generated(state, "test", test_items_generated)

    # ── 4-7. Notes (Groq llama-3.3-70b) ──────────────────────────────────────
    note_topics, state2 = topic_picker.pick_multiple_topics("note", CLASS_SLUG, count=4, different_subjects=True)
    note_items_generated = []

    for i, t in enumerate(note_topics):
        item_num += 1
        print(f"[{item_num}/11] Note (Groq 70B): {t['subjectName']}/{t['topic']}...")
        result, err = safe_run(
            f"note {i+1}",
            content_builder.build_note,
            t
        )
        if result:
            uid, path, entry = result
            index_entries.append(entry)
            note_items_generated.append((t, uid))
            print(f"  ✅ Done: {entry['title']}\n")
        else:
            errors.append(f"Note {i+1}: {err}")
            print(f"  ⚠️  Skipped\n")

    if note_items_generated:
        topic_picker.mark_generated(state2, "note", note_items_generated)

    # ── 8-11. Formulas (Groq llama-3.1-8b) ───────────────────────────────────
    formula_topics, state3 = topic_picker.pick_multiple_topics("formula", CLASS_SLUG, count=4, different_subjects=True)
    formula_items_generated = []

    for i, t in enumerate(formula_topics):
        item_num += 1
        print(f"[{item_num}/11] Formula (Groq 8B): {t['subjectName']}/{t['topic']}...")
        result, err = safe_run(
            f"formula {i+1}",
            content_builder.build_formula,
            t
        )
        if result:
            uid, path, entry = result
            index_entries.append(entry)
            formula_items_generated.append((t, uid))
            print(f"  ✅ Done: {entry['title']}\n")
        else:
            errors.append(f"Formula {i+1}: {err}")
            print(f"  ⚠️  Skipped\n")

    if formula_items_generated:
        topic_picker.mark_generated(state3, "formula", formula_items_generated)

    # ── Update catalog index ──────────────────────────────────────────────────
    if index_entries:
        print(f"Updating catalog index: {len(index_entries)} entries...")
        catalog_updater.add_entries(index_entries)

    # ── Final report ──────────────────────────────────────────────────────────
    success_count = len(index_entries)
    fail_count    = len(errors)

    print(f"\n{'='*65}")
    print(f"  DONE — {success_count}/11 items generated successfully")
    if errors:
        print(f"  Skipped {fail_count} items:")
        for e in errors:
            print(f"    - {e}")
    print(f"{'='*65}\n")

    # Only exit with error if NOTHING was generated at all
    if success_count == 0:
        print("ERROR: No items generated. Check API keys and quotas.")
        sys.exit(1)
    # Partial success is fine — exit 0 so GitHub Actions marks run as passed
    sys.exit(0)

if __name__ == "__main__":
    run()

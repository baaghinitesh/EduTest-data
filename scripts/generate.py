"""
generate.py — EduTest content runner. Smart per-model delays.

Per run generates 7 HIGH-QUALITY items:
  1 full-length test  (Gemini Pro → Flash, 3 groups)
  3 subject tests     (Gemini Flash)
  2 notes             (Groq 70B)
  1 formula           (Groq 70B)

DELAY STRATEGY — wait only between calls to the SAME model:
  Gemini Pro:   15s between calls (5 RPM limit = 12s min)
  Gemini Flash: 8s between calls  (10 RPM limit = 6s min)
  Groq 70B:     65s between calls (TPM resets per minute)
  Cross-model:  NO wait needed (different quotas)

4 runs/day = 28 quality items/day
"""
import os, sys, random, traceback, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import topic_picker
import content_builder
import catalog_updater
from gemini_client import GeminiError
from groq_client import GroqError

CLASS_SLUG = "norcet"

# ── Per-model delay trackers ──────────────────────────────────────────────────
# We track the last call time per model and sleep only when calling the same model again.
_last_model_call = {"pro": 0.0, "flash": 0.0, "groq": 0.0}

# Minimum gaps between consecutive calls to the SAME model
GAP_PRO   = 16.0   # Gemini Pro: 5 RPM → 12s min, use 16s for safety
GAP_FLASH = 8.0    # Gemini Flash: 10 RPM → 6s min, use 8s for safety
GAP_GROQ  = 65.0   # Groq 70B: 6000 TPM/min → wait 65s between calls

def wait_for_model(model_key):
    """Sleep only if needed to respect per-model rate limits."""
    gaps = {"pro": GAP_PRO, "flash": GAP_FLASH, "groq": GAP_GROQ}
    gap = gaps.get(model_key, 0)
    elapsed = time.time() - _last_model_call[model_key]
    if elapsed < gap:
        wait = gap - elapsed + 0.5
        print(f"  [ModelGap/{model_key}] Waiting {wait:.1f}s (same model cooldown)...")
        time.sleep(wait)
    _last_model_call[model_key] = time.time()

def safe_run_with_model(label, model_key, fn, *args, **kwargs):
    """Run fn with per-model delay tracking."""
    wait_for_model(model_key)
    try:
        result = fn(*args, **kwargs)
        return result, None
    except (GeminiError, GroqError) as e:
        msg = f"API error: {e}"
        print(f"  [SKIP] {label} — {msg[:100]}")
        return None, msg
    except Exception as e:
        msg = f"Unexpected error: {e}"
        print(f"  [SKIP] {label} — {msg[:100]}")
        traceback.print_exc()
        return None, msg

def run():
    mode = os.environ.get("GENERATE_MODE", "").strip()

    print(f"\n{'='*65}")
    print(f"  EduTest Generator  |  Smart per-model delays")
    print(f"  Exam: {CLASS_SLUG.upper()}  |  Target: 7 quality items per run")
    print(f"  Pro gap: {GAP_PRO}s | Flash gap: {GAP_FLASH}s | Groq gap: {GAP_GROQ}s")
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

    # ── 1. Full-length test (Gemini Pro → Flash, 3 groups) ────────────────────
    # Each group uses Pro (or Flash fallback). 16s gap between Pro calls.
    # The build_full_length_test function makes 3 sequential Pro calls internally.
    # We track "pro" model for the whole block.
    item_num += 1
    print(f"[{item_num}/7] Full-length test (Gemini Pro → Flash, 3 groups)...")
    dist = topic_picker.pick_subjects_for_full_length(CLASS_SLUG)
    result, err = safe_run_with_model(
        "full-length test", "pro",
        content_builder.build_full_length_test,
        CLASS_SLUG, exam_config, dist, board
    )
    if result:
        uid, path, entry = result
        index_entries.append(entry)
        print(f"  ✅ Done: {entry['title']}\n")
    else:
        errors.append(f"Full-length test: {err}")
        print(f"  ⚠️  Skipped — {err[:80]}\n")

    # ── 2-4. Subject tests (Gemini Flash) ─────────────────────────────────────
    # Flash calls — 8s gap between each Flash call.
    # No wait needed after Pro call above (different model).
    test_topics, state = topic_picker.pick_multiple_topics("test", CLASS_SLUG, count=3, different_subjects=True)
    test_items_generated = []

    for i, t in enumerate(test_topics):
        item_num += 1
        print(f"[{item_num}/7] Subject test (Flash): {t['subjectName']}/{t['topic']}...")
        result, err = safe_run_with_model(
            f"subject test {i+1}", "flash",
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
            print(f"  ⚠️  Skipped — {err[:80]}\n")

    if test_items_generated:
        topic_picker.mark_generated(state, "test", test_items_generated)

    # ── 5-6. Notes (Groq 70B) ─────────────────────────────────────────────────
    # Groq calls — 65s gap between each Groq call (TPM resets per minute).
    # No wait needed after Flash calls above (different model).
    note_topics, state2 = topic_picker.pick_multiple_topics("note", CLASS_SLUG, count=2, different_subjects=True)
    note_items_generated = []

    for i, t in enumerate(note_topics):
        item_num += 1
        print(f"[{item_num}/7] Note (Groq 70B): {t['subjectName']}/{t['topic']}...")
        result, err = safe_run_with_model(
            f"note {i+1}", "groq",
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
            print(f"  ⚠️  Skipped — {err[:80]}\n")

    if note_items_generated:
        topic_picker.mark_generated(state2, "note", note_items_generated)

    # ── 7. Formula (Groq 70B) ─────────────────────────────────────────────────
    # Another Groq call — 65s gap enforced automatically since last Groq call.
    formula_topics, state3 = topic_picker.pick_multiple_topics("formula", CLASS_SLUG, count=1, different_subjects=True)
    formula_items_generated = []

    for i, t in enumerate(formula_topics):
        item_num += 1
        print(f"[{item_num}/7] Formula (Groq 70B): {t['subjectName']}/{t['topic']}...")
        result, err = safe_run_with_model(
            f"formula", "groq",
            content_builder.build_formula,
            t
        )
        if result:
            uid, path, entry = result
            index_entries.append(entry)
            formula_items_generated.append((t, uid))
            print(f"  ✅ Done: {entry['title']}\n")
        else:
            errors.append(f"Formula: {err}")
            print(f"  ⚠️  Skipped — {err[:80]}\n")

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
    print(f"  DONE — {success_count}/7 quality items generated")
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

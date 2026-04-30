"""
generate.py — EduTest content runner (Mode-based, same-exam per run).

Two modes decided by UTC hour (or GENERATE_MODE env var):

  MODE "full"    (UTC 00:xx = 5:30 AM IST)
    • 1 full-length test  (Flash-only pipeline — saves Pro quota)
    • 1 note              (Groq)
    • 1 formula           (Groq)
    • 1 resource          (Groq)
    All 4 items from the SAME randomly-picked exam.

  MODE "subject" (all other runs)
    • 1 subject/topic-wise test  (4-agent: Flash→Pro K1→Pro K2→Groq)
    • 2 notes                    (Groq)
    • 2 formulas                 (Groq)
    All 5 items from the SAME randomly-picked exam.

Daily API budget (4 runs: 1 full + 3 subject):
  Flash K1  : ~1 + 3×2   =  7 calls   (limit 1500 RPD) ✅
  Pro K1    : 0 + 3×1    =  3 calls   (limit 25 RPD)   ✅
  Pro K2    : 0 + 3×1    =  3 calls   (limit 25 RPD)   ✅
  Groq      : 4 + 3×5    = 19 calls   (limit 14400 RPD) ✅
  (Full-length NEET adds ~16 Flash calls but still well within limits)
"""
import os, sys, traceback
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import topic_picker
import content_builder
from gemini_client import GeminiError
from groq_client   import GroqError


# ── Helpers ───────────────────────────────────────────────────────────────────

def safe_run(label, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs), None
    except (GeminiError, GroqError) as e:
        msg = f"API error: {e}"
        print(f"  [SKIP] {label} — {msg[:120]}")
        return None, msg
    except Exception as e:
        msg = f"Unexpected error: {e}"
        print(f"  [SKIP] {label} — {msg[:120]}")
        traceback.print_exc()
        return None, msg


def get_mode():
    """
    Determine run mode.
    Priority: GENERATE_MODE env var → UTC-hour heuristic.
    First run of day (UTC 00:xx = 5:30 AM IST) → 'full', all others → 'subject'.
    """
    override = os.environ.get("GENERATE_MODE", "").strip().lower()
    if override in ("full", "subject"):
        print(f"  [Mode] Override via env: {override}")
        return override
    hour_utc = datetime.now(timezone.utc).hour
    mode = "full" if hour_utc == 0 else "subject"
    print(f"  [Mode] UTC hour={hour_utc} → {mode}")
    return mode


def pick_and_build(label, ct, class_slug, builder_fn, index_entries, errors):
    """Pick a topic from class_slug, call builder_fn, record result."""
    try:
        topic_info, state = topic_picker.pick_topic_from_exam(ct, class_slug)
        print(f"  Picked ({ct}): {topic_info['subjectName']} / {topic_info['topic']}")
        result, err = safe_run(label, builder_fn, topic_info)
        if result:
            uid, path, entry = result
            index_entries.append(entry)
            topic_picker.mark_generated(state, ct, [(topic_info, uid)])
            print(f"  ✅ {entry['title']}\n")
        else:
            errors.append(f"{label}: {err}")
    except Exception as e:
        errors.append(f"{label} picker: {e}")
        print(f"  ⚠️  {label} picker failed: {e}\n")


# ── Mode runners ──────────────────────────────────────────────────────────────

def run_full_mode(class_slug, index_entries, errors):
    """
    Full-length test + 1 note + 1 formula + 1 resource (all same exam).
    Uses Flash-only for test generation → preserves Pro quota.
    """
    print(f"\n{'─'*60}")
    print(f"  [1/4] Full-Length Test  ({class_slug.upper()})")
    print(f"{'─'*60}")
    try:
        result, err = safe_run("full-length test", content_builder.build_full_length_test, class_slug)
        if result:
            uid, path, entry = result
            index_entries.append(entry)
            print(f"  ✅ {entry['title']}\n")
        else:
            errors.append(f"Full-length test: {err}")
    except Exception as e:
        errors.append(f"Full-length test: {e}")
        print(f"  ⚠️  Full-length test failed: {e}\n")

    print(f"{'─'*60}")
    print(f"  [2/4] Note  (Groq)")
    pick_and_build("note", "note", class_slug, content_builder.build_note, index_entries, errors)

    print(f"{'─'*60}")
    print(f"  [3/4] Formula  (Groq)")
    pick_and_build("formula", "formula", class_slug, content_builder.build_formula, index_entries, errors)

    print(f"{'─'*60}")
    print(f"  [4/4] Resource  (Groq)")
    pick_and_build("resource", "resource", class_slug, content_builder.build_resource, index_entries, errors)


def run_subject_mode(class_slug, index_entries, errors):
    """
    Subject-wise test + 2 notes + 2 formulas (all same exam).
    Test uses 4-agent pipeline (Flash→Pro K1→Pro K2→Groq).
    """
    print(f"\n{'─'*60}")
    print(f"  [1/5] Subject Test  (4-agent: Flash→Pro K1→Pro K2→Groq)  ({class_slug.upper()})")
    print(f"{'─'*60}")
    pick_and_build("subject test", "test", class_slug, content_builder.build_subject_test, index_entries, errors)

    for i in range(2):
        num = i + 2
        print(f"{'─'*60}")
        print(f"  [{num}/5] Note {i+1}  (Groq)")
        pick_and_build(f"note {i+1}", "note", class_slug, content_builder.build_note, index_entries, errors)

    for i in range(2):
        num = i + 4
        print(f"{'─'*60}")
        print(f"  [{num}/5] Formula {i+1}  (Groq)")
        pick_and_build(f"formula {i+1}", "formula", class_slug, content_builder.build_formula, index_entries, errors)


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    print(f"\n{'='*65}")
    print(f"  EduTest Generator  |  Quota-Smart Mode")
    print(f"  Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*65}\n")

    mode       = get_mode()
    # Allow forcing a specific exam via env var (workflow_dispatch)
    forced_exam = os.environ.get("FORCE_EXAM", "").strip().lower()
    class_slug  = forced_exam if forced_exam else topic_picker.pick_random_exam()
    print(f"  Selected exam: {class_slug.upper()}{' (forced)' if forced_exam else ''}")

    index_entries = []
    errors        = []
    expected      = 4 if mode == "full" else 5

    if mode == "full":
        run_full_mode(class_slug, index_entries, errors)
    else:
        run_subject_mode(class_slug, index_entries, errors)

    # Final report
    ok = len(index_entries)
    print(f"\n{'='*65}")
    print(f"  DONE — {ok}/{expected} items generated  |  mode={mode}  |  exam={class_slug}")
    if errors:
        print(f"  Skipped {len(errors)} item(s):")
        for e in errors:
            print(f"    - {e}")
    print(f"{'='*65}\n")

    if ok == 0:
        print("ERROR: No items generated. Check API keys and quotas.")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    run()

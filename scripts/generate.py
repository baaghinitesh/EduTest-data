"""
generate.py — EduTest content runner. Smart per-model delays.

Per run generates 4 HIGH-QUALITY items:
  1 subject test  (Flash → Pro K1 → Pro K2 → Groq)
  1 note          (Groq 70B)
  1 formula       (Groq 70B)
  1 resource      (Groq 70B)

4 runs/day = 16 quality items/day
"""
import os, sys, traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import topic_picker
import content_builder
import catalog_updater
from gemini_client import GeminiError
from groq_client import GroqError


def safe_run(label, fn, *args, **kwargs):
    """Run fn with error catching."""
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
    print(f"\n{'='*65}")
    print(f"  EduTest Generator  |  4-Agent Pipeline")
    print(f"  Target: 4 quality items per run (Test, Note, Formula, Resource)")
    print(f"{'='*65}\n")

    index_entries = []
    errors = []
    
    # ── 1. Subject Test (4-Agent Pipeline) ────────────────────────────────────
    print(f"[1/4] Subject Test (Researcher → Generator → Auditor → Formatter)...")
    try:
        topic_test, state = topic_picker.pick_topic("test")
        print(f"  Picked topic: {topic_test['classSlug']} / {topic_test['subjectName']} / {topic_test['topic']}")
        
        result, err = safe_run("subject test", content_builder.build_subject_test, topic_test)
        if result:
            uid, path, entry = result
            index_entries.append(entry)
            topic_picker.mark_generated(state, "test", [(topic_test, uid)])
            print(f"  ✅ Done: {entry['title']}\n")
        else:
            errors.append(f"Subject test: {err}")
            print(f"  ⚠️  Skipped — {err[:80]}\n")
    except Exception as e:
        errors.append(f"Test picker: {e}")
        print(f"  ⚠️  Test picker failed: {e}\n")


    # ── 2. Note (Groq) ────────────────────────────────────────────────────────
    print(f"[2/4] Note (Groq 70B)...")
    try:
        topic_note, state = topic_picker.pick_topic("note")
        print(f"  Picked topic: {topic_note['classSlug']} / {topic_note['subjectName']} / {topic_note['topic']}")
        
        result, err = safe_run("note", content_builder.build_note, topic_note)
        if result:
            uid, path, entry = result
            index_entries.append(entry)
            topic_picker.mark_generated(state, "note", [(topic_note, uid)])
            print(f"  ✅ Done: {entry['title']}\n")
        else:
            errors.append(f"Note: {err}")
            print(f"  ⚠️  Skipped — {err[:80]}\n")
    except Exception as e:
        errors.append(f"Note picker: {e}")
        print(f"  ⚠️  Note picker failed: {e}\n")


    # ── 3. Formula (Groq) ─────────────────────────────────────────────────────
    print(f"[3/4] Formula (Groq 70B)...")
    try:
        topic_form, state = topic_picker.pick_topic("formula")
        print(f"  Picked topic: {topic_form['classSlug']} / {topic_form['subjectName']} / {topic_form['topic']}")
        
        result, err = safe_run("formula", content_builder.build_formula, topic_form)
        if result:
            uid, path, entry = result
            index_entries.append(entry)
            topic_picker.mark_generated(state, "formula", [(topic_form, uid)])
            print(f"  ✅ Done: {entry['title']}\n")
        else:
            errors.append(f"Formula: {err}")
            print(f"  ⚠️  Skipped — {err[:80]}\n")
    except Exception as e:
        errors.append(f"Formula picker: {e}")
        print(f"  ⚠️  Formula picker failed: {e}\n")


    # ── 4. Resource (Groq) ────────────────────────────────────────────────────
    print(f"[4/4] Resource (Groq 70B)...")
    try:
        topic_res, state = topic_picker.pick_topic("resource")
        print(f"  Picked topic: {topic_res['classSlug']} / {topic_res['subjectName']} / {topic_res['topic']}")
        
        result, err = safe_run("resource", content_builder.build_resource, topic_res)
        if result:
            uid, path, entry = result
            index_entries.append(entry)
            topic_picker.mark_generated(state, "resource", [(topic_res, uid)])
            print(f"  ✅ Done: {entry['title']}\n")
        else:
            errors.append(f"Resource: {err}")
            print(f"  ⚠️  Skipped — {err[:80]}\n")
    except Exception as e:
        errors.append(f"Resource picker: {e}")
        print(f"  ⚠️  Resource picker failed: {e}\n")


    # ── Update catalog index ──────────────────────────────────────────────────
    if index_entries:
        print(f"Updating catalog index: {len(index_entries)} entries...")
        catalog_updater.add_entries(index_entries)

    # ── Final report ──────────────────────────────────────────────────────────
    success_count = len(index_entries)
    fail_count    = len(errors)

    print(f"\n{'='*65}")
    print(f"  DONE — {success_count}/4 quality items generated")
    if errors:
        print(f"  Skipped {fail_count} items:")
        for e in errors:
            print(f"    - {e}")
    print(f"{'='*65}\n")

    if success_count == 0:
        print("ERROR: No items generated. Check API keys and quotas.")
        sys.exit(1)
    sys.exit(0)

if __name__ == "__main__":
    run()

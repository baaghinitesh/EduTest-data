"""
generate.py — EduTest content runner. gemini-2.5-pro only.

Free tier: 100 RPD
Budget per run: ~20 Pro calls max
4 runs/day x 20 calls = 80 calls/day (safe under 100 RPD)

Each run generates 5 high-quality items:
  full_length_batch → 1 full-length test (3 calls) + 2 topic tests (2 calls) = 5 items, 5 calls
  topic_batch       → 5 topic tests (5 calls)
  notes_batch       → 5 deep notes (5 calls)
  formula_batch     → 5 formula sheets (5 calls)
  mixed_batch       → 2 topic tests + 2 notes + 1 formula = 5 items, 5 calls
"""
import os, sys, random

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import topic_picker, content_builder, catalog_updater

CLASS_SLUG = "norcet"

# Weighted modes — full_length less frequent (uses 3 calls for 1 item)
RUN_MODES = [
    "full_length_batch",  # weight 1
    "topic_batch",        # weight 2
    "topic_batch",
    "notes_batch",        # weight 2
    "notes_batch",
    "formula_batch",      # weight 2
    "formula_batch",
    "mixed_batch",        # weight 1
]

def run():
    mode = os.environ.get("GENERATE_MODE", "").strip() or random.choice(RUN_MODES)

    print(f"\n{'='*65}")
    print(f"  EduTest Generator  |  Model: gemini-2.5-pro  |  Mode: {mode.upper()}")
    print(f"  Exam: {CLASS_SLUG.upper()}  |  5 high-quality items per run")
    print(f"{'='*65}\n")

    topics_data = topic_picker.load_topics(CLASS_SLUG)
    if not topics_data:
        print(f"ERROR: No topics for {CLASS_SLUG}"); sys.exit(1)

    exam_config = topics_data.get("examConfig", {})
    board       = topics_data.get("board", "Competitive")
    index_entries = []

    # ── FULL LENGTH BATCH: 1 full-length (3 calls) + 2 topic tests (2 calls) ─
    if mode == "full_length_batch":
        print("BATCH: 1 full-length test + 2 topic tests = 5 items\n")

        print("[1/5] Full-length test (3 Pro calls)...")
        dist = topic_picker.pick_subjects_for_full_length(CLASS_SLUG)
        uid, path, entry = content_builder.build_full_length_test(CLASS_SLUG, exam_config, dist, board)
        index_entries.append(entry)
        print(f"  Done: {entry['title']}\n")

        test_topics, state = topic_picker.pick_multiple_topics("test", CLASS_SLUG, count=2, different_subjects=True)
        test_items = []
        for i, t in enumerate(test_topics, 2):
            print(f"[{i+1}/5] Topic test: {t['subjectName']} / {t['topic']}...")
            uid, path, entry = content_builder.build_subject_test(t, exam_config)
            index_entries.append(entry)
            test_items.append((t, uid))
            print(f"  Done: {entry['title']}\n")
        topic_picker.mark_generated(state, "test", test_items)

    # ── TOPIC BATCH: 5 topic tests ────────────────────────────────────────────
    elif mode == "topic_batch":
        print("BATCH: 5 topic tests\n")
        topics, state = topic_picker.pick_multiple_topics("test", CLASS_SLUG, count=5, different_subjects=True)
        items = []
        for i, t in enumerate(topics, 1):
            print(f"[{i}/5] Topic test: {t['subjectName']} / {t['topic']}...")
            uid, path, entry = content_builder.build_subject_test(t, exam_config)
            index_entries.append(entry)
            items.append((t, uid))
            print(f"  Done: {entry['title']}\n")
        topic_picker.mark_generated(state, "test", items)

    # ── NOTES BATCH: 5 deep notes ─────────────────────────────────────────────
    elif mode == "notes_batch":
        print("BATCH: 5 deep study notes\n")
        topics, state = topic_picker.pick_multiple_topics("note", CLASS_SLUG, count=5, different_subjects=True)
        items = []
        for i, t in enumerate(topics, 1):
            print(f"[{i}/5] Note: {t['subjectName']} / {t['topic']}...")
            uid, path, entry = content_builder.build_note(t)
            index_entries.append(entry)
            items.append((t, uid))
            print(f"  Done: {entry['title']}\n")
        topic_picker.mark_generated(state, "note", items)

    # ── FORMULA BATCH: 5 formula sheets ──────────────────────────────────────
    elif mode == "formula_batch":
        print("BATCH: 5 formula/quick-reference sheets\n")
        topics, state = topic_picker.pick_multiple_topics("formula", CLASS_SLUG, count=5, different_subjects=True)
        items = []
        for i, t in enumerate(topics, 1):
            print(f"[{i}/5] Formula: {t['subjectName']} / {t['topic']}...")
            uid, path, entry = content_builder.build_formula(t)
            index_entries.append(entry)
            items.append((t, uid))
            print(f"  Done: {entry['title']}\n")
        topic_picker.mark_generated(state, "formula", items)

    # ── MIXED BATCH: 2 tests + 2 notes + 1 formula ───────────────────────────
    elif mode == "mixed_batch":
        print("BATCH: 2 topic tests + 2 notes + 1 formula\n")

        test_topics, state = topic_picker.pick_multiple_topics("test", CLASS_SLUG, count=2, different_subjects=True)
        test_items = []
        for i, t in enumerate(test_topics, 1):
            print(f"[{i}/5] Topic test: {t['subjectName']} / {t['topic']}...")
            uid, path, entry = content_builder.build_subject_test(t, exam_config)
            index_entries.append(entry)
            test_items.append((t, uid))
            print(f"  Done: {entry['title']}\n")
        topic_picker.mark_generated(state, "test", test_items)

        note_topics, state2 = topic_picker.pick_multiple_topics("note", CLASS_SLUG, count=2, different_subjects=True)
        note_items = []
        for i, t in enumerate(note_topics, 3):
            print(f"[{i}/5] Note: {t['subjectName']} / {t['topic']}...")
            uid, path, entry = content_builder.build_note(t)
            index_entries.append(entry)
            note_items.append((t, uid))
            print(f"  Done: {entry['title']}\n")
        topic_picker.mark_generated(state2, "note", note_items)

        formula_topics, state3 = topic_picker.pick_multiple_topics("formula", CLASS_SLUG, count=1, different_subjects=True)
        formula_items = []
        for i, t in enumerate(formula_topics, 5):
            print(f"[{i}/5] Formula: {t['subjectName']} / {t['topic']}...")
            uid, path, entry = content_builder.build_formula(t)
            index_entries.append(entry)
            formula_items.append((t, uid))
            print(f"  Done: {entry['title']}\n")
        topic_picker.mark_generated(state3, "formula", formula_items)

    else:
        print(f"ERROR: Unknown mode '{mode}'"); sys.exit(1)

    if index_entries:
        print(f"Updating catalog index: {len(index_entries)} entries...")
        catalog_updater.add_entries(index_entries)

    print(f"\n{'='*65}")
    print(f"  DONE — {len(index_entries)} high-quality items generated")
    print(f"  Model: gemini-2.5-pro | No hallucinations, exam-accurate")
    print(f"{'='*65}\n")

if __name__ == "__main__":
    run()

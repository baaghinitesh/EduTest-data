"""
generate.py — EduTest content automation runner.
Called by GitHub Actions 4 times per day.

Each run generates 10 content items:
  full_length_test  → 1 full-length test (100Q, Flash) + 4 topic tests (Pro) + 5 notes (Flash)
  subject_batch     → 6 topic tests (Pro) + 4 formula sheets (Pro)
  notes_batch       → 5 long notes (Flash) + 5 formula sheets (Pro)
  mixed_batch       → 1 multi-subject test (Flash) + 4 topic tests (Pro) + 5 formulas (Pro)

Model assignment:
  Flash → full-length tests, multi-subject tests, long notes
  Pro   → topic/subject tests (25-30Q), formula sheets
"""
import os, sys, random

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import topic_picker
import content_builder
import catalog_updater

# ── Config ────────────────────────────────────────────────────────────────────
CLASS_SLUG = "norcet"

# Weighted run modes — each produces exactly 10 content items
RUN_MODES = [
    "full_length_batch",   # 1 full-length(Flash) + 4 topic tests(Pro) + 5 notes(Flash)
    "subject_batch",       # 6 topic tests(Pro) + 4 formulas(Pro)
    "subject_batch",       # doubled weight
    "notes_batch",         # 5 notes(Flash) + 5 formulas(Pro)
    "notes_batch",         # doubled weight
    "mixed_batch",         # 1 multi-subject(Flash) + 4 topic tests(Pro) + 5 formulas(Pro)
]

def run():
    mode = os.environ.get("GENERATE_MODE", "").strip()
    if not mode:
        mode = random.choice(RUN_MODES)

    print(f"\n{'='*65}")
    print(f"  EduTest Content Generator  |  Mode: {mode.upper()}")
    print(f"  Exam: {CLASS_SLUG.upper()}  |  Target: 10 items per run")
    print(f"{'='*65}\n")

    topics_data = topic_picker.load_topics(CLASS_SLUG)
    if not topics_data:
        print(f"ERROR: No topics found for {CLASS_SLUG}")
        sys.exit(1)

    exam_config = topics_data.get("examConfig", {})
    board = topics_data.get("board", "Competitive")
    index_entries = []
    generated_items = []   # (topic_dict, uid, content_type)

    # ── FULL LENGTH BATCH ─────────────────────────────────────────────────────
    # 1 full-length test (Flash) + 4 topic tests (Pro) + 5 notes (Flash) = 10
    if mode == "full_length_batch":
        print("BATCH: 1 full-length test + 4 topic tests + 5 notes\n")

        # 1. Full-length test (Flash)
        print("[1/10] Full-length test (Flash)...")
        subjects_dist = topic_picker.pick_subjects_for_full_length(CLASS_SLUG)
        uid, path, entry = content_builder.build_full_length_test(
            CLASS_SLUG, exam_config, subjects_dist, board
        )
        index_entries.append(entry)
        print(f"  Done: {entry['title']}\n")

        # 2-5. Topic tests (Pro)
        test_topics, state = topic_picker.pick_multiple_topics("test", CLASS_SLUG, count=4, different_subjects=True)
        for i, topic_info in enumerate(test_topics, 2):
            print(f"[{i}/10] Topic test (Pro): {topic_info['subjectName']} / {topic_info['topic']}...")
            uid, path, entry = content_builder.build_subject_test(topic_info, exam_config)
            index_entries.append(entry)
            generated_items.append((topic_info, uid, "test"))
            print(f"  Done: {entry['title']}\n")
        topic_picker.mark_generated(state, "test", [(t, u) for t, u, _ in generated_items if _ == "test"])

        # 6-10. Notes (Flash)
        note_topics, state2 = topic_picker.pick_multiple_topics("note", CLASS_SLUG, count=5, different_subjects=True)
        note_items = []
        for i, topic_info in enumerate(note_topics, 6):
            print(f"[{i}/10] Study note (Flash): {topic_info['subjectName']} / {topic_info['topic']}...")
            uid, path, entry = content_builder.build_note(topic_info)
            index_entries.append(entry)
            note_items.append((topic_info, uid))
            print(f"  Done: {entry['title']}\n")
        topic_picker.mark_generated(state2, "note", note_items)

    # ── SUBJECT BATCH ─────────────────────────────────────────────────────────
    # 6 topic tests (Pro) + 4 formula sheets (Pro) = 10
    elif mode == "subject_batch":
        print("BATCH: 6 topic tests + 4 formula sheets\n")

        # 1-6. Topic tests (Pro)
        test_topics, state = topic_picker.pick_multiple_topics("test", CLASS_SLUG, count=6, different_subjects=True)
        test_items = []
        for i, topic_info in enumerate(test_topics, 1):
            print(f"[{i}/10] Topic test (Pro): {topic_info['subjectName']} / {topic_info['topic']}...")
            uid, path, entry = content_builder.build_subject_test(topic_info, exam_config)
            index_entries.append(entry)
            test_items.append((topic_info, uid))
            print(f"  Done: {entry['title']}\n")
        topic_picker.mark_generated(state, "test", test_items)

        # 7-10. Formula sheets (Pro)
        formula_topics, state2 = topic_picker.pick_multiple_topics("formula", CLASS_SLUG, count=4, different_subjects=True)
        formula_items = []
        for i, topic_info in enumerate(formula_topics, 7):
            print(f"[{i}/10] Formula sheet (Pro): {topic_info['subjectName']} / {topic_info['topic']}...")
            uid, path, entry = content_builder.build_formula(topic_info)
            index_entries.append(entry)
            formula_items.append((topic_info, uid))
            print(f"  Done: {entry['title']}\n")
        topic_picker.mark_generated(state2, "formula", formula_items)

    # ── NOTES BATCH ───────────────────────────────────────────────────────────
    # 5 long notes (Flash) + 5 formula sheets (Pro) = 10
    elif mode == "notes_batch":
        print("BATCH: 5 long notes + 5 formula sheets\n")

        # 1-5. Notes (Flash)
        note_topics, state = topic_picker.pick_multiple_topics("note", CLASS_SLUG, count=5, different_subjects=True)
        note_items = []
        for i, topic_info in enumerate(note_topics, 1):
            print(f"[{i}/10] Study note (Flash): {topic_info['subjectName']} / {topic_info['topic']}...")
            uid, path, entry = content_builder.build_note(topic_info)
            index_entries.append(entry)
            note_items.append((topic_info, uid))
            print(f"  Done: {entry['title']}\n")
        topic_picker.mark_generated(state, "note", note_items)

        # 6-10. Formula sheets (Pro)
        formula_topics, state2 = topic_picker.pick_multiple_topics("formula", CLASS_SLUG, count=5, different_subjects=True)
        formula_items = []
        for i, topic_info in enumerate(formula_topics, 6):
            print(f"[{i}/10] Formula sheet (Pro): {topic_info['subjectName']} / {topic_info['topic']}...")
            uid, path, entry = content_builder.build_formula(topic_info)
            index_entries.append(entry)
            formula_items.append((topic_info, uid))
            print(f"  Done: {entry['title']}\n")
        topic_picker.mark_generated(state2, "formula", formula_items)

    # ── MIXED BATCH ───────────────────────────────────────────────────────────
    # 1 multi-subject test (Flash) + 4 topic tests (Pro) + 5 formulas (Pro) = 10
    elif mode == "mixed_batch":
        print("BATCH: 1 multi-subject test + 4 topic tests + 5 formulas\n")

        # 1. Multi-subject test (Flash)
        print("[1/10] Multi-subject test (Flash)...")
        multi_topics, state = topic_picker.pick_multiple_topics("test", CLASS_SLUG, count=5, different_subjects=True)
        uid, path, entry = content_builder.build_multi_subject_test(multi_topics, exam_config)
        index_entries.append(entry)
        multi_items = [(t, uid) for t in multi_topics]
        topic_picker.mark_generated(state, "test", multi_items)
        print(f"  Done: {entry['title']}\n")

        # 2-5. Topic tests (Pro)
        test_topics, state2 = topic_picker.pick_multiple_topics("test", CLASS_SLUG, count=4, different_subjects=True)
        test_items = []
        for i, topic_info in enumerate(test_topics, 2):
            print(f"[{i}/10] Topic test (Pro): {topic_info['subjectName']} / {topic_info['topic']}...")
            uid, path, entry = content_builder.build_subject_test(topic_info, exam_config)
            index_entries.append(entry)
            test_items.append((topic_info, uid))
            print(f"  Done: {entry['title']}\n")
        topic_picker.mark_generated(state2, "test", test_items)

        # 6-10. Formula sheets (Pro)
        formula_topics, state3 = topic_picker.pick_multiple_topics("formula", CLASS_SLUG, count=5, different_subjects=True)
        formula_items = []
        for i, topic_info in enumerate(formula_topics, 6):
            print(f"[{i}/10] Formula sheet (Pro): {topic_info['subjectName']} / {topic_info['topic']}...")
            uid, path, entry = content_builder.build_formula(topic_info)
            index_entries.append(entry)
            formula_items.append((topic_info, uid))
            print(f"  Done: {entry['title']}\n")
        topic_picker.mark_generated(state3, "formula", formula_items)

    else:
        print(f"ERROR: Unknown mode '{mode}'")
        sys.exit(1)

    # ── Update catalog index ──────────────────────────────────────────────────
    if index_entries:
        print(f"Updating catalog index with {len(index_entries)} entries...")
        catalog_updater.add_entries(index_entries)

    print(f"\n{'='*65}")
    print(f"  DONE! Generated {len(index_entries)}/10 content items")
    print(f"  Flash calls: full-length tests, multi-subject tests, notes")
    print(f"  Pro calls:   topic tests (25Q), formula sheets")
    print(f"{'='*65}\n")

if __name__ == "__main__":
    run()

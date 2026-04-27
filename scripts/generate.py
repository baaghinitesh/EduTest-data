"""
generate.py — Main automation runner for EduTest content generation.
Called by GitHub Actions 3 times per day.

Run modes (randomly selected each run):
  full_length_test  → 1 full-length test (all subjects, exact exam marks/questions)
  subject_tests     → 4 subject-wise chapter tests
  multi_subject     → 1 multi-subject mock test (100q covering multiple subjects)
  notes             → 5 high-quality study notes
  formulas          → 5 formula sheets
"""
import os, sys, random

# Add scripts dir to path so imports work
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import topic_picker
import content_builder
import catalog_updater

# ── Config ────────────────────────────────────────────────────────────────────
CLASS_SLUG = "norcet"   # Primary exam to generate content for

# Weighted run modes — full_length less frequent (it is heavy)
RUN_MODES = [
    "full_length_test",   # weight 1
    "subject_tests",      # weight 2
    "subject_tests",      # weight 2 (doubled for more subject tests)
    "multi_subject",      # weight 1
    "notes",              # weight 2
    "notes",              # weight 2
    "formulas",           # weight 2
    "formulas",           # weight 2
]

def run():
    # Allow override via env var (for manual runs)
    mode = os.environ.get("GENERATE_MODE", "").strip()
    if not mode:
        mode = random.choice(RUN_MODES)

    print(f"\n{'='*60}")
    print(f"  EduTest Content Generator")
    print(f"  Mode: {mode.upper()}")
    print(f"  Exam: {CLASS_SLUG.upper()}")
    print(f"{'='*60}\n")

    index_entries = []
    generated_items = []  # list of (topic_dict, uid)

    topics_data = topic_picker.load_topics(CLASS_SLUG)
    if not topics_data:
        print(f"ERROR: No topics found for {CLASS_SLUG}")
        sys.exit(1)

    exam_config = topics_data.get("examConfig", {})

    # ── Full-length test ──────────────────────────────────────────────────────
    if mode == "full_length_test":
        print("Generating 1 full-length test...")
        subjects_dist = topic_picker.pick_subjects_for_full_length(CLASS_SLUG)
        if not subjects_dist:
            print("ERROR: No subjects distribution available")
            sys.exit(1)

        uid, path, entry = content_builder.build_full_length_test(
            CLASS_SLUG, exam_config, subjects_dist,
            topics_data.get("board", "Competitive")
        )
        index_entries.append(entry)
        print(f"\n  Full-length test generated: {entry['title']}")
        print(f"  Path: {path}")

    # ── Subject-wise tests (4 at once) ────────────────────────────────────────
    elif mode == "subject_tests":
        print("Generating 4 subject-wise tests...")
        picked_topics, state = topic_picker.pick_multiple_topics("test", CLASS_SLUG, count=4, different_subjects=True)

        for i, topic_info in enumerate(picked_topics, 1):
            print(f"\n  Test {i}/4:")
            uid, path, entry = content_builder.build_subject_test(topic_info, exam_config, i)
            index_entries.append(entry)
            generated_items.append((topic_info, uid))
            print(f"  Generated: {entry['title']}")

        topic_picker.mark_generated(state, "test", generated_items)

    # ── Multi-subject test ────────────────────────────────────────────────────
    elif mode == "multi_subject":
        print("Generating 1 multi-subject test...")
        # Pick topics from different subjects
        picked_topics, state = topic_picker.pick_multiple_topics("test", CLASS_SLUG, count=5, different_subjects=True)

        uid, path, entry = content_builder.build_multi_subject_test(picked_topics, exam_config)
        index_entries.append(entry)
        generated_items = [(t, uid) for t in picked_topics]
        topic_picker.mark_generated(state, "test", generated_items)
        print(f"\n  Multi-subject test generated: {entry['title']}")

    # ── Notes (5 at once) ─────────────────────────────────────────────────────
    elif mode == "notes":
        print("Generating 5 study notes...")
        picked_topics, state = topic_picker.pick_multiple_topics("note", CLASS_SLUG, count=5, different_subjects=True)

        for i, topic_info in enumerate(picked_topics, 1):
            print(f"\n  Note {i}/5:")
            uid, path, entry = content_builder.build_note(topic_info)
            index_entries.append(entry)
            generated_items.append((topic_info, uid))
            print(f"  Generated: {entry['title']}")

        topic_picker.mark_generated(state, "note", generated_items)

    # ── Formulas (5 at once) ──────────────────────────────────────────────────
    elif mode == "formulas":
        print("Generating 5 formula sheets...")
        picked_topics, state = topic_picker.pick_multiple_topics("formula", CLASS_SLUG, count=5, different_subjects=True)

        for i, topic_info in enumerate(picked_topics, 1):
            print(f"\n  Formula {i}/5:")
            uid, path, entry = content_builder.build_formula(topic_info)
            index_entries.append(entry)
            generated_items.append((topic_info, uid))
            print(f"  Generated: {entry['title']}")

        topic_picker.mark_generated(state, "formula", generated_items)

    else:
        print(f"ERROR: Unknown mode '{mode}'")
        sys.exit(1)

    # ── Update catalog index ──────────────────────────────────────────────────
    if index_entries:
        print(f"\nUpdating catalog index with {len(index_entries)} entries...")
        catalog_updater.add_entries(index_entries)

    print(f"\n{'='*60}")
    print(f"  DONE! Generated {len(index_entries)} content item(s)")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    run()

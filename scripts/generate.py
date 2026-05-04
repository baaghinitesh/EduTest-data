"""
generate.py — EduTest TEST-ONLY generator.

════════════════════════════════════════════════════════════════════
  BEHAVIOUR RULES
════════════════════════════════════════════════════════════════════

  ┌─────────────────────────────────────────────────────────────────┐
  │  SCHOOL CLASSES  (type = "school")                              │
  │  e.g. CBSE Class 10, ICSE Class 9, Maharashtra Class 7 …       │
  │                                                                 │
  │  full-length  → pick ONE subject at random, generate 100Q       │
  │                 spanning ALL chapters of that subject.          │
  │                 Title: "CBSE CLASS 10 — Mathematics |           │
  │                          Full Length Test"                      │
  │                 Duration: 150 min                               │
  │                                                                 │
  │  subject-wise → pick ONE subject + ONE topic, generate          │
  │                 25Q, 40Q, or 50Q (chosen randomly each run).    │
  │                 Title: "CBSE CLASS 10 — Mathematics | Algebra"  │
  │                 Duration: 60 min                                │
  │                                                                 │
  │  NO combined tests for school classes.                          │
  │  Same subject can repeat across runs — only the specific        │
  │  chapter+topic is tracked to avoid question repetition.         │
  └─────────────────────────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────────────────────────┐
  │  COMPETITIVE EXAMS  (type = "competitive")                      │
  │  e.g. NEET-UG, JEE Mains, NORCET, CDS, UPSC CSE …             │
  │                                                                 │
  │  full-length  → ALL subjects combined, exact question count     │
  │                 per subject from full-length.json / weightage.  │
  │                 Title: "NEET UG — Full Length Mock Test"        │
  │                                                                 │
  │  subject-wise → pick ONE subject, generate subjectWiseQ         │
  │                 (exam-configured, e.g. 40-45Q) on a topic.      │
  │                 Title: "NEET UG — Physics | Thermodynamics"     │
  │                                                                 │
  │  combined     → pick 2-3 subjects, subjectWiseQ each.           │
  │                 Title: "NEET UG — Combined: Physics + Chemistry"│
  │                 50% chance vs single-subject.                   │
  └─────────────────────────────────────────────────────────────────┘

  Mode decision (per run):
    Priority 1: GENERATE_MODE env var ("full" or "subject")
    Priority 2: UTC hour — 00:xx = "full", all others = "subject"

  No-repeat tracking:
    Each (classSlug, subjectSlug, chapter, topic) combo is marked
    used after generation. When all topics for a class are exhausted,
    the tracker resets — so the same subject can be picked again
    with a fresh set of topics (no question repetition guaranteed
    within a cycle).

API keys (5 total — exact GitHub secret names):
  GEMINI_API_KEY    → Flash K1  (researcher + gen fallback)
  GEMINI_API_KEY_1  → Flash K2  (researcher fallback + gen fallback)
  GEMINI_API_KEY2   → Flash K3  (primary question generator, round-robin)
  GROQ_API_KEY      → Groq K1   (primary structurer)
  GROQ_API_KEY2     → Groq K2   (fallback structurer)
"""
import os, sys, traceback, random
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import topic_picker
import content_builder
from gemini_client import GeminiError
from groq_client   import GroqError


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe(label, fn, *args, **kwargs):
    """Run fn, return (result, None) or (None, error_str)."""
    try:
        return fn(*args, **kwargs), None
    except (GeminiError, GroqError) as e:
        msg = f"API error: {e}"
        print(f"  [FAIL] {label} — {msg[:150]}")
        return None, msg
    except Exception as e:
        msg = f"Unexpected: {e}"
        print(f"  [FAIL] {label} — {msg[:150]}")
        traceback.print_exc()
        return None, msg


def _get_mode():
    override = os.environ.get("GENERATE_MODE", "").strip().lower()
    if override in ("full", "subject"):
        print(f"  [Mode] env override → {override}")
        return override
    hour = datetime.now(timezone.utc).hour
    mode = "full" if hour == 0 else "subject"
    print(f"  [Mode] UTC hour={hour} → {mode}")
    return mode


def _is_school(class_slug):
    """Return True if this slug belongs to a school class (not a competitive exam)."""
    topics_data = topic_picker.load_topics(class_slug)
    return (topics_data or {}).get("type", "competitive") == "school"


def _validate_keys():
    keys = {
        "GEMINI_API_KEY":   "Flash K1 (researcher + gen fallback)",
        "GEMINI_API_KEY_1": "Flash K2 (researcher fallback + gen fallback)",
        "GEMINI_API_KEY2":  "Pro/Flash K3 (primary generator)",
        "GROQ_API_KEY":     "Groq K1 (primary structurer)",
        "GROQ_API_KEY2":    "Groq K2 (fallback structurer)",
    }
    print("\n  API Keys:")
    missing_critical = []
    for env, role in keys.items():
        val = os.environ.get(env, "").strip()
        if val:
            print(f"    ✅ {env} ({role}) — {len(val)} chars")
        else:
            print(f"    ❌ {env} ({role}) — NOT SET")
            if env in ("GEMINI_API_KEY2", "GROQ_API_KEY"):
                missing_critical.append(env)
    if missing_critical:
        print(f"\n  ⚠️  Critical keys missing: {missing_critical}")
        print("  Generation will attempt fallbacks but may fail.\n")


# ── Full-length runners ───────────────────────────────────────────────────────

def run_full(class_slug):
    """
    Generate one full-length test.

    SCHOOL CLASS  → pick ONE subject, generate 100Q spanning all its chapters.
    COMPETITIVE   → ALL subjects combined, exact per-subject question counts.
    """
    print(f"\n{'─'*65}")

    if _is_school(class_slug):
        # ── School: single-subject full-length (100Q) ─────────────────────────
        print(f"  FULL-LENGTH TEST (School — Single Subject)  |  {class_slug.upper()}")
        print(f"{'─'*65}")
        result, err = _safe(
            "school full-length test",
            content_builder.build_school_full_length_test,
            class_slug
        )
    else:
        # ── Competitive: all-subjects full-length ─────────────────────────────
        print(f"  FULL-LENGTH TEST (Exam — All Subjects)  |  {class_slug.upper()}")
        print(f"{'─'*65}")
        result, err = _safe(
            "exam full-length test",
            content_builder.build_exam_full_length_test,
            class_slug
        )

    if result:
        uid, path, entry = result
        print(f"\n  ✅ Generated: {entry['title']}")
        print(f"     {entry['totalQuestions']}Q | {entry['duration']}min | {entry['totalMarks']} marks")
        return entry
    else:
        print(f"\n  ❌ Full-length test failed: {err}")
        return None


# ── Subject-wise / combined runner ───────────────────────────────────────────

# School subject-wise sizes — picked randomly each run
_SCHOOL_SIZES = [25, 40, 50]

def run_subject(class_slug):
    """
    Generate one subject-wise test (school) or subject-wise / combined test (exam).

    SCHOOL CLASSES:
      - Always single-subject, no combined.
      - Question count chosen randomly from [25, 40, 50] each run.
      - Same subject can be picked again on future runs (different topic).

    COMPETITIVE EXAMS:
      - 50% chance combined (2-3 subjects) vs single-subject.
      - Question count from examConfig.subjectWiseQuestions.
    """
    print(f"\n{'─'*65}")

    if _is_school(class_slug):
        # ── School: always single-subject, random size ─────────────────────────
        q_count = random.choice(_SCHOOL_SIZES)
        print(f"  SUBJECT-WISE TEST (School — {q_count}Q)  |  {class_slug.upper()}")
        print(f"{'─'*65}")

        try:
            topic_info, state = topic_picker.pick_topic("test", class_slug)
            print(f"  Topic: {topic_info['subjectName']} / {topic_info['chapter']} / {topic_info['topic']}")
            print(f"  Size:  {q_count}Q (randomly chosen from 25/40/50)")

            result, err = _safe(
                "school subject test",
                content_builder.build_subject_test,
                topic_info,
                q_count_override=q_count
            )
            if result:
                uid, path, entry = result
                topic_picker.mark_generated(state, "test", [(topic_info, uid)])
                print(f"\n  ✅ Generated: {entry['title']}")
                print(f"     {entry['totalQuestions']}Q | {entry['duration']}min | {entry['totalMarks']} marks")
                return entry
            else:
                print(f"\n  ❌ School subject test failed: {err}")
                return None

        except Exception as e:
            print(f"\n  ❌ Topic pick failed: {e}")
            return None

    # ── Competitive: combined (50%) or single-subject ─────────────────────────
    use_combined = random.random() < 0.5
    topics_data  = topic_picker.load_topics(class_slug)
    num_subjects = len((topics_data or {}).get("subjects", []))

    if use_combined and num_subjects >= 2:
        n = 2 if num_subjects == 2 else random.choice([2, 3])
        n = min(n, num_subjects)
        print(f"  COMBINED TEST ({n} subjects)  |  {class_slug.upper()}")
        print(f"{'─'*65}")

        try:
            picked_topics = []
            used_subjects = set()
            state = topic_picker.load_state()

            for _ in range(n * 3):
                if len(picked_topics) >= n:
                    break
                topic_info, state = topic_picker.pick_topic("test", class_slug)
                if topic_info["subjectSlug"] not in used_subjects:
                    picked_topics.append(topic_info)
                    used_subjects.add(topic_info["subjectSlug"])

            if len(picked_topics) < 2:
                raise ValueError(f"Could not pick {n} different subjects")

            result, err = _safe(
                "combined test",
                content_builder.build_combined_test,
                class_slug, picked_topics
            )
            if result:
                uid, path, entry = result
                topic_picker.mark_generated(
                    state, "test",
                    [(t, uid) for t in picked_topics]
                )
                print(f"\n  ✅ Generated: {entry['title']}")
                print(f"     {entry['totalQuestions']}Q | {entry['duration']}min | {entry['totalMarks']} marks")
                return entry
            else:
                print(f"\n  ❌ Combined test failed: {err}")
                print("  Falling back to single-subject test...")

        except Exception as e:
            print(f"  ⚠️  Combined test setup failed ({e}) — falling back to single-subject")

    # ── Competitive single-subject ─────────────────────────────────────────────
    print(f"  SUBJECT-WISE TEST  |  {class_slug.upper()}")
    print(f"{'─'*65}")

    try:
        topic_info, state = topic_picker.pick_topic("test", class_slug)
        print(f"  Topic: {topic_info['subjectName']} / {topic_info['chapter']} / {topic_info['topic']}")

        result, err = _safe("subject test", content_builder.build_subject_test, topic_info)
        if result:
            uid, path, entry = result
            topic_picker.mark_generated(state, "test", [(topic_info, uid)])
            print(f"\n  ✅ Generated: {entry['title']}")
            print(f"     {entry['totalQuestions']}Q | {entry['duration']}min | {entry['totalMarks']} marks")
            return entry
        else:
            print(f"\n  ❌ Subject test failed: {err}")
            return None

    except Exception as e:
        print(f"\n  ❌ Topic pick failed: {e}")
        return None


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    print(f"\n{'='*65}")
    print(f"  EduTest Generator  |  Tests Only")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*65}")

    _validate_keys()

    mode        = _get_mode()
    forced_exam = os.environ.get("FORCE_EXAM", "").strip().lower()
    class_slug  = forced_exam if forced_exam else topic_picker.pick_random_exam()
    forced_str  = " (forced)" if forced_exam else ""

    topics_data  = topic_picker.load_topics(class_slug)
    exam_type    = (topics_data or {}).get("type", "competitive")
    type_label   = "School Class" if exam_type == "school" else "Competitive Exam"

    print(f"\n  Exam: {class_slug.upper()}{forced_str}")
    print(f"  Type: {type_label}")
    print(f"  Mode: {mode.upper()}\n")

    if mode == "full":
        entry = run_full(class_slug)
    else:
        entry = run_subject(class_slug)

    print(f"\n{'='*65}")
    if entry:
        print(f"  ✅ SUCCESS — 1 test generated")
        print(f"  Title: {entry['title']}")
        print(f"  Type:  {entry['testType']}")
        print(f"  Path:  {entry.get('path', 'N/A')}")
    else:
        print(f"  ❌ FAILED — 0 tests generated")
        print(f"  Check API keys and quotas above.")
    print(f"{'='*65}\n")

    if not entry:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    run()

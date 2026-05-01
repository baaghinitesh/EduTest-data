"""
generate.py — EduTest TEST-ONLY generator.

Each run produces exactly ONE test:
  MODE "full"    → 1 full-length test  (all subjects, exact question count from examConfig)
  MODE "subject" → 1 subject-wise OR combined test (2-3 subjects, 50/50 chance)

Mode decision:
  Priority 1: GENERATE_MODE env var ("full" or "subject")
  Priority 2: UTC hour — 00:xx = "full", all others = "subject"

Pipeline per test:
  Step 1 — Research   : Flash K1 → Flash K2 → cached → empty (never crashes)
  Step 2 — Generate   : Pro K2 → Flash K2 → Flash K1 → Flash K2 (raw text)
  Step 3 — Structure  : Groq K1 → Groq K2 → Flash K1 → Flash K2 (JSON)

API keys used (5 total — exact GitHub secret names):
  GEMINI_API_KEY    → Flash K1  (researcher + gen fallback)
  GEMINI_API_KEY_1  → Flash K2  (researcher fallback + gen fallback)
  GEMINI_API_KEY2   → Flash K3  (primary question generator, round-robin)
  GROQ_API_KEY      → Groq K1   (primary structurer)
  GROQ_API_KEY2     → Groq K2   (fallback structurer)

Daily budget (4 runs: 1 full + 3 subject):
  Flash K1/K2/K3 : ~4-16 calls/day each  (limit 1500 RPD each) ✅
  Groq K1/K2     : ~4-12 structure calls/day (limit 100K TPD each) ✅
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


def _validate_keys():
    """Print which keys are set. Warn if critical ones are missing."""
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


# ── Mode runners ──────────────────────────────────────────────────────────────

def run_full(class_slug):
    """Generate one full-length test covering all subjects."""
    print(f"\n{'─'*65}")
    print(f"  FULL-LENGTH TEST  |  {class_slug.upper()}")
    print(f"{'─'*65}")

    result, err = _safe("full-length test", content_builder.build_full_length_test, class_slug)
    if result:
        uid, path, entry = result
        print(f"\n  ✅ Generated: {entry['title']}")
        print(f"     {entry['totalQuestions']}Q | {entry['duration']}min | {entry['totalMarks']} marks")
        return entry
    else:
        print(f"\n  ❌ Full-length test failed: {err}")
        return None


def run_subject(class_slug):
    """
    Generate one subject-wise OR combined test.
    50% chance of combined (2-3 subjects) vs single-subject.
    Combined only if the exam has ≥ 2 subjects with topics.
    """
    print(f"\n{'─'*65}")

    # Decide: single subject or combined?
    use_combined = random.random() < 0.5
    topics_data  = topic_picker.load_topics(class_slug)
    num_subjects = len((topics_data or {}).get("subjects", []))

    if use_combined and num_subjects >= 2:
        # Pick 2-3 topics from different subjects
        n = 2 if num_subjects == 2 else random.choice([2, 3])
        n = min(n, num_subjects)
        print(f"  COMBINED TEST ({n} subjects)  |  {class_slug.upper()}")
        print(f"{'─'*65}")

        try:
            picked_topics = []
            used_subjects = set()
            state = topic_picker.load_state()

            for _ in range(n * 3):   # try up to 3× to get n different subjects
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
                # Mark all picked topics as used
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
                # Fall through to single-subject below

        except Exception as e:
            print(f"  ⚠️  Combined test setup failed ({e}) — falling back to single-subject")

    # Single-subject test
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
    print(f"\n  Exam: {class_slug.upper()}{forced_str}")
    print(f"  Mode: {mode.upper()}\n")

    if mode == "full":
        entry = run_full(class_slug)
    else:
        entry = run_subject(class_slug)

    # Final report
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

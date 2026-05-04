"""
generate.py -- EduTest TEST-ONLY generator.

BEHAVIOUR RULES
===============

SCHOOL CLASSES (type = school):
  full-length  -> ONE subject, 100Q spanning ALL chapters.
  subject-wise -> ONE subject + ONE topic, 25Q / 40Q / 50Q (random).
  NO combined tests for school classes.

COMPETITIVE EXAMS (type = competitive):
  full-length  -> ALL subjects combined, exact per-subject counts.
  subject-wise -> ONE subject, random Q from [min..max] range.
  combined     -> 2-3 subjects, combinedTestQPerSubject each (50% chance).

Run loop (per GitHub Actions invocation):
  - Generates up to MAX_TESTS_PER_RUN tests per run.
  - Stops early when ALL Gemini keys return quota errors (AllKeysExhausted).
  - Each test is saved immediately when generated, even if partial.
  - Partial tests (fewer Q than target) are saved with actual count.
  - Topic is only marked used AFTER successful save.
  - Always exits 0 so git commit step runs and saves partial content.

Mode decision:
  Priority 1: GENERATE_MODE env var (full or subject)
  Priority 2: UTC hour -- 00:xx = full, all others = subject

API keys (5 total -- exact GitHub secret names):
  GEMINI_API_KEY    -> Flash K1  (researcher + gen fallback)
  GEMINI_API_KEY_1  -> Flash K2  (researcher fallback + gen fallback)
  GEMINI_API_KEY2   -> Flash K3  (primary question generator, round-robin)
  GROQ_API_KEY      -> Groq K1   (primary structurer)
  GROQ_API_KEY2     -> Groq K2   (fallback structurer)
"""
import os, sys, traceback, random
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import topic_picker
import content_builder
from gemini_client import GeminiError, GeminiQuotaError
from groq_client   import GroqError

# How many tests to attempt per GitHub Actions run.
# 4 runs/day x 3 tests = 12 tests/day -- well within free-tier limits.
MAX_TESTS_PER_RUN = 3


class AllKeysExhausted(Exception):
    """Raised when every Gemini key has hit its quota. Stops the run loop."""
    pass


def _safe(label, fn, *args, **kwargs):
    """
    Run fn, return (result, None) or (None, error_str).
    Re-raises AllKeysExhausted so the run loop can stop cleanly.
    """
    try:
        return fn(*args, **kwargs), None
    except AllKeysExhausted:
        raise
    except GeminiQuotaError as e:
        # All Gemini keys exhausted -- stop the whole run
        raise AllKeysExhausted(str(e))
    except (GeminiError, GroqError) as e:
        msg = "API error: " + str(e)
        print("  [FAIL] " + label + " -- " + msg[:150])
        return None, msg
    except Exception as e:
        msg = "Unexpected: " + str(e)
        print("  [FAIL] " + label + " -- " + msg[:150])
        traceback.print_exc()
        return None, msg


def _get_mode():
    override = os.environ.get("GENERATE_MODE", "").strip().lower()
    if override in ("full", "subject"):
        print("  [Mode] env override -> " + override)
        return override
    hour = datetime.now(timezone.utc).hour
    mode = "full" if hour == 0 else "subject"
    print("  [Mode] UTC hour=" + str(hour) + " -> " + mode)
    return mode


def _is_school(class_slug):
    topics_data = topic_picker.load_topics(class_slug)
    return (topics_data or {}).get("type", "competitive") == "school"


def _validate_keys():
    keys = {
        "GEMINI_API_KEY":   "Flash K1 (researcher + gen fallback)",
        "GEMINI_API_KEY_1": "Flash K2 (researcher fallback + gen fallback)",
        "GEMINI_API_KEY2":  "Flash K3 (primary generator)",
        "GROQ_API_KEY":     "Groq K1 (primary structurer)",
        "GROQ_API_KEY2":    "Groq K2 (fallback structurer)",
    }
    print("\n  API Keys:")
    missing_critical = []
    for env, role in keys.items():
        val = os.environ.get(env, "").strip()
        if val:
            print("    ✅ " + env + " (" + role + ") -- " + str(len(val)) + " chars")
        else:
            print("    ❌ " + env + " (" + role + ") -- NOT SET")
            if env in ("GEMINI_API_KEY2", "GROQ_API_KEY"):
                missing_critical.append(env)
    if missing_critical:
        print("\n  ⚠️  Critical keys missing: " + str(missing_critical))
        print("  Generation will attempt fallbacks but may fail.\n")


# School subject-wise sizes -- picked randomly each run
_SCHOOL_SIZES = [25, 40, 50]


def _pick_exam_sw_size(cfg):
    """Pick a random subject-wise Q count within [subjectWiseQMin, subjectWiseQMax]."""
    sw_min  = cfg.get("subjectWiseQMin")
    sw_max  = cfg.get("subjectWiseQMax")
    default = cfg.get("subjectWiseQuestions", 30)
    if sw_min and sw_max and sw_min < sw_max:
        candidates = [v for v in range(sw_min, sw_max + 1) if v % 5 == 0]
        return random.choice(candidates) if candidates else default
    return default


def _print_result(entry):
    q   = entry["totalQuestions"]
    tgt = entry.get("targetQuestions", q)
    partial = " (partial: " + str(q) + "/" + str(tgt) + "Q)" if q < tgt else ""
    print("\n  ✅ Generated" + partial + ": " + entry["title"])
    print("     " + str(q) + "Q | " + str(entry["duration"]) + "min | " + str(entry["totalMarks"]) + " marks")


# ---- Full-length runner -------------------------------------------------------

def run_full(class_slug):
    """
    Generate one full-length test.
    Returns entry dict on success (even if partial), None on non-quota failure.
    Raises AllKeysExhausted when quota is gone.
    """
    sep = chr(9472) * 65
    print("\n" + sep)
    if _is_school(class_slug):
        print("  FULL-LENGTH TEST (School -- Single Subject)  |  " + class_slug.upper())
        print(sep)
        result, err = _safe(
            "school full-length",
            content_builder.build_school_full_length_test,
            class_slug
        )
    else:
        print("  FULL-LENGTH TEST (Exam -- All Subjects)  |  " + class_slug.upper())
        print(sep)
        result, err = _safe(
            "exam full-length",
            content_builder.build_exam_full_length_test,
            class_slug
        )
    if result:
        uid, path, entry = result
        _print_result(entry)
        return entry
    print("\n  ❌ Full-length failed: " + str(err))
    return None


# ---- Subject-wise / combined runner -------------------------------------------

def run_subject(class_slug):
    """
    Generate one subject-wise or combined test.
    Returns entry dict on success (even if partial), None on non-quota failure.
    Raises AllKeysExhausted when quota is gone.
    """
    sep = chr(9472) * 65
    print("\n" + sep)
    topics_data = topic_picker.load_topics(class_slug)
    cfg         = (topics_data or {}).get("examConfig", {})

    if _is_school(class_slug):
        # School: always single-subject, random size
        q_count = random.choice(_SCHOOL_SIZES)
        print("  SUBJECT-WISE TEST (School -- " + str(q_count) + "Q)  |  " + class_slug.upper())
        print(sep)
        try:
            topic_info, state = topic_picker.pick_topic("test", class_slug)
            print("  Topic: " + topic_info["subjectName"] + " / " + topic_info["chapter"] + " / " + topic_info["topic"])
            print("  Size:  " + str(q_count) + "Q (randomly chosen from 25/40/50)")
            result, err = _safe(
                "school subject",
                content_builder.build_subject_test,
                topic_info,
                q_count_override=q_count
            )
            if result:
                uid, path, entry = result
                topic_picker.mark_generated(state, "test", [(topic_info, uid)])
                _print_result(entry)
                return entry
            print("\n  ❌ School subject test failed: " + str(err))
            return None
        except AllKeysExhausted:
            raise
        except Exception as e:
            print("\n  ❌ Topic pick failed: " + str(e))
            return None

    # Competitive: combined (50%) or single-subject
    use_combined = random.random() < 0.5
    num_subjects = len((topics_data or {}).get("subjects", []))

    if use_combined and num_subjects >= 2:
        n      = 2 if num_subjects == 2 else random.choice([2, 3])
        n      = min(n, num_subjects)
        comb_q = cfg.get("combinedTestQPerSubject",
                         cfg.get("subjectWiseQMin",
                                 cfg.get("subjectWiseQuestions", 25)))
        print("  COMBINED TEST (" + str(n) + " subjects, " + str(comb_q) + "Q/subj)  |  " + class_slug.upper())
        print(sep)
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
                raise ValueError("Could not pick " + str(n) + " different subjects")
            result, err = _safe(
                "combined",
                content_builder.build_combined_test,
                class_slug, picked_topics,
                q_per_subj_override=comb_q
            )
            if result:
                uid, path, entry = result
                topic_picker.mark_generated(state, "test", [(t, uid) for t in picked_topics])
                _print_result(entry)
                return entry
            print("\n  ❌ Combined failed: " + str(err) + " -- falling back to single-subject")
        except AllKeysExhausted:
            raise
        except Exception as e:
            print("  ⚠️  Combined setup failed (" + str(e) + ") -- falling back to single-subject")

    # Single-subject
    q_count = _pick_exam_sw_size(cfg)
    sw_min  = cfg.get("subjectWiseQMin", "?")
    sw_max  = cfg.get("subjectWiseQMax", "?")
    print("  SUBJECT-WISE TEST (" + str(q_count) + "Q)  |  " + class_slug.upper())
    print(sep)
    try:
        topic_info, state = topic_picker.pick_topic("test", class_slug)
        print("  Topic: " + topic_info["subjectName"] + " / " + topic_info["chapter"] + " / " + topic_info["topic"])
        print("  Size:  " + str(q_count) + "Q (range " + str(sw_min) + "--" + str(sw_max) + ")")
        result, err = _safe(
            "subject",
            content_builder.build_subject_test,
            topic_info,
            q_count_override=q_count
        )
        if result:
            uid, path, entry = result
            topic_picker.mark_generated(state, "test", [(topic_info, uid)])
            _print_result(entry)
            return entry
        print("\n  ❌ Subject test failed: " + str(err))
        return None
    except AllKeysExhausted:
        raise
    except Exception as e:
        print("\n  ❌ Topic pick failed: " + str(e))
        return None


# ---- Main run loop ------------------------------------------------------------

def run():
    eq = chr(61) * 65
    dq = chr(9552) * 65
    print("\n" + eq)
    print("  EduTest Generator  |  Tests Only")
    print("  " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    print(eq)

    _validate_keys()

    mode        = _get_mode()
    forced_exam = os.environ.get("FORCE_EXAM", "").strip().lower()
    forced_str  = " (forced)" if forced_exam else ""
    class_slug  = forced_exam if forced_exam else topic_picker.pick_random_exam()
    topics_data = topic_picker.load_topics(class_slug)
    exam_type   = (topics_data or {}).get("type", "competitive")
    type_label  = "School Class" if exam_type == "school" else "Competitive Exam"

    print("\n  Exam: " + class_slug.upper() + forced_str)
    print("  Type: " + type_label)
    print("  Mode: " + mode.upper())
    print("  Max tests this run: " + str(MAX_TESTS_PER_RUN) + "\n")

    generated = []
    failed    = 0

    for attempt in range(1, MAX_TESTS_PER_RUN + 1):
        print("\n" + dq)
        print("  Test " + str(attempt) + "/" + str(MAX_TESTS_PER_RUN))
        print(dq)
        try:
            entry = run_full(class_slug) if mode == "full" else run_subject(class_slug)
            if entry:
                generated.append(entry)
            else:
                failed += 1
                print("  ⚠️  Test " + str(attempt) + " failed (non-quota) -- continuing to next")
        except AllKeysExhausted as e:
            print("\n  🛑 ALL API KEYS EXHAUSTED: " + str(e))
            print("  Stopping after " + str(len(generated)) + " test(s) generated.")
            break

    # Final report
    print("\n" + eq)
    print("  RUN COMPLETE")
    print("  Generated : " + str(len(generated)) + " test(s)")
    print("  Failed    : " + str(failed) + " test(s)")
    for e in generated:
        q   = e["totalQuestions"]
        tgt = e.get("targetQuestions", q)
        partial = " [" + str(q) + "/" + str(tgt) + "Q partial]" if q < tgt else " [" + str(q) + "Q]"
        print("    ✅ " + e["title"] + partial)
    print(eq + "\n")

    # Always exit 0 -- git commit step must run to save any partial content
    sys.exit(0)


if __name__ == "__main__":
    run()

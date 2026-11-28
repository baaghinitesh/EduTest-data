"""
generate.py -- PrepGuide Lite TEST-ONLY generator.

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
  PHASE 1 — Resume pending drafts from previous runs:
    For each pending draft:
      → Try to complete it (resume generation)
      → If complete: publish to CDN + MongoDB, delete draft ✅
      → If still partial: save updated draft, STOP the run entirely
        (quota likely exhausted — no point starting new tests)
      → If quota exhausted: save progress, STOP

  PHASE 2 — Generate up to MAX_TESTS_PER_RUN new tests:
    (Only runs if Phase 1 left zero pending drafts)
    For each test slot (up to 3):
      → Pick a fresh exam slug for each slot (variety across slots)
      → Generate the test
      → If partial (quota hit mid-generation):
          Immediately try to resume/complete it in the SAME run
          If still partial after resume: save draft, STOP (no more new tests)
          If complete after resume: publish ✅, continue to next slot
      → If complete on first try: publish ✅, continue to next slot
      → If ALL keys exhausted: STOP (no more new tests)
      → If non-quota failure: skip this slot, try next

  Key guarantee: a partial test is ALWAYS attempted for completion
  before the run ends. Only if ALL keys are truly exhausted does it
  get saved as a draft for the next run.

API keys (all 6 × 2 models tried before giving up):
  GEMINI_API_KEY    -> Flash K1  (researcher + gen fallback)
  GEMINI_API_KEY_1  -> Flash K2  (researcher fallback + gen fallback)
  GEMINI_API_KEY2   -> Flash K3  (primary question generator, round-robin)
  GROQ_API_KEY      -> Groq K1   (primary structurer)
  GROQ_API_KEY2     -> Groq K2   (fallback structurer)
  MISTRAL_API_KEY   -> Mistral   (4th gen fallback + 3rd structurer, optional)

Generation rotation per chunk (ALL 9 combinations tried before giving up):
  K_a-Flash2.5 → K_a-Flash2.0 → K_b-Flash2.5 → K_b-Flash2.0
  → K_c-Flash2.5 → K_c-Flash2.0 → Mistral → GroqK1 → GroqK2
  Key order (a,b,c) rotates by chunk index for load spreading.

Quota handling:
  RPM/TPM hit (short wait ≤ 90s) → sleep stated time, retry SAME key+model
  RPD hit on Flash2.5 → try Flash2.0 on same key before moving to next key
  RPD hit on both models for a key → mark key fully exhausted, try next key
  AllKeysExhausted only raised when EVERY key+model combination is exhausted
  Safety: inter-key sleep, consecutive-fail cap, no infinite loops
"""
import os, sys, traceback, random
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import topic_picker
import content_builder
import draft_store
from gemini_client import GeminiError, GeminiQuotaError
from groq_client   import GroqError
from run_budget    import RunTimeoutError, MAX_RUN_SECONDS, elapsed_seconds, is_expired

# How many new tests to attempt per run (when no drafts are pending).
# Configurable via MAX_TESTS_PER_RUN env var (default 3; Render uses 1-2, Actions uses 2).
def _get_max_tests_per_run():
    raw = os.environ.get("MAX_TESTS_PER_RUN", "").strip()
    if raw:
        try:
            return max(1, min(int(raw), 10))
        except ValueError:
            pass
    return 3

MAX_TESTS_PER_RUN = _get_max_tests_per_run()


class AllKeysExhausted(Exception):
    """Raised when every API key (Gemini + Groq + Mistral) is truly exhausted."""
    pass


def _safe(label, fn, *args, **kwargs):
    """
    Run fn, return (result, None) or (None, error_str).
    Re-raises AllKeysExhausted so the run loop can stop cleanly.
    GeminiQuotaError from builders means ALL keys exhausted → stop run.
    """
    try:
        return fn(*args, **kwargs), None
    except (AllKeysExhausted, RunTimeoutError):
        raise
    except GeminiQuotaError as e:
        # All Gemini + Mistral keys exhausted — stop the whole run
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
        "GEMINI_API_KEY":   ("Flash K1 (researcher + gen fallback)",          "critical"),
        "GEMINI_API_KEY_1": ("Flash K2 (researcher fallback + gen)",           "critical"),
        "GEMINI_API_KEY2":  ("Flash K3 (primary generator)",                   "critical"),
        "GROQ_API_KEY":     ("Groq K1 (primary structurer + gen last-resort)", "critical"),
        "GROQ_API_KEY2":    ("Groq K2 (fallback structurer + gen last-resort)","critical"),
        "MISTRAL_API_KEY":  ("Mistral (gen rotation + 3rd structurer)",        "optional"),
    }
    print("\n  API Keys (all 6 tried before giving up):")
    missing_critical = []
    total_set = 0
    for env, (role, importance) in keys.items():
        val = os.environ.get(env, "").strip()
        if val:
            total_set += 1
            print("    ✅ " + env + " (" + role + ") -- " + str(len(val)) + " chars")
        else:
            if importance == "optional":
                print("    ⚠️  " + env + " (" + role + ") -- optional, not set")
            else:
                print("    ❌ " + env + " (" + role + ") -- NOT SET")
                missing_critical.append(env)
    print(f"\n  {total_set}/6 keys configured")
    if missing_critical:
        print("  ⚠️  Missing keys: " + str(missing_critical))
        print("  Generation will attempt fallbacks but may produce fewer questions.\n")
    else:
        print("  All keys set — maximum free-tier capacity available.\n")
    
    # Show key rotation strategy
    print("  Key rotation strategy (ALL 6 keys × 2 tiers tried per chunk):")
    print("    Generation : K_a-3.8→K_b-3.8→K_c-3.8→Mistral→Groq(gpt-oss-120b)→K_*-3.5-lite→Groq(qwen3.8-27b)")
    print("    Key order rotates per chunk to spread load across keys")
    print("    Research   : K1/K2/K3-3.5-lite(rotated)→3.8-flash→Mistral→Groq (all tried before empty ctx)")
    print("    Structuring: Groq(qwen3.8-27b rotated K1/K2)→Mistral→Groq(gpt-oss-20b)→Gemini3.5-Lite→3.8-Flash")
    print(f"    Safety     : permanent 401/402/403/404 circuit-break, 503 fast-failover, {int(MAX_RUN_SECONDS/60)}m hard time cap")
    print()


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


def _print_final_report(generated, failed):
    import gemini_client as _gc
    import groq_client   as _gq
    try:
        import mistral_client as _mc
        mistral_exhausted = _mc._is_exhausted()
    except Exception:
        mistral_exhausted = False

    eq = chr(61) * 65
    pending = draft_store.load_all_drafts()
    print("\n" + eq)
    print("  RUN COMPLETE")
    print("  Published  : " + str(len(generated)) + " test(s)")
    print("  Failed     : " + str(failed) + " test(s)")
    print("  Drafts left: " + str(len(pending)))
    for e in generated:
        q   = e["totalQuestions"]
        tgt = e.get("targetQuestions", q)
        status = " [partial: " + str(q) + "/" + str(tgt) + "Q]" if q < tgt else " [" + str(q) + "Q]"
        print("    ✅ " + e["title"] + status)
    if pending:
        print("  ⏳ Pending (will resume next run):")
        for d in pending:
            pct = int(d["collected_q"] / d["target_q"] * 100) if d["target_q"] else 0
            print("    • " + d["title"] + " [" + str(d["collected_q"]) + "/" + str(d["target_q"]) + "Q  " + str(pct) + "%]")

    # Show which keys hit their daily limit or were disabled this run
    exhausted_keys = list(_gc._rpd_exhausted) + list(_gq._tpd_exhausted) + list(_gq._auth_failed)
    if mistral_exhausted:
        exhausted_keys.append("MISTRAL_API_KEY")

    # Show per-model exhaustion detail for Gemini keys
    model_detail = []
    for key_env in [_gc.KEY_K1, _gc.KEY_K2, _gc.KEY_K3]:
        if not os.environ.get(key_env, "").strip():
            continue
        ex_pri  = _gc._model_exhausted.get((key_env, _gc.MODEL_PRIMARY), False)
        ex_lite = _gc._model_exhausted.get((key_env, _gc.MODEL_LITE), False)
        if ex_pri or ex_lite:
            models = []
            if ex_pri:  models.append("3.8-Flash")
            if ex_lite: models.append("3.5-Lite")
            model_detail.append(f"{key_env}({'+'.join(models)})")

    if exhausted_keys:
        print("  🔑 Keys exhausted/disabled this run: " + ", ".join(dict.fromkeys(exhausted_keys)))
    if _gq._disabled_models:
        print("  ⚠️  Groq disabled models: " + ", ".join(sorted(_gq._disabled_models)))
    if model_detail:
        print("  📉 Per-model exhaustion: " + ", ".join(model_detail))

    total_keys = 6
    exhausted_count = len(set(exhausted_keys))
    remaining = max(0, total_keys - exhausted_count)
    print(f"  📊 Key status: {remaining}/{total_keys} keys still available")
    print(f"  ⏱️  Elapsed   : {elapsed_seconds() / 60.0:.1f}m / {MAX_RUN_SECONDS / 60.0:.0f}m budget")
    print(eq + "\n")


def _try_complete(entry, label):
    """
    If entry is partial (totalQ < targetQ), immediately try to resume it
    in the same run. Returns the (possibly updated) entry.
    Raises AllKeysExhausted if quota is gone.
    """
    q   = entry.get("totalQuestions", 0)
    tgt = entry.get("targetQuestions", q)
    if q >= tgt:
        return entry   # already complete

    print("\n  ♻️  " + label + " is partial (" + str(q) + "/" + str(tgt) + "Q) — attempting completion now")

    # Find the draft that was just saved for this entry
    uid = entry.get("id")
    drafts = draft_store.load_all_drafts()
    draft = next((d for d in drafts if d.get("uid") == uid), None)

    if not draft:
        print("  ⚠️  Draft not found for uid=" + str(uid) + " — cannot resume inline")
        return entry

    try:
        resume_result = content_builder.resume_draft(draft)
        uid2, path2, completed_entry = resume_result
        if completed_entry is not None:
            print("  ✅ Completed inline: " + completed_entry["title"])
            return completed_entry
        else:
            print("  ⏳ Still partial after inline resume — will continue next run")
            return entry   # return the partial entry (draft already updated)
    except (AllKeysExhausted, GeminiQuotaError) as e:
        raise AllKeysExhausted(str(e))   # propagate — stops the run loop
    except RunTimeoutError:
        raise
    except Exception as e:
        print("  ⚠️  Inline resume error (" + str(e) + ") — keeping partial draft")
        return entry


# ---- Main run loop ------------------------------------------------------------

def run():
    eq = chr(61) * 65
    dq = chr(9552) * 65
    print("\n" + eq)
    print("  PrepGuide Lite Generator  |  Tests Only")
    print("  " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    print(eq)

    _validate_keys()

    mode        = _get_mode()
    forced_exam = os.environ.get("FORCE_EXAM", "").strip().lower()
    forced_exam = topic_picker.normalize_exam_slug(forced_exam) if forced_exam else ""

    # Custom generation instructions from workflow_dispatch input
    custom_instructions = os.environ.get("GENERATION_INSTRUCTIONS", "").strip()

    generated = []
    failed    = 0

    # ══════════════════════════════════════════════════════════════════
    #  PHASE 1 — Resume pending drafts from previous runs
    #  Rule: try to complete each draft. If still partial after attempt
    #  (quota exhausted), stop the run — don't start new tests.
    # ══════════════════════════════════════════════════════════════════
    pending_drafts = draft_store.load_all_drafts()

    if pending_drafts:
        print("\n  📋 PHASE 1: " + str(len(pending_drafts)) + " pending draft(s) — resuming before new work")

        for draft in pending_drafts:
            if is_expired():
                print("\n  ⏱️  Time budget reached before resuming remaining drafts — stopping run")
                _print_final_report(generated, failed)
                sys.exit(0)
            print("\n" + dq)
            print("  RESUME DRAFT: " + draft["title"])
            print("  Progress: " + str(draft["collected_q"]) + "/" + str(draft["target_q"]) + "Q")
            print(dq)
            try:
                resume_result = content_builder.resume_draft(draft)
                uid, path, entry = resume_result
                if entry is not None:
                    generated.append(entry)
                    print("  ✅ Draft completed and published: " + entry["title"])
                else:
                    # Still partial — check if actually exhausted
                    import gemini_client as _gc
                    if _gc.all_keys_exhausted():
                        print("  🛑 Draft still partial and all keys exhausted — stopping run")
                        print("  ℹ️  No new tests started — will resume this draft next run")
                        _print_final_report(generated, failed)
                        sys.exit(0)
                    else:
                        print("  ⏳ Draft still partial (non-quota issue) — keeping draft, continuing to next draft")
                        failed += 1

            except RunTimeoutError as e:
                print("\n  ⏱️  TIME BUDGET REACHED during draft resume: " + str(e))
                _print_final_report(generated, failed)
                sys.exit(0)
            except AllKeysExhausted as e:
                print("\n  🛑 ALL API KEYS EXHAUSTED during draft resume: " + str(e))
                _print_final_report(generated, failed)
                sys.exit(0)
            except GeminiQuotaError as e:
                print("\n  🛑 Quota exhausted during draft resume: " + str(e))
                _print_final_report(generated, failed)
                sys.exit(0)
            except Exception as e:
                print("  ⚠️  Draft resume error (" + str(e) + ") — skipping this draft")
                traceback.print_exc()

        # If any drafts still remain (e.g. resume errored / still partial), don't start new tests
        still_pending = draft_store.load_all_drafts()
        if still_pending:
            import gemini_client as _gc
            if _gc.all_keys_exhausted():
                print("\n  ⏳ " + str(len(still_pending)) + " draft(s) still pending & keys exhausted — stopping run")
                _print_final_report(generated, failed)
                sys.exit(0)
            else:
                print("\n  ⏳ " + str(len(still_pending)) + " draft(s) still pending — no new tests this run to avoid piling drafts")
                _print_final_report(generated, failed)
                sys.exit(0)

        print("\n  ✅ All drafts completed — proceeding to new generation")

    # ══════════════════════════════════════════════════════════════════
    #  PHASE 2 — Generate new tests (only when draft queue is empty)
    #
    #  For each slot (up to MAX_TESTS_PER_RUN):
    #    1. Pick a fresh exam slug (variety across slots in same run)
    #    2. Generate the test
    #    3. If partial → immediately try to complete it in this same run
    #    4. If still partial after inline resume → save draft, STOP
    #       (quota exhausted — don't start another new test)
    #    5. If complete → publish, move to next slot
    #    6. If quota exhausted → STOP
    #    7. If non-quota failure → skip slot, try next
    # ══════════════════════════════════════════════════════════════════
    # Base exam for this run (used when FORCE_EXAM is set)
    base_exam = forced_exam if forced_exam else None

    print("\n  📝 PHASE 2: Generating new tests")
    if base_exam:
        print("  Exam: " + base_exam.upper() + " (forced)")
    else:
        print("  Exam: random per slot (variety across slots)")
    print("  Mode: " + mode.upper())
    print("  Target: " + str(MAX_TESTS_PER_RUN) + " test(s) this run")
    if custom_instructions:
        print("  Instructions: " + custom_instructions[:120])
        # Pass instructions into content_builder for this run
        content_builder.set_custom_instructions(custom_instructions)
    print()

    for attempt in range(1, MAX_TESTS_PER_RUN + 1):
        if is_expired():
            print(f"\n  ⏱️  Time budget reached before Test {attempt}/{MAX_TESTS_PER_RUN} — stopping cleanly")
            break
        print("\n" + dq)
        print("  Test " + str(attempt) + "/" + str(MAX_TESTS_PER_RUN))
        print(dq)

        # Pick exam for this slot — fresh pick each slot for variety
        # (unless forced via FORCE_EXAM env var)
        class_slug = base_exam if base_exam else topic_picker.pick_random_exam()
        topics_data = topic_picker.load_topics(class_slug)
        exam_type   = (topics_data or {}).get("type", "competitive")
        type_label  = "School Class" if exam_type == "school" else "Competitive Exam"
        print("  Exam: " + class_slug.upper() + " (" + type_label + ")")

        try:
            entry = run_full(class_slug) if mode == "full" else run_subject(class_slug)

            if entry is None:
                # Non-quota failure (bad topic, API glitch) — try next slot
                failed += 1
                print("  ⚠️  Test " + str(attempt) + " failed (non-quota) — trying next slot")
                continue

            # Check if partial — if so, try to complete it right now
            q   = entry.get("totalQuestions", 0)
            tgt = entry.get("targetQuestions", q)
            if q < tgt:
                entry = _try_complete(entry, "Test " + str(attempt))
                # Re-check after inline resume
                q2   = entry.get("totalQuestions", 0)
                tgt2 = entry.get("targetQuestions", q2)
                if q2 < tgt2:
                    # Still partial — check if truly all keys exhausted
                    import gemini_client as _gc
                    if _gc.all_keys_exhausted():
                        print("\n  🛑 Test " + str(attempt) + " still partial and all keys exhausted — stopping run")
                        print("  ℹ️  Draft saved. No more new tests this run.")
                        generated.append(entry)   # count the partial as attempted
                        break
                    else:
                        print("\n  ⏳ Test " + str(attempt) + " still partial (non-quota issue) — keeping draft, continuing to next slot")
                        generated.append(entry)
                        continue

            generated.append(entry)

        except RunTimeoutError as e:
            print("\n  ⏱️  TIME BUDGET REACHED: " + str(e))
            print("  Stopping cleanly after " + str(len(generated)) + " test(s) — drafts saved.")
            break
        except AllKeysExhausted as e:
            print("\n  🛑 ALL API KEYS EXHAUSTED: " + str(e))
            print("  Stopping after " + str(len(generated)) + " test(s).")
            break

    _print_final_report(generated, failed)
    # Always exit 0 — git commit step must run to save any partial content / drafts
    sys.exit(0)


if __name__ == "__main__":
    run()

# start

"""
content_builder.py — TEST-ONLY generation pipeline.

════════════════════════════════════════════════════════════════════
  BEHAVIOUR RULES
════════════════════════════════════════════════════════════════════

  SCHOOL CLASSES  (type = "school")
  ─────────────────────────────────
  full-length  → ONE subject picked at random, 100Q spanning ALL its
                 chapters (broad coverage, not a single topic).
                 Title: "CBSE CLASS 10 — Mathematics | Full Length Test"
                 Prompt style: school exam, board-pattern questions,
                 NCERT-aligned, age-appropriate difficulty.

  subject-wise → ONE subject, subjectWiseQuestions (e.g. 20Q) on a
                 specific chapter/topic.
                 Title: "CBSE CLASS 10 — Mathematics | Algebra"
                 Prompt style: chapter-focused, NCERT-based.

  combined     → 2-3 subjects, subjectWiseQuestions each.
                 Title: "CBSE CLASS 10 — Combined: Maths + Physics"
                 Prompt style: multi-subject school paper.

  COMPETITIVE EXAMS  (type = "competitive")
  ──────────────────────────────────────────
  full-length  → ALL subjects combined, exact per-subject question
                 counts from full-length.json / weightage.
                 Title: "NEET UG — Full Length Mock Test"
                 Prompt style: competitive exam standard, high
                 difficulty, negative marking awareness.

  subject-wise → ONE subject, subjectWiseQuestions (e.g. 40-45Q) on
                 a specific chapter/topic.
                 Title: "NEET UG — Physics | Thermodynamics"
                 Prompt style: competitive, exam-pattern questions.

  combined     → 2-3 subjects, subjectWiseQuestions each.
                 Title: "NEET UG — Combined: Physics + Chemistry"
                 Prompt style: competitive multi-subject.

Pipeline per chunk:
  Step 1 — Research   : Flash K1 → Flash K2 → cached (once per subject)
  Step 2 — Generate   : K3 → K1 → K2 (rotates per chunk, round-robin)
  Step 3 — Structure  : Groq K1 → Groq K2 → Flash K1 → Flash K2
"""
import os, json, uuid, re, random
from datetime import datetime, timezone

import gemini_client
import groq_client
import research_cache
import publish_to_server
import draft_store
from gemini_client import GeminiError, GeminiQuotaError
from groq_client   import GroqError
import topic_picker

ROOT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CDN_BASE = "https://prepguidedata.launchyourconcept.com"

CHUNK = 10   # always generate in 10Q chunks — safe for all API limits

# ── Custom instructions (set by generate.py from GENERATION_INSTRUCTIONS env) ─
_CUSTOM_INSTRUCTIONS = ""

def set_custom_instructions(instructions: str):
    """Called by generate.py when GENERATION_INSTRUCTIONS env var is set."""
    global _CUSTOM_INSTRUCTIONS
    _CUSTOM_INSTRUCTIONS = instructions.strip()
    if _CUSTOM_INSTRUCTIONS:
        print(f"  [ContentBuilder] Custom instructions active: {_CUSTOM_INSTRUCTIONS[:100]}")

# ── Compact JSON schema sent to structurer ────────────────────────────────────
_SCHEMA = (
    '[{"question":"...","difficulty":"medium","explanation":"...",'
    '"hint":"","options":['
    '{"letter":"A","text":"...","isCorrect":true,"explanation":"..."},'
    '{"letter":"B","text":"...","isCorrect":false,"explanation":"..."},'
    '{"letter":"C","text":"...","isCorrect":false,"explanation":"..."},'
    '{"letter":"D","text":"...","isCorrect":false,"explanation":"..."}]}]'
)


# ── Utility helpers ───────────────────────────────────────────────────────────

def _make_id():
    return str(uuid.uuid4())

def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def _cdn(path):
    return f"{CDN_BASE}/{path.lstrip('/')}"

def _slug(title, uid):
    s = re.sub(r"[^a-z0-9\s-]", "", title.lower())
    s = re.sub(r"\s+", "-", s.strip())
    s = re.sub(r"-+", "-", s)[:55]
    return f"{s}-{uid.split('-')[0]}"

def _admin_path(folder, uid, title, class_slug):
    safe = re.sub(r"[^a-z0-9-]", "-", (class_slug or "general").lower())
    safe = re.sub(r"-+", "-", safe).strip("-") or "general"
    return f"data/admin/{safe}/{folder}/{_slug(title, uid)}.json"

def _save(path, data):
    full = os.path.join(ROOT, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  [Saved] {path}")

def _save_and_publish(path, data):
    """Save to CDN path AND publish to MongoDB. Only called for COMPLETE tests."""
    _save(path, data)
    publish_to_server.publish_test(data, path)

def _save_draft(draft_path, data):
    """
    Save a PARTIAL test to data/drafts/ — committed to git but NOT published
    to MongoDB. The app never sees drafts. Next run will resume from here.
    """
    full = os.path.join(ROOT, draft_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  [Draft] Saved partial test: {draft_path}")

def _tags(class_slug, subj_slug, *extra):
    tags = {class_slug, subj_slug}
    for phrase in extra:
        for w in str(phrase).lower().split():
            if len(w) > 3:
                tags.add(re.sub(r"[^a-z0-9]", "", w))
    return list(tags)[:10]

def _exam_name(class_slug):
    return class_slug.upper().replace("-", " ")

def _is_school(topics_data):
    return (topics_data or {}).get("type", "competitive") == "school"


# ── Question validation ───────────────────────────────────────────────────────

def _validate_q(raw, idx, marks, neg_val, neg_marking, subj_slug):
    qid = f"q-{int(datetime.now().timestamp()*1000)}-{uuid.uuid4().hex[:5]}"
    options = raw.get("options", [])
    validated = []
    for i, opt in enumerate(options[:4]):
        validated.append({
            "letter":      opt.get("letter") or chr(65 + i),
            "text":        str(opt.get("text", f"Option {chr(65+i)}")).strip(),
            "isCorrect":   bool(opt.get("isCorrect", False)),
            "index":       i,
            "imageUrl":    "",
            "explanation": str(opt.get("explanation", "")).strip(),
        })
    while len(validated) < 4:
        i = len(validated)
        validated.append({
            "letter": chr(65+i), "text": f"Option {chr(65+i)}",
            "isCorrect": False, "index": i, "imageUrl": "", "explanation": "",
        })
    correct = [i for i, o in enumerate(validated) if o["isCorrect"]]
    if not correct:
        validated[0]["isCorrect"] = True
    elif len(correct) > 1:
        for i, o in enumerate(validated):
            o["isCorrect"] = (i == correct[0])
    diff = raw.get("difficulty", "medium").lower()
    if diff not in ("easy", "medium", "hard"):
        diff = "medium"
    return {
        "id": qid, "order": idx + 1,
        "question":    str(raw.get("question", f"Question {idx+1}")).strip(),
        "type":        "mcq", "options": validated,
        "answer":      "", "alternateAnswers": [],
        "explanation": str(raw.get("explanation", "")).strip(),
        "hint":        str(raw.get("hint", "")).strip(),
        "marks":       marks,
        "negativeMark":    neg_val if neg_marking else 0,
        "negativeMarking": neg_marking,
        "imageUrl":    "", "subject": subj_slug,
        "difficulty":  diff, "hasError": False,
    }


# ── Research prompts ──────────────────────────────────────────────────────────

def _research_prompt_school(exam_name, subject_name, chapters_str, class_number=None):
    """Research prompt for school class subjects — NCERT/board aligned."""
    grade_ctx = f"Class {class_number}" if class_number else exam_name
    return (
        f"You are a senior {exam_name} board exam expert.\n"
        f"Subject: {subject_name} | Grade: {grade_ctx}\n"
        f"Key chapters: {chapters_str}\n\n"
        "Provide a focused 200-word analysis for question setters:\n"
        "1. Most-tested question patterns in board exams for this subject\n"
        "2. Difficulty distribution (Easy 40% / Medium 40% / Hard 20%)\n"
        "3. Top 6 high-yield topics students must master\n"
        "4. Common mistakes students make in this subject\n"
        "5. NCERT-specific concepts that appear frequently\n"
        "6. Numerical/formula areas with worked examples\n"
        "Be specific to the board exam pattern. No generic advice."
    )

def _research_prompt_exam(exam_name, subject_name, chapters_str):
    """Research prompt for competitive exam subjects — high difficulty, exam-pattern."""
    custom_note = (
        f"\n\nOperator focus for this run: {_CUSTOM_INSTRUCTIONS}"
        if _CUSTOM_INSTRUCTIONS else ""
    )
    return (
        f"You are a senior {exam_name} competitive exam expert.\n"
        f"Subject: {subject_name}\n"
        f"Key chapters: {chapters_str}\n\n"
        "Provide a detailed 250-word analysis for question setters:\n"
        "1. Most-tested question patterns and formats in this exam\n"
        "2. Difficulty distribution (Easy 25% / Medium 45% / Hard 30%)\n"
        "3. Top 8 high-yield topics students must master\n"
        "4. Common wrong-answer traps and misconceptions\n"
        "5. Numerical/formula-heavy areas with calculation examples\n"
        "6. Recent trend: topics gaining more weightage\n"
        f"Be specific and exam-focused. No generic advice.{custom_note}"
    )

def _full_exam_research_prompt(exam_name, subjects_summary):
    """Research prompt for full competitive exam overview."""
    return (
        f"You are a senior {exam_name} competitive exam expert.\n"
        f"Subjects: {subjects_summary}\n\n"
        "Provide a detailed 300-word full-exam analysis:\n"
        "1. Overall difficulty profile and marking scheme\n"
        "2. Subject-wise weightage and question distribution\n"
        "3. Top 4 high-yield topics per subject\n"
        "4. Time management strategy for the full paper\n"
        "5. Most common traps across all subjects\n"
        "6. Which subjects to attempt first for maximum score\n"
        "Be specific to this exam. No generic advice."
    )

def _full_school_research_prompt(exam_name, subject_name, chapters_str, class_number=None):
    """Research prompt for school full-length single-subject test (100Q)."""
    grade_ctx = f"Class {class_number}" if class_number else exam_name
    return (
        f"You are a senior {exam_name} board exam expert.\n"
        f"Subject: {subject_name} | Grade: {grade_ctx}\n"
        f"All chapters: {chapters_str}\n\n"
        "Provide a 250-word analysis for a comprehensive full-length test:\n"
        "1. Chapter-wise weightage for board exams\n"
        "2. Most important topics across ALL chapters\n"
        "3. Difficulty distribution for a full-length paper\n"
        "4. Question types: MCQ, assertion-reason, case-based\n"
        "5. Common errors students make across chapters\n"
        "6. NCERT exemplar-level questions to include\n"
        "Be specific to the board exam pattern."
    )


# ── Research fetchers (with cache) ────────────────────────────────────────────

def _get_research(class_slug, subject_slug, subject_name, chapters_str,
                  exam_name, is_school_exam=False, class_number=None):
    """Fetch or return cached subject research."""
    cached = research_cache.get_subject_research(class_slug, subject_slug, chapters_str)
    if cached:
        print(f"  [Research] ✅ Cache hit: {subject_slug}")
        return cached

    if is_school_exam:
        prompt = _research_prompt_school(exam_name, subject_name, chapters_str, class_number)
    else:
        prompt = _research_prompt_exam(exam_name, subject_name, chapters_str)

    print(f"  [Research] Fetching: {subject_slug}...")
    result = gemini_client.research(prompt, max_tokens=1500)
    if result:
        research_cache.store_subject_research(class_slug, subject_slug, result, chapters_str)
        print(f"  [Research] Cached {len(result)} chars for {subject_slug}")
    return result or "Standard exam difficulty. Focus on core concepts."


def _get_full_exam_research(class_slug, exam_name, subjects_summary):
    """Fetch or return cached full-exam research (competitive only)."""
    cached = research_cache.get_full_exam_research(class_slug)
    if cached:
        print(f"  [Research] ✅ Full-exam cache hit: {class_slug}")
        return cached

    prompt = _full_exam_research_prompt(exam_name, subjects_summary)
    print(f"  [Research] Fetching full-exam: {class_slug}...")
    result = gemini_client.research(prompt, max_tokens=2000)
    if result:
        research_cache.store_full_exam_research(class_slug, result)
        print(f"  [Research] Cached {len(result)} chars for {class_slug}")
    return result or "Standard exam. Cover all subjects equally."


def _get_school_full_research(class_slug, subject_slug, subject_name,
                               chapters_str, exam_name, class_number=None):
    """Fetch or return cached school full-length subject research."""
    cache_key = f"{subject_slug}-full"
    cached = research_cache.get_subject_research(class_slug, cache_key, chapters_str)
    if cached:
        print(f"  [Research] ✅ School full-length cache hit: {subject_slug}")
        return cached

    prompt = _full_school_research_prompt(exam_name, subject_name, chapters_str, class_number)
    print(f"  [Research] Fetching school full-length: {subject_slug}...")
    result = gemini_client.research(prompt, max_tokens=2000)
    if result:
        research_cache.store_subject_research(class_slug, cache_key, result, chapters_str)
        print(f"  [Research] Cached {len(result)} chars for {subject_slug}-full")
    return result or "Standard school exam. Cover all chapters equally."


# ── Generation prompts ────────────────────────────────────────────────────────

def _gen_prompt_school_subject(count, exam_name, subject_name, research_ctx,
                                topic_str, class_number=None, cycle=1):
    """Prompt for school class subject-wise test chunk."""
    grade_ctx = f"Class {class_number}" if class_number else exam_name
    cycle_note = (
        f"\nIMPORTANT: This is generation cycle {cycle}. "
        f"Generate COMPLETELY DIFFERENT questions from any previous tests on this topic. "
        f"Use different question angles, different numerical values, different scenarios."
        if cycle > 1 else ""
    )
    return (
        f"You are an expert question setter for {exam_name} board exams.\n\n"
        f"EXAM RESEARCH (use this to set correct difficulty and focus):\n"
        f"{research_ctx}\n\n"
        f"TASK: Generate exactly {count} high-quality MCQ questions.\n"
        f"Grade: {grade_ctx} | Subject: {subject_name}\n"
        f"Topic/Chapter: {topic_str}{cycle_note}\n\n"
        f"Requirements:\n"
        f"- Follow NCERT/board exam pattern strictly\n"
        f"- Difficulty: 40% easy, 40% medium, 20% hard\n"
        f"- Include: direct recall, application, assertion-reason, case-based\n"
        f"- Each question must have a clear, unambiguous stem\n"
        f"- 4 options (A/B/C/D) — only ONE is correct\n"
        f"- Mark the correct answer with an asterisk: *A) or *B) etc.\n"
        f"- After each question, write: EXPLANATION: <why correct answer is right>\n"
        f"- Questions must match board exam standard — not trivial, not too hard\n"
        f"- For numerical questions, show the calculation step-by-step\n"
        f"- Do NOT repeat questions that would have the same answer as obvious recall\n\n"
        f"Format: number each question 1. 2. 3. etc.\n"
        f"Do NOT output JSON. Write in plain text."
    )

def _gen_prompt_school_full(count, exam_name, subject_name, research_ctx,
                             chapters_str, class_number=None, cycle=1):
    """Prompt for school class full-length test chunk (100Q, all chapters)."""
    grade_ctx = f"Class {class_number}" if class_number else exam_name
    return (
        f"You are an expert question setter for {exam_name} board exams.\n\n"
        f"EXAM RESEARCH (use this to set correct difficulty and focus):\n"
        f"{research_ctx}\n\n"
        f"TASK: Generate exactly {count} high-quality MCQ questions.\n"
        f"Grade: {grade_ctx} | Subject: {subject_name}\n"
        f"Chapters to cover: {chapters_str}\n\n"
        f"Requirements:\n"
        f"- This is a FULL-LENGTH test — cover ALL chapters proportionally\n"
        f"- Follow NCERT/board exam pattern strictly\n"
        f"- Difficulty: 35% easy, 45% medium, 20% hard\n"
        f"- Include: direct recall, application, assertion-reason, case-based\n"
        f"- Spread questions across different chapters — do NOT focus on one chapter\n"
        f"- Each question must have a clear, unambiguous stem\n"
        f"- 4 options (A/B/C/D) — only ONE is correct\n"
        f"- Mark the correct answer with an asterisk: *A) or *B) etc.\n"
        f"- After each question, write: EXPLANATION: <why correct answer is right>\n"
        f"- For numerical questions, show the calculation step-by-step\n\n"
        f"Format: number each question 1. 2. 3. etc.\n"
        f"Do NOT output JSON. Write in plain text."
    )

def _gen_prompt_exam_subject(count, exam_name, subject_name, research_ctx,
                             topic_str, cycle=1):
    """Prompt for competitive exam subject-wise / combined test chunk."""
    cycle_note = (
        f"\nIMPORTANT: This is generation cycle {cycle}. "
        f"Generate COMPLETELY DIFFERENT questions from any previous tests on this topic. "
        f"Use different question angles, different numerical values, different application scenarios."
        if cycle > 1 else ""
    )
    custom_note = (
        f"\n\nSPECIAL INSTRUCTIONS FROM OPERATOR:\n{_CUSTOM_INSTRUCTIONS}"
        if _CUSTOM_INSTRUCTIONS else ""
    )
    return (
        f"You are an expert question setter for the {exam_name} competitive exam.\n\n"
        f"EXAM RESEARCH (use this to set correct difficulty and focus):\n"
        f"{research_ctx}\n\n"
        f"TASK: Generate exactly {count} high-quality MCQ questions.\n"
        f"Subject: {subject_name}\n"
        f"Topic/Chapter: {topic_str}{cycle_note}{custom_note}\n\n"
        f"Requirements:\n"
        f"- Match the exact difficulty and style of {exam_name} official papers\n"
        f"- Difficulty: 25% easy, 45% medium, 30% hard\n"
        f"- Each question must have a clear, unambiguous stem\n"
        f"- 4 options (A/B/C/D) — only ONE is correct\n"
        f"- Mark the correct answer with an asterisk: *A) or *B) etc.\n"
        f"- After each question, write: EXPLANATION: <why correct answer is right>\n"
        f"- Questions must be exam-standard — include tricky distractors\n"
        f"- For numerical questions, show the calculation step-by-step\n"
        f"- Vary question types: direct recall, application, analytical\n"
        f"- Do NOT repeat questions with the same stem or answer pattern\n\n"
        f"Format: number each question 1. 2. 3. etc.\n"
        f"Do NOT output JSON. Write in plain text."
    )

def _gen_prompt_exam_full(count, exam_name, subject_name, research_ctx,
                          chapters_str, cycle=1):
    """Prompt for competitive exam full-length test chunk (one subject section)."""
    custom_note = (
        f"\n\nSPECIAL INSTRUCTIONS FROM OPERATOR:\n{_CUSTOM_INSTRUCTIONS}"
        if _CUSTOM_INSTRUCTIONS else ""
    )
    return (
        f"You are an expert question setter for the {exam_name} competitive exam.\n\n"
        f"EXAM RESEARCH (use this to set correct difficulty and focus):\n"
        f"{research_ctx}\n\n"
        f"TASK: Generate exactly {count} high-quality MCQ questions.\n"
        f"Subject: {subject_name} (section of the full-length {exam_name} paper)\n"
        f"Chapters to cover: {chapters_str}\n\n"
        f"Requirements:\n"
        f"- This is the {subject_name} SECTION of a full-length {exam_name} mock test\n"
        f"- Match the exact difficulty and style of {exam_name} official papers\n"
        f"- Difficulty: 25% easy, 45% medium, 30% hard\n"
        f"- Spread questions across different chapters — do NOT focus on one chapter\n"
        f"- Each question must have a clear, unambiguous stem\n"
        f"- 4 options (A/B/C/D) — only ONE is correct\n"
        f"- Mark the correct answer with an asterisk: *A) or *B) etc.\n"
        f"- After each question, write: EXPLANATION: <why correct answer is right>\n"
        f"- Include tricky distractors that test deep understanding\n"
        f"- For numerical questions, show the calculation step-by-step\n"
        f"{custom_note}\n"
        f"Format: number each question 1. 2. 3. etc.\n"
        f"Do NOT output JSON. Write in plain text."
    )


# ── Step 2: Generate one 10Q chunk ────────────────────────────────────────────

def _generate_one_chunk(prompt, chunk_index):
    """
    Generate one chunk using the given prompt.
    Rotates across Gemini keys by chunk_index to spread RPM/RPD load.
    Returns raw text string or empty string on failure.
    Raises GeminiQuotaError if ALL keys are quota-exhausted (caller stops iteration).
    """
    try:
        raw = gemini_client.generate_questions_for_chunk(
            prompt, chunk_index=chunk_index, max_tokens=4096
        )
        if raw and len(raw) > 80:
            return raw
        print(f"    [Generate] Response too short — skipping chunk")
    except GeminiQuotaError:
        raise   # propagate — all keys exhausted, stop the run
    except GeminiError as e:
        s = str(e)
        # If the error message indicates all keys are exhausted, re-raise as quota error
        if any(x in s.lower() for x in ["exhausted", "all flash keys", "all keys"]):
            raise GeminiQuotaError(f"All generation keys exhausted: {s}")
        print(f"    [Generate] All keys failed: {e}")
    return ""


# ── Step 3: Structure one chunk into JSON ─────────────────────────────────────

def _structure_one_chunk(raw_text, exam_name, subject_name):
    """
    Structure raw text into JSON list using the full fallback chain:

      Step A — groq_client.format_questions():
        Groq K1 → Groq K2 → Mistral
        (all 3 tried per chunk before moving to Step B)

      Step B — gemini_client.structure_with_flash():
        Flash 2.0 K1 → Flash 2.0 K2 → Flash 2.0 K3
        → Flash 2.5 K1 → Flash 2.5 K2 → Flash 2.5 K3
        (only reached if Groq K1 + K2 + Mistral all fail)

    Returns list of question dicts, or [] if everything fails.
    """
    try:
        qs = groq_client.format_questions(raw_text, _SCHEMA, exam_name, subject_name)
        if isinstance(qs, list) and qs:
            return qs
        if isinstance(qs, dict):
            return [qs]
    except GroqError as e:
        print(f"    [Structure] Groq+Mistral all failed ({e}) — trying Flash fallback...")

    # Flash fallback — only reached when Groq K1 + K2 + Mistral all failed
    flash_prompt = (
        f"Extract ALL questions from the text below.\n"
        f"Output ONLY a JSON array. Schema: {_SCHEMA}\n"
        f"Rules: ONE isCorrect:true per question, "
        f"difficulty must be 'easy', 'medium', or 'hard'.\n\n"
        f"TEXT:\n{raw_text}"
    )
    try:
        qs = gemini_client.structure_with_flash(flash_prompt, max_tokens=4096)
        if isinstance(qs, list) and qs:
            print(f"    [Structure] ✅ Flash fallback succeeded")
            return qs
        if isinstance(qs, dict):
            return [qs]
    except GeminiError as e:
        print(f"    [Structure] Flash fallback also failed: {e}")

    return []


# ── Core iterator ─────────────────────────────────────────────────────────────

def _iterate_to_target(target_count, exam_name, subject_name,
                        research_ctx, topic_str, prompt_fn, prompt_kwargs=None):
    """
    Iterate 10Q chunks until target_count questions are collected.
    prompt_fn(count, exam_name, subject_name, research_ctx, topic_str, **prompt_kwargs)

    Returns (list_of_raw_question_dicts, quota_exhausted: bool).
      - quota_exhausted=True  → ALL API keys are exhausted; caller must stop the run
      - quota_exhausted=False → stopped due to API glitch / structuring error (non-fatal)

    The caller decides whether the partial result is acceptable.

    Enhanced behaviour:
      - Each chunk tries ALL 6 keys × 2 models before giving up (via gemini_client)
      - Short sleep between chunks to avoid hammering rate limits
      - On structuring failure: retries the chunk once with a fresh prompt before giving up
      - On generation failure (non-quota): retries the chunk once before giving up
      - Safety: max_chunks cap prevents infinite loops
    """
    import time as _time

    all_qs             = []
    collected          = 0
    chunk_idx          = 0
    quota_exhausted    = False
    consecutive_fails  = 0   # consecutive chunk failures (non-quota)
    MAX_CONSECUTIVE    = 3   # stop after 3 consecutive non-quota failures
    INTER_CHUNK_SLEEP  = 1   # seconds between successful chunks (light pacing)
    max_chunks         = (target_count // CHUNK) + 5  # +5 buffer for retries

    total_needed = (target_count + CHUNK - 1) // CHUNK
    print(f"    Target: {target_count}Q in {CHUNK}Q chunks ({total_needed} chunks needed)")

    while collected < target_count and chunk_idx < max_chunks:
        remaining = target_count - collected
        cnt       = min(remaining, CHUNK)
        chunk_num = chunk_idx + 1

        print(f"    ── Chunk {chunk_num}/{total_needed} ({cnt}Q, key slot {chunk_idx % 4}) ──")

        kwargs = prompt_kwargs or {}
        prompt = prompt_fn(cnt, exam_name, subject_name, research_ctx, topic_str, **kwargs)

        # ── Generation step ───────────────────────────────────────────────────
        raw = None
        try:
            raw = _generate_one_chunk(prompt, chunk_idx)
        except GeminiQuotaError:
            # ALL keys exhausted — save what we have and signal the run loop to stop
            print(f"    [Generate] ⚠️  ALL keys quota-exhausted after {collected}Q — stopping")
            quota_exhausted = True
            break

        if not raw:
            consecutive_fails += 1
            print(f"    Chunk {chunk_num} generation failed (fail #{consecutive_fails})")
            if consecutive_fails >= MAX_CONSECUTIVE:
                print(f"    {MAX_CONSECUTIVE} consecutive failures — stopping iteration")
                break
            # Short sleep before retrying next chunk slot
            _time.sleep(3)
            chunk_idx += 1
            continue

        # ── Structuring step ──────────────────────────────────────────────────
        qs = _structure_one_chunk(raw, exam_name, subject_name)

        if not qs:
            # Retry structuring once with a slightly different prompt framing
            print(f"    Chunk {chunk_num} structuring failed — retrying once...")
            _time.sleep(2)
            qs = _structure_one_chunk(raw, exam_name, subject_name)

        if qs:
            all_qs.extend(qs)
            collected += len(qs)
            consecutive_fails = 0  # reset on success
            print(f"    Chunk {chunk_num} → {len(qs)}Q extracted "
                  f"(total so far: {collected}/{target_count})")
            # Light pacing between successful chunks
            if collected < target_count and INTER_CHUNK_SLEEP > 0:
                _time.sleep(INTER_CHUNK_SLEEP)
        else:
            consecutive_fails += 1
            print(f"    Chunk {chunk_num} structuring failed after retry (fail #{consecutive_fails})")
            if consecutive_fails >= MAX_CONSECUTIVE:
                print(f"    {MAX_CONSECUTIVE} consecutive failures — stopping iteration")
                break
            _time.sleep(3)

        chunk_idx += 1

    print(f"    Iteration complete: {len(all_qs)}/{target_count}Q collected"
          + (" [QUOTA EXHAUSTED]" if quota_exhausted else ""))
    return all_qs, quota_exhausted


# ════════════════════════════════════════════════════════════════════
#  EXAM-ACCURATE INSTRUCTIONS BUILDER
# ════════════════════════════════════════════════════════════════════

# Per-exam instruction templates — reflects real 2025/2026 exam patterns.
_EXAM_INSTRUCTIONS = {
    "neet-ug": {
        "full": [
            "Total Questions: 200 | Attempt any 180",
            "Each subject: Section A — 35 questions (all compulsory)",
            "Each subject: Section B — 15 questions (attempt any 10)",
            "Correct answer: +4 marks | Wrong answer: -1 mark",
            "Unattempted questions: 0 marks (no penalty)",
            "Total Marks: 720 | Duration: 200 minutes",
            "Subjects: Physics (45), Chemistry (45), Botany (45), Zoology (45)",
            "Exam mode: Offline (OMR-based pen & paper)",
        ],
        "subject": [
            "Section A: All questions compulsory",
            "Section B: Attempt any 10 out of 15 questions",
            "Correct answer: +4 marks | Wrong answer: -1 mark",
            "Unattempted questions carry no penalty",
            "NCERT-based — focus on conceptual clarity",
        ],
    },
    "jee-mains": {
        "full": [
            "Total Questions: 90 (30 per subject) | Attempt 75",
            "Section A: 20 MCQs per subject — all compulsory (+4/-1)",
            "Section B: 10 Numerical questions per subject — attempt any 5",
            "Section B numerical: +4 correct | 0 wrong (no negative marking)",
            "Total Marks: 300 | Duration: 180 minutes",
            "Subjects: Physics (100), Chemistry (100), Mathematics (100)",
            "Exam mode: Computer-Based Test (CBT)",
        ],
        "subject": [
            "Section A: MCQ questions — all compulsory (+4/-1)",
            "Section B: Numerical value questions — no negative marking",
            "Correct MCQ: +4 marks | Wrong MCQ: -1 mark",
            "Numerical: +4 marks correct | 0 marks wrong (no penalty)",
            "Enter numerical answers as integers rounded to nearest value",
        ],
    },
    "jee-advanced": {
        "full": [
            "Simulates JEE Advanced Paper 1 (180 marks, 51 questions)",
            "Real exam: 2 papers × 180 marks = 360 total marks",
            "Single-correct MCQ: +3 correct / -1 wrong",
            "Multi-correct MCQ: +4 full / partial marks / -2 wrong",
            "Numerical answer type: +3 correct / 0 wrong (no negative)",
            "Duration: 180 minutes | Subjects: Physics, Chemistry, Mathematics",
            "Highest difficulty — requires deep conceptual understanding",
        ],
        "subject": [
            "JEE Advanced difficulty — conceptual and multi-step problems",
            "Single-correct MCQ: +3 correct / -1 wrong",
            "Numerical questions: +3 correct / no negative marking",
            "Focus on derivations, applications, and problem-solving",
            "Multi-step problems — show working for numerical answers",
        ],
    },
    "upsc-cse": {
        "full": [
            "Simulates UPSC Prelims GS Paper 1 (100 questions, 200 marks)",
            "Correct answer: +2 marks | Wrong answer: -0.67 marks (1/3 deduction)",
            "Unattempted questions: 0 marks",
            "Duration: 120 minutes | Total Marks: 200",
            "Real exam also has CSAT Paper 2 (qualifying — need 33%)",
            "GS Paper 1 covers: History, Geography, Polity, Economy, Environment, Science",
        ],
        "subject": [
            "UPSC Prelims GS Paper 1 style questions",
            "Correct answer: +2 marks | Wrong answer: -0.67 marks",
            "Unattempted questions carry no penalty",
            "Focus on factual accuracy and current affairs linkage",
        ],
    },
}


def _build_exam_instructions(
    class_slug, cfg, total_q, total_m, duration,
    mark_val, neg_val, neg,
    subject_name=None, topic_name=None, is_subject_wise=False
):
    """
    Return an accurate instructions list for a generated test.
    Uses per-exam templates where available; falls back to generic.
    """
    template = _EXAM_INSTRUCTIONS.get(class_slug.lower())

    if template:
        key = "subject" if is_subject_wise else "full"
        instrs = list(template.get(key, template.get("full", [])))
        if is_subject_wise and subject_name:
            instrs = [f"Subject: {subject_name}"] + instrs
        if is_subject_wise and topic_name:
            instrs.append(f"Topic: {topic_name}")
        instrs.append(f"Duration: {duration} minutes")
        return instrs

    # Generic fallback for all other exams
    neg_instr = f"Wrong answer: -{neg_val} mark" if neg else "No negative marking"
    instrs = [
        f"Total Questions: {total_q}",
        f"Total Marks: {total_m}",
        f"Duration: {duration} minutes",
        f"Correct answer: +{mark_val} mark",
        neg_instr,
    ]
    if is_subject_wise and subject_name:
        instrs = [f"Subject: {subject_name}"] + instrs
    if is_subject_wise and topic_name:
        instrs.append(f"Topic: {topic_name}")
    return instrs


# ════════════════════════════════════════════════════════════════════
#  SCHOOL CLASS BUILDERS
# ════════════════════════════════════════════════════════════════════

def build_school_full_length_test(class_slug):
    """
    School full-length test:
      - Pick ONE subject at random from the class
      - Generate 100Q spanning ALL chapters of that subject
      - Title: "CBSE CLASS 10 — Mathematics | Full Length Test"
    """
    topics_data = topic_picker.load_topics(class_slug)
    if not topics_data:
        raise ValueError(f"No topic data for {class_slug}")

    cfg          = topics_data.get("examConfig", {})
    board        = topics_data.get("board", "School")
    class_number = topics_data.get("classNumber")
    exam_name    = _exam_name(class_slug)

    # School full-length = 100Q from one subject
    total_q  = 100
    mark_val = cfg.get("correctMarkValue", 1)
    neg      = cfg.get("negativeMarking", False)
    neg_val  = cfg.get("negativeMarkValue", 0)
    # Duration: 100Q at ~1.5 min each = 150 min
    duration = 150

    # Pick one subject at random
    subjects = topics_data.get("subjects", [])
    if not subjects:
        raise ValueError(f"No subjects for {class_slug}")

    subj = random.choice(subjects)
    subj_slug = subj["slug"]
    subj_name = subj["name"]
    chapters  = subj.get("chapters", [])
    ch_str    = ", ".join(c["name"] for c in chapters) if chapters else "All Topics"

    uid   = _make_id()
    title = f"{exam_name} — {subj_name} | Full Length Test"

    print(f"\n  [School Full-Length] {title}")
    print(f"  Subject: {subj_name} | Chapters: {ch_str[:80]}")
    print(f"  Target: {total_q}Q spanning all chapters\n")

    # Research: full-subject coverage
    research = _get_school_full_research(
        class_slug, subj_slug, subj_name, ch_str, exam_name, class_number
    )

    # Generate 100Q — prompt instructs to spread across all chapters
    raw_qs, quota_hit = _iterate_to_target(
        total_q, exam_name, subj_name, research, ch_str,
        prompt_fn=_gen_prompt_school_full,
        prompt_kwargs={"class_number": class_number}
    )

    # Accept any result with at least 1 question — partial is better than nothing
    if len(raw_qs) < 1:
        if quota_hit:
            raise GeminiQuotaError(f"All keys exhausted — zero questions for {class_slug} full-length")
        raise Exception(f"Zero questions generated for {class_slug} full-length")

    questions = [
        _validate_q(q, i, mark_val, neg_val, neg, subj_slug)
        for i, q in enumerate(raw_qs[:total_q])
    ]

    actual_q    = len(questions)
    total_marks = actual_q * mark_val
    neg_instr   = f"Wrong answer: -{neg_val} mark" if neg else "No negative marking"
    is_partial  = actual_q < total_q
    if is_partial:
        status = " [QUOTA EXHAUSTED]" if quota_hit else ""
        print(f"  [Partial] {actual_q}/{total_q}Q — saving draft, will resume next run{status}")

    payload = {
        "id": uid, "title": title,
        "description": (
            f"Full-length {subj_name} test for {exam_name}. "
            f"{actual_q} questions covering all chapters."
        ),
        "board": board, "classSlug": class_slug,
        "classNumber": class_number or 0,
        "subjects": [subj_slug], "subject": subj_slug,
        "chapter": "All Chapters", "topic": "Full Length",
        "testType": "full-length",
        "duration": duration,
        "totalMarks": total_marks,
        "passingMarks": round(total_marks * 0.35),
        "totalQuestions": actual_q,
        "targetQuestions": total_q,
        "isPublic": True, "isPublished": True, "isAdmin": True,
        "showCorrectAnswers": True, "randomizeQuestions": True,
        "unlimitedAttempts": True, "allowedAttempts": 0,
        "tags": _tags(class_slug, subj_slug, "full-length", subj_name),
        "instructions": _build_exam_instructions(
            class_slug, cfg, actual_q, total_marks, duration,
            mark_val, neg_val, neg,
            subject_name=subj_name, is_subject_wise=False
        ) + ["Covers all chapters of the subject"],
        "createdBy": "admin", "createdByName": "PrepGuide Lite Admin",
        "publishedAt": _now(), "questions": questions,
    }
    path = _admin_path("tests", uid, title, class_slug)

    if is_partial:
        # Save draft — NOT published to MongoDB, app won't see it
        draft_store.save_draft(
            uid=uid, title=title, class_slug=class_slug,
            test_type="school-full-length", target_q=total_q,
            payload=payload, final_path=path,
            resume_context={
                "subj_slug": subj_slug, "subj_name": subj_name,
                "ch_str": ch_str, "class_number": class_number,
                "remaining_q": total_q - actual_q,
            }
        )
        if quota_hit:
            # Signal to generate.py that all keys are exhausted — stop the run
            raise GeminiQuotaError(
                f"All keys exhausted after {actual_q}/{total_q}Q for {title}. "
                "Draft saved — will resume next run."
            )
    else:
        # Complete — save to CDN and publish to MongoDB
        _save_and_publish(path, payload)

    entry = {
        "id": uid, "type": "test", "title": title,
        "description": payload["description"],
        "subjects": [subj_slug], "subject": subj_slug,
        "board": board, "classSlug": class_slug,
        "classNumber": class_number or 0,
        "chapter": "All Chapters", "testType": "full-length",
        "totalQuestions": actual_q,
        "targetQuestions": total_q,
        "duration": duration, "totalMarks": total_marks,
        "author": "PrepGuide Lite Admin", "authorId": "admin", "isAdmin": True,
        "tags": payload["tags"], "cdnUrl": _cdn(path), "path": path,
        "publishedAt": payload["publishedAt"],
    }
    return uid, path, entry


# ════════════════════════════════════════════════════════════════════
#  COMPETITIVE EXAM BUILDERS
# ════════════════════════════════════════════════════════════════════

def build_exam_full_length_test(class_slug):
    """
    Competitive exam full-length test:
      - ALL subjects combined
      - Exact per-subject question counts from full-length.json / weightage
      - Title: "NEET UG — Full Length Mock Test"
    """
    topics_data = topic_picker.load_topics(class_slug)
    if not topics_data:
        raise ValueError(f"No topic data for {class_slug}")

    cfg       = topics_data.get("examConfig", {})
    board     = topics_data.get("board", "Competitive")
    exam_name = _exam_name(class_slug)
    total_q   = cfg.get("fullLengthQuestions", 100)
    total_m   = cfg.get("fullLengthMarks", total_q)
    duration  = cfg.get("fullLengthDuration", 120)
    mark_val  = cfg.get("correctMarkValue", 1)
    neg       = cfg.get("negativeMarking", False)
    neg_val   = cfg.get("negativeMarkValue", 0)

    subjects_data = topic_picker.pick_subjects_for_full_length(class_slug)
    if not subjects_data:
        raise ValueError(f"No subjects for {class_slug}")

    uid   = _make_id()
    title = f"{exam_name} — Full Length Mock Test"

    subjects_summary = ", ".join(s["subjectName"] for s in subjects_data)
    exam_research    = _get_full_exam_research(class_slug, exam_name, subjects_summary)

    print(f"\n  [Exam Full-Length] {title}")
    print(f"  Subjects: {subjects_summary}")
    print(f"  Target: {total_q}Q | Duration: {duration}min\n")

    all_questions = []

    for subj in subjects_data:
        subj_slug = subj["subjectSlug"]
        subj_name = subj["subjectName"]
        q_count   = subj["questionCount"]
        chapters  = subj.get("chapters", [])
        ch_str    = ", ".join(c["name"] for c in chapters[:6]) if chapters else "General Topics"

        print(f"\n  [{subj_name}] Target: {q_count}Q")

        subj_research = _get_research(
            class_slug, subj_slug, subj_name, ch_str, exam_name,
            is_school_exam=False
        )
        combined_ctx = (
            f"EXAM OVERVIEW:\n{exam_research}\n\n"
            f"SUBJECT DETAIL:\n{subj_research}"
        ).strip()

        raw_qs, quota_hit = _iterate_to_target(
            q_count, exam_name, subj_name, combined_ctx, ch_str,
            prompt_fn=_gen_prompt_exam_full
        )
        for raw_q in raw_qs[:q_count]:
            idx = len(all_questions)
            all_questions.append(_validate_q(raw_q, idx, mark_val, neg_val, neg, subj_slug))
        if quota_hit:
            print(f"  [ExamFull] Quota exhausted after {subj_name} — stopping subject loop")
            break

    if len(all_questions) < 1:
        if quota_hit:
            raise GeminiQuotaError(
                f"All keys exhausted — zero questions for {class_slug} full-length. "
                "Check API keys and quotas."
            )
        raise Exception(
            f"Zero questions generated for {class_slug} full-length. "
            "Check API keys and quotas."
        )

    actual_q  = len(all_questions)
    is_partial = actual_q < total_q
    if is_partial:
        status = " [QUOTA EXHAUSTED]" if quota_hit else ""
        print(f"  [Partial] {actual_q}/{total_q}Q — saving draft, will resume next run{status}")
        total_m = round(total_m * actual_q / total_q)

    neg_instr = f"Wrong answer: -{neg_val} mark" if neg else "No negative marking"

    # ── Build exam-accurate instructions ──────────────────────────────────────
    exam_instructions = _build_exam_instructions(class_slug, cfg, actual_q, total_m, duration, mark_val, neg_val, neg)

    payload = {
        "id": uid, "title": title,
        "description": (
            f"Full-length mock test for {exam_name}. "
            f"{actual_q} questions across all subjects."
        ),
        "board": board, "classSlug": class_slug, "classNumber": 0,
        "subjects": [s["subjectSlug"] for s in subjects_data],
        "subject": "all", "chapter": "Full Syllabus", "topic": "Full Length",
        "testType": "full-length",
        "duration": duration, "totalMarks": total_m,
        "passingMarks": round(total_m * 0.35),
        "totalQuestions": actual_q,
        "targetQuestions": total_q,
        "isPublic": True, "isPublished": True, "isAdmin": True,
        "showCorrectAnswers": True, "randomizeQuestions": True,
        "unlimitedAttempts": True, "allowedAttempts": 0,
        "tags": [class_slug, "full-length", "mock-test", "all-subjects"],
        "instructions": exam_instructions,
        "createdBy": "admin", "createdByName": "PrepGuide Lite Admin",
        "publishedAt": _now(), "questions": all_questions,
    }
    path = _admin_path("tests", uid, title, class_slug)

    # Work out which subjects still need questions (for resume)
    collected_per_subj = {}
    for q in all_questions:
        s = q.get("subject", "")
        collected_per_subj[s] = collected_per_subj.get(s, 0) + 1
    remaining_subjects = []
    for subj in subjects_data:
        needed = subj["questionCount"] - collected_per_subj.get(subj["subjectSlug"], 0)
        if needed > 0:
            remaining_subjects.append({**subj, "questionCount": needed})

    if is_partial:
        draft_store.save_draft(
            uid=uid, title=title, class_slug=class_slug,
            test_type="exam-full-length", target_q=total_q,
            payload=payload, final_path=path,
            resume_context={
                "remaining_subjects": remaining_subjects,
                "mark_val": mark_val, "neg": neg, "neg_val": neg_val,
                "exam_research": exam_research,
            }
        )
        if quota_hit:
            raise GeminiQuotaError(
                f"All keys exhausted after {actual_q}/{total_q}Q for {title}. "
                "Draft saved — will resume next run."
            )
    else:
        _save_and_publish(path, payload)

    entry = {
        "id": uid, "type": "test", "title": title,
        "description": payload["description"],
        "subjects": payload["subjects"], "subject": "all",
        "board": board, "classSlug": class_slug, "classNumber": 0,
        "chapter": "Full Syllabus", "testType": "full-length",
        "totalQuestions": actual_q,
        "targetQuestions": total_q,
        "duration": duration, "totalMarks": total_m,
        "author": "PrepGuide Lite Admin", "authorId": "admin", "isAdmin": True,
        "tags": payload["tags"], "cdnUrl": _cdn(path), "path": path,
        "publishedAt": payload["publishedAt"],
    }
    return uid, path, entry


# ════════════════════════════════════════════════════════════════════
#  SHARED BUILDERS (subject-wise + combined — work for both types)
# ════════════════════════════════════════════════════════════════════

def build_subject_test(topic_info, q_count_override=None):
    """
    Subject-wise test for both school classes and competitive exams.

    q_count_override: for both school and competitive, the caller (generate.py)
                      passes the randomly chosen size. If None, falls back to
                      examConfig.subjectWiseQuestions.

    Uses the correct prompt style based on exam type.
    Duration scales with question count.
    """
    class_slug   = topic_info["classSlug"]
    board        = topic_info["board"]
    subj_slug    = topic_info["subjectSlug"]
    subj_name    = topic_info["subjectName"]
    chapter      = topic_info["chapter"]
    topic        = topic_info["topic"]
    exam_name    = _exam_name(class_slug)
    cfg          = topic_info.get("examConfig", {})
    exam_type    = topic_info.get("type", "competitive")
    is_school    = (exam_type == "school")
    class_number = None

    if is_school:
        topics_data  = topic_picker.load_topics(class_slug)
        class_number = (topics_data or {}).get("classNumber")

    # Cycle number — passed in topic_info by topic_picker
    cycle = topic_info.get("cycle", 1)

    # Question count: caller override > config default
    if q_count_override:
        q_count = q_count_override
        if is_school:
            # School: ~1.5 min/Q
            duration = max(30, round(q_count * 1.5))
        else:
            # Competitive: use configured duration (it's per-session, not per-Q)
            duration = cfg.get("subjectWiseDuration", 60)
    else:
        q_count  = cfg.get("subjectWiseQuestions", 20 if is_school else 30)
        duration = cfg.get("subjectWiseDuration", 60)

    mark_val = cfg.get("correctMarkValue", 1)
    neg      = cfg.get("negativeMarking", False)
    neg_val  = cfg.get("negativeMarkValue", 0)

    uid   = _make_id()
    title = f"{exam_name} — {subj_name} | {topic}"

    print(f"\n  [Subject Test] {title}")
    print(f"  Type: {'School' if is_school else 'Competitive'} | Target: {q_count}Q")

    research = _get_research(
        class_slug, subj_slug, subj_name, chapter, exam_name,
        is_school_exam=is_school, class_number=class_number
    )

    if is_school:
        prompt_fn     = _gen_prompt_school_subject
        prompt_kwargs = {"class_number": class_number, "cycle": cycle}
    else:
        prompt_fn     = _gen_prompt_exam_subject
        prompt_kwargs = {"cycle": cycle}

    raw_qs, quota_hit = _iterate_to_target(
        q_count, exam_name, subj_name, research,
        f"{chapter} — {topic}",
        prompt_fn=prompt_fn, prompt_kwargs=prompt_kwargs
    )

    # Accept any result with at least 1 question — partial is better than nothing
    if len(raw_qs) < 1:
        if quota_hit:
            raise GeminiQuotaError(f"All keys exhausted — zero questions for {title}")
        raise Exception(f"Zero questions generated for {title}")

    questions   = [_validate_q(q, i, mark_val, neg_val, neg, subj_slug) for i, q in enumerate(raw_qs[:q_count])]
    actual_q    = len(questions)
    is_partial  = actual_q < q_count
    total_marks = actual_q * mark_val
    neg_instr   = f"Wrong answer: -{neg_val} mark" if neg else "No negative marking"
    if is_partial:
        status = " [QUOTA EXHAUSTED]" if quota_hit else ""
        print(f"  [Partial] {actual_q}/{q_count}Q — saving draft, will resume next run{status}")

    # ── Build exam-accurate instructions ──────────────────────────────────────
    cfg_for_instr = topic_info.get("examConfig", {})
    sw_instructions = _build_exam_instructions(
        class_slug, cfg_for_instr, actual_q, total_marks, duration,
        mark_val, neg_val, neg,
        subject_name=subj_name, topic_name=topic, is_subject_wise=True
    )

    payload = {
        "id": uid, "title": title,
        "description": (
            f"Subject-wise test on {topic} ({subj_name}) for {exam_name}. "
            f"{actual_q}Q."
        ),
        "board": board, "classSlug": class_slug,
        "classNumber": class_number or 0,
        "subjects": [subj_slug], "subject": subj_slug,
        "chapter": chapter, "topic": topic,
        "testType": "subject-wise",
        "duration": duration,
        "totalMarks": total_marks,
        "passingMarks": round(total_marks * 0.4),
        "totalQuestions": actual_q,
        "targetQuestions": q_count,
        "isPublic": True, "isPublished": True, "isAdmin": True,
        "showCorrectAnswers": True, "randomizeQuestions": False,
        "unlimitedAttempts": True, "allowedAttempts": 0,
        "tags": _tags(class_slug, subj_slug, chapter, topic),
        "instructions": sw_instructions,
        "createdBy": "admin", "createdByName": "PrepGuide Lite Admin",
        "publishedAt": _now(), "questions": questions,
    }
    path = _admin_path("tests", uid, title, class_slug)

    if is_partial:
        draft_store.save_draft(
            uid=uid, title=title, class_slug=class_slug,
            test_type="subject-wise", target_q=q_count,
            payload=payload, final_path=path,
            resume_context={
                "remaining_q": q_count - actual_q,
                "subj_slug": subj_slug, "subj_name": subj_name,
                "chapter": chapter, "topic": topic,
                "is_school": is_school, "class_number": class_number,
                "cycle": cycle,
            }
        )
        if quota_hit:
            raise GeminiQuotaError(
                f"All keys exhausted after {actual_q}/{q_count}Q for {title}. "
                "Draft saved — will resume next run."
            )
    else:
        _save_and_publish(path, payload)

    entry = {
        "id": uid, "type": "test", "title": title,
        "description": payload["description"],
        "subjects": [subj_slug], "subject": subj_slug,
        "board": board, "classSlug": class_slug,
        "classNumber": class_number or 0,
        "chapter": chapter, "topic": topic, "testType": "subject-wise",
        "totalQuestions": actual_q,
        "targetQuestions": q_count,
        "duration": duration, "totalMarks": total_marks,
        "author": "PrepGuide Lite Admin", "authorId": "admin", "isAdmin": True,
        "tags": payload["tags"], "cdnUrl": _cdn(path), "path": path,
        "publishedAt": payload["publishedAt"],
    }
    return uid, path, entry


def build_combined_test(class_slug, subject_topics, q_per_subj_override=None):
    """
    Combined test (2-3 subjects) for competitive exams only.

    q_per_subj_override: caller passes combinedTestQPerSubject from examConfig.
                         Falls back to combinedTestQPerSubject in config, then
                         subjectWiseQMin, then subjectWiseQuestions.
    """
    if not subject_topics:
        raise ValueError("No subjects provided for combined test")

    topics_data  = topic_picker.load_topics(class_slug)
    cfg          = (topics_data or {}).get("examConfig", {})
    board        = (topics_data or {}).get("board", "Competitive")
    exam_name    = _exam_name(class_slug)
    is_school    = _is_school(topics_data)
    class_number = (topics_data or {}).get("classNumber") if is_school else None

    # Q per subject: caller override > config combinedTestQPerSubject > subjectWiseQMin > subjectWiseQuestions
    if q_per_subj_override:
        q_per_subj = q_per_subj_override
    else:
        q_per_subj = cfg.get(
            "combinedTestQPerSubject",
            cfg.get("subjectWiseQMin",
                    cfg.get("subjectWiseQuestions", 25))
        )

    mark_val   = cfg.get("correctMarkValue", 1)
    neg        = cfg.get("negativeMarking", False)
    neg_val    = cfg.get("negativeMarkValue", 0)

    # Duration: configured subject-wise duration (it's per-session, not scaled)
    subj_dur   = cfg.get("subjectWiseDuration", 60)
    duration   = min(subj_dur * len(subject_topics), 120)

    uid        = _make_id()
    subj_names = " + ".join(t["subjectName"] for t in subject_topics)
    title      = f"{exam_name} — Combined: {subj_names}"
    total_q    = q_per_subj * len(subject_topics)

    # Use the highest cycle number across all picked topics
    cycle = max((ti.get("cycle", 1) for ti in subject_topics), default=1)

    print(f"\n  [Combined Test] {title}")
    print(f"  Type: {'School' if is_school else 'Competitive'} | Cycle: {cycle}")
    print(f"  Target: {total_q}Q ({q_per_subj}Q × {len(subject_topics)} subjects)")

    if is_school:
        prompt_fn     = _gen_prompt_school_subject
        prompt_kwargs = {"class_number": class_number, "cycle": cycle}
    else:
        prompt_fn     = _gen_prompt_exam_subject
        prompt_kwargs = {"cycle": cycle}

    all_questions  = []
    all_subj_slugs = []

    for ti in subject_topics:
        subj_slug = ti["subjectSlug"]
        subj_name = ti["subjectName"]
        chapter   = ti["chapter"]
        topic     = ti["topic"]
        all_subj_slugs.append(subj_slug)

        print(f"\n  [{subj_name}] Target: {q_per_subj}Q on {topic}")
        research = _get_research(
            class_slug, subj_slug, subj_name, chapter, exam_name,
            is_school_exam=is_school, class_number=class_number
        )

        raw_qs, quota_hit = _iterate_to_target(
            q_per_subj, exam_name, subj_name, research,
            f"{chapter} — {topic}",
            prompt_fn=prompt_fn, prompt_kwargs=prompt_kwargs
        )
        for raw_q in raw_qs[:q_per_subj]:
            idx = len(all_questions)
            all_questions.append(_validate_q(raw_q, idx, mark_val, neg_val, neg, subj_slug))
        if quota_hit:
            print(f"  [Combined] Quota exhausted after {subj_name} — stopping subject loop")
            break

    # Accept any result with at least 1 question — partial is better than nothing
    if len(all_questions) < 1:
        if quota_hit:
            raise GeminiQuotaError(f"All keys exhausted — zero questions for combined test: {title}")
        raise Exception(f"Zero questions generated for combined test: {title}")

    actual_q    = len(all_questions)
    is_partial  = actual_q < total_q
    total_marks = actual_q * mark_val
    neg_instr   = f"Wrong answer: -{neg_val} mark" if neg else "No negative marking"
    if is_partial:
        status = " [QUOTA EXHAUSTED]" if quota_hit else ""
        print(f"  [Partial] {actual_q}/{total_q}Q — saving draft, will resume next run{status}")

    payload = {
        "id": uid, "title": title,
        "description": (
            f"Combined test: {subj_names} for {exam_name}. "
            f"{actual_q}Q."
        ),
        "board": board, "classSlug": class_slug,
        "classNumber": class_number or 0,
        "subjects": all_subj_slugs, "subject": all_subj_slugs[0],
        "chapter": "Multiple Chapters", "topic": "Combined",
        "testType": "subject-wise",
        "duration": duration,
        "totalMarks": total_marks,
        "passingMarks": round(total_marks * 0.4),
        "totalQuestions": actual_q,
        "targetQuestions": total_q,
        "isPublic": True, "isPublished": True, "isAdmin": True,
        "showCorrectAnswers": True, "randomizeQuestions": False,
        "unlimitedAttempts": True, "allowedAttempts": 0,
        "tags": _tags(class_slug, "+".join(all_subj_slugs), subj_names),
        "instructions": _build_exam_instructions(
            class_slug, cfg, actual_q, total_marks, duration,
            mark_val, neg_val, neg,
            subject_name=subj_names, is_subject_wise=True
        ),
        "createdBy": "admin", "createdByName": "PrepGuide Lite Admin",
        "publishedAt": _now(), "questions": all_questions,
    }
    path = _admin_path("tests", uid, title, class_slug)

    # Work out which subjects still need questions (for resume)
    collected_per_subj = {}
    for q in all_questions:
        s = q.get("subject", "")
        collected_per_subj[s] = collected_per_subj.get(s, 0) + 1
    remaining_topics = [
        ti for ti in subject_topics
        if collected_per_subj.get(ti["subjectSlug"], 0) < q_per_subj
    ]

    if is_partial:
        draft_store.save_draft(
            uid=uid, title=title, class_slug=class_slug,
            test_type="combined", target_q=total_q,
            payload=payload, final_path=path,
            resume_context={
                "remaining_topics": remaining_topics,
                "q_per_subj": q_per_subj,
                "collected_per_subj": collected_per_subj,
                "is_school": is_school, "class_number": class_number,
                "cycle": cycle,
            }
        )
        if quota_hit:
            raise GeminiQuotaError(
                f"All keys exhausted after {actual_q}/{total_q}Q for {title}. "
                "Draft saved — will resume next run."
            )
    else:
        _save_and_publish(path, payload)

    entry = {
        "id": uid, "type": "test", "title": title,
        "description": payload["description"],
        "subjects": all_subj_slugs, "subject": all_subj_slugs[0],
        "board": board, "classSlug": class_slug,
        "classNumber": class_number or 0,
        "chapter": "Multiple Chapters", "testType": "subject-wise",
        "totalQuestions": actual_q,
        "targetQuestions": total_q,
        "duration": duration, "totalMarks": total_marks,
        "author": "PrepGuide Lite Admin", "authorId": "admin", "isAdmin": True,
        "tags": payload["tags"], "cdnUrl": _cdn(path), "path": path,
        "publishedAt": payload["publishedAt"],
    }
    return uid, path, entry


# ── Legacy alias — kept so any external callers still work ───────────────────
def build_full_length_test(class_slug):
    """
    Backwards-compatible wrapper. Routes to the correct builder based on type.
    """
    topics_data = topic_picker.load_topics(class_slug)
    if _is_school(topics_data):
        return build_school_full_length_test(class_slug)
    return build_exam_full_length_test(class_slug)


# ════════════════════════════════════════════════════════════════════
#  DRAFT RESUME
# ════════════════════════════════════════════════════════════════════

def resume_draft(draft):
    """
    Resume a partial test from a saved draft.

    Generates the remaining questions, appends them to the existing
    questions in the draft payload, then:
      - If now complete → saves to CDN + publishes to MongoDB + deletes draft
      - If still partial → updates the draft file for the next run

    Returns (uid, path, entry) on completion, or (None, None, None) if
    still incomplete after this run.
    Raises GeminiQuotaError if all keys are exhausted mid-resume.
    """
    uid          = draft["uid"]
    title        = draft["title"]
    class_slug   = draft["class_slug"]
    test_type    = draft["test_type"]
    target_q     = draft["target_q"]
    payload      = draft["payload"]
    final_path   = draft["final_path"]
    resume_ctx   = draft["resume_context"]
    exam_name    = _exam_name(class_slug)

    existing_qs  = payload.get("questions", [])
    collected    = len(existing_qs)
    still_needed = target_q - collected

    print(f"\n  [Resume] {title}")
    print(f"  Progress: {collected}/{target_q}Q — need {still_needed} more")

    topics_data  = topic_picker.load_topics(class_slug)
    cfg          = (topics_data or {}).get("examConfig", {})
    mark_val     = cfg.get("correctMarkValue", 1)
    neg          = cfg.get("negativeMarking", False)
    neg_val      = cfg.get("negativeMarkValue", 0)

    new_qs    = []
    quota_hit = False   # tracks whether any _iterate_to_target call hit quota

    if test_type in ("subject-wise",):
        # ── Resume subject-wise / combined subject ────────────────────────────
        subj_slug    = resume_ctx["subj_slug"]
        subj_name    = resume_ctx["subj_name"]
        chapter      = resume_ctx["chapter"]
        topic        = resume_ctx["topic"]
        is_school    = resume_ctx.get("is_school", False)
        class_number = resume_ctx.get("class_number")
        cycle        = resume_ctx.get("cycle", 1)

        research = _get_research(
            class_slug, subj_slug, subj_name, chapter, exam_name,
            is_school_exam=is_school, class_number=class_number
        )
        if is_school:
            prompt_fn     = _gen_prompt_school_subject
            prompt_kwargs = {"class_number": class_number, "cycle": cycle}
        else:
            prompt_fn     = _gen_prompt_exam_subject
            prompt_kwargs = {"cycle": cycle}

        raw_qs, quota_hit = _iterate_to_target(
            still_needed, exam_name, subj_name, research,
            f"{chapter} — {topic}",
            prompt_fn=prompt_fn, prompt_kwargs=prompt_kwargs
        )
        for raw_q in raw_qs[:still_needed]:
            idx = len(existing_qs) + len(new_qs)
            new_qs.append(_validate_q(raw_q, idx, mark_val, neg_val, neg, subj_slug))

    elif test_type in ("school-full-length",):
        # ── Resume school full-length ─────────────────────────────────────────
        subj_slug    = resume_ctx["subj_slug"]
        subj_name    = resume_ctx["subj_name"]
        ch_str       = resume_ctx["ch_str"]
        class_number = resume_ctx.get("class_number")

        research = _get_school_full_research(
            class_slug, subj_slug, subj_name, ch_str, exam_name, class_number
        )
        raw_qs, quota_hit = _iterate_to_target(
            still_needed, exam_name, subj_name, research, ch_str,
            prompt_fn=_gen_prompt_school_full,
            prompt_kwargs={"class_number": class_number}
        )
        for raw_q in raw_qs[:still_needed]:
            idx = len(existing_qs) + len(new_qs)
            new_qs.append(_validate_q(raw_q, idx, mark_val, neg_val, neg, subj_slug))

    elif test_type in ("exam-full-length",):
        # ── Resume exam full-length — continue from remaining subjects ────────
        remaining_subjects = resume_ctx.get("remaining_subjects", [])
        exam_research      = resume_ctx.get("exam_research", "")
        quota_hit          = False

        for subj in remaining_subjects:
            subj_slug = subj["subjectSlug"]
            subj_name = subj["subjectName"]
            q_count   = subj["questionCount"]
            chapters  = subj.get("chapters", [])
            ch_str    = ", ".join(c["name"] for c in chapters[:6]) if chapters else "General Topics"

            print(f"\n  [{subj_name}] Resume: {q_count}Q remaining")
            subj_research = _get_research(
                class_slug, subj_slug, subj_name, ch_str, exam_name,
                is_school_exam=False
            )
            combined_ctx = (
                f"EXAM OVERVIEW:\n{exam_research}\n\n"
                f"SUBJECT DETAIL:\n{subj_research}"
            ).strip()

            raw_qs, quota_hit = _iterate_to_target(
                q_count, exam_name, subj_name, combined_ctx, ch_str,
                prompt_fn=_gen_prompt_exam_full
            )
            for raw_q in raw_qs[:q_count]:
                idx = len(existing_qs) + len(new_qs)
                new_qs.append(_validate_q(raw_q, idx, mark_val, neg_val, neg, subj_slug))
            if quota_hit:
                print(f"  [Resume] Quota exhausted after {subj_name} — stopping subject loop")
                break

    elif test_type in ("combined",):
        # ── Resume combined — continue from remaining subjects ────────────────
        remaining_topics   = resume_ctx.get("remaining_topics", [])
        q_per_subj         = resume_ctx.get("q_per_subj", 25)
        collected_per_subj = resume_ctx.get("collected_per_subj", {})
        is_school          = resume_ctx.get("is_school", False)
        class_number       = resume_ctx.get("class_number")
        cycle              = resume_ctx.get("cycle", 1)
        quota_hit          = False

        if is_school:
            prompt_fn     = _gen_prompt_school_subject
            prompt_kwargs = {"class_number": class_number, "cycle": cycle}
        else:
            prompt_fn     = _gen_prompt_exam_subject
            prompt_kwargs = {"cycle": cycle}

        for ti in remaining_topics:
            subj_slug = ti["subjectSlug"]
            subj_name = ti["subjectName"]
            chapter   = ti["chapter"]
            topic     = ti["topic"]
            already   = collected_per_subj.get(subj_slug, 0)
            need      = q_per_subj - already

            print(f"\n  [{subj_name}] Resume: {need}Q remaining on {topic}")
            research = _get_research(
                class_slug, subj_slug, subj_name, chapter, exam_name,
                is_school_exam=is_school, class_number=class_number
            )
            raw_qs, quota_hit = _iterate_to_target(
                need, exam_name, subj_name, research,
                f"{chapter} — {topic}",
                prompt_fn=prompt_fn, prompt_kwargs=prompt_kwargs
            )
            for raw_q in raw_qs[:need]:
                idx = len(existing_qs) + len(new_qs)
                new_qs.append(_validate_q(raw_q, idx, mark_val, neg_val, neg, subj_slug))
            if quota_hit:
                print(f"  [Resume] Quota exhausted after {subj_name} — stopping subject loop")
                break

    # ── Merge new questions into payload ──────────────────────────────────────
    all_questions = existing_qs + new_qs
    actual_q      = len(all_questions)
    is_partial    = actual_q < target_q

    # Update payload with merged questions and correct counts
    total_marks = actual_q * mark_val
    payload["questions"]      = all_questions
    payload["totalQuestions"] = actual_q
    payload["totalMarks"]     = total_marks
    payload["passingMarks"]   = round(total_marks * 0.35)

    print(f"\n  [Resume] Now have {actual_q}/{target_q}Q"
          + (" [QUOTA EXHAUSTED]" if quota_hit else ""))

    if is_partial:
        # Still not done — update draft for next run
        print(f"  [Resume] Still partial — updating draft for next run")
        # Rebuild resume_context with updated remaining info
        if test_type == "exam-full-length":
            collected_per_subj = {}
            for q in all_questions:
                s = q.get("subject", "")
                collected_per_subj[s] = collected_per_subj.get(s, 0) + 1
            orig_subjects = topic_picker.pick_subjects_for_full_length(class_slug)
            remaining_subjects = []
            for subj in orig_subjects:
                needed = subj["questionCount"] - collected_per_subj.get(subj["subjectSlug"], 0)
                if needed > 0:
                    remaining_subjects.append({**subj, "questionCount": needed})
            resume_ctx["remaining_subjects"] = remaining_subjects
        elif test_type == "combined":
            collected_per_subj = {}
            for q in all_questions:
                s = q.get("subject", "")
                collected_per_subj[s] = collected_per_subj.get(s, 0) + 1
            resume_ctx["collected_per_subj"] = collected_per_subj
            resume_ctx["remaining_topics"] = [
                ti for ti in resume_ctx.get("remaining_topics", [])
                if collected_per_subj.get(ti["subjectSlug"], 0) < resume_ctx.get("q_per_subj", 25)
            ]
        else:
            resume_ctx["remaining_q"] = target_q - actual_q

        draft_store.save_draft(
            uid=uid, title=title, class_slug=class_slug,
            test_type=test_type, target_q=target_q,
            payload=payload, final_path=final_path,
            resume_context=resume_ctx
        )
        # If quota was exhausted, signal the run loop to stop
        if quota_hit:
            raise GeminiQuotaError(
                f"All keys exhausted during resume of {title} "
                f"({actual_q}/{target_q}Q). Draft updated — will resume next run."
            )
        return None, None, None

    # ── Complete! Save to CDN, publish to MongoDB, delete draft ──────────────
    print(f"  [Resume] ✅ Complete! Publishing to CDN + MongoDB")
    _save_and_publish(final_path, payload)
    draft_store.delete_draft(uid, class_slug)

    entry = {
        "id": uid, "type": "test", "title": title,
        "description": payload.get("description", ""),
        "subjects": payload.get("subjects", []),
        "subject":  payload.get("subject", ""),
        "board":    payload.get("board", ""),
        "classSlug": class_slug,
        "classNumber": payload.get("classNumber", 0),
        "chapter":  payload.get("chapter", ""),
        "topic":    payload.get("topic", ""),
        "testType": payload.get("testType", "subject-wise"),
        "totalQuestions": actual_q,
        "targetQuestions": target_q,
        "duration":   payload.get("duration", 60),
        "totalMarks": total_marks,
        "author": "PrepGuide Lite Admin", "authorId": "admin", "isAdmin": True,
        "tags": payload.get("tags", []),
        "cdnUrl": _cdn(final_path), "path": final_path,
        "publishedAt": payload.get("publishedAt", _now()),
    }
    return uid, final_path, entry

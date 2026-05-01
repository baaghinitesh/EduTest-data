"""
content_builder.py — TEST-ONLY generation pipeline.

Design:
  - Always generate in 10Q chunks — never more per API call
  - Iterate chunks until target reached: 10→20→30→40→50 for subject tests
  - Full-length: iterate until exam's fullLengthQuestions count reached
  - All 3 Gemini keys rotate across chunks (round-robin) — no single key overloaded
  - Research done ONCE per subject, full quality, cached for reuse
  - Groq structures each chunk independently — K1→K2→Flash fallback per chunk
  - Accept partial results — save what we have if a chunk fails mid-way

Pipeline per chunk:
  Step 1 — Research   : Flash K1 → Flash K2 → cached (once per subject)
  Step 2 — Generate   : K3 Pro → K1 Flash → K2 Flash → K3 Flash (rotates per chunk)
  Step 3 — Structure  : Groq K1 → Groq K2 → Flash K1 → Flash K2 (per chunk)
"""
import os, json, uuid, re
from datetime import datetime, timezone

import gemini_client
import groq_client
import research_cache
import publish_to_server
from gemini_client import GeminiError
from groq_client   import GroqError
import topic_picker

ROOT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CDN_BASE = "https://prepguidedata.launchyourconcept.com"

# ── 10Q per chunk — safe for all API limits ───────────────────────────────────
CHUNK = 10

# ── Compact JSON schema ───────────────────────────────────────────────────────
_SCHEMA = (
    '[{"question":"...","difficulty":"medium","explanation":"...",'
    '"hint":"","options":['
    '{"letter":"A","text":"...","isCorrect":true,"explanation":"..."},'
    '{"letter":"B","text":"...","isCorrect":false,"explanation":"..."},'
    '{"letter":"C","text":"...","isCorrect":false,"explanation":"..."},'
    '{"letter":"D","text":"...","isCorrect":false,"explanation":"..."}]}]'
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_id():
    return str(uuid.uuid4())

def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def _cdn(path):
    return f"{CDN_BASE}/{path}"

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
    """Save JSON to disk AND publish to server MongoDB so it appears in the app."""
    _save(path, data)
    # Publish to server — creates MongoDB record with cdnUrl + githubPath
    # so the test shows up in /api/content immediately
    publish_to_server.publish_test(data, path)

def _tags(class_slug, subj_slug, *extra):
    tags = {class_slug, subj_slug}
    for phrase in extra:
        for w in str(phrase).lower().split():
            if len(w) > 3:
                tags.add(re.sub(r"[^a-z0-9]", "", w))
    return list(tags)[:10]

def _exam_name(class_slug):
    return class_slug.upper().replace("-", " ")


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


# ── Step 1: Research (with cache) ─────────────────────────────────────────────

def _get_research(class_slug, subject_slug, subject_name, chapters_str, exam_name):
    """
    Full-quality research for a subject. Cached — only fetched once per subject.
    Returns complete research text (not trimmed) for high-quality generation.
    """
    cached = research_cache.get_subject_research(class_slug, subject_slug, chapters_str)
    if cached:
        print(f"  [Research] ✅ Cache hit: {subject_slug}")
        return cached

    prompt = (
        f"You are a senior {exam_name} exam expert.\n"
        f"Subject: {subject_name}\n"
        f"Key chapters: {chapters_str}\n\n"
        "Provide a detailed 250-word analysis:\n"
        "1. Most-tested question patterns and formats in this exam\n"
        "2. Difficulty distribution (Easy/Medium/Hard %)\n"
        "3. Top 8 high-yield topics students must master\n"
        "4. Common wrong-answer traps and misconceptions\n"
        "5. Numerical/formula-heavy areas with examples\n"
        "6. Recent trend: topics gaining more weightage\n"
        "Be specific and exam-focused. No generic advice."
    )
    print(f"  [Research] Fetching: {subject_slug}...")
    result = gemini_client.research(prompt, max_tokens=1500)
    if result:
        research_cache.store_subject_research(class_slug, subject_slug, result, chapters_str)
        print(f"  [Research] Cached {len(result)} chars for {subject_slug}")
    return result or "Standard exam difficulty. Focus on core concepts."


def _get_full_exam_research(class_slug, exam_name, subjects_summary):
    """Full-quality full-exam research. Cached at exam level."""
    cached = research_cache.get_full_exam_research(class_slug)
    if cached:
        print(f"  [Research] ✅ Full-exam cache hit: {class_slug}")
        return cached

    prompt = (
        f"You are a senior {exam_name} exam expert.\n"
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
    print(f"  [Research] Fetching full-exam: {class_slug}...")
    result = gemini_client.research(prompt, max_tokens=2000)
    if result:
        research_cache.store_full_exam_research(class_slug, result)
        print(f"  [Research] Cached {len(result)} chars for {class_slug}")
    return result or "Standard exam. Cover all subjects equally."


# ── Step 2: Generate one 10Q chunk (round-robin key selection) ────────────────

def _generate_one_chunk(count, exam_name, subject_name, research_ctx,
                         topic_str, chunk_index):
    """
    Generate exactly `count` questions (≤10) as raw text.
    Uses chunk_index to rotate across Gemini keys — spreads RPM/RPD load.
    Returns raw text string or empty string on failure.
    """
    prompt = (
        f"You are an expert question setter for the {exam_name} exam.\n\n"
        f"EXAM RESEARCH (use this to set correct difficulty and focus):\n"
        f"{research_ctx}\n\n"
        f"TASK: Generate exactly {count} high-quality MCQ questions.\n"
        f"Subject: {subject_name}\n"
        f"Topic/Chapter: {topic_str}\n\n"
        f"Requirements:\n"
        f"- Difficulty: 30% easy, 45% medium, 25% hard\n"
        f"- Each question must have a clear, unambiguous stem\n"
        f"- 4 options (A/B/C/D) — only ONE is correct\n"
        f"- Mark the correct answer with an asterisk: *A) or *B) etc.\n"
        f"- After each question, write: EXPLANATION: <why correct answer is right>\n"
        f"- Questions must be exam-standard, not trivial\n"
        f"- For numerical questions, show the calculation step-by-step\n"
        f"- Vary question types: direct recall, application, clinical/practical\n\n"
        f"Format: number each question 1. 2. 3. etc.\n"
        f"Do NOT output JSON. Write in plain text."
    )
    try:
        raw = gemini_client.generate_questions_for_chunk(
            prompt, chunk_index=chunk_index, max_tokens=4096
        )
        if raw and len(raw) > 80:
            return raw
        print(f"    [Generate] Response too short — skipping chunk")
    except GeminiError as e:
        print(f"    [Generate] All keys failed: {e}")
    return ""


# ── Step 3: Structure one chunk into JSON ─────────────────────────────────────

def _structure_one_chunk(raw_text, exam_name, subject_name):
    """
    Structure raw text (one 10Q chunk) into JSON list.
    Groq K1 → Groq K2 → Flash K1 → Flash K2.
    Returns list of question dicts (may be empty on total failure).
    """
    try:
        qs = groq_client.format_questions(raw_text, _SCHEMA, exam_name, subject_name)
        if isinstance(qs, list) and qs:
            return qs
        if isinstance(qs, dict):
            return [qs]
    except GroqError as e:
        print(f"    [Structure] Groq failed ({e}) — trying Flash fallback...")

    # Flash fallback
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


# ── Core iterator: generate until target reached ──────────────────────────────

def _iterate_to_target(target_count, exam_name, subject_name,
                        research_ctx, topic_str):
    """
    Iterate 10Q chunks until `target_count` questions are collected.
    Each chunk uses the next Gemini key in rotation.

    target_count can be 10, 20, 30, 40, 50 (subject) or any number (full-length).
    Stops when target reached OR when a chunk fails (saves what we have).
    Returns list of raw question dicts.
    """
    all_qs    = []
    collected = 0
    chunk_idx = 0
    max_chunks = (target_count // CHUNK) + 2  # allow 2 extra attempts for partial chunks

    print(f"    Target: {target_count}Q in {CHUNK}Q chunks "
          f"({(target_count + CHUNK - 1) // CHUNK} chunks needed)")

    while collected < target_count and chunk_idx < max_chunks:
        remaining = target_count - collected
        cnt       = min(remaining, CHUNK)
        chunk_num = chunk_idx + 1
        total_needed = (target_count + CHUNK - 1) // CHUNK

        print(f"    ── Chunk {chunk_num}/{total_needed} ({cnt}Q, "
              f"key rotation slot {chunk_idx % 3}) ──")

        # Step 2: Generate
        raw = _generate_one_chunk(
            cnt, exam_name, subject_name, research_ctx, topic_str, chunk_idx
        )
        if not raw:
            print(f"    Chunk {chunk_num} generation failed — stopping iteration")
            break

        # Step 3: Structure
        qs = _structure_one_chunk(raw, exam_name, subject_name)
        if qs:
            all_qs.extend(qs)
            collected += len(qs)
            print(f"    Chunk {chunk_num} → {len(qs)}Q extracted "
                  f"(total so far: {collected}/{target_count})")
        else:
            print(f"    Chunk {chunk_num} structuring failed — stopping iteration")
            break

        chunk_idx += 1

    print(f"    Iteration complete: {len(all_qs)}/{target_count}Q collected")
    return all_qs


# ── Full-length test builder ──────────────────────────────────────────────────

def build_full_length_test(class_slug):
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
    exam_research = _get_full_exam_research(class_slug, exam_name, subjects_summary)

    print(f"\n  [Full-Length] {title}")
    print(f"  Subjects: {subjects_summary}")
    print(f"  Target: {total_q}Q | Chunk size: {CHUNK}Q\n")

    all_questions = []

    for subj in subjects_data:
        subj_slug = subj["subjectSlug"]
        subj_name = subj["subjectName"]
        q_count   = subj["questionCount"]
        chapters  = subj.get("chapters", [])
        ch_str    = ", ".join(c["name"] for c in chapters[:6]) if chapters else "General Topics"

        print(f"\n  [{subj_name}] Target: {q_count}Q")

        # Full research per subject (cached after first fetch)
        subj_research = _get_research(class_slug, subj_slug, subj_name, ch_str, exam_name)
        # Combine exam-level + subject-level research for maximum context
        combined_ctx = (
            f"EXAM OVERVIEW:\n{exam_research}\n\n"
            f"SUBJECT DETAIL:\n{subj_research}"
        ).strip()

        raw_qs = _iterate_to_target(
            q_count, exam_name, subj_name, combined_ctx, ch_str
        )
        for raw_q in raw_qs[:q_count]:
            idx = len(all_questions)
            all_questions.append(_validate_q(raw_q, idx, mark_val, neg_val, neg, subj_slug))

    # Accept if we got at least 5 questions
    if len(all_questions) < 5:
        raise Exception(
            f"Too few questions: {len(all_questions)}/{total_q}. "
            "Check API keys and quotas."
        )

    neg_instr = f"Wrong answer: -{neg_val} mark" if neg else "No negative marking"
    payload = {
        "id": uid, "title": title,
        "description": f"Full-length mock test for {exam_name}. {len(all_questions)} questions.",
        "board": board, "classSlug": class_slug, "classNumber": 0,
        "subjects": [s["subjectSlug"] for s in subjects_data],
        "subject": "all", "chapter": "Full Syllabus", "topic": "Full Length",
        "testType": "full-length",
        "duration": duration, "totalMarks": total_m,
        "passingMarks": round(total_m * 0.35),
        "totalQuestions": len(all_questions),
        "isPublic": True, "isPublished": True, "isAdmin": True,
        "showCorrectAnswers": True, "randomizeQuestions": True,
        "unlimitedAttempts": True, "allowedAttempts": 0,
        "tags": [class_slug, "full-length", "mock-test", "all-subjects"],
        "instructions": [
            f"Total Questions: {len(all_questions)}",
            f"Total Marks: {total_m}",
            f"Duration: {duration} minutes",
            f"Correct answer: +{mark_val} mark",
            neg_instr,
        ],
        "createdBy": "admin", "createdByName": "EduTest Admin",
        "publishedAt": _now(), "questions": all_questions,
    }
    path = _admin_path("tests", uid, title, class_slug)
    _save_and_publish(path, payload)

    entry = {
        "id": uid, "type": "test", "title": title,
        "description": payload["description"],
        "subjects": payload["subjects"], "subject": "all",
        "board": board, "classSlug": class_slug, "classNumber": 0,
        "chapter": "Full Syllabus", "testType": "full-length",
        "totalQuestions": len(all_questions),
        "duration": duration, "totalMarks": total_m,
        "author": "EduTest Admin", "authorId": "admin", "isAdmin": True,
        "tags": payload["tags"], "cdnUrl": _cdn(path), "path": path,
        "publishedAt": payload["publishedAt"],
    }
    return uid, path, entry


# ── Subject-wise test builder ─────────────────────────────────────────────────

def build_subject_test(topic_info):
    class_slug = topic_info["classSlug"]
    board      = topic_info["board"]
    subj_slug  = topic_info["subjectSlug"]
    subj_name  = topic_info["subjectName"]
    chapter    = topic_info["chapter"]
    topic      = topic_info["topic"]
    exam_name  = _exam_name(class_slug)
    cfg        = topic_info.get("examConfig", {})

    # Use the exam's configured question count (10, 20, 30, 40, 50 — whatever the exam defines)
    q_count  = cfg.get("subjectWiseQuestions", 20)
    duration = cfg.get("subjectWiseDuration", 45)
    mark_val = cfg.get("correctMarkValue", 1)
    neg      = cfg.get("negativeMarking", False)
    neg_val  = cfg.get("negativeMarkValue", 0)

    uid   = _make_id()
    title = f"{exam_name} — {subj_name} | {topic}"

    print(f"\n  [Subject Test] {title}")
    print(f"  Target: {q_count}Q in {CHUNK}Q chunks")

    # Full research (cached)
    chapters_str = chapter
    research = _get_research(class_slug, subj_slug, subj_name, chapters_str, exam_name)

    # Iterate chunks until q_count reached
    raw_qs = _iterate_to_target(
        q_count, exam_name, subj_name, research, f"{chapter} — {topic}"
    )

    # Accept if we got at least 5 questions
    if len(raw_qs) < 5:
        raise Exception(f"Too few questions: {len(raw_qs)}/{q_count}")

    questions = [
        _validate_q(q, i, mark_val, neg_val, neg, subj_slug)
        for i, q in enumerate(raw_qs[:q_count])
    ]

    payload = {
        "id": uid, "title": title,
        "description": f"Subject-wise test on {topic} ({subj_name}) for {exam_name}. {len(questions)}Q.",
        "board": board, "classSlug": class_slug, "classNumber": 0,
        "subjects": [subj_slug], "subject": subj_slug,
        "chapter": chapter, "topic": topic,
        "testType": "subject-wise",
        "duration": duration,
        "totalMarks": len(questions) * mark_val,
        "passingMarks": round(len(questions) * mark_val * 0.4),
        "totalQuestions": len(questions),
        "isPublic": True, "isPublished": True, "isAdmin": True,
        "showCorrectAnswers": True, "randomizeQuestions": False,
        "unlimitedAttempts": True, "allowedAttempts": 0,
        "tags": _tags(class_slug, subj_slug, chapter, topic),
        "instructions": [
            f"Subject: {subj_name}", f"Topic: {topic}",
            f"Duration: {duration} mins",
            f"+{mark_val} per correct answer",
        ],
        "createdBy": "admin", "createdByName": "EduTest Admin",
        "publishedAt": _now(), "questions": questions,
    }
    path = _admin_path("tests", uid, title, class_slug)
    _save_and_publish(path, payload)

    entry = {
        "id": uid, "type": "test", "title": title,
        "description": payload["description"],
        "subjects": [subj_slug], "subject": subj_slug,
        "board": board, "classSlug": class_slug, "classNumber": 0,
        "chapter": chapter, "topic": topic, "testType": "subject-wise",
        "totalQuestions": len(questions),
        "duration": duration, "totalMarks": payload["totalMarks"],
        "author": "EduTest Admin", "authorId": "admin", "isAdmin": True,
        "tags": payload["tags"], "cdnUrl": _cdn(path), "path": path,
        "publishedAt": payload["publishedAt"],
    }
    return uid, path, entry


# ── Combined test builder ─────────────────────────────────────────────────────

def build_combined_test(class_slug, subject_topics):
    if not subject_topics:
        raise ValueError("No subjects provided for combined test")

    topics_data = topic_picker.load_topics(class_slug)
    cfg         = (topics_data or {}).get("examConfig", {})
    board       = (topics_data or {}).get("board", "Competitive")
    exam_name   = _exam_name(class_slug)

    # Use exam's configured per-subject count (no artificial cap)
    q_per_subj = cfg.get("subjectWiseQuestions", 20)
    duration   = cfg.get("subjectWiseDuration", 45) * len(subject_topics)
    mark_val   = cfg.get("correctMarkValue", 1)
    neg        = cfg.get("negativeMarking", False)
    neg_val    = cfg.get("negativeMarkValue", 0)

    uid        = _make_id()
    subj_names = " + ".join(t["subjectName"] for t in subject_topics)
    title      = f"{exam_name} — Combined: {subj_names}"
    total_q    = q_per_subj * len(subject_topics)

    print(f"\n  [Combined Test] {title}")
    print(f"  Target: {total_q}Q ({q_per_subj}Q × {len(subject_topics)} subjects)")

    all_questions  = []
    all_subj_slugs = []

    for ti in subject_topics:
        subj_slug = ti["subjectSlug"]
        subj_name = ti["subjectName"]
        chapter   = ti["chapter"]
        topic     = ti["topic"]
        all_subj_slugs.append(subj_slug)

        print(f"\n  [{subj_name}] Target: {q_per_subj}Q on {topic}")
        research = _get_research(class_slug, subj_slug, subj_name, chapter, exam_name)

        raw_qs = _iterate_to_target(
            q_per_subj, exam_name, subj_name, research, f"{chapter} — {topic}"
        )
        for raw_q in raw_qs[:q_per_subj]:
            idx = len(all_questions)
            all_questions.append(_validate_q(raw_q, idx, mark_val, neg_val, neg, subj_slug))

    if len(all_questions) < 5:
        raise Exception(f"Too few questions: {len(all_questions)}/{total_q}")

    payload = {
        "id": uid, "title": title,
        "description": f"Combined test: {subj_names} for {exam_name}. {len(all_questions)}Q.",
        "board": board, "classSlug": class_slug, "classNumber": 0,
        "subjects": all_subj_slugs, "subject": all_subj_slugs[0],
        "chapter": "Multiple Chapters", "topic": "Combined",
        "testType": "subject-wise",
        "duration": min(duration, 90),
        "totalMarks": len(all_questions) * mark_val,
        "passingMarks": round(len(all_questions) * mark_val * 0.4),
        "totalQuestions": len(all_questions),
        "isPublic": True, "isPublished": True, "isAdmin": True,
        "showCorrectAnswers": True, "randomizeQuestions": False,
        "unlimitedAttempts": True, "allowedAttempts": 0,
        "tags": _tags(class_slug, "+".join(all_subj_slugs), subj_names),
        "instructions": [
            f"Subjects: {subj_names}",
            f"Duration: {min(duration, 90)} mins",
            f"+{mark_val} per correct answer",
        ],
        "createdBy": "admin", "createdByName": "EduTest Admin",
        "publishedAt": _now(), "questions": all_questions,
    }
    path = _admin_path("tests", uid, title, class_slug)
    _save_and_publish(path, payload)

    entry = {
        "id": uid, "type": "test", "title": title,
        "description": payload["description"],
        "subjects": all_subj_slugs, "subject": all_subj_slugs[0],
        "board": board, "classSlug": class_slug, "classNumber": 0,
        "chapter": "Multiple Chapters", "testType": "subject-wise",
        "totalQuestions": len(all_questions),
        "duration": payload["duration"], "totalMarks": payload["totalMarks"],
        "author": "EduTest Admin", "authorId": "admin", "isAdmin": True,
        "tags": payload["tags"], "cdnUrl": _cdn(path), "path": path,
        "publishedAt": payload["publishedAt"],
    }
    return uid, path, entry

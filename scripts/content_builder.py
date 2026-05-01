"""
content_builder.py — TEST-ONLY generation pipeline.

Two test types per run (decided by generate.py):
  full     → 1 full-length test covering ALL subjects (exact question count from examConfig)
  subject  → 1 subject-wise OR combined (2-3 subjects) test

Pipeline per test:
  Step 1 — Research   : Flash K1 → Flash K2 → cached → empty (never crashes)
  Step 2 — Generate   : Pro K2 → Flash K2 → Flash K1 → Flash K2 (raw text)
  Step 3 — Structure  : Groq K1 → Groq K2 → Flash K1 → Flash K2 (JSON)

Research is cached in data/research/<class_slug>.json (30-day TTL).
If the same exam is picked again, cached research is reused — no extra API call.
"""
import os, json, uuid, re, random
from datetime import datetime, timezone

import gemini_client
import groq_client
import research_cache
from gemini_client import GeminiError
from groq_client   import GroqError
import topic_picker

ROOT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CDN_BASE = "https://prepguidedata.launchyourconcept.com"

# ── Shared JSON schema for the structurer ─────────────────────────────────────
_SCHEMA = """[
  {
    "question": "Full question text here?",
    "difficulty": "medium",
    "explanation": "Detailed explanation of why the correct answer is right",
    "hint": "Optional short hint",
    "options": [
      {"letter": "A", "text": "Option text", "isCorrect": true,  "explanation": "Why A is correct"},
      {"letter": "B", "text": "Option text", "isCorrect": false, "explanation": "Why B is wrong"},
      {"letter": "C", "text": "Option text", "isCorrect": false, "explanation": "Why C is wrong"},
      {"letter": "D", "text": "Option text", "isCorrect": false, "explanation": "Why D is wrong"}
    ]
  }
]"""


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
    """Normalise a raw question dict into the app's expected schema."""
    qid = f"q-{int(datetime.now().timestamp()*1000)}-{uuid.uuid4().hex[:5]}"

    options = raw.get("options", [])
    validated = []
    for i, opt in enumerate(options[:4]):
        validated.append({
            "letter":      opt.get("letter") or chr(65 + i),
            "text":        str(opt.get("text", f"Option {chr(65+i)}")).strip(),
            "isCorrect":   bool(opt.get("isCorrect", False)),
            "index":       i,
            "imageUrl":    str(opt.get("imageUrl", "")).strip(),
            "explanation": str(opt.get("explanation", "")).strip(),
        })
    # Pad to 4 options
    while len(validated) < 4:
        i = len(validated)
        validated.append({
            "letter": chr(65+i), "text": f"Option {chr(65+i)}",
            "isCorrect": False, "index": i, "imageUrl": "", "explanation": "",
        })

    # Ensure exactly one correct answer
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
        "type":        "mcq",
        "options":     validated,
        "answer":      "", "alternateAnswers": [],
        "explanation": str(raw.get("explanation", "")).strip(),
        "hint":        str(raw.get("hint", "")).strip(),
        "marks":       marks,
        "negativeMark":    neg_val if neg_marking else 0,
        "negativeMarking": neg_marking,
        "imageUrl":    str(raw.get("imageUrl", "")).strip(),
        "subject":     subj_slug,
        "difficulty":  diff,
        "hasError":    False,
    }


# ── Step 1: Research (with cache) ─────────────────────────────────────────────

def _get_research(class_slug, subject_slug, subject_name, chapters_str, exam_name):
    """
    Return research text for a subject.
    Cache hit → return immediately (no API call).
    Cache miss → call Flash K1 → Flash K2 → store → return.
    """
    cached = research_cache.get_subject_research(class_slug, subject_slug, chapters_str)
    if cached:
        print(f"  [Research] ✅ Cache hit for {subject_slug}")
        return cached

    prompt = (
        f"You are a senior {exam_name} exam expert.\n"
        f"Subject: {subject_name}\n"
        f"Key chapters: {chapters_str}\n\n"
        "Provide a focused 200-word analysis covering:\n"
        "1. Most-tested question patterns and formats\n"
        "2. Difficulty distribution (Easy/Medium/Hard %)\n"
        "3. Top 6 high-yield topics students must master\n"
        "4. Common wrong-answer traps and misconceptions\n"
        "5. Any numerical/formula-heavy areas to watch\n"
        "Be specific and exam-focused. No generic advice."
    )
    print(f"  [Research] Fetching for {subject_slug}...")
    result = gemini_client.research(prompt, max_tokens=1500)
    if result:
        research_cache.store_subject_research(class_slug, subject_slug, result, chapters_str)
    return result or "Standard exam difficulty. Focus on core concepts."


def _get_full_exam_research(class_slug, exam_name, subjects_summary):
    """Research for full-length tests — cached at exam level."""
    cached = research_cache.get_full_exam_research(class_slug)
    if cached:
        print(f"  [Research] ✅ Full-exam cache hit for {class_slug}")
        return cached

    prompt = (
        f"You are a senior {exam_name} exam expert.\n"
        f"Subjects covered: {subjects_summary}\n\n"
        "Provide a 250-word full-exam analysis:\n"
        "1. Overall difficulty profile and marking scheme\n"
        "2. Subject-wise weightage and question distribution\n"
        "3. Top 3 high-yield topics per subject\n"
        "4. Time management strategy for the full paper\n"
        "5. Most common traps across all subjects\n"
        "Be specific to this exam. No generic advice."
    )
    print(f"  [Research] Fetching full-exam research for {class_slug}...")
    result = gemini_client.research(prompt, max_tokens=2000)
    if result:
        research_cache.store_full_exam_research(class_slug, result)
    return result or "Standard exam. Cover all subjects equally."


# ── Step 2+3: Generate + Structure (with full fallback) ───────────────────────

def _generate_and_structure(count, exam_name, subject_name, research_ctx,
                             chapters_str, subj_slug, class_slug):
    """
    Generate `count` questions for one subject chunk.
    Step 2: Gemini generates raw text.
    Step 3: Groq K1 → Groq K2 → Flash structures into JSON.
    Returns list of raw question dicts (may be shorter than count if some fail).
    """
    gen_prompt = (
        f"EXAM RESEARCH CONTEXT:\n{research_ctx}\n\n"
        f"TASK: Generate exactly {count} high-quality MCQ questions for the "
        f"{exam_name} exam.\n"
        f"Subject: {subject_name}\n"
        f"Chapters/Topics: {chapters_str}\n\n"
        "Requirements:\n"
        "- Difficulty: 25% easy, 50% medium, 25% hard\n"
        "- Each question: clear stem, 4 options (A/B/C/D), mark the ONE correct answer\n"
        "- Include a detailed explanation for the correct answer\n"
        "- Include a brief note on why each wrong option is incorrect\n"
        "- Questions must be exam-standard, not trivial\n"
        "- For numerical questions, show the calculation in the explanation\n\n"
        "Output format: plain text, numbered 1 to {count}. "
        "Do NOT output JSON. Write naturally."
    ).replace("{count}", str(count))

    print(f"    [Step 2: Generate] {count}Q for {subject_name}...")
    try:
        raw_text = gemini_client.generate_questions(gen_prompt, max_tokens=8192)
    except GeminiError as e:
        print(f"    [Step 2] Generation failed: {e}")
        return []

    if not raw_text or len(raw_text) < 100:
        print("    [Step 2] Response too short — skipping chunk")
        return []

    # Step 3: Structure with Groq, fall back to Flash
    struct_prompt = (
        f"Extract ALL questions from the text below and output a JSON array.\n"
        f"Schema for each question:\n{_SCHEMA}\n"
        f"Rules:\n"
        f"- Output ONLY the JSON array starting with [ and ending with ]\n"
        f"- Include ALL {count} questions\n"
        f"- Mark exactly ONE option as isCorrect: true per question\n"
        f"- difficulty must be 'easy', 'medium', or 'hard'\n\n"
        f"TEXT TO EXTRACT FROM:\n{raw_text}\n["
    )

    print(f"    [Step 3: Structure] Extracting JSON...")
    try:
        questions = groq_client.format_questions(raw_text, _SCHEMA, exam_name, subject_name)
        if isinstance(questions, list) and questions:
            return questions
        if isinstance(questions, dict):
            return [questions]
    except GroqError as e:
        print(f"    [Step 3] Both Groq keys failed ({e}) — falling back to Flash...")

    # Flash fallback for structuring
    try:
        questions = gemini_client.structure_with_flash(struct_prompt, max_tokens=6144)
        if isinstance(questions, list) and questions:
            print(f"    [Step 3] ✅ Flash fallback succeeded")
            return questions
        if isinstance(questions, dict):
            return [questions]
    except GeminiError as e:
        print(f"    [Step 3] Flash fallback also failed: {e}")

    return []


# ── Full-length test builder ──────────────────────────────────────────────────

def build_full_length_test(class_slug):
    """
    Full-length test covering ALL subjects.
    Question count comes from examConfig.fullLengthQuestions.
    Uses Flash-only for research (Pro saved for subject tests).
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

    # Full-exam research (cached)
    subjects_summary = ", ".join(s["subjectName"] for s in subjects_data)
    exam_research = _get_full_exam_research(class_slug, exam_name, subjects_summary)

    print(f"\n  [Full-Length] {title}")
    print(f"  Subjects: {subjects_summary}")
    print(f"  Target: {total_q}Q across {len(subjects_data)} subjects\n")

    all_questions = []
    CHUNK = 20   # Flash handles 20Q per call comfortably

    for subj in subjects_data:
        subj_slug  = subj["subjectSlug"]
        subj_name  = subj["subjectName"]
        q_count    = subj["questionCount"]
        chapters   = subj.get("chapters", [])
        ch_str     = ", ".join(c["name"] for c in chapters[:6]) if chapters else "General Topics"

        print(f"  [{subj_name}] {q_count}Q")

        # Subject-level research (cached, reuses full-exam context if subject cache empty)
        subj_research = _get_research(class_slug, subj_slug, subj_name, ch_str, exam_name)
        combined_ctx  = f"{exam_research}\n\nSubject-specific:\n{subj_research}".strip()

        # Generate in chunks
        chunks = [CHUNK] * (q_count // CHUNK)
        if q_count % CHUNK:
            chunks.append(q_count % CHUNK)

        subj_qs = []
        for i, cnt in enumerate(chunks, 1):
            print(f"    Chunk {i}/{len(chunks)} ({cnt}Q):")
            chunk = _generate_and_structure(
                cnt, exam_name, subj_name, combined_ctx, ch_str, subj_slug, class_slug
            )
            subj_qs.extend(chunk)
            if len(subj_qs) >= q_count:
                break

        # Validate and add
        for raw_q in subj_qs[:q_count]:
            idx = len(all_questions)
            all_questions.append(_validate_q(raw_q, idx, mark_val, neg_val, neg, subj_slug))

    if len(all_questions) < max(10, total_q // 4):
        raise Exception(
            f"Too few questions generated: {len(all_questions)}/{total_q}. "
            "Check API keys and quotas."
        )

    neg_instr = f"Wrong answer: -{neg_val} mark" if neg else "No negative marking"
    payload = {
        "id": uid, "title": title,
        "description": (
            f"Full-length mock test for {exam_name}. "
            f"{len(all_questions)} questions across all subjects."
        ),
        "board": board, "classSlug": class_slug, "classNumber": 0,
        "subjects": [s["subjectSlug"] for s in subjects_data],
        "subject": "all", "chapter": "Full Syllabus", "topic": "Full Length",
        "testType": "full-length",
        "duration": duration,
        "totalMarks": total_m,
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
    _save(path, payload)

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


# ── Subject-wise / Combined test builder ─────────────────────────────────────

def build_subject_test(topic_info):
    """
    Subject-wise test: one topic from one subject.
    Uses the full pipeline: Pro K2 → Flash fallback → Groq structure.
    Question count from examConfig.subjectWiseQuestions (default 20).
    """
    class_slug = topic_info["classSlug"]
    board      = topic_info["board"]
    subj_slug  = topic_info["subjectSlug"]
    subj_name  = topic_info["subjectName"]
    chapter    = topic_info["chapter"]
    topic      = topic_info["topic"]
    exam_name  = _exam_name(class_slug)
    cfg        = topic_info.get("examConfig", {})

    q_count  = cfg.get("subjectWiseQuestions", 20)
    duration = cfg.get("subjectWiseDuration", 45)
    mark_val = cfg.get("correctMarkValue", 1)
    neg      = cfg.get("negativeMarking", False)
    neg_val  = cfg.get("negativeMarkValue", 0)

    uid   = _make_id()
    title = f"{exam_name} — {subj_name} | {topic}"

    print(f"\n  [Subject Test] {title} ({q_count}Q)")

    # Research (cached)
    ch_str   = chapter
    research = _get_research(class_slug, subj_slug, subj_name, ch_str, exam_name)

    # Generate all questions (single chunk for subject tests)
    raw_qs = _generate_and_structure(
        q_count, exam_name, subj_name, research, f"{chapter} — {topic}", subj_slug, class_slug
    )

    if len(raw_qs) < max(3, q_count // 4):
        raise Exception(f"Too few questions: {len(raw_qs)}/{q_count}")

    questions = [
        _validate_q(q, i, mark_val, neg_val, neg, subj_slug)
        for i, q in enumerate(raw_qs[:q_count])
    ]

    payload = {
        "id": uid, "title": title,
        "description": f"Subject-wise test on {topic} ({subj_name}) for {exam_name}. {len(questions)} questions.",
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
    _save(path, payload)

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


def build_combined_test(class_slug, subject_topics):
    """
    Combined test: 2-3 subjects, each contributing questions proportionally.
    subject_topics: list of topic_info dicts (one per subject).
    Total questions from examConfig.subjectWiseQuestions * num_subjects (capped at 40).
    """
    if not subject_topics:
        raise ValueError("No subjects provided for combined test")

    topics_data = topic_picker.load_topics(class_slug)
    cfg         = (topics_data or {}).get("examConfig", {})
    board       = (topics_data or {}).get("board", "Competitive")
    exam_name   = _exam_name(class_slug)

    per_subj_q = cfg.get("subjectWiseQuestions", 20)
    total_q    = min(per_subj_q * len(subject_topics), 40)
    q_per_subj = total_q // len(subject_topics)
    duration   = cfg.get("subjectWiseDuration", 45) * len(subject_topics)
    mark_val   = cfg.get("correctMarkValue", 1)
    neg        = cfg.get("negativeMarking", False)
    neg_val    = cfg.get("negativeMarkValue", 0)

    uid         = _make_id()
    subj_names  = " + ".join(t["subjectName"] for t in subject_topics)
    title       = f"{exam_name} — Combined: {subj_names}"

    print(f"\n  [Combined Test] {title} ({total_q}Q across {len(subject_topics)} subjects)")

    all_questions = []
    all_subj_slugs = []

    for ti in subject_topics:
        subj_slug = ti["subjectSlug"]
        subj_name = ti["subjectName"]
        chapter   = ti["chapter"]
        topic     = ti["topic"]
        ch_str    = chapter
        all_subj_slugs.append(subj_slug)

        print(f"  [{subj_name}] {q_per_subj}Q on {topic}")
        research = _get_research(class_slug, subj_slug, subj_name, ch_str, exam_name)

        raw_qs = _generate_and_structure(
            q_per_subj, exam_name, subj_name, research,
            f"{chapter} — {topic}", subj_slug, class_slug
        )
        for raw_q in raw_qs[:q_per_subj]:
            idx = len(all_questions)
            all_questions.append(_validate_q(raw_q, idx, mark_val, neg_val, neg, subj_slug))

    if len(all_questions) < max(5, total_q // 4):
        raise Exception(f"Too few questions: {len(all_questions)}/{total_q}")

    payload = {
        "id": uid, "title": title,
        "description": (
            f"Combined test covering {subj_names} for {exam_name}. "
            f"{len(all_questions)} questions."
        ),
        "board": board, "classSlug": class_slug, "classNumber": 0,
        "subjects": all_subj_slugs, "subject": all_subj_slugs[0],
        "chapter": "Multiple Chapters", "topic": "Combined",
        "testType": "subject-wise",
        "duration": min(duration, 120),
        "totalMarks": len(all_questions) * mark_val,
        "passingMarks": round(len(all_questions) * mark_val * 0.4),
        "totalQuestions": len(all_questions),
        "isPublic": True, "isPublished": True, "isAdmin": True,
        "showCorrectAnswers": True, "randomizeQuestions": False,
        "unlimitedAttempts": True, "allowedAttempts": 0,
        "tags": _tags(class_slug, "+".join(all_subj_slugs), subj_names),
        "instructions": [
            f"Subjects: {subj_names}",
            f"Duration: {min(duration, 120)} mins",
            f"+{mark_val} per correct answer",
        ],
        "createdBy": "admin", "createdByName": "EduTest Admin",
        "publishedAt": _now(), "questions": all_questions,
    }
    path = _admin_path("tests", uid, title, class_slug)
    _save(path, payload)

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

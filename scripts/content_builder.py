"""
content_builder.py — Builds test/note/formula JSON payloads using Gemini.

Model usage:
  gemini-2.5-flash → full-length tests (100Q), long notes (1000+ words)
  gemini-2.5-pro   → formula sheets, short/topic tests (25-30Q), subject tests (40Q)

All output validated against exact schema the EduTest website expects.
"""
import os, json, uuid, re
from datetime import datetime, timezone

import gemini_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CDN_BASE = "https://prepguidedata.launchyourconcept.com"

# ── Helpers ───────────────────────────────────────────────────────────────────

def make_id():
    return str(uuid.uuid4())

def make_slug(title, uid):
    slug = re.sub(r"[^a-z0-9\s-]", "", title.lower())
    slug = re.sub(r"\s+", "-", slug.strip())
    slug = re.sub(r"-+", "-", slug)[:50]
    short_id = uid.split("-")[0]
    return f"{slug}-{short_id}"

def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def admin_test_path(uid, title):
    return f"data/admin/tests/{make_slug(title, uid)}.json"

def admin_note_path(uid, title):
    return f"data/admin/notes/{make_slug(title, uid)}.json"

def admin_formula_path(uid, title):
    return f"data/admin/formulas/{make_slug(title, uid)}.json"

def save_file(path, data):
    full = os.path.join(ROOT, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  [Builder] Saved {path}")

def cdn_url(path):
    return f"{CDN_BASE}/{path}"

def make_tags(class_slug, subj_slug, chapter, topic):
    """Generate clean, relevant tags."""
    tags = set()
    tags.add(class_slug)
    tags.add(subj_slug)
    # Add chapter words (skip short words)
    for word in chapter.lower().split():
        if len(word) > 3:
            tags.add(word)
    # Add topic words
    for word in topic.lower().split():
        if len(word) > 3:
            tags.add(word.replace("'", ""))
    return list(tags)[:8]

# ── Question validator ────────────────────────────────────────────────────────

def validate_question(q, idx, marks_per_q, negative_mark, negative_marking, subject_slug):
    """Ensure question matches exact schema the EduTest website expects."""
    qid = f"q-{int(datetime.now().timestamp()*1000)}-{uuid.uuid4().hex[:5]}"
    options = q.get("options", [])

    # Build exactly 4 validated options
    validated_opts = []
    for i, opt in enumerate(options[:4]):
        letter = opt.get("letter") or chr(65 + i)
        validated_opts.append({
            "letter": letter,
            "text": str(opt.get("text", f"Option {letter}")).strip(),
            "isCorrect": bool(opt.get("isCorrect", False)),
            "index": i,
            "imageUrl": "",
            "explanation": str(opt.get("explanation", "")).strip()
        })
    # Pad to 4 if fewer options returned
    while len(validated_opts) < 4:
        i = len(validated_opts)
        validated_opts.append({
            "letter": chr(65 + i), "text": f"Option {chr(65+i)}",
            "isCorrect": False, "index": i, "imageUrl": "", "explanation": ""
        })

    # Enforce exactly ONE correct answer for MCQ
    correct_count = sum(1 for o in validated_opts if o["isCorrect"])
    if correct_count == 0:
        validated_opts[0]["isCorrect"] = True
    elif correct_count > 1:
        first_correct = next(i for i, o in enumerate(validated_opts) if o["isCorrect"])
        for i, o in enumerate(validated_opts):
            o["isCorrect"] = (i == first_correct)

    difficulty = q.get("difficulty", "medium")
    if difficulty not in ("easy", "medium", "hard"):
        difficulty = "medium"

    question_text = str(q.get("question", "")).strip()
    if not question_text:
        question_text = f"Question {idx + 1}"

    return {
        "id": qid,
        "order": idx + 1,
        "question": question_text,
        "type": "mcq",
        "options": validated_opts,
        "answer": "",
        "alternateAnswers": [],
        "explanation": str(q.get("explanation", "")).strip(),
        "hint": str(q.get("hint", "")).strip(),
        "marks": marks_per_q,
        "negativeMark": negative_mark,
        "negativeMarking": negative_marking,
        "imageUrl": "",
        "subject": subject_slug,
        "difficulty": difficulty,
        "hasError": False
    }

# ── Shared question prompt template ──────────────────────────────────────────

def _question_prompt(exam_name, subj_name, chapter, topic, q_count, context=""):
    return f"""You are a senior {exam_name} exam question setter with 10+ years experience.
Generate exactly {q_count} high-quality MCQ questions.

Exam: {exam_name}
Subject: {subj_name}
Chapter: {chapter}
Topic: {topic}
{context}

STRICT RULES:
1. Every question must be directly about: {topic}
2. Exactly 4 options per question (A, B, C, D)
3. Exactly ONE option must have "isCorrect": true
4. Every option must have a clear explanation (1-2 sentences) — why correct or why wrong
5. Difficulty mix: first 30% easy, middle 50% medium, last 20% hard
6. Questions must be clinically/factually accurate and exam-standard
7. No repeated or similar questions
8. No question numbering in the question text
9. Return ONLY a valid JSON array — no markdown, no extra text, no explanation outside JSON

Required JSON format for each question:
{{
  "question": "Complete question text ending with ?",
  "difficulty": "easy",
  "explanation": "The correct answer is X because...",
  "hint": "Think about...",
  "options": [
    {{"letter": "A", "text": "Option text", "isCorrect": false, "explanation": "This is wrong because..."}},
    {{"letter": "B", "text": "Option text", "isCorrect": true,  "explanation": "This is correct because..."}},
    {{"letter": "C", "text": "Option text", "isCorrect": false, "explanation": "This is wrong because..."}},
    {{"letter": "D", "text": "Option text", "isCorrect": false, "explanation": "This is wrong because..."}}
  ]
}}

Generate exactly {q_count} questions as a JSON array:"""

# ── Full-length test (FLASH model) ────────────────────────────────────────────

def build_full_length_test(class_slug, exam_config, subjects_distribution, board):
    """
    Generate 1 full-length test covering all subjects proportionally.
    Uses gemini-2.5-flash (large context, fast for bulk generation).
    """
    import random
    total_q = exam_config.get("fullLengthQuestions", 100)
    total_marks = exam_config.get("fullLengthMarks", 100)
    duration = exam_config.get("fullLengthDuration", 90)
    negative_marking = exam_config.get("negativeMarking", False)
    correct_mark = exam_config.get("correctMarkValue", 1)
    negative_mark = exam_config.get("negativeMarkValue", 0)
    marks_per_q = correct_mark
    exam_name = class_slug.upper().replace("-", " ")

    uid = make_id()
    month_year = datetime.now().strftime("%B %Y")
    title = f"{exam_name} Full Length Mock Test — {month_year}"

    print(f"\n  [Flash] Building full-length test: {total_q}Q / {total_marks} marks / {duration} min")

    all_questions = []
    q_idx = 0

    for subj_info in subjects_distribution:
        subj_name = subj_info["subjectName"]
        subj_slug = subj_info["subjectSlug"]
        q_count = subj_info["questionCount"]
        chapters = subj_info.get("chapters", [])

        # Pick diverse chapter-topic pairs
        chapter_topics = []
        for ch in chapters:
            for t in ch.get("topics", []):
                chapter_topics.append((ch["name"], t))
        random.shuffle(chapter_topics)
        topics_sample = chapter_topics[:min(8, len(chapter_topics))]
        topics_str = "; ".join([f"{ch}: {t}" for ch, t in topics_sample])

        prompt = _question_prompt(
            exam_name, subj_name,
            "Full Syllabus", subj_name,
            q_count,
            context=f"Cover these topics proportionally: {topics_str}"
        )

        print(f"    Generating {q_count}Q for {subj_name} [Flash]...")
        raw_qs = gemini_client.generate_flash(prompt, expect_json=True, max_tokens=8192)
        if not isinstance(raw_qs, list):
            raw_qs = [raw_qs] if isinstance(raw_qs, dict) else []

        for q in raw_qs[:q_count]:
            validated = validate_question(q, q_idx, marks_per_q, negative_mark, negative_marking, subj_slug)
            all_questions.append(validated)
            q_idx += 1

    all_questions = all_questions[:total_q]

    instructions = [
        f"This test contains {len(all_questions)} questions covering all {exam_name} subjects.",
        f"Each correct answer carries {correct_mark} mark(s).",
    ]
    if negative_marking:
        instructions.append(f"Each wrong answer carries -{negative_mark} mark(s) penalty.")
    else:
        instructions.append("There is NO negative marking.")
    instructions.append("Do not leave the exam window during the test.")
    instructions.append(f"Total duration: {duration} minutes.")

    payload = {
        "id": uid,
        "title": title,
        "description": f"Full length mock test for {exam_name} covering all subjects. {len(all_questions)} questions, {total_marks} marks, {duration} minutes.",
        "board": board,
        "classSlug": class_slug,
        "classNumber": 0,
        "subject": "",
        "chapter": "Full Syllabus",
        "topic": "Full Length",
        "testType": "full-length",
        "duration": duration,
        "totalMarks": total_marks,
        "passingMarks": round(total_marks * 0.4),
        "totalQuestions": len(all_questions),
        "isPublic": True,
        "isPublished": True,
        "showCorrectAnswers": True,
        "randomizeQuestions": True,
        "unlimitedAttempts": True,
        "allowedAttempts": 0,
        "tags": [class_slug, "full-length", "mock-test", exam_name.lower().replace(" ", "-")],
        "instructions": instructions,
        "createdBy": "admin",
        "createdByName": "EduTest Admin",
        "publishedAt": now_iso(),
        "isAdmin": True,
        "questions": all_questions
    }

    path = admin_test_path(uid, title)
    save_file(path, payload)

    index_entry = {
        "id": uid, "type": "test", "title": title,
        "description": payload["description"],
        "subject": "", "board": board, "classSlug": class_slug, "classNumber": 0,
        "chapter": "Full Syllabus", "testType": "full-length",
        "totalQuestions": len(all_questions), "duration": duration, "totalMarks": total_marks,
        "author": "EduTest Admin", "authorId": "admin", "isAdmin": True,
        "tags": payload["tags"], "cdnUrl": cdn_url(path), "path": path,
        "publishedAt": payload["publishedAt"]
    }
    return uid, path, index_entry

# ── Subject/topic-wise test (PRO model) ───────────────────────────────────────

def build_subject_test(topic_info, exam_config):
    """
    Generate 1 subject/chapter/topic-wise test (25-30 questions).
    Uses gemini-2.5-pro for precision and accuracy.
    """
    class_slug = topic_info["classSlug"]
    board = topic_info["board"]
    subj_slug = topic_info["subjectSlug"]
    subj_name = topic_info["subjectName"]
    chapter = topic_info["chapter"]
    topic = topic_info["topic"]

    # Short tests: 25-30 questions (Pro handles this well)
    q_count = 25
    marks_per_q = exam_config.get("correctMarkValue", 1)
    total_marks = q_count * marks_per_q
    duration = 30
    negative_marking = exam_config.get("negativeMarking", False)
    negative_mark = exam_config.get("negativeMarkValue", 0)
    exam_name = class_slug.upper().replace("-", " ")

    uid = make_id()
    title = f"{exam_name} — {subj_name} | {topic}"

    print(f"    Generating {q_count}Q topic test: {topic} [Pro]...")
    prompt = _question_prompt(exam_name, subj_name, chapter, topic, q_count)
    raw_qs = gemini_client.generate_pro(prompt, expect_json=True, max_tokens=4096)
    if not isinstance(raw_qs, list):
        raw_qs = [raw_qs] if isinstance(raw_qs, dict) else []

    questions = []
    for i, q in enumerate(raw_qs[:q_count]):
        validated = validate_question(q, i, marks_per_q, negative_mark, negative_marking, subj_slug)
        questions.append(validated)

    instructions = [
        f"Topic-wise test on: {topic}",
        f"{len(questions)} questions, {duration} minutes.",
        f"Each correct answer: {marks_per_q} mark(s).",
    ]
    if negative_marking:
        instructions.append(f"Negative marking: -{negative_mark} per wrong answer.")

    payload = {
        "id": uid,
        "title": title,
        "description": f"Topic-wise test on {topic} ({chapter}) for {exam_name}. {len(questions)} questions.",
        "board": board, "classSlug": class_slug, "classNumber": 0,
        "subject": subj_slug, "chapter": chapter, "topic": topic,
        "testType": "chapter",
        "duration": duration,
        "totalMarks": len(questions) * marks_per_q,
        "passingMarks": round(len(questions) * marks_per_q * 0.4),
        "totalQuestions": len(questions),
        "isPublic": True, "isPublished": True,
        "showCorrectAnswers": True, "randomizeQuestions": False,
        "unlimitedAttempts": True, "allowedAttempts": 0,
        "tags": make_tags(class_slug, subj_slug, chapter, topic),
        "instructions": instructions,
        "createdBy": "admin", "createdByName": "EduTest Admin",
        "publishedAt": now_iso(), "isAdmin": True,
        "questions": questions
    }

    path = admin_test_path(uid, title)
    save_file(path, payload)

    index_entry = {
        "id": uid, "type": "test", "title": title,
        "description": payload["description"],
        "subject": subj_slug, "board": board, "classSlug": class_slug, "classNumber": 0,
        "chapter": chapter, "testType": "chapter",
        "totalQuestions": len(questions), "duration": duration,
        "totalMarks": payload["totalMarks"],
        "author": "EduTest Admin", "authorId": "admin", "isAdmin": True,
        "tags": payload["tags"], "cdnUrl": cdn_url(path), "path": path,
        "publishedAt": payload["publishedAt"]
    }
    return uid, path, index_entry

# ── Multi-subject test (FLASH model) ─────────────────────────────────────────

def build_multi_subject_test(topics_list, exam_config):
    """
    Generate 1 multi-subject mock test (100Q across multiple subjects).
    Uses gemini-2.5-flash for bulk generation.
    """
    import random
    class_slug = topics_list[0]["classSlug"]
    board = topics_list[0]["board"]
    exam_name = class_slug.upper().replace("-", " ")
    total_q = exam_config.get("multiSubjectTestQuestions", 100)
    marks_per_q = exam_config.get("correctMarkValue", 1)
    total_marks = exam_config.get("multiSubjectTestMarks", 100)
    duration = exam_config.get("multiSubjectTestDuration", 90)
    negative_marking = exam_config.get("negativeMarking", False)
    negative_mark = exam_config.get("negativeMarkValue", 0)

    q_per_topic = max(5, total_q // len(topics_list))
    uid = make_id()
    subjects_covered = list(dict.fromkeys(t["subjectName"] for t in topics_list))
    title = f"{exam_name} — Multi-Subject Mock | {' + '.join(subjects_covered[:3])}"

    print(f"\n  [Flash] Building multi-subject test: {total_q}Q across {len(topics_list)} topics")

    all_questions = []
    q_idx = 0

    for topic_info in topics_list:
        subj_name = topic_info["subjectName"]
        subj_slug = topic_info["subjectSlug"]
        chapter = topic_info["chapter"]
        topic = topic_info["topic"]

        prompt = _question_prompt(exam_name, subj_name, chapter, topic, q_per_topic)
        print(f"    Generating {q_per_topic}Q: {subj_name}/{topic} [Flash]...")
        raw_qs = gemini_client.generate_flash(prompt, expect_json=True, max_tokens=4096)
        if not isinstance(raw_qs, list):
            raw_qs = [raw_qs] if isinstance(raw_qs, dict) else []
        for q in raw_qs[:q_per_topic]:
            validated = validate_question(q, q_idx, marks_per_q, negative_mark, negative_marking, subj_slug)
            all_questions.append(validated)
            q_idx += 1

    all_questions = all_questions[:total_q]
    instructions = [
        f"Multi-subject test covering: {', '.join(subjects_covered)}.",
        f"{len(all_questions)} questions, {duration} minutes.",
        f"Each correct answer: {marks_per_q} mark(s).",
    ]
    if negative_marking:
        instructions.append(f"Negative marking: -{negative_mark} per wrong answer.")

    payload = {
        "id": uid, "title": title,
        "description": f"Multi-subject {exam_name} mock test covering {', '.join(subjects_covered)}.",
        "board": board, "classSlug": class_slug, "classNumber": 0,
        "subject": "", "chapter": "Multi-Subject", "topic": "Mixed Topics",
        "testType": "mock",
        "duration": duration, "totalMarks": total_marks,
        "passingMarks": round(total_marks * 0.4),
        "totalQuestions": len(all_questions),
        "isPublic": True, "isPublished": True,
        "showCorrectAnswers": True, "randomizeQuestions": True,
        "unlimitedAttempts": True, "allowedAttempts": 0,
        "tags": [class_slug, "multi-subject", "mock-test"] + [t["subjectSlug"] for t in topics_list[:4]],
        "instructions": instructions,
        "createdBy": "admin", "createdByName": "EduTest Admin",
        "publishedAt": now_iso(), "isAdmin": True,
        "questions": all_questions
    }

    path = admin_test_path(uid, title)
    save_file(path, payload)

    index_entry = {
        "id": uid, "type": "test", "title": title,
        "description": payload["description"],
        "subject": "", "board": board, "classSlug": class_slug, "classNumber": 0,
        "chapter": "Multi-Subject", "testType": "mock",
        "totalQuestions": len(all_questions), "duration": duration, "totalMarks": total_marks,
        "author": "EduTest Admin", "authorId": "admin", "isAdmin": True,
        "tags": payload["tags"], "cdnUrl": cdn_url(path), "path": path,
        "publishedAt": payload["publishedAt"]
    }
    return uid, path, index_entry

# ── Note builder (FLASH model — long content) ─────────────────────────────────

def build_note(topic_info):
    """
    Generate 1 comprehensive study note (800-1200 words HTML).
    Uses gemini-2.5-flash (1M token context, best for long content).
    """
    class_slug = topic_info["classSlug"]
    board = topic_info["board"]
    subj_slug = topic_info["subjectSlug"]
    subj_name = topic_info["subjectName"]
    chapter = topic_info["chapter"]
    topic = topic_info["topic"]
    exam_name = class_slug.upper().replace("-", " ")

    uid = make_id()
    title = f"{subj_name} — {topic} | Complete Notes"

    prompt = f"""You are a senior {exam_name} educator and subject expert.
Create comprehensive, exam-focused study notes for:

Exam: {exam_name}
Subject: {subj_name}
Chapter: {chapter}
Topic: {topic}

The notes must be in clean HTML format for direct browser rendering.
Structure (use all of these):
1. <h2> for main topic heading
2. <h3> for subsections
3. <p> for explanations — use <strong> for key terms, <em> for important values
4. <ul><li> for bullet points (key facts, features, types)
5. <code> for formulas, values, drug names, normal ranges
6. <table> for comparisons (if applicable)
7. A "Key Points for Exam" section at the end with <ul>
8. Clinical significance / exam tips section

Requirements:
- 800-1200 words of actual content
- Factually accurate for {exam_name} exam level
- Include normal values, drug doses, or reference ranges where relevant
- Include mnemonics if helpful
- Cover: definition, types/classification, pathophysiology/mechanism, clinical features, management, nursing care (if applicable)

Return ONLY a JSON object — no markdown fences:
{{
  "title": "{title}",
  "description": "One sentence describing what this note covers",
  "content": "<h2>...</h2><p>...</p>...",
  "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"]
}}"""

    print(f"    Generating note: {subj_name}/{topic} [Flash]...")
    result = gemini_client.generate_flash(prompt, expect_json=True, max_tokens=8192)

    if not isinstance(result, dict):
        result = {"title": title, "description": f"Notes on {topic}", "content": str(result), "tags": []}

    content_html = result.get("content", "")
    if not content_html:
        content_html = f"<h2>{topic}</h2><p>Study notes on {topic} from {chapter}.</p>"

    payload = {
        "id": uid,
        "title": result.get("title", title),
        "description": result.get("description", f"Complete study notes on {topic} for {exam_name}"),
        "type": "note",
        "board": board, "classSlug": class_slug, "classNumber": 0,
        "subject": subj_slug, "chapter": chapter,
        "tags": result.get("tags", make_tags(class_slug, subj_slug, chapter, topic)),
        "createdBy": "admin", "createdByName": "EduTest Admin",
        "publishedAt": now_iso(),
        "isPublic": True, "isPublished": True, "isAdmin": True,
        "content": content_html,
        "imageUrls": []
    }

    path = admin_note_path(uid, payload["title"])
    save_file(path, payload)

    index_entry = {
        "id": uid, "type": "note",
        "title": payload["title"], "description": payload["description"],
        "subject": subj_slug, "board": board, "classSlug": class_slug, "classNumber": 0,
        "chapter": chapter,
        "author": "EduTest Admin", "authorId": "admin", "isAdmin": True,
        "tags": payload["tags"], "cdnUrl": cdn_url(path), "path": path,
        "publishedAt": payload["publishedAt"]
    }
    return uid, path, index_entry

# ── Formula builder (PRO model — precision) ───────────────────────────────────

def build_formula(topic_info):
    """
    Generate 1 formula/quick-reference sheet.
    Uses gemini-2.5-pro for maximum factual precision.
    """
    class_slug = topic_info["classSlug"]
    board = topic_info["board"]
    subj_slug = topic_info["subjectSlug"]
    subj_name = topic_info["subjectName"]
    chapter = topic_info["chapter"]
    topic = topic_info["topic"]
    exam_name = class_slug.upper().replace("-", " ")

    uid = make_id()
    title = f"{subj_name} — {topic} | Quick Reference"

    prompt = f"""You are a senior {exam_name} educator creating a precision quick-reference sheet.
Create a comprehensive formula and key-facts sheet for:

Exam: {exam_name}
Subject: {subj_name}
Chapter: {chapter}
Topic: {topic}

The sheet must be in clean HTML format.
Include ALL of the following that apply:
1. <h2> main heading
2. <h3> subsections (Formulas, Normal Values, Key Facts, Classifications, Mnemonics)
3. All formulas in <code> tags
4. Normal values / reference ranges in <strong> tags
5. Drug doses / concentrations in <code> tags
6. Classifications in <ul><li> lists
7. Comparison tables with <table><tr><th><td>
8. Mnemonics in <em> tags
9. "Exam Traps" section — common wrong answers to avoid

Requirements:
- Factually precise — every value must be correct for {exam_name} level
- Concise but complete (400-700 words)
- Organized for quick revision before exam

Return ONLY a JSON object — no markdown fences:
{{
  "title": "{title}",
  "description": "One sentence describing this formula sheet",
  "content": "<h2>...</h2>...",
  "tags": ["tag1", "tag2", "tag3"]
}}"""

    print(f"    Generating formula: {subj_name}/{topic} [Pro]...")
    result = gemini_client.generate_pro(prompt, expect_json=True, max_tokens=4096)

    if not isinstance(result, dict):
        result = {"title": title, "description": f"Formula sheet for {topic}", "content": str(result), "tags": []}

    content_html = result.get("content", "")
    if not content_html:
        content_html = f"<h2>{topic} — Key Reference</h2><p>Quick reference for {topic}.</p>"

    payload = {
        "id": uid,
        "title": result.get("title", title),
        "description": result.get("description", f"Quick reference sheet for {topic}"),
        "type": "formula",
        "board": board, "classSlug": class_slug, "classNumber": 0,
        "subject": subj_slug, "chapter": chapter,
        "tags": result.get("tags", make_tags(class_slug, subj_slug, chapter, topic)),
        "createdBy": "admin", "createdByName": "EduTest Admin",
        "publishedAt": now_iso(),
        "isPublic": True, "isPublished": True, "isAdmin": True,
        "content": content_html,
        "imageUrls": []
    }

    path = admin_formula_path(uid, payload["title"])
    save_file(path, payload)

    index_entry = {
        "id": uid, "type": "formula",
        "title": payload["title"], "description": payload["description"],
        "subject": subj_slug, "board": board, "classSlug": class_slug, "classNumber": 0,
        "chapter": chapter,
        "author": "EduTest Admin", "authorId": "admin", "isAdmin": True,
        "tags": payload["tags"], "cdnUrl": cdn_url(path), "path": path,
        "publishedAt": payload["publishedAt"]
    }
    return uid, path, index_entry

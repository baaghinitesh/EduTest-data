"""
content_builder.py — Builds test/note/formula JSON payloads using Gemini.
All output is validated against the exact schema the EduTest website expects.
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

# ── Question validator ────────────────────────────────────────────────────────

def validate_question(q, idx, marks_per_q, negative_mark, negative_marking, subject_slug):
    """Ensure question matches exact schema the website expects."""
    qid = q.get("id") or f"q-{int(datetime.now().timestamp()*1000)}-{uuid.uuid4().hex[:5]}"
    options = q.get("options", [])
    # Ensure exactly 4 options with correct schema
    validated_opts = []
    for i, opt in enumerate(options[:4]):
        letter = opt.get("letter") or chr(65 + i)
        validated_opts.append({
            "letter": letter,
            "text": str(opt.get("text", f"Option {letter}")),
            "isCorrect": bool(opt.get("isCorrect", False)),
            "index": i,
            "imageUrl": "",
            "explanation": str(opt.get("explanation", ""))
        })
    # Pad to 4 if fewer
    while len(validated_opts) < 4:
        i = len(validated_opts)
        validated_opts.append({
            "letter": chr(65 + i), "text": f"Option {chr(65+i)}",
            "isCorrect": False, "index": i, "imageUrl": "", "explanation": ""
        })
    # Ensure exactly one correct answer for MCQ
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

    return {
        "id": qid,
        "order": idx + 1,
        "question": str(q.get("question", "")).strip(),
        "type": "mcq",
        "options": validated_opts,
        "answer": "",
        "alternateAnswers": [],
        "explanation": str(q.get("explanation", "")),
        "hint": str(q.get("hint", "")),
        "marks": marks_per_q,
        "negativeMark": negative_mark,
        "negativeMarking": negative_marking,
        "imageUrl": "",
        "subject": subject_slug,
        "difficulty": difficulty,
        "hasError": False
    }

# ── Full-length test builder ──────────────────────────────────────────────────

def build_full_length_test(class_slug, exam_config, subjects_distribution, board):
    """Generate 1 full-length test covering all subjects proportionally."""
    total_q = exam_config.get("fullLengthQuestions", 100)
    total_marks = exam_config.get("fullLengthMarks", 100)
    duration = exam_config.get("fullLengthDuration", 90)
    negative_marking = exam_config.get("negativeMarking", False)
    correct_mark = exam_config.get("correctMarkValue", 1)
    negative_mark = exam_config.get("negativeMarkValue", 0)
    marks_per_q = correct_mark

    exam_name = class_slug.upper().replace("-", " ")
    uid = make_id()
    title = f"{exam_name} Full Length Mock Test — {datetime.now().strftime('%B %Y')}"

    all_questions = []
    q_idx = 0

    for subj_info in subjects_distribution:
        subj_name = subj_info["subjectName"]
        subj_slug = subj_info["subjectSlug"]
        q_count = subj_info["questionCount"]
        chapters = subj_info.get("chapters", [])

        # Pick random chapters/topics for variety
        import random
        chapter_topics = []
        for ch in chapters:
            for t in ch.get("topics", []):
                chapter_topics.append((ch["name"], t))
        random.shuffle(chapter_topics)

        # Build prompt for this subject's questions
        topics_sample = chapter_topics[:min(10, len(chapter_topics))]
        topics_str = "; ".join([f"{ch}: {t}" for ch, t in topics_sample])

        prompt = f"""You are an expert {exam_name} exam question setter.
Generate exactly {q_count} high-quality MCQ questions for {exam_name} exam, subject: {subj_name}.
Topics to cover (mix them): {topics_str}

STRICT RULES:
1. Each question must have exactly 4 options (A, B, C, D)
2. Exactly ONE option must be isCorrect: true
3. Each option must have a brief explanation (1-2 sentences) explaining why it is correct or incorrect
4. Questions must be exam-standard difficulty (mix: 30% easy, 50% medium, 20% hard)
5. No repeated questions
6. Return ONLY a valid JSON array, no markdown, no extra text

JSON format for each question:
{{
  "question": "Question text here?",
  "difficulty": "easy|medium|hard",
  "explanation": "Overall explanation of the correct answer",
  "hint": "A helpful hint",
  "options": [
    {{"letter": "A", "text": "Option A text", "isCorrect": false, "explanation": "Why A is wrong"}},
    {{"letter": "B", "text": "Option B text", "isCorrect": true, "explanation": "Why B is correct"}},
    {{"letter": "C", "text": "Option C text", "isCorrect": false, "explanation": "Why C is wrong"}},
    {{"letter": "D", "text": "Option D text", "isCorrect": false, "explanation": "Why D is wrong"}}
  ]
}}

Generate exactly {q_count} questions now as a JSON array:"""

        print(f"  [Builder] Generating {q_count} questions for {subj_name}...")
        raw_questions = gemini_client.generate(prompt, expect_json=True)
        if not isinstance(raw_questions, list):
            raw_questions = [raw_questions] if isinstance(raw_questions, dict) else []

        for q in raw_questions[:q_count]:
            validated = validate_question(q, q_idx, marks_per_q, negative_mark, negative_marking, subj_slug)
            all_questions.append(validated)
            q_idx += 1

    # Trim or pad to exact count
    all_questions = all_questions[:total_q]

    instructions = [
        f"This test contains {total_q} questions.",
        f"Each correct answer carries {correct_mark} mark(s).",
    ]
    if negative_marking:
        instructions.append(f"Each wrong answer carries -{negative_mark} mark(s) penalty.")
    else:
        instructions.append("There is no negative marking.")
    instructions.append("Do not leave the exam window during the test.")

    payload = {
        "id": uid,
        "title": title,
        "description": f"Full length mock test for {exam_name} covering all subjects. {total_q} questions, {total_marks} marks, {duration} minutes.",
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
        "id": uid,
        "type": "test",
        "title": title,
        "description": payload["description"],
        "subject": "",
        "board": board,
        "classSlug": class_slug,
        "classNumber": 0,
        "chapter": "Full Syllabus",
        "testType": "full-length",
        "totalQuestions": len(all_questions),
        "duration": duration,
        "totalMarks": total_marks,
        "author": "EduTest Admin",
        "authorId": "admin",
        "isAdmin": True,
        "tags": payload["tags"],
        "cdnUrl": cdn_url(path),
        "path": path,
        "publishedAt": payload["publishedAt"]
    }

    return uid, path, index_entry

# ── Subject-wise test builder ─────────────────────────────────────────────────

def build_subject_test(topic_info, exam_config, test_number=1):
    """Generate 1 subject/chapter/topic-wise test."""
    class_slug = topic_info["classSlug"]
    board = topic_info["board"]
    subj_slug = topic_info["subjectSlug"]
    subj_name = topic_info["subjectName"]
    chapter = topic_info["chapter"]
    topic = topic_info["topic"]

    q_count = exam_config.get("subjectWiseQuestions", 40)
    marks_per_q = exam_config.get("correctMarkValue", 1)
    total_marks = q_count * marks_per_q
    duration = exam_config.get("subjectWiseDuration", 60)
    negative_marking = exam_config.get("negativeMarking", False)
    negative_mark = exam_config.get("negativeMarkValue", 0)
    exam_name = class_slug.upper().replace("-", " ")

    uid = make_id()
    title = f"{exam_name} — {subj_name} | {chapter} | {topic}"

    prompt = f"""You are an expert {exam_name} exam question setter.
Generate exactly {q_count} high-quality MCQ questions for {exam_name} exam.
Subject: {subj_name}
Chapter: {chapter}
Topic: {topic}

STRICT RULES:
1. ALL questions must be specifically about: {topic} (within {chapter})
2. Each question must have exactly 4 options (A, B, C, D)
3. Exactly ONE option must be isCorrect: true
4. Each option must have a brief explanation (1-2 sentences)
5. Questions must progress from easy to hard (first 30% easy, middle 50% medium, last 20% hard)
6. Questions must be clinically accurate and exam-standard
7. No repeated questions
8. Return ONLY a valid JSON array, no markdown, no extra text

JSON format:
{{
  "question": "Question text?",
  "difficulty": "easy|medium|hard",
  "explanation": "Why the correct answer is correct",
  "hint": "A helpful hint",
  "options": [
    {{"letter": "A", "text": "...", "isCorrect": false, "explanation": "Why wrong"}},
    {{"letter": "B", "text": "...", "isCorrect": true, "explanation": "Why correct"}},
    {{"letter": "C", "text": "...", "isCorrect": false, "explanation": "Why wrong"}},
    {{"letter": "D", "text": "...", "isCorrect": false, "explanation": "Why wrong"}}
  ]
}}

Generate exactly {q_count} questions now as a JSON array:"""

    print(f"  [Builder] Generating subject test: {subj_name} / {chapter} / {topic}")
    raw_questions = gemini_client.generate(prompt, expect_json=True)
    if not isinstance(raw_questions, list):
        raw_questions = [raw_questions] if isinstance(raw_questions, dict) else []

    questions = []
    for i, q in enumerate(raw_questions[:q_count]):
        validated = validate_question(q, i, marks_per_q, negative_mark, negative_marking, subj_slug)
        questions.append(validated)

    instructions = [
        f"This test contains {len(questions)} questions on {topic}.",
        f"Each correct answer carries {marks_per_q} mark(s).",
    ]
    if negative_marking:
        instructions.append(f"Each wrong answer carries -{negative_mark} mark(s) penalty.")

    payload = {
        "id": uid,
        "title": title,
        "description": f"Chapter-wise test on {topic} from {chapter} for {exam_name}. {len(questions)} questions.",
        "board": board,
        "classSlug": class_slug,
        "classNumber": 0,
        "subject": subj_slug,
        "chapter": chapter,
        "topic": topic,
        "testType": "chapter",
        "duration": duration,
        "totalMarks": len(questions) * marks_per_q,
        "passingMarks": round(len(questions) * marks_per_q * 0.4),
        "totalQuestions": len(questions),
        "isPublic": True,
        "isPublished": True,
        "showCorrectAnswers": True,
        "randomizeQuestions": False,
        "unlimitedAttempts": True,
        "allowedAttempts": 0,
        "tags": [class_slug, subj_slug, chapter.lower().replace(" ", "-"), topic.lower().replace(" ", "-")],
        "instructions": instructions,
        "createdBy": "admin",
        "createdByName": "EduTest Admin",
        "publishedAt": now_iso(),
        "isAdmin": True,
        "questions": questions
    }

    path = admin_test_path(uid, title)
    save_file(path, payload)

    index_entry = {
        "id": uid,
        "type": "test",
        "title": title,
        "description": payload["description"],
        "subject": subj_slug,
        "board": board,
        "classSlug": class_slug,
        "classNumber": 0,
        "chapter": chapter,
        "testType": "chapter",
        "totalQuestions": len(questions),
        "duration": duration,
        "totalMarks": payload["totalMarks"],
        "author": "EduTest Admin",
        "authorId": "admin",
        "isAdmin": True,
        "tags": payload["tags"],
        "cdnUrl": cdn_url(path),
        "path": path,
        "publishedAt": payload["publishedAt"]
    }

    return uid, path, index_entry

# ── Multi-subject test builder ────────────────────────────────────────────────

def build_multi_subject_test(topics_list, exam_config):
    """Generate 1 multi-subject test from multiple topics."""
    class_slug = topics_list[0]["classSlug"]
    board = topics_list[0]["board"]
    exam_name = class_slug.upper().replace("-", " ")
    total_q = exam_config.get("multiSubjectTestQuestions", 100)
    marks_per_q = exam_config.get("correctMarkValue", 1)
    total_marks = exam_config.get("multiSubjectTestMarks", 100)
    duration = exam_config.get("multiSubjectTestDuration", 90)
    negative_marking = exam_config.get("negativeMarking", False)
    negative_mark = exam_config.get("negativeMarkValue", 0)

    q_per_topic = max(1, total_q // len(topics_list))
    uid = make_id()
    subjects_covered = list(set(t["subjectName"] for t in topics_list))
    title = f"{exam_name} — Multi-Subject Test | {' + '.join(subjects_covered[:3])}"

    all_questions = []
    q_idx = 0

    for topic_info in topics_list:
        subj_name = topic_info["subjectName"]
        subj_slug = topic_info["subjectSlug"]
        chapter = topic_info["chapter"]
        topic = topic_info["topic"]

        prompt = f"""You are an expert {exam_name} exam question setter.
Generate exactly {q_per_topic} high-quality MCQ questions.
Subject: {subj_name}, Chapter: {chapter}, Topic: {topic}

STRICT RULES:
1. Exactly 4 options per question, exactly ONE isCorrect: true
2. Each option must have a brief explanation
3. Mix of easy/medium/hard difficulty
4. Return ONLY a valid JSON array

JSON format:
[{{"question":"...","difficulty":"medium","explanation":"...","hint":"...","options":[{{"letter":"A","text":"...","isCorrect":false,"explanation":"..."}},{{"letter":"B","text":"...","isCorrect":true,"explanation":"..."}},{{"letter":"C","text":"...","isCorrect":false,"explanation":"..."}},{{"letter":"D","text":"...","isCorrect":false,"explanation":"..."}}]}}]

Generate {q_per_topic} questions:"""

        print(f"  [Builder] Multi-subject: {subj_name}/{topic} ({q_per_topic}q)")
        raw_qs = gemini_client.generate(prompt, expect_json=True)
        if not isinstance(raw_qs, list):
            raw_qs = [raw_qs] if isinstance(raw_qs, dict) else []
        for q in raw_qs[:q_per_topic]:
            validated = validate_question(q, q_idx, marks_per_q, negative_mark, negative_marking, subj_slug)
            all_questions.append(validated)
            q_idx += 1

    all_questions = all_questions[:total_q]
    instructions = [f"Multi-subject test covering: {', '.join(subjects_covered)}.",
                    f"{len(all_questions)} questions, {duration} minutes."]
    if negative_marking:
        instructions.append(f"Negative marking: -{negative_mark} per wrong answer.")

    payload = {
        "id": uid,
        "title": title,
        "description": f"Multi-subject {exam_name} test covering {', '.join(subjects_covered)}.",
        "board": board,
        "classSlug": class_slug,
        "classNumber": 0,
        "subject": "",
        "chapter": "Multi-Subject",
        "topic": "Mixed Topics",
        "testType": "mock",
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
        "tags": [class_slug, "multi-subject", "mock-test"] + [t["subjectSlug"] for t in topics_list],
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
        "id": uid,
        "type": "test",
        "title": title,
        "description": payload["description"],
        "subject": "",
        "board": board,
        "classSlug": class_slug,
        "classNumber": 0,
        "chapter": "Multi-Subject",
        "testType": "mock",
        "totalQuestions": len(all_questions),
        "duration": duration,
        "totalMarks": total_marks,
        "author": "EduTest Admin",
        "authorId": "admin",
        "isAdmin": True,
        "tags": payload["tags"],
        "cdnUrl": cdn_url(path),
        "path": path,
        "publishedAt": payload["publishedAt"]
    }

    return uid, path, index_entry

# ── Note builder ──────────────────────────────────────────────────────────────

def build_note(topic_info):
    """Generate 1 high-quality study note."""
    class_slug = topic_info["classSlug"]
    board = topic_info["board"]
    subj_slug = topic_info["subjectSlug"]
    subj_name = topic_info["subjectName"]
    chapter = topic_info["chapter"]
    topic = topic_info["topic"]
    exam_name = class_slug.upper().replace("-", " ")

    uid = make_id()
    title = f"{subj_name} — {topic} | Complete Notes"

    prompt = f"""You are an expert {exam_name} educator creating high-quality study notes.
Create comprehensive study notes for:
Exam: {exam_name}
Subject: {subj_name}
Chapter: {chapter}
Topic: {topic}

The notes must be in HTML format (for direct rendering in a browser).
Include:
1. A clear heading (h2) for the topic
2. Key definitions and concepts (use <strong> for important terms)
3. Detailed explanations with examples
4. Important points in bullet lists (<ul><li>)
5. Any relevant formulas in <code> tags
6. Memory tips or mnemonics if applicable
7. Common exam questions/points to remember
8. Clinical significance (if medical topic)

Return ONLY a JSON object with this exact structure:
{{
  "title": "{title}",
  "description": "Brief 1-sentence description of what this note covers",
  "content": "<h2>Topic Heading</h2><p>Content here...</p>",
  "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"]
}}

The content field must be valid HTML string. Make it comprehensive (500-1000 words of content).
Return ONLY the JSON object, no markdown fences:"""

    print(f"  [Builder] Generating note: {subj_name} / {topic}")
    result = gemini_client.generate(prompt, expect_json=True)

    if not isinstance(result, dict):
        result = {"title": title, "description": f"Notes on {topic}", "content": str(result), "tags": []}

    content_html = result.get("content", "")
    if not content_html:
        content_html = f"<h2>{topic}</h2><p>Notes on {topic} from {chapter}.</p>"

    payload = {
        "id": uid,
        "title": result.get("title", title),
        "description": result.get("description", f"Complete study notes on {topic} for {exam_name}"),
        "type": "note",
        "board": board,
        "classSlug": class_slug,
        "classNumber": 0,
        "subject": subj_slug,
        "chapter": chapter,
        "tags": result.get("tags", [class_slug, subj_slug, topic.lower().replace(" ", "-")]),
        "createdBy": "admin",
        "createdByName": "EduTest Admin",
        "publishedAt": now_iso(),
        "isPublic": True,
        "isPublished": True,
        "isAdmin": True,
        "content": content_html,
        "imageUrls": []
    }

    path = admin_note_path(uid, payload["title"])
    save_file(path, payload)

    index_entry = {
        "id": uid,
        "type": "note",
        "title": payload["title"],
        "description": payload["description"],
        "subject": subj_slug,
        "board": board,
        "classSlug": class_slug,
        "classNumber": 0,
        "chapter": chapter,
        "author": "EduTest Admin",
        "authorId": "admin",
        "isAdmin": True,
        "tags": payload["tags"],
        "cdnUrl": cdn_url(path),
        "path": path,
        "publishedAt": payload["publishedAt"]
    }

    return uid, path, index_entry

# ── Formula builder ───────────────────────────────────────────────────────────

def build_formula(topic_info):
    """Generate 1 formula sheet."""
    class_slug = topic_info["classSlug"]
    board = topic_info["board"]
    subj_slug = topic_info["subjectSlug"]
    subj_name = topic_info["subjectName"]
    chapter = topic_info["chapter"]
    topic = topic_info["topic"]
    exam_name = class_slug.upper().replace("-", " ")

    uid = make_id()
    title = f"{subj_name} — {topic} | Formula Sheet"

    prompt = f"""You are an expert {exam_name} educator creating a formula/quick-reference sheet.
Create a comprehensive formula and key-facts sheet for:
Exam: {exam_name}
Subject: {subj_name}
Chapter: {chapter}
Topic: {topic}

Return ONLY a JSON object with this exact structure:
{{
  "title": "{title}",
  "description": "Brief description",
  "content": "<h2>Formulas and Key Facts</h2><p>Content here...</p>",
  "tags": ["tag1", "tag2", "tag3"]
}}

The content field must be valid HTML with:
- All important formulas in <code> tags
- Key values and normal ranges in <strong> tags
- Organized sections with <h3> subheadings
- Bullet lists for quick reference points
- Mnemonics if applicable
- Normal values / reference ranges if medical topic

Make it a complete quick-reference sheet (300-600 words).
Return ONLY the JSON object:"""

    print(f"  [Builder] Generating formula: {subj_name} / {topic}")
    result = gemini_client.generate(prompt, expect_json=True)

    if not isinstance(result, dict):
        result = {"title": title, "description": f"Formula sheet for {topic}", "content": str(result), "tags": []}

    content_html = result.get("content", "")
    if not content_html:
        content_html = f"<h2>{topic} — Key Formulas</h2><p>Formula sheet for {topic}.</p>"

    payload = {
        "id": uid,
        "title": result.get("title", title),
        "description": result.get("description", f"Formula sheet and quick reference for {topic}"),
        "type": "formula",
        "board": board,
        "classSlug": class_slug,
        "classNumber": 0,
        "subject": subj_slug,
        "chapter": chapter,
        "tags": result.get("tags", [class_slug, subj_slug, "formula"]),
        "createdBy": "admin",
        "createdByName": "EduTest Admin",
        "publishedAt": now_iso(),
        "isPublic": True,
        "isPublished": True,
        "isAdmin": True,
        "content": content_html,
        "imageUrls": []
    }

    path = admin_formula_path(uid, payload["title"])
    save_file(path, payload)

    index_entry = {
        "id": uid,
        "type": "formula",
        "title": payload["title"],
        "description": payload["description"],
        "subject": subj_slug,
        "board": board,
        "classSlug": class_slug,
        "classNumber": 0,
        "chapter": chapter,
        "author": "EduTest Admin",
        "authorId": "admin",
        "isAdmin": True,
        "tags": payload["tags"],
        "cdnUrl": cdn_url(path),
        "path": path,
        "publishedAt": payload["publishedAt"]
    }

    return uid, path, index_entry

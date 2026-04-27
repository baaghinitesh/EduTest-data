"""
content_builder.py — Builds test/note/formula JSON using gemini-2.5-pro.
All content uses Pro for maximum medical accuracy and quality.

Per run: 5 items max to stay within 100 RPD free tier.
  - Full-length test: 3 API calls (subjects split into 3 groups)
  - Topic test (25Q): 1 API call
  - Note: 1 API call
  - Formula sheet: 1 API call
"""
import os, json, uuid, re
from datetime import datetime, timezone
import gemini_client

ROOT    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CDN_BASE = "https://prepguidedata.launchyourconcept.com"

# ── Helpers ───────────────────────────────────────────────────────────────────

def make_id():
    return str(uuid.uuid4())

def make_slug(title, uid):
    slug = re.sub(r"[^a-z0-9\s-]", "", title.lower())
    slug = re.sub(r"\s+", "-", slug.strip())
    slug = re.sub(r"-+", "-", slug)[:50]
    return f"{slug}-{uid.split('-')[0]}"

def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def cdn_url(path):
    return f"{CDN_BASE}/{path}"

def save_file(path, data):
    full = os.path.join(ROOT, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  [Saved] {path}")

def make_tags(class_slug, subj_slug, chapter, topic):
    tags = {class_slug, subj_slug}
    for word in (chapter + " " + topic).lower().split():
        if len(word) > 3:
            tags.add(word.replace("'","").replace(",",""))
    return list(tags)[:8]

def admin_test_path(uid, title):
    return f"data/admin/tests/{make_slug(title, uid)}.json"

def admin_note_path(uid, title):
    return f"data/admin/notes/{make_slug(title, uid)}.json"

def admin_formula_path(uid, title):
    return f"data/admin/formulas/{make_slug(title, uid)}.json"

# ── Question validator ────────────────────────────────────────────────────────

def validate_question(q, idx, marks, neg_mark, neg_marking, subj_slug):
    """Validate and normalize a question to exact website schema."""
    qid = f"q-{int(datetime.now().timestamp()*1000)}-{uuid.uuid4().hex[:5]}"
    options = q.get("options", [])

    validated = []
    for i, opt in enumerate(options[:4]):
        validated.append({
            "letter": opt.get("letter") or chr(65+i),
            "text": str(opt.get("text", f"Option {chr(65+i)}")).strip(),
            "isCorrect": bool(opt.get("isCorrect", False)),
            "index": i,
            "imageUrl": "",
            "explanation": str(opt.get("explanation", "")).strip()
        })
    while len(validated) < 4:
        i = len(validated)
        validated.append({"letter": chr(65+i), "text": f"Option {chr(65+i)}",
                          "isCorrect": False, "index": i, "imageUrl": "", "explanation": ""})

    # Exactly one correct answer
    correct = [i for i, o in enumerate(validated) if o["isCorrect"]]
    if len(correct) == 0:
        validated[0]["isCorrect"] = True
    elif len(correct) > 1:
        for i, o in enumerate(validated):
            o["isCorrect"] = (i == correct[0])

    diff = q.get("difficulty", "medium")
    if diff not in ("easy", "medium", "hard"):
        diff = "medium"

    return {
        "id": qid,
        "order": idx + 1,
        "question": str(q.get("question", f"Question {idx+1}")).strip(),
        "type": "mcq",
        "options": validated,
        "answer": "",
        "alternateAnswers": [],
        "explanation": str(q.get("explanation", "")).strip(),
        "hint": str(q.get("hint", "")).strip(),
        "marks": marks,
        "negativeMark": neg_mark,
        "negativeMarking": neg_marking,
        "imageUrl": "",
        "subject": subj_slug,
        "difficulty": diff,
        "hasError": False
    }

# ── Shared question prompt ────────────────────────────────────────────────────

def _q_prompt(exam_name, subj_name, chapter, topic, count, extra=""):
    return f"""You are a senior {exam_name} exam expert and question setter with 15+ years experience.
Generate exactly {count} high-quality MCQ questions for {exam_name}.

Subject: {subj_name}
Chapter: {chapter}
Topic: {topic}
{extra}

STRICT REQUIREMENTS:
1. Every question must be specifically about: {topic}
2. Exactly 4 options (A, B, C, D) per question
3. Exactly ONE option isCorrect: true
4. Every option must have a clear explanation (why correct or why wrong, 1-2 sentences)
5. Overall explanation must explain the correct answer clearly
6. Difficulty: first 30% easy, middle 50% medium, last 20% hard
7. Clinically/factually accurate — no hallucinated values
8. No question numbering in question text
9. Return ONLY a valid JSON array — no markdown, no text outside JSON

Format per question:
{{
  "question": "Complete question ending with ?",
  "difficulty": "easy|medium|hard",
  "explanation": "The correct answer is X because...",
  "hint": "Think about...",
  "options": [
    {{"letter":"A","text":"...","isCorrect":false,"explanation":"Wrong because..."}},
    {{"letter":"B","text":"...","isCorrect":true, "explanation":"Correct because..."}},
    {{"letter":"C","text":"...","isCorrect":false,"explanation":"Wrong because..."}},
    {{"letter":"D","text":"...","isCorrect":false,"explanation":"Wrong because..."}}
  ]
}}

Generate exactly {count} questions as a JSON array:"""

# ── Full-length test (3 API calls, subjects split into 3 groups) ──────────────

def build_full_length_test(class_slug, exam_config, subjects_distribution, board):
    """
    1 full-length test using 3 Pro API calls.
    Subjects split into 3 groups to keep each prompt focused and within token limits.
    """
    import random, math
    total_q    = exam_config.get("fullLengthQuestions", 100)
    total_marks= exam_config.get("fullLengthMarks", 100)
    duration   = exam_config.get("fullLengthDuration", 90)
    neg        = exam_config.get("negativeMarking", False)
    mark_val   = exam_config.get("correctMarkValue", 1)
    neg_val    = exam_config.get("negativeMarkValue", 0)
    exam_name  = class_slug.upper().replace("-", " ")

    uid   = make_id()
    title = f"{exam_name} Full Length Mock Test — {datetime.now().strftime('%B %Y')}"
    print(f"\n  [Pro] Full-length test: {total_q}Q / {total_marks} marks / {duration} min")

    # Split subjects into 3 groups for 3 API calls
    n = len(subjects_distribution)
    group_size = math.ceil(n / 3)
    groups = [subjects_distribution[i:i+group_size] for i in range(0, n, group_size)]

    all_questions = []
    q_idx = 0

    for g_num, group in enumerate(groups, 1):
        # Build subject+topic summary for this group
        lines = []
        group_q_total = 0
        for s in group:
            chapters = s.get("chapters", [])
            ct = []
            for ch in chapters:
                for t in ch.get("topics", []):
                    ct.append(f"{ch['name']}: {t}")
            random.shuffle(ct)
            sample = ct[:6]
            lines.append(f"- {s['subjectName']} ({s['questionCount']}Q): {'; '.join(sample)}")
            group_q_total += s["questionCount"]

        subjects_str = "\n".join(lines)
        print(f"    Group {g_num}/3: {group_q_total}Q across {len(group)} subjects [Pro]...")

        prompt = f"""You are a senior {exam_name} exam expert.
Generate exactly {group_q_total} MCQ questions for a full-length {exam_name} mock test.

Cover these subjects and question counts proportionally:
{subjects_str}

STRICT REQUIREMENTS:
1. Distribute questions exactly as specified per subject
2. Each question: 4 options, exactly ONE isCorrect: true
3. Each option must have an explanation (1-2 sentences)
4. Include subject field in each question matching the subject name
5. Difficulty mix: 30% easy, 50% medium, 20% hard
6. Clinically accurate — no hallucinated drug doses or values
7. Return ONLY a valid JSON array

Format:
[{{"question":"...","difficulty":"medium","subject":"Subject Name",
   "explanation":"...","hint":"...",
   "options":[
     {{"letter":"A","text":"...","isCorrect":false,"explanation":"..."}},
     {{"letter":"B","text":"...","isCorrect":true,"explanation":"..."}},
     {{"letter":"C","text":"...","isCorrect":false,"explanation":"..."}},
     {{"letter":"D","text":"...","isCorrect":false,"explanation":"..."}}
   ]}}]

Generate exactly {group_q_total} questions:"""

        raw = gemini_client.generate(prompt, expect_json=True, max_tokens=8192)
        if not isinstance(raw, list):
            raw = [raw] if isinstance(raw, dict) else []

        # Map subject name back to slug
        subj_name_to_slug = {s["subjectName"]: s["subjectSlug"] for s in group}
        for q in raw[:group_q_total]:
            subj_name_q = q.get("subject", group[0]["subjectName"])
            subj_slug = subj_name_to_slug.get(subj_name_q, group[0]["subjectSlug"])
            validated = validate_question(q, q_idx, mark_val, neg_val, neg, subj_slug)
            all_questions.append(validated)
            q_idx += 1

    all_questions = all_questions[:total_q]

    instructions = [
        f"Full length {exam_name} mock test — {len(all_questions)} questions.",
        f"Each correct answer: +{mark_val} mark(s).",
        f"Negative marking: {'-' + str(neg_val) + ' per wrong answer' if neg else 'None'}.",
        f"Duration: {duration} minutes.",
        "Do not leave the exam window during the test."
    ]

    payload = {
        "id": uid, "title": title,
        "description": f"Full length {exam_name} mock test. {len(all_questions)}Q, {total_marks} marks, {duration} min.",
        "board": board, "classSlug": class_slug, "classNumber": 0,
        "subject": "", "chapter": "Full Syllabus", "topic": "Full Length",
        "testType": "full-length",
        "duration": duration, "totalMarks": total_marks,
        "passingMarks": round(total_marks * 0.4),
        "totalQuestions": len(all_questions),
        "isPublic": True, "isPublished": True,
        "showCorrectAnswers": True, "randomizeQuestions": True,
        "unlimitedAttempts": True, "allowedAttempts": 0,
        "tags": [class_slug, "full-length", "mock-test"],
        "instructions": instructions,
        "createdBy": "admin", "createdByName": "EduTest Admin",
        "publishedAt": now_iso(), "isAdmin": True,
        "questions": all_questions
    }
    path = admin_test_path(uid, title)
    save_file(path, payload)

    entry = {
        "id": uid, "type": "test", "title": title,
        "description": payload["description"],
        "subject": "", "board": board, "classSlug": class_slug, "classNumber": 0,
        "chapter": "Full Syllabus", "testType": "full-length",
        "totalQuestions": len(all_questions), "duration": duration, "totalMarks": total_marks,
        "author": "EduTest Admin", "authorId": "admin", "isAdmin": True,
        "tags": payload["tags"], "cdnUrl": cdn_url(path), "path": path,
        "publishedAt": payload["publishedAt"]
    }
    return uid, path, entry

# ── Topic test (1 API call, 25Q) ──────────────────────────────────────────────

def build_subject_test(topic_info, exam_config):
    """25-question topic test. 1 Pro API call."""
    class_slug = topic_info["classSlug"]
    board      = topic_info["board"]
    subj_slug  = topic_info["subjectSlug"]
    subj_name  = topic_info["subjectName"]
    chapter    = topic_info["chapter"]
    topic      = topic_info["topic"]
    exam_name  = class_slug.upper().replace("-", " ")

    q_count  = 25
    mark_val = exam_config.get("correctMarkValue", 1)
    neg      = exam_config.get("negativeMarking", False)
    neg_val  = exam_config.get("negativeMarkValue", 0)
    duration = 30
    uid      = make_id()
    title    = f"{exam_name} — {subj_name} | {topic}"

    print(f"    [Pro] Topic test 25Q: {subj_name} / {topic}")
    raw = gemini_client.generate(_q_prompt(exam_name, subj_name, chapter, topic, q_count),
                                  expect_json=True, max_tokens=6144)
    if not isinstance(raw, list):
        raw = [raw] if isinstance(raw, dict) else []

    questions = [validate_question(q, i, mark_val, neg_val, neg, subj_slug)
                 for i, q in enumerate(raw[:q_count])]

    instructions = [
        f"Topic: {topic} | Chapter: {chapter}",
        f"{len(questions)} questions, {duration} minutes.",
        f"Each correct answer: +{mark_val} mark(s).",
    ]
    if neg:
        instructions.append(f"Negative marking: -{neg_val} per wrong answer.")

    payload = {
        "id": uid, "title": title,
        "description": f"Topic-wise test on {topic} ({chapter}) for {exam_name}. {len(questions)} questions.",
        "board": board, "classSlug": class_slug, "classNumber": 0,
        "subject": subj_slug, "chapter": chapter, "topic": topic,
        "testType": "chapter",
        "duration": duration, "totalMarks": len(questions) * mark_val,
        "passingMarks": round(len(questions) * mark_val * 0.4),
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

    entry = {
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
    return uid, path, entry

# ── Note (1 API call, deep HTML content) ─────────────────────────────────────

def build_note(topic_info):
    """Deep study note. 1 Pro API call."""
    class_slug = topic_info["classSlug"]
    board      = topic_info["board"]
    subj_slug  = topic_info["subjectSlug"]
    subj_name  = topic_info["subjectName"]
    chapter    = topic_info["chapter"]
    topic      = topic_info["topic"]
    exam_name  = class_slug.upper().replace("-", " ")
    uid        = make_id()
    title      = f"{subj_name} — {topic} | Complete Notes"

    print(f"    [Pro] Note: {subj_name} / {topic}")

    prompt = f"""You are a senior {exam_name} educator and subject expert with 15+ years experience.
Create comprehensive, exam-focused study notes for:

Exam: {exam_name}
Subject: {subj_name}
Chapter: {chapter}
Topic: {topic}

Write in clean HTML for direct browser rendering. Include ALL of:
1. <h2> main topic heading
2. <h3> subsections: Definition, Classification/Types, Pathophysiology/Mechanism,
   Clinical Features, Diagnosis, Management/Treatment, Nursing Care, Complications
3. <p> with <strong> for key terms, <em> for important values/doses
4. <ul><li> for lists of features, types, steps
5. <code> for drug doses, normal values, formulas, lab values
6. <table> for comparisons (e.g., Type 1 vs Type 2, different drugs)
7. A "Key Points for {exam_name} Exam" section at the end
8. Mnemonics where helpful (in <em> tags)

Requirements:
- 900-1200 words of actual content
- Every value (drug dose, normal range, lab value) must be factually correct
- Written at {exam_name} exam level — not too basic, not too advanced
- Include what examiners commonly ask about this topic

Return ONLY a JSON object — no markdown fences:
{{
  "title": "{title}",
  "description": "One sentence describing what this note covers for {exam_name}",
  "content": "<h2>...</h2><p>...</p>...",
  "tags": ["tag1","tag2","tag3","tag4","tag5"]
}}"""

    result = gemini_client.generate(prompt, expect_json=True, max_tokens=8192)
    if not isinstance(result, dict):
        result = {"title": title, "description": f"Notes on {topic}", "content": str(result), "tags": []}

    content = result.get("content") or f"<h2>{topic}</h2><p>Notes on {topic}.</p>"

    payload = {
        "id": uid,
        "title": result.get("title", title),
        "description": result.get("description", f"Complete notes on {topic} for {exam_name}"),
        "type": "note",
        "board": board, "classSlug": class_slug, "classNumber": 0,
        "subject": subj_slug, "chapter": chapter,
        "tags": result.get("tags", make_tags(class_slug, subj_slug, chapter, topic)),
        "createdBy": "admin", "createdByName": "EduTest Admin",
        "publishedAt": now_iso(),
        "isPublic": True, "isPublished": True, "isAdmin": True,
        "content": content, "imageUrls": []
    }
    path = admin_note_path(uid, payload["title"])
    save_file(path, payload)

    entry = {
        "id": uid, "type": "note",
        "title": payload["title"], "description": payload["description"],
        "subject": subj_slug, "board": board, "classSlug": class_slug, "classNumber": 0,
        "chapter": chapter,
        "author": "EduTest Admin", "authorId": "admin", "isAdmin": True,
        "tags": payload["tags"], "cdnUrl": cdn_url(path), "path": path,
        "publishedAt": payload["publishedAt"]
    }
    return uid, path, entry

# ── Formula sheet (1 API call, precision content) ────────────────────────────

def build_formula(topic_info):
    """Precision formula/quick-reference sheet. 1 Pro API call."""
    class_slug = topic_info["classSlug"]
    board      = topic_info["board"]
    subj_slug  = topic_info["subjectSlug"]
    subj_name  = topic_info["subjectName"]
    chapter    = topic_info["chapter"]
    topic      = topic_info["topic"]
    exam_name  = class_slug.upper().replace("-", " ")
    uid        = make_id()
    title      = f"{subj_name} — {topic} | Quick Reference"

    print(f"    [Pro] Formula: {subj_name} / {topic}")

    prompt = f"""You are a senior {exam_name} educator creating a precision quick-reference sheet.

Exam: {exam_name}
Subject: {subj_name}
Chapter: {chapter}
Topic: {topic}

Create a comprehensive formula and key-facts sheet in clean HTML.
Include ALL that apply:
1. <h2> main heading
2. <h3> sections: Key Formulas, Normal Values/Reference Ranges, Classifications,
   Drug Doses (if applicable), Mnemonics, Exam Traps (common wrong answers)
3. All formulas in <code> tags
4. All normal values and reference ranges in <strong> tags
5. Drug doses in <code> tags with route and frequency
6. Classifications in <ul><li> lists
7. Comparison <table> where useful
8. Mnemonics in <em> tags
9. "Exam Traps" — 3-5 common mistakes students make on this topic

Requirements:
- Every value must be factually correct for {exam_name} level
- Concise but complete (400-600 words)
- Organized for 5-minute revision before exam

Return ONLY a JSON object — no markdown fences:
{{
  "title": "{title}",
  "description": "One sentence describing this quick reference",
  "content": "<h2>...</h2>...",
  "tags": ["tag1","tag2","tag3"]
}}"""

    result = gemini_client.generate(prompt, expect_json=True, max_tokens=4096)
    if not isinstance(result, dict):
        result = {"title": title, "description": f"Quick reference for {topic}", "content": str(result), "tags": []}

    content = result.get("content") or f"<h2>{topic}</h2><p>Quick reference for {topic}.</p>"

    payload = {
        "id": uid,
        "title": result.get("title", title),
        "description": result.get("description", f"Quick reference for {topic}"),
        "type": "formula",
        "board": board, "classSlug": class_slug, "classNumber": 0,
        "subject": subj_slug, "chapter": chapter,
        "tags": result.get("tags", make_tags(class_slug, subj_slug, chapter, topic)),
        "createdBy": "admin", "createdByName": "EduTest Admin",
        "publishedAt": now_iso(),
        "isPublic": True, "isPublished": True, "isAdmin": True,
        "content": content, "imageUrls": []
    }
    path = admin_formula_path(uid, payload["title"])
    save_file(path, payload)

    entry = {
        "id": uid, "type": "formula",
        "title": payload["title"], "description": payload["description"],
        "subject": subj_slug, "board": board, "classSlug": class_slug, "classNumber": 0,
        "chapter": chapter,
        "author": "EduTest Admin", "authorId": "admin", "isAdmin": True,
        "tags": payload["tags"], "cdnUrl": cdn_url(path), "path": path,
        "publishedAt": payload["publishedAt"]
    }
    return uid, path, entry

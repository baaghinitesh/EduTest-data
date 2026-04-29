"""
content_builder.py — Builds content using 3 APIs.

  gemini-2.5-pro   (GEMINI_API_KEY_PRO) → full-length tests
  gemini-2.5-flash (GEMINI_API_KEY)     → subject-wise tests (10-50Q)
  groq llama-3.3   (GROQ_API_KEY)       → notes
  groq llama-3.1   (GROQ_API_KEY)       → formulas

All builders return (uid, path, index_entry) on success.
On API failure they raise the original error — caller catches and skips.
"""
import os, json, uuid, re, random
from datetime import datetime, timezone

import gemini_client
from gemini_client import GeminiError
import groq_client
from groq_client import GroqError

ROOT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
            tags.add(word.replace("'", "").replace(",", ""))
    return list(tags)[:8]

def admin_test_path(uid, title):
    return f"data/admin/tests/{make_slug(title, uid)}.json"

def admin_note_path(uid, title):
    return f"data/admin/notes/{make_slug(title, uid)}.json"

def admin_formula_path(uid, title):
    return f"data/admin/formulas/{make_slug(title, uid)}.json"

# ── Question validator ────────────────────────────────────────────────────────

def validate_question(q, idx, marks, neg_mark, neg_marking, subj_slug):
    qid = f"q-{int(datetime.now().timestamp()*1000)}-{uuid.uuid4().hex[:5]}"
    options = q.get("options", [])
    validated = []
    for i, opt in enumerate(options[:4]):
        validated.append({
            "letter": opt.get("letter") or chr(65 + i),
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

# ── Question prompt ───────────────────────────────────────────────────────────

def _q_prompt(exam_name, subj_name, chapter, topic, count):
    return f"""You are a senior {exam_name} exam expert and question setter.
Generate exactly {count} high-quality MCQ questions.

Subject: {subj_name}
Chapter: {chapter}
Topic: {topic}

STRICT RULES:
1. Every question must be about: {topic}
2. Exactly 4 options (A, B, C, D) per question
3. Exactly ONE option isCorrect: true
4. Every option must have a clear explanation (1-2 sentences)
5. Difficulty: first 30% easy, middle 50% medium, last 20% hard
6. Clinically accurate — no hallucinated values or doses
7. No question numbering in question text
8. Return ONLY a valid JSON array — no markdown, no extra text

Format:
[{{"question":"...?","difficulty":"medium","explanation":"...","hint":"...",
  "options":[
    {{"letter":"A","text":"...","isCorrect":false,"explanation":"..."}},
    {{"letter":"B","text":"...","isCorrect":true,"explanation":"..."}},
    {{"letter":"C","text":"...","isCorrect":false,"explanation":"..."}},
    {{"letter":"D","text":"...","isCorrect":false,"explanation":"..."}}
  ]}}]

Generate exactly {count} questions:"""

# ── Full-length test (Gemini Pro — 1 call per run) ────────────────────────────

def build_full_length_test(class_slug, exam_config, subjects_distribution, board):
    """
    1 full-length test using gemini-2.5-pro.
    Subjects split into 3 groups → 3 API calls.
    Raises GeminiError if Pro quota exhausted.
    """
    import math
    total_q    = exam_config.get("fullLengthQuestions", 100)
    total_marks= exam_config.get("fullLengthMarks", 100)
    duration   = exam_config.get("fullLengthDuration", 90)
    neg        = exam_config.get("negativeMarking", False)
    mark_val   = exam_config.get("correctMarkValue", 1)
    neg_val    = exam_config.get("negativeMarkValue", 0)
    exam_name  = class_slug.upper().replace("-", " ")

    uid   = make_id()
    title = f"{exam_name} Full Length Mock Test — {datetime.now().strftime('%B %Y')}"
    print(f"\n  [Pro] Full-length: {total_q}Q / {total_marks} marks / {duration} min")

    # Split subjects into 3 groups for 3 API calls
    n = len(subjects_distribution)
    group_size = math.ceil(n / 3)
    groups = [subjects_distribution[i:i+group_size] for i in range(0, n, group_size)]

    all_questions = []
    q_idx = 0

    for g_num, group in enumerate(groups, 1):
        lines = []
        group_q_total = 0
        for s in group:
            chapters = s.get("chapters", [])
            ct = []
            for ch in chapters:
                for t in ch.get("topics", []):
                    ct.append(f"{ch['name']}: {t}")
            random.shuffle(ct)
            sample = ct[:5]
            lines.append(f"- {s['subjectName']} ({s['questionCount']}Q): {'; '.join(sample)}")
            group_q_total += s["questionCount"]

        subjects_str = "\n".join(lines)
        print(f"    Group {g_num}/3: {group_q_total}Q [Pro]...")

        prompt = f"""You are a senior {exam_name} exam expert.
Generate exactly {group_q_total} MCQ questions for a full-length {exam_name} mock test.

Cover these subjects proportionally:
{subjects_str}

RULES:
1. Distribute questions as specified per subject
2. Each question: 4 options, exactly ONE isCorrect: true
3. Each option must have an explanation (1-2 sentences)
4. Include "subject" field matching the subject name
5. Difficulty: 30% easy, 50% medium, 20% hard
6. Clinically accurate
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

        raw = gemini_client.generate_pro(prompt, expect_json=True, max_tokens=8192)
        if not isinstance(raw, list):
            raw = [raw] if isinstance(raw, dict) else []

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

# ── Subject-wise test (Gemini Flash — varied question counts) ─────────────────

# Varied test sizes for diversity
TEST_SIZES = [
    {"q": 10, "duration": 12, "label": "Quick Quiz"},
    {"q": 20, "duration": 25, "label": "Short Test"},
    {"q": 30, "duration": 35, "label": "Chapter Test"},
    {"q": 40, "duration": 50, "label": "Extended Test"},
    {"q": 50, "duration": 60, "label": "Full Subject Test"},
]

def build_subject_test(topic_info, exam_config, size_override=None):
    """
    Subject-wise test using gemini-2.5-flash.
    Randomly picks question count (10/20/30/40/50) for variety.
    Raises GeminiError if Flash quota exhausted.
    """
    class_slug = topic_info["classSlug"]
    board      = topic_info["board"]
    subj_slug  = topic_info["subjectSlug"]
    subj_name  = topic_info["subjectName"]
    chapter    = topic_info["chapter"]
    topic      = topic_info["topic"]
    exam_name  = class_slug.upper().replace("-", " ")

    size = size_override or random.choice(TEST_SIZES)
    q_count  = size["q"]
    duration = size["duration"]
    mark_val = exam_config.get("correctMarkValue", 1)
    neg      = exam_config.get("negativeMarking", False)
    neg_val  = exam_config.get("negativeMarkValue", 0)

    uid   = make_id()
    title = f"{exam_name} — {subj_name} | {topic} ({q_count}Q)"

    print(f"    [Flash] {size['label']} {q_count}Q: {subj_name}/{topic}")
    raw = gemini_client.generate_flash(
        _q_prompt(exam_name, subj_name, chapter, topic, q_count),
        expect_json=True, max_tokens=6144
    )
    if not isinstance(raw, list):
        raw = [raw] if isinstance(raw, dict) else []

    questions = [validate_question(q, i, mark_val, neg_val, neg, subj_slug)
                 for i, q in enumerate(raw[:q_count])]

    instructions = [
        f"{size['label']}: {topic} | {chapter}",
        f"{len(questions)} questions, {duration} minutes.",
        f"Each correct answer: +{mark_val} mark(s).",
    ]
    if neg:
        instructions.append(f"Negative marking: -{neg_val} per wrong answer.")

    payload = {
        "id": uid, "title": title,
        "description": f"{size['label']} on {topic} ({chapter}) for {exam_name}. {len(questions)} questions.",
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

# ── Note (Groq llama-3.3-70b) ─────────────────────────────────────────────────

def build_note(topic_info):
    """
    Deep study note using Groq llama-3.3-70b.
    Raises GroqError on failure — caller catches and skips.
    """
    class_slug = topic_info["classSlug"]
    board      = topic_info["board"]
    subj_slug  = topic_info["subjectSlug"]
    subj_name  = topic_info["subjectName"]
    chapter    = topic_info["chapter"]
    topic      = topic_info["topic"]
    exam_name  = class_slug.upper().replace("-", " ")
    uid        = make_id()
    title      = f"{subj_name} — {topic} | Complete Notes"

    print(f"    [Groq/70B] Note: {subj_name}/{topic}")

    prompt = f"""You are a senior {exam_name} educator and subject expert.
Create comprehensive study notes for:
Exam: {exam_name} | Subject: {subj_name} | Chapter: {chapter} | Topic: {topic}

Write in clean HTML for browser rendering. Include:
1. <h2> main heading for the topic
2. <h3> subsections: Definition, Types/Classification, Mechanism/Pathophysiology, Clinical Features, Management, Nursing Care, Key Points
3. <p> with <strong> for key terms, <em> for important values
4. <ul><li> for lists of features, types, steps
5. <code> for drug doses, normal values, lab values
6. <table> for comparisons where useful
7. A "Key Points for {exam_name} Exam" section at the end
8. Mnemonics in <em> tags where helpful

Requirements:
- 800-1000 words of actual content
- Every value (drug dose, normal range) must be factually correct
- Written at {exam_name} exam level

Return ONLY a JSON object — no markdown fences:
{{"title":"{title}","description":"One sentence description","content":"<h2>...</h2><p>...</p>...","tags":["tag1","tag2","tag3","tag4","tag5"]}}"""

    result = groq_client.generate_note(prompt, expect_json=True)
    if not isinstance(result, dict):
        result = {"title": title, "description": f"Notes on {topic}", "content": str(result), "tags": []}

    content = result.get("content") or f"<h2>{topic}</h2><p>Study notes on {topic}.</p>"

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

# ── Formula (Groq llama-3.1-8b) ───────────────────────────────────────────────

def build_formula(topic_info):
    """
    Formula/quick-reference sheet using Groq llama-3.1-8b.
    Raises GroqError on failure — caller catches and skips.
    """
    class_slug = topic_info["classSlug"]
    board      = topic_info["board"]
    subj_slug  = topic_info["subjectSlug"]
    subj_name  = topic_info["subjectName"]
    chapter    = topic_info["chapter"]
    topic      = topic_info["topic"]
    exam_name  = class_slug.upper().replace("-", " ")
    uid        = make_id()
    title      = f"{subj_name} — {topic} | Quick Reference"

    print(f"    [Groq/8B] Formula: {subj_name}/{topic}")

    prompt = f"""You are a senior {exam_name} educator creating a quick-reference sheet.
Exam: {exam_name} | Subject: {subj_name} | Chapter: {chapter} | Topic: {topic}

Create a formula and key-facts sheet in clean HTML. Include:
1. <h2> main heading
2. <h3> sections: Key Formulas/Values, Normal Ranges, Classifications, Drug Doses (if applicable), Mnemonics, Exam Traps
3. All formulas and values in <code> tags
4. Normal ranges in <strong> tags
5. Classifications in <ul><li> lists
6. Comparison <table> where useful
7. "Exam Traps" — 3-5 common mistakes students make

Requirements:
- Every value must be factually correct for {exam_name} level
- 400-600 words, organized for quick revision

Return ONLY a JSON object — no markdown fences:
{{"title":"{title}","description":"One sentence description","content":"<h2>...</h2>...","tags":["tag1","tag2","tag3"]}}"""

    result = groq_client.generate_formula(prompt, expect_json=True)
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

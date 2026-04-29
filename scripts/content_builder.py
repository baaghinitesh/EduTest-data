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
    # Build a few example questions inline so the model understands the exact format
    return f"""You are a senior {exam_name} exam expert and question setter.
Generate exactly {count} high-quality MCQ questions for the NORCET nursing exam.

Subject: {subj_name}
Chapter: {chapter}
Topic: {topic}

CRITICAL RULES — READ CAREFULLY:
1. You MUST return a JSON ARRAY containing exactly {count} question objects
2. The array MUST start with [ and end with ]
3. Every question must be specifically about: {topic}
4. Each question object MUST have exactly 4 options (A, B, C, D)
5. Exactly ONE option must have "isCorrect": true — the others must have "isCorrect": false
6. Every option must have an "explanation" field (1-2 sentences)
7. Difficulty distribution: questions 1-{max(1,count//3)} = easy, {max(1,count//3)+1}-{max(1,count*4//5)} = medium, rest = hard
8. All values, drug doses, and clinical facts must be accurate for NORCET level
9. Do NOT number the questions in the question text
10. Return ONLY the JSON array — absolutely no markdown, no ```json, no explanation text

EXACT FORMAT (copy this structure for all {count} questions):
[
  {{
    "question": "Which of the following best describes [topic concept]?",
    "difficulty": "easy",
    "explanation": "The correct answer is X because [reason]. This is important for NORCET because [clinical relevance].",
    "hint": "Think about [key concept].",
    "options": [
      {{"letter": "A", "text": "First option text", "isCorrect": false, "explanation": "This is wrong because [reason]."}},
      {{"letter": "B", "text": "Second option text", "isCorrect": true,  "explanation": "This is correct because [reason]."}},
      {{"letter": "C", "text": "Third option text",  "isCorrect": false, "explanation": "This is wrong because [reason]."}},
      {{"letter": "D", "text": "Fourth option text", "isCorrect": false, "explanation": "This is wrong because [reason]."}}
    ]
  }},
  ... (continue for all {count} questions)
]

NOW generate exactly {count} questions about {topic} as a JSON array:"""

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

    # Handle case where model returns a single object instead of array
    if isinstance(raw, dict):
        # Single question returned — retry with stronger instruction
        print(f"    [Flash] Got single object, retrying with array instruction...")
        retry_prompt = (
            f"IMPORTANT: You must return a JSON ARRAY of {q_count} objects, not a single object.\n"
            f"The response MUST start with [ and end with ].\n\n"
        ) + _q_prompt(exam_name, subj_name, chapter, topic, q_count)
        raw = gemini_client.generate_flash(retry_prompt, expect_json=True, max_tokens=6144)

    if not isinstance(raw, list):
        raise GeminiError(f"Gemini returned {type(raw).__name__} instead of list after retry")

    if len(raw) < q_count // 2:
        raise GeminiError(f"Only {len(raw)} questions returned, expected {q_count}")

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

    prompt = f"""You are a senior {exam_name} educator and subject expert with 15+ years of experience.
Create COMPREHENSIVE study notes for NORCET nursing exam preparation.

Exam: {exam_name} | Subject: {subj_name} | Chapter: {chapter} | Topic: {topic}

REQUIREMENTS:
- Write 250-350 lines of detailed HTML content (approximately 1500-2000 words)
- Cover ALL aspects thoroughly — this is a complete study guide, not a summary
- Every fact, value, and dose must be clinically accurate for NORCET level

HTML STRUCTURE (use ALL of these sections):
<h2>{topic}</h2>
<h3>1. Definition and Overview</h3>
  - Clear definition with pathophysiology
  - Epidemiology/incidence if relevant
  - Etiology/causes (list all major causes)

<h3>2. Classification/Types</h3>
  - All types with distinguishing features
  - Use <table> for comparison of types

<h3>3. Pathophysiology/Mechanism</h3>
  - Step-by-step mechanism
  - Use <ul><li> for each step
  - Include relevant lab values in <code> tags

<h3>4. Clinical Features/Signs and Symptoms</h3>
  - Complete list of symptoms
  - Early vs late features
  - Use <strong> for cardinal symptoms

<h3>5. Diagnosis</h3>
  - Diagnostic criteria
  - Lab investigations with normal ranges in <code> tags
  - Imaging/special tests

<h3>6. Management/Treatment</h3>
  - Medical management with drug names, doses in <code> tags
  - Surgical management if applicable
  - Step-by-step treatment protocol

<h3>7. Nursing Care/Nursing Management</h3>
  - Nursing assessment
  - Nursing diagnoses (NANDA format)
  - Nursing interventions (detailed list)
  - Patient education points

<h3>8. Complications</h3>
  - All major complications
  - Emergency complications with management

<h3>9. Mnemonics and Memory Aids</h3>
  - At least 2-3 mnemonics in <em> tags
  - Memory tricks for key values

<h3>10. Key Points for NORCET Exam</h3>
  - 10-15 most important exam points
  - Common exam traps to avoid
  - Frequently asked question patterns

FORMATTING RULES:
- Use <strong> for all key terms and important values
- Use <code> for all drug doses, lab values, normal ranges
- Use <em> for mnemonics and memory aids
- Use <table><tr><th><td> for comparisons
- Use <ul><li> for lists
- Use <ol><li> for sequential steps

CRITICAL: Return ONLY a JSON object — no markdown fences, no extra text:
{{"title":"{title}","description":"Comprehensive NORCET study notes on {topic} covering definition, pathophysiology, clinical features, management, and nursing care","content":"<h2>...</h2><h3>...</h3>...","tags":["{subj_slug}","norcet","{chapter.lower().replace(' ','-')}","{topic.lower().replace(' ','-')[:20]}","nursing"]}}"""

    result = groq_client.generate_note(prompt, expect_json=True)
    if not isinstance(result, dict):
        result = {"title": title, "description": f"Notes on {topic}", "content": str(result), "tags": []}

    content = result.get("content", "")

    # Validate content is actual HTML, not a list/garbage
    if not content or not isinstance(content, str) or not content.strip().startswith("<"):
        raise GroqError(f"Note content is not valid HTML: {str(content)[:100]}")

    # Minimum quality check — must have at least 500 chars of content
    if len(content) < 500:
        raise GroqError(f"Note content too short ({len(content)} chars) — likely a failed generation")

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

    prompt = f"""You are a senior {exam_name} educator creating a comprehensive quick-reference sheet.
Exam: {exam_name} | Subject: {subj_name} | Chapter: {chapter} | Topic: {topic}

Create a DETAILED formula and key-facts sheet in clean HTML. Include ALL sections below:

<h2>{topic} — Quick Reference</h2>

<h3>Key Formulas / Normal Values</h3>
  - All important formulas in <code> tags
  - All normal ranges in <strong> tags with units
  - Reference values with <code>value (unit)</code> format

<h3>Classification / Types</h3>
  - Complete classification with distinguishing features
  - Use <table> for side-by-side comparison

<h3>Drug Doses (if applicable)</h3>
  - Drug name: <code>dose route frequency</code>
  - Include monitoring parameters

<h3>Key Diagnostic Criteria</h3>
  - Numbered criteria using <ol><li>
  - Threshold values in <code> tags

<h3>Mnemonics</h3>
  - At least 2 mnemonics in <em> tags
  - Explain what each letter stands for

<h3>Exam Traps — Common Mistakes</h3>
  - 4-6 common wrong answers students choose
  - Why each is wrong

<h3>Quick Recall Points</h3>
  - 8-10 bullet points of most tested facts
  - Use <strong> for the key value/fact

FORMATTING: Use <strong> for values, <code> for doses/ranges, <em> for mnemonics, <table> for comparisons.
Target: 150-200 lines of HTML content.

CRITICAL: Return ONLY a JSON object — no markdown fences, no extra text:
{{"title":"{title}","description":"Quick reference sheet for {topic} covering normal values, classifications, drug doses, and exam traps for NORCET","content":"<h2>...</h2><h3>...</h3>...","tags":["{subj_slug}","norcet","quick-reference","{topic.lower().replace(' ','-')[:20]}"]}}"""

    result = groq_client.generate_formula(prompt, expect_json=True)
    if not isinstance(result, dict):
        result = {"title": title, "description": f"Quick reference for {topic}", "content": str(result), "tags": []}

    content = result.get("content", "")

    # Validate content is actual HTML, not a list/garbage
    if not content or not isinstance(content, str) or not content.strip().startswith("<"):
        raise GroqError(f"Formula content is not valid HTML: {str(content)[:100]}")

    # Minimum quality check
    if len(content) < 300:
        raise GroqError(f"Formula content too short ({len(content)} chars) — likely a failed generation")

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

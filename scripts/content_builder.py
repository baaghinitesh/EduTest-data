"""
content_builder.py — High-quality content generation.

  gemini-2.5-pro → full-length tests (with Flash fallback if quota exhausted)
  gemini-2.5-flash → subject-wise tests
  groq llama-3.3-70b → notes and formulas

Quality standards:
  - Questions: clinical accuracy, per-option explanations, hints, image links where relevant
  - Notes: 1500-2000 words, 10 sections, mnemonics, tables, image links
  - Formulas: normal values, drug doses, mnemonics, exam traps, image links
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
    """Validate and normalize question to exact website schema."""
    qid = f"q-{int(datetime.now().timestamp()*1000)}-{uuid.uuid4().hex[:5]}"
    options = q.get("options", [])
    validated = []
    for i, opt in enumerate(options[:4]):
        validated.append({
            "letter": opt.get("letter") or chr(65 + i),
            "text": str(opt.get("text", f"Option {chr(65+i)}")).strip(),
            "isCorrect": bool(opt.get("isCorrect", False)),
            "index": i,
            "imageUrl": str(opt.get("imageUrl", "")).strip(),
            "explanation": str(opt.get("explanation", "")).strip()
        })
    while len(validated) < 4:
        i = len(validated)
        validated.append({"letter": chr(65+i), "text": f"Option {chr(65+i)}",
                          "isCorrect": False, "index": i, "imageUrl": "", "explanation": ""})
    # Enforce exactly one correct answer
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
        "imageUrl": str(q.get("imageUrl", "")).strip(),
        "subject": subj_slug,
        "difficulty": diff,
        "hasError": False
    }

# ── Question prompt ───────────────────────────────────────────────────────────

def _q_prompt(exam_name, subj_name, chapter, topic, count):
    """Concise, reliable MCQ prompt — ends with [ to force array continuation."""
    return f"""Generate {count} high-quality NORCET nursing exam MCQ questions about: {topic} ({subj_name}, {chapter})

Return ONLY a JSON array. Start with [ and end with ]. No other text before or after.

Each question object format:
{{"question":"Clinical question ending with ?","difficulty":"easy|medium|hard","explanation":"Why correct answer is right + clinical significance for NORCET.","hint":"Helpful clue without giving answer.","imageUrl":"","options":[{{"letter":"A","text":"Option text","isCorrect":false,"explanation":"Why this is wrong.","imageUrl":""}},{{"letter":"B","text":"Option text","isCorrect":true,"explanation":"Why this is correct.","imageUrl":""}},{{"letter":"C","text":"Option text","isCorrect":false,"explanation":"Why this is wrong.","imageUrl":""}},{{"letter":"D","text":"Option text","isCorrect":false,"explanation":"Why this is wrong.","imageUrl":""}}]}}

Requirements:
- All {count} questions specifically about: {topic}
- Exactly 1 isCorrect:true per question
- Clinically accurate — correct drug doses, lab values, normal ranges
- Difficulty: first {max(1,count//3)} easy, middle {max(1,count//2)} medium, last {max(1,count//5)} hard
- Questions test clinical application, not just memorization
- imageUrl: leave empty string "" (images not required for text questions)

["""

# ── Full-length test (Pro with Flash fallback) ────────────────────────────────

def build_full_length_test(class_slug, exam_config, subjects_distribution, board):
    """
    Full-length test using gemini-2.5-pro.
    Automatically falls back to Flash if Pro quota exhausted.
    Subjects split into 3 groups → 3 API calls.
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
    print(f"\n  [Pro→Flash] Full-length: {total_q}Q / {total_marks} marks / {duration} min")

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
        print(f"    Group {g_num}/3: {group_q_total}Q [Pro→Flash]...")

        prompt = f"""Generate {group_q_total} high-quality NORCET nursing exam MCQ questions.

Cover these subjects proportionally:
{subjects_str}

Return ONLY a JSON array. Start with [ end with ]. No other text.

Each object: {{"question":"...?","difficulty":"easy|medium|hard","subject":"Subject Name","explanation":"Clinical explanation.","hint":"Helpful hint.","imageUrl":"","options":[{{"letter":"A","text":"...","isCorrect":false,"explanation":"...","imageUrl":""}},{{"letter":"B","text":"...","isCorrect":true,"explanation":"...","imageUrl":""}},{{"letter":"C","text":"...","isCorrect":false,"explanation":"...","imageUrl":""}},{{"letter":"D","text":"...","isCorrect":false,"explanation":"...","imageUrl":""}}]}}

Rules: distribute questions per subject as specified, 1 isCorrect per question, clinically accurate, 30% easy 50% medium 20% hard.

["""

        raw = gemini_client.generate_pro(prompt, expect_json=True, max_tokens=8192)

        # Normalize to list
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list):
            raw = []

        subj_name_to_slug = {s["subjectName"]: s["subjectSlug"] for s in group}
        # Filter out non-dict items
        raw = [q for q in raw if isinstance(q, dict)]
        for q in raw[:group_q_total]:
            subj_name_q = q.get("subject", group[0]["subjectName"])
            subj_slug = subj_name_to_slug.get(subj_name_q, group[0]["subjectSlug"])
            validated = validate_question(q, q_idx, mark_val, neg_val, neg, subj_slug)
            all_questions.append(validated)
            q_idx += 1

        # 16s gap between Pro groups (same model cooldown)
        if g_num < len(groups):
            import time
            print(f"    [ModelGap/pro] Waiting 16s before next group...")
            time.sleep(16)

    all_questions = all_questions[:total_q]
    if len(all_questions) == 0:
        raise GeminiError("No questions generated for full-length test")

    instructions = [
        f"Full length {exam_name} mock test — {len(all_questions)} questions.",
        f"Each correct answer: +{mark_val} mark(s).",
        f"Negative marking: {'-' + str(neg_val) + ' per wrong answer' if neg else 'None'}.",
        f"Duration: {duration} minutes.",
        "Read all questions carefully before answering.",
    ]

    payload = {
        "id": uid, "title": title,
        "description": f"Full length {exam_name} mock test covering all subjects. {len(all_questions)} questions, {total_marks} marks, {duration} minutes.",
        "board": board, "classSlug": class_slug, "classNumber": 0,
        "subject": "", "chapter": "Full Syllabus", "topic": "Full Length",
        "testType": "full-length",
        "duration": duration, "totalMarks": total_marks,
        "passingMarks": round(total_marks * 0.4),
        "totalQuestions": len(all_questions),
        "isPublic": True, "isPublished": True,
        "showCorrectAnswers": True, "randomizeQuestions": True,
        "unlimitedAttempts": True, "allowedAttempts": 0,
        "tags": [class_slug, "full-length", "mock-test", "norcet"],
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

# ── Subject-wise test (Flash) ─────────────────────────────────────────────────

TEST_SIZES = [
    {"q": 10, "duration": 12, "label": "Quick Quiz"},
    {"q": 20, "duration": 25, "label": "Short Test"},
    {"q": 30, "duration": 35, "label": "Chapter Test"},
    {"q": 40, "duration": 50, "label": "Extended Test"},
    {"q": 50, "duration": 60, "label": "Full Subject Test"},
]

def build_subject_test(topic_info, exam_config, size_override=None):
    """Subject-wise test using gemini-2.5-flash."""
    class_slug = topic_info["classSlug"]
    board      = topic_info["board"]
    subj_slug  = topic_info["subjectSlug"]
    subj_name  = topic_info["subjectName"]
    chapter    = topic_info["chapter"]
    topic      = topic_info["topic"]
    exam_name  = class_slug.upper().replace("-", " ")

    size     = size_override or random.choice(TEST_SIZES)
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

    # Normalize to list
    if isinstance(raw, dict):
        print(f"    [Flash] Got single object, requesting more...")
        raw = [raw]
        if q_count > 1:
            extra = gemini_client.generate_flash(
                f"Generate {q_count - 1} more NORCET MCQ questions about {topic} ({subj_name}). Return ONLY JSON array starting with [.\n[",
                expect_json=True, max_tokens=6144
            )
            if isinstance(extra, list):
                raw.extend(extra)
            elif isinstance(extra, dict):
                raw.append(extra)

    if not isinstance(raw, list) or len(raw) == 0:
        raise GeminiError(f"No valid questions returned (got {type(raw).__name__})")

    # Filter out non-dict items (Flash sometimes returns strings)
    raw = [q for q in raw if isinstance(q, dict)]

    if len(raw) < max(1, q_count // 3):
        raise GeminiError(f"Too few questions: {len(raw)} returned, need at least {q_count // 3}")

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
        "description": f"{size['label']} on {topic} ({chapter}) for {exam_name}. {len(questions)} questions, {duration} minutes.",
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

# ── Note (Groq 70B) ───────────────────────────────────────────────────────────

def build_note(topic_info):
    """Comprehensive study note using Groq llama-3.3-70b."""
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

    # Generate HTML content directly — more reliable than asking for JSON with embedded HTML
    html_prompt = f"""Write comprehensive NORCET nursing exam study notes as HTML.
Subject: {subj_name} | Chapter: {chapter} | Topic: {topic}

Write 1500-2000 words of detailed HTML. Include ALL these sections:

<h2>{topic}</h2>

<h3>1. Definition and Overview</h3>
Full definition, epidemiology (incidence/prevalence), all causes/etiology.
Use <strong> for key terms. Use <ul><li> for causes list.

<h3>2. Classification / Types</h3>
All types/grades/stages with distinguishing features.
Use <table><tr><th>Type</th><th>Features</th><th>Examples</th></tr>...</table>

<h3>3. Pathophysiology / Mechanism</h3>
Step-by-step mechanism using <ol><li>.
Include relevant lab values in <code>value (unit)</code> format.

<h3>4. Clinical Features / Signs and Symptoms</h3>
Complete symptom list. Use <strong> for cardinal symptoms.
Separate early vs late features. Include objective and subjective findings.

<h3>5. Diagnosis</h3>
Diagnostic criteria. Lab investigations with normal ranges: <code>test: normal range</code>.
Imaging and special tests.

<h3>6. Management / Treatment</h3>
Medical management with drug names and doses: <code>Drug: dose route frequency</code>.
Surgical management if applicable. Step-by-step protocol using <ol><li>.

<h3>7. Nursing Care</h3>
Nursing assessment checklist. NANDA nursing diagnoses (at least 3).
Detailed nursing interventions using <ul><li>. Patient education points.

<h3>8. Complications</h3>
All major complications. Emergency complications with immediate management.

<h3>9. Mnemonics and Memory Aids</h3>
At least 3 mnemonics in <em>WORD</em> format. Explain each letter clearly.
Memory tricks for key values and normal ranges.

<h3>10. Key Points for NORCET Exam</h3>
12-15 most important exam points as <ol><li>.
Common exam traps — what students get wrong.
Most frequently asked question patterns.

FORMATTING RULES:
- <strong> for ALL key terms, values, drug names
- <code> for ALL doses, lab values, normal ranges
- <em> for mnemonics only
- <table> for comparisons
- <ul><li> for unordered lists
- <ol><li> for steps and ranked points

Write ONLY the HTML content. Start with <h2> and end with </ol> or </ul>. No JSON, no markdown."""

    html_content = groq_client.generate_note(html_prompt, expect_json=False)

    # Validate it's actual HTML
    if not html_content or not isinstance(html_content, str):
        raise GroqError("Note generation returned empty content")

    html_content = html_content.strip()
    # Strip any accidental markdown fences
    html_content = re.sub(r"```(?:html)?\s*", "", html_content)
    html_content = re.sub(r"```", "", html_content).strip()

    if not html_content.startswith("<"):
        raise GroqError(f"Note content is not HTML: {html_content[:80]}")

    # If content is too short, expand it with a second call
    if len(html_content) < 3000:
        print(f"    [Groq] Note too short ({len(html_content)} chars), expanding...")
        try:
            extra = groq_client.expand_content(html_content, topic, subj_name, "note")
            if extra and isinstance(extra, str) and len(extra) > 200:
                html_content = html_content + "\n" + extra.strip()
                print(f"    [Groq] Expanded to {len(html_content)} chars")
        except Exception as e:
            print(f"    [Groq] Expansion failed (keeping original): {e}")

    if len(html_content) < 1000:
        raise GroqError(f"Note too short ({len(html_content)} chars)")

    content = html_content

    payload = {
        "id": uid,
        "title": title,
        "description": f"Comprehensive NORCET study notes on {topic} — definition, pathophysiology, clinical features, management, and nursing care",
        "type": "note",
        "board": board, "classSlug": class_slug, "classNumber": 0,
        "subject": subj_slug, "chapter": chapter,
        "tags": make_tags(class_slug, subj_slug, chapter, topic) + ["nursing", "norcet"],
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

# ── Formula (Groq 70B) ────────────────────────────────────────────────────────

def build_formula(topic_info):
    """Quick-reference formula sheet using Groq llama-3.3-70b."""
    class_slug = topic_info["classSlug"]
    board      = topic_info["board"]
    subj_slug  = topic_info["subjectSlug"]
    subj_name  = topic_info["subjectName"]
    chapter    = topic_info["chapter"]
    topic      = topic_info["topic"]
    exam_name  = class_slug.upper().replace("-", " ")
    uid        = make_id()
    title      = f"{subj_name} — {topic} | Quick Reference"

    print(f"    [Groq/70B] Formula: {subj_name}/{topic}")

    prompt = f"""Create a NORCET nursing quick-reference sheet.
Subject: {subj_name} | Chapter: {chapter} | Topic: {topic}

Write HTML content (120-180 lines) with:

<h2>{topic} — Quick Reference</h2>

<h3>Normal Values / Key Formulas</h3>
All values: <strong>value (unit)</strong>. Formulas: <code>formula</code>.

<h3>Classification</h3>
<table> for comparison of types/grades/stages.

<h3>Drug Doses</h3>
<code>Drug: dose route frequency</code>. Monitoring parameters.

<h3>Diagnostic Criteria</h3>
<ol><li> with threshold values in <code>.

<h3>Mnemonics</h3>
2 mnemonics in <em>MNEMONIC</em>. Explain each letter.

<h3>Exam Traps</h3>
4-5 common mistakes with <strong>correct answer</strong> vs wrong choice.

<h3>Quick Recall</h3>
8-10 bullet points. <strong>key value</strong> for each.

For diagrams: add <!-- IMAGE: description --> where helpful.

Return ONLY this JSON (no markdown):
{{"title":"{title}","description":"NORCET quick reference for {topic} — normal values, drug doses, mnemonics, exam traps","content":"<h2>...</h2>...","tags":["{subj_slug}","norcet","quick-reference","{topic.lower().replace(' ','-')[:25]}"]}}"""

    result = groq_client.generate_formula(prompt, expect_json=True)
    if not isinstance(result, dict):
        result = {"title": title, "description": f"Quick reference for {topic}", "content": str(result), "tags": []}

    content = result.get("content", "")

    # Validate HTML
    if not content or not isinstance(content, str) or not content.strip().startswith("<"):
        print(f"    [Groq] Formula not HTML, retrying...")
        simple = f'Create NORCET quick reference for {topic} ({subj_name}). Return ONLY JSON: {{"title":"{title}","description":"Quick reference for {topic}","content":"<h2>{topic}</h2>...HTML...","tags":["{subj_slug}","norcet"]}}'
        result2 = groq_client.generate_formula(simple, expect_json=True)
        if isinstance(result2, dict):
            content = result2.get("content", "")
            if content and isinstance(content, str) and content.strip().startswith("<"):
                result = result2
            else:
                raise GroqError(f"Formula content not valid HTML after retry")
        else:
            raise GroqError("Formula retry failed")

    if len(content) < 200:
        raise GroqError(f"Formula too short ({len(content)} chars)")

    # If formula is too short, expand it
    if len(content) < 1500:
        print(f"    [Groq] Formula too short ({len(content)} chars), expanding...")
        try:
            extra = groq_client.expand_content(content, topic, subj_name, "formula/quick-reference")
            if extra and isinstance(extra, str) and len(extra) > 200:
                content = content + "\n" + extra.strip()
                print(f"    [Groq] Expanded to {len(content)} chars")
        except Exception as e:
            print(f"    [Groq] Expansion failed (keeping original): {e}")

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

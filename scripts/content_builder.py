"""
content_builder.py — High-quality content generation with 4-Agent Pipeline.

Agent 1: Researcher (Flash)
Agent 2: Generator (Pro Key 1, chunked)
Agent 3: Auditor (Pro spare quota)
Agent 4: Formatter (Groq 70B)
"""
import os, json, uuid, re, random
from datetime import datetime, timezone

import gemini_client
import groq_client
from gemini_client import GeminiError
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

def admin_path(type_folder, uid, title):
    return f"data/admin/{type_folder}/{make_slug(title, uid)}.json"


# ── Question Validation ───────────────────────────────────────────────────────

def validate_question(q, idx, marks, neg_val, neg_marking, subj_slug):
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
        validated.append({"letter": chr(65+i), "text": f"Option {chr(65+i)}", "isCorrect": False, "index": i, "imageUrl": "", "explanation": ""})
    
    correct = [i for i, o in enumerate(validated) if o["isCorrect"]]
    if len(correct) == 0:
        validated[0]["isCorrect"] = True
    elif len(correct) > 1:
        for i, o in enumerate(validated):
            o["isCorrect"] = (i == correct[0])
            
    diff = q.get("difficulty", "medium").lower()
    if diff not in ("easy", "medium", "hard"): diff = "medium"
    
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
        "negativeMark": neg_val if neg_marking else 0,
        "negativeMarking": neg_marking,
        "imageUrl": str(q.get("imageUrl", "")).strip(),
        "subject": subj_slug,
        "difficulty": diff,
        "hasError": False
    }


# ── 4-Agent Pipeline for Tests ────────────────────────────────────────────────

def _build_test_questions(topic_info, q_count, exam_name):
    """Executes the 4-agent pipeline to generate exactly q_count questions."""
    subj_name = topic_info["subjectName"]
    topic     = topic_info["topic"]
    
    # Adaptive chunking based on exam type
    class_slug = topic_info["classSlug"]
    if "jee-advanced" in class_slug or "olympiad" in class_slug:
        chunk_size = 5
    elif "jee-mains" in class_slug or "neet" in class_slug:
        chunk_size = 10
    else:
        chunk_size = 15

    # 1. Researcher
    res_prompt = (
        f"You are a senior exam expert for {exam_name}.\n"
        f"Topic: {subj_name} / {topic}\n"
        "Provide a strict, bulleted analysis (max 200 words):\n"
        "1. Question format used in official papers\n"
        "2. Difficulty level ratio\n"
        "3. Top 5 core concepts guaranteed to appear\n"
        "4. Common traps / distractors\n"
    )
    print("    [Agent 1: Researcher] Analyzing exam context...")
    research_ctx = gemini_client.research(res_prompt)
    if not research_ctx:
        research_ctx = "Standard exam difficulty."

    all_questions = []
    chunks = [chunk_size] * (q_count // chunk_size)
    if q_count % chunk_size != 0:
        chunks.append(q_count % chunk_size)

    schema_example = """[
  {
    "question": "Question text?",
    "difficulty": "medium",
    "explanation": "Overall explanation",
    "hint": "Helpful hint",
    "options": [
      {"letter":"A", "text":"opt", "isCorrect":true, "explanation":"why correct"},
      {"letter":"B", "text":"opt", "isCorrect":false, "explanation":"why wrong"}
    ]
  }
]"""

    for i, count in enumerate(chunks, 1):
        print(f"    Chunk {i}/{len(chunks)} ({count}Q):")
        
        # 2. Generator
        gen_prompt = (
            f"Using this exam research:\n{research_ctx}\n\n"
            f"Generate {count} high-difficulty MCQ questions for {exam_name} on {topic} ({subj_name}).\n"
            "Include tricky options and detailed deductive solutions. Output raw text (not JSON)."
        )
        print("      [Agent 2: Generator] Drafting questions...")
        raw_text = gemini_client.generate_pro(gen_prompt, expect_json=False)

        # 3. Auditor
        print("      [Agent 3: Auditor] Reviewing quality...")
        audited = gemini_client.audit(raw_text, exam_name, topic)
        final_text = audited if audited else raw_text

        # 4. Formatter
        print("      [Agent 4: Formatter] Extracting structured JSON...")
        try:
            chunk_qs = groq_client.format_questions(final_text, schema_example, exam_name, subj_name)
            if isinstance(chunk_qs, list):
                all_questions.extend(chunk_qs)
            elif isinstance(chunk_qs, dict):
                all_questions.append(chunk_qs)
        except Exception as e:
            print(f"      [Formatter Error] {e}")

    return all_questions

# ── Content Builders ──────────────────────────────────────────────────────────

def build_subject_test(topic_info):
    """Subject-wise test using the 4-agent pipeline."""
    class_slug = topic_info["classSlug"]
    board      = topic_info["board"]
    subj_slug  = topic_info["subjectSlug"]
    subj_name  = topic_info["subjectName"]
    chapter    = topic_info["chapter"]
    topic      = topic_info["topic"]
    exam_name  = class_slug.upper().replace("-", " ")
    exam_cfg   = topic_info.get("examConfig", {})
    
    q_count  = 15
    duration = 20
    mark_val = exam_cfg.get("correctMarkValue", 1)
    neg      = exam_cfg.get("negativeMarking", False)
    neg_val  = exam_cfg.get("negativeMarkValue", 0)

    uid   = make_id()
    title = f"{exam_name} — {subj_name} | {topic} Test"

    print(f"\n  [Pipeline] Building Test: {title}")
    raw_qs = _build_test_questions(topic_info, q_count, exam_name)
    
    if not raw_qs or len(raw_qs) < max(1, q_count // 3):
        raise Exception(f"Insufficient questions generated: {len(raw_qs)}")

    questions = [validate_question(q, i, mark_val, neg_val, neg, subj_slug) 
                 for i, q in enumerate(raw_qs[:q_count])]

    payload = {
        "id": uid, "title": title,
        "description": f"Automated test on {topic} for {exam_name}. {len(questions)} questions.",
        "board": board, "classSlug": class_slug, "classNumber": 0,
        "subject": subj_slug, "chapter": chapter, "topic": topic,
        "testType": "topic-wise",
        "duration": duration, "totalMarks": len(questions) * mark_val,
        "passingMarks": round(len(questions) * mark_val * 0.4),
        "totalQuestions": len(questions),
        "isPublic": True, "isPublished": True, "isAdmin": True,
        "showCorrectAnswers": True, "randomizeQuestions": False,
        "unlimitedAttempts": True, "allowedAttempts": 0,
        "tags": make_tags(class_slug, subj_slug, chapter, topic),
        "instructions": [f"Test on {topic}.", f"Duration: {duration} mins.", f"+{mark_val} per correct answer."],
        "createdBy": "admin", "createdByName": "EduTest Admin",
        "publishedAt": now_iso(), "questions": questions
    }
    path = admin_path("tests", uid, title)
    save_file(path, payload)

    entry = {
        "id": uid, "type": "test", "title": title, "description": payload["description"],
        "subject": subj_slug, "board": board, "classSlug": class_slug, "classNumber": 0,
        "chapter": chapter, "testType": "topic-wise",
        "totalQuestions": len(questions), "duration": duration, "totalMarks": payload["totalMarks"],
        "author": "EduTest Admin", "authorId": "admin", "isAdmin": True,
        "tags": payload["tags"], "cdnUrl": cdn_url(path), "path": path,
        "publishedAt": payload["publishedAt"]
    }
    return uid, path, entry


def build_note(topic_info):
    class_slug, subj_slug, subj_name, chapter, topic, board = (
        topic_info["classSlug"], topic_info["subjectSlug"], topic_info["subjectName"],
        topic_info["chapter"], topic_info["topic"], topic_info["board"]
    )
    exam_name = class_slug.upper().replace("-", " ")
    uid   = make_id()
    title = f"{subj_name} — {topic} | Complete Notes"

    print(f"\n  [Groq] Building Note: {title}")
    prompt = (
        f"Write comprehensive {exam_name} study notes as HTML.\n"
        f"Subject: {subj_name} | Chapter: {chapter} | Topic: {topic}\n"
        "Write 1500-2000 words. Include: <h2>Topic</h2>, <h3>1. Overview</h3>, "
        "<h3>2. Key Concepts</h3>, <h3>3. Applications</h3>, <h3>4. Exam Traps</h3>. "
        "Use <strong> for key terms, <ul><li> for lists, <table> for comparisons. "
        "Write ONLY HTML content."
    )
    content = groq_client.generate_note(prompt, exam_name, subj_name, expect_json=False)
    if not content or len(content) < 500:
        raise GroqError("Note generation failed or too short.")

    payload = {
        "id": uid, "title": title, "description": f"Comprehensive notes on {topic} for {exam_name}.",
        "type": "note", "board": board, "classSlug": class_slug, "classNumber": 0,
        "subject": subj_slug, "chapter": chapter, "tags": make_tags(class_slug, subj_slug, chapter, topic),
        "createdBy": "admin", "createdByName": "EduTest Admin", "publishedAt": now_iso(),
        "isPublic": True, "isPublished": True, "isAdmin": True,
        "content": content, "imageUrls": []
    }
    path = admin_path("notes", uid, title)
    save_file(path, payload)

    entry = {
        "id": uid, "type": "note", "title": title, "description": payload["description"],
        "subject": subj_slug, "board": board, "classSlug": class_slug, "classNumber": 0,
        "chapter": chapter, "author": "EduTest Admin", "authorId": "admin", "isAdmin": True,
        "tags": payload["tags"], "cdnUrl": cdn_url(path), "path": path, "publishedAt": payload["publishedAt"]
    }
    return uid, path, entry


def build_formula(topic_info):
    class_slug, subj_slug, subj_name, chapter, topic, board = (
        topic_info["classSlug"], topic_info["subjectSlug"], topic_info["subjectName"],
        topic_info["chapter"], topic_info["topic"], topic_info["board"]
    )
    exam_name = class_slug.upper().replace("-", " ")
    uid   = make_id()
    title = f"{subj_name} — {topic} | Quick Reference"

    print(f"\n  [Groq] Building Formula: {title}")
    prompt = (
        f"Create a {exam_name} quick-reference sheet for {topic} ({subj_name}).\n"
        "Write HTML with <h2>{topic} — Quick Reference</h2>, <h3>Formulas/Values</h3> with <code>, "
        "<h3>Mnemonics</h3> in <em>. Return ONLY JSON:\n"
        '{"content": "<h2>...</h2>..."}'
    )
    res = groq_client.generate_formula(prompt, exam_name, subj_name, expect_json=True)
    content = res.get("content", "") if isinstance(res, dict) else ""
    if not content or len(content) < 200:
        raise GroqError("Formula generation failed or too short.")

    payload = {
        "id": uid, "title": title, "description": f"Quick reference for {topic}.",
        "type": "formula", "board": board, "classSlug": class_slug, "classNumber": 0,
        "subject": subj_slug, "chapter": chapter, "tags": make_tags(class_slug, subj_slug, chapter, topic),
        "createdBy": "admin", "createdByName": "EduTest Admin", "publishedAt": now_iso(),
        "isPublic": True, "isPublished": True, "isAdmin": True,
        "content": content, "imageUrls": []
    }
    path = admin_path("formulas", uid, title)
    save_file(path, payload)

    entry = {
        "id": uid, "type": "formula", "title": title, "description": payload["description"],
        "subject": subj_slug, "board": board, "classSlug": class_slug, "classNumber": 0,
        "chapter": chapter, "author": "EduTest Admin", "authorId": "admin", "isAdmin": True,
        "tags": payload["tags"], "cdnUrl": cdn_url(path), "path": path, "publishedAt": payload["publishedAt"]
    }
    return uid, path, entry


def build_resource(topic_info):
    class_slug, subj_slug, subj_name, chapter, topic, board = (
        topic_info["classSlug"], topic_info["subjectSlug"], topic_info["subjectName"],
        topic_info["chapter"], topic_info["topic"], topic_info["board"]
    )
    exam_name = class_slug.upper().replace("-", " ")
    uid   = make_id()
    title = f"{subj_name} — {topic} | Study Resources"

    print(f"\n  [Groq] Building Resource: {title}")
    prompt = (
        f"Create a curated resource list for {topic} ({subj_name}, {exam_name}).\n"
        "Write HTML content summarizing why these resources are good. Then provide a list of 3-5 generic "
        "links (like Wikipedia, generic YouTube searches, or textbook references).\n"
        'Return ONLY JSON:\n{"content": "<h2>...</h2>...", "links": [{"title": "...", "url": "...", "type": "video|article|book"}]}'
    )
    res = groq_client.generate_resource(prompt, exam_name, subj_name, expect_json=True)
    content = res.get("content", "") if isinstance(res, dict) else ""
    links   = res.get("links", []) if isinstance(res, dict) else []
    if not content or len(content) < 100:
        raise GroqError("Resource generation failed or too short.")

    payload = {
        "id": uid, "title": title, "description": f"Curated resources for {topic}.",
        "type": "resource", "board": board, "classSlug": class_slug, "classNumber": 0,
        "subject": subj_slug, "chapter": chapter, "tags": make_tags(class_slug, subj_slug, chapter, topic),
        "createdBy": "admin", "createdByName": "EduTest Admin", "publishedAt": now_iso(),
        "isPublic": True, "isPublished": True, "isAdmin": True,
        "content": content, "links": links, "imageUrls": []
    }
    path = admin_path("resources", uid, title)
    save_file(path, payload)

    entry = {
        "id": uid, "type": "resource", "title": title, "description": payload["description"],
        "subject": subj_slug, "board": board, "classSlug": class_slug, "classNumber": 0,
        "chapter": chapter, "author": "EduTest Admin", "authorId": "admin", "isAdmin": True,
        "tags": payload["tags"], "cdnUrl": cdn_url(path), "path": path, "publishedAt": payload["publishedAt"]
    }
    return uid, path, entry

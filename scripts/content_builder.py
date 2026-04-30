"""
content_builder.py — High-quality content generation.

Two pipelines:
  - Subject-wise test: Flash → Pro K1 → Pro K2 → Groq  (4-agent, high quality)
  - Full-length test:  Flash research → Flash generate (chunked) → Groq format
                       (Flash-only saves Pro quota; Flash 2.5 is fully capable)

Groq 70B: all notes, formulas, resources (zero Gemini cost).
"""
import os, json, uuid, re, random
from datetime import datetime, timezone

import gemini_client
import groq_client
from gemini_client import GeminiError
from groq_client import GroqError
import topic_picker

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
            "letter":      opt.get("letter") or chr(65 + i),
            "text":        str(opt.get("text", f"Option {chr(65+i)}")).strip(),
            "isCorrect":   bool(opt.get("isCorrect", False)),
            "index":       i,
            "imageUrl":    str(opt.get("imageUrl", "")).strip(),
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

    diff = q.get("difficulty", "medium").lower()
    if diff not in ("easy", "medium", "hard"):
        diff = "medium"

    return {
        "id": qid, "order": idx + 1,
        "question": str(q.get("question", f"Question {idx+1}")).strip(),
        "type": "mcq", "options": validated,
        "answer": "", "alternateAnswers": [],
        "explanation": str(q.get("explanation", "")).strip(),
        "hint":        str(q.get("hint", "")).strip(),
        "marks": marks, "negativeMark": neg_val if neg_marking else 0,
        "negativeMarking": neg_marking, "imageUrl": str(q.get("imageUrl", "")).strip(),
        "subject": subj_slug, "difficulty": diff, "hasError": False
    }


# ── Shared JSON schema for Groq formatter ─────────────────────────────────────

_SCHEMA = """[
  {
    "question": "Question text?",
    "difficulty": "medium",
    "explanation": "Full explanation",
    "hint": "Optional hint",
    "options": [
      {"letter":"A","text":"option","isCorrect":true,"explanation":"why correct"},
      {"letter":"B","text":"option","isCorrect":false,"explanation":"why wrong"},
      {"letter":"C","text":"option","isCorrect":false,"explanation":"why wrong"},
      {"letter":"D","text":"option","isCorrect":false,"explanation":"why wrong"}
    ]
  }
]"""


# ── Pipeline A: Flash-only (Full-Length Tests) ─────────────────────────────────
# Uses Flash for research + generation, Groq for formatting.
# Saves ALL Pro quota for subject-wise tests.

def _flash_generate_chunk(count, exam_name, subj_name, research_ctx, chapters_str):
    """One chunk: Flash generate → Groq format. Returns list of raw question dicts."""
    gen_prompt = (
        f"Research context:\n{research_ctx}\n\n"
        f"Generate exactly {count} high-quality MCQ questions for {exam_name} "
        f"on {subj_name} (covering: {chapters_str}).\n"
        "Difficulty distribution: 30% easy, 50% medium, 20% hard.\n"
        "Each question: question text, 4 options (A/B/C/D), mark the correct one, "
        "brief explanation per option, overall explanation.\n"
        "Output raw text — NOT JSON."
    )
    print(f"      [Flash Generate] {count}Q for {subj_name}...")
    raw = gemini_client.generate_flash(gen_prompt, expect_json=False, max_tokens=6000)

    print(f"      [Groq Format] Extracting JSON...")
    try:
        qs = groq_client.format_questions(raw, _SCHEMA, exam_name, subj_name)
        return qs if isinstance(qs, list) else ([qs] if isinstance(qs, dict) else [])
    except Exception as e:
        print(f"      [Groq Format Error] {e}")
        return []


def _build_full_length_questions(subjects_with_counts, exam_name):
    """
    Flash-only pipeline for full-length tests.
    subjects_with_counts: [{subjectSlug, subjectName, questionCount, chapters}]
    Returns list of (validated_question, subj_slug) tuples.
    """
    all_results = []
    chunk_size = 20  # Flash handles 20Q per call comfortably

    for subj in subjects_with_counts:
        subj_slug  = subj["subjectSlug"]
        subj_name  = subj["subjectName"]
        q_count    = subj["questionCount"]
        chapters   = subj.get("chapters", [])
        chapters_str = ", ".join(ch["name"] for ch in chapters[:6]) if chapters else "General Topics"

        print(f"\n    [{subj_name}] Generating {q_count}Q (Flash-only)...")

        # Research (1 Flash call per subject)
        res_prompt = (
            f"You are a senior {exam_name} exam expert.\n"
            f"Subject: {subj_name} | Key chapters: {chapters_str}\n"
            "Provide a concise 120-word analysis:\n"
            "1. Most-tested question patterns\n"
            "2. Difficulty ratio (Easy/Medium/Hard)\n"
            "3. Top 5 high-yield topics\n"
            "4. Common wrong-answer traps\n"
        )
        research_ctx = gemini_client.research(res_prompt) or "Standard exam difficulty."

        # Generate in chunks
        chunks = [chunk_size] * (q_count // chunk_size)
        if q_count % chunk_size:
            chunks.append(q_count % chunk_size)

        subj_qs = []
        for i, cnt in enumerate(chunks, 1):
            print(f"      Chunk {i}/{len(chunks)} ({cnt}Q):")
            chunk_qs = _flash_generate_chunk(cnt, exam_name, subj_name, research_ctx, chapters_str)
            subj_qs.extend(chunk_qs)

        # Trim / pad to exact count
        all_results.extend([(q, subj_slug) for q in subj_qs[:q_count]])

    return all_results


# ── Pipeline B: 4-Agent (Subject-Wise Tests) ──────────────────────────────────
# Flash Research → Pro K1 Generate → Pro K2 Audit → Groq Format

def _build_subject_questions(topic_info, q_count, exam_name):
    """4-agent pipeline for subject-wise / topic-wise tests (high quality)."""
    subj_name = topic_info["subjectName"]
    topic     = topic_info["topic"]
    class_slug = topic_info["classSlug"]

    # Adaptive chunk: 15Q per call (single chunk for subject tests)
    chunk_size = 10 if "jee" in class_slug or "neet" in class_slug else 15

    # Agent 1 — Researcher (Flash)
    res_prompt = (
        f"Senior exam expert for {exam_name}.\n"
        f"Topic: {subj_name} / {topic}\n"
        "Strict 150-word analysis:\n"
        "1. Official paper question format\n"
        "2. Difficulty ratio\n"
        "3. Top 5 core concepts\n"
        "4. Common traps / distractors\n"
    )
    print("    [Agent 1: Researcher] Analyzing exam context...")
    research_ctx = gemini_client.research(res_prompt) or "Standard exam difficulty."

    all_questions = []
    chunks = [chunk_size] * (q_count // chunk_size)
    if q_count % chunk_size:
        chunks.append(q_count % chunk_size)

    for i, count in enumerate(chunks, 1):
        print(f"    Chunk {i}/{len(chunks)} ({count}Q):")

        # Agent 2 — Generator (Pro K1)
        gen_prompt = (
            f"Using this exam research:\n{research_ctx}\n\n"
            f"Generate {count} high-difficulty MCQ questions for {exam_name} "
            f"on {topic} ({subj_name}).\n"
            "Include tricky options and detailed deductive solutions. "
            "Output raw text — NOT JSON."
        )
        print("      [Agent 2: Generator] Drafting questions...")
        raw_text = gemini_client.generate_pro(gen_prompt, expect_json=False)

        # Agent 3 — Auditor (Pro K2)
        print("      [Agent 3: Auditor] Reviewing quality...")
        audited   = gemini_client.audit(raw_text, exam_name, topic)
        final_txt = audited if audited else raw_text

        # Agent 4 — Formatter (Groq)
        print("      [Agent 4: Formatter] Extracting structured JSON...")
        try:
            chunk_qs = groq_client.format_questions(final_txt, _SCHEMA, exam_name, subj_name)
            if isinstance(chunk_qs, list):
                all_questions.extend(chunk_qs)
            elif isinstance(chunk_qs, dict):
                all_questions.append(chunk_qs)
        except Exception as e:
            print(f"      [Formatter Error] {e}")

    return all_questions


# ── Public Builders ───────────────────────────────────────────────────────────

def build_full_length_test(class_slug):
    """
    Full-length test using Flash-only pipeline (saves Pro quota).
    Covers ALL subjects with weightage-based question distribution.
    """
    topics_data = topic_picker.load_topics(class_slug)
    if not topics_data:
        raise ValueError(f"No topic data for {class_slug}")

    exam_cfg   = topics_data.get("examConfig", {})
    board      = topics_data.get("board", "Competitive")
    exam_name  = class_slug.upper().replace("-", " ")
    total_q    = exam_cfg.get("fullLengthQuestions", 100)
    total_m    = exam_cfg.get("fullLengthMarks", 100)
    duration   = exam_cfg.get("fullLengthDuration", 120)
    mark_val   = exam_cfg.get("correctMarkValue", 1)
    neg        = exam_cfg.get("negativeMarking", False)
    neg_val    = exam_cfg.get("negativeMarkValue", 0)

    subjects_data = topic_picker.pick_subjects_for_full_length(class_slug)
    if not subjects_data:
        raise ValueError(f"No subjects found for {class_slug}")

    uid   = make_id()
    title = f"{exam_name} — Full Length Mock Test"

    print(f"\n  [Full-Length] Building: {title}")
    print(f"  Subjects: {[s['subjectName'] for s in subjects_data]}")

    raw_pairs = _build_full_length_questions(subjects_data, exam_name)

    if not raw_pairs or len(raw_pairs) < max(10, total_q // 4):
        raise Exception(f"Insufficient questions generated: {len(raw_pairs)}/{total_q}")

    questions = []
    for i, (raw_q, subj_slug) in enumerate(raw_pairs[:total_q]):
        questions.append(validate_question(raw_q, i, mark_val, neg_val, neg, subj_slug))

    neg_instr = f"Wrong answer: -{neg_val} mark" if neg else "No negative marking"
    payload = {
        "id": uid, "title": title,
        "description": f"Full-length mock test for {exam_name}. {len(questions)} questions across all subjects.",
        "board": board, "classSlug": class_slug, "classNumber": 0,
        "subject": "all", "chapter": "Full Syllabus", "topic": "Full Length",
        "testType": "full-length",
        "duration": duration, "totalMarks": total_m,
        "passingMarks": round(total_m * 0.35),
        "totalQuestions": len(questions),
        "isPublic": True, "isPublished": True, "isAdmin": True,
        "showCorrectAnswers": True, "randomizeQuestions": True,
        "unlimitedAttempts": True, "allowedAttempts": 0,
        "tags": [class_slug, "full-length", "mock-test", "all-subjects"],
        "instructions": [
            f"Total Questions: {len(questions)}",
            f"Total Marks: {total_m}",
            f"Duration: {duration} minutes",
            f"Correct answer: +{mark_val} mark",
            neg_instr,
        ],
        "createdBy": "admin", "createdByName": "EduTest Admin",
        "publishedAt": now_iso(), "questions": questions
    }
    path = admin_path("tests", uid, title)
    save_file(path, payload)

    entry = {
        "id": uid, "type": "test", "title": title, "description": payload["description"],
        "subject": "all", "board": board, "classSlug": class_slug, "classNumber": 0,
        "chapter": "Full Syllabus", "testType": "full-length",
        "totalQuestions": len(questions), "duration": duration, "totalMarks": total_m,
        "author": "EduTest Admin", "authorId": "admin", "isAdmin": True,
        "tags": payload["tags"], "cdnUrl": cdn_url(path), "path": path,
        "publishedAt": payload["publishedAt"]
    }
    return uid, path, entry


def build_subject_test(topic_info):
    """Subject/topic-wise test using the full 4-agent pipeline (highest quality)."""
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

    print(f"\n  [Subject Test] Building: {title}")
    raw_qs = _build_subject_questions(topic_info, q_count, exam_name)

    if not raw_qs or len(raw_qs) < max(1, q_count // 3):
        raise Exception(f"Insufficient questions: {len(raw_qs)}/{q_count}")

    questions = [validate_question(q, i, mark_val, neg_val, neg, subj_slug)
                 for i, q in enumerate(raw_qs[:q_count])]

    payload = {
        "id": uid, "title": title,
        "description": f"Topic-wise test on {topic} for {exam_name}. {len(questions)} questions.",
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
        "instructions": [f"Topic: {topic}", f"Duration: {duration} mins",
                         f"+{mark_val} per correct answer"],
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
    class_slug = topic_info["classSlug"]
    subj_slug  = topic_info["subjectSlug"]
    subj_name  = topic_info["subjectName"]
    chapter    = topic_info["chapter"]
    topic      = topic_info["topic"]
    board      = topic_info["board"]
    exam_name  = class_slug.upper().replace("-", " ")
    uid   = make_id()
    title = f"{subj_name} — {topic} | Complete Notes"

    print(f"\n  [Groq] Building Note: {title}")
    prompt = (
        f"Write comprehensive study notes for {exam_name} as HTML.\n"
        f"Subject: {subj_name} | Chapter: {chapter} | Topic: {topic}\n"
        "Length: 1500-2000 words. Structure:\n"
        f"<h2>{topic}</h2>\n"
        "<h3>1. Overview & Definition</h3> — detailed explanation\n"
        "<h3>2. Key Concepts</h3> — use <strong> for terms, <ul><li> for lists\n"
        "<h3>3. Formulas / Values / Classifications</h3> — use <code> for formulas\n"
        "<h3>4. Clinical / Practical Applications</h3>\n"
        "<h3>5. Exam Tips & Common Mistakes</h3>\n"
        "Use <table> for comparisons. Write ONLY HTML, no markdown."
    )
    content = groq_client.generate_note(prompt, exam_name, subj_name, expect_json=False)
    if not content or len(content) < 500:
        raise GroqError("Note too short or failed.")

    payload = {
        "id": uid, "title": title,
        "description": f"Comprehensive notes on {topic} for {exam_name}.",
        "type": "note", "board": board, "classSlug": class_slug, "classNumber": 0,
        "subject": subj_slug, "chapter": chapter,
        "tags": make_tags(class_slug, subj_slug, chapter, topic),
        "createdBy": "admin", "createdByName": "EduTest Admin",
        "publishedAt": now_iso(), "isPublic": True, "isPublished": True, "isAdmin": True,
        "content": content, "imageUrls": []
    }
    path = admin_path("notes", uid, title)
    save_file(path, payload)

    entry = {
        "id": uid, "type": "note", "title": title, "description": payload["description"],
        "subject": subj_slug, "board": board, "classSlug": class_slug, "classNumber": 0,
        "chapter": chapter, "author": "EduTest Admin", "authorId": "admin", "isAdmin": True,
        "tags": payload["tags"], "cdnUrl": cdn_url(path), "path": path,
        "publishedAt": payload["publishedAt"]
    }
    return uid, path, entry


def build_formula(topic_info):
    class_slug = topic_info["classSlug"]
    subj_slug  = topic_info["subjectSlug"]
    subj_name  = topic_info["subjectName"]
    chapter    = topic_info["chapter"]
    topic      = topic_info["topic"]
    board      = topic_info["board"]
    exam_name  = class_slug.upper().replace("-", " ")
    uid   = make_id()
    title = f"{subj_name} — {topic} | Quick Reference"

    print(f"\n  [Groq] Building Formula: {title}")
    # FIX: all lines are f-strings so {topic} and {subj_name} are correctly substituted
    prompt = (
        f"Create a {exam_name} quick-reference sheet for {topic} ({subj_name}).\n"
        f"Write HTML with <h2>{topic} — Quick Reference</h2>, "
        f"<h3>Key Formulas / Values</h3> using <code> for each formula, "
        f"<h3>Mnemonics</h3> in <em>, <h3>Key Points</h3> as <ul><li>.\n"
        "Return ONLY this JSON (no markdown fences):\n"
        '{\"content\": \"<h2>...</h2>...full HTML...\"}'
    )
    res     = groq_client.generate_formula(prompt, exam_name, subj_name, expect_json=True)
    content = res.get("content", "") if isinstance(res, dict) else ""
    if not content or len(content) < 200:
        raise GroqError("Formula generation failed or too short.")

    payload = {
        "id": uid, "title": title,
        "description": f"Quick reference formula sheet for {topic}.",
        "type": "formula", "board": board, "classSlug": class_slug, "classNumber": 0,
        "subject": subj_slug, "chapter": chapter,
        "tags": make_tags(class_slug, subj_slug, chapter, topic),
        "createdBy": "admin", "createdByName": "EduTest Admin",
        "publishedAt": now_iso(), "isPublic": True, "isPublished": True, "isAdmin": True,
        "content": content, "imageUrls": []
    }
    path = admin_path("formulas", uid, title)
    save_file(path, payload)

    entry = {
        "id": uid, "type": "formula", "title": title, "description": payload["description"],
        "subject": subj_slug, "board": board, "classSlug": class_slug, "classNumber": 0,
        "chapter": chapter, "author": "EduTest Admin", "authorId": "admin", "isAdmin": True,
        "tags": payload["tags"], "cdnUrl": cdn_url(path), "path": path,
        "publishedAt": payload["publishedAt"]
    }
    return uid, path, entry


def build_resource(topic_info):
    class_slug = topic_info["classSlug"]
    subj_slug  = topic_info["subjectSlug"]
    subj_name  = topic_info["subjectName"]
    chapter    = topic_info["chapter"]
    topic      = topic_info["topic"]
    board      = topic_info["board"]
    exam_name  = class_slug.upper().replace("-", " ")
    uid   = make_id()
    title = f"{subj_name} — {topic} | Study Resources"

    print(f"\n  [Groq] Building Resource: {title}")
    prompt = (
        f"Create a curated study resource guide for {topic} ({subj_name}, {exam_name}).\n"
        "Write structured HTML summarising the best ways to study this topic. "
        "Then list 3-5 reference links (Wikipedia, NCERT, standard textbook chapters — use generic real URLs).\n"
        "Return ONLY this JSON:\n"
        '{\"content\": \"<h2>...</h2>...HTML...\", '
        '\"links\": [{\"title\": \"...\", \"url\": \"https://...\", \"type\": \"article\"}]}'
    )
    res     = groq_client.generate_resource(prompt, exam_name, subj_name, expect_json=True)
    content = res.get("content", "") if isinstance(res, dict) else ""
    links   = res.get("links", [])   if isinstance(res, dict) else []
    if not content or len(content) < 100:
        raise GroqError("Resource generation failed or too short.")

    payload = {
        "id": uid, "title": title,
        "description": f"Curated study resources for {topic}.",
        "type": "resource", "board": board, "classSlug": class_slug, "classNumber": 0,
        "subject": subj_slug, "chapter": chapter,
        "tags": make_tags(class_slug, subj_slug, chapter, topic),
        "createdBy": "admin", "createdByName": "EduTest Admin",
        "publishedAt": now_iso(), "isPublic": True, "isPublished": True, "isAdmin": True,
        "content": content, "links": links, "imageUrls": []
    }
    path = admin_path("resources", uid, title)
    save_file(path, payload)

    entry = {
        "id": uid, "type": "resource", "title": title, "description": payload["description"],
        "subject": subj_slug, "board": board, "classSlug": class_slug, "classNumber": 0,
        "chapter": chapter, "author": "EduTest Admin", "authorId": "admin", "isAdmin": True,
        "tags": payload["tags"], "cdnUrl": cdn_url(path), "path": path,
        "publishedAt": payload["publishedAt"]
    }
    return uid, path, entry

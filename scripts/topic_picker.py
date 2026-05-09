"""
topic_picker.py — Smart topic selector with no-repeat tracking.

Picks randomly from ALL available exam topic files (competitive + school).
Handles modular structure (index.json + subjects/*.json).

No-repeat guarantee:
  Each (classSlug, subjectSlug, chapter, topic) combo is tracked in state.json.
  When ALL topics for a class are exhausted, the tracker resets automatically
  and a new cycle begins — so the same subject can be picked again but with
  fresh topic selection, ensuring question variety across cycles.

  The cycle number is stored per class so generation prompts can reference it
  ("this is cycle 2 — generate different questions from cycle 1").
"""
import os, json, random
from datetime import datetime, timezone

ROOT       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOPICS_DIR = os.path.join(ROOT, "topics")
STATE_FILE = os.path.join(ROOT, "scripts", "state.json")


# ── Slug normalisation ─────────────────────────────────────────────────────────
# Maps any user-supplied variant → canonical classSlug (folder name).
# Keys are lowercased + stripped; values are the exact folder names.
_SLUG_ALIASES = {
    # JEE Mains
    "jee":              "jee-mains",
    "jee-main":         "jee-mains",
    "jee-mains":        "jee-mains",
    "jeemains":         "jee-mains",
    "jeemain":          "jee-mains",
    "jee mains":        "jee-mains",
    "jee main":         "jee-mains",
    # JEE Advanced
    "jee-advance":      "jee-advanced",
    "jee-advanced":     "jee-advanced",
    "jeeadvanced":      "jee-advanced",
    "jeeadvance":       "jee-advanced",
    "jee advance":      "jee-advanced",
    "jee advanced":     "jee-advanced",
    # NEET
    "neet":             "neet-ug",
    "neet-ug":          "neet-ug",
    "neetug":           "neet-ug",
    "neet ug":          "neet-ug",
    "neet-pg":          "neet-pg",
    "neetpg":           "neet-pg",
    # UPSC
    "upsc":             "upsc-cse",
    "upsc-cse":         "upsc-cse",
    "upsccse":          "upsc-cse",
    # SSC
    "ssc":              "ssc-cgl",
    "ssc-cgl":          "ssc-cgl",
    "ssccgl":           "ssc-cgl",
    "ssc-chsl":         "ssc-chsl",
    "ssc-mts":          "ssc-mts",
    # GATE
    "gate":             "gate",
    # CAT
    "cat":              "cat",
    # CUET
    "cuet":             "cuet-ug",
    "cuet-ug":          "cuet-ug",
    "cuet-pg":          "cuet-pg",
    # IBPS
    "ibps":             "ibps-po",
    "ibps-po":          "ibps-po",
    "ibps-clerk":       "ibps-clerk",
    # SBI
    "sbi":              "sbi-po",
    "sbi-po":           "sbi-po",
    # RRB
    "rrb":              "rrb-ntpc",
    "rrb-ntpc":         "rrb-ntpc",
    "rrb-group-d":      "rrb-group-d",
    # NDA / CDS / CLAT / CTET / KVPY / DSSSB / NORCET
    "nda":              "nda",
    "cds":              "cds",
    "clat":             "clat",
    "ctet":             "ctet",
    "kvpy":             "kvpy",
    "dsssb":            "dsssb",
    "norcet":           "norcet",
    # Olympiads
    "olympiad":         "olympiad-math",
    "olympiad-math":    "olympiad-math",
    "olympiad-science": "olympiad-science",
}


def normalize_exam_slug(raw: str) -> str:
    """
    Convert any user-supplied exam name to the canonical classSlug.

    Handles:
      - Case insensitivity:  JEE-ADVANCED → jee-advanced
      - Spacing variants:    "jee advance" → jee-advanced
      - Missing hyphens:     jeeadvanced   → jee-advanced
      - Short aliases:       jee           → jee-mains
      - Already correct:     jee-mains     → jee-mains

    Falls back to the lowercased, hyphenated form if no alias matches,
    so unknown-but-valid slugs (e.g. a new exam folder) still work.
    """
    if not raw:
        return raw
    # Normalise: lowercase, collapse whitespace/underscores to hyphens
    key = raw.strip().lower().replace("_", "-").replace("  ", " ")
    # Try alias lookup (with and without spaces-as-hyphens)
    if key in _SLUG_ALIASES:
        return _SLUG_ALIASES[key]
    key_hyphen = key.replace(" ", "-")
    if key_hyphen in _SLUG_ALIASES:
        return _SLUG_ALIASES[key_hyphen]
    # Return hyphenated lowercase as best-effort canonical form
    return key_hyphen


# ── State I/O ──────────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "generated_tests": [],
        "generated_notes": [],
        "generated_formulas": [],
        "generated_resources": [],
        "cycles": {},          # classSlug → cycle number (increments on reset)
    }


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def get_cycle(state, class_slug):
    """Return the current generation cycle number for a class (starts at 1)."""
    return state.get("cycles", {}).get(class_slug, 1)


def _increment_cycle(state, class_slug):
    """Increment cycle counter when topics are exhausted and reset."""
    state.setdefault("cycles", {})
    state["cycles"][class_slug] = state["cycles"].get(class_slug, 1) + 1
    print(f"  [TopicPicker] 🔄 {class_slug} cycle reset → cycle {state['cycles'][class_slug]}")


# ── Topic file loaders ─────────────────────────────────────────────────────────

def _load_modular_data(base_path, class_slug):
    """Load index.json and resolve all subject files."""
    index_path = os.path.join(base_path, "index.json")
    if not os.path.exists(index_path):
        return None
    with open(index_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    resolved_subjects = []
    for s in data.get("subjects", []):
        sub_file = s.get("file")
        if sub_file:
            sub_path = os.path.join(base_path, sub_file)
            if os.path.exists(sub_path):
                with open(sub_path, "r", encoding="utf-8") as sf:
                    sub_data = json.load(sf)
                    # Merge properties from index into the loaded subject
                    sub_data.update(s)
                    resolved_subjects.append(sub_data)
        else:
            resolved_subjects.append(s)
    
    data["subjects"] = resolved_subjects
    return data

def load_topics(class_slug):
    """
    Load topics JSON for a classSlug.
    Handles both modular dirs and flat json files.
    If no topic data exists at all, auto-generates and saves at least 10 topics
    per subject using the AI pipeline before returning.
    """
    data = _load_topics_raw(class_slug)
    if data and _has_topics(data):
        return data

    # ── No usable topic data — auto-generate via AI ───────────────────────────
    print(f"  [TopicPicker] ⚠️  No topic data for '{class_slug}' — auto-generating topics...")
    data = _generate_and_save_topics(class_slug, existing_data=data)
    return data


def _load_topics_raw(class_slug):
    """Internal: load topic data without auto-generation fallback."""
    # 1. Check competitive directory modular
    comp_dir = os.path.join(TOPICS_DIR, "competitive", class_slug)
    if os.path.isdir(comp_dir):
        return _load_modular_data(comp_dir, class_slug)

    # 2. Check competitive flat file
    comp_file = os.path.join(TOPICS_DIR, "competitive", class_slug + ".json")
    if os.path.exists(comp_file):
        with open(comp_file, "r", encoding="utf-8") as f:
            return json.load(f)

    # 3. Check school boards modular
    for board in ["cbse", "icse", "maharashtra", "up-board", "isc"]:
        if class_slug.startswith(board):
            num = class_slug.replace(board + "-class-", "")
            school_dir = os.path.join(TOPICS_DIR, "school", board, f"class-{num}")
            if os.path.isdir(school_dir):
                return _load_modular_data(school_dir, class_slug)

            # 4. Check school flat file
            school_file = os.path.join(TOPICS_DIR, "school", board, f"class-{num}.json")
            if os.path.exists(school_file):
                with open(school_file, "r", encoding="utf-8") as f:
                    return json.load(f)
    return None


# ── AI-powered topic generation ────────────────────────────────────────────────

def _generate_and_save_topics(class_slug, existing_data=None):
    """
    Use Gemini to generate at least 10 topics per subject for an exam that has
    no topic data. Saves the result to topics/competitive/<class_slug>/index.json
    and subjects/*.json so subsequent runs can use it normally.

    Returns the generated topics_data dict (or a minimal fallback on failure).
    """
    try:
        import gemini_client
    except ImportError:
        print("  [TopicPicker] gemini_client not available — using minimal fallback topics")
        return _minimal_fallback_topics(class_slug)

    exam_name = class_slug.upper().replace("-", " ")

    # If we have partial data (index.json exists but subjects are empty), use it
    # as a skeleton; otherwise build from scratch.
    if existing_data and existing_data.get("subjects"):
        subjects_meta = [
            {"name": s.get("name", s.get("slug", "General")),
             "slug": s.get("slug", s.get("name", "general").lower().replace(" ", "-"))}
            for s in existing_data["subjects"]
        ]
    else:
        # Ask AI what subjects this exam has
        subjects_meta = _ai_get_subjects(gemini_client, exam_name, class_slug)

    if not subjects_meta:
        print(f"  [TopicPicker] Could not determine subjects for {class_slug} — using fallback")
        return _minimal_fallback_topics(class_slug)

    # Generate chapters + topics for each subject
    generated_subjects = []
    for subj in subjects_meta:
        subj_name = subj["name"]
        subj_slug = subj["slug"]
        print(f"  [TopicPicker] Generating topics for {exam_name} — {subj_name}...")
        chapters = _ai_get_chapters_and_topics(gemini_client, exam_name, subj_name)
        if not chapters:
            # Minimal fallback for this subject
            chapters = [{"name": "General Topics",
                         "topics": [f"Topic {i+1}" for i in range(10)]}]
        generated_subjects.append({
            "slug": subj_slug,
            "name": subj_name,
            "icon": subj.get("icon", "📚"),
            "color": subj.get("color", "#6366f1"),
            "weightage": subj.get("weightage", round(100 / len(subjects_meta))),
            "chapters": chapters,
        })

    # Build the full topics_data structure
    exam_config = (existing_data or {}).get("examConfig") or _default_exam_config(class_slug)
    board       = (existing_data or {}).get("board", "Competitive")
    exam_type   = (existing_data or {}).get("type", "competitive")

    topics_data = {
        "classSlug":  class_slug,
        "board":      board,
        "type":       exam_type,
        "examConfig": exam_config,
        "subjects":   generated_subjects,
    }

    # Persist to disk so future runs load it directly
    _save_generated_topics(class_slug, topics_data, exam_type)
    total_topics = sum(
        len(t) for s in generated_subjects
        for c in s["chapters"]
        for t in [c.get("topics", [])]
    )
    print(f"  [TopicPicker] ✅ Auto-generated {total_topics} topics across "
          f"{len(generated_subjects)} subjects for {class_slug}")
    return topics_data


def _ai_get_subjects(gemini_client, exam_name, class_slug):
    """Ask AI for the list of subjects in this exam. Returns list of {name, slug, ...}."""
    prompt = (
        f"List the main subjects tested in the {exam_name} exam.\n"
        f"Output ONLY a JSON array, no explanation. Each item: "
        f'{{\"name\": \"Physics\", \"slug\": \"physics\", \"icon\": \"⚛️\", '
        f'\"color\": \"#3b82f6\", \"weightage\": 33}}\n'
        f"Include weightage (%) that sums to 100. Typical exams have 2-5 subjects."
    )
    try:
        raw = gemini_client.research(prompt, max_tokens=800)
        if not raw:
            return None
        # Extract JSON array from response
        import re
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        print(f"  [TopicPicker] Subject AI call failed: {e}")
    return None


def _ai_get_chapters_and_topics(gemini_client, exam_name, subject_name):
    """
    Ask AI for chapters and at least 10 topics per chapter for this exam+subject.
    Returns list of {name, topics: [...]} dicts.
    """
    prompt = (
        f"List the key chapters and topics for {subject_name} in the {exam_name} exam.\n"
        f"Requirements:\n"
        f"- At least 8 chapters\n"
        f"- At least 10 topics per chapter (specific, exam-relevant topic names)\n"
        f"- Topics should reflect actual {exam_name} syllabus\n"
        f"Output ONLY a JSON array, no explanation:\n"
        f'[{{"name": "Chapter Name", "topics": ["Topic 1", "Topic 2", ...]}}]\n'
        f"Be specific and comprehensive. No generic placeholders."
    )
    try:
        raw = gemini_client.research(prompt, max_tokens=3000)
        if not raw:
            return None
        import re
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if match:
            chapters = json.loads(match.group())
            # Ensure each chapter has at least 10 topics
            for ch in chapters:
                topics = ch.get("topics", [])
                if len(topics) < 10:
                    # Pad with generic but named topics
                    while len(topics) < 10:
                        topics.append(f"{ch['name']} — Advanced Concepts {len(topics)+1}")
                    ch["topics"] = topics
            return chapters
    except Exception as e:
        print(f"  [TopicPicker] Chapters AI call failed: {e}")
    return None


def _save_generated_topics(class_slug, topics_data, exam_type):
    """Save generated topics to the topics/ directory in modular format."""
    if exam_type == "school":
        # School: topics/school/<board>/class-<num>/
        # For now save as competitive flat file — school slugs are more complex
        out_dir  = os.path.join(TOPICS_DIR, "competitive")
        out_file = os.path.join(out_dir, class_slug + ".json")
    else:
        out_dir  = os.path.join(TOPICS_DIR, "competitive", class_slug)
        out_file = os.path.join(out_dir, "index.json")

    os.makedirs(out_dir, exist_ok=True)

    # For modular format: save index.json (without chapters) + subjects/*.json
    if exam_type != "school":
        subjects_dir = os.path.join(out_dir, "subjects")
        os.makedirs(subjects_dir, exist_ok=True)

        index_subjects = []
        for subj in topics_data["subjects"]:
            subj_slug = subj["slug"]
            subj_file = f"subjects/{subj_slug}.json"
            # Save subject file with chapters
            subj_path = os.path.join(out_dir, subj_file)
            subj_data = {
                "slug":      subj_slug,
                "name":      subj["name"],
                "icon":      subj.get("icon", "📚"),
                "color":     subj.get("color", "#6366f1"),
                "weightage": subj.get("weightage", 33),
                "classSlug": class_slug,
                "board":     topics_data.get("board", "Competitive"),
                "chapters":  subj.get("chapters", []),
            }
            with open(subj_path, "w", encoding="utf-8") as f:
                json.dump(subj_data, f, indent=2, ensure_ascii=False)
            print(f"  [TopicPicker] Saved {subj_path}")

            index_subjects.append({
                "slug":      subj_slug,
                "name":      subj["name"],
                "icon":      subj.get("icon", "📚"),
                "color":     subj.get("color", "#6366f1"),
                "weightage": subj.get("weightage", 33),
                "file":      subj_file,
            })

        # Save index.json (no chapters — those are in subject files)
        index_data = {k: v for k, v in topics_data.items() if k != "subjects"}
        index_data["subjects"] = index_subjects
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(index_data, f, indent=2, ensure_ascii=False)
        print(f"  [TopicPicker] Saved {out_file}")
    else:
        # Flat file for school or fallback
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(topics_data, f, indent=2, ensure_ascii=False)
        print(f"  [TopicPicker] Saved {out_file}")


def _default_exam_config(class_slug):
    """Return a sensible default examConfig for an unknown exam."""
    return {
        "fullLengthQuestions": 100,
        "fullLengthMarks": 400,
        "fullLengthDuration": 180,
        "subjectWiseQuestions": 30,
        "subjectWiseMarks": 120,
        "subjectWiseDuration": 60,
        "negativeMarking": False,
        "correctMarkValue": 4,
        "negativeMarkValue": 1,
        "subjectWiseQMin": 20,
        "subjectWiseQMax": 40,
        "combinedTestQPerSubject": 20,
    }


def _minimal_fallback_topics(class_slug):
    """Return a minimal in-memory topics structure when AI is unavailable."""
    exam_name = class_slug.upper().replace("-", " ")
    return {
        "classSlug":  class_slug,
        "board":      "Competitive",
        "type":       "competitive",
        "examConfig": _default_exam_config(class_slug),
        "subjects": [{
            "slug":      f"{class_slug}-general",
            "name":      "General",
            "icon":      "📚",
            "color":     "#6366f1",
            "weightage": 100,
            "chapters": [{
                "name":   "Core Concepts",
                "topics": [f"{exam_name} Topic {i+1}" for i in range(10)],
            }],
        }],
    }


def get_all_available_exams():
    """Return all classSlug values that have at least one subject with chapters+topics."""
    exams = []
    for sub in ["competitive", "school"]:
        base = os.path.join(TOPICS_DIR, sub)
        if not os.path.exists(base):
            continue
        
        # Check directories (modular)
        for d in os.listdir(base):
            d_path = os.path.join(base, d)
            if os.path.isdir(d_path):
                # School structure is school/<board>/class-<num>
                if sub == "school":
                    for sd in os.listdir(d_path):
                        sd_path = os.path.join(d_path, sd)
                        if os.path.isdir(sd_path) and sd.startswith("class-"):
                            slug = f"{d}-{sd}"
                            data = _load_modular_data(sd_path, slug)
                            if _has_topics(data):
                                exams.append(slug)
                        elif sd_path.endswith(".json"):
                            slug = f"{d}-{sd.replace('.json', '')}"
                            try:
                                with open(sd_path, "r", encoding="utf-8") as fh:
                                    data = json.load(fh)
                                if _has_topics(data):
                                    exams.append(slug)
                            except: pass
                else: # Competitive
                    data = _load_modular_data(d_path, d)
                    if _has_topics(data):
                        exams.append(d)
            elif d.endswith(".json"):
                slug = d.replace(".json", "")
                try:
                    with open(d_path, "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                    if _has_topics(data):
                        exams.append(slug)
                except: pass
    return exams if exams else ["norcet"]


def _has_topics(data):
    if not data: return False
    return any(
        any(len(ch.get("topics", [])) > 0 for ch in s.get("chapters", []))
        for s in data.get("subjects", [])
    )


# ── Used-key helpers ───────────────────────────────────────────────────────────

def _used_key(ct, class_slug, subj_slug, chapter, topic):
    return f"{ct}|{class_slug}|{subj_slug}|{chapter}|{topic}"


def _is_used(state, ct, class_slug, subj_slug, chapter, topic):
    key   = _used_key(ct, class_slug, subj_slug, chapter, topic)
    store = state.get(f"generated_{ct}s", [])
    return any(e.get("key") == key for e in store)


def _mark_used(state, ct, class_slug, subj_slug, chapter, topic, gen_ids):
    key       = _used_key(ct, class_slug, subj_slug, chapter, topic)
    store_key = f"generated_{ct}s"
    state.setdefault(store_key, []).append({
        "key": key, "classSlug": class_slug, "subject": subj_slug,
        "chapter": chapter, "topic": topic, "ids": gen_ids,
        "generatedAt": __import__("datetime").datetime.utcnow().isoformat() + "Z"
    })


# ── Core pickers ───────────────────────────────────────────────────────────────

def _build_available(state, ct, topics_data, class_slug):
    """Return list of all unused (subject, chapter, topic) combos for a classSlug."""
    available = []
    for subj in topics_data.get("subjects", []):
        for chapter in subj.get("chapters", []):
            for topic in chapter.get("topics", []):
                if not _is_used(state, ct, class_slug, subj["slug"], chapter["name"], topic):
                    available.append({
                        "classSlug":   class_slug,
                        "board":       topics_data.get("board", "Competitive"),
                        "type":        topics_data.get("type", "competitive"),
                        "examConfig":  topics_data.get("examConfig", {}),
                        "subjectSlug": subj["slug"],
                        "subjectName": subj["name"],
                        "subjectIcon": subj.get("icon", "📚"),
                        "subjectColor": subj.get("color", "#3b82f6"),
                        "chapter":     chapter["name"],
                        "topic":       topic,
                    })
    return available


def _reset_and_rebuild(state, ct, topics_data, class_slug):
    """Clear used entries for this class+ct, increment cycle, rebuild available list."""
    store_key = f"generated_{ct}s"
    used_count = sum(1 for e in state.get(store_key, []) if e.get("classSlug") == class_slug)
    state[store_key] = [e for e in state.get(store_key, []) if e.get("classSlug") != class_slug]
    _increment_cycle(state, class_slug)
    save_state(state)
    total = sum(
        len(t) for s in topics_data.get("subjects", [])
        for c in s.get("chapters", [])
        for t in [c.get("topics", [])]
    )
    print(f"  [TopicPicker] Reset {class_slug}: cleared {used_count} used entries, {total} topics available again")
    return _build_available(state, ct, topics_data, class_slug)


def pick_random_exam():
    """Pick a random exam slug from all available exams."""
    return random.choice(get_all_available_exams())


def pick_topic(ct, class_slug=None):
    """
    Pick one unused topic. Randomly selects exam if class_slug is None.
    Returns (topic_info, state). topic_info includes 'cycle' so the
    generation prompt can vary questions across cycles.
    """
    state = load_state()
    if class_slug is None:
        class_slug = random.choice(get_all_available_exams())
    topics_data = load_topics(class_slug)
    if not topics_data:
        raise ValueError(f"No topics for {class_slug}")
    available = _build_available(state, ct, topics_data, class_slug)
    if not available:
        print(f"  [TopicPicker] All {class_slug} topics used — resetting cycle")
        available = _reset_and_rebuild(state, ct, topics_data, class_slug)
    if not available:
        raise ValueError(f"No topics at all for {class_slug}")
    topic = random.choice(available)
    topic["cycle"] = get_cycle(state, class_slug)
    return topic, state


def pick_topic_from_exam(ct, class_slug):
    """Pick one unused topic from a specific exam (for same-exam runs)."""
    return pick_topic(ct, class_slug=class_slug)


def pick_multiple_from_exam(ct, class_slug, count=2):
    """Pick `count` unused topics from a specific exam, preferring different subjects."""
    results = []
    for _ in range(count):
        try:
            topic, state = pick_topic_from_exam(ct, class_slug)
            results.append((topic, state))
        except Exception as e:
            print(f"  [topic_picker] Could not pick topic {len(results)+1}/{count}: {e}")
            break
    return results


def pick_multiple_topics(ct, class_slug=None, count=3, different_subjects=True):
    """Pick multiple unused topics. If class_slug is None picks from a random exam."""
    state = load_state()
    if class_slug is None:
        class_slug = random.choice(get_all_available_exams())
    topics_data = load_topics(class_slug)
    if not topics_data:
        raise ValueError(f"No topics for {class_slug}")

    available = _build_available(state, ct, topics_data, class_slug)
    if len(available) < count:
        available = _reset_and_rebuild(state, ct, topics_data, class_slug)

    random.shuffle(available)

    if different_subjects:
        picked, used_subjs = [], set()
        for item in available:
            if item["subjectSlug"] not in used_subjs:
                picked.append(item)
                used_subjs.add(item["subjectSlug"])
            if len(picked) == count:
                break
        # fill remaining from any subject
        for item in available:
            if len(picked) == count:
                break
            if item not in picked:
                picked.append(item)
    else:
        picked = available[:count]

    return picked, state


def mark_generated(state, ct, items_with_ids):
    """Mark topics as generated. items_with_ids: [(topic_dict, id_str), ...]"""
    for topic_dict, gen_id in items_with_ids:
        _mark_used(
            state, ct,
            topic_dict["classSlug"], topic_dict["subjectSlug"],
            topic_dict["chapter"],   topic_dict["topic"],
            [gen_id]
        )
    save_state(state)


def pick_subjects_for_full_length(class_slug="norcet"):
    """Return all subjects with question distribution for a full-length test."""
    topics_data = load_topics(class_slug)
    if not topics_data:
        return []
    subjects    = topics_data.get("subjects", [])
    total_q     = topics_data.get("examConfig", {}).get("fullLengthQuestions", 100)
    total_weight = sum(s.get("weightage", 1) for s in subjects) or 1
    result = []
    for subj in subjects:
        w       = subj.get("weightage", 1)
        q_count = max(1, round((w / total_weight) * total_q))
        result.append({
            "subjectSlug":  subj["slug"],
            "subjectName":  subj["name"],
            "questionCount": q_count,
            "chapters":     subj.get("chapters", []),
        })
    return result

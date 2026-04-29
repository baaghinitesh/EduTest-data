"""
topic_picker.py — Smart topic/subject selector with no-repeat tracking.

New folder-per-exam structure:
  topics/competitive/norcet/
    index.json          ← config + testTypes + subject stubs
    subjects/
      norcet-msn.json   ← full chapters + topics
      ...
    full-length.json    ← subject distribution for full-length generation

  topics/school/cbse/class-10/
    index.json
    subjects/
      mathematics.json
      ...
    full-length.json
"""
import os, json, random

ROOT       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOPICS_DIR = os.path.join(ROOT, "topics")
STATE_FILE = os.path.join(ROOT, "scripts", "state.json")

# ── State helpers ─────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "generated_tests":    [],
        "generated_notes":    [],
        "generated_formulas": [],
        "topic_exhaustion":   {}
    }

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

# ── Path resolution ───────────────────────────────────────────────────────────

def _exam_dir(class_slug):
    """
    Return the folder path for a given classSlug.
    Supports:
      competitive: topics/competitive/{slug}/
      school:      topics/school/{board}/class-{n}/
                   where class_slug = "{board}-class-{n}"
    """
    # Competitive
    comp = os.path.join(TOPICS_DIR, "competitive", class_slug)
    if os.path.isdir(comp):
        return comp

    # School boards — slug format: cbse-class-10, up-board-class-9, etc.
    for board in ["cbse", "icse", "isc", "maharashtra", "up-board"]:
        prefix = board + "-class-"
        if class_slug.startswith(prefix):
            num = class_slug[len(prefix):]
            path = os.path.join(TOPICS_DIR, "school", board, "class-" + num)
            if os.path.isdir(path):
                return path

    return None

def _load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

# ── Public loaders ────────────────────────────────────────────────────────────

def load_index(class_slug):
    """Load index.json for an exam/class. Returns None if not found."""
    d = _exam_dir(class_slug)
    if not d:
        return None
    idx = os.path.join(d, "index.json")
    if not os.path.exists(idx):
        return None
    return _load_json(idx)

def load_subject(class_slug, subject_slug):
    """Load a single subject file. Returns None if not found."""
    d = _exam_dir(class_slug)
    if not d:
        return None
    path = os.path.join(d, "subjects", subject_slug + ".json")
    if not os.path.exists(path):
        return None
    return _load_json(path)

def load_all_subjects(class_slug):
    """
    Load all subject files for an exam.
    Returns list of subject dicts (with chapters+topics).
    """
    index = load_index(class_slug)
    if not index:
        return []
    d = _exam_dir(class_slug)
    subjects = []
    for stub in index.get("subjects", []):
        slug = stub["slug"]
        subj = load_subject(class_slug, slug)
        if subj:
            subjects.append(subj)
        else:
            # Subject file missing — use stub (no chapters)
            subjects.append(stub)
    return subjects

def load_topics(class_slug):
    """
    Compatibility shim — returns a merged dict that looks like the old flat JSON.
    Used by generate.py and content_builder.py.
    """
    index = load_index(class_slug)
    if not index:
        return None
    subjects = load_all_subjects(class_slug)
    if not subjects:
        return None
    return {
        "classSlug":  index.get("classSlug", class_slug),
        "board":      index.get("board", "Competitive"),
        "type":       index.get("type", "competitive"),
        "examConfig": index.get("examConfig", {}),
        "testTypes":  index.get("testTypes", []),
        "subjects":   subjects
    }

def load_full_length_config(class_slug):
    """Load full-length.json for a class."""
    d = _exam_dir(class_slug)
    if not d:
        return None
    path = os.path.join(d, "full-length.json")
    if not os.path.exists(path):
        return None
    return _load_json(path)

def get_all_available_exams():
    """Return list of all classSlug values that have subjects defined."""
    exams = []
    comp_dir = os.path.join(TOPICS_DIR, "competitive")
    if os.path.exists(comp_dir):
        for name in sorted(os.listdir(comp_dir)):
            if os.path.isdir(os.path.join(comp_dir, name)):
                idx = load_index(name)
                if idx and idx.get("subjects"):
                    exams.append(name)
    return exams

# ── State key helpers ─────────────────────────────────────────────────────────

def _used_key(content_type, class_slug, subject_slug, chapter, topic):
    return f"{content_type}|{class_slug}|{subject_slug}|{chapter}|{topic}"

def _is_used(state, content_type, class_slug, subject_slug, chapter, topic):
    key   = _used_key(content_type, class_slug, subject_slug, chapter, topic)
    store = state.get("generated_" + content_type + "s", [])
    return any(e.get("key") == key for e in store)

def _mark_used(state, content_type, class_slug, subject_slug, chapter, topic, generated_ids):
    key       = _used_key(content_type, class_slug, subject_slug, chapter, topic)
    store_key = "generated_" + content_type + "s"
    if store_key not in state:
        state[store_key] = []
    state[store_key].append({
        "key":         key,
        "classSlug":   class_slug,
        "subject":     subject_slug,
        "chapter":     chapter,
        "topic":       topic,
        "ids":         generated_ids,
        "generatedAt": __import__("datetime").datetime.utcnow().isoformat() + "Z"
    })

# ── Build available pool ──────────────────────────────────────────────────────

def _build_pool(state, content_type, class_slug, subjects, index):
    """Build list of all (subject, chapter, topic) combos not yet used."""
    exam_config = index.get("examConfig", {})
    board       = index.get("board", "Competitive")
    type_       = index.get("type", "competitive")
    available   = []
    for subj in subjects:
        for chapter in subj.get("chapters", []):
            for topic in chapter.get("topics", []):
                if not _is_used(state, content_type, class_slug, subj["slug"], chapter["name"], topic):
                    available.append({
                        "classSlug":   class_slug,
                        "board":       board,
                        "type":        type_,
                        "examConfig":  exam_config,
                        "subjectSlug": subj["slug"],
                        "subjectName": subj["name"],
                        "subjectIcon": subj.get("icon", "📚"),
                        "subjectColor":subj.get("color", "#3b82f6"),
                        "chapter":     chapter["name"],
                        "topic":       topic
                    })
    return available

def _reset_pool(state, content_type, class_slug):
    """Clear generated entries for this class+type so we can cycle again."""
    store_key = "generated_" + content_type + "s"
    state[store_key] = [
        e for e in state.get(store_key, [])
        if e.get("classSlug") != class_slug
    ]
    save_state(state)

# ── Public pickers ────────────────────────────────────────────────────────────

def pick_topic(content_type, class_slug=None, subject_slug=None):
    """
    Pick a single topic for generation.
    content_type: 'test', 'note', 'formula'
    Returns (topic_dict, state).
    """
    state = load_state()
    if class_slug is None:
        class_slug = "norcet"

    index    = load_index(class_slug)
    subjects = load_all_subjects(class_slug)

    if not index or not subjects:
        raise ValueError(f"No topics found for {class_slug}")

    # Filter to specific subject if requested
    if subject_slug:
        subjects = [s for s in subjects if s["slug"] == subject_slug]
        if not subjects:
            raise ValueError(f"Subject {subject_slug} not found in {class_slug}")

    available = _build_pool(state, content_type, class_slug, subjects, index)

    if not available:
        print(f"  [TopicPicker] All topics exhausted for {class_slug}/{content_type}, resetting...")
        _reset_pool(state, content_type, class_slug)
        available = _build_pool(state, content_type, class_slug, subjects, index)

    if not available:
        raise ValueError(f"No topics available at all for {class_slug}")

    return random.choice(available), state

def pick_multiple_topics(content_type, class_slug=None, count=4, different_subjects=True):
    """
    Pick multiple distinct topics for batch generation.
    Returns (list_of_topic_dicts, state).
    """
    state = load_state()
    if class_slug is None:
        class_slug = "norcet"

    index    = load_index(class_slug)
    subjects = load_all_subjects(class_slug)

    if not index or not subjects:
        raise ValueError(f"No topics found for {class_slug}")

    available = _build_pool(state, content_type, class_slug, subjects, index)

    if len(available) < count:
        print(f"  [TopicPicker] Not enough topics ({len(available)}<{count}), resetting {class_slug}/{content_type}...")
        _reset_pool(state, content_type, class_slug)
        available = _build_pool(state, content_type, class_slug, subjects, index)

    random.shuffle(available)

    if different_subjects:
        picked        = []
        used_subjects = set()
        for item in available:
            if item["subjectSlug"] not in used_subjects:
                picked.append(item)
                used_subjects.add(item["subjectSlug"])
            if len(picked) == count:
                break
        # Fill remaining from any subject if needed
        if len(picked) < count:
            for item in available:
                if item not in picked:
                    picked.append(item)
                if len(picked) == count:
                    break
    else:
        picked = available[:count]

    return picked, state

def mark_generated(state, content_type, items_with_ids):
    """Mark topics as generated. items_with_ids: list of (topic_dict, id_string)"""
    for topic_dict, gen_id in items_with_ids:
        _mark_used(
            state, content_type,
            topic_dict["classSlug"],
            topic_dict["subjectSlug"],
            topic_dict["chapter"],
            topic_dict["topic"],
            [gen_id]
        )
    save_state(state)

def pick_subjects_for_full_length(class_slug="norcet"):
    """
    For full-length test generation.
    Loads full-length.json (pre-computed distribution) and enriches with chapter data.
    Falls back to computing from index if full-length.json is missing.
    """
    fl = load_full_length_config(class_slug)
    index = load_index(class_slug)

    if not index:
        return []

    subjects_full = load_all_subjects(class_slug)
    subj_map = {s["slug"]: s for s in subjects_full}

    if fl and fl.get("subjects"):
        # Use pre-computed distribution, enrich with chapters
        result = []
        for entry in fl["subjects"]:
            slug = entry["subjectSlug"]
            subj = subj_map.get(slug, {})
            result.append({
                "subjectSlug":   slug,
                "subjectName":   entry.get("subjectName", subj.get("name", slug)),
                "questionCount": entry.get("questionCount", 1),
                "chapters":      subj.get("chapters", [])
            })
        return result

    # Fallback: compute from index weightages
    exam_config  = index.get("examConfig", {})
    total_q      = exam_config.get("fullLengthQuestions", 100)
    total_weight = sum(s.get("weightage", 1) for s in subjects_full)
    result = []
    for subj in subjects_full:
        w = subj.get("weightage", 1)
        q = max(1, round((w / total_weight) * total_q)) if total_weight else 1
        result.append({
            "subjectSlug":   subj["slug"],
            "subjectName":   subj["name"],
            "questionCount": q,
            "chapters":      subj.get("chapters", [])
        })
    return result

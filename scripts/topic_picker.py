"""
topic_picker.py — Smart topic selector with no-repeat tracking.

Picks randomly from ALL available exam topic files (competitive + school).
Falls back to norcet if nothing else is available.
"""
import os, json, random

ROOT       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOPICS_DIR = os.path.join(ROOT, "topics")
STATE_FILE = os.path.join(ROOT, "scripts", "state.json")


# ── State I/O ──────────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"generated_tests": [], "generated_notes": [], "generated_formulas": [], "generated_resources": []}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ── Topic file loaders ─────────────────────────────────────────────────────────

def load_topics(class_slug):
    """Load topics JSON for a classSlug. Checks competitive/ then school/ dirs."""
    path = os.path.join(TOPICS_DIR, "competitive", class_slug + ".json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    # School: cbse-class-10 → topics/school/cbse/class-10.json
    for board in ["cbse", "icse", "maharashtra", "up-board", "isc"]:
        if class_slug.startswith(board):
            num  = class_slug.replace(board + "-class-", "")
            path = os.path.join(TOPICS_DIR, "school", board, f"class-{num}.json")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
    return None


def get_all_available_exams():
    """Return all classSlug values that have at least one subject with chapters+topics."""
    exams = []
    for sub in ["competitive", "school"]:
        base = os.path.join(TOPICS_DIR, sub)
        if not os.path.exists(base):
            continue
        for root, dirs, files in os.walk(base):
            for f in files:
                if not f.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(root, f), "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                    # Must have at least one subject with at least one chapter with topics
                    if any(
                        any(len(ch.get("topics", [])) > 0 for ch in s.get("chapters", []))
                        for s in data.get("subjects", [])
                    ):
                        slug = data.get("classSlug") or f.replace(".json", "")
                        exams.append(slug)
                except Exception:
                    pass
    return exams if exams else ["norcet"]


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
    """Clear used entries for this class+ct, rebuild available list."""
    store_key = f"generated_{ct}s"
    state[store_key] = [e for e in state.get(store_key, []) if e.get("classSlug") != class_slug]
    save_state(state)
    return _build_available(state, ct, topics_data, class_slug)


def pick_topic(ct, class_slug=None):
    """Pick one unused topic. Randomly selects exam if class_slug is None."""
    state = load_state()
    if class_slug is None:
        class_slug = random.choice(get_all_available_exams())
    topics_data = load_topics(class_slug)
    if not topics_data:
        raise ValueError(f"No topics for {class_slug}")
    available = _build_available(state, ct, topics_data, class_slug)
    if not available:
        available = _reset_and_rebuild(state, ct, topics_data, class_slug)
    if not available:
        raise ValueError(f"No topics at all for {class_slug}")
    return random.choice(available), state


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

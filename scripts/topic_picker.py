"""
topic_picker.py — Smart topic/subject selector with no-repeat tracking.
Reads topics/ JSON files and state.json to pick what to generate next.
"""
import os, json, random

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOPICS_DIR = os.path.join(ROOT, "topics")
STATE_FILE = os.path.join(ROOT, "scripts", "state.json")

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "generated_tests": [],
        "generated_notes": [],
        "generated_formulas": [],
        "topic_exhaustion": {}
    }

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def load_topics(class_slug):
    """Load topics JSON for a given classSlug. Returns None if not found."""
    # Try competitive first
    path = os.path.join(TOPICS_DIR, "competitive", class_slug + ".json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    # Try school boards
    for board in ["cbse", "icse", "maharashtra", "up-board", "isc"]:
        # classSlug format: cbse-class-10
        if class_slug.startswith(board):
            num = class_slug.replace(board + "-class-", "")
            path = os.path.join(TOPICS_DIR, "school", board, "class-" + num + ".json")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
    return None

def get_all_available_exams():
    """Return list of all classSlug values that have topics defined."""
    exams = []
    comp_dir = os.path.join(TOPICS_DIR, "competitive")
    if os.path.exists(comp_dir):
        for f in os.listdir(comp_dir):
            if f.endswith(".json"):
                slug = f.replace(".json", "")
                data = load_topics(slug)
                if data and data.get("subjects"):
                    exams.append(slug)
    return exams

def _used_key(content_type, class_slug, subject_slug, chapter, topic):
    return f"{content_type}|{class_slug}|{subject_slug}|{chapter}|{topic}"

def _is_used(state, content_type, class_slug, subject_slug, chapter, topic):
    key = _used_key(content_type, class_slug, subject_slug, chapter, topic)
    store = state.get("generated_" + content_type + "s", [])
    return any(
        e.get("key") == key for e in store
    )

def _mark_used(state, content_type, class_slug, subject_slug, chapter, topic, generated_ids):
    key = _used_key(content_type, class_slug, subject_slug, chapter, topic)
    store_key = "generated_" + content_type + "s"
    if store_key not in state:
        state[store_key] = []
    state[store_key].append({
        "key": key,
        "classSlug": class_slug,
        "subject": subject_slug,
        "chapter": chapter,
        "topic": topic,
        "ids": generated_ids,
        "generatedAt": __import__("datetime").datetime.utcnow().isoformat() + "Z"
    })

def pick_topic(content_type, class_slug=None, subject_slug=None):
    """
    Pick a topic for generation. Returns dict with all needed info.
    content_type: 'test', 'note', 'formula'
    If class_slug is None, picks from NORCET (primary exam).
    """
    state = load_state()

    # Default to NORCET if no class specified
    if class_slug is None:
        class_slug = "norcet"

    topics_data = load_topics(class_slug)
    if not topics_data or not topics_data.get("subjects"):
        raise ValueError(f"No topics found for {class_slug}")

    subjects = topics_data["subjects"]
    exam_config = topics_data.get("examConfig", {})

    # Filter to specific subject if requested
    if subject_slug:
        subjects = [s for s in subjects if s["slug"] == subject_slug]
        if not subjects:
            raise ValueError(f"Subject {subject_slug} not found in {class_slug}")

    # Build list of all (subject, chapter, topic) combos not yet used
    available = []
    for subj in subjects:
        for chapter in subj.get("chapters", []):
            for topic in chapter.get("topics", []):
                if not _is_used(state, content_type, class_slug, subj["slug"], chapter["name"], topic):
                    available.append({
                        "classSlug": class_slug,
                        "board": topics_data.get("board", "Competitive"),
                        "type": topics_data.get("type", "competitive"),
                        "examConfig": exam_config,
                        "subjectSlug": subj["slug"],
                        "subjectName": subj["name"],
                        "subjectIcon": subj.get("icon", "📚"),
                        "subjectColor": subj.get("color", "#3b82f6"),
                        "chapter": chapter["name"],
                        "topic": topic
                    })

    # If all topics exhausted, reset and start over
    if not available:
        print(f"  [TopicPicker] All topics exhausted for {class_slug}/{content_type}, resetting...")
        # Remove all entries for this class+content_type
        store_key = "generated_" + content_type + "s"
        state[store_key] = [
            e for e in state.get(store_key, [])
            if e.get("classSlug") != class_slug
        ]
        save_state(state)
        # Rebuild available
        for subj in subjects:
            for chapter in subj.get("chapters", []):
                for topic in chapter.get("topics", []):
                    available.append({
                        "classSlug": class_slug,
                        "board": topics_data.get("board", "Competitive"),
                        "type": topics_data.get("type", "competitive"),
                        "examConfig": exam_config,
                        "subjectSlug": subj["slug"],
                        "subjectName": subj["name"],
                        "subjectIcon": subj.get("icon", "📚"),
                        "subjectColor": subj.get("color", "#3b82f6"),
                        "chapter": chapter["name"],
                        "topic": topic
                    })

    if not available:
        raise ValueError(f"No topics available at all for {class_slug}")

    return random.choice(available), state

def pick_multiple_topics(content_type, class_slug=None, count=4, different_subjects=True):
    """Pick multiple distinct topics for batch generation (subject-wise tests, notes, formulas)."""
    state = load_state()
    if class_slug is None:
        class_slug = "norcet"

    topics_data = load_topics(class_slug)
    if not topics_data or not topics_data.get("subjects"):
        raise ValueError(f"No topics found for {class_slug}")

    subjects = topics_data["subjects"]
    exam_config = topics_data.get("examConfig", {})

    # Build all available combos
    available = []
    for subj in subjects:
        for chapter in subj.get("chapters", []):
            for topic in chapter.get("topics", []):
                if not _is_used(state, content_type, class_slug, subj["slug"], chapter["name"], topic):
                    available.append({
                        "classSlug": class_slug,
                        "board": topics_data.get("board", "Competitive"),
                        "type": topics_data.get("type", "competitive"),
                        "examConfig": exam_config,
                        "subjectSlug": subj["slug"],
                        "subjectName": subj["name"],
                        "subjectIcon": subj.get("icon", "📚"),
                        "subjectColor": subj.get("color", "#3b82f6"),
                        "chapter": chapter["name"],
                        "topic": topic
                    })

    # If not enough, reset
    if len(available) < count:
        store_key = "generated_" + content_type + "s"
        state[store_key] = [e for e in state.get(store_key, []) if e.get("classSlug") != class_slug]
        save_state(state)
        available = []
        for subj in subjects:
            for chapter in subj.get("chapters", []):
                for topic in chapter.get("topics", []):
                    available.append({
                        "classSlug": class_slug,
                        "board": topics_data.get("board", "Competitive"),
                        "type": topics_data.get("type", "competitive"),
                        "examConfig": exam_config,
                        "subjectSlug": subj["slug"],
                        "subjectName": subj["name"],
                        "subjectIcon": subj.get("icon", "📚"),
                        "subjectColor": subj.get("color", "#3b82f6"),
                        "chapter": chapter["name"],
                        "topic": topic
                    })

    random.shuffle(available)

    if different_subjects:
        # Try to pick from different subjects
        picked = []
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
    """Mark topics as generated in state. items_with_ids: list of (topic_dict, id_string)"""
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
    """For full-length test: return all subjects with their weightage for question distribution."""
    topics_data = load_topics(class_slug)
    if not topics_data:
        return []
    subjects = topics_data.get("subjects", [])
    exam_config = topics_data.get("examConfig", {})
    total_q = exam_config.get("fullLengthQuestions", 100)

    result = []
    total_weight = sum(s.get("weightage", 1) for s in subjects)
    for subj in subjects:
        weight = subj.get("weightage", 1)
        q_count = max(1, round((weight / total_weight) * total_q))
        result.append({
            "subjectSlug": subj["slug"],
            "subjectName": subj["name"],
            "questionCount": q_count,
            "chapters": subj.get("chapters", [])
        })
    return result
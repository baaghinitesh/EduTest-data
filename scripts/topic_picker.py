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
    """Load topics JSON for a classSlug. Handles both modular dirs and flat json files."""
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

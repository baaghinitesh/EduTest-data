"""
migrate_topics.py — One-time migration script.

Splits each flat topics JSON into the new folder-per-exam structure:

  OLD: topics/competitive/norcet.json  (one big file)
  NEW: topics/competitive/norcet/
         index.json          ← config + testTypes + subject stubs
         subjects/
           norcet-msn.json   ← full chapters + topics
           norcet-fon.json
           ...
         full-length.json    ← subject distribution for full-length generation

  OLD: topics/school/cbse/class-10.json
  NEW: topics/school/cbse/class-10/
         index.json
         subjects/
           mathematics.json
           science.json
           ...

Run once: python scripts/migrate_topics.py
"""
import os, json, shutil

ROOT       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOPICS_DIR = os.path.join(ROOT, "topics")

def save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  [saved] {os.path.relpath(path, ROOT)}")

def migrate_exam(flat_path, exam_dir):
    """Split one flat JSON into index.json + subjects/*.json + full-length.json"""
    with open(flat_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    subjects = data.get("subjects", [])
    exam_config = data.get("examConfig", {})

    # ── index.json ────────────────────────────────────────────────────────────
    subject_stubs = []
    for s in subjects:
        subject_stubs.append({
            "slug":      s["slug"],
            "name":      s["name"],
            "icon":      s.get("icon", "📚"),
            "color":     s.get("color", "#3b82f6"),
            "weightage": s.get("weightage", 1),
            "file":      f"subjects/{s['slug']}.json"
        })

    index = {
        "classSlug":  data.get("classSlug"),
        "board":      data.get("board"),
        "type":       data.get("type"),
        "examConfig": exam_config,
        "testTypes":  data.get("testTypes", []),
        "subjects":   subject_stubs
    }
    save(os.path.join(exam_dir, "index.json"), index)

    # ── subjects/*.json ───────────────────────────────────────────────────────
    for s in subjects:
        subj_data = {
            "slug":      s["slug"],
            "name":      s["name"],
            "icon":      s.get("icon", "📚"),
            "color":     s.get("color", "#3b82f6"),
            "weightage": s.get("weightage", 1),
            "classSlug": data.get("classSlug"),
            "board":     data.get("board"),
            "chapters":  s.get("chapters", [])
        }
        save(os.path.join(exam_dir, "subjects", f"{s['slug']}.json"), subj_data)

    # ── full-length.json ──────────────────────────────────────────────────────
    total_q     = exam_config.get("fullLengthQuestions", 100)
    total_weight = sum(s.get("weightage", 1) for s in subjects)
    fl_subjects = []
    for s in subjects:
        w = s.get("weightage", 1)
        q = max(1, round((w / total_weight) * total_q)) if total_weight else 1
        fl_subjects.append({
            "subjectSlug":   s["slug"],
            "subjectName":   s["name"],
            "questionCount": q
        })

    full_length = {
        "classSlug":    data.get("classSlug"),
        "board":        data.get("board"),
        "totalQuestions": total_q,
        "totalMarks":   exam_config.get("fullLengthMarks", total_q),
        "duration":     exam_config.get("fullLengthDuration", 90),
        "subjects":     fl_subjects
    }
    save(os.path.join(exam_dir, "full-length.json"), full_length)

    print(f"  → {len(subjects)} subjects split into {exam_dir}")

def run():
    migrated = 0

    # ── Competitive exams ─────────────────────────────────────────────────────
    comp_dir = os.path.join(TOPICS_DIR, "competitive")
    for fname in sorted(os.listdir(comp_dir)):
        if not fname.endswith(".json"):
            continue
        slug     = fname[:-5]
        flat     = os.path.join(comp_dir, fname)
        exam_dir = os.path.join(comp_dir, slug)

        # Skip if already migrated
        if os.path.isdir(exam_dir):
            print(f"[SKIP] {slug} — already a folder")
            continue

        print(f"\n[Migrate] competitive/{slug}")
        migrate_exam(flat, exam_dir)
        # Keep old flat file as .bak for safety
        shutil.move(flat, flat + ".bak")
        print(f"  [bak]  {fname} → {fname}.bak")
        migrated += 1

    # ── School boards ─────────────────────────────────────────────────────────
    school_dir = os.path.join(TOPICS_DIR, "school")
    for board in sorted(os.listdir(school_dir)):
        board_dir = os.path.join(school_dir, board)
        if not os.path.isdir(board_dir):
            continue
        for fname in sorted(os.listdir(board_dir)):
            if not fname.endswith(".json"):
                continue
            class_name = fname[:-5]          # e.g. "class-10"
            flat       = os.path.join(board_dir, fname)
            class_dir  = os.path.join(board_dir, class_name)

            if os.path.isdir(class_dir):
                print(f"[SKIP] school/{board}/{class_name} — already a folder")
                continue

            print(f"\n[Migrate] school/{board}/{class_name}")
            migrate_exam(flat, class_dir)
            shutil.move(flat, flat + ".bak")
            print(f"  [bak]  {fname} → {fname}.bak")
            migrated += 1

    print(f"\n✅ Migration complete — {migrated} exams/classes migrated.")
    print("   Old flat files renamed to *.bak — delete them once verified.")

if __name__ == "__main__":
    run()

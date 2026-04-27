"""
catalog_updater.py — Updates data/catalog/index.json with new entries.
"""
import os, json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_FILE = os.path.join(ROOT, "data", "catalog", "index.json")

def load_index():
    if os.path.exists(INDEX_FILE):
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    return []

def save_index(index):
    os.makedirs(os.path.dirname(INDEX_FILE), exist_ok=True)
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)

def add_entries(entries):
    """Add or update entries in the catalog index. entries: list of dicts."""
    index = load_index()
    existing_ids = {e["id"]: i for i, e in enumerate(index)}
    for entry in entries:
        eid = entry.get("id")
        if eid and eid in existing_ids:
            index[existing_ids[eid]] = entry  # update
        else:
            index.insert(0, entry)  # prepend newest first
    save_index(index)
    print(f"  [Catalog] Updated index: {len(entries)} entries added/updated, total={len(index)}")

def remove_entry(entry_id):
    index = load_index()
    index = [e for e in index if e.get("id") != entry_id]
    save_index(index)
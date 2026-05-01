"""
backfill_to_server.py — One-time script to publish all existing generated content
to the EduTest server (MongoDB) so it appears in the app.

Reads every JSON file under data/admin/ and calls the appropriate server endpoint:
  - tests/     → POST /api/github/publish/test
  - notes/     → POST /api/github/publish/note
  - formulas/  → POST /api/github/publish/note  (type=formula)
  - resources/ → POST /api/github/publish/note  (type=resource)

Run locally:
  cd EduTest-data
  set EDUTEST_API_URL=https://serverprepguide.launchyourconcept.com
  set EDUTEST_ADMIN_EMAIL=baaghinitesh@gmail.com
  set EDUTEST_ADMIN_PASSWORD=nitesh@1234
  python scripts/backfill_to_server.py

Or run via GitHub Actions workflow_dispatch with mode=backfill.
"""
import os, sys, json, time, re
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN   = os.path.join(ROOT, "data", "admin")
API_URL = os.environ.get("EDUTEST_API_URL",
                         "https://serverprepguide.launchyourconcept.com").rstrip("/")
EMAIL   = os.environ.get("EDUTEST_ADMIN_EMAIL", "")
PASSWD  = os.environ.get("EDUTEST_ADMIN_PASSWORD", "")
CDN_BASE = "https://prepguidedata.launchyourconcept.com"

_token    = None
_token_ts = 0.0


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _post(endpoint, payload, token=None, retries=3):
    url  = f"{API_URL}{endpoint}"
    data = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            last_err = f"HTTP {e.code}: {body[:300]}"
            print(f"    attempt {attempt}/{retries}: {last_err}")
            if e.code in (400, 401, 403):
                break
            if attempt < retries:
                time.sleep(5 * attempt)
        except Exception as e:
            last_err = str(e)[:150]
            print(f"    attempt {attempt}/{retries} error: {last_err}")
            if attempt < retries:
                time.sleep(5 * attempt)
    raise RuntimeError(f"POST {endpoint} failed: {last_err}")


def _get_token():
    global _token, _token_ts
    if _token and (time.time() - _token_ts) < 6 * 3600:
        return _token
    if not EMAIL or not PASSWD:
        raise RuntimeError("EDUTEST_ADMIN_EMAIL / EDUTEST_ADMIN_PASSWORD not set")
    print(f"Authenticating as {EMAIL}...")
    resp  = _post("/api/auth/admin-token", {"email": EMAIL, "password": PASSWD})
    token = resp.get("token")
    if not token:
        raise RuntimeError(f"No token in response: {resp}")
    _token    = token
    _token_ts = time.time()
    role = resp.get("user", {}).get("role", "?")
    print(f"✅ Authenticated (role: {role})\n")
    return _token


# ── File discovery ────────────────────────────────────────────────────────────

def _find_all_files():
    """Walk data/admin/ and return list of (abs_path, rel_path, content_type)."""
    results = []
    if not os.path.isdir(ADMIN):
        print(f"No data/admin/ directory found at {ADMIN}")
        return results

    for dirpath, _, filenames in os.walk(ADMIN):
        for fname in filenames:
            if not fname.endswith(".json"):
                continue
            abs_path = os.path.join(dirpath, fname)
            # Relative path from repo root (forward slashes)
            rel_path = os.path.relpath(abs_path, ROOT).replace("\\", "/")

            # Determine content type from folder name
            parts = rel_path.split("/")
            # parts: ['data', 'admin', '<exam>', '<type>', '<file>.json']
            folder = parts[3] if len(parts) >= 4 else ""
            if folder == "tests":
                ctype = "test"
            elif folder == "notes":
                ctype = "note"
            elif folder == "formulas":
                ctype = "formula"
            elif folder == "resources":
                ctype = "resource"
            else:
                ctype = "note"  # default

            results.append((abs_path, rel_path, ctype))

    return sorted(results)


# ── Publish one file ──────────────────────────────────────────────────────────

def _publish_file(abs_path, rel_path, ctype, token):
    """Load JSON and publish to server. Returns True/False."""
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"  ❌ Failed to read {rel_path}: {e}")
        return False

    # Ensure cdnUrl and path are set correctly
    cdn_url = f"{CDN_BASE}/{rel_path}"
    data["cdnUrl"]     = cdn_url
    data["path"]       = rel_path
    data["githubPath"] = rel_path
    data["isPublished"] = True
    data["isAdmin"]     = True
    data["isPublic"]    = True

    try:
        if ctype == "test":
            resp = _post("/api/github/publish/test",
                         {"test": data, "isAdmin": True}, token=token)
        else:
            # notes, formulas, resources all go through publish/note
            data["type"] = ctype
            resp = _post("/api/github/publish/note",
                         {"note": data, "isAdmin": True}, token=token)

        db_id = resp.get("dbId", "N/A")
        print(f"  ✅ {ctype.upper()} — dbId={db_id}")
        return True

    except Exception as e:
        print(f"  ❌ Publish failed: {e}")
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    print("=" * 65)
    print("  EduTest Backfill — Publishing existing content to MongoDB")
    print(f"  Server: {API_URL}")
    print("=" * 65 + "\n")

    if not EMAIL or not PASSWD:
        print("❌ EDUTEST_ADMIN_EMAIL and EDUTEST_ADMIN_PASSWORD must be set.")
        print("   Set them as environment variables and re-run.")
        sys.exit(1)

    try:
        token = _get_token()
    except Exception as e:
        print(f"❌ Authentication failed: {e}")
        sys.exit(1)

    files = _find_all_files()
    if not files:
        print("No files found in data/admin/ — nothing to backfill.")
        sys.exit(0)

    print(f"Found {len(files)} file(s) to publish:\n")

    ok = 0
    fail = 0
    for abs_path, rel_path, ctype in files:
        print(f"[{ctype.upper()}] {rel_path}")
        if _publish_file(abs_path, rel_path, ctype, token):
            ok += 1
        else:
            fail += 1
        time.sleep(0.5)   # small delay to avoid overwhelming the server

    print(f"\n{'=' * 65}")
    print(f"  Backfill complete: {ok} published, {fail} failed")
    print(f"{'=' * 65}\n")

    if fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    run()

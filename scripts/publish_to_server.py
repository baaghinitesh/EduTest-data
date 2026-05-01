"""
publish_to_server.py — Publishes generated tests to the EduTest server (MongoDB).

Flow:
  1. POST /api/auth/admin-token  → get JWT (token in response body)
  2. POST /api/github/publish/test  → creates MongoDB record with cdnUrl + githubPath
     so the test appears in the app immediately via /api/content

Required GitHub Actions secrets:
  EDUTEST_API_URL        → https://serverprepguide.launchyourconcept.com
  EDUTEST_ADMIN_EMAIL    → baaghinitesh@gmail.com
  EDUTEST_ADMIN_PASSWORD → nitesh@1234

If any env var is missing, publish is skipped gracefully.
Content is still saved to CDN — it just won't appear in the app until published.
"""
import os, json, time
import urllib.request
import urllib.error

# ── Config from env ───────────────────────────────────────────────────────────
# Default to the correct server URL so it works even if secret is not set
API_URL   = os.environ.get("EDUTEST_API_URL", "https://serverprepguide.launchyourconcept.com").rstrip("/")
ADM_EMAIL = os.environ.get("EDUTEST_ADMIN_EMAIL", "")
ADM_PASS  = os.environ.get("EDUTEST_ADMIN_PASSWORD", "")

_token    = None   # cached JWT for this run
_token_ts = 0.0


def _configured():
    # API_URL has a default, so only check credentials
    return bool(ADM_EMAIL and ADM_PASS)


def _http_post(endpoint, payload, token=None, retries=3):
    """POST JSON to server. Returns parsed response dict or raises RuntimeError."""
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
            print(f"  [Publish] attempt {attempt}/{retries}: {last_err}")
            if e.code in (400, 401, 403):
                break   # no point retrying auth/validation errors
            if attempt < retries:
                time.sleep(5 * attempt)
        except Exception as e:
            last_err = str(e)[:150]
            print(f"  [Publish] attempt {attempt}/{retries} error: {last_err}")
            if attempt < retries:
                time.sleep(5 * attempt)

    raise RuntimeError(f"POST {endpoint} failed: {last_err}")


def _get_token():
    """Get JWT via /api/auth/admin-token. Cached for 6 hours."""
    global _token, _token_ts
    if _token and (time.time() - _token_ts) < 6 * 3600:
        return _token

    print(f"  [Publish] Authenticating as {ADM_EMAIL}...")
    resp  = _http_post("/api/auth/admin-token",
                       {"email": ADM_EMAIL, "password": ADM_PASS})
    token = resp.get("token")
    if not token:
        raise RuntimeError(f"No token in response: {resp}")

    _token    = token
    _token_ts = time.time()
    role = resp.get("user", {}).get("role", "?")
    print(f"  [Publish] ✅ Authenticated (role: {role})")
    return _token


def publish_test(test_data, file_path):
    """
    Publish one generated test to the EduTest server.

    test_data : dict — full test JSON (as saved to disk by content_builder.py)
    file_path : str  — relative CDN path, e.g. "data/admin/neet-ug/tests/xxx.json"

    Returns True on success, False on failure (never raises).
    """
    if not _configured():
        print("  [Publish] Server credentials not set — skipping (content on CDN only)")
        return False

    try:
        token = _get_token()
    except Exception as e:
        print(f"  [Publish] Auth failed ({e}) — skipping server publish")
        return False

    try:
        resp = _http_post(
            "/api/github/publish/test",
            {"test": test_data, "isAdmin": True},
            token=token,
        )
        db_id   = resp.get("dbId", "N/A")
        cdn_url = resp.get("cdnUrl", "")
        print(f"  [Publish] ✅ MongoDB record created — dbId={db_id}")
        if cdn_url:
            print(f"  [Publish]    cdnUrl={cdn_url[:70]}...")
        return True

    except Exception as e:
        print(f"  [Publish] ❌ Failed: {e}")
        return False

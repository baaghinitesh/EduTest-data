"""
publish_to_server.py — Publishes generated tests to the PrepGuide Lite server (MongoDB).

Flow:
  1. POST /api/auth/admin-token  → get JWT (token in response body)
  2. POST /api/github/publish/test  → creates MongoDB record with cdnUrl + githubPath
     so the test appears in the app immediately via /api/content

Uses `requests.Session` with browser-compatible headers so Cloudflare Browser
Integrity Check does not block Python's default `Python-urllib` User-Agent
(Error 1010).
"""
import os, time
import requests

from run_budget import RunTimeoutError, safe_sleep

API_URL   = os.environ.get("EDUTEST_API_URL", "https://serverprepguide.launchyourconcept.com").rstrip("/")
ADM_EMAIL = os.environ.get("EDUTEST_ADMIN_EMAIL", "")
ADM_PASS  = os.environ.get("EDUTEST_ADMIN_PASSWORD", "")

_token    = None
_token_ts = 0.0
_session  = None


def _get_session():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://prepguide.launchyourconcept.com",
            "Referer": "https://prepguide.launchyourconcept.com/",
        })
    return _session


def _configured():
    return bool(ADM_EMAIL and ADM_PASS)


def _http_post(endpoint, payload, token=None, retries=3):
    """POST JSON to server. Returns parsed response dict or raises RuntimeError."""
    url     = f"{API_URL}{endpoint}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    session = _get_session()

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.post(url, json=payload, headers=headers, timeout=30)
            if 200 <= resp.status_code < 300:
                return resp.json()

            body = resp.text[:300]
            last_err = f"HTTP {resp.status_code}: {body}"
            print(f"  [Publish] attempt {attempt}/{retries}: {last_err}")
            if resp.status_code in (400, 401, 403):
                break  # don't retry auth / validation / Cloudflare block errors
            if attempt < retries:
                safe_sleep(5 * attempt, "publish retry")
        except RunTimeoutError:
            raise
        except Exception as e:
            last_err = str(e)[:150]
            print(f"  [Publish] attempt {attempt}/{retries} error: {last_err}")
            if attempt < retries:
                safe_sleep(5 * attempt, "publish retry")

    raise RuntimeError(f"POST {endpoint} failed: {last_err}")


def _get_token():
    """Get JWT via /api/auth/admin-token. Cached for 6 hours."""
    global _token, _token_ts
    if _token and (time.time() - _token_ts) < 6 * 3600:
        return _token

    print("  [Publish] Authenticating with server...")
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
    Publish one generated test to the PrepGuide Lite server.
    Returns True on success, False on failure (never raises except RunTimeoutError).
    """
    if not _configured():
        print("  [Publish] Server credentials not set — skipping (content on CDN only)")
        return False

    try:
        token = _get_token()
    except RunTimeoutError:
        raise
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

    except RunTimeoutError:
        raise
    except Exception as e:
        print(f"  [Publish] ❌ Failed: {e}")
        return False

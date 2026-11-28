# GitHub as Database + Cloudflare CDN

## Why GitHub?

Instead of storing test content in MongoDB (costly at scale), PrepGuide uses GitHub as a free content store:

- **Free storage** — public GitHub repos are free
- **Version history** — every publish is a git commit
- **Global CDN** — Cloudflare Pages auto-deploys on every push, serving from 300+ edge locations
- **Zero read cost** — CDN reads are free and fast

MongoDB only stores **metadata** (title, subject, classSlug, cdnUrl, author) — small, cheap, searchable.

---

## Repository Structure

```
edutest-data/
├── _headers                     ← Cloudflare CORS: Access-Control-Allow-Origin: *
├── test.json                    ← Health check
└── data/
    ├── catalog/
    │   └── index.json           ← Searchable index of ALL content
    ├── admin/
    │   ├── tests/{testId}.json
    │   └── notes/{noteId}.json
    └── users/{userId}/
        ├── tests/{testId}.json
        └── notes/{noteId}.json
```

---

## CDN URL

Base: `https://prepguidedata.launchyourconcept.com`

```
/data/catalog/index.json
/data/admin/tests/test-math-class10-cbse-001.json
/data/users/userId123/tests/mytest.json
```

---

## Catalog Index Schema

Every published item has an entry in `data/catalog/index.json`:

```json
{
  "id": "test-math-class10-cbse-001",
  "type": "test",
  "title": "Mathematics — Algebra",
  "subject": "mathematics",
  "board": "CBSE",
  "classSlug": "cbse-class-10",
  "classNumber": 10,
  "chapter": "Quadratic Equations",
  "testType": "chapter",
  "totalQuestions": 10,
  "duration": 45,
  "totalMarks": 40,
  "author": "PrepGuide Admin",
  "isAdmin": true,
  "tags": ["algebra", "class10", "cbse"],
  "cdnUrl": "https://ef7551b0.edutest-data.pages.dev/data/admin/tests/test-math-class10-cbse-001.json",
  "path": "data/admin/tests/test-math-class10-cbse-001.json",
  "publishedAt": "2025-04-01T10:00:00.000Z"
}
```

The client fetches this index once (cached in IndexedDB for 5 minutes) and filters client-side.

---

## Server GitHub Service (`server/services/github.js`)

Uses `@octokit/rest`.

| Function | Description |
|----------|-------------|
| `upsertFile(path, content, message)` | Create or update a file |
| `deleteFile(path)` | Delete a file |
| `fetchFromCDN(path)` | Fetch from Cloudflare CDN |
| `queueCommit(path, content, message)` | Add to 30s batch queue |
| `flushQueue()` | Commit all queued changes |
| `updateIndex(entry)` | Update catalog index |
| `adminTestPath(id)` | `data/admin/tests/{id}.json` |
| `userTestPath(userId, id)` | `data/users/{userId}/tests/{id}.json` |

---

## Client GitHub Library (`client/src/lib/github.js`)

| Function | Description |
|----------|-------------|
| `fetchFromCDN(path)` | CDN fetch with IndexedDB cache (5 min TTL) |
| `fetchIndex()` | Fetch catalog index, returns `[]` on 404 |
| `fetchContent(path)` | Fetch a specific test/note JSON |
| `publishTest(test, isAdmin)` | POST to `/api/github/publish/test` |
| `publishNote(note, isAdmin)` | POST to `/api/github/publish/note` |
| `searchContent(query, filters)` | Filter index client-side |

---

## Publish Flow

1. User clicks "Publish" in CreateTestPage
2. Client calls `publishTest(test, isAdmin)`
3. Server `/api/github/publish/test`:
   - Pushes JSON to GitHub via Octokit
   - Saves metadata to MongoDB
   - Updates `data/catalog/index.json`
4. Cloudflare detects push → redeploys in ~30 seconds
5. Test appears on subject page (CDN index filtered by classSlug + subject)

---

## Environment Variables

**Server `.env`:**
```
GITHUB_TOKEN=ghp_...        # Personal access token with repo write
GITHUB_OWNER=baaghinitesh   # GitHub username
GITHUB_REPO=EduTest-data    # Repo name
CDN_BASE=https://prepguidedata.launchyourconcept.com
```

**Client `.env`:**
```
VITE_CDN_BASE=https://prepguidedata.launchyourconcept.com
```

---

## Adding Content Manually

To add tests/notes without the app, create JSON files in the correct path and update `data/catalog/index.json`, then push to GitHub. Cloudflare will auto-deploy.

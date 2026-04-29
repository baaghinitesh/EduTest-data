# EduTest Content Generation — Plan V2
> Review this before implementation begins. Confirm to proceed.

---

## 1. Problem with Current Setup

| Issue | Detail |
|---|---|
| `gemini-2.5-pro` free tier | **limit: 0** — completely exhausted, no free quota |
| `gemini-2.5-flash` free tier | Only 20 RPD — too low for any meaningful generation |
| Single API | One failure = entire run fails |

---

## 2. Proposed Solution: 3-API Strategy

Use **3 APIs concurrently** in each run:

| API | Model | Free Limit | Used For |
|---|---|---|---|
| **Gemini API Key 1** | `gemini-2.5-pro` | 25 RPD (preview) | 1 full-length test per run (1 call) |
| **Gemini API Key 2** | `gemini-2.5-flash` | 20 RPD | Subject-wise tests: 10/20/30/40/50Q (1-2 calls) |
| **Groq API** | `llama-3.3-70b` or `mixtral-8x7b` | 14,400 RPD FREE | Notes, formulas, resources (4-6 calls) |

> **Two Gemini API keys** = two separate Google accounts = double the quota.
> This is allowed — Google permits multiple accounts.

---

## 3. Groq — Is it good for notes/formulas?

**Yes, Groq is excellent for notes and formulas.** Here is why:

- **Llama 3.3 70B** on Groq: Very high quality, comparable to GPT-4 for structured content
- **Speed**: Groq is the fastest inference provider — 500+ tokens/second
- **Free tier**: 14,400 requests/day, 6000 tokens/minute — essentially unlimited for our use
- **Medical accuracy**: Llama 3.3 70B has strong medical knowledge for NORCET-level content
- **HTML generation**: Excellent at generating structured HTML notes with proper formatting

**Groq free tier models available:**
- `llama-3.3-70b-versatile` — best quality, use for notes
- `llama-3.1-8b-instant` — fast, use for formulas/quick reference
- `mixtral-8x7b-32768` — good for long content

---

## 4. Content Schema — What the App Expects

After analyzing the full app code, here is exactly what each content type needs:

### 4.1 Tests (consumed by TakeTestPage.jsx + ExamSubjectContentPage.jsx)

**Catalog index entry** (in `data/catalog/index.json`):
```json
{
  "id": "uuid",
  "type": "test",
  "title": "...",
  "description": "...",
  "subject": "norcet-msn",          ← subject SLUG (not name)
  "board": "Competitive",
  "classSlug": "norcet",
  "classNumber": 0,
  "chapter": "Cardiovascular System",
  "testType": "chapter|full-length|mock",
  "totalQuestions": 25,
  "duration": 30,
  "totalMarks": 25,
  "author": "EduTest Admin",
  "authorId": "admin",
  "isAdmin": true,
  "tags": ["norcet", "norcet-msn"],
  "cdnUrl": "https://prepguidedata.launchyourconcept.com/data/admin/tests/xxx.json",
  "path": "data/admin/tests/xxx.json",
  "publishedAt": "ISO timestamp"
}
```

**Full test JSON** (in `data/admin/tests/xxx.json`):
```json
{
  "id": "uuid",
  "title": "...",
  "description": "...",
  "board": "Competitive",
  "classSlug": "norcet",
  "classNumber": 0,
  "subject": "norcet-msn",
  "chapter": "...",
  "topic": "...",
  "testType": "chapter",
  "duration": 30,
  "totalMarks": 25,
  "passingMarks": 10,
  "totalQuestions": 25,
  "isPublic": true,
  "isPublished": true,
  "showCorrectAnswers": true,
  "randomizeQuestions": false,
  "unlimitedAttempts": true,
  "allowedAttempts": 0,
  "tags": [...],
  "instructions": [...],
  "createdBy": "admin",
  "createdByName": "EduTest Admin",
  "publishedAt": "ISO",
  "isAdmin": true,
  "questions": [
    {
      "id": "q-timestamp-random",
      "order": 1,
      "question": "Question text?",
      "type": "mcq",
      "options": [
        {"letter":"A","text":"...","isCorrect":false,"index":0,"imageUrl":"","explanation":"Why wrong"},
        {"letter":"B","text":"...","isCorrect":true, "index":1,"imageUrl":"","explanation":"Why correct"},
        {"letter":"C","text":"...","isCorrect":false,"index":2,"imageUrl":"","explanation":"Why wrong"},
        {"letter":"D","text":"...","isCorrect":false,"index":3,"imageUrl":"","explanation":"Why wrong"}
      ],
      "answer": "",
      "alternateAnswers": [],
      "explanation": "Overall explanation",
      "hint": "Hint text",
      "marks": 1,
      "negativeMark": 0,
      "negativeMarking": false,
      "imageUrl": "",
      "subject": "norcet-msn",
      "difficulty": "easy|medium|hard",
      "hasError": false
    }
  ]
}
```

### 4.2 Notes (consumed by NoteViewer.jsx via MaterialViewerPage.jsx)

**Critical**: NoteViewer renders `content` field using `dangerouslySetInnerHTML`.
So `content` must be **valid HTML string**.

**Catalog index entry**:
```json
{
  "id": "uuid",
  "type": "note",
  "title": "...",
  "description": "...",
  "subject": "norcet-msn",
  "board": "Competitive",
  "classSlug": "norcet",
  "classNumber": 0,
  "chapter": "...",
  "author": "EduTest Admin",
  "authorId": "admin",
  "isAdmin": true,
  "tags": [...],
  "cdnUrl": "...",
  "path": "data/admin/notes/xxx.json",
  "publishedAt": "ISO"
}
```

**Full note JSON** (in `data/admin/notes/xxx.json`):
```json
{
  "id": "uuid",
  "title": "...",
  "description": "...",
  "type": "note",
  "board": "Competitive",
  "classSlug": "norcet",
  "classNumber": 0,
  "subject": "norcet-msn",
  "chapter": "...",
  "tags": [...],
  "createdBy": "admin",
  "createdByName": "EduTest Admin",
  "publishedAt": "ISO",
  "isPublic": true,
  "isPublished": true,
  "isAdmin": true,
  "content": "<h2>Topic</h2><p>HTML content here...</p>",
  "imageUrls": []
}
```

### 4.3 Formulas (consumed by FormulaViewer.jsx)

FormulaViewer reads:
- `content` field → rendered as HTML (same as NoteViewer)
- `latex` field → rendered via KaTeX (optional, for math formulas)
- `variables[]` → array of {symbol, meaning, unit}
- `difficulty`, `category` → displayed as badges

**Full formula JSON** (in `data/admin/formulas/xxx.json`):
```json
{
  "id": "uuid",
  "title": "...",
  "description": "...",
  "type": "formula",
  "board": "Competitive",
  "classSlug": "norcet",
  "classNumber": 0,
  "subject": "norcet-msn",
  "chapter": "...",
  "tags": [...],
  "createdBy": "admin",
  "createdByName": "EduTest Admin",
  "publishedAt": "ISO",
  "isPublic": true,
  "isPublished": true,
  "isAdmin": true,
  "content": "<h2>...</h2><p>HTML content...</p>",
  "latex": "F = ma",
  "variables": [
    {"symbol": "F", "meaning": "Force", "unit": "Newton (N)"},
    {"symbol": "m", "meaning": "Mass", "unit": "kg"},
    {"symbol": "a", "meaning": "Acceleration", "unit": "m/s²"}
  ],
  "difficulty": "Basic|Intermediate|Advanced",
  "category": "Pharmacology|Anatomy|etc",
  "imageUrls": []
}
```

### 4.4 How the App Filters Content (CRITICAL)

From `ExamSubjectContentPage.jsx`:
```javascript
// CDN items are filtered by:
i.type === 'test' && i.isAdmin !== false && i.classSlug === examSlug && matchesSubject(i)

// matchesSubject checks:
subjectNames.some(s => i.subject === s || (Array.isArray(i.subjects) && i.subjects.includes(s)))

// subjectNames = [subjectSlug, sub?.name, sub?.slug]
// e.g. ["norcet-msn", "Medical Surgical Nursing", "norcet-msn"]
```

**This means**: The `subject` field in catalog index MUST match the subject SLUG
(e.g., `"norcet-msn"` not `"Medical Surgical Nursing"`).

### 4.5 Edit Flow (from MaterialViewerPage.jsx)

When admin clicks Edit:
- Tests → `/create-test?id={materialId}`
- Formulas → `/create-formula?id={materialId}`
- Notes → `/create-note?id={materialId}`

The `isOwner` check:
```javascript
const isOwner = isAdmin || (
  user && material && (
    material.createdBy?.toString() === user.id ||
    material.uploadedBy?.toString() === user.id ||
    material.authorId?.toString() === user.id ||
    material.publishedBy?.toString() === user.id
  )
);
```

**For CDN content**: `fromCDN: true` means Export button is hidden, but Edit still shows for admins.

---

## 5. Per-Run Generation Plan

### Each run generates: 1 full-length test + 2 subject tests + 4 notes + 4 formulas = 11 items

```
Run timeline (concurrent API calls):
  t=0s   → Gemini Pro Key 1:  1 full-length test (100Q, 1 call)
  t=0s   → Gemini Flash Key 2: 2 subject tests (25-50Q each, 2 calls)
  t=0s   → Groq:              4 notes (4 calls, ~2s each)
  t=0s   → Groq:              4 formulas (4 calls, ~2s each)

  All run in parallel → total time ~60-90s per run
```

### 4 runs/day = 44 items/day

| API | Calls per run | Calls per day (4 runs) | Free limit | Usage % |
|---|---|---|---|---|
| Gemini Pro (Key 1) | 1 | 4 | 25 RPD | 16% ✅ |
| Gemini Flash (Key 2) | 2 | 8 | 20 RPD | 40% ✅ |
| Groq | 8 | 32 | 14,400 RPD | 0.2% ✅ |

---

## 6. Subject-wise Test Question Counts

The app shows `totalQuestions` on the test card. We will generate varied sizes:

| Test type | Questions | Duration | Marks |
|---|---|---|---|
| Quick topic test | 10Q | 12 min | 10 |
| Short chapter test | 20Q | 25 min | 20 |
| Standard test | 30Q | 35 min | 30 |
| Extended test | 40Q | 50 min | 40 |
| Full subject test | 50Q | 60 min | 50 |

Randomly pick size per run so content is varied.

---

## 7. GitHub Secrets Required

Add these in repo Settings → Secrets → Actions:

| Secret | Value |
|---|---|
| `GEMINI_API_KEY_1` | Gemini API key from Google Account 1 (for Pro model) |
| `GEMINI_API_KEY_2` | Gemini API key from Google Account 2 (for Flash model) |
| `GROQ_API_KEY` | Groq API key from console.groq.com (free) |
| `GH_PAT` | GitHub Personal Access Token (already set) |

---

## 8. Files to Create/Update

```
scripts/
  gemini_client.py      ← UPDATE: dual key support (Key1=Pro, Key2=Flash)
  groq_client.py        ← NEW: Groq API wrapper
  content_builder.py    ← UPDATE: use correct API per content type
  generate.py           ← UPDATE: concurrent execution, 11 items/run
  requirements.txt      ← UPDATE: add groq package
  state.json            ← no change needed
  topic_picker.py       ← no change needed
  catalog_updater.py    ← no change needed
.github/workflows/
  generate-content.yml  ← UPDATE: add new secrets, 4 runs/day
```

---

## 9. Groq API Setup (Free)

1. Go to: https://console.groq.com
2. Sign up (free, no credit card)
3. Go to API Keys → Create API Key
4. Copy key (starts with `gsk_...`)
5. Add as `GROQ_API_KEY` secret in GitHub

**Groq free tier**: 14,400 requests/day, 6,000 tokens/minute — more than enough.

---

## 10. Content Editability from App

After analyzing the app code:

✅ **Notes**: Fully editable via `/create-note?id=xxx` — admin can edit title, content, tags, subject
✅ **Formulas**: Fully editable via `/create-formula?id=xxx` — admin can edit all fields
✅ **Tests**: Fully editable via `/create-test?id=xxx` — admin can edit questions, options, answers
✅ **CDN content**: Shows "Edit" button for admin users even for `fromCDN: true` items
✅ **No errors**: All required fields are present in our schema

**One important note**: CDN content (fromCDN: true) does NOT show the Export button.
This is by design — CDN content is managed via GitHub, not local export.

---

## 11. Summary

| What | Decision |
|---|---|
| Full-length tests | Gemini 2.5 Pro (Key 1) — 1 per run |
| Subject-wise tests (10-50Q) | Gemini 2.5 Flash (Key 2) — 2 per run |
| Notes | Groq Llama 3.3 70B — 4 per run |
| Formulas | Groq Llama 3.1 8B — 4 per run |
| Total per run | 11 items |
| Runs per day | 4 |
| Total per day | 44 items |
| All APIs | Free tier, no credit card |

---

## Awaiting Your Confirmation

Please confirm:
1. ✅ Do you have/can create a second Google account for Gemini Key 2?
2. ✅ Will you create a Groq account at console.groq.com?
3. ✅ Are you happy with 11 items per run / 44 items per day?
4. ✅ Any changes to the plan before I implement?

Once confirmed, I will update all scripts immediately.

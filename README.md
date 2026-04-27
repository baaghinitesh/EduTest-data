# EduTest-data

GitHub repository serving as a **database** for EduTest content (tests, notes, resources). Deployed to Cloudflare Pages for global CDN delivery.

## Why This Repo?

Instead of storing test questions and note content in MongoDB (expensive at scale), EduTest stores them as JSON files in this GitHub repo. Benefits:

- **Free storage** — GitHub repos are free
- **Version control** — every publish is a git commit
- **Global CDN** — Cloudflare Pages serves files from 300+ edge locations
- **Zero read cost** — CDN reads are free and fast
- **Searchable** — `data/catalog/index.json` provides a searchable index

---

## Repository Structure

```
edutest-data/
├── _headers                     # Cloudflare CORS config
├── test.json                    # Health check file
└── data/
    ├── catalog/
    │   └── index.json           # Searchable index of ALL content
    ├── admin/
    │   ├── tests/
    │   │   ├── test-math-class10-cbse-001.json
    │   │   ├── test-science-class9-cbse-001.json
    │   │   └── test-jee-physics-001.json
    │   └── notes/
    │       ├── note-math-quadratic-formulas.json
    │       └── note-physics-mechanics-jee.json
    └── users/
        └── {userId}/
            ├── tests/
            │   └── {testId}.json
            └── notes/
                └── {noteId}.json
```

---

## CDN URL

**Base:** `https://prepguidedata.launchyourconcept.com`

**Examples:**
```
https://prepguidedata.launchyourconcept.com/test.json
https://prepguidedata.launchyourconcept.com/data/catalog/index.json
https://prepguidedata.launchyourconcept.com/data/admin/tests/test-math-class10-cbse-001.json
```

---

## Catalog Index (`data/catalog/index.json`)

This is the **searchable index** of all published content. Every test/note has an entry:

```json
{
  "id": "test-math-class10-cbse-001",
  "type": "test",
  "title": "Mathematics — Algebra & Quadratic Equations",
  "subject": "mathematics",
  "board": "CBSE",
  "classSlug": "cbse-class-10",
  "classNumber": 10,
  "chapter": "Quadratic Equations",
  "testType": "chapter",
  "totalQuestions": 10,
  "duration": 45,
  "totalMarks": 40,
  "author": "EduTest Admin",
  "authorId": "admin",
  "isAdmin": true,
  "tags": ["algebra", "quadratic", "class10", "cbse", "mathematics"],
  "cdnUrl": "https://prepguidedata.launchyourconcept.com/data/admin/tests/test-math-class10-cbse-001.json",
  "path": "data/admin/tests/test-math-class10-cbse-001.json",
  "publishedAt": "2025-04-01T10:00:00.000Z"
}
```

The EduTest client fetches this index once (cached for 5 minutes in IndexedDB) and filters it client-side by `classSlug`, `subject`, `board`, `tags`, etc.

---

## Test JSON Schema

```json
{
  "id": "test-math-class10-cbse-001",
  "title": "Mathematics — Algebra & Quadratic Equations",
  "description": "Chapter-wise test...",
  "board": "CBSE",
  "classSlug": "cbse-class-10",
  "classNumber": 10,
  "subject": "mathematics",
  "chapter": "Quadratic Equations",
  "topic": "Factorisation & Discriminant",
  "testType": "chapter",
  "duration": 45,
  "totalMarks": 40,
  "passingMarks": 40,
  "totalQuestions": 10,
  "isPublic": true,
  "isPublished": true,
  "tags": ["algebra", "quadratic", "class10", "cbse"],
  "instructions": [
    "All questions are compulsory.",
    "Each MCQ carries 4 marks."
  ],
  "createdBy": "admin",
  "createdByName": "EduTest Admin",
  "publishedAt": "2025-04-01T10:00:00.000Z",
  "questions": [
    {
      "id": "q1",
      "order": 1,
      "question": "Which of the following is a quadratic equation?",
      "type": "mcq",
      "marks": 4,
      "options": [
        { "letter": "A", "text": "x³ + 2x + 1 = 0", "isCorrect": false, "index": 0 },
        { "letter": "B", "text": "x² + 5x + 6 = 0", "isCorrect": true,  "index": 1 },
        { "letter": "C", "text": "2x + 3 = 0",       "isCorrect": false, "index": 2 },
        { "letter": "D", "text": "1/x + x = 2",      "isCorrect": false, "index": 3 }
      ],
      "explanation": "A quadratic equation has degree 2...",
      "hint": "Look for the highest power of x."
    }
  ]
}
```

---

## Note JSON Schema

```json
{
  "id": "note-math-quadratic-formulas",
  "title": "Quadratic Equations — Complete Formula Sheet",
  "description": "All key formulas...",
  "type": "note",
  "board": "CBSE",
  "classSlug": "cbse-class-10",
  "subject": "mathematics",
  "chapter": "Quadratic Equations",
  "tags": ["quadratic", "formula", "class10"],
  "createdBy": "admin",
  "publishedAt": "2025-04-01T12:00:00.000Z",
  "content": [
    { "type": "heading", "text": "Standard Form" },
    { "type": "formula", "text": "ax² + bx + c = 0" },
    { "type": "list", "items": ["Item 1", "Item 2"] }
  ]
}
```

---

## Adding Content Manually

1. Create a JSON file in the correct path:
   - Admin tests: `data/admin/tests/{testId}.json`
   - User tests: `data/users/{userId}/tests/{testId}.json`
   - Admin notes: `data/admin/notes/{noteId}.json`

2. Add an entry to `data/catalog/index.json`

3. Commit and push:
   ```bash
   git add .
   git commit -m "Add new test: Your Test Title"
   git push
   ```

4. Cloudflare Pages auto-deploys in ~30 seconds

---

## CORS Configuration (`_headers`)

```
/*
  Access-Control-Allow-Origin: *
```

This allows the EduTest client (any domain) to fetch JSON files from the CDN.

---

## Cloudflare Pages Deployment

- **Connected to:** `baaghinitesh/edutest-data` (GitHub)
- **Branch:** `master`
- **Build command:** (none — static files)
- **Build output directory:** `/` (repo root)
- **Auto-deploy:** on every push

**Deployment URL:** `https://prepguidedata.launchyourconcept.com`

---

## Verifying Deployment

```bash
# Check health
curl https://prepguidedata.launchyourconcept.com/test.json

# Check catalog index
curl https://prepguidedata.launchyourconcept.com/data/catalog/index.json

# Check a specific test
curl https://prepguidedata.launchyourconcept.com/data/admin/tests/test-math-class10-cbse-001.json
```

All should return JSON (not 404).

# EduTest-Data Automation Plan
> Reference document for AI-assisted content generation pipeline.
> Written after full analysis of both EduTest (website) and EduTest-data (CDN database) repos.

---

## 1. What This System Is

**PrepGuide** (EduTest) is a full-stack educational platform:
- React + Vite frontend
- Express + MongoDB backend
- **GitHub (EduTest-data repo) as the primary content database**
- Cloudflare Pages CDN auto-deploys on every push → `https://prepguidedata.launchyourconcept.com`

The website reads ALL tests, notes, formulas, and resources as **static JSON files** from the CDN.
The catalog index (`data/catalog/index.json`) is the searchable master list the website fetches first.

---

## 2. Current State of EduTest-data

```
EduTest-data/
├── _headers                          ← CORS: Access-Control-Allow-Origin: *
├── test.json                         ← health check
├── README.md
└── data/
    ├── catalog/
    │   └── index.json                ← ONLY 1 entry (NORCET PYQ test)
    └── admin/
        └── tests/
            └── norcet-pyq-2024-september-15-50ea56c3.json
```

**Problem:** Almost empty. The website has no content to show.
**Goal:** Automate daily content creation using Gemini API via GitHub Actions.

---

## 3. Target Folder Structure to Build

```
EduTest-data/
├── _headers
├── test.json
├── README.md
├── AUTOMATION_PLAN.md                ← this file
│
├── topics/                           ← NEW: topic registry per class/exam
│   ├── school/
│   │   ├── cbse/
│   │   │   ├── class-6.json
│   │   │   ├── class-7.json
│   │   │   ├── class-8.json
│   │   │   ├── class-9.json
│   │   │   ├── class-10.json
│   │   │   ├── class-11.json
│   │   │   └── class-12.json
│   │   ├── icse/
│   │   │   └── (same pattern)
│   │   ├── maharashtra/
│   │   │   └── (same pattern)
│   │   └── up-board/
│   │       └── (same pattern)
│   └── competitive/
│       ├── neet-ug.json
│       ├── jee-mains.json
│       ├── jee-advanced.json
│       ├── norcet.json
│       ├── upsc-cse.json
│       ├── ssc-cgl.json
│       ├── ibps-po.json
│       └── (all other exams)
│
├── data/
│   ├── catalog/
│   │   └── index.json                ← master searchable index (auto-updated)
│   └── admin/
│       ├── tests/
│       │   └── {slug}-{shortId}.json
│       ├── notes/
│       │   └── {slug}-{shortId}.json
│       ├── formulas/
│       │   └── {slug}-{shortId}.json
│       └── resources/
│           └── {slug}-{shortId}.json
│
├── scripts/                          ← NEW: Python automation engine
│   ├── generate.py                   ← main runner (called by GitHub Actions)
│   ├── gemini_client.py              ← Gemini API wrapper
│   ├── topic_picker.py               ← smart topic/subject selector (no repeats)
│   ├── content_builder.py            ← builds test/note/formula JSON payloads
│   ├── catalog_updater.py            ← updates data/catalog/index.json
│   ├── state.json                    ← tracks what was already generated (no repeats)
│   └── prompts/
│       ├── test_full_length.txt      ← prompt template for full-length tests
│       ├── test_subjectwise.txt      ← prompt template for subject-wise tests
│       ├── notes.txt                 ← prompt template for notes
│       └── formulas.txt             ← prompt template for formulas
│
└── .github/
    └── workflows/
        └── generate-content.yml      ← GitHub Actions: 3 runs/day
```

---

## 4. Topics JSON Schema (per class/exam file)

Each file in `topics/` defines what subjects and topics exist for that class/exam.
The automation engine reads these to pick what to generate next.

```json
{
  "classSlug": "cbse-class-10",
  "classNumber": 10,
  "board": "CBSE",
  "type": "school",
  "subjects": [
    {
      "slug": "mathematics",
      "name": "Mathematics",
      "chapters": [
        {
          "name": "Real Numbers",
          "topics": ["Euclid's Division Lemma", "Fundamental Theorem of Arithmetic", "Irrational Numbers", "Decimal Expansions"]
        },
        {
          "name": "Polynomials",
          "topics": ["Zeros of Polynomial", "Relationship between Zeros and Coefficients", "Division Algorithm"]
        },
        {
          "name": "Quadratic Equations",
          "topics": ["Factorisation", "Completing the Square", "Discriminant", "Nature of Roots"]
        }
      ]
    },
    {
      "slug": "science",
      "name": "Science",
      "chapters": [...]
    }
  ]
}
```

For competitive exams:
```json
{
  "classSlug": "neet-ug",
  "board": "Competitive",
  "type": "competitive",
  "examConfig": {
    "fullLengthQuestions": 180,
    "fullLengthMarks": 720,
    "fullLengthDuration": 200,
    "subjectWiseQuestions": 45,
    "subjectWiseMarks": 180,
    "subjectWiseDuration": 60,
    "negativeMarking": true,
    "negativeMarkValue": -1,
    "correctMarkValue": 4
  },
  "subjects": [
    {
      "slug": "physics",
      "name": "Physics",
      "chapters": [...]
    }
  ]
}
```

---

## 5. Exam Configs (Full-Length Test Parameters)

Different exams have different marks/questions. The automation must use these exactly.

| Exam | Questions | Marks | Duration | Negative |
|------|-----------|-------|----------|---------|
| NEET UG | 180 | 720 | 200 min | -1 per wrong |
| JEE Mains | 90 | 300 | 180 min | -1 per wrong |
| JEE Advanced | 54 | 183 | 180 min | varies |
| NORCET | 100 | 100 | 90 min | 0 |
| UPSC CSE (Prelims) | 100 | 200 | 120 min | -0.66 |
| SSC CGL (Tier 1) | 100 | 200 | 60 min | -0.5 |
| IBPS PO (Prelims) | 100 | 100 | 60 min | -0.25 |
| CAT | 66 | 198 | 120 min | -1 per wrong |
| CLAT | 150 | 150 | 120 min | -0.25 |
| NDA | 150 | 600 | 150 min | -0.83 |
| CBSE Class 10 (Board) | 40 | 80 | 180 min | 0 |
| CBSE Class 12 (Board) | 35 | 70 | 180 min | 0 |
| Subject-wise (any) | 30–45 | 30–45 | 45–60 min | 0 |

---

## 6. Content Types & Generation Rules

### 6.1 Full-Length Test
- **Count per run:** 1 only (high quality)
- **Questions:** per exam config above
- **Type:** MCQ with 4 options, correct answer, explanation per question
- **Covers:** multiple subjects/chapters (full syllabus mix)
- **No repeat:** track generated topics in `scripts/state.json`

### 6.2 Subject-wise Test
- **Count per run:** 4 tests at once
- **Questions:** 30–45 per test
- **Type:** MCQ, topic-focused
- **Each test:** different subject or different chapter
- **No repeat:** same topic not used again until all topics exhausted (then reset)

### 6.3 Notes
- **Count per run:** 5 notes (high quality)
- **Format:** structured JSON with heading, paragraph, formula, list, table, code blocks
- **Coverage:** key concepts, definitions, examples, mnemonics
- **Randomly pick:** subject + topic from topics registry

### 6.4 Formulas
- **Count per run:** 5 formula sheets (same as notes)
- **Format:** structured JSON with formula blocks, derivations, units, examples
- **Randomly pick:** subject + topic

### 6.5 Run Mode Selection
Each GitHub Actions run randomly picks ONE mode:
- `full_length_test` → 1 full-length test
- `subject_tests` → 4 subject-wise tests
- `notes` → 5 notes
- `formulas` → 5 formula sheets

---

## 7. No-Repeat System

`scripts/state.json` tracks everything generated:

```json
{
  "generated_tests": [
    {
      "id": "uuid",
      "classSlug": "neet-ug",
      "subject": "physics",
      "chapter": "Laws of Motion",
      "topic": "Newton's Second Law",
      "type": "subject-wise",
      "generatedAt": "2026-04-27T10:00:00Z"
    }
  ],
  "generated_notes": [...],
  "generated_formulas": [...],
  "topic_exhaustion": {
    "neet-ug": {
      "physics": ["Laws of Motion", "Thermodynamics"],
      "chemistry": ["Periodic Table"]
    }
  }
}
```

**Logic:**
1. Load state.json
2. Get all available topics for chosen class/exam
3. Filter out already-used topics
4. If all topics exhausted → reset that subject's list and start over
5. Pick randomly from remaining
6. After generation → append to state.json and commit

---

## 8. JSON File Schemas (What the Website Expects)

### Test JSON (`data/admin/tests/{slug}.json`)
```json
{
  "id": "uuid-v4",
  "title": "NEET UG — Physics Full Length Mock Test 1",
  "description": "...",
  "board": "Competitive",
  "classSlug": "neet-ug",
  "classNumber": 0,
  "subject": "physics",
  "chapter": "Full Syllabus",
  "topic": "Full Length",
  "testType": "full-length",
  "duration": 200,
  "totalMarks": 720,
  "passingMarks": 360,
  "totalQuestions": 180,
  "isPublic": true,
  "isPublished": true,
  "showCorrectAnswers": true,
  "randomizeQuestions": true,
  "unlimitedAttempts": true,
  "allowedAttempts": 0,
  "tags": ["neet", "physics", "full-length", "mock"],
  "instructions": ["Each correct answer: +4 marks", "Each wrong answer: -1 mark"],
  "createdBy": "admin",
  "createdByName": "EduTest Admin",
  "publishedAt": "ISO-timestamp",
  "questions": [
    {
      "id": "q-{timestamp}-{random}",
      "order": 1,
      "question": "Question text here",
      "type": "mcq",
      "marks": 4,
      "negativeMark": 1,
      "negativeMarking": true,
      "difficulty": "medium",
      "subject": "physics",
      "options": [
        { "letter": "A", "text": "Option A", "isCorrect": false, "index": 0, "imageUrl": "", "explanation": "Why A is wrong" },
        { "letter": "B", "text": "Option B", "isCorrect": true,  "index": 1, "imageUrl": "", "explanation": "Why B is correct" },
        { "letter": "C", "text": "Option C", "isCorrect": false, "index": 2, "imageUrl": "", "explanation": "Why C is wrong" },
        { "letter": "D", "text": "Option D", "isCorrect": false, "index": 3, "imageUrl": "", "explanation": "Why D is wrong" }
      ],
      "answer": "",
      "alternateAnswers": [],
      "explanation": "Full explanation of the answer",
      "hint": "Optional hint",
      "imageUrl": "",
      "hasError": false
    }
  ]
}
```

### Note JSON (`data/admin/notes/{slug}.json`)
```json
{
  "id": "uuid-v4",
  "title": "Laws of Motion — Complete Notes",
  "description": "...",
  "type": "note",
  "board": "Competitive",
  "classSlug": "neet-ug",
  "subject": "physics",
  "chapter": "Laws of Motion",
  "tags": ["physics", "newton", "neet"],
  "createdBy": "admin",
  "createdByName": "EduTest Admin",
  "publishedAt": "ISO-timestamp",
  "isPublic": true,
  "isPublished": true,
  "content": [
    { "type": "heading", "text": "Newton's Laws of Motion" },
    { "type": "paragraph", "text": "Newton's first law states that..." },
    { "type": "formula", "text": "F = ma" },
    { "type": "list", "items": ["Item 1", "Item 2"] },
    { "type": "table", "headers": ["Law", "Statement"], "rows": [["First", "..."], ["Second", "..."]] },
    { "type": "note", "text": "Important: Remember that..." }
  ]
}
```

### Formula JSON (`data/admin/formulas/{slug}.json`)
```json
{
  "id": "uuid-v4",
  "title": "Thermodynamics — Formula Sheet",
  "type": "formula",
  "board": "Competitive",
  "classSlug": "neet-ug",
  "subject": "physics",
  "chapter": "Thermodynamics",
  "tags": ["physics", "thermodynamics", "formulas"],
  "createdBy": "admin",
  "createdByName": "EduTest Admin",
  "publishedAt": "ISO-timestamp",
  "isPublic": true,
  "isPublished": true,
  "content": [
    { "type": "heading", "text": "First Law of Thermodynamics" },
    { "type": "formula", "text": "ΔU = Q - W", "description": "Change in internal energy equals heat added minus work done" },
    { "type": "formula", "text": "Q = nCvΔT", "description": "Heat at constant volume" },
    { "type": "note", "text": "Units: Joules (J)" }
  ]
}
```

### Catalog Index Entry (`data/catalog/index.json` — one entry per item)
```json
{
  "id": "uuid-v4",
  "type": "test",
  "title": "...",
  "description": "...",
  "subject": "physics",
  "board": "Competitive",
  "classSlug": "neet-ug",
  "classNumber": 0,
  "chapter": "Full Syllabus",
  "testType": "full-length",
  "totalQuestions": 180,
  "duration": 200,
  "totalMarks": 720,
  "author": "EduTest Admin",
  "authorId": "admin",
  "isAdmin": true,
  "tags": ["neet", "physics", "full-length"],
  "cdnUrl": "https://prepguidedata.launchyourconcept.com/data/admin/tests/{slug}.json",
  "path": "data/admin/tests/{slug}.json",
  "publishedAt": "ISO-timestamp"
}
```

---

## 9. GitHub Actions Workflow

**File:** `.github/workflows/generate-content.yml`

**Schedule:** 3 runs per day
- Run 1: `0 2 * * *` (2:00 AM UTC = 7:30 AM IST)
- Run 2: `0 8 * * *` (8:00 AM UTC = 1:30 PM IST)
- Run 3: `0 14 * * *` (2:00 PM UTC = 7:30 PM IST)

**Secrets needed in GitHub repo settings:**
- `GEMINI_API_KEY` — Google Gemini API key
- `GH_PAT` — GitHub Personal Access Token (to commit files back to repo)

**Workflow steps:**
1. Checkout repo
2. Setup Python 3.11
3. Install dependencies (`google-generativeai`, `requests`)
4. Run `python scripts/generate.py`
5. Commit and push generated files + updated index + updated state.json

---

## 10. Python Scripts Architecture

### `scripts/generate.py` (main runner)
```
1. Load state.json
2. Randomly pick run mode: full_length_test | subject_tests | notes | formulas
3. Call topic_picker.py → get target class/exam + subject + chapter + topic
4. Call content_builder.py → build prompt → call Gemini → parse response → build JSON
5. Save JSON files to correct paths
6. Call catalog_updater.py → update data/catalog/index.json
7. Update state.json with what was generated
8. Git add + commit + push (done by GitHub Actions)
```

### `scripts/gemini_client.py`
- Wraps `google.generativeai`
- Uses model: `gemini-1.5-flash` (fast, cheap, high quality)
- Handles retries (3x) on API errors
- Validates response is valid JSON before returning
- Strips markdown code fences from response

### `scripts/topic_picker.py`
- Reads all `topics/` JSON files
- Loads `state.json` to know what's been used
- Implements round-robin + random selection
- Returns: `{ classSlug, board, subject, chapter, topic, examConfig }`

### `scripts/content_builder.py`
- Takes topic info + run mode
- Selects correct prompt template from `scripts/prompts/`
- Fills in variables (exam name, subject, chapter, topic, question count, marks)
- Calls Gemini
- Parses and validates the JSON response
- Builds final file payload with correct schema
- Returns list of file objects: `[{ path, content, indexEntry }]`

### `scripts/catalog_updater.py`
- Reads current `data/catalog/index.json`
- Appends new entries (deduplicates by id)
- Writes back

---

## 11. Prompt Strategy

### Full-Length Test Prompt (key points)
- Tell Gemini: exact exam name, total questions, marks per question, negative marking
- Ask for JSON array of questions directly (no markdown, no explanation outside JSON)
- Specify difficulty distribution: 30% easy, 50% medium, 20% hard
- Each question must have: question text, 4 options, correct answer marked, explanation per option
- Cover multiple subjects proportionally (e.g., NEET: 45 Physics + 45 Chemistry + 45 Botany + 45 Zoology)

### Subject-wise Test Prompt
- Specific chapter + topic
- 30–45 questions
- All from that one topic
- Increasing difficulty order

### Notes Prompt
- Ask for structured JSON content array
- Cover: definition, key concepts, examples, common mistakes, exam tips
- Use heading/paragraph/formula/list/table content types

### Formulas Prompt
- Ask for formula sheet JSON
- Each formula: expression, description, units, derivation hint, example

---

## 12. Files to Create (Implementation Checklist)

### Phase 1 — Topics Registry
- [ ] `topics/school/cbse/class-6.json` through `class-12.json`
- [ ] `topics/school/icse/class-6.json` through `class-12.json`
- [ ] `topics/school/maharashtra/class-6.json` through `class-12.json`
- [ ] `topics/school/up-board/class-6.json` through `class-12.json`
- [ ] `topics/competitive/neet-ug.json`
- [ ] `topics/competitive/jee-mains.json`
- [ ] `topics/competitive/jee-advanced.json`
- [ ] `topics/competitive/norcet.json`
- [ ] `topics/competitive/upsc-cse.json`
- [ ] `topics/competitive/ssc-cgl.json`
- [ ] `topics/competitive/ibps-po.json`
- [ ] `topics/competitive/cat.json`
- [ ] `topics/competitive/clat.json`
- [ ] `topics/competitive/nda.json`
- [ ] `topics/competitive/gate.json`
- [ ] `topics/competitive/ctet.json`
- [ ] `topics/competitive/rrb-ntpc.json`
- [ ] `topics/competitive/cuet-ug.json`

### Phase 2 — Python Scripts
- [ ] `scripts/generate.py`
- [ ] `scripts/gemini_client.py`
- [ ] `scripts/topic_picker.py`
- [ ] `scripts/content_builder.py`
- [ ] `scripts/catalog_updater.py`
- [ ] `scripts/state.json` (initial empty state)
- [ ] `scripts/prompts/test_full_length.txt`
- [ ] `scripts/prompts/test_subjectwise.txt`
- [ ] `scripts/prompts/notes.txt`
- [ ] `scripts/prompts/formulas.txt`
- [ ] `scripts/requirements.txt`

### Phase 3 — GitHub Actions
- [ ] `.github/workflows/generate-content.yml`

### Phase 4 — Folder Scaffolding
- [ ] `data/admin/notes/.keep`
- [ ] `data/admin/formulas/.keep`
- [ ] `data/admin/resources/.keep`

---

## 13. Key Constraints & Rules

1. **Never repeat a topic** until all topics in that subject are exhausted, then reset
2. **Full-length test:** exactly 1 per run, use exact exam question/mark counts
3. **Subject-wise tests:** exactly 4 per run
4. **Notes/Formulas:** exactly 5 per run
5. **File naming:** `{title-slug}-{first-8-chars-of-uuid}.json`
6. **Always update** `data/catalog/index.json` after generating
7. **Always update** `scripts/state.json` after generating
8. **Gemini response** must be valid JSON — retry up to 3 times if not
9. **CDN base URL:** `https://prepguidedata.launchyourconcept.com`
10. **GitHub repo:** `baaghinitesh/EduTest-data`, branch: `master`
11. **Admin path:** `data/admin/tests/`, `data/admin/notes/`, `data/admin/formulas/`
12. **isAdmin: true** on all auto-generated content
13. **createdBy: "admin"**, **createdByName: "EduTest Admin"**

---

## 14. GitHub Secrets Required

Set these in: `https://github.com/baaghinitesh/EduTest-data/settings/secrets/actions`

| Secret Name | Value |
|-------------|-------|
| `GEMINI_API_KEY` | Your Google Gemini API key |
| `GH_PAT` | GitHub Personal Access Token with `repo` scope |

---

## 15. Implementation Order

1. Create all `topics/` JSON files (the data source for topic picking)
2. Create `scripts/state.json` (empty initial state)
3. Create `scripts/requirements.txt`
4. Create `scripts/prompts/*.txt` (prompt templates)
5. Create `scripts/gemini_client.py`
6. Create `scripts/catalog_updater.py`
7. Create `scripts/topic_picker.py`
8. Create `scripts/content_builder.py`
9. Create `scripts/generate.py` (main runner — ties everything together)
10. Create `.github/workflows/generate-content.yml`
11. Create folder scaffolding (`.keep` files)
12. Test locally: `python scripts/generate.py`
13. Push to GitHub, add secrets, verify first Actions run

---

## 16. CDN URL Pattern

All generated files are accessible at:
```
https://prepguidedata.launchyourconcept.com/data/admin/tests/{slug}.json
https://prepguidedata.launchyourconcept.com/data/admin/notes/{slug}.json
https://prepguidedata.launchyourconcept.com/data/admin/formulas/{slug}.json
https://prepguidedata.launchyourconcept.com/data/catalog/index.json
```

The website client (`EduTest/client/src/lib/github.js`) fetches the index first, then individual files by their `cdnUrl` field. As long as files are committed to this repo and Cloudflare deploys (auto, ~30s), the website will show the new content.

---

*Last updated: 2026-04-27 | Status: Planning complete, ready for implementation*

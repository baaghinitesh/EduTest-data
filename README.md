<div align="center">

<img src="./assets/prepguide-logo.png" alt="PrepGuide Lite Logo" width="340" />

<h3>PrepGuide Lite — Open Study &amp; Test Content Bank</h3>

<p>
  <strong>Static JSON Content Database &amp; Automated AI Question Bank for PrepGuide Lite</strong><br/>
  1,980+ curated mock tests across 81 school classes &amp; competitive exams — deployed globally on Cloudflare Pages CDN.
</p>

<p>
  <a href="https://prepguide.launchyourconcept.com">
    <img src="https://img.shields.io/badge/Live_App-PrepGuide_Lite-6366f1?style=for-the-badge&logo=googlechrome&logoColor=white" alt="Live App" />
  </a>
  <a href="https://prepguidedata.launchyourconcept.com/test.json">
    <img src="https://img.shields.io/badge/CDN-Cloudflare_Pages-f38020?style=for-the-badge&logo=cloudflare&logoColor=white" alt="Cloudflare CDN" />
  </a>
  <a href="https://github.com/baaghinitesh/EduTest-data/actions/workflows/generate-content.yml">
    <img src="https://github.com/baaghinitesh/EduTest-data/actions/workflows/generate-content.yml/badge.svg" alt="Automated Test Generator" />
  </a>
  <img src="https://img.shields.io/badge/Format-JSON_%2B_KaTeX_LaTeX-10b981?style=for-the-badge" alt="Format JSON + LaTeX" />
</p>

<br/>

<img src="./assets/prepguide-banner.png" alt="PrepGuide Lite Banner" width="760" />

</div>

---

## What is `EduTest-data`?

`EduTest-data` is the **content bank and global CDN store** powering **[PrepGuide Lite](https://prepguide.launchyourconcept.com)** (built by [Launch Your Concept](https://www.launchyourconcept.com)).

Instead of storing large question banks, explanations, and study notes inside a traditional database, PrepGuide Lite stores every test and study material as a **version-controlled static JSON file** in this repository and serves it worldwide via **Cloudflare Pages** (`https://prepguidedata.launchyourconcept.com`).

### Key Benefits
- **Zero-Cost Global Read Scale** — Served from 300+ Cloudflare edge locations with CORS enabled (`_headers`)
- **Offline-First Ready** — Designed to cache seamlessly inside the client's IndexedDB (`prepguide-offline`)
- **Rich Math & Science Support** — Full KaTeX / LaTeX formulas (`$...$`, `$$...$$`), hints, step-by-step explanations, and per-option breakdowns
- **Automated Daily Growth** — GitHub Actions runs 4× daily to research topics, generate exam-aligned questions, structure JSON, and publish to both CDN and MongoDB metadata
- **Community + Official Content** — Separates official curated materials (`data/admin/`) from educator and student contributions (`data/teacher/`, `data/student/`)

---

## System Architecture

```
┌──────────────────────────────────────────────────────────┐
│           PrepGuide Lite Client (React + Vite)           │
│     IndexedDB Offline Cache · KaTeX · PWA · Exam UI      │
└──────────────────────────┬───────────────────────────────┘
                           │
        ┌──────────────────┴──────────────────┐
        │ Metadata & Auth                     │ Full Test / Note JSON
        ▼                                     ▼
┌────────────────────────┐      ┌──────────────────────────────────┐
│  Express + MongoDB API │      │  Cloudflare Pages Global CDN     │
│  (Search & Leaderboard)│      │  prepguidedata.launchyourconcept │
└───────────┬────────────┘      └─────────────────▲────────────────┘
            │ @octokit/rest                       │ Auto-deploy on push
            └──────────────► GitHub: EduTest-data ◄── GitHub Actions (4x/day)
```

---

## Repository Structure

```text
EduTest-data/
├── assets/                          # Brand logo, icon, and banner assets
├── _headers                         # Cloudflare CORS config (Access-Control-Allow-Origin: *)
├── test.json                        # CDN health-check endpoint
├── data/
│   ├── admin/                       # Official PrepGuide Lite content (organised by exam/class slug)
│   │   ├── cbse-class-10/tests/     # e.g. cbse-class-10-mathematics-...json
│   │   ├── jee-mains/tests/         # Full-length, combined & subject-wise tests
│   │   ├── jee-advanced/tests/
│   │   ├── neet-ug/tests/
│   │   ├── upsc-cse/tests/
│   │   └── ... (81 exams & classes)
│   ├── teacher/                     # Educator-published tests & notes
│   ├── student/                     # Student-published tests & notes
│   ├── research/                    # Cached syllabus & exam-pattern research
│   └── drafts/                      # In-progress generation drafts (resumed automatically)
├── topics/                          # Structured syllabus registry (81 curricula)
│   ├── school/                      # CBSE, ICSE, ISC, UP, Maharashtra, Rajasthan, West Bengal (Classes 1–12)
│   └── competitive/                 # JEE, NEET, UPSC, GATE, CAT, SSC, IBPS, SBI, RRB, NDA, CDS, CLAT, NORCET, etc.
├── scripts/                         # Python multi-provider test generation & publishing pipeline
│   ├── generate.py                  # Main orchestrator (resumes drafts → generates new tests)
│   ├── content_builder.py           # Exam-specific prompt builder, chunking & JSON assembly
│   ├── topic_picker.py              # Cycle-based topic selector (prevents duplicate topics)
│   ├── gemini_client.py             # Gemini Flash 2.5 / 2.0 multi-key rotation
│   ├── groq_client.py               # Groq Llama 3.3 70B structuring client
│   ├── mistral_client.py            # Mistral Small fallback client
│   ├── draft_store.py               # Partial-test persistence across runs
│   ├── research_cache.py            # Cached subject/topic exam research
│   └── publish_to_server.py         # Syncs metadata to PrepGuide Lite backend
└── .github/workflows/
    └── generate-content.yml         # Scheduled 4×/day GitHub Actions workflow
```

---

## Covered Exams & School Boards (`topics/`)

| Category | Coverage |
|---|---|
| **School Boards (Classes 1–12)** | CBSE, ICSE, ISC, Maharashtra Board, UP Board, Rajasthan Board, West Bengal Board |
| **Engineering & Science** | JEE Mains, JEE Advanced, GATE, CUET UG, CUET PG, KVPY, Math & Science Olympiads |
| **Medical & Nursing** | NEET UG, NEET PG, NORCET |
| **Civil Services & Defence** | UPSC CSE, UPSC CAPF, NDA, CDS |
| **SSC, Banking & Railways** | SSC CGL, SSC CHSL, SSC MTS, IBPS PO, IBPS Clerk, SBI PO, RRB NTPC, RRB Group D |
| **Management, Law & Teaching** | CAT, CLAT, CTET, DSSSB |

---

## Content JSON Schemas

### 1. Test JSON (`data/admin/{classSlug}/tests/{slug}-{shortId}.json`)

```json
{
  "id": "98bd07ae-9f65-4bee-8004-92b2c820f66b",
  "title": "CAT — Combined: Data Interpretation LR + Quantitative Aptitude",
  "description": "Combined test: Data Interpretation LR + Quantitative Aptitude for CAT. 30Q.",
  "board": "Competitive",
  "classSlug": "cat",
  "classNumber": 0,
  "subjects": ["cat-dilr", "cat-qa"],
  "subject": "cat-dilr",
  "chapter": "Multiple Chapters",
  "topic": "Combined",
  "testType": "subject-wise",
  "duration": 80,
  "totalMarks": 90,
  "passingMarks": 36,
  "totalQuestions": 30,
  "isPublic": true,
  "isPublished": true,
  "isAdmin": true,
  "showCorrectAnswers": true,
  "randomizeQuestions": false,
  "unlimitedAttempts": true,
  "allowedAttempts": 0,
  "tags": ["cat", "cat-dilr+cat-qa"],
  "instructions": [
    "Subject: Data Interpretation LR + Quantitative Aptitude",
    "Total Questions: 30",
    "Total Marks: 90",
    "Duration: 80 minutes"
  ],
  "createdBy": "admin",
  "createdByName": "PrepGuide Lite Admin",
  "publishedAt": "2026-05-01T10:00:00Z",
  "questions": [
    {
      "id": "q-uuid",
      "order": 1,
      "type": "mcq",
      "question": "Question text with optional LaTeX like $E = mc^2$",
      "difficulty": "medium",
      "marks": 3,
      "negativeMarking": true,
      "negativeMark": 1,
      "hint": "Optional hint for students",
      "explanation": "Step-by-step deductive solution",
      "options": [
        { "letter": "A", "text": "Option A", "isCorrect": false, "index": 0, "explanation": "..." },
        { "letter": "B", "text": "Option B", "isCorrect": true,  "index": 1, "explanation": "..." },
        { "letter": "C", "text": "Option C", "isCorrect": false, "index": 2, "explanation": "..." },
        { "letter": "D", "text": "Option D", "isCorrect": false, "index": 3, "explanation": "..." }
      ]
    }
  ]
}
```

**Supported question types (`questions[].type`):** `mcq`, `msq`, `true-false`, `one-word`, `short-answer`, `large`.

---

### 2. Note / Formula / Resource JSON (`data/admin/{classSlug}/notes/{slug}-{shortId}.json`)

```json
{
  "id": "note-uuid",
  "title": "Quadratic Equations — Complete Formula Sheet",
  "description": "All key formulas and identities for Class 10 CBSE",
  "type": "note",
  "board": "CBSE",
  "classSlug": "cbse-class-10",
  "subject": "mathematics",
  "chapter": "Quadratic Equations",
  "tags": ["quadratic", "formula", "class10"],
  "isPublic": true,
  "isAdmin": true,
  "createdBy": "admin",
  "publishedAt": "2026-05-01T12:00:00Z",
  "content": "<p>Rich HTML + KaTeX content...</p>"
}
```

**Supported material types:** `note`, `formula`, `book`, `pdf`, `video`, `link`.

---

## Using the CDN Directly

Cloudflare Pages serves all JSON files with `Access-Control-Allow-Origin: *` (configured in [`_headers`](./_headers)):

```bash
# CDN health check
curl https://prepguidedata.launchyourconcept.com/test.json

# Fetch a test directly from CDN
curl https://prepguidedata.launchyourconcept.com/data/admin/cat/tests/cat-combined-data-interpretation-lr-quantitative-a-98bd07ae.json
```

> **Note on Search & Indexing:** Content discovery and search metadata (title, class, board, subject, author, `cdnUrl`) are served by the PrepGuide Lite API (`GET /api/content`), while heavy question payloads are fetched directly from Cloudflare Pages CDN.

---

## Automated Generation Schedule (GitHub Actions)

[`generate-content.yml`](./.github/workflows/generate-content.yml) runs **4 times daily** (and supports manual `workflow_dispatch` runs with optional `mode`, `exam`, `instructions`, and `max_tests`):

| Schedule (UTC) | Time (IST) | Mode | Purpose |
|---|---|---|---|
| `30 0 * * *` | 6:00 AM IST | `full` | Full-length mock test generation |
| `30 6 * * *` | 12:00 PM IST | `subject` | Subject-wise / combined tests |
| `30 11 * * *` | 5:00 PM IST | `subject` | Subject-wise / combined (post-quota reset) |
| `30 15 * * *` | 9:00 PM IST | `subject` | Subject-wise / combined tests |

---

<div align="center">
  <img src="./assets/prepguide-icon.png" alt="PrepGuide Lite Icon" width="44" />
  <br/>
  <strong>PrepGuide Lite</strong> · Built by <a href="https://www.launchyourconcept.com">Launch Your Concept</a>
</div>

# Architecture

## Overview

PrepGuide uses a **hybrid storage architecture** designed to minimize database costs while maximizing read performance:

- **MongoDB** stores metadata (user accounts, test metadata, attempt records, class/subject catalog)
- **GitHub** stores full content (test questions, note content) as JSON files
- **Cloudflare Pages CDN** serves those JSON files globally at edge speed
- **IndexedDB** stores everything offline in the browser

## Tech Stack

### Client
| Technology | Version | Purpose |
|-----------|---------|---------|
| React | 19.2 | UI framework |
| Vite | 8.x | Build tool and dev server |
| React Router | v7 | Client-side routing |
| Tailwind CSS | v3 | Styling |
| react-helmet-async | 3.x | SEO meta tags |
| Recharts | 3.x | Analytics charts |
| idb | 8.x | IndexedDB wrapper for offline storage |
| socket.io-client | 4.x | Real-time test timer sync |
| lucide-react | 1.x | Icons |
| KaTeX | 0.16 | LaTeX math formula rendering |
| framer-motion | 12.x | Animations |

### Server
| Technology | Version | Purpose |
|-----------|---------|---------|
| Express.js | 4.18 | REST API |
| Mongoose | 8.x | MongoDB ODM |
| Socket.io | 4.x | Real-time features (test timers) |
| jsonwebtoken | 9.x | JWT authentication |
| bcryptjs | 2.x | Password hashing |
| @octokit/rest | 22.x | GitHub API (publish content) |
| cloudinary | 2.x | Image upload and compression |
| multer | 1.x | File upload middleware |
| cookie-parser | 1.x | HTTP-only cookie sessions |

### Infrastructure
| Service | Purpose |
|---------|---------|
| MongoDB Atlas | User data, metadata, attempt records |
| GitHub (EduTest-data repo) | Content storage (tests, notes as JSON) |
| Cloudflare Pages | CDN serving GitHub content globally |
| Cloudinary | Image hosting (compressed) |

---

## Data Flow

### Reading a test (subject page)
```
User visits /class/10/CBSE/mathematics
    │
    ├─ 1. GET /api/content?classSlug=cbse-class-10&subject=mathematics
    │      Returns unified list from MongoDB (tests + materials)
    │
    ├─ 2. For CDN-hosted tests: fetch full JSON from Cloudflare CDN
    │      (cdnUrl stored in MongoDB metadata)
    │
    └─ 3. Display test cards in the Tests tab
```

### Taking a test
```
User clicks "Start Test" → /take/:testId
    │
    ├─ 1. Check IndexedDB (offline/cloned tests)
    ├─ 2. Check MongoDB API (/api/tests/:id) — ObjectId only
    │      If test has inline questions → use them
    │      If test has cdnUrl → fetch from CDN
    └─ 3. Fetch CDN directly (data/admin/tests/:id.json)
```

### Submitting a test (leaderboard flow)
```
Student submits test
    │
    ├─ 1. Grade locally (examUtils.evaluateQuestion)
    ├─ 2. Save to IndexedDB (always — works offline)
    └─ 3. Persist to MongoDB via POST /api/attempts/submit
           │
           ├─ If testId is a MongoDB ObjectId → use directly
           ├─ If testId is a UUID → GET /api/tests/by-uuid/:uuid
           │   to resolve UUID → MongoDB _id
           └─ Attempt saved → leaderboard pipeline picks it up
```

### Publishing a test
```
User clicks "Publish"
    │
    ├─ If online:
    │   ├─ POST /api/github/publish/test
    │   │   ├─ Push JSON to GitHub (via @octokit/rest)
    │   │   ├─ Save metadata to MongoDB
    │   │   └─ Update data/catalog/index.json
    │   └─ Cloudflare auto-deploys within ~30 seconds
    │
    └─ If offline:
        └─ Add to IndexedDB syncQueue
           OfflineContext syncs when back online
```

---

## Leaderboard Architecture

The leaderboard is powered by `server/services/LeaderboardService.js` — a single source of truth used by all ranking surfaces.

```
LeaderboardService
    │
    ├─ getLeaderboard(scope, options)
    │   ├─ scope: 'global' | 'class:8' | 'exam:jee-mains' | 'test:<id>'
    │   ├─ Runs MongoDB aggregation pipeline (best attempt per student)
    │   ├─ Assigns rank numbers (ties share same rank)
    │   └─ Returns: { leaders, myEntry, stats, meta, pagination }
    │
    ├─ getStudentRankInfo(userId)
    │   ├─ Returns global rank + class/exam scope rank
    │   └─ Used by: /api/leaderboard/my-rank, /api/users/dashboard
    │
    └─ getTopNForWidget(n)
        └─ Used by: /api/leaderboard/widget (home page, community page)
```

**Ranking pipeline (MongoDB aggregation):**
1. Match completed/auto-submitted attempts
2. Sort by `percentage DESC, totalTimeSpent ASC, submittedTime ASC`
3. Group by student — `$first` picks best attempt
4. Re-sort after grouping
5. Join user + test info
6. Exclude admins and suspended users
7. Assign rank numbers in JS (ties share rank)

**Rank consistency:** `/api/users/dashboard` uses `LeaderboardService.getStudentRankInfo()` — so the rank shown on the dashboard always matches the leaderboard page exactly.

---

## Navigation Hierarchy

```
/classes
├── School Classes (tabs)
│   ├── /class/1  → /class/1/CBSE  → /class/1/CBSE/mathematics
│   └── /class/12 → /class/12/CBSE → /class/12/CBSE/physics
│
└── Competitive Exams (tabs)
    ├── /exam/jee-mains  → /exam/jee-mains/physics
    ├── /exam/neet-ug    → /exam/neet-ug/biology
    └── /exam/upsc-cse   → /exam/upsc-cse/indian-history
```

Each subject page has 5 content tabs: **Tests · Notes · Books · Formulas · Resources**

---

## Real-Time Features (Socket.io)

When a student takes a test:
1. Client emits `join-test` with `{ attemptId, testId, studentId, duration }`
2. Server starts a countdown timer and stores it in memory
3. Server emits `timer-sync` to keep client in sync
4. On time expiry, server emits `auto-submit`
5. Client emits `submit-test` on manual submission

---

## Math Formula Rendering

LaTeX math is rendered client-side using **KaTeX** (lazy-loaded).

**Component:** `components/MathText.jsx`

**Supported delimiters:**
| Delimiter | Type | Example |
|-----------|------|---------|
| `$...$` | Inline | `$K = \frac{p^2}{2m}$` |
| `$$...$$` | Display | `$$\int_0^\infty e^{-x^2} dx$$` |
| `\(...\)` | Inline | `\(x^2 + y^2\)` |
| `\[...\]` | Display | `\[\sum_{i=1}^n i = \frac{n(n+1)}{2}\]` |

**Parser protection:** `parser.js` `cleanLatexDuplication()` protects all LaTeX blocks with placeholders before any text cleaning, then restores them — so formulas are never stripped from pasted content.

---

## Security Model

| Route type | Auth required |
|-----------|--------------|
| GET /api/classes | None |
| GET /api/content | None |
| GET /api/search | None |
| GET /api/seo/render | None (Crawler SSR) |
| GET /api/content/sitemap.xml | None |
| GET /api/tests | None (public tests only) |
| GET /api/leaderboard | None (optionalAuth) |
| GET /api/leaderboard/my-rank | Auth required |
| POST /api/attempts/submit | Auth required |
| POST /api/github/publish | Any logged-in user |
| DELETE /api/github/delete | Own content or Admin |
| /admin/* routes | Admin/Superadmin only |
| /dashboard, /workspace | Any logged-in user |
| /take/:testId | Any logged-in user |

---

## Search Engine, pSEO & Ecosystem

- **Smart Search Engine (`server/services/searchIndex.js`, `/search`)**: Indexes test metadata and individual MCQ/MSQ questions across `EduTest-data` + MongoDB so learners can search tests, questions, topics, and study notes instantaneously (`Ctrl+K` / `/search`).
- **Dynamic Sitemap & Crawler SSR (`server/routes/content.js`, `server/routes/seo.js`)**: Automatically exposes every public test, exam, class/board, and topic page at `/sitemap.xml`, serving full JSON-LD (`Quiz`, `FAQPage`, `Course`, `Article`) and 53+ custom exam/class/board Open Graph cards (`/og/*.png`) via Vercel crawler rewrites.
- **PrepGuide Institutions (`https://preppguide.com`)**: Offline + digital coaching partner opening for Classes 8, 9 & 10 (ICSE, CBSE, West Bengal Board) in PCMB & Computer with AC classrooms, experienced teachers, and full digital + printable notes — built and maintained alongside PrepGuide Lite by **[Launch Your Concept](https://www.launchyourconcept.com)**.

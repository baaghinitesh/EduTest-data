# Classes and Exams Reference

This document lists all supported classes and competitive exams in PrepGuide.

## School Classes (1-12)

| Class | Slug Pattern | Boards |
|-------|-------------|--------|
| Class 1 | `cbse-class-1`, `icse-class-1`, etc. | CBSE, ICSE, ISC, State Boards |
| Class 2 | `cbse-class-2`, `icse-class-2`, etc. | CBSE, ICSE, ISC, State Boards |
| Class 3 | `cbse-class-3`, `icse-class-3`, etc. | CBSE, ICSE, ISC, State Boards |
| Class 4 | `cbse-class-4`, `icse-class-4`, etc. | CBSE, ICSE, ISC, State Boards |
| Class 5 | `cbse-class-5`, `icse-class-5`, etc. | CBSE, ICSE, ISC, State Boards |
| Class 6 | `cbse-class-6`, `icse-class-6`, etc. | CBSE, ICSE, ISC, State Boards |
| Class 7 | `cbse-class-7`, `icse-class-7`, etc. | CBSE, ICSE, ISC, State Boards |
| Class 8 | `cbse-class-8`, `icse-class-8`, etc. | CBSE, ICSE, ISC, State Boards |
| Class 9 | `cbse-class-9`, `icse-class-9`, etc. | CBSE, ICSE, ISC, State Boards |
| Class 10 | `cbse-class-10`, `icse-class-10`, etc. | CBSE, ICSE, ISC, State Boards |
| Class 11 | `cbse-class-11`, `isc-class-11`, etc. | CBSE, ISC, State Boards |
| Class 12 | `cbse-class-12`, `isc-class-12`, etc. | CBSE, ISC, State Boards |

## Competitive Exams

### Engineering
| Exam | Slug | Description |
|------|------|-------------|
| JEE Mains | `jee-mains` | Joint Entrance Examination — Mains for B.Tech/B.E. admissions |
| JEE Advanced | `jee-advanced` | JEE Advanced for IIT admissions |
| GATE | `gate` | Graduate Aptitude Test in Engineering |
| KVPY | `kvpy` | Kishore Vaigyanik Protsahan Yojana |

### Medical
| Exam | Slug | Description |
|------|------|-------------|
| NEET UG | `neet-ug` | National Eligibility cum Entrance Test — Undergraduate |
| NEET PG | `neet-pg` | NEET Postgraduate for MD/MS admissions |
| NORCET | `norcet` | Nursing Officer Recruitment Common Eligibility Test — AIIMS |

### Civil Services
| Exam | Slug | Description |
|------|------|-------------|
| UPSC CSE | `upsc-cse` | UPSC Civil Services Examination — IAS/IPS/IFS |
| UPSC CAPF | `upsc-capf` | Central Armed Police Forces Examination |
| CTET | `ctet` | Central Teacher Eligibility Test |
| DSSSB | `dsssb` | Delhi Subordinate Services Selection Board |

### Banking & SSC
| Exam | Slug | Description |
|------|------|-------------|
| IBPS PO | `ibps-po` | Institute of Banking Personnel Selection — Probationary Officer |
| IBPS Clerk | `ibps-clerk` | IBPS Clerk Examination |
| SBI PO | `sbi-po` | State Bank of India Probationary Officer |
| SBI Clerk | `sbi-clerk` | SBI Clerk Examination |
| SSC CGL | `ssc-cgl` | Staff Selection Commission — Combined Graduate Level |
| SSC CHSL | `ssc-chsl` | SSC Combined Higher Secondary Level |
| SSC MTS | `ssc-mts` | SSC Multi-Tasking Staff |
| RRB NTPC | `rrb-ntpc` | Railway Recruitment Board — Non-Technical Popular Categories |
| RRB Group D | `rrb-group-d` | RRB Group D Examination |

### Law & Management
| Exam | Slug | Description |
|------|------|-------------|
| CLAT | `clat` | Common Law Admission Test |
| CAT | `cat` | Common Admission Test for IIM/MBA |
| CUET UG | `cuet-ug` | Common University Entrance Test — Undergraduate |
| CUET PG | `cuet-pg` | Common University Entrance Test — Postgraduate |

## Data Structure

### Content Item Fields
```javascript
{
  id: string,
  title: string,
  description: string,
  type: 'test' | 'note' | 'formula' | 'pdf' | 'video' | 'link' | 'book' | 'resource',
  
  // IMPORTANT: classSlug is used for BOTH classes AND exams
  // For school classes: 'cbse-class-10', 'icse-class-9', etc.
  // For exams: 'jee-mains', 'neet-ug', 'cat', 'upsc-cse', etc.
  classSlug: string,
  
  // Legacy fields (not commonly used)
  classNumber: number,          // Extracted number (for classes only)
  examType: string,             // Rarely used
  examSlug: string,             // Rarely used
  
  // Metadata
  subject: string,
  board: string,
  tags: string[],
  
  // Ownership
  authorId: string,
  author: string,
  isAdmin: boolean,
  authorRole: 'admin' | 'superadmin' | 'educator' | 'student',
  
  // Publishing
  status: 'draft' | 'published',
  cdnUrl: string,
  githubPath: string,
  cloudDbId: string,
  
  // Timestamps
  createdAt: string,
  updatedAt: string,
  publishedAt: string
}
```

### How Content is Categorized

**School Classes (1-12):**
- Identified by `classSlug` matching pattern: `/class[- ](\d+)/i`
- Examples: `"cbse-class-10"`, `"icse-class-9"`, `"up-board-class-7"`
- Extracted class number: `10`, `9`, `7`

**Competitive Exams:**
- Identified by `classSlug` that does NOT match the class pattern
- Examples: `"norcet"`, `"cat"`, `"upsc-cse"`, `"jee-mains"`, `"ibps-po"`
- The `classSlug` value itself is used as the exam slug

**Uncategorized:**
- Items without a `classSlug` or with invalid values

## Permissions

### Regular Users (Students)
- ✅ View all content (own + others + admin)
- ✅ Clone any content to their workspace
- ✅ Create new content
- ✅ Edit their own content
- ✅ Delete their own content
- ✅ Publish their own content
- ❌ Edit others' content
- ❌ Delete others' content

### Admins & Superadmins
- ✅ All user permissions
- ✅ Edit any content
- ✅ Delete any content
- ✅ Manage users
- ✅ Access admin panel
- ✅ Bulk operations

## UI Guidelines

### Card Display
- Show class/exam cards only when content exists
- Display content counts: Tests, Notes, Formulas, Resources
- Separate Official (admin) vs Community (user) content
- Use distinct colors: Blue for classes, Violet for exams

### Content Cards
- Show title, description, subject, board
- Display author with role badge (Official, Tutor, Community)
- Show action buttons based on permissions:
  - Own content: Edit, Delete, Share, Take Test/View
  - Others' content: Clone, Share, Take Test/View
  - Admin: All actions on all content


## Content Grouping Logic

The `groupByClassAndExam()` function groups content by analyzing the `classSlug` field:

### Classes (1-12)
- Checks if `classSlug` matches pattern: `/class[- ](\d+)/i`
- Extracts the class number
- Groups under `classes[classNumber]`
- Example: `"cbse-class-10"` → `classes[10]`

### Exams
- Checks if `classSlug` exists but does NOT match the class pattern
- Uses the `classSlug` value as the exam slug
- Groups under `exams[examSlug]`
- Examples:
  - `"norcet"` → `exams["norcet"]`
  - `"cat"` → `exams["cat"]`
  - `"upsc-cse"` → `exams["upsc-cse"]`
  - `"jee-mains"` → `exams["jee-mains"]`

### Uncategorized
- Items without `classSlug` or with invalid values
- Grouped under `uncategorized[]`

### Implementation
```javascript
const groupByClassAndExam = (items) => {
  const groups = { classes: {}, exams: {}, uncategorized: [] };
  
  items.forEach(item => {
    // Check for class pattern
    const classMatch = item.classSlug?.match(/class[- ](\d+)/i);
    const classNum = classMatch ? parseInt(classMatch[1]) : null;
    
    // Check if it's an exam (has classSlug but no class pattern)
    const isExamSlug = item.classSlug && !item.classSlug.match(/class[- ]\d+/i);
    
    if (classNum && classNum >= 1 && classNum <= 12) {
      // School class
      if (!groups.classes[classNum]) {
        groups.classes[classNum] = { tests: [], notes: [], formulas: [], resources: [] };
      }
      // Categorize by type...
    } else if (isExamSlug) {
      // Competitive exam
      const examSlug = item.classSlug.toLowerCase().trim();
      if (!groups.exams[examSlug]) {
        groups.exams[examSlug] = { tests: [], notes: [], formulas: [], resources: [] };
      }
      // Categorize by type...
    } else {
      groups.uncategorized.push(item);
    }
  });
  
  return groups;
};
```

## Key Insight

**The `classSlug` field serves dual purpose:**
1. For school content: Contains board + class (e.g., `"cbse-class-10"`)
2. For exam content: Contains exam slug (e.g., `"norcet"`, `"cat"`)

This is why we check the pattern - if it contains "class-X", it's a school class. Otherwise, it's an exam.

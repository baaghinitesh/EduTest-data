"""
populate_all_topics.py
Populates every competitive exam and school class with complete
subjects / chapters / topics, then rewrites index.json + creates
subjects/*.json + full-length.json for each.

Run once:  python scripts/populate_all_topics.py
"""
import os, json, math

ROOT       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOPICS_DIR = os.path.join(ROOT, "topics")

def save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def write_exam(exam_dir, class_slug, board, exam_type, exam_config, test_types, subjects_data):
    """Write index.json + subjects/*.json + full-length.json for one exam/class."""

    # ── subject stubs for index ───────────────────────────────────────────────
    stubs = []
    for s in subjects_data:
        stubs.append({
            "slug":      s["slug"],
            "name":      s["name"],
            "icon":      s.get("icon", "📚"),
            "color":     s.get("color", "#3b82f6"),
            "weightage": s.get("weightage", 1),
            "file":      f"subjects/{s['slug']}.json"
        })

    index = {
        "classSlug":  class_slug,
        "board":      board,
        "type":       exam_type,
        "examConfig": exam_config,
        "testTypes":  test_types,
        "subjects":   stubs
    }
    save(os.path.join(exam_dir, "index.json"), index)

    # ── individual subject files ──────────────────────────────────────────────
    for s in subjects_data:
        subj_file = {
            "slug":      s["slug"],
            "name":      s["name"],
            "icon":      s.get("icon", "📚"),
            "color":     s.get("color", "#3b82f6"),
            "weightage": s.get("weightage", 1),
            "classSlug": class_slug,
            "board":     board,
            "chapters":  s.get("chapters", [])
        }
        save(os.path.join(exam_dir, "subjects", s["slug"] + ".json"), subj_file)

    # ── full-length.json ──────────────────────────────────────────────────────
    total_q      = exam_config.get("fullLengthQuestions", 100)
    total_marks  = exam_config.get("fullLengthMarks", total_q)
    duration     = exam_config.get("fullLengthDuration", 90)
    total_weight = sum(s.get("weightage", 1) for s in subjects_data) or 1
    fl_subjects  = []
    for s in subjects_data:
        w = s.get("weightage", 1)
        q = max(1, round((w / total_weight) * total_q))
        fl_subjects.append({
            "subjectSlug":   s["slug"],
            "subjectName":   s["name"],
            "questionCount": q
        })
    save(os.path.join(exam_dir, "full-length.json"), {
        "classSlug":      class_slug,
        "board":          board,
        "totalQuestions": total_q,
        "totalMarks":     total_marks,
        "duration":       duration,
        "subjects":       fl_subjects
    })

    print(f"  [OK] {class_slug} — {len(subjects_data)} subjects")


# ═══════════════════════════════════════════════════════════════════════════════
# COMPETITIVE EXAM DATA
# ═══════════════════════════════════════════════════════════════════════════════

COMPETITIVE_EXAMS = {}

# ── NEET UG ───────────────────────────────────────────────────────────────────
COMPETITIVE_EXAMS["neet-ug"] = {
  "board": "Competitive", "type": "competitive",
  "examConfig": {"fullLengthQuestions":180,"fullLengthMarks":720,"fullLengthDuration":200,
    "subjectWiseQuestions":45,"subjectWiseMarks":180,"subjectWiseDuration":60,
    "negativeMarking":True,"correctMarkValue":4,"negativeMarkValue":1},
  "testTypes": [
    {"type":"full-length","label":"Full Length Test","questions":180,"marks":720,"duration":200},
    {"type":"subject-wise","label":"Subject Wise Test","questions":45,"marks":180,"duration":60},
    {"type":"chapter-wise","label":"Chapter Wise Test","questions":30,"marks":120,"duration":40},
    {"type":"topic-wise","label":"Topic Wise Test","questions":15,"marks":60,"duration":20}
  ],
  "subjects": [
    {"slug":"neet-physics","name":"Physics","icon":"⚛️","color":"#3b82f6","weightage":25,"chapters":[
      {"name":"Mechanics","topics":["Laws of Motion","Work Energy Power","Rotational Motion","Gravitation","Simple Harmonic Motion","Fluid Mechanics","Elasticity","Surface Tension","Viscosity","Bernoulli Theorem","Projectile Motion","Circular Motion","Centre of Mass","Collision","Friction"]},
      {"name":"Thermodynamics","topics":["Kinetic Theory of Gases","Laws of Thermodynamics","Carnot Engine","Heat Transfer","Thermal Expansion","Specific Heat","Calorimetry","Radiation","Stefan Law","Newton Law of Cooling"]},
      {"name":"Electrostatics","topics":["Coulombs Law","Electric Field","Electric Potential","Capacitors","Gauss Law","Dielectrics","Van de Graaff Generator","Electric Flux","Equipotential Surfaces","Energy Stored in Capacitor"]},
      {"name":"Current Electricity","topics":["Ohms Law","Kirchhoffs Laws","Wheatstone Bridge","Potentiometer","Meter Bridge","Cells EMF","Internal Resistance","Power Dissipation","Drift Velocity","Mobility"]},
      {"name":"Magnetism","topics":["Biot Savart Law","Amperes Law","Magnetic Force","Cyclotron","Moving Coil Galvanometer","Electromagnetic Induction","Faradays Law","Lenz Law","Mutual Inductance","AC Circuits"]},
      {"name":"Optics","topics":["Reflection","Refraction","Total Internal Reflection","Lenses","Mirrors","Optical Instruments","Wave Optics","Interference","Diffraction","Polarisation"]},
      {"name":"Modern Physics","topics":["Photoelectric Effect","de Broglie Wavelength","Bohr Model","Nuclear Fission","Nuclear Fusion","Radioactivity","X Rays","Semiconductors","Logic Gates","Communication Systems"]}
    ]},
    {"slug":"neet-chemistry","name":"Chemistry","icon":"🧪","color":"#10b981","weightage":25,"chapters":[
      {"name":"Physical Chemistry","topics":["Mole Concept","Atomic Structure","Chemical Bonding","States of Matter","Thermodynamics","Equilibrium","Electrochemistry","Chemical Kinetics","Surface Chemistry","Solutions"]},
      {"name":"Inorganic Chemistry","topics":["Periodic Table","s Block Elements","p Block Elements","d Block Elements","f Block Elements","Coordination Compounds","Metallurgy","Qualitative Analysis","Hydrogen","Environmental Chemistry"]},
      {"name":"Organic Chemistry","topics":["IUPAC Nomenclature","Isomerism","Hydrocarbons","Haloalkanes","Alcohols Phenols","Aldehydes Ketones","Carboxylic Acids","Amines","Biomolecules","Polymers"]}
    ]},
    {"slug":"neet-botany","name":"Botany","icon":"🌿","color":"#22c55e","weightage":25,"chapters":[
      {"name":"Cell Biology","topics":["Cell Theory","Prokaryotic Cell","Eukaryotic Cell","Cell Organelles","Cell Division Mitosis","Cell Division Meiosis","Cell Wall","Plasma Membrane","Nucleus","Endoplasmic Reticulum"]},
      {"name":"Plant Physiology","topics":["Photosynthesis Light Reactions","Calvin Cycle","Transpiration","Mineral Nutrition","Respiration in Plants","Plant Growth Regulators","Seed Germination","Photoperiodism","Vernalisation","Stomatal Movement"]},
      {"name":"Genetics","topics":["Mendels Laws","Chromosomal Theory","Linkage Crossing Over","Sex Determination","Mutation","DNA Structure","DNA Replication","Transcription","Translation","Gene Regulation"]},
      {"name":"Plant Diversity","topics":["Algae","Bryophytes","Pteridophytes","Gymnosperms","Angiosperms","Taxonomy Classification","Morphology of Flowering Plants","Anatomy of Flowering Plants","Reproduction in Plants","Pollination"]}
    ]},
    {"slug":"neet-zoology","name":"Zoology","icon":"🐾","color":"#f59e0b","weightage":25,"chapters":[
      {"name":"Human Physiology","topics":["Digestion Absorption","Breathing Gas Exchange","Body Fluids Circulation","Excretory Products","Locomotion Movement","Neural Control","Chemical Coordination","Reproduction","Immune System","Endocrine Glands"]},
      {"name":"Animal Diversity","topics":["Animal Kingdom Classification","Phylum Porifera","Phylum Coelenterata","Phylum Platyhelminthes","Phylum Nematoda","Phylum Annelida","Phylum Arthropoda","Phylum Mollusca","Phylum Echinodermata","Phylum Chordata"]},
      {"name":"Evolution","topics":["Origin of Life","Darwins Theory","Natural Selection","Hardy Weinberg Principle","Speciation","Human Evolution","Evidences of Evolution","Adaptive Radiation","Lamarckism","Modern Synthetic Theory"]},
      {"name":"Ecology","topics":["Ecosystem Structure","Food Chain Web","Energy Flow","Biogeochemical Cycles","Population Ecology","Community Ecology","Biodiversity","Environmental Issues","Succession","Biomes"]}
    ]}
  ]
}

# ── JEE MAINS ─────────────────────────────────────────────────────────────────
COMPETITIVE_EXAMS["jee-mains"] = {
  "board": "Competitive", "type": "competitive",
  "examConfig": {"fullLengthQuestions":90,"fullLengthMarks":300,"fullLengthDuration":180,
    "subjectWiseQuestions":30,"subjectWiseMarks":100,"subjectWiseDuration":60,
    "negativeMarking":True,"correctMarkValue":4,"negativeMarkValue":1},
  "testTypes": [
    {"type":"full-length","label":"Full Length Test","questions":90,"marks":300,"duration":180},
    {"type":"subject-wise","label":"Subject Wise Test","questions":30,"marks":100,"duration":60},
    {"type":"chapter-wise","label":"Chapter Wise Test","questions":20,"marks":80,"duration":40},
    {"type":"topic-wise","label":"Topic Wise Test","questions":10,"marks":40,"duration":20}
  ],
  "subjects": [
    {"slug":"jee-physics","name":"Physics","icon":"⚛️","color":"#3b82f6","weightage":33,"chapters":[
      {"name":"Mechanics","topics":["Kinematics 1D 2D","Laws of Motion","Work Power Energy","Rotational Dynamics","Gravitation","Simple Harmonic Motion","Fluid Statics Dynamics","Elasticity","Waves","Sound Waves"]},
      {"name":"Thermodynamics","topics":["Thermal Properties","Kinetic Theory","Thermodynamic Processes","Heat Engines","Entropy","Calorimetry","Heat Transfer Conduction","Radiation","Thermal Expansion","Psychrometry"]},
      {"name":"Electromagnetism","topics":["Electrostatics","Gauss Law","Capacitance","Current Electricity","Magnetic Effects","Electromagnetic Induction","Alternating Current","Electromagnetic Waves","Ray Optics","Wave Optics"]},
      {"name":"Modern Physics","topics":["Dual Nature of Matter","Atomic Models","Nuclear Physics","Radioactivity","Semiconductors","Communication Systems","Photoelectric Effect","X Rays","Nuclear Reactions","Laser"]}
    ]},
    {"slug":"jee-chemistry","name":"Chemistry","icon":"🧪","color":"#10b981","weightage":33,"chapters":[
      {"name":"Physical Chemistry","topics":["Mole Concept Stoichiometry","Atomic Structure","Chemical Bonding VSEPR","Gaseous State","Thermochemistry","Chemical Equilibrium","Ionic Equilibrium","Electrochemistry","Chemical Kinetics","Nuclear Chemistry"]},
      {"name":"Inorganic Chemistry","topics":["Periodic Properties","Hydrogen Compounds","s Block","p Block Group 13 14","p Block Group 15 16 17 18","d Block Transition","f Block Lanthanides","Coordination Chemistry","Metallurgy","Qualitative Salt Analysis"]},
      {"name":"Organic Chemistry","topics":["General Organic Chemistry","Hydrocarbons Alkanes","Alkenes Alkynes","Aromatic Compounds","Haloalkanes Haloarenes","Alcohols Ethers","Carbonyl Compounds","Carboxylic Acids Derivatives","Nitrogen Compounds","Biomolecules Polymers"]}
    ]},
    {"slug":"jee-maths","name":"Mathematics","icon":"📐","color":"#8b5cf6","weightage":34,"chapters":[
      {"name":"Algebra","topics":["Complex Numbers","Quadratic Equations","Sequences Series","Permutations Combinations","Binomial Theorem","Matrices Determinants","Mathematical Induction","Sets Relations Functions","Logarithms","Inequalities"]},
      {"name":"Calculus","topics":["Limits Continuity","Differentiability","Differentiation","Applications of Derivatives","Indefinite Integration","Definite Integration","Area Under Curves","Differential Equations","Mean Value Theorems","L Hopital Rule"]},
      {"name":"Coordinate Geometry","topics":["Straight Lines","Circles","Parabola","Ellipse","Hyperbola","3D Geometry","Vectors","Distance Formula","Section Formula","Locus"]},
      {"name":"Trigonometry","topics":["Trigonometric Ratios","Trigonometric Identities","Inverse Trigonometry","Heights and Distances","Properties of Triangles","Trigonometric Equations","General Solutions","Sine Cosine Rule","Half Angle Formulas","Product Formulas"]}
    ]}
  ]
}

# ── JEE ADVANCED ──────────────────────────────────────────────────────────────
COMPETITIVE_EXAMS["jee-advanced"] = {
  "board": "Competitive", "type": "competitive",
  "examConfig": {"fullLengthQuestions":54,"fullLengthMarks":183,"fullLengthDuration":180,
    "subjectWiseQuestions":18,"subjectWiseMarks":61,"subjectWiseDuration":60,
    "negativeMarking":True,"correctMarkValue":3,"negativeMarkValue":1},
  "testTypes": [
    {"type":"full-length","label":"Full Length Test","questions":54,"marks":183,"duration":180},
    {"type":"subject-wise","label":"Subject Wise Test","questions":18,"marks":61,"duration":60},
    {"type":"chapter-wise","label":"Chapter Wise Test","questions":12,"marks":40,"duration":40}
  ],
  "subjects": [
    {"slug":"jee-adv-physics","name":"Physics","icon":"⚛️","color":"#3b82f6","weightage":33,"chapters":[
      {"name":"Mechanics Advanced","topics":["Rigid Body Dynamics","Rolling Motion","Angular Momentum","Gyroscope","Fluid Mechanics Advanced","Elasticity Stress Strain","Dimensional Analysis","Error Analysis","Relative Motion","Non Inertial Frames"]},
      {"name":"Waves Optics","topics":["Superposition of Waves","Stationary Waves","Doppler Effect","Youngs Double Slit","Diffraction Grating","Polarisation Brewster","Optical Instruments Advanced","Interference Thin Films","Resolving Power","Huygen Principle"]},
      {"name":"Electromagnetism Advanced","topics":["Electric Field Advanced","Potential Energy Systems","Capacitor Networks","RC Circuits","Magnetic Field Advanced","Electromagnetic Induction Advanced","LCR Circuits","Transformer","Maxwell Equations","Displacement Current"]},
      {"name":"Modern Physics Advanced","topics":["Photoelectric Effect Advanced","Compton Effect","de Broglie Hypothesis","Bohr Model Advanced","Nuclear Binding Energy","Radioactive Decay","Fission Fusion","Semiconductor Devices","Logic Gates","Particle Physics Basics"]}
    ]},
    {"slug":"jee-adv-chemistry","name":"Chemistry","icon":"🧪","color":"#10b981","weightage":33,"chapters":[
      {"name":"Physical Chemistry Advanced","topics":["Thermodynamics Advanced","Electrochemistry Advanced","Chemical Kinetics Advanced","Colligative Properties","Phase Equilibrium","Adsorption Isotherms","Colloids","Quantum Numbers","Molecular Orbital Theory","Hybridisation Advanced"]},
      {"name":"Inorganic Advanced","topics":["Crystal Field Theory","Isomerism in Coordination","Organometallics","Bioinorganic Chemistry","Industrial Chemistry","Cement Glass Ceramics","Extraction of Metals","Analytical Chemistry","Qualitative Analysis Advanced","Periodic Trends Advanced"]},
      {"name":"Organic Advanced","topics":["Reaction Mechanisms","Stereochemistry","Named Reactions","Retrosynthesis","Spectroscopy NMR IR","Carbohydrates Advanced","Amino Acids Proteins","Nucleic Acids","Dyes Drugs","Polymers Advanced"]}
    ]},
    {"slug":"jee-adv-maths","name":"Mathematics","icon":"��","color":"#8b5cf6","weightage":34,"chapters":[
      {"name":"Calculus Advanced","topics":["Rolle Mean Value Theorem","Taylor Maclaurin Series","Improper Integrals","Multiple Integrals","Vector Calculus","Differential Equations Advanced","Partial Derivatives","Maxima Minima Advanced","Curve Tracing","Asymptotes"]},
      {"name":"Algebra Advanced","topics":["Group Theory Basics","Number Theory","Functional Equations","Inequalities Advanced","Combinatorics Advanced","Probability Advanced","Complex Numbers Advanced","Polynomials","Recurrence Relations","Graph Theory Basics"]},
      {"name":"Geometry Advanced","topics":["Conic Sections Advanced","3D Geometry Advanced","Vectors Advanced","Projective Geometry","Coordinate Geometry Advanced","Locus Problems","Parametric Equations","Polar Coordinates","Transformations","Geometric Inequalities"]}
    ]}
  ]
}


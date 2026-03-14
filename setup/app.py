"""
arXiv Digest — Setup Wizard
A Streamlit web app that helps researchers configure their personal arXiv digest.
Generates a config.yaml (+ workflow snippet) ready to use with the arxiv-digest template.

Created by Silke S. Dainese · dainese@phys.au.dk · silkedainese.github.io
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import yaml
import streamlit as st

try:
    import anthropic as _anthropic_lib
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

try:
    from google import genai as _genai_lib
    from google.genai import types as _genai_types
    _GEMINI_AVAILABLE = True
except ImportError:
    _GEMINI_AVAILABLE = False

# Allow imports from the project root (one level up from setup/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brand import PINE, GOLD, CARD_BORDER, WARM_GREY
from pure_scraper import fetch_orcid_person, fetch_orcid_works, find_au_colleagues, scrape_pure_profile, search_pure_profiles

# ─────────────────────────────────────────────────────────────
#  Page config
# ─────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="arXiv Digest Setup",
    page_icon="🔭",
    layout="centered",
)

# ── Custom CSS for brand styling ──
st.markdown(f"""
<style>
    @import url('https://fonts.googleapis.com/css2?family=DM+Serif+Display&family=IBM+Plex+Sans:wght@300;400;600&family=DM+Mono:wght@400&display=swap');

    h1, h2, h3 {{ font-family: 'DM Serif Display', Georgia, serif !important; }}
    .stMarkdown p, .stMarkdown li {{ font-family: 'IBM Plex Sans', sans-serif; }}
    code, .stCode {{ font-family: 'DM Mono', monospace !important; }}

    /* Brand card styling */
    .brand-card {{
        background: white;
        border: 1px solid {CARD_BORDER};
        border-radius: 6px;
        padding: 24px;
        margin: 12px 0;
    }}
    .brand-label {{
        font-family: 'DM Mono', monospace;
        font-size: 10px;
        letter-spacing: 0.1em;
        text-transform: uppercase;
        color: {WARM_GREY};
    }}
    .step-number {{
        display: inline-block;
        background: {PINE};
        color: white;
        width: 28px;
        height: 28px;
        border-radius: 50%;
        text-align: center;
        line-height: 28px;
        font-family: 'DM Mono', monospace;
        font-size: 14px;
        margin-right: 8px;
    }}
</style>
""", unsafe_allow_html=True)


_ORCID_ID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")


def _weight_label(w: int) -> str:
    """Return a human-readable label for a keyword weight (1–10)."""
    if w <= 2:
        return "loosely follow"
    if w <= 5:
        return "interested"
    if w <= 8:
        return "main field"
    return "everything"


# ─────────────────────────────────────────────────────────────
#  arXiv categories + AI suggestion hints
# ─────────────────────────────────────────────────────────────

ARXIV_CATEGORIES = {
    # Astrophysics
    "astro-ph.EP": "Earth and Planetary Astrophysics",
    "astro-ph.SR": "Solar and Stellar Astrophysics",
    "astro-ph.GA": "Astrophysics of Galaxies",
    "astro-ph.CO": "Cosmology and Nongalactic Astrophysics",
    "astro-ph.HE": "High Energy Astrophysical Phenomena",
    "astro-ph.IM": "Instrumentation and Methods",
    # Condensed Matter
    "cond-mat.dis-nn": "Disordered Systems and Neural Networks",
    "cond-mat.mes-hall": "Mesoscale and Nanoscale Physics",
    "cond-mat.mtrl-sci": "Materials Science",
    "cond-mat.other": "Other Condensed Matter",
    "cond-mat.quant-gas": "Quantum Gases",
    "cond-mat.soft": "Soft Condensed Matter",
    "cond-mat.stat-mech": "Statistical Mechanics",
    "cond-mat.str-el": "Strongly Correlated Electrons",
    "cond-mat.supr-con": "Superconductivity",
    # General Relativity & Quantum Cosmology
    "gr-qc": "General Relativity and Quantum Cosmology",
    # High Energy Physics
    "hep-ex": "High Energy Physics — Experiment",
    "hep-lat": "High Energy Physics — Lattice",
    "hep-ph": "High Energy Physics — Phenomenology",
    "hep-th": "High Energy Physics — Theory",
    # Mathematical Physics
    "math-ph": "Mathematical Physics",
    # Nonlinear Sciences
    "nlin.AO": "Adaptation and Self-Organizing Systems",
    "nlin.CD": "Chaotic Dynamics",
    "nlin.CG": "Cellular Automata and Lattice Gases",
    "nlin.PS": "Pattern Formation and Solitons",
    "nlin.SI": "Exactly Solvable and Integrable Systems",
    # Nuclear
    "nucl-ex": "Nuclear Experiment",
    "nucl-th": "Nuclear Theory",
    # Physics
    "physics.acc-ph": "Accelerator Physics",
    "physics.ao-ph": "Atmospheric and Oceanic Physics",
    "physics.atom-ph": "Atomic Physics",
    "physics.atm-clus": "Atomic and Molecular Clusters",
    "physics.bio-ph": "Biological Physics",
    "physics.chem-ph": "Chemical Physics",
    "physics.class-ph": "Classical Physics",
    "physics.comp-ph": "Computational Physics",
    "physics.data-an": "Data Analysis, Statistics and Probability",
    "physics.ed-ph": "Physics Education",
    "physics.flu-dyn": "Fluid Dynamics",
    "physics.gen-ph": "General Physics",
    "physics.geo-ph": "Geophysics",
    "physics.hist-ph": "History and Philosophy of Physics",
    "physics.ins-det": "Instrumentation and Detectors",
    "physics.med-ph": "Medical Physics",
    "physics.optics": "Optics",
    "physics.plasm-ph": "Plasma Physics",
    "physics.pop-ph": "Popular Physics",
    "physics.soc-ph": "Physics and Society",
    "physics.space-ph": "Space Physics",
    # Quantum Physics
    "quant-ph": "Quantum Physics",
    # Computer Science
    "cs.AI": "Artificial Intelligence",
    "cs.CL": "Computation and Language (NLP)",
    "cs.CV": "Computer Vision",
    "cs.DS": "Data Structures and Algorithms",
    "cs.LG": "Machine Learning",
    "cs.NE": "Neural and Evolutionary Computing",
    "cs.RO": "Robotics",
    # Mathematics
    "math.AG": "Algebraic Geometry",
    "math.AP": "Analysis of PDEs",
    "math.DG": "Differential Geometry",
    "math.DS": "Dynamical Systems",
    "math.NT": "Number Theory",
    "math.PR": "Probability",
    "math.ST": "Statistics Theory",
    # Statistics
    "stat.AP": "Applications (Statistics)",
    "stat.CO": "Computation (Statistics)",
    "stat.ME": "Methodology (Statistics)",
    "stat.ML": "Machine Learning (Statistics)",
    "stat.TH": "Statistics Theory",
    # Electrical Engineering
    "eess.AS": "Audio and Speech Processing",
    "eess.IV": "Image and Video Processing",
    "eess.SP": "Signal Processing",
    "eess.SY": "Systems and Control",
    # Quantitative Biology
    "q-bio.BM": "Biomolecules",
    "q-bio.CB": "Cell Behavior",
    "q-bio.GN": "Genomics",
    "q-bio.NC": "Neurons and Cognition",
    "q-bio.PE": "Populations and Evolution",
    "q-bio.QM": "Quantitative Methods",
}

# Hierarchical grouping for the category picker UI
ARXIV_GROUPS = {
    "Astrophysics": [
        "astro-ph.EP", "astro-ph.SR", "astro-ph.GA",
        "astro-ph.CO", "astro-ph.HE", "astro-ph.IM",
    ],
    "Condensed Matter": [
        "cond-mat.dis-nn", "cond-mat.mes-hall", "cond-mat.mtrl-sci",
        "cond-mat.other", "cond-mat.quant-gas", "cond-mat.soft",
        "cond-mat.stat-mech", "cond-mat.str-el", "cond-mat.supr-con",
    ],
    "General Relativity & Quantum Cosmology": ["gr-qc"],
    "High Energy Physics": ["hep-ex", "hep-lat", "hep-ph", "hep-th"],
    "Mathematical Physics": ["math-ph"],
    "Nonlinear Sciences": ["nlin.AO", "nlin.CD", "nlin.CG", "nlin.PS", "nlin.SI"],
    "Nuclear Physics": ["nucl-ex", "nucl-th"],
    "Physics": [
        "physics.acc-ph", "physics.ao-ph", "physics.atom-ph", "physics.atm-clus",
        "physics.bio-ph", "physics.chem-ph", "physics.class-ph", "physics.comp-ph",
        "physics.data-an", "physics.ed-ph", "physics.flu-dyn", "physics.gen-ph",
        "physics.geo-ph", "physics.hist-ph", "physics.ins-det", "physics.med-ph",
        "physics.optics", "physics.plasm-ph", "physics.pop-ph", "physics.soc-ph",
        "physics.space-ph",
    ],
    "Quantum Physics": ["quant-ph"],
    "Computer Science": ["cs.AI", "cs.CL", "cs.CV", "cs.DS", "cs.LG", "cs.NE", "cs.RO"],
    "Mathematics": ["math.AG", "math.AP", "math.DG", "math.DS", "math.NT", "math.PR", "math.ST"],
    "Statistics": ["stat.AP", "stat.CO", "stat.ME", "stat.ML", "stat.TH"],
    "Electrical Engineering": ["eess.AS", "eess.IV", "eess.SP", "eess.SY"],
    "Quantitative Biology": [
        "q-bio.BM", "q-bio.CB", "q-bio.GN", "q-bio.NC", "q-bio.PE", "q-bio.QM",
    ],
}

# "Include if" hints — shown inside each group expander
ARXIV_GROUP_HINTS = {
    "Astrophysics": "You study stars, planets, galaxies, or cosmic phenomena.",
    "Condensed Matter": "You study materials, superconductors, or quantum systems in matter.",
    "General Relativity & Quantum Cosmology": "You work on spacetime, black holes, or gravitational waves.",
    "High Energy Physics": "You work on particles, colliders, or fundamental field theory.",
    "Mathematical Physics": "You bridge rigorous mathematics and physical theory.",
    "Nonlinear Sciences": "You study chaos, complex systems, or emergent patterns.",
    "Nuclear Physics": "You study atomic nuclei, nuclear reactions, or related theory.",
    "Physics": "You work in applied or specialized sub-fields of physics.",
    "Quantum Physics": "You work on quantum information, computing, or quantum optics.",
    "Computer Science": "You use or develop AI, ML, NLP, vision, or robotics methods.",
    "Mathematics": "You work in pure or applied mathematics.",
    "Statistics": "You work on statistical methods, ML theory, or data analysis.",
    "Electrical Engineering": "You process signals, audio, images, or design control systems.",
    "Quantitative Biology": "You apply quantitative methods to biological systems.",
}

# Terms that hint at which arXiv categories to suggest
CATEGORY_HINTS = {
    "astro-ph.EP": [
        "exoplanet", "planet formation", "transit", "radial velocity", "habitable",
        "atmosphere", "JWST", "Kepler", "TESS", "circumbinary", "hot jupiter",
        "sub-neptune", "super-earth", "protoplanetary", "protoplanetary disk",
        "planetary system", "planet formation", "transmission spectroscopy",
        "radial-velocity", "earth-like", "biosignature", "accretion disk",
    ],
    "astro-ph.SR": [
        "stellar", "stellar rotation", "binary star", "spectroscopy", "magnetic",
        "angular momentum", "obliquity", "spin-orbit", "vsini", "vbroad", "Gaia",
        "gyrochronology", "asteroseismology", "variable star", "pulsation",
        "white dwarf", "red giant", "main sequence", "chromosphere",
        "flare", "metallicity", "abundance", "spectral type", "eclipsing binary",
        "Rossiter-McLaughlin", "Doppler tomography", "Kraft break",
    ],
    "astro-ph.GA": [
        "galaxy", "galaxies", "galactic", "Milky Way", "dark matter",
        "interstellar", "ISM", "star formation", "AGN", "quasar",
        "merger", "cluster", "halo", "bulge", "spiral", "elliptical",
        "HII region", "nebula", "chemical evolution", "stellar population",
    ],
    "astro-ph.CO": [
        "cosmolog", "CMB", "dark energy", "inflation", "baryon",
        "large-scale structure", "BAO", "Hubble", "redshift", "gravitational lensing",
        "cosmic microwave", "primordial", "Big Bang", "expansion",
    ],
    "astro-ph.HE": [
        "black hole", "neutron star", "pulsar", "magnetar", "GRB",
        "gamma-ray", "X-ray", "accretion", "relativistic jet", "relativistic",
        "gravitational wave", "LIGO", "compact object", "supernova",
    ],
    "astro-ph.IM": [
        "instrument", "detector", "telescope", "survey", "pipeline",
        "calibration", "photometry", "astrometry", "spectrograph",
        "adaptive optics", "CCD", "coronagraph", "interferometry",
    ],
    "hep-th": [
        "string theory", "quantum field theory", "supersymmetry", "AdS/CFT",
        "holograph", "conformal field", "gauge theory", "brane", "S-matrix",
        "amplitude", "duality", "topological field theory",
    ],
    "hep-ph": [
        "particle physics", "Standard Model", "Higgs", "collider", "LHC",
        "neutrino", "dark matter candidate", "beyond Standard Model",
        "parton", "QCD", "electroweak", "flavor physics", "CP violation",
    ],
    "hep-ex": [
        "collider experiment", "ATLAS", "CMS", "LHCb", "Belle", "BaBar",
        "particle detector", "particle beam", "cross section measurement",
        "high energy experiment", "calorimeter",
    ],
    "hep-lat": [
        "lattice QCD", "lattice gauge", "Monte Carlo lattice", "Wilson fermion",
        "lattice field theory", "non-perturbative QCD",
    ],
    "gr-qc": [
        "general relativity", "gravitational wave", "black hole", "spacetime",
        "metric", "Einstein", "curvature", "singularity", "LIGO",
        "post-Newtonian", "numerical relativity", "wormhole", "geodesic",
    ],
    "math-ph": [
        "mathematical physics", "rigorous", "spectral theory", "operator algebra",
        "integrable system", "Hamiltonian", "symplectic", "functional analysis",
    ],
    "nucl-th": [
        "nuclear theory", "nuclear structure", "shell model", "nuclear force",
        "nucleon", "hadronic", "quark-gluon plasma", "nuclear matter", "fission",
    ],
    "nucl-ex": [
        "nuclear experiment", "nuclear reaction", "radioactive beam", "heavy-ion",
        "nuclear decay", "nuclear spectroscopy", "CERN", "RHIC", "nuclear cross section",
    ],
    "nlin.CD": [
        "chaos", "chaotic dynamics", "Lyapunov", "strange attractor", "bifurcation",
        "nonlinear dynamics", "sensitive dependence",
    ],
    "nlin.PS": [
        "soliton", "pattern formation", "nonlinear wave", "reaction-diffusion",
        "Turing pattern", "amplitude equation", "modulational instability",
    ],
    "nlin.SI": [
        "integrable", "inverse scattering", "Lax pair", "Painlevé", "exact solution",
        "conservation law", "Bäcklund",
    ],
    "nlin.AO": [
        "self-organization", "adaptation", "complex system", "emergence",
        "network dynamics", "synchronization",
    ],
    "cond-mat.supr-con": [
        "superconductor", "superconductivity", "BCS", "Cooper pair", "vortex",
        "Josephson", "pairing", "Tc", "Meissner", "flux",
    ],
    "cond-mat.str-el": [
        "strongly correlated", "Mott insulator", "Hubbard", "Kondo", "heavy fermion",
        "correlated electron", "charge order", "orbital order", "spin liquid", "Wigner",
    ],
    "cond-mat.mes-hall": [
        "quantum dot", "quantum well", "nanostructure", "mesoscopic", "Hall effect",
        "edge state", "nanowire", "carbon nanotube", "graphene", "2D material",
        "topological insulator", "Dirac", "Weyl", "quantum transport", "spintronics",
    ],
    "cond-mat.mtrl-sci": [
        "material", "thin film", "alloy", "doping", "defect", "grain boundary",
        "first-principles", "DFT", "density functional", "ab initio", "crystal structure",
        "X-ray diffraction", "XRD", "synthesis", "epitaxy", "heterostructure",
    ],
    "cond-mat.stat-mech": [
        "statistical mechanics", "phase transition", "critical exponent", "renormalization",
        "Ising model", "Monte Carlo", "entropy", "free energy", "thermodynamic",
        "universality", "scaling", "order parameter",
    ],
    "cond-mat.soft": [
        "soft matter", "polymer", "colloid", "liquid crystal", "gel", "foam",
        "active matter", "self-assembly", "rheology", "viscoelastic", "amphiphile",
    ],
    "cond-mat.quant-gas": [
        "ultracold", "Bose-Einstein condensate", "BEC", "optical lattice", "cold atom",
        "Fermi gas", "Feshbach resonance", "superfluidity", "quantum gas",
    ],
    "cond-mat.dis-nn": [
        "disorder", "Anderson localization", "spin glass", "random", "amorphous",
        "percolation", "neural network", "glassy", "many-body localization", "MBL",
    ],
    "quant-ph": [
        "quantum computing", "qubit", "entanglement", "quantum information",
        "quantum optics", "decoherence", "quantum error", "quantum algorithm",
    ],
    "physics.atom-ph": [
        "atomic physics", "cold atoms", "laser cooling", "Bose-Einstein", "ion trap",
        "optical clock", "precision measurement", "atomic spectrum", "photoionization",
    ],
    "physics.atm-clus": [
        "atomic cluster", "nanoparticle", "fullerene", "cluster physics",
        "molecular cluster", "van der Waals cluster",
    ],
    "physics.chem-ph": [
        "chemical physics", "molecular physics", "reaction dynamics", "potential energy surface",
        "spectroscopy", "photochemistry", "adiabatic", "Born-Oppenheimer",
    ],
    "physics.comp-ph": [
        "computational physics", "simulation", "numerical method", "finite element",
        "molecular dynamics simulation", "Monte Carlo simulation", "FDTD", "algorithm",
    ],
    "physics.plasm-ph": [
        "plasma", "fusion", "tokamak", "plasma wave", "magnetohydrodynamic", "MHD",
        "inertial confinement", "plasma instability", "ITER", "laser plasma",
    ],
    "physics.space-ph": [
        "space physics", "solar wind", "magnetosphere", "ionosphere", "cosmic ray",
        "heliosphere", "aurora", "geomagnetic storm", "Van Allen",
    ],
    "physics.ao-ph": [
        "atmospheric", "climate", "ocean", "meteorology", "geophysical fluid",
        "El Niño", "general circulation", "aerosol", "cloud physics",
    ],
    "physics.geo-ph": [
        "geophysics", "seismic", "earthquake", "mantle", "plate tectonics",
        "seismology", "geodesy", "geodynamics", "geomagnetism",
    ],
    "physics.ins-det": [
        "detector", "instrumentation", "sensor", "readout", "signal processing",
        "particle detector", "scintillator", "photodetector", "FPGA",
    ],
    "physics.med-ph": [
        "medical physics", "radiation therapy", "MRI", "CT scan", "dosimetry",
        "radiobiology", "proton therapy", "nuclear medicine", "imaging",
    ],
    "physics.optics": [
        "optical", "laser", "photon", "waveguide", "fiber", "lens",
        "diffraction", "nonlinear optics", "ultrafast", "plasmon",
    ],
    "physics.bio-ph": [
        "biophysics", "protein", "membrane", "DNA", "RNA", "cell",
        "molecular dynamics", "biological", "enzyme", "single-molecule",
    ],
    "physics.flu-dyn": [
        "fluid", "turbulence", "Navier-Stokes", "flow", "viscous",
        "Reynolds", "boundary layer", "vortex", "aerodynamics",
    ],
    "physics.acc-ph": [
        "accelerator", "synchrotron", "free electron laser", "beam physics",
        "particle accelerator", "storage ring", "undulator", "linac",
    ],
    "physics.data-an": [
        "data analysis", "statistical method", "uncertainty quantification",
        "systematic error", "likelihood", "goodness of fit",
    ],
    "cs.AI": [
        "artificial intelligence", "reasoning", "planning", "knowledge",
        "agent", "reinforcement learning", "multi-agent",
    ],
    "cs.LG": [
        "machine learning", "deep learning", "neural network", "transformer",
        "GPT", "training", "gradient", "optimization", "generalization",
    ],
    "cs.CV": [
        "computer vision", "image", "object detection", "segmentation",
        "convolutional", "CNN", "visual", "recognition",
    ],
    "cs.CL": [
        "natural language", "NLP", "language model", "text", "translation",
        "sentiment", "parsing", "BERT", "tokeniz",
    ],
    "cs.DS": [
        "algorithm", "data structure", "complexity", "graph algorithm",
        "sorting", "combinatorial", "approximation algorithm",
    ],
    "cs.RO": [
        "robot", "robotics", "motion planning", "control", "autonomous",
        "SLAM", "manipulation", "drone",
    ],
    "cs.NE": [
        "evolutionary", "genetic algorithm", "swarm", "neuroevolution",
        "neural architecture search", "bio-inspired",
    ],
    "math.AP": [
        "partial differential equation", "PDE", "elliptic", "parabolic",
        "hyperbolic", "weak solution", "regularity", "Sobolev",
    ],
    "math.PR": [
        "probability", "stochastic process", "Brownian motion", "Markov chain",
        "random walk", "martingale", "diffusion process",
    ],
    "math.ST": [
        "statistics", "hypothesis testing", "estimator", "asymptotic",
        "consistency", "maximum likelihood", "nonparametric",
    ],
    "math.DS": [
        "dynamical system", "ergodic", "attractor", "invariant measure",
        "topological dynamics", "symbolic dynamics",
    ],
    "math.NT": [
        "number theory", "prime", "Riemann", "arithmetic", "Diophantine",
        "algebraic number", "modular form", "L-function",
    ],
    "math.AG": [
        "algebraic geometry", "variety", "scheme", "sheaf", "cohomology",
        "moduli", "Hodge theory", "Calabi-Yau",
    ],
    "math.DG": [
        "differential geometry", "manifold", "Riemannian", "curvature tensor",
        "connection", "fiber bundle", "symplectic manifold",
    ],
    "math-ph": [
        "mathematical physics", "rigorous", "spectral theory", "operator algebra",
        "integrable system", "Hamiltonian", "symplectic",
    ],
    "stat.ML": [
        "statistical learning", "Bayesian", "inference", "regression",
        "classification", "kernel", "non-parametric", "MCMC",
    ],
    "stat.ME": [
        "statistical methodology", "survey sampling", "experimental design",
        "causal inference", "missing data", "mixed model",
    ],
    "stat.AP": [
        "applied statistics", "biostatistics", "clinical trial", "survival analysis",
        "epidemiology", "econometrics",
    ],
    "eess.SP": [
        "signal processing", "Fourier", "filter", "time series", "spectral estimation",
        "compressed sensing", "wavelet",
    ],
    "eess.SY": [
        "control system", "feedback", "stability", "optimal control",
        "robust control", "system identification", "Lyapunov",
    ],
    "eess.IV": [
        "image processing", "video", "compression", "super-resolution",
        "image reconstruction", "denoising",
    ],
    "eess.AS": [
        "audio", "speech recognition", "speaker", "acoustic", "sound",
        "music information retrieval", "spoken language",
    ],
    "q-bio.BM": [
        "biomolecule", "protein structure", "protein folding", "molecular biology",
        "enzyme kinetics", "RNA structure", "AlphaFold",
    ],
    "q-bio.NC": [
        "neuroscience", "neural circuit", "brain", "synapse", "spike",
        "neural coding", "cortex", "connectome",
    ],
    "q-bio.PE": [
        "evolution", "population genetics", "phylogeny", "fitness",
        "selection", "mutation rate", "ecological dynamics",
    ],
    "q-bio.GN": [
        "genomics", "genome", "gene expression", "RNA-seq", "CRISPR",
        "sequencing", "transcriptome", "epigenome",
    ],
    "q-bio.QM": [
        "quantitative biology", "mathematical biology", "systems biology",
        "computational biology", "bioinformatics",
    ],
    "q-bio.CB": [
        "cell motility", "cell signaling", "cytoskeleton", "cell division",
        "chemotaxis", "collective cell migration", "cellular mechanics",
    ],
    "stat.CO": [
        "computational statistics", "MCMC algorithm", "variational inference",
        "approximate Bayesian", "ABC", "expectation maximization",
    ],
    "stat.TH": [
        "statistical theory", "minimax", "risk bound", "concentration inequality",
        "semiparametric", "empirical process", "decision theory",
    ],
    "nlin.CG": [
        "cellular automaton", "lattice gas", "rule", "automata", "discrete simulation",
    ],
    "physics.soc-ph": [
        "social physics", "opinion dynamics", "social network", "agent-based social",
        "econophysics", "complex network",
    ],
}


def suggest_categories(text: str) -> list[str]:
    """Return up to 6 relevant arXiv category codes for the given research description.

    Uses AI when available for field-agnostic suggestions; falls back to keyword
    matching against CATEGORY_HINTS.
    """
    if _ai_available():
        cat_list = "\n".join(
            f"  {code}: {name}" for code, name in ARXIV_CATEGORIES.items()
        )
        prompt = (
            f"A researcher describes their work as:\n\"{text}\"\n\n"
            f"Here is the full list of arXiv categories:\n{cat_list}\n\n"
            "Return ONLY a JSON array of the 4–6 most relevant category codes "
            "(e.g. [\"cond-mat.supr-con\", \"cond-mat.mes-hall\"]). "
            "Pick the best-matching sub-categories — never return a bare top-level code "
            "like 'cond-mat' or 'astro-ph' unless it appears exactly in the list above. "
            "No explanation, no other text."
        )
        raw = _call_ai(prompt)
        if raw:
            try:
                raw = re.sub(r"^```[a-z]*\n?", "", raw.strip())
                raw = re.sub(r"\n?```$", "", raw)
                cats = json.loads(raw)
                # Keep only codes that actually exist in our catalogue
                valid = [c for c in cats if c in ARXIV_CATEGORIES]
                if valid:
                    return valid[:6]
            except Exception:
                pass  # fall through to regex fallback

    # Regex fallback — keyword overlap
    text_lower = text.lower()
    scores = {}
    for cat, hints in CATEGORY_HINTS.items():
        if cat not in ARXIV_CATEGORIES:
            continue
        score = sum(1 for h in hints if h.lower() in text_lower)
        if score >= 2:
            scores[cat] = score
    return sorted(scores, key=scores.get, reverse=True)[:6]


def _call_ai(prompt: str, max_tokens: int = 512) -> str | None:
    """Call Gemini (preferred, free tier) or Claude. Returns text or None on failure."""
    # Try Gemini first — free tier available via Google AI Studio
    gemini_key = _get_gemini_key()
    if gemini_key and _GEMINI_AVAILABLE:
        try:
            _client = _genai_lib.Client(api_key=gemini_key)
            response = _client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
            )
            return response.text.strip()
        except Exception:
            pass

    # Fall back to Anthropic
    anthropic_key = _get_anthropic_key()
    if anthropic_key and _ANTHROPIC_AVAILABLE:
        try:
            client = _anthropic_lib.Anthropic(api_key=anthropic_key)
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text.strip()
        except Exception:
            pass

    return None


@st.cache_data(show_spinner=False)
def _test_ai_key(gemini_key: str, anthropic_key: str) -> tuple[bool, str, str]:
    """
    Validate that at least one AI key actually works.

    Cached by key values so it only runs once per unique key combination.

    Returns (ok, provider_name, error_message).
    """
    if gemini_key and _GEMINI_AVAILABLE:
        try:
            _client = _genai_lib.Client(api_key=gemini_key)
            _client.models.generate_content(
                model="gemini-2.0-flash",
                contents="Hi",
                config=_genai_types.GenerateContentConfig(max_output_tokens=1),
            )
            return True, "Gemini", ""
        except Exception as e:
            gemini_err = str(e)
    else:
        gemini_err = ""

    if anthropic_key and _ANTHROPIC_AVAILABLE:
        try:
            client = _anthropic_lib.Anthropic(api_key=anthropic_key)
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1,
                messages=[{"role": "user", "content": "Hi"}],
            )
            return True, "Anthropic", ""
        except Exception as e:
            anthropic_err = str(e)
    else:
        anthropic_err = ""

    errors = []
    if gemini_err:
        errors.append(f"Gemini: {gemini_err}")
    if anthropic_err:
        errors.append(f"Anthropic: {anthropic_err}")
    return False, "", "  |  ".join(errors) if errors else "No valid key entered."


def draft_research_description(keywords: dict[str, int]) -> str:
    """Generate a first-person research description from keywords using AI."""
    top_keywords = [k for k, _ in sorted(keywords.items(), key=lambda x: -x[1])[:10]]
    prompt = (
        f"A researcher has these keywords extracted from their publications:\n"
        f"{', '.join(top_keywords)}\n\n"
        "Write a 3-4 sentence research description in first person (starting with 'I') "
        "that this researcher could use to describe their work to a colleague. "
        "Be specific and technical. Return only the description, no other text."
    )
    result = _call_ai(prompt, max_tokens=200)
    return result if result else f"My research focuses on {', '.join(top_keywords[:5])}."


def _summarise_research(titles: list[str]) -> str:
    """Generate a first-person research summary from publication titles using AI."""
    sample = titles[:30]  # Cap to avoid token limits
    titles_block = "\n".join(f"- {t}" for t in sample)
    prompt = (
        "Here are publication titles from a researcher's ORCID profile:\n"
        f"{titles_block}\n\n"
        "Write a 3-4 sentence research description in first person (starting with 'I') "
        "that captures what this researcher works on. Be specific about methods, objects, "
        "or phenomena — avoid generic filler. Return only the description, no other text."
    )
    result = _call_ai(prompt, max_tokens=200)
    return result or ""


def _get_gemini_key() -> str | None:
    """Return Gemini API key: session state → secrets → env → None."""
    user_key = st.session_state.get("user_gemini_key", "").strip()
    if user_key:
        return user_key
    try:
        key = st.secrets.get("GEMINI_API_KEY")
        if key:
            return key
    except Exception:
        pass
    return os.environ.get("GEMINI_API_KEY")


def _get_anthropic_key() -> str | None:
    """Return Anthropic API key: session state → secrets → env → None."""
    user_key = st.session_state.get("user_anthropic_key", "").strip()
    if user_key:
        return user_key
    try:
        key = st.secrets.get("ANTHROPIC_API_KEY")
        if key:
            return key
    except Exception:
        pass
    return os.environ.get("ANTHROPIC_API_KEY")


def _ai_available() -> bool:
    """True if any AI key is configured."""
    return bool((_get_gemini_key() and _GEMINI_AVAILABLE) or
                (_get_anthropic_key() and _ANTHROPIC_AVAILABLE))


def _keyword_regex_fallback(text: str) -> dict[str, int]:
    """Extract keywords from research description using pattern matching (no API needed)."""
    words = text.split()
    candidates: dict[str, int] = {}
    stopwords = {
        "i", "my", "me", "we", "our", "the", "a", "an", "and", "or", "but",
        "in", "on", "at", "to", "for", "of", "with", "by", "from", "as",
        "is", "was", "are", "were", "been", "be", "have", "has", "had",
        "do", "does", "did", "will", "would", "could", "should", "that",
        "which", "who", "this", "these", "it", "its", "their", "also",
        "using", "such", "both", "between", "about", "into", "through",
        "particularly", "specifically", "especially", "including", "focus",
        "work", "study", "research", "currently", "mainly", "primarily",
    }
    clean_words = [re.sub(r"[.,;:!?()\"']", "", w) for w in words if w]

    for w in clean_words:
        if not w or len(w) < 3:
            continue
        if w.isupper() and len(w) >= 2 and w.isalpha():
            candidates[w] = 8
        elif w[0].isupper() and not w.isupper() and len(w) > 3:
            candidates[w.lower()] = 5

    for i in range(len(clean_words) - 1):
        w1, w2 = clean_words[i].lower(), clean_words[i + 1].lower()
        if w1 not in stopwords and w2 not in stopwords and len(w1) > 2 and len(w2) > 2:
            bigram = f"{w1} {w2}"
            if bigram not in candidates:
                candidates[bigram] = 7

    for i in range(len(clean_words) - 2):
        w1, w2, w3 = clean_words[i].lower(), clean_words[i + 1].lower(), clean_words[i + 2].lower()
        if all(w not in stopwords and len(w) > 2 for w in (w1, w2, w3)):
            trigram = f"{w1} {w2} {w3}"
            if len(trigram) > 10:
                candidates[trigram] = 9

    generic = {"et al", "ground based", "non linear"}
    return {k: v for k, v in sorted(candidates.items(), key=lambda x: -x[1])[:15]
            if k.lower() not in generic}


def suggest_keywords_from_context(text: str, orcid_keywords: dict | None = None) -> dict[str, int]:
    """Score research keywords by relevance using Claude if available, regex otherwise.

    When orcid_keywords is provided, Claude re-scores those publication-derived keywords
    against the research description so that frequency in titles doesn't dominate.
    """
    api_key = _get_anthropic_key()

    if not _ai_available():
        return _keyword_regex_fallback(text)

    # Build candidate list: ORCID keywords + regex-derived keywords from description
    regex_kws = _keyword_regex_fallback(text)
    all_candidates = dict(regex_kws)
    if orcid_keywords:
        all_candidates.update(orcid_keywords)

    candidate_list = "\n".join(f"- {kw}" for kw in all_candidates)

    prompt = (
        f"A researcher describes their work as:\n\"{text}\"\n\n"
        f"These are candidate keywords (some from publication titles, some from the description):\n"
        f"{candidate_list}\n\n"
        "Score each keyword's relevance to this researcher's specific field on a scale of 1–10. "
        "Prefer specific technical terms over generic words. Generic words that happen to appear "
        "in paper titles (like 'water', 'worlds', 'population') should score low unless they are "
        "genuinely central to this specific research. Return ONLY a JSON object mapping each "
        "keyword to its integer score. No other text."
    )

    raw = _call_ai(prompt)
    if not raw:
        return _keyword_regex_fallback(text)
    try:
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        scored: dict[str, int] = json.loads(raw)
        return dict(sorted(
            {k: max(1, min(10, int(v))) for k, v in scored.items()}.items(),
            key=lambda x: -x[1],
        )[:15])
    except Exception:
        return _keyword_regex_fallback(text)


# ─────────────────────────────────────────────────────────────
#  Profile import helpers
# ─────────────────────────────────────────────────────────────

def _apply_orcid_keywords(keywords: dict, orcid_url: str = "") -> None:
    """Merge ORCID keywords into session state and auto-draft description."""
    if keywords:
        merged = dict(st.session_state.keywords)
        merged.update(keywords)
        st.session_state.keywords = merged
        if not st.session_state.research_description:
            api_key = _get_anthropic_key()
            if api_key and _ANTHROPIC_AVAILABLE:
                st.session_state.research_description = draft_research_description(merged)
    st.session_state.pure_scanned = True
    if orcid_url:
        st.session_state.pure_confirmed_url = orcid_url


def _apply_pure_keywords(keywords: dict | None, coauthors: list | None) -> None:
    """Merge Pure-scraped keywords and co-authors into session state."""
    if keywords:
        merged = dict(st.session_state.keywords)
        merged.update(keywords)
        st.session_state.keywords = merged
    if coauthors:
        for name in coauthors[:15]:
            parts = name.split()
            if len(parts) >= 2:
                match_pattern = f"{parts[-1]}, {parts[0][0]}"
                if not any(c["name"] == name for c in st.session_state.colleagues_people):
                    st.session_state.colleagues_people.append({"name": name, "match": [match_pattern]})
    st.session_state.pure_scanned = True


def _import_profile(result: dict) -> None:
    """Full import from a single ORCID search result: fill profile + extract keywords."""
    # Pre-fill profile fields
    st.session_state.profile_name = result["name"]
    if result.get("department"):
        st.session_state.profile_institution = result["department"]
    st.session_state.pure_confirmed_url = result["url"]

    # Extract keywords from publications
    orcid_id = result["url"].rstrip("/").split("/")[-1]
    keywords, _, _coauthors, error = fetch_orcid_works(orcid_id)
    if not error and keywords:
        _apply_orcid_keywords(keywords, orcid_url=result["url"])
    else:
        st.session_state.pure_scanned = True
        st.session_state.pure_confirmed_url = result["url"]

    st.rerun()


# ─────────────────────────────────────────────────────────────
#  Server-key mode + rate limiting
# ─────────────────────────────────────────────────────────────

def _has_server_key() -> bool:
    """True if a Gemini key is baked into Streamlit secrets (server-side)."""
    try:
        return bool(st.secrets.get("GEMINI_API_KEY"))
    except Exception:
        return False


def _server_smtp() -> tuple[str, str]:
    """Return (smtp_user, smtp_password) from secrets, or ('', '')."""
    try:
        return (
            st.secrets.get("SMTP_USER", ""),
            st.secrets.get("SMTP_PASSWORD", ""),
        )
    except Exception:
        return "", ""

# Module-level dict: email (lowercased) → unix timestamp of last use.
# Persists for the lifetime of the Streamlit process (resets on redeploy).
_DAILY_USAGE: dict[str, float] = {}
_RATE_LIMIT_SECONDS = 86400  # 24 hours


def _is_rate_limited(email: str) -> bool:
    last = _DAILY_USAGE.get(email.lower().strip(), 0.0)
    return (time.time() - last) < _RATE_LIMIT_SECONDS


def _record_usage(email: str) -> None:
    _DAILY_USAGE[email.lower().strip()] = time.time()


# ─────────────────────────────────────────────────────────────
#  Session state defaults
# ─────────────────────────────────────────────────────────────

if "keywords" not in st.session_state:
    st.session_state.keywords = {}
if "colleagues_people" not in st.session_state:
    st.session_state.colleagues_people = []
if "colleagues_institutions" not in st.session_state:
    st.session_state.colleagues_institutions = []
if "research_authors" not in st.session_state:
    st.session_state.research_authors = []
if "pure_scanned" not in st.session_state:
    st.session_state.pure_scanned = False
if "self_match" not in st.session_state:
    st.session_state.self_match = []
if "ai_suggested_cats" not in st.session_state:
    st.session_state.ai_suggested_cats = []
if "ai_suggested_kws" not in st.session_state:
    st.session_state.ai_suggested_kws = {}
# Profile prefill from ORCID scan
if "profile_name" not in st.session_state:
    st.session_state.profile_name = ""
if "profile_institution" not in st.session_state:
    st.session_state.profile_institution = ""
if "profile_department" not in st.session_state:
    st.session_state.profile_department = ""
# Research description (editable, can be auto-drafted from publications)
if "research_description" not in st.session_state:
    st.session_state.research_description = ""


# ─────────────────────────────────────────────────────────────
#  Welcome
# ─────────────────────────────────────────────────────────────

st.markdown("# 🔭 arXiv Digest Setup")
st.markdown("""
Set up your personal arXiv digest in 5 minutes. This wizard generates a `config.yaml`
that you drop into your GitHub fork — then you'll get curated papers delivered to your inbox.
""")

st.markdown(f"""
<div style="font-family: 'DM Mono', monospace; font-size: 10px; letter-spacing: 0.1em;
     text-transform: uppercase; color: {WARM_GREY}; margin-top: -8px; margin-bottom: 24px;">
     Built by <a href="https://silkedainese.github.io" style="color: {PINE};">Silke S. Dainese</a>
</div>
""", unsafe_allow_html=True)

# ── AI setup — server key (no user key needed) or bring-your-own ──
if _has_server_key():
    # Server has a key — just ask for email for rate limiting
    st.markdown("## Get started")
    st.markdown("AI is included — no API key needed. Enter your email to begin.")
    user_email = st.text_input(
        "Your email",
        placeholder="you@university.edu",
        key="user_email_rl",
    )
    if not user_email.strip():
        st.stop()
    if _is_rate_limited(user_email):
        st.warning(
            "You've already generated a config today. "
            "Come back tomorrow, or [get your own free Gemini key](https://aistudio.google.com/app/apikey) to run without limits."
        )
        st.stop()
    _record_usage(user_email)
    st.success("Ready — AI powered by Gemini.")
else:
    # No server key — user must bring their own
    st.markdown("## Choose your AI")
    st.markdown(
        "AI is used throughout — for finding your profile, suggesting keywords, and scoring papers in your daily digest. "
        "Your key is only used during this session and never stored."
    )
    col_g, col_a = st.columns(2)
    with col_g:
        st.markdown("**Gemini** — free tier, no credit card")
        st.text_input(
            "Gemini API key",
            type="password",
            placeholder="AIza...",
            key="user_gemini_key",
            label_visibility="collapsed",
            help="Get a free key at aistudio.google.com",
        )
        st.caption("[Get a free key →](https://aistudio.google.com/app/apikey)")
    with col_a:
        st.markdown("**Anthropic** — Claude")
        st.text_input(
            "Anthropic API key",
            type="password",
            placeholder="sk-ant-...",
            key="user_anthropic_key",
            label_visibility="collapsed",
            help="Get a key at console.anthropic.com",
        )
        st.caption("[Get a key →](https://console.anthropic.com/settings/keys)")

    if _ai_available():
        with st.spinner("Checking key..."):
            _key_ok, _provider, _key_err = _test_ai_key(
                _get_gemini_key() or "", _get_anthropic_key() or ""
            )
        if _key_ok:
            st.success(f"AI ready — using {_provider}.")
        else:
            st.error(f"Key didn't work: {_key_err}")
            st.stop()
    else:
        st.warning("Enter an API key above to continue. AI is required for profile search and paper scoring.")
        st.stop()

ai_assist = True  # AI is always on when we reach this point

st.divider()


# ─────────────────────────────────────────────────────────────
#  Section 1: Profile Scan (optional)
# ─────────────────────────────────────────────────────────────

st.markdown("## 1. Your ORCID")
st.markdown("Enter your ORCID ID — we'll pull your profile and publications automatically.")

if "pure_confirmed_url" not in st.session_state:
    st.session_state.pure_confirmed_url = ""
if "orcid_preview" not in st.session_state:
    st.session_state.orcid_preview = None  # dict with name/institution/orcid_url/keywords when pending

def _commit_preview() -> None:
    """Write the staged orcid_preview into session state and mark as scanned."""
    p = st.session_state.orcid_preview
    if not p:
        return
    st.session_state.profile_name = p["name"]
    st.session_state.profile_institution = p["institution"]
    st.session_state.pure_confirmed_url = p["orcid_url"]

    if p["keywords"]:
        merged = dict(st.session_state.keywords)
        merged.update(p["keywords"])
        st.session_state.keywords = merged

    # Research description: prefer the AI summary from titles; fall back to keywords
    if p.get("research_summary") and not st.session_state.research_description:
        st.session_state.research_description = p["research_summary"]
    elif p["keywords"] and not st.session_state.research_description:
        st.session_state.research_description = draft_research_description(p["keywords"])

    # Add confirmed AU colleagues
    for name in p.get("selected_colleagues", []):
        parts = name.split()
        if len(parts) >= 2:
            match_pattern = f"{parts[-1]}, {parts[0][0]}"
        else:
            match_pattern = name
        if not any(c["name"] == name for c in st.session_state.colleagues_people):
            st.session_state.colleagues_people.append({"name": name, "match": [match_pattern]})

    st.session_state.pure_scanned = True
    st.session_state.orcid_preview = None


# ── Already confirmed — show summary and allow reset ──
if st.session_state.pure_scanned:
    st.success(f"✓ Profile loaded from {st.session_state.pure_confirmed_url}")
    if st.button("↺ Use a different ORCID", type="secondary"):
        st.session_state.pure_scanned = False
        st.session_state.pure_confirmed_url = ""
        st.session_state.orcid_preview = None
        st.rerun()

else:
    # ── ORCID input ──
    col_input, col_btn = st.columns([5, 1])
    with col_input:
        orcid_input = st.text_input(
            "ORCID",
            placeholder="0000-0001-2345-6789  or  https://orcid.org/0000-0001-2345-6789",
            key="orcid_input_field",
            label_visibility="collapsed",
        )
    with col_btn:
        fetch_clicked = st.button("🔍 Fetch", type="primary", use_container_width=True)

    if orcid_input and fetch_clicked:
        inp = orcid_input.strip().rstrip("/")
        # Accept bare ID or full URL
        if inp.startswith("https://orcid.org/"):
            orcid_id = inp.split("/")[-1]
            orcid_url = inp
        elif _ORCID_ID_RE.match(inp):
            orcid_id = inp
            orcid_url = f"https://orcid.org/{inp}"
        else:
            st.error("That doesn't look like an ORCID. Expected format: 0000-0001-2345-6789")
            orcid_id = ""
            orcid_url = ""

        if orcid_id:
            with st.spinner("Fetching profile and publications from ORCID..."):
                full_name, institution, person_error = fetch_orcid_person(orcid_id)
                keywords, titles, coauthor_map, works_error = fetch_orcid_works(orcid_id)

            if person_error:
                st.error(f"Could not fetch profile: {person_error}")
            else:
                # Find AU colleagues in the background (parallel ORCID checks)
                au_colleagues: list[str] = []
                if coauthor_map:
                    with st.spinner("Checking co-authors for Aarhus University affiliation..."):
                        au_colleagues = find_au_colleagues(
                            coauthor_map,
                            institution=institution or "Aarhus University",
                        )

                # Build research summary from titles using AI
                research_summary = ""
                if titles and ai_assist:
                    with st.spinner("Summarising your research..."):
                        research_summary = _summarise_research(titles)

                st.session_state.orcid_preview = {
                    "name": full_name,
                    "institution": institution or "Aarhus University",
                    "orcid_url": orcid_url,
                    "keywords": keywords or {},
                    "titles": titles or [],
                    "au_colleagues": au_colleagues,
                    "all_coauthors": sorted(coauthor_map.values()) if coauthor_map else [],
                    "research_summary": research_summary,
                    # Track which colleagues the user wants to import
                    "selected_colleagues": list(au_colleagues),
                }
                if works_error:
                    st.warning("Profile found but no publications on ORCID — keywords and colleagues will be empty.")

    # ── Review card: show what was found, let user correct ──
    if st.session_state.orcid_preview:
        p = st.session_state.orcid_preview
        st.markdown("**Review what we found — correct anything before importing:**")

        p["name"] = st.text_input("Name", value=p["name"], key="preview_name")
        p["institution"] = st.text_input("Institution", value=p["institution"], key="preview_institution")
        st.caption(f"ORCID: {p['orcid_url']}")

        if p.get("research_summary"):
            st.markdown("**Research summary** — edit freely, this is yours:")
            p["research_summary"] = st.text_area(
                "Research summary",
                value=p["research_summary"],
                height=110,
                key="preview_summary",
                label_visibility="collapsed",
            )

        if p["keywords"]:
            st.markdown("**Keywords from your publications:**")
            kw_display = "  ·  ".join(
                k for k, _ in sorted(p["keywords"].items(), key=lambda x: -x[1])[:12]
            )
            st.caption(kw_display + "  _(you can adjust these below)_")
        else:
            st.caption("No keywords found — you can add them manually below.")

        # ── Colleagues ──
        st.markdown("**Colleagues to track** — papers by these people always appear in your digest:")
        # Preserve manually added colleagues across re-renders
        p.setdefault("selected_colleagues", [])

        if p.get("au_colleagues"):
            st.caption(f"Found {len(p['au_colleagues'])} co-authors at {p['institution']} — uncheck any to exclude, or add more below.")
            manual = [c for c in p["selected_colleagues"] if c not in p["au_colleagues"]]
            selected = list(manual)
            for colleague in p["au_colleagues"]:
                checked = st.checkbox(colleague, value=True, key=f"colleague_{colleague}")
                if checked:
                    selected.append(colleague)
            p["selected_colleagues"] = selected
        else:
            if p.get("titles"):
                st.caption(f"No co-authors with confirmed {p['institution']} affiliation found automatically.")

        # Show manually added colleagues (not from auto-detection)
        manual_added = [c for c in p["selected_colleagues"] if c not in p.get("au_colleagues", [])]
        if manual_added:
            st.caption("Manually added colleagues:")
            to_remove = []
            for mc in manual_added:
                mc_col, rm_col = st.columns([6, 1])
                with mc_col:
                    st.markdown(f"· {mc}")
                with rm_col:
                    if st.button("✕", key=f"rm_manual_{mc}"):
                        to_remove.append(mc)
            for mc in to_remove:
                p["selected_colleagues"].remove(mc)
            if to_remove:
                st.rerun()

        # Manual add by ORCID — for colleagues not found automatically
        st.caption("Add a colleague by their ORCID:")
        extra_col, extra_btn = st.columns([4, 1])
        with extra_col:
            extra_orcid = st.text_input(
                "Colleague ORCID",
                placeholder="0000-0001-2345-6789  or  https://orcid.org/...",
                key="preview_extra_orcid",
                label_visibility="collapsed",
            )
        with extra_btn:
            add_clicked = st.button("Look up", key="preview_add_colleague")

        if add_clicked and extra_orcid.strip():
            inp = extra_orcid.strip().rstrip("/")
            if inp.startswith("https://orcid.org/"):
                lookup_id = inp.split("/")[-1]
            elif _ORCID_ID_RE.match(inp):
                lookup_id = inp
            else:
                st.error("Enter a valid ORCID (e.g. 0000-0001-2345-6789).")
                lookup_id = ""

            if lookup_id:
                with st.spinner("Looking up colleague..."):
                    found_name, found_inst, found_err = fetch_orcid_person(lookup_id)
                if found_err:
                    st.error(f"Could not fetch: {found_err}")
                elif found_name and found_name not in p["selected_colleagues"]:
                    p["selected_colleagues"].append(found_name)
                    st.success(f"Added {found_name} ({found_inst or 'no institution on ORCID'})")
                    st.rerun()

        # Pick from all co-authors on previous papers (already fetched from ORCID)
        all_coauthors = p.get("all_coauthors", [])
        pickable = [n for n in all_coauthors if n not in p["selected_colleagues"]]
        if pickable:
            with st.expander(f"Or pick from your {len(all_coauthors)} ORCID co-authors"):
                pick_filter = st.text_input("Filter by name", key="coauthor_pick_filter", placeholder="type to filter…")
                filtered = [n for n in pickable if pick_filter.lower() in n.lower()] if pick_filter else pickable
                for name in filtered[:30]:
                    if st.button(f"+ {name}", key=f"pick_coauthor_{name}"):
                        p["selected_colleagues"].append(name)
                        st.rerun()
                if len(filtered) > 30:
                    st.caption(f"Showing 30 of {len(filtered)} — type more to narrow.")

        if st.button("✓ Looks good — import", type="primary"):
            _commit_preview()
            st.rerun()

st.divider()


# ─────────────────────────────────────────────────────────────
#  Section 2: Your Profile
# ─────────────────────────────────────────────────────────────

st.markdown("## 2. Your Profile")

col1, col2 = st.columns(2)
with col1:
    researcher_name = st.text_input("Your name", placeholder="Jane Smith", key="profile_name")
    institution = st.text_input("Institution (optional)", placeholder="Aarhus University", key="profile_institution")
with col2:
    digest_name = st.text_input("Digest name", value="arXiv Digest", help="Appears in the email subject line")
    department = st.text_input("Department (optional)", placeholder="Dept. of Physics & Astronomy", key="profile_department")

tagline = st.text_input("Footer tagline (optional)", placeholder="Ad astra per aspera", help="A quote or motto for the email footer")

# ── Self-match (your own name on arXiv) ──
st.markdown("**Your name on arXiv** — if you publish a paper, you'll get a special celebration in your digest!")
col1, col2 = st.columns([3, 1])
with col1:
    new_self = st.text_input("Author match pattern", placeholder="Smith, J", key="self_match_input", label_visibility="collapsed",
                              help="How your name appears in arXiv author lists (e.g. 'Smith, J' or 'Jane Smith')")
with col2:
    if st.button("Add", key="add_self_match", use_container_width=True):
        if new_self.strip() and new_self.strip() not in st.session_state.self_match:
            st.session_state.self_match.append(new_self.strip())
            st.rerun()

if st.session_state.self_match:
    to_remove = []
    for pattern in st.session_state.self_match:
        col1, col2 = st.columns([4, 1])
        with col1:
            st.markdown(f"- `{pattern}`")
        with col2:
            if st.button("✕", key=f"rm_self_{pattern}"):
                to_remove.append(pattern)
    for p in to_remove:
        st.session_state.self_match.remove(p)
        st.rerun()

st.divider()


# ─────────────────────────────────────────────────────────────
#  Section 3: Research Description
# ─────────────────────────────────────────────────────────────

st.markdown("## 3. Your Research Description")

if ai_assist:
    if st.session_state.research_description:
        st.markdown(
            "Auto-drafted from your publications — edit freely. "
            "Then hit the button below to suggest categories and score your keywords."
        )
    else:
        st.markdown(
            "Describe your research in 3-5 sentences, like you'd tell a colleague. "
            "We'll use this to **suggest arXiv categories and score keywords** for you."
        )
else:
    st.markdown("Describe your research in 3-5 sentences. This is what the AI uses to score papers.")

research_context = st.text_area(
    "Research context",
    height=120,
    placeholder="I study exoplanet atmospheres using transmission spectroscopy with JWST and ground-based instruments. I focus on hot Jupiters and sub-Neptunes, particularly their atmospheric composition and cloud properties.",
    label_visibility="collapsed",
    key="research_description",
)

# ── AI suggestions: auto-run if description was auto-drafted, else show button ──
if ai_assist and research_context and len(research_context) > 30:
    _has_orcid_kws = bool(st.session_state.keywords)
    _api_available = _ai_available()
    _cats_already_suggested = bool(st.session_state.ai_suggested_cats)

    # Auto-trigger when profile was imported and description was drafted automatically
    _auto_trigger = st.session_state.pure_scanned and not _cats_already_suggested
    if _auto_trigger:
        with st.spinner("Suggesting categories and scoring keywords..."):
            st.session_state.ai_suggested_cats = suggest_categories(research_context)
            st.session_state.ai_suggested_kws = suggest_keywords_from_context(
                research_context,
                orcid_keywords=st.session_state.keywords if _has_orcid_kws else None,
            )
            if _api_available and _has_orcid_kws:
                st.session_state.keywords = {
                    k: v for k, v in st.session_state.ai_suggested_kws.items()
                    if k in st.session_state.keywords
                }
        st.rerun()
    else:
        _btn_label = (
            "🤖 Re-score categories & keywords"
            if _cats_already_suggested
            else ("🤖 Suggest categories & score keywords" if _api_available
                  else "🤖 Suggest categories & keywords")
        )
        if st.button(_btn_label, type="secondary" if _cats_already_suggested else "primary"):
            st.session_state.ai_suggested_cats = suggest_categories(research_context)
            st.session_state.ai_suggested_kws = suggest_keywords_from_context(
                research_context,
                orcid_keywords=st.session_state.keywords if _has_orcid_kws else None,
            )
            if _api_available and _has_orcid_kws:
                st.session_state.keywords = {
                    k: v for k, v in st.session_state.ai_suggested_kws.items()
                    if k in st.session_state.keywords
                }

    if st.session_state.ai_suggested_cats:
        st.success(f"Suggested {len(st.session_state.ai_suggested_cats)} categories and {len(st.session_state.ai_suggested_kws)} keywords — review them below.")

st.divider()


# ─────────────────────────────────────────────────────────────
#  Section 4: arXiv Categories
# ─────────────────────────────────────────────────────────────

st.markdown("## 4. arXiv Categories")

# Build set of AI-suggested categories for pre-selection
ai_suggested_set = set(st.session_state.ai_suggested_cats) if ai_assist else set()

# Track which categories the user has selected across all groups
if "selected_categories" not in st.session_state:
    st.session_state.selected_categories = set(ai_suggested_set)

# If AI suggestions just arrived, merge them into the selection
if ai_suggested_set and not st.session_state.selected_categories.issuperset(ai_suggested_set):
    st.session_state.selected_categories.update(ai_suggested_set)

if ai_assist and ai_suggested_set:
    st.success(
        f"AI suggested {len(ai_suggested_set)} categories based on your research description. "
        f"They are pre-selected below — review and adjust as needed."
    )

st.markdown(
    "Pick the arXiv groups you want to monitor, then choose sub-categories within each group. "
    "Each group header shows a hint for when to include it."
)

# ── Group-level hierarchical picker ──
to_add = set()
to_remove = set()

for group_name, group_cats in ARXIV_GROUPS.items():
    selected_in_group = [c for c in group_cats if c in st.session_state.selected_categories]
    n_selected = len(selected_in_group)
    n_total = len(group_cats)
    hint = ARXIV_GROUP_HINTS.get(group_name, "")

    count_label = f"{n_selected}/{n_total} selected" if n_selected > 0 else ""
    with st.expander(
        f"**{group_name}**" + (f" — {count_label}" if count_label else ""),
        expanded=(n_selected > 0),
    ):
        if hint:
            st.caption(f"Include if: {hint}")

        col_all, col_none, col_spacer = st.columns([1, 1, 4])
        with col_all:
            if st.button("Select all", key=f"grp_all_{group_name}", use_container_width=True):
                to_add.update(group_cats)
        with col_none:
            if st.button("Clear", key=f"grp_none_{group_name}", use_container_width=True):
                to_remove.update(group_cats)

        for cat_id in group_cats:
            label = ARXIV_CATEGORIES.get(cat_id, cat_id)
            is_selected = cat_id in st.session_state.selected_categories
            # ✦ marks AI-suggested categories (Unicode, not an emoji)
            display_label = f"{label} \u2726" if cat_id in ai_suggested_set else label
            checked = st.checkbox(
                display_label,
                value=is_selected,
                key=f"cat_{cat_id}",
                help=f"`{cat_id}`" + (" — AI suggested" if cat_id in ai_suggested_set else ""),
            )
            if checked and not is_selected:
                to_add.add(cat_id)
            elif not checked and is_selected:
                to_remove.add(cat_id)

# Apply batch updates after the loop (avoids mid-loop state mutations)
if to_add or to_remove:
    st.session_state.selected_categories = (
        st.session_state.selected_categories | to_add
    ) - to_remove
    st.rerun()

# Final categories list — flat list of strings for config output
categories = sorted(st.session_state.selected_categories)

if categories:
    st.markdown(
        f"**{len(categories)} categories selected:** "
        + ", ".join(f"`{c}`" for c in categories)
    )
else:
    st.info("No categories selected yet. Expand a group above to choose.")

st.divider()


# ─────────────────────────────────────────────────────────────
#  Section 5: Keywords
# ─────────────────────────────────────────────────────────────

st.markdown("## 5. Keywords")
st.markdown("Papers matching these keywords get pre-filtered before AI scoring. Higher weight = more important.")

# If AI suggested keywords, offer to add them
if ai_assist and st.session_state.ai_suggested_kws:
    new_suggestions = {k: v for k, v in st.session_state.ai_suggested_kws.items()
                       if k not in st.session_state.keywords}
    if new_suggestions:
        st.markdown("**Suggested keywords** — click to add:")
        cols = st.columns(3)
        to_add = {}
        for i, (kw, weight) in enumerate(new_suggestions.items()):
            with cols[i % 3]:
                if st.button(f"+ {kw} ({weight})", key=f"add_sug_{kw}", use_container_width=True):
                    to_add[kw] = weight
        if to_add:
            st.session_state.keywords.update(to_add)
            st.rerun()

        if st.button("Add all suggested keywords"):
            st.session_state.keywords.update(new_suggestions)
            st.rerun()

# Manual keyword entry
st.markdown("**Add keyword manually:**")
col1, col2, col3 = st.columns([3, 2, 1])
with col1:
    new_kw = st.text_input("Keyword", placeholder="transmission spectroscopy", label_visibility="collapsed", key="new_kw_input")
with col2:
    new_weight = st.slider("Weight", 1, 10, 7, label_visibility="collapsed", key="new_kw_weight")
    st.caption(f"_{_weight_label(new_weight)}_")
with col3:
    if st.button("Add", use_container_width=True, key="add_kw_btn"):
        if new_kw.strip():
            st.session_state.keywords[new_kw.strip()] = new_weight
            st.rerun()

# Display existing keywords with editable weight sliders
if st.session_state.keywords:
    st.markdown("**Your keywords:**")
    to_remove = []
    for kw, weight in sorted(st.session_state.keywords.items(), key=lambda x: -x[1]):
        col1, col2, col3 = st.columns([3, 2, 1])
        with col1:
            st.markdown(f"`{kw}`")
        with col2:
            new_w = st.slider(
                "weight", 1, 10, weight,
                key=f"kw_slider_{kw}",
                label_visibility="collapsed",
            )
            st.caption(f"_{_weight_label(new_w)}_")
            # Update weight in-place — no rerun needed, slider state persists
            st.session_state.keywords[kw] = new_w
        with col3:
            if st.button("✕", key=f"rm_kw_{kw}", help=f"Remove {kw}"):
                to_remove.append(kw)
    for kw in to_remove:
        del st.session_state.keywords[kw]
        st.rerun()
else:
    st.info("No keywords yet. Add some above, scan your Pure profile, or use AI suggestions.")

st.divider()


# ─────────────────────────────────────────────────────────────
#  Section 6: Research Authors
# ─────────────────────────────────────────────────────────────

st.markdown("## 6. Research Authors")
st.markdown("Papers by these people get a relevance boost. Use partial name strings (e.g. 'Madhusudhan').")

new_author = st.text_input("Add research author", placeholder="Madhusudhan", key="new_ra_input")
if st.button("Add author") and new_author.strip():
    if new_author.strip() not in st.session_state.research_authors:
        st.session_state.research_authors.append(new_author.strip())
        st.rerun()

if st.session_state.research_authors:
    to_remove = []
    for author in st.session_state.research_authors:
        col1, col2 = st.columns([4, 1])
        with col1:
            st.markdown(f"- {author}")
        with col2:
            if st.button("✕", key=f"rm_ra_{author}"):
                to_remove.append(author)
    for a in to_remove:
        st.session_state.research_authors.remove(a)
        st.rerun()

st.divider()


# ─────────────────────────────────────────────────────────────
#  Section 7: Colleagues
# ─────────────────────────────────────────────────────────────

st.markdown("## 7. Colleagues")
st.markdown("Papers by colleagues always appear in a special section, even if off-topic. Great for staying social!")

st.markdown("**People:**")
col1, col2 = st.columns([2, 2])
with col1:
    new_coll_name = st.text_input("Colleague name", placeholder="Jane Smith", key="new_coll_name")
with col2:
    new_coll_match = st.text_input("Match pattern", placeholder="Smith, J", key="new_coll_match", help="How their name appears in arXiv author lists")

if st.button("Add colleague") and new_coll_name.strip() and new_coll_match.strip():
    st.session_state.colleagues_people.append({
        "name": new_coll_name.strip(),
        "match": [new_coll_match.strip()],
    })
    st.rerun()

if st.session_state.colleagues_people:
    to_remove = []
    for i, coll in enumerate(st.session_state.colleagues_people):
        col1, col2, col3 = st.columns([2, 2, 1])
        with col1:
            st.markdown(f"**{coll['name']}**")
        with col2:
            st.markdown(f"match: `{', '.join(coll['match'])}`")
        with col3:
            if st.button("✕", key=f"rm_coll_{i}"):
                to_remove.append(i)
    for idx in sorted(to_remove, reverse=True):
        st.session_state.colleagues_people.pop(idx)
    if to_remove:
        st.rerun()

st.markdown("**Institutions** (match against abstract text):")
new_inst = st.text_input("Add institution", placeholder="Aarhus University", key="new_inst_input")
if st.button("Add institution") and new_inst.strip():
    if new_inst.strip() not in st.session_state.colleagues_institutions:
        st.session_state.colleagues_institutions.append(new_inst.strip())
        st.rerun()

if st.session_state.colleagues_institutions:
    to_remove = []
    for inst in st.session_state.colleagues_institutions:
        col1, col2 = st.columns([4, 1])
        with col1:
            st.markdown(f"- {inst}")
        with col2:
            if st.button("✕", key=f"rm_inst_{inst}"):
                to_remove.append(inst)
    for inst in to_remove:
        st.session_state.colleagues_institutions.remove(inst)
    if to_remove:
        st.rerun()

st.divider()


# ─────────────────────────────────────────────────────────────
#  Section 8: Digest Mode & Schedule
# ─────────────────────────────────────────────────────────────

st.markdown("## 8. Digest Mode & Schedule")

# ── Digest mode ──
st.markdown("**How much do you want to read?**")
digest_mode = st.radio(
    "Digest mode",
    options=["highlights", "in_depth"],
    format_func=lambda x: {
        "highlights": "🎯 Highlights — just the top papers (fewer, higher quality)",
        "in_depth": "📚 In-depth — wider net, more papers to browse",
    }[x],
    horizontal=True,
    label_visibility="collapsed",
)

# Show what the mode means
if digest_mode == "highlights":
    st.caption("Default: up to 6 papers, min score 5/10. Only the most relevant papers make it through.")
else:
    st.caption("Default: up to 15 papers, min score 2/10. Casts a wider net — great for staying broadly informed.")

# ── Advanced overrides ──
mode_defaults = {"highlights": (6, 5), "in_depth": (15, 2)}
default_max, default_min = mode_defaults[digest_mode]
override_max = False
override_min = False

with st.expander("Fine-tune (optional)"):
    col1, col2 = st.columns(2)
    with col1:
        max_papers = st.number_input("Max papers per digest", min_value=1, max_value=30, value=default_max)
    with col2:
        min_score = st.number_input("Min relevance score (1-10)", min_value=1, max_value=10, value=default_min)

    override_max = max_papers != default_max
    override_min = min_score != default_min

st.markdown("---")

# ── Schedule ──
st.markdown("**How often should the digest arrive?**")
schedule_options = {
    "mon_wed_fri": "Mon / Wed / Fri",
    "daily": "Every weekday (Mon–Fri)",
    "weekly": "Once a week (Monday)",
}
schedule = st.radio(
    "Frequency",
    options=list(schedule_options.keys()),
    format_func=lambda x: schedule_options[x],
    horizontal=True,
    label_visibility="collapsed",
)

# ── Days back (auto-set based on schedule, with override) ──
schedule_days_back = {"daily": 2, "mon_wed_fri": 4, "weekly": 8}
days_back = schedule_days_back[schedule]

with st.expander("Override days back"):
    days_back = st.number_input("Days to look back", min_value=1, max_value=14, value=days_back)

st.caption(f"Will look back **{days_back} days** for new papers.")

# ── Send time ──
st.markdown("**What time should it arrive?** (UTC)")
send_hour_utc = st.slider(
    "Send hour (UTC)",
    min_value=0, max_value=23, value=7,
    help="Default is 7 UTC = 9am Danish time (CET). Adjust for your timezone.",
    label_visibility="collapsed",
)

# Show common timezone equivalents
tz_examples = []
if 0 <= send_hour_utc <= 23:
    cet = (send_hour_utc + 1) % 24
    cest = (send_hour_utc + 2) % 24
    est = (send_hour_utc - 5) % 24
    pst = (send_hour_utc - 8) % 24
    tz_examples = [
        f"CET: {cet}:00",
        f"CEST: {cest}:00",
        f"EST: {est}:00",
        f"PST: {pst}:00",
    ]
st.caption(" · ".join(tz_examples))

# ── Generate cron expression ──
CRON_MAP = {
    "daily": f"0 {send_hour_utc} * * 1-5",
    "mon_wed_fri": f"0 {send_hour_utc} * * 1,3,5",
    "weekly": f"0 {send_hour_utc} * * 1",
}
cron_expr = CRON_MAP[schedule]

st.divider()


# ─────────────────────────────────────────────────────────────
#  Section 9: Email Provider
# ─────────────────────────────────────────────────────────────

st.markdown("## 9. Email Provider")
smtp_options = {"Gmail": ("smtp.gmail.com", 587), "Outlook / Office 365": ("smtp.office365.com", 587)}
smtp_choice = st.radio("SMTP provider", options=list(smtp_options.keys()), horizontal=True, label_visibility="collapsed")
smtp_server, smtp_port = smtp_options[smtp_choice]

github_repo = st.text_input("GitHub repo (optional)", placeholder="username/arxiv-digest", help="Enables self-service links in emails")

st.divider()


# ─────────────────────────────────────────────────────────────
#  Section 10: Preview & Download
# ─────────────────────────────────────────────────────────────

st.markdown("## 10. Preview & Download")

# Build config dict
config = {
    "digest_name": digest_name or "arXiv Digest",
    "researcher_name": researcher_name or "Reader",
    "research_context": research_context or "",
    "categories": categories if categories else ["astro-ph.EP", "astro-ph.SR", "astro-ph.GA", "astro-ph.HE", "astro-ph.IM"],
    "keywords": dict(st.session_state.keywords) if st.session_state.keywords else {"example keyword": 5},
    "self_match": list(st.session_state.self_match),
    "research_authors": list(st.session_state.research_authors),
    "colleagues": {
        "people": list(st.session_state.colleagues_people),
        "institutions": list(st.session_state.colleagues_institutions),
    },
    "digest_mode": digest_mode,
    "days_back": days_back,
    "schedule": schedule,
    "send_hour_utc": send_hour_utc,
    "institution": institution or "",
    "department": department or "",
    "tagline": tagline or "",
    "smtp_server": smtp_server,
    "smtp_port": smtp_port,
    "github_repo": github_repo or "",
}

# Only include overrides if user changed them from mode defaults
if override_max:
    config["max_papers"] = max_papers
if override_min:
    config["min_score"] = min_score

config_yaml = yaml.dump(config, default_flow_style=False, sort_keys=False, allow_unicode=True)

tab1, tab2 = st.tabs(["config.yaml", "Workflow cron"])

with tab1:
    st.code(config_yaml, language="yaml")

with tab2:
    st.markdown("If you change the schedule from the default (Mon/Wed/Fri 7am UTC), update this line in `.github/workflows/digest.yml`:")
    st.code(f"    - cron: '{cron_expr}'  # {schedule_options[schedule]} at {send_hour_utc}:00 UTC", language="yaml")
    if schedule != "mon_wed_fri" or send_hour_utc != 7:
        st.warning("Your schedule differs from the default. Remember to update the cron line in your workflow file after forking!")

col1, col2 = st.columns(2)
with col1:
    st.download_button(
        label="📥 Download config.yaml",
        data=config_yaml,
        file_name="config.yaml",
        mime="text/yaml",
        type="primary",
        use_container_width=True,
    )
with col2:
    if st.button("📋 Copy to clipboard", use_container_width=True):
        st.code(config_yaml, language="yaml")
        st.info("Select all text above and copy (Ctrl/Cmd+C)")

st.divider()


# ─────────────────────────────────────────────────────────────
#  Section 11: Next Steps
# ─────────────────────────────────────────────────────────────

st.markdown("## Next Steps")

# Custom schedule note
schedule_note = ""
if schedule != "mon_wed_fri" or send_hour_utc != 7:
    schedule_note = f"""
<div class="brand-card" style="border-left: 4px solid {GOLD};">
<p>⚠️ <strong>Update your schedule</strong></p>
<p style="margin-left: 36px;">
Since you chose <strong>{schedule_options[schedule]} at {send_hour_utc}:00 UTC</strong>, open
<code>.github/workflows/digest.yml</code> in your fork and change the cron line to:<br>
<code>- cron: '{cron_expr}'</code>
</p>
</div>
"""

st.markdown(f"""
<div class="brand-card">
<p><span class="step-number">1</span> <strong>Fork the template repo</strong></p>
<p style="margin-left: 36px;">
Go to <a href="https://github.com/SilkeDainese/arxiv-digest" style="color: {PINE};">github.com/SilkeDainese/arxiv-digest</a>
and click <strong>Fork</strong>.
</p>
</div>

<div class="brand-card">
<p><span class="step-number">2</span> <strong>Upload your config.yaml</strong></p>
<p style="margin-left: 36px;">
In your fork, click <strong>Add file → Upload files</strong> and upload the <code>config.yaml</code>
you just downloaded. It will replace the example config.
</p>
</div>

<div class="brand-card">
<p><span class="step-number">3</span> <strong>Add secrets to your fork</strong></p>
<p style="margin-left: 36px;">
Go to your fork's <strong>Settings → Secrets and variables → Actions</strong> and add the secrets shown below.
</p>
</div>

{schedule_note}
""", unsafe_allow_html=True)

_smtp_user, _smtp_password = _server_smtp()
_server_gemini = _has_server_key()

if _smtp_user and _smtp_password:
    # Pre-filled secrets — user only needs to add RECIPIENT_EMAIL
    st.markdown("**Copy these secrets into your fork** (Settings → Secrets and variables → Actions):")
    _secrets_block = f"RECIPIENT_EMAIL = your-email@example.com  ← change this\nSMTP_USER      = {_smtp_user}\nSMTP_PASSWORD  = {_smtp_password}"
    if not _server_gemini:
        _secrets_block += "\nGEMINI_API_KEY = your-key  ← get free at aistudio.google.com"
    st.code(_secrets_block, language="ini")
    st.caption("Emails will be sent from **" + _smtp_user + "**. Change only the `RECIPIENT_EMAIL` line to your own address.")
else:
    # No server SMTP — user configures their own
    st.markdown("**Add these secrets to your fork** (Settings → Secrets and variables → Actions):")
    st.code(
        "RECIPIENT_EMAIL = your-email@example.com\n"
        "SMTP_USER       = your-gmail@gmail.com\n"
        "SMTP_PASSWORD   = your-app-password  ← not your login password!\n"
        "GEMINI_API_KEY  = AIza...             ← optional, for AI scoring",
        language="ini",
    )
    st.caption(
        "Gmail app password: Google Account → Security → 2-Step Verification → "
        "[App passwords](https://myaccount.google.com/apppasswords)"
    )

st.success(f"That's it! Your digest will run {schedule_options[schedule].lower()} at {send_hour_utc}:00 UTC. 🎉")

st.divider()

# ── Footer ──
st.markdown(f"""
<div style="text-align: center; font-family: 'DM Mono', monospace; font-size: 10px;
     letter-spacing: 0.1em; color: {WARM_GREY}; margin-top: 24px; margin-bottom: 24px;">
     Built by <a href="https://silkedainese.github.io" style="color: {PINE};">Silke S. Dainese</a> ·
     <a href="mailto:dainese@phys.au.dk" style="color: {WARM_GREY};">dainese@phys.au.dk</a> ·
     <a href="https://github.com/SilkeDainese" style="color: {WARM_GREY};">GitHub</a>
</div>
""", unsafe_allow_html=True)

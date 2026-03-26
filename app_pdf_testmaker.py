"""
╔══════════════════════════════════════════════════════════════╗
║          LOCAL PDF → PRACTICE TEST GENERATOR                ║
║          100% offline · No API keys · GPU-accelerated       ║
╠══════════════════════════════════════════════════════════════╣
║  QUICK SETUP (one-time):                                     ║
║                                                              ║
║  1. Install Ollama:                                          ║
║     https://ollama.com/download                              ║
║     Then pull a model:                                       ║
║       ollama pull llama3.2          (4GB, recommended)       ║
║       ollama pull mistral           (4GB, alternative)       ║
║       ollama pull llama3.2:1b       (1GB, fast/light)        ║
║                                                              ║
║  2. Install Python dependencies:                             ║
║     pip install gradio pymupdf ollama                        ║
║                                                              ║
║  3. Start Ollama (if not running as a service):              ║
║     ollama serve                                             ║
║                                                              ║
║  4. Run this app:                                            ║
║     python app.py                                            ║
║     Then open http://localhost:7860 in your browser.         ║
║                                                              ║
║  GPU NOTES:                                                  ║
║  - Ollama auto-detects NVIDIA (CUDA), AMD (ROCm), Apple      ║
║    Silicon (Metal) — nothing extra needed.                   ║
║  - If you prefer llama-cpp-python instead of Ollama,         ║
║    see the ALTERNATIVE BACKEND section below.                ║
╚══════════════════════════════════════════════════════════════╝

ALTERNATIVE BACKEND (llama-cpp-python):
  pip install llama-cpp-python --extra-index-url \
      https://abetlen.github.io/llama-cpp-python/whl/cu121
  Then change USE_OLLAMA = False below and set GGUF_MODEL_PATH.
"""

import os
import re
import json
import time
import threading
import tempfile
from pathlib import Path
from typing import Optional

import gradio as gr

# ─────────────────────────────────────────────────────────────
#  BACKEND CONFIGURATION — edit here if needed
# ─────────────────────────────────────────────────────────────

USE_OLLAMA       = True          # False → use llama-cpp-python
OLLAMA_MODEL     = "llama3.2"    # any model you've pulled via `ollama pull`
OLLAMA_HOST      = "http://localhost:11434"

# llama-cpp-python settings (only used if USE_OLLAMA = False)
GGUF_MODEL_PATH  = "models/llama-3.2-3b-instruct.Q4_K_M.gguf"
LLAMA_N_GPU_LAYERS = -1          # -1 = all layers on GPU (max speed)
LLAMA_CTX        = 8192

# ─────────────────────────────────────────────────────────────
#  PDF TEXT EXTRACTION  (PyMuPDF / fitz — fully local)
# ─────────────────────────────────────────────────────────────

def extract_text_from_pdf(path: str) -> str:
    """Extract all text from a PDF file using PyMuPDF."""
    try:
        import fitz  # PyMuPDF
        doc  = fitz.open(path)
        pages = []
        for page in doc:
            pages.append(page.get_text())
        doc.close()
        return "\n\n".join(pages)
    except ImportError:
        # fallback: pypdf
        try:
            from pypdf import PdfReader
            reader = PdfReader(path)
            return "\n\n".join(
                page.extract_text() or "" for page in reader.pages
            )
        except ImportError:
            raise RuntimeError(
                "No PDF library found. Install one:\n"
                "  pip install pymupdf\n"
                "  -- or --\n"
                "  pip install pypdf"
            )


def clean_text(raw: str) -> str:
    """Basic PDF text cleanup: fix hyphenated line-breaks, collapse whitespace."""
    text = re.sub(r"-\n(\S)", r"\1", raw)          # join hyphenated words
    text = re.sub(r"\n{3,}", "\n\n", text)          # max 2 blank lines
    text = re.sub(r"[ \t]{2,}", " ", text)          # collapse spaces
    return text.strip()


# ─────────────────────────────────────────────────────────────
#  LOCAL LLM BACKEND
# ─────────────────────────────────────────────────────────────

_llama_instance = None   # cached llama-cpp-python instance

def _get_llama():
    """Lazy-load the llama-cpp-python model (GPU-accelerated)."""
    global _llama_instance
    if _llama_instance is None:
        from llama_cpp import Llama
        _llama_instance = Llama(
            model_path    = GGUF_MODEL_PATH,
            n_gpu_layers  = LLAMA_N_GPU_LAYERS,  # -1 = all layers on GPU
            n_ctx         = LLAMA_CTX,
            verbose       = False,
        )
    return _llama_instance


def llm_generate(prompt: str, max_tokens: int = 3000) -> str:
    """
    Send a prompt to the local LLM and return the response text.
    Supports both Ollama and llama-cpp-python.
    GPU acceleration is automatic:
      - Ollama: detects CUDA/Metal/ROCm at startup
      - llama-cpp-python: controlled by n_gpu_layers above
    """
    if USE_OLLAMA:
        try:
            import ollama as ol
            response = ol.chat(
                model   = OLLAMA_MODEL,
                messages= [{"role": "user", "content": prompt}],
                options = {"num_predict": max_tokens},
            )
            return response["message"]["content"]
        except Exception as e:
            raise RuntimeError(f"Ollama error: {e}\n\n"
                "Make sure Ollama is running (`ollama serve`) "
                f"and the model '{OLLAMA_MODEL}' is pulled "
                f"(`ollama pull {OLLAMA_MODEL}`).")
    else:
        llm = _get_llama()
        out = llm(
            prompt,
            max_tokens  = max_tokens,
            temperature = 0.7,
            stop        = ["</s>", "[INST]"],
        )
        return out["choices"][0]["text"]


# ─────────────────────────────────────────────────────────────
#  PRACTICE TEST GENERATION
# ─────────────────────────────────────────────────────────────

GENERATE_PROMPT = """\
You are an expert educator and test designer. Given the source material below, create a {n_questions}-question multiple-choice practice test.

REQUIREMENTS:
- Difficulty: {difficulty}
- Each question must have exactly 4 answer choices labeled A, B, C, D.
- Provide the correct answer letter and a 1-sentence explanation for each question.
- Base questions ONLY on the provided material. Do not invent facts.
- Vary question types: recall, comprehension, application, analysis.

OUTPUT FORMAT — respond ONLY with valid JSON, no markdown fences, no extra text:
{{
  "title": "Practice Test: <topic from material>",
  "questions": [
    {{
      "id": 1,
      "question": "...",
      "choices": {{"A": "...", "B": "...", "C": "...", "D": "..."}},
      "answer": "A",
      "explanation": "..."
    }}
  ]
}}

SOURCE MATERIAL:
{text}
"""

EXPLAIN_PROMPT = """\
A student answered a multiple-choice question incorrectly. Provide a short, encouraging explanation.

Question: {question}
Correct answer: {answer} — {correct_text}
Student chose: {chosen} — {chosen_text}

Write 2-3 sentences explaining why the correct answer is right and why the student's choice is incorrect. Be encouraging and educational.
"""


def build_prompt(text: str, n_questions: int, difficulty: str) -> str:
    # Trim to ~12,000 chars to stay within typical context windows
    max_chars = 12000
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[... content truncated for length ...]"
    return GENERATE_PROMPT.format(
        n_questions = n_questions,
        difficulty  = difficulty,
        text        = text,
    )


def parse_test_json(raw: str) -> dict:
    """Parse LLM output, stripping accidental markdown fences and extracting JSON."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        raw = raw.rsplit("```", 1)[0]
    raw = raw.strip()
    
    # Extract JSON block using regex (handles embedded JSON in text)
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        return json.loads(match.group())
    else:
        raise ValueError("No valid JSON found in LLM response")


# ─────────────────────────────────────────────────────────────
#  GRADIO UI  — Custom CSS
# ─────────────────────────────────────────────────────────────

CSS = """
/* ── Fonts ── */
@import url('https://fonts.googleapis.com/css2?family=DM+Serif+Display:ital@0;1&family=DM+Sans:wght@300;400;500;600&display=swap');

:root {
    --bg:        #0c0f1a;
    --bg2:       #141824;
    --bg3:       #1c2236;
    --card:      #1a2032;
    --border:    #2a3352;
    --border-hi: #3d5080;
    --fg:        #dde3f5;
    --fg2:       #7a8ab0;
    --fg3:       #4a5878;
    --accent:    #6c8fff;
    --accent2:   #94b0ff;
    --gold:      #f5c542;
    --green:     #4ade80;
    --red:       #f87171;
    --radius:    12px;
    --shadow:    0 4px 24px rgba(0,0,0,0.5);
}

/* ── Global resets ── */
body, .gradio-container {
    background: var(--bg) !important;
    font-family: 'DM Sans', sans-serif !important;
    color: var(--fg) !important;
    min-height: 100vh;
}

/* ── Remove default Gradio chrome ── */
.gradio-container > .main > .wrap { padding: 0 !important; }
footer { display: none !important; }

/* ── Landing hero ── */
.hero-wrap {
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 40px 24px;
    background: radial-gradient(ellipse 80% 60% at 50% 0%, rgba(108,143,255,0.12) 0%, transparent 70%);
    position: relative;
}
.hero-wrap::before {
    content: '';
    position: absolute;
    inset: 0;
    background: url("data:image/svg+xml,%3Csvg width='60' height='60' viewBox='0 0 60 60' xmlns='http://www.w3.org/2000/svg'%3E%3Cg fill='none' fill-rule='evenodd'%3E%3Cg fill='%23ffffff' fill-opacity='0.018'%3E%3Cpath d='M36 34v-4h-2v4h-4v2h4v4h2v-4h4v-2h-4zm0-30V0h-2v4h-4v2h4v4h2V6h4V4h-4zM6 34v-4H4v4H0v2h4v4h2v-4h4v-2H6zM6 4V0H4v4H0v2h4v4h2V6h4V4H6z'/%3E%3C/g%3E%3C/g%3E%3C/svg%3E");
    pointer-events: none;
}
.hero-badge {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    background: rgba(108,143,255,0.1);
    border: 1px solid rgba(108,143,255,0.3);
    border-radius: 100px;
    padding: 6px 16px;
    font-size: 13px;
    color: var(--accent2);
    margin-bottom: 32px;
    letter-spacing: 0.05em;
    font-weight: 500;
}
.hero-title {
    font-family: 'DM Serif Display', serif !important;
    font-size: clamp(40px, 7vw, 72px) !important;
    font-weight: 400 !important;
    line-height: 1.1 !important;
    text-align: center !important;
    color: var(--fg) !important;
    margin: 0 0 8px !important;
    letter-spacing: -0.02em !important;
}
.hero-title em {
    font-style: italic;
    color: var(--accent2);
}
.hero-sub {
    font-size: 18px !important;
    color: var(--fg2) !important;
    text-align: center !important;
    max-width: 520px !important;
    line-height: 1.6 !important;
    margin: 16px auto 48px !important;
}
.hero-pills {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
    justify-content: center;
    margin-bottom: 48px;
}
.hero-pill {
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: 100px;
    padding: 8px 18px;
    font-size: 14px;
    color: var(--fg2);
    display: flex;
    align-items: center;
    gap: 8px;
}
.start-btn button {
    background: var(--accent) !important;
    color: #fff !important;
    border: none !important;
    border-radius: var(--radius) !important;
    padding: 16px 48px !important;
    font-size: 17px !important;
    font-weight: 600 !important;
    font-family: 'DM Sans', sans-serif !important;
    cursor: pointer !important;
    transition: all 0.2s ease !important;
    box-shadow: 0 0 40px rgba(108,143,255,0.3) !important;
    letter-spacing: 0.01em !important;
}
.start-btn button:hover {
    background: var(--accent2) !important;
    transform: translateY(-2px) !important;
    box-shadow: 0 4px 50px rgba(108,143,255,0.45) !important;
}

/* ── App shell ── */
.app-wrap {
    max-width: 900px;
    margin: 0 auto;
    padding: 40px 24px 80px;
}
.app-header {
    display: flex;
    align-items: center;
    gap: 16px;
    margin-bottom: 40px;
    padding-bottom: 24px;
    border-bottom: 1px solid var(--border);
}
.app-logo {
    width: 44px;
    height: 44px;
    background: var(--accent);
    border-radius: 10px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 22px;
    flex-shrink: 0;
}
.app-title-text {
    font-family: 'DM Serif Display', serif;
    font-size: 24px;
    color: var(--fg);
}
.app-subtitle {
    font-size: 14px;
    color: var(--fg2);
    margin-top: 2px;
}

/* ── Sections ── */
.section-label {
    font-size: 11px !important;
    font-weight: 600 !important;
    letter-spacing: 0.1em !important;
    color: var(--fg3) !important;
    text-transform: uppercase !important;
    margin-bottom: 12px !important;
    display: block !important;
}

/* ── File upload area ── */
.upload-area .wrap {
    background: var(--bg3) !important;
    border: 2px dashed var(--border-hi) !important;
    border-radius: var(--radius) !important;
    min-height: 160px !important;
    transition: border-color 0.2s !important;
}
.upload-area .wrap:hover {
    border-color: var(--accent) !important;
}
.upload-area .wrap .label {
    color: var(--fg2) !important;
    font-size: 15px !important;
}

/* ── File list card ── */
.file-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px;
    margin-bottom: 16px;
}
.file-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 14px;
    background: var(--bg3);
    border-radius: 8px;
    margin-bottom: 8px;
}
.file-name { color: var(--fg); font-size: 14px; }
.file-size { color: var(--fg3); font-size: 13px; }

/* ── Settings card ── */
.settings-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 24px;
    margin-bottom: 24px;
}
.settings-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
}

/* ── Inputs & Sliders (Gradio overrides) ── */
label.svelte-1b6s6vi, .label-wrap { color: var(--fg2) !important; font-size: 13px !important; }
input[type=number], input[type=text], select, textarea {
    background: var(--bg3) !important;
    border: 1px solid var(--border) !important;
    border-radius: 8px !important;
    color: var(--fg) !important;
    font-family: 'DM Sans', sans-serif !important;
}
.svelte-1ipelgc input { background: var(--bg3) !important; color: var(--fg) !important; }

/* ── Buttons ── */
button.primary {
    background: var(--accent) !important;
    border: none !important;
    border-radius: var(--radius) !important;
    color: #fff !important;
    font-weight: 600 !important;
    font-size: 15px !important;
    transition: all 0.2s !important;
}
button.primary:hover { background: var(--accent2) !important; }
button.secondary {
    background: var(--bg3) !important;
    border: 1px solid var(--border) !important;
    border-radius: var(--radius) !important;
    color: var(--fg) !important;
    transition: all 0.2s !important;
}
button.secondary:hover { border-color: var(--accent) !important; color: var(--accent) !important; }

/* ── Generate button ── */
.generate-btn button {
    background: linear-gradient(135deg, var(--accent), #8b5cf6) !important;
    border: none !important;
    border-radius: var(--radius) !important;
    color: #fff !important;
    font-size: 16px !important;
    font-weight: 600 !important;
    font-family: 'DM Sans', sans-serif !important;
    padding: 14px 32px !important;
    width: 100% !important;
    transition: all 0.25s !important;
    box-shadow: 0 4px 24px rgba(108,143,255,0.25) !important;
    letter-spacing: 0.02em !important;
}
.generate-btn button:hover {
    opacity: 0.9 !important;
    transform: translateY(-1px) !important;
    box-shadow: 0 6px 32px rgba(108,143,255,0.35) !important;
}
.generate-btn button:disabled { opacity: 0.5 !important; transform: none !important; }

/* ── Submit / Results buttons ── */
.submit-btn button {
    background: var(--green) !important;
    border: none !important;
    color: #0c0f1a !important;
    font-weight: 700 !important;
    font-size: 15px !important;
    font-family: 'DM Sans', sans-serif !important;
    border-radius: var(--radius) !important;
    padding: 12px 32px !important;
    width: 100% !important;
    transition: all 0.2s !important;
}
.submit-btn button:hover { opacity: 0.85 !important; }
.retry-btn button {
    background: var(--bg3) !important;
    border: 1px solid var(--border) !important;
    border-radius: var(--radius) !important;
    color: var(--fg) !important;
    font-size: 15px !important;
    font-family: 'DM Sans', sans-serif !important;
    padding: 12px 32px !important;
    width: 100% !important;
    transition: all 0.2s !important;
}
.retry-btn button:hover { border-color: var(--accent) !important; }

/* ── Status/progress bar ── */
.status-bar {
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 16px;
    font-size: 14px;
    color: var(--fg2);
    min-height: 44px;
    display: flex;
    align-items: center;
}

/* ── Test question cards ── */
.question-block {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 24px;
    margin-bottom: 16px;
    transition: border-color 0.2s;
}
.question-block:hover { border-color: var(--border-hi); }
.question-num {
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.08em;
    color: var(--accent);
    text-transform: uppercase;
    margin-bottom: 10px;
}
.question-text {
    font-size: 16px;
    color: var(--fg);
    line-height: 1.55;
    margin-bottom: 18px;
    font-weight: 500;
}
.choice-row { padding: 3px 0; }
input[type=radio] { accent-color: var(--accent); width: 15px; height: 15px; }
.choice-label { font-size: 15px; color: var(--fg); cursor: pointer; line-height: 1.5; }

/* Radio groups from Gradio */
.gr-radio-group { gap: 6px !important; }
.gr-radio-group label {
    background: var(--bg3) !important;
    border: 1px solid var(--border) !important;
    border-radius: 8px !important;
    padding: 10px 16px !important;
    cursor: pointer !important;
    transition: all 0.15s !important;
    color: var(--fg) !important;
    font-size: 14px !important;
}
.gr-radio-group label:hover { border-color: var(--accent) !important; background: var(--bg2) !important; }
.gr-radio-group label.selected { border-color: var(--accent) !important; background: rgba(108,143,255,0.08) !important; }

/* ── Score banner ── */
.score-banner {
    text-align: center;
    padding: 40px 24px;
    border-radius: var(--radius);
    margin-bottom: 24px;
}
.score-banner.great { background: rgba(74,222,128,0.08); border: 1px solid rgba(74,222,128,0.25); }
.score-banner.ok    { background: rgba(245,197,66,0.08);  border: 1px solid rgba(245,197,66,0.25); }
.score-banner.poor  { background: rgba(248,113,113,0.08); border: 1px solid rgba(248,113,113,0.25); }
.score-number { font-family: 'DM Serif Display', serif; font-size: 72px; line-height: 1; }
.score-label  { font-size: 18px; color: var(--fg2); margin-top: 8px; }
.score-banner.great .score-number { color: var(--green); }
.score-banner.ok    .score-number { color: var(--gold); }
.score-banner.poor  .score-number { color: var(--red); }

/* ── Result items ── */
.result-item {
    border-radius: var(--radius);
    padding: 20px;
    margin-bottom: 12px;
    border: 1px solid;
}
.result-item.correct { background: rgba(74,222,128,0.05); border-color: rgba(74,222,128,0.2); }
.result-item.wrong   { background: rgba(248,113,113,0.05); border-color: rgba(248,113,113,0.2); }
.result-q  { font-size: 15px; font-weight: 500; color: var(--fg); margin-bottom: 10px; }
.result-ans{ font-size: 14px; margin: 3px 0; }
.result-correct-lbl { color: var(--green); font-weight: 600; }
.result-wrong-lbl   { color: var(--red); font-weight: 600; }
.result-expl { font-size: 14px; color: var(--fg2); margin-top: 10px; line-height: 1.5;
               border-top: 1px solid var(--border); padding-top: 10px; }

/* ── Progress bar ── */
.progress-wrap { background: var(--bg3); border-radius: 100px; height: 6px; overflow: hidden; margin-bottom: 8px; }
.progress-fill { height: 100%; background: linear-gradient(90deg, var(--accent), #8b5cf6); border-radius: 100px;
                 transition: width 0.4s ease; }

/* ── Error box ── */
.error-box {
    background: rgba(248,113,113,0.08);
    border: 1px solid rgba(248,113,113,0.3);
    border-radius: var(--radius);
    padding: 16px 20px;
    color: var(--red);
    font-size: 14px;
    display: flex;
    align-items: flex-start;
    gap: 10px;
}

/* ── Misc ── */
.gr-box, .gr-panel { background: var(--card) !important; border-color: var(--border) !important; }
.gradio-slider input[type=range] { accent-color: var(--accent); }
hr { border-color: var(--border) !important; }
"""

# ─────────────────────────────────────────────────────────────
#  STATE  (session-local; Gradio gr.State)
# ─────────────────────────────────────────────────────────────
# We store: uploaded file paths, extracted text, generated test JSON,
# and user answers.  All in-memory — nothing leaves the machine.

def _empty_state():
    return {
        "files":    [],        # list of (name, size_str, path)
        "text":     "",        # combined extracted text
        "test":     None,      # parsed dict from LLM
        "answers":  {},        # {question_id: chosen_letter}
        "results":  None,      # graded results list
        "page":     "home",    # home | upload | test | results
    }


# ─────────────────────────────────────────────────────────────
#  HTML HELPERS
# ─────────────────────────────────────────────────────────────

def _size_str(path: str) -> str:
    sz = os.path.getsize(path)
    if sz < 1024:       return f"{sz} B"
    if sz < 1024**2:    return f"{sz/1024:.1f} KB"
    return f"{sz/1024**2:.1f} MB"


def render_file_list(files: list) -> str:
    if not files:
        return '<p style="color:var(--fg3);font-size:14px;text-align:center;padding:16px 0">No files uploaded yet.</p>'
    items = ""
    for name, size, _ in files:
        items += f"""
        <div class="file-item">
          <span>📄 <span class="file-name">{name}</span></span>
          <span class="file-size">{size}</span>
        </div>"""
    return f'<div class="file-card">{items}</div>'


def render_test_html(test: dict) -> str:
    """Render the generated test as styled HTML with radio choices."""
    if not test:
        return ""
    qs = test.get("questions", [])
    blocks = ""
    for q in qs:
        qid  = q["id"]
        choices = "".join(
            f'<div class="choice-row">'
            f'<label style="display:flex;align-items:center;gap:10px;cursor:pointer">'
            f'<input type="radio" name="q{qid}" value="{k}" '
            f'onchange="window._answers = window._answers||{{}}; window._answers[{qid}]=\'{k}\'">'
            f'<span class="choice-label"><strong>{k}.</strong> {v}</span>'
            f'</label></div>'
            for k, v in q["choices"].items()
        )
        blocks += f"""
        <div class="question-block">
          <div class="question-num">Question {qid}</div>
          <div class="question-text">{q['question']}</div>
          {choices}
        </div>"""
    return blocks


def render_score_html(results: list, total: int) -> str:
    correct = sum(1 for r in results if r["correct"])
    pct     = int(correct / total * 100) if total else 0
    cls     = "great" if pct >= 70 else ("ok" if pct >= 50 else "poor")
    emoji   = "🎉" if pct >= 70 else ("😐" if pct >= 50 else "📚")

    items = ""
    for r in results:
        status = "correct" if r["correct"] else "wrong"
        tick   = "✅" if r["correct"] else "❌"
        items += f"""
        <div class="result-item {status}">
          <div class="result-q">{tick} Q{r['id']}: {r['question']}</div>
          <div class="result-ans result-correct-lbl">✓ Correct: {r['answer']} — {r['correct_text']}</div>
          {"" if r['correct'] else f'<div class="result-ans result-wrong-lbl">✗ Your answer: {r["chosen"]} — {r["chosen_text"]}</div>'}
          <div class="result-expl">💡 {r['explanation']}</div>
        </div>"""

    return f"""
    <div class="score-banner {cls}">
      <div class="score-number">{emoji} {pct}%</div>
      <div class="score-label">{correct} out of {total} correct</div>
    </div>
    {items}"""


# ─────────────────────────────────────────────────────────────
#  GRADIO APP
# ─────────────────────────────────────────────────────────────

def build_app():
    with gr.Blocks(css=CSS, title="Local PDF Practice Test") as app:
        state = gr.State(_empty_state())

        # ── PAGE: HOME ────────────────────────────────────────
        with gr.Column(visible=True, elem_classes="hero-wrap") as home_page:
            gr.HTML("""
            <div class="hero-badge">🔒 100% Local &nbsp;·&nbsp; 🚀 GPU-Accelerated &nbsp;·&nbsp; 🔑 No API Keys</div>
            <h1 class="hero-title">Turn any PDF into a<br><em>practice test</em></h1>
            <p class="hero-sub">
              Upload your lecture notes, textbooks, or documents.
              A local AI reads them and generates a full multiple-choice
              exam — entirely on your machine.
            </p>
            <div class="hero-pills">
              <div class="hero-pill">📄 Multiple PDFs</div>
              <div class="hero-pill">🤖 Local LLM (Ollama)</div>
              <div class="hero-pill">🎯 Adjustable Difficulty</div>
              <div class="hero-pill">✅ Instant Grading</div>
            </div>
            """)
            start_btn = gr.Button("Get Started →", elem_classes="start-btn")

        # ── PAGE: UPLOAD & GENERATE ───────────────────────────
        with gr.Column(visible=False, elem_classes="app-wrap") as upload_page:
            gr.HTML("""
            <div class="app-header">
              <div class="app-logo">📚</div>
              <div>
                <div class="app-title-text">PDF Practice Test Generator</div>
                <div class="app-subtitle">Fully local · GPU-accelerated · Zero cloud</div>
              </div>
            </div>
            """)

            with gr.Row():
                with gr.Column(scale=3):
                    gr.HTML('<span class="section-label">📂 Upload PDFs</span>')
                    file_upload = gr.File(
                        label="",
                        file_count="multiple",
                        file_types=[".pdf"],
                        elem_classes="upload-area",
                    )
                    file_list_html = gr.HTML('<p style="color:var(--fg3);font-size:14px;text-align:center;padding:16px 0">No files uploaded yet.</p>')
                    clear_btn = gr.Button("🗑  Clear All Files", elem_classes="retry-btn")

                with gr.Column(scale=2):
                    gr.HTML('<span class="section-label">⚙️ Test Settings</span>')
                    with gr.Group(elem_classes="settings-card"):
                        n_questions = gr.Slider(
                            minimum=5, maximum=30, value=15, step=1,
                            label="Number of Questions",
                        )
                        difficulty = gr.Radio(
                            choices=["Easy", "Medium", "Hard", "Mixed"],
                            value="Medium",
                            label="Difficulty",
                        )
                        model_info = gr.HTML(f"""
                        <div style="margin-top:12px;padding:10px 12px;background:var(--bg3);
                             border-radius:8px;font-size:13px;color:var(--fg2);">
                          🤖 Using: <strong style="color:var(--fg)">
                          {"Ollama / " + OLLAMA_MODEL if USE_OLLAMA else "llama-cpp-python"}</strong>
                        </div>""")

            status_html = gr.HTML('<div class="status-bar">Ready. Upload PDFs and click Generate.</div>')
            generate_btn = gr.Button(
                "✨ Generate Practice Test",
                elem_classes="generate-btn",
                size="lg",
            )
            back_btn1 = gr.Button("← Back to Home", elem_classes="retry-btn")

        # ── PAGE: TEST ────────────────────────────────────────
        with gr.Column(visible=False, elem_classes="app-wrap") as test_page:
            gr.HTML("""
            <div class="app-header">
              <div class="app-logo">🎯</div>
              <div>
                <div class="app-title-text">Practice Test</div>
                <div class="app-subtitle">Answer all questions, then click Submit</div>
              </div>
            </div>
            """)
            test_title_html  = gr.HTML("")
            test_body_html   = gr.HTML("")

            # We collect answers via a JSON text field (hidden but functional)
            # Users select radio buttons → JS writes to this field
            answers_json = gr.Textbox(
                label="Your Answers (auto-filled by radio buttons)",
                visible=False,
            )
            gr.HTML("""
            <div style="background:var(--bg3);border:1px solid var(--border);border-radius:8px;
                 padding:12px 16px;font-size:13px;color:var(--fg2);margin-bottom:16px;">
              ℹ️ Select one answer per question, then click Submit below.
            </div>""")

            # Per-question radio buttons (up to 30 questions)
            radio_components = []
            with gr.Column():
                for i in range(30):
                    r = gr.Radio(
                        choices=[],
                        label="",
                        visible=False,
                        elem_id=f"q_{i}",
                    )
                    radio_components.append(r)

            with gr.Row():
                back_btn2   = gr.Button("← Back", elem_classes="retry-btn", scale=1)
                submit_btn  = gr.Button("✅ Submit Test", elem_classes="submit-btn", scale=3)

        # ── PAGE: RESULTS ─────────────────────────────────────
        with gr.Column(visible=False, elem_classes="app-wrap") as results_page:
            gr.HTML("""
            <div class="app-header">
              <div class="app-logo">🏆</div>
              <div>
                <div class="app-title-text">Your Results</div>
                <div class="app-subtitle">Review your answers and explanations</div>
              </div>
            </div>
            """)
            results_html = gr.HTML("")
            with gr.Row():
                retry_btn  = gr.Button("🔄 Retake Test",       elem_classes="retry-btn", scale=1)
                new_btn    = gr.Button("📄 New PDFs",           elem_classes="retry-btn", scale=1)
                home_btn   = gr.Button("🏠 Home",               elem_classes="retry-btn", scale=1)

        # ─────────────────────────────────────────────────────
        #  EVENT HANDLERS
        # ─────────────────────────────────────────────────────

        def go_home(_state):
            s = _empty_state()
            return (
                s,
                gr.update(visible=True),   # home_page
                gr.update(visible=False),  # upload_page
                gr.update(visible=False),  # test_page
                gr.update(visible=False),  # results_page
            )

        def go_upload(_state):
            return (
                gr.update(visible=False),  # home_page
                gr.update(visible=True),   # upload_page
                gr.update(visible=False),  # test_page
                gr.update(visible=False),  # results_page
            )

        # ── File upload ──────────────────────────────────────
        def on_upload(files, state):
            if not files:
                return state, render_file_list([])
            existing = {p for _, _, p in state["files"]}
            for f in files:
                path = f.name
                if path not in existing:
                    name = Path(path).name
                    size = _size_str(path)
                    state["files"].append((name, size, path))
                    existing.add(path)
            return state, render_file_list(state["files"])

        def on_clear(state):
            state["files"] = []
            state["text"]  = ""
            return state, render_file_list([])

        # ── Generate test ─────────────────────────────────────
        def on_generate(state, n_q, diff):
            if not state["files"]:
                yield (
                    state,
                    '<div class="error-box">⚠️ Please upload at least one PDF before generating.</div>',
                    gr.update(visible=False), gr.update(visible=True),
                    gr.update(visible=False), gr.update(visible=False),
                    "", "", *([gr.update(visible=False, choices=[])] * 30),
                )
                return

            # Step 1: extract text
            yield (
                state,
                '<div class="status-bar">📖 Extracting text from PDFs…</div>',
                gr.update(visible=False), gr.update(visible=True),
                gr.update(visible=False), gr.update(visible=False),
                "", "", *([gr.update(visible=False, choices=[])] * 30),
            )

            combined = []
            for name, _, path in state["files"]:
                try:
                    raw  = extract_text_from_pdf(path)
                    combined.append(clean_text(raw))
                except Exception as e:
                    yield (
                        state,
                        f'<div class="error-box">⚠️ Error reading {name}: {e}</div>',
                        gr.update(visible=False), gr.update(visible=True),
                        gr.update(visible=False), gr.update(visible=False),
                        "", "", *([gr.update(visible=False, choices=[])] * 30),
                    )
                    return

            state["text"] = "\n\n---\n\n".join(combined)

            # Step 2: LLM call
            yield (
                state,
                f'<div class="status-bar">🤖 Generating {n_q} questions with local AI… (this may take 30–120 seconds)</div>',
                gr.update(visible=False), gr.update(visible=True),
                gr.update(visible=False), gr.update(visible=False),
                "", "", *([gr.update(visible=False, choices=[])] * 30),
            )

            try:
                prompt   = build_prompt(state["text"], int(n_q), diff)
                raw_resp = llm_generate(prompt, max_tokens=4000)
                test     = parse_test_json(raw_resp)
            except json.JSONDecodeError as e:
                yield (
                    state,
                    f'<div class="error-box">⚠️ The model returned malformed JSON. Try again or use a larger model.<br><small>{e}</small></div>',
                    gr.update(visible=False), gr.update(visible=True),
                    gr.update(visible=False), gr.update(visible=False),
                    "", "", *([gr.update(visible=False, choices=[])] * 30),
                )
                return
            except RuntimeError as e:
                yield (
                    state,
                    f'<div class="error-box">⚠️ {e}</div>',
                    gr.update(visible=False), gr.update(visible=True),
                    gr.update(visible=False), gr.update(visible=False),
                    "", "", *([gr.update(visible=False, choices=[])] * 30),
                )
                return
            except Exception as e:
                yield (
                    state,
                    f'<div class="error-box">⚠️ Unexpected error: {e}</div>',
                    gr.update(visible=False), gr.update(visible=True),
                    gr.update(visible=False), gr.update(visible=False),
                    "", "", *([gr.update(visible=False, choices=[])] * 30),
                )
                return

            state["test"]    = test
            state["answers"] = {}
            state["results"] = None

            # Build radio updates
            questions = test.get("questions", [])
            radio_updates = []
            for i in range(30):
                if i < len(questions):
                    q = questions[i]
                    choices = [f"{k}: {v}" for k, v in q["choices"].items()]
                    radio_updates.append(gr.update(
                        visible=True,
                        choices=choices,
                        value=None,
                        label=f"Q{q['id']}. {q['question']}",
                    ))
                else:
                    radio_updates.append(gr.update(visible=False, choices=[]))

            title_html = f"""
            <div style="margin-bottom:24px;">
              <h2 style="font-family:'DM Serif Display',serif;font-size:28px;color:var(--fg);margin:0 0 8px">
                {test.get('title','Practice Test')}
              </h2>
              <p style="color:var(--fg2);font-size:14px">{len(questions)} questions · {diff} difficulty</p>
            </div>"""

            yield (
                state,
                '<div class="status-bar">✅ Test generated! Switch to the test tab.</div>',
                gr.update(visible=False), gr.update(visible=False),
                gr.update(visible=True),  gr.update(visible=False),
                title_html, "", *radio_updates,
            )

        # ── Submit test ───────────────────────────────────────
        def on_submit(state, *radio_vals):
            test = state.get("test")
            if not test:
                return state, '<div class="error-box">⚠️ No test loaded.</div>', gr.update(), gr.update(), gr.update(), gr.update()

            questions = test["questions"]

            # Collect answers from radio values
            answers = {}
            for i, val in enumerate(radio_vals[:len(questions)]):
                if val:
                    # val is like "A: some text" — extract just the letter
                    letter = val.split(":")[0].strip()
                    answers[questions[i]["id"]] = letter

            # Grade
            results = []
            for q in questions:
                qid       = q["id"]
                chosen    = answers.get(qid, "—")
                correct   = q["answer"]
                is_correct= (chosen == correct)
                results.append({
                    "id":           qid,
                    "question":     q["question"],
                    "correct":      is_correct,
                    "answer":       correct,
                    "correct_text": q["choices"].get(correct, ""),
                    "chosen":       chosen,
                    "chosen_text":  q["choices"].get(chosen, "Not answered"),
                    "explanation":  q.get("explanation", ""),
                })

            state["results"]  = results
            state["answers"]  = answers

            html = render_score_html(results, len(questions))
            return (
                state,
                html,
                gr.update(visible=False),  # home
                gr.update(visible=False),  # upload
                gr.update(visible=False),  # test
                gr.update(visible=True),   # results
            )

        def on_retry(state):
            state["answers"] = {}
            state["results"] = None
            return (
                state,
                gr.update(visible=False),
                gr.update(visible=False),
                gr.update(visible=True),
                gr.update(visible=False),
            )

        # ─────────────────────────────────────────────────────
        #  WIRE UP EVENTS
        # ─────────────────────────────────────────────────────

        page_outputs = [home_page, upload_page, test_page, results_page]

        start_btn.click(go_upload, inputs=[state], outputs=page_outputs)
        back_btn1.click(go_home,   inputs=[state], outputs=[state] + page_outputs)
        home_btn.click( go_home,   inputs=[state], outputs=[state] + page_outputs)
        new_btn.click(  go_upload, inputs=[state], outputs=page_outputs)

        file_upload.upload(
            on_upload,
            inputs=[file_upload, state],
            outputs=[state, file_list_html],
        )
        clear_btn.click(on_clear, inputs=[state], outputs=[state, file_list_html])

        back_btn2.click(
            lambda s: (gr.update(visible=False), gr.update(visible=True),
                       gr.update(visible=False), gr.update(visible=False)),
            inputs=[state], outputs=page_outputs,
        )

        retry_btn.click(on_retry, inputs=[state], outputs=[state] + page_outputs)

        # Generate — uses yield for streaming status updates
        gen_outputs = (
            [state, status_html] + page_outputs +
            [test_title_html, test_body_html] +
            radio_components
        )
        generate_btn.click(
            on_generate,
            inputs=[state, n_questions, difficulty],
            outputs=gen_outputs,
        )

        # Submit
        submit_btn.click(
            on_submit,
            inputs=[state] + radio_components,
            outputs=[state, results_html] + page_outputs,
        )

    return app


# ─────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════════════╗
║   Local PDF Practice Test Generator                  ║
║   Fully offline · No API keys · GPU-accelerated      ║
╠══════════════════════════════════════════════════════╣
║  Opening at: http://localhost:7860                   ║
╚══════════════════════════════════════════════════════╝
""")
    app = build_app()
    app.launch(
        server_name = "0.0.0.0",   # accessible on local network too
        server_port = 7860,
        share       = False,       # never shares to Gradio cloud
        show_error  = True,
    )

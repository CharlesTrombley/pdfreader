"""
pdf_speech_reader.py — PDF Voice Reader + Podcast Generator
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Reads PDFs aloud with Kokoro TTS. Includes a slide-out Podcast panel that:
  1. Uses Claude AI, local Ollama model, or a smart splitter to write a two-host script.
  2. Previews the podcast live with two distinct voices.
  3. Saves to MP3 or WAV.

Install:
    pip install kokoro sounddevice soundfile pymupdf numpy anthropic requests
    pip install espeak-ng   (Windows: download installer from https://github.com/espeak-ng/espeak-ng/releases)
    Install Ollama: https://ollama.ai/
    Run: ollama pull llama3.2

For GPU (Nvidia CUDA):
    pip install torch --index-url https://download.pytorch.org/whl/cu121

Run standalone:
    python pdf_speech_reader.py

Or import the class:
    from pdf_speech_reader import PDFSpeechReader, VOICES, P, pdf_to_sentences, pause_after
"""

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import fitz
import threading
import re
import os
import time
import json
import subprocess
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf
from kokoro import KPipeline

from config import VOICES, P
from utils import pdf_to_sentences, pause_after
from ui import UIBuilder
from tts import TTSHandler

try:
    import anthropic as _anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

try:
    import requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

# Claude system prompt
SYSTEM_PROMPT = """\
You are a podcast script writer. Given source material from a PDF, write a
lively, engaging podcast episode between two hosts.

The hosts will be named based on user input — use the exact names provided.

Rules:
- Host A opens and closes the episode.
- Alternate turns naturally — each turn is 1-4 sentences.
- Stay faithful to the source material — no invented facts.
- Use casual spoken language: contractions, light humour, reactions like
  "Right,", "Exactly,", "That's wild,", "Interesting point,".
- Vary sentence length for natural rhythm.
- Aim for 40-80 turns depending on content length.
- Do NOT include stage directions, sound effects, or music cues.

Output ONLY valid JSON — an array of objects with "host" and "line" keys:
[
  {"host": "HOST_A_NAME", "line": "Hey everyone, welcome back to the show..."},
  {"host": "HOST_B_NAME", "line": "Today we're covering something really interesting..."},
  ...
]
Replace HOST_A_NAME and HOST_B_NAME with the actual names given.
No markdown fences, no extra keys, nothing outside the JSON array.
"""

def _get_length_directive(length: str) -> str:
    """Return script length guidance based on user selection."""
    directives = {
        "short": "Aim for 20-40 turns total.",
        "medium": "Aim for 60-100 turns total.",
        "long": "Aim for 120-180 turns total. Make it comprehensive and detailed."
    }
    return directives.get(length, directives["long"])

def _generate_script_via_api(text: str, api_key: str,
                              host_a: str, host_b: str, length: str = "long") -> list[dict]:
    client = _anthropic.Anthropic(api_key=api_key)
    excerpt = text[:14000] + ("\n\n[… content truncated …]" if len(text) > 14000 else "")
    prompt = SYSTEM_PROMPT.replace("SCRIPT_LENGTH_DIRECTIVE", _get_length_directive(length))
    prompt = prompt.replace("HOST_A_NAME", host_a).replace("HOST_B_NAME", host_b)
    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=6000,
        system=prompt,
        messages=[{"role": "user", "content": f"Source material:\n\n{excerpt}"}],
    )
    raw = msg.content[0].text.strip()
    # Strip any accidental markdown fences
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(raw)


def _generate_script_via_local(text: str, model: str,
                               host_a: str, host_b: str, length: str = "long") -> list[dict]:
    excerpt = text[:14000] + ("\n\n[… content truncated …]" if len(text) > 14000 else "")
    prompt = SYSTEM_PROMPT.replace("SCRIPT_LENGTH_DIRECTIVE", _get_length_directive(length))
    prompt = prompt.replace("HOST_A_NAME", host_a).replace("HOST_B_NAME", host_b)
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"Source material:\n\n{excerpt}"}
    ]
    response = requests.post("http://localhost:11434/api/chat", json={
        "model": model,
        "messages": messages,
        "stream": False
    })
    response.raise_for_status()
    result = response.json()
    raw = result["message"]["content"].strip()
    print(f"DEBUG: Raw Ollama response: {repr(raw[:500])}")  # Debug line
    # Strip any accidental markdown fences
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"DEBUG: JSON parse error: {e}")
        print(f"DEBUG: Attempting to fix common issues...")
        # Try to extract JSON from the response
        import re
        json_match = re.search(r'\[.*\]', raw, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except:
                pass
        # If all else fails, fall back to simple split
        print(f"DEBUG: Falling back to simple split")
        from utils import pdf_to_sentences
        sentences = pdf_to_sentences(text)
        return _generate_script_simple(sentences, host_a, host_b)


def _generate_script_simple(sentences: list[str],
                             host_a: str, host_b: str) -> list[dict]:
    """Fallback: smart alternating split with connective tissue."""
    intros = [
        (host_a, f"Welcome to the show. Today we're working through some really interesting material."),
        (host_b, f"That's right — let's get into it."),
    ]
    outros = [
        (host_a, "And that covers everything for today."),
        (host_b, f"Thanks for listening everyone — see you next time."),
    ]
    hosts = [host_a, host_b]
    body = [{"host": hosts[i % 2], "line": s} for i, s in enumerate(sentences)]
    return ([{"host": h, "line": l} for h, l in intros]
            + body
            + [{"host": h, "line": l} for h, l in outros])


# ── Main class ─────────────────────────────────────────────────────────────────

class PDFSpeechReader:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("PDF Voice Reader + Podcast  •  Kokoro")
        self.root.geometry("1200x760")
        self.root.minsize(860, 560)
        self.root.configure(bg=P["bg"])
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        # Podcast configuration
        self._pod_host_a_name = tk.StringVar(value="Alex")
        self._pod_host_b_name = tk.StringVar(value="Jordan")
        self._pod_host_a_voice = tk.StringVar(value="Heart (AF)")
        self._pod_host_b_voice = tk.StringVar(value="George (BM)")
        self._pod_apikey_var = tk.StringVar(value="")
        self._pod_status_var = tk.StringVar(value="Load PDF then Generate Script")
        self._pod_progress = tk.DoubleVar(value=0.0)
        self._pod_script: list[dict] = []
        self._pod_generating = False
        self._pod_panel_visible = False
        self._pod_panel: tk.Frame | None = None
        self._script_preview: tk.Text | None = None
        self._apikey_entry: tk.Entry | None = None
        self._show_key = tk.BooleanVar(value=False)
        self._use_local_ai = tk.BooleanVar(value=False)
        self._local_model_var = tk.StringVar(value="deepseek-r1")
        self._pod_length_var = tk.StringVar(value="long")
        self._pod_length_var = tk.StringVar(value="long")

        self.pages:  list[str]    = []
        self.page_offsets: list[int] = []
        self.sentence_spans: list[tuple[int, int, str]] = []

        self._current_sent_idx: int | None = None
        self._cursor_sent_idx:  int | None = None

        self.voice_var   = tk.StringVar(value="Heart (AF)")
        self.speed_var   = tk.StringVar(value="1.0")
        self.threads_var = tk.IntVar(value=min(4, os.cpu_count() or 4))
        self.device_var  = tk.StringVar(value="CPU")
        self.status_var  = tk.StringVar(value="Open a PDF to begin")
        self.file_var    = tk.StringVar(value="No file loaded")

        UIBuilder.apply_style(self.root)
        widgets = UIBuilder.build_ui(self.root, self.voice_var, self.speed_var, self.threads_var, self.device_var, self.status_var, self.file_var, self.open_pdf, self.read_from_cursor, self.read_selected, self.read_all, self.request_stop, self.toggle_pause, header_extras_callback=self._build_header_extras)
        self.page_list = widgets["page_list"]
        self.text = widgets["text"]
        self.progress_var = widgets["progress_var"]
        self.device_combo = widgets["device_combo"]
        self.page_list.bind("<<ListboxSelect>>", self._on_page_select)
        self.page_list.bind("<Double-Button-1>", self._on_page_double)
        self.text.bind("<ButtonRelease-1>", self._on_text_click)
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_change)

        self.tts = TTSHandler(self.root, self.voice_var, self.speed_var, self.device_var, self.status_var, self.progress_var, self._highlight_current)

        self._pod_panel = tk.Frame(self.root, bg=P["bg2"], width=300)
        self._build_podcast_panel(self._pod_panel)

    def _build_header_extras(self, hdr: tk.Frame):
        """Hook for subclasses to add widgets to the header bar."""
        ttk.Button(hdr, text="🎙 Podcast", command=self._toggle_podcast_panel,
                   style="Ghost.TButton").pack(side="right", padx=(0, 16), pady=10)

    def _toggle_podcast_panel(self):
        if self._pod_panel_visible:
            self._pod_panel.grid_remove()
            self._pod_panel_visible = False
        else:
            self._pod_panel.grid(row=1, column=2, rowspan=2, sticky="nsew")
            self._pod_panel_visible = True

    def _build_podcast_panel(self, panel: tk.Frame):
        panel.grid_propagate(False)

        def sep():
            tk.Frame(panel, bg=P["border"], height=1).pack(fill="x", padx=16, pady=10)

        def section(text: str):
            tk.Label(panel, text=text, bg=P["bg2"], fg=P["fg2"],
                     font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=16, pady=(12, 3))

        def row_frame():
            f = tk.Frame(panel, bg=P["bg2"])
            f.pack(fill="x", padx=16, pady=(0, 4))
            return f

        # ── Title ─────────────────────────────────────────────────────────────
        tk.Label(panel, text="🎙  Podcast Generator",
                 bg=P["bg2"], fg=P["fg"],
                 font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=16, pady=(16, 0))
        sep()

        # ── Host A ────────────────────────────────────────────────────────────
        section("HOST A")
        rf = row_frame()
        tk.Label(rf, text="Name", bg=P["bg2"], fg=P["fg2"],
                 font=("Segoe UI", 9), width=6, anchor="w").pack(side="left")
        tk.Entry(rf, textvariable=self._pod_host_a_name,
                 bg=P["bg3"], fg=P["fg"], insertbackground=P["accent"],
                 relief="flat", font=("Segoe UI", 9), width=12,
                 highlightthickness=1,
                 highlightbackground=P["border"],
                 highlightcolor=P["accent"]).pack(side="left", padx=(0, 8))
        tk.Label(rf, text="Voice", bg=P["bg2"], fg=P["fg2"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 4))
        ttk.Combobox(rf, textvariable=self._pod_host_a_voice,
                     values=list(VOICES.keys()), width=14,
                     state="readonly").pack(side="left")

        # ── Host B ────────────────────────────────────────────────────────────
        section("HOST B")
        rf = row_frame()
        tk.Label(rf, text="Name", bg=P["bg2"], fg=P["fg2"],
                 font=("Segoe UI", 9), width=6, anchor="w").pack(side="left")
        tk.Entry(rf, textvariable=self._pod_host_b_name,
                 bg=P["bg3"], fg=P["fg"], insertbackground=P["accent"],
                 relief="flat", font=("Segoe UI", 9), width=12,
                 highlightthickness=1,
                 highlightbackground=P["border"],
                 highlightcolor=P["accent"]).pack(side="left", padx=(0, 8))
        tk.Label(rf, text="Voice", bg=P["bg2"], fg=P["fg2"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 4))
        ttk.Combobox(rf, textvariable=self._pod_host_b_voice,
                     values=list(VOICES.keys()), width=14,
                     state="readonly").pack(side="left")

        sep()

        # ── Script Length ─────────────────────────────────────────────────────
        section("SCRIPT LENGTH")
        length_frame = tk.Frame(panel, bg=P["bg2"])
        length_frame.pack(fill="x", padx=16, pady=(0, 4))
        tk.Label(length_frame, text="Duration:", bg=P["bg2"], fg=P["fg2"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 4))
        ttk.Combobox(length_frame, textvariable=self._pod_length_var,
                     values=["short", "medium", "long"],
                     state="readonly", width=10).pack(side="left")
        tk.Label(length_frame, text="(20-40 | 60-100 | 120-180 lines)",
                 bg=P["bg2"], fg=P["fg2"],
                 font=("Segoe UI", 8)).pack(side="left", padx=(8, 0))

        sep()

        # ── API key ───────────────────────────────────────────────────────────
        section("ANTHROPIC API KEY  (optional)")
        tk.Label(panel,
                 text="Leave blank to use local AI or simple alternating split.\n"
                      "Add a key to have Claude write a real script.",
                 bg=P["bg2"], fg=P["fg2"], font=("Segoe UI", 8),
                 justify="left").pack(anchor="w", padx=16, pady=(0, 4))
        apikey_frame = tk.Frame(panel, bg=P["bg2"])
        apikey_frame.pack(fill="x", padx=16)
        self._apikey_entry = tk.Entry(
            apikey_frame, textvariable=self._pod_apikey_var,
            bg=P["bg3"], fg=P["fg"], insertbackground=P["accent"],
            relief="flat", font=("Segoe UI", 9), show="•", width=26,
            highlightthickness=1,
            highlightbackground=P["border"], highlightcolor=P["accent"],
        )
        self._apikey_entry.pack(side="left", padx=(0, 6))
        # Toggle show/hide key
        ttk.Checkbutton(apikey_frame, text="Show",
                        variable=self._show_key,
                        command=self._toggle_key_visibility,
                        style="TCheckbutton").pack(side="left")

        sep()

        # ── Local AI ──────────────────────────────────────────────────────────
        section("LOCAL AI (Ollama)")
        tk.Label(panel,
                 text="Use local Ollama model instead of API.\n"
                      "Requires Ollama running locally.",
                 bg=P["bg2"], fg=P["fg2"], font=("Segoe UI", 8),
                 justify="left").pack(anchor="w", padx=16, pady=(0, 4))
        local_frame = tk.Frame(panel, bg=P["bg2"])
        local_frame.pack(fill="x", padx=16, pady=(0, 4))
        ttk.Checkbutton(local_frame, text="Use local AI",
                        variable=self._use_local_ai,
                        style="TCheckbutton").pack(side="left", padx=(0, 10))
        tk.Label(local_frame, text="Model:", bg=P["bg2"], fg=P["fg2"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 4))
        ttk.Combobox(local_frame, textvariable=self._local_model_var,
                     values=["deepseek-r1", "qwen3:8b", "mixtral", "deepseek-llm", "llama3.2", "neural-chat", "zephyr"],
                     state="readonly", width=14).pack(side="left", padx=(0, 6))
        ttk.Button(local_frame, text="📥 Pull",
                   command=self._pull_model_async,
                   style="Ghost.TButton", width=6).pack(side="left")

        sep()

        # ── Script preview ────────────────────────────────────────────────────
        section("SCRIPT PREVIEW")
        script_frame = tk.Frame(panel, bg=P["bg2"])
        script_frame.pack(fill="both", expand=True, padx=16, pady=(0, 6))
        self._script_preview = tk.Text(
            script_frame, bg=P["bg3"], fg=P["fg"], font=("Segoe UI", 9),
            wrap="word", relief="flat", borderwidth=0, highlightthickness=0,
            state="disabled", width=36,
        )
        self._script_preview.pack(side="left", fill="both", expand=True)
        sp_scr = ttk.Scrollbar(script_frame, command=self._script_preview.yview)
        sp_scr.pack(side="right", fill="y")
        self._script_preview.config(yscrollcommand=sp_scr.set)
        self._script_preview.tag_configure("host_a",
            foreground=P["accent"], font=("Segoe UI", 9, "bold"))
        self._script_preview.tag_configure("host_b",
            foreground="#7ec8e3", font=("Segoe UI", 9, "bold"))
        self._script_preview.tag_configure("line", foreground=P["fg"])
        self._script_preview.tag_configure("playing",
            background=P["hl_bg"], foreground=P["hl_fg"])

        # ── Progress ──────────────────────────────────────────────────────────
        ttk.Progressbar(panel, variable=self._pod_progress,
                        maximum=100, mode="determinate"
                        ).pack(padx=16, pady=(4, 2), fill="x")

        tk.Label(panel, textvariable=self._pod_status_var,
                 bg=P["bg2"], fg=P["fg2"], font=("Segoe UI", 8),
                 wraplength=320, justify="left"
                 ).pack(anchor="w", padx=16, pady=(0, 8))

        # ── Action buttons ────────────────────────────────────────────────────
        sep()
        btn_frame = tk.Frame(panel, bg=P["bg2"])
        btn_frame.pack(padx=16, pady=(0, 16), fill="x")

        ttk.Button(btn_frame, text="✦ Generate Script",
                   command=self._generate_script_async,
                   style="Accent.TButton").pack(side="left", padx=(0, 6))
        ttk.Button(btn_frame, text="▶ Preview",
                   command=self._preview_podcast,
                   style="Ghost.TButton").pack(side="left", padx=(0, 6))
        ttk.Button(btn_frame, text="■ Stop",
                   command=self.request_stop,
                   style="Stop.TButton").pack(side="left", padx=(0, 6))
        ttk.Button(btn_frame, text="💾 Save",
                   command=self._save_podcast_async,
                   style="Ghost.TButton").pack(side="left")

    def _toggle_key_visibility(self):
        self._apikey_entry.config(show="" if self._show_key.get() else "•")

    def _pull_model_async(self):
        model_name = self._local_model_var.get()
        self._pod_status_var.set(f"Pulling {model_name}…")
        self._pod_progress.set(5)
        threading.Thread(target=self._pull_model_worker, args=(model_name,), daemon=True).start()

    def _pull_model_worker(self, model_name: str):
        try:
            self.root.after(0, lambda: self._pod_status_var.set(f"Downloading {model_name}… (this may take a few minutes)"))
            result = subprocess.run(["ollama", "pull", model_name], capture_output=True, text=True, timeout=600)
            if result.returncode == 0:
                self.root.after(0, lambda m=model_name: (
                    self._pod_status_var.set(f"✓ {m} ready!"),
                    self._pod_progress.set(100),
                ))
                time.sleep(2)
                self.root.after(0, lambda: self._pod_status_var.set("Load PDF then Generate Script"))
            else:
                self.root.after(0, lambda e=result.stderr: (
                    messagebox.showerror("Pull Error", f"Failed to pull model:\n{e}"),
                    self._pod_status_var.set("Pull failed. Check console."),
                ))
        except Exception as exc:
            self.root.after(0, lambda e=exc: (
                messagebox.showerror("Pull Error", str(e)),
                self._pod_status_var.set("Pull error."),
            ))
        finally:
            self._pod_progress.set(0)

    def _generate_script_async(self):
        if self._pod_generating:
            return
        if not self.pages:
            messagebox.showinfo("No PDF", "Open a PDF first.")
            return
        self._pod_generating = True
        self._pod_status_var.set("Generating script…")
        self._pod_progress.set(5)
        threading.Thread(target=self._generate_script_worker, daemon=True).start()

    def _generate_script_worker(self):
        try:
            full_text = "\n".join(self.pages)
            api_key   = self._pod_apikey_var.get().strip()
            host_a    = self._pod_host_a_name.get().strip() or "Alex"
            host_b    = self._pod_host_b_name.get().strip() or "Jordan"
            length    = self._pod_length_var.get()

            use_local = self._use_local_ai.get()
            if use_local and _REQUESTS_AVAILABLE:
                self.root.after(0, lambda: self._pod_status_var.set(
                    "Calling local AI to write the script…"))
                self._pod_script = _generate_script_via_local(
                    full_text, self._local_model_var.get(), host_a, host_b, length)
            elif api_key and _ANTHROPIC_AVAILABLE:
                self.root.after(0, lambda: self._pod_status_var.set(
                    "Calling Claude to write the script…"))
                self._pod_script = _generate_script_via_api(
                    full_text, api_key, host_a, host_b, length)
            else:
                if api_key and not _ANTHROPIC_AVAILABLE:
                    self.root.after(0, lambda: self._pod_status_var.set(
                        "anthropic not installed — using simple split."))
                    time.sleep(1)
                elif use_local and not _REQUESTS_AVAILABLE:
                    self.root.after(0, lambda: self._pod_status_var.set(
                        "requests not installed — using simple split."))
                    time.sleep(1)
                sentences = pdf_to_sentences(full_text)
                self._pod_script = _generate_script_simple(sentences, host_a, host_b)

            self.root.after(0, self._show_script_preview)
            self.root.after(0, lambda: self._pod_progress.set(100))
            self.root.after(0, lambda n=len(self._pod_script): self._pod_status_var.set(
                f"Script ready — {n} lines. Press ▶ Preview or 💾 Save."))
        except Exception as exc:
            self.root.after(0, lambda e=exc: (
                messagebox.showerror("Script Error", str(e)),
                self._pod_status_var.set("Error generating script."),
            ))
        finally:
            self._pod_generating = False

    def _show_script_preview(self, highlight_idx: int | None = None):
        self._script_preview.config(state="normal")
        self._script_preview.delete("1.0", tk.END)
        host_a = self._pod_host_a_name.get().strip() or "Alex"
        for i, item in enumerate(self._pod_script):
            host = item.get("host", "?")
            line = item.get("line", "")
            tag  = "host_a" if host == host_a else "host_b"
            mark = f"line_{i}"
            self._script_preview.mark_set(mark, tk.END)
            self._script_preview.insert(tk.END, f"{host}: ", (tag,))
            self._script_preview.insert(tk.END, f"{line}\n\n", ("line",))
        self._script_preview.config(state="disabled")
        if highlight_idx is not None:
            self._highlight_playing_line(highlight_idx)

    def _highlight_playing_line(self, idx: int):
        self._script_preview.config(state="normal")
        self._script_preview.tag_remove("playing", "1.0", tk.END)
        # Each entry is 2 lines (text + blank), so line number = idx*2+1
        start = f"{idx * 2 + 1}.0"
        end   = f"{idx * 2 + 2}.0"
        self._script_preview.tag_add("playing", start, end)
        self._script_preview.see(start)
        self._script_preview.config(state="disabled")

    def _synthesise_as(self, text: str, voice_key: str) -> tuple[np.ndarray, int]:
        """Synthesise using a specific voice (not the default dropdown voice)."""
        return self.tts.synthesise_with_voice(text, voice_key)

    def _preview_podcast(self):
        if not self._pod_script:
            messagebox.showinfo("No script", "Generate a script first.")
            return
        if self.tts.is_running:
            messagebox.showinfo("Busy", "Stop the reader first.")
            return
        self.tts.is_running  = True
        self.tts.should_stop = False
        self.tts.paused      = False
        self.tts._pause_evt.set()
        threading.Thread(target=self._podcast_play_worker,
                         args=(self._pod_script,), daemon=True).start()

    def _podcast_play_worker(self, script: list[dict]):
        try:
            if not self.tts._ensure_kokoro():
                return

            host_a      = self._pod_host_a_name.get().strip() or "Alex"
            voice_a_key = self._pod_host_a_voice.get()
            voice_b_key = self._pod_host_b_voice.get()
            total       = len(script)

            for idx, item in enumerate(script):
                if self.tts.should_stop:
                    break
                self.tts._pause_evt.wait()
                if self.tts.should_stop:
                    break

                host = item.get("host", host_a)
                line = item.get("line", "").strip()
                if not line:
                    continue

                voice_key = voice_a_key if host == host_a else voice_b_key

                self.root.after(0, lambda i=idx: self._highlight_playing_line(i))
                self.root.after(0, lambda n=idx+1, t=total, h=host:
                    self._pod_status_var.set(f"[{n}/{t}]  {h}"))
                self.root.after(0, lambda n=idx+1, t=total:
                    self._pod_progress.set(n / t * 100))

                samples, sr = self._synthesise_as(line, voice_key)
                if self.tts.should_stop:
                    break

                sd.play(samples, samplerate=sr)
                sd.wait()

                gap = pause_after(line)
                if gap and not self.tts.should_stop:
                    time.sleep(gap)

        finally:
            self.tts.is_running = False
            self.root.after(0, lambda: (
                self._pod_status_var.set(
                    "Preview stopped." if self.tts.should_stop else "Preview finished."),
                self._pod_progress.set(0),
            ))

    def _save_podcast_async(self):
        if not self._pod_script:
            messagebox.showinfo("No script", "Generate a script first.")
            return
        if self._pod_generating:
            messagebox.showinfo("Busy", "Script is still generating.")
            return
        out_path = filedialog.asksaveasfilename(
            title="Save podcast as…",
            defaultextension=".wav",
            filetypes=[("WAV audio", "*.wav"), ("MP3 audio", "*.mp3"), ("All files", "*.*")],
        )
        if not out_path:
            return
        self._pod_generating = True
        self._pod_status_var.set("Synthesising…")
        self._pod_progress.set(0)
        threading.Thread(target=self._save_podcast_worker,
                         args=(self._pod_script, out_path), daemon=True).start()

    def _save_podcast_worker(self, script: list[dict], out_path: str):
        try:
            if not self.tts._ensure_kokoro():
                return

            host_a      = self._pod_host_a_name.get().strip() or "Alex"
            voice_a_key = self._pod_host_a_voice.get()
            voice_b_key = self._pod_host_b_voice.get()
            total       = len(script)
            frames      = []
            sample_rate = 24000

            for idx, item in enumerate(script, 1):
                host = item.get("host", host_a)
                line = item.get("line", "").strip()
                if not line:
                    continue

                voice_key = voice_a_key if host == host_a else voice_b_key

                self.root.after(0, lambda n=idx, t=total, h=host:
                    self._pod_status_var.set(f"Synthesising [{n}/{t}]  {h}…"))
                self.root.after(0, lambda n=idx, t=total:
                    self._pod_progress.set(n / t * 100))

                samples, sr = self._synthesise_as(line, voice_key)
                sample_rate  = sr
                frames.append(samples)
                frames.append(np.zeros(int(sr * 0.18), dtype=np.float32))  # ~180ms gap

            if not frames:
                return

            combined = np.concatenate(frames)
            ext      = Path(out_path).suffix.lower()

            if ext == ".wav":
                self.root.after(0, lambda: self._pod_status_var.set("Writing WAV…"))
                sf.write(out_path, combined, sample_rate, subtype="PCM_16")

            elif ext == ".mp3":
                self.root.after(0, lambda: self._pod_status_var.set("Encoding MP3…"))
                try:
                    from pydub import AudioSegment
                except ImportError:
                    self.root.after(0, lambda: messagebox.showerror(
                        "Missing package", "MP3 needs pydub:\n  pip install pydub"))
                    return
                ffmpeg = self._find_ffmpeg()
                if not ffmpeg:
                    import queue as _q
                    q = _q.Queue()
                    self.root.after(0, lambda: q.put(
                        filedialog.askopenfilename(
                            title="Locate ffmpeg.exe",
                            filetypes=[("ffmpeg", "ffmpeg.exe"), ("All files", "*.*")])))
                    ffmpeg = q.get() or None
                if not ffmpeg:
                    self.root.after(0, lambda: messagebox.showerror(
                        "ffmpeg not found", "Install ffmpeg:\n  winget install ffmpeg"))
                    return
                AudioSegment.converter = ffmpeg
                pcm16 = np.clip(combined * 32767, -32768, 32767).astype(np.int16)
                AudioSegment(pcm16.tobytes(), frame_rate=sample_rate,
                             sample_width=2, channels=1
                             ).export(out_path, format="mp3", bitrate="192k")

            self.root.after(0, lambda p=out_path: (
                self._pod_status_var.set(f"Saved → {Path(p).name}"),
                messagebox.showinfo("Podcast saved", f"Saved to:\n{p}"),
            ))

        except Exception as exc:
            self.root.after(0, lambda e=exc: (
                messagebox.showerror("Save Error", str(e)),
                self._pod_status_var.set("Save failed."),
            ))
        finally:
            self._pod_generating = False
            self.root.after(0, lambda: self._pod_progress.set(0))

    # ══════════════════════════════════════════════════════════════════════════
    #  Controls
    # ══════════════════════════════════════════════════════════════════════════

    def on_closing(self):
        self.tts.request_stop()
        self.root.destroy()

    def request_stop(self):
        self.tts.request_stop()

    def toggle_pause(self):
        self.tts.toggle_pause()

    # ══════════════════════════════════════════════════════════════════════════
    #  PDF loading and display
    # ══════════════════════════════════════════════════════════════════════════

    def open_pdf(self):
        path = filedialog.askopenfilename(
            title="Select PDF", filetypes=[("PDF files", "*.pdf")]
        )
        if not path:
            return
        try:
            with fitz.open(path) as doc:
                self.pages = [p.get_text("text") for p in doc]
        except Exception as e:
            messagebox.showerror("PDF Error", f"Cannot open PDF\n\n{e}")
            return
        self.file_var.set(f"  {Path(path).name}")
        self._populate_display()
        self.status_var.set(
            f"Loaded {len(self.pages)} pages · {len(self.sentence_spans)} sentences  —  "
            f"click anywhere then press  ▶ Read from cursor"
        )

    def _populate_display(self):
        self.text.config(state="normal")
        self.text.delete("1.0", tk.END)
        self.page_list.delete(0, tk.END)
        self.sentence_spans.clear()
        self.page_offsets.clear()

        display_parts: list[str] = []
        char_offset = 0

        for i, raw in enumerate(self.pages):
            sep = f"── Page {i+1} ──\n"
            display_parts.append(sep)
            self.page_offsets.append(char_offset)
            char_offset += len(sep)

            cleaned = self._clean_page(raw)
            if not cleaned:
                display_parts.append("\n")
                char_offset += 1
                self.page_list.insert(tk.END, f"  {i+1}")
                continue

            sentences = pdf_to_sentences(cleaned)
            search_start = 0
            for sent in sentences:
                idx = cleaned.find(sent, search_start)
                if idx == -1:
                    idx = search_start
                self.sentence_spans.append((char_offset + idx, char_offset + idx + len(sent), sent))
                search_start = idx + len(sent)

            display_parts.append(cleaned + "\n\n")
            char_offset += len(cleaned) + 2
            self.page_list.insert(tk.END, f"  Page {i+1}")

        self.text.insert("1.0", "".join(display_parts))

        for i in range(len(self.pages)):
            offset = self.page_offsets[i]
            sep    = f"── Page {i+1} ──\n"
            self.text.tag_add("page_sep",
                              f"1.0 + {offset} chars",
                              f"1.0 + {offset + len(sep)} chars")

        self.text.config(state="disabled")

    def _clean_page(self, raw: str) -> str:
        text = re.sub(r"-\n(\S)", r"\1", raw)
        text = re.sub(r"\n{2,}", "\x00", text)
        text = re.sub(r"\n", " ", text)
        text = text.replace("\x00", "\n\n")
        return re.sub(r" {2,}", " ", text).strip()

    # ══════════════════════════════════════════════════════════════════════════
    #  Interaction
    # ══════════════════════════════════════════════════════════════════════════

    def _on_page_select(self, _event=None):
        sel = self.page_list.curselection()
        if not sel or sel[0] >= len(self.page_offsets):
            return
        self.text.see(f"1.0 + {self.page_offsets[sel[0]]} chars")

    def _on_page_double(self, _event=None):
        sel = self.page_list.curselection()
        if not sel:
            return
        page_idx   = sel[0]
        page_start = self.page_offsets[page_idx]
        page_end   = self.page_offsets[page_idx + 1] if page_idx + 1 < len(self.page_offsets) else float("inf")
        for i, (s, e, _) in enumerate(self.sentence_spans):
            if page_start <= s < page_end:
                self._start_reading_from(i)
                return

    def _on_text_click(self, event):
        idx         = self.text.index(f"@{event.x},{event.y}")
        char_offset = int(self.text.count("1.0", idx, "chars")[0])
        sent_idx    = self._sent_index_at(char_offset)
        if sent_idx is None:
            return
        self._cursor_sent_idx = sent_idx
        self._highlight_cursor(sent_idx)

    def _sent_index_at(self, char_offset: int) -> int | None:
        for i, (s, e, _) in enumerate(self.sentence_spans):
            if s <= char_offset < e:
                return i
        for i, (s, e, _) in enumerate(self.sentence_spans):
            if char_offset < s:
                return i
        return None

    def _highlight_cursor(self, sent_idx: int):
        self.text.tag_remove("cursor_sent", "1.0", tk.END)
        if sent_idx is None:
            return
        s, e, _ = self.sentence_spans[sent_idx]
        self.text.tag_add("cursor_sent", f"1.0 + {s} chars", f"1.0 + {e} chars")

    def _highlight_current(self, sent_idx: int | None):
        self.text.tag_remove("current", "1.0", tk.END)
        if sent_idx is None:
            return
        s, e, _ = self.sentence_spans[sent_idx]
        start_idx = f"1.0 + {s} chars"
        self.text.tag_add("current", start_idx, f"1.0 + {e} chars")
        self.text.see(start_idx)

    # ══════════════════════════════════════════════════════════════════════════
    #  Reading control
    # ══════════════════════════════════════════════════════════════════════════

    def read_from_cursor(self):
        if not self.sentence_spans:
            messagebox.showinfo("No text", "Open a PDF first.")
            return
        self._start_reading_from(self._cursor_sent_idx or 0)

    def read_selected(self):
        try:
            raw = self.text.get("sel.first", "sel.last").strip()
        except tk.TclError:
            messagebox.showinfo("Selection", "Select some text first.")
            return
        if not raw:
            return
        sents = pdf_to_sentences(raw)
        if not sents:
            return
        for i, (_, _, t) in enumerate(self.sentence_spans):
            if t == sents[0]:
                self._start_reading_from(i, override_sents=sents)
                return
        self._start_reading_from(0, override_sents=sents)

    def read_all(self):
        if not self.sentence_spans:
            messagebox.showinfo("No text", "Open a PDF first.")
            return
        self._start_reading_from(0)

    def _start_reading_from(self, sent_idx: int, override_sents: list[str] | None = None):
        if self.tts.is_running:
            messagebox.showinfo("Busy", "Already reading — stop first.")
            return
        self.tts.is_running  = True
        self.tts.should_stop = False
        self.tts.paused      = False
        self.tts._pause_evt.set()

        if override_sents:
            sentences    = override_sents
            start_idx    = 0
            global_start = None
        else:
            sentences    = [t for _, _, t in self.sentence_spans]
            start_idx    = sent_idx
            global_start = sent_idx

        self.status_var.set(f"Reading {len(sentences) - start_idx} sentences…")
        threading.Thread(
            target=self.tts._reading_thread,
            args=(sentences, start_idx, global_start),
            daemon=True,
        ).start()

    # ══════════════════════════════════════════════════════════════════════════
    #  Save audio
    # ══════════════════════════════════════════════════════════════════════════

    def save_audio(self):
        """Prompt for a file path and synthesise the whole PDF to MP3 or WAV."""
        if not self.sentence_spans:
            messagebox.showinfo("No text", "Open a PDF first.")
            return
        if self.is_running:
            messagebox.showinfo("Busy", "Stop playback before saving.")
            return
        out_path = filedialog.asksaveasfilename(
            title="Save audio as…",
            defaultextension=".wav",
            filetypes=[("WAV audio", "*.wav"), ("MP3 audio", "*.mp3"), ("All files", "*.*")],
        )
        if not out_path:
            return
        threading.Thread(
            target=self._save_audio_worker,
            args=([t for _, _, t in self.sentence_spans], out_path),
            daemon=True,
        ).start()

    def _save_audio_worker(self, sentences: list[str], out_path: str):
        try:
            if not self.tts._ensure_kokoro():
                return

            from concurrent.futures import ThreadPoolExecutor, as_completed

            total       = len(sentences)
            sample_rate = 24000
            results: dict[int, tuple[np.ndarray, int]] = {}
            completed   = 0

            # Synthesise sentences in parallel (Kokoro ONNX is thread-safe).
            # Workers capped at 4 — beyond that, CPU-bound ONNX shows diminishing returns.
            n_workers = min(self.threads_var.get(), os.cpu_count() or 1)
            self.root.after(0, lambda: self.status_var.set(
                f"Synthesising {total} sentences ({n_workers} threads)…"))

            def synth(idx: int, text: str):
                return idx, self.tts._synthesise(text)

            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                futures = {pool.submit(synth, i, s): i for i, s in enumerate(sentences)}
                for fut in as_completed(futures):
                    idx, (samples, sr) = fut.result()
                    results[idx] = (samples, sr)
                    sample_rate = sr
                    completed += 1
                    self.root.after(0, lambda c=completed, t=total: (
                        self.status_var.set(f"Synthesising [{c}/{t}]…"),
                        self.progress_var.set(f"{int(c/t*100)}%"),
                    ))

            # Reassemble in sentence order with punctuation-aware gaps
            frames = []
            for idx, sentence in enumerate(sentences):
                samples, sr = results[idx]
                frames.append(samples)
                gap = pause_after(sentence)
                if gap:
                    frames.append(np.zeros(int(sr * gap), dtype=np.float32))

            if not frames:
                return

            combined = np.concatenate(frames)
            ext      = Path(out_path).suffix.lower()

            if ext == ".wav":
                # WAV — pure soundfile, no ffmpeg needed at all
                self.root.after(0, lambda: self.status_var.set("Writing WAV…"))
                sf.write(out_path, combined, sample_rate, subtype="PCM_16")

            elif ext == ".mp3":
                self.root.after(0, lambda: self.status_var.set("Encoding MP3…"))

                # Check pydub is installed
                try:
                    from pydub import AudioSegment
                except ImportError:
                    self.root.after(0, lambda: messagebox.showerror(
                        "Missing package",
                        "MP3 export requires pydub.\n\nInstall it:\n  pip install pydub"
                    ))
                    return

                # Locate ffmpeg (needed by pydub for MP3 encoding)
                ffmpeg_exe = self._find_ffmpeg()
                if ffmpeg_exe is None:
                    # Ask the user to point to ffmpeg.exe — must run on main thread
                    import queue as _queue
                    q = _queue.Queue()
                    self.root.after(0, lambda: q.put(
                        filedialog.askopenfilename(
                            title="Locate ffmpeg.exe  (needed for MP3 export)",
                            filetypes=[("ffmpeg", "ffmpeg.exe"), ("All files", "*.*")],
                        )
                    ))
                    ffmpeg_exe = q.get() or None
                    if ffmpeg_exe:
                        self._ffmpeg_path = ffmpeg_exe   # cache for next time

                if not ffmpeg_exe:
                    self.root.after(0, lambda: messagebox.showerror(
                        "ffmpeg not found",
                        "MP3 encoding needs ffmpeg.\n\n"
                        "Install it (then restart this app):\n"
                        "  winget install ffmpeg\n\n"
                        "Or download from https://ffmpeg.org/download.html\n"
                        "and browse to ffmpeg.exe when prompted."
                    ))
                    return

                # Point pydub at the ffmpeg binary and encode
                AudioSegment.converter = ffmpeg_exe
                pcm16 = np.clip(combined * 32767, -32768, 32767).astype(np.int16)
                AudioSegment(
                    pcm16.tobytes(),
                    frame_rate=sample_rate,
                    sample_width=2,
                    channels=1,
                ).export(out_path, format="mp3", bitrate="192k")

            else:
                # Unknown extension — fall back to WAV
                out_path = out_path + ".wav"
                self.root.after(0, lambda: self.status_var.set("Writing WAV…"))
                sf.write(out_path, combined, sample_rate, subtype="PCM_16")

            self.root.after(0, lambda p=out_path: (
                self.status_var.set(f"Saved → {Path(p).name}"),
                self.progress_var.set(""),
                messagebox.showinfo("Saved", f"Audio saved to:\n{p}"),
            ))

        except Exception as exc:
            self.root.after(0, lambda e=exc: (
                messagebox.showerror("Save Error", str(e)),
                self.status_var.set("Save failed."),
                self.progress_var.set(""),
            ))

    def _find_ffmpeg(self) -> str | None:
        """
        Return the path to ffmpeg.exe, or None if not found.
        Checks (in order):
          1. A path saved from a previous session (self._ffmpeg_path)
          2. System PATH  (works if installed via winget / chocolatey / scoop)
          3. Common manual install locations on Windows
        """
        import shutil

        # Cached from a previous call this session
        if getattr(self, "_ffmpeg_path", None):
            return self._ffmpeg_path

        # On PATH
        found = shutil.which("ffmpeg")
        if found:
            self._ffmpeg_path = found
            return found

        # Common Windows locations people drop ffmpeg into
        common = [
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
            str(Path.home() / "ffmpeg" / "bin" / "ffmpeg.exe"),
            str(SCRIPT_DIR / "ffmpeg.exe"),          # next to the script
            str(SCRIPT_DIR / "ffmpeg" / "bin" / "ffmpeg.exe"),
        ]
        for p in common:
            if Path(p).exists():
                self._ffmpeg_path = p
                return p

        return None

    def _on_device_change(self, _event=None):
        """Changing device requires reloading the model — clear cached pipelines."""
        self.tts.reset_kokoro()
        device = self.device_var.get()
        self.status_var.set(f"Device changed to {device} — model will reload on next use.")

# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    root = tk.Tk()
    app = PDFSpeechReader(root)
    root.mainloop()
"""
podcast_maker.py — PodcastMaker (subclass of PDFSpeechReader)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Adds a slide-out Podcast panel that:
  1. Uses Claude AI (optional) or a smart splitter to write a two-host script.
  2. Previews the podcast live with two distinct voices.
  3. Saves to MP3 or WAV.

Extra install:
    pip install anthropic

Run:
    python podcast_maker.py
"""

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import threading
import time
import json
import numpy as np
import sounddevice as sd
import soundfile as sf
from pathlib import Path

from pdf_speech_reader import PDFSpeechReader, VOICES, P, pdf_to_sentences, pause_after

try:
    import anthropic as _anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False


# ── Claude system prompt ───────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a podcast script writer. Given source material from a PDF, write a
lively, natural-sounding podcast conversation between two hosts. It must sound
like two real people talking — NOT like two essays being read aloud.

The hosts will be named based on user input — use the exact names provided.

━━━ CONVERSATION RULES ━━━

NATURALNESS — this is the most important rule:
- Write the way people actually talk, not the way they write.
- Use casual speech: "yeah", "honestly", "I mean", "you know", "right?",
  "wait, really?", "that's wild", "okay but—", "huh, interesting".
- Vary sentence length dramatically. Some turns are a single word or short
  reaction. Others are 2-3 sentences. Never write long paragraphs per turn.
- Include filler sounds and natural pauses: "[laughs]", "[pause]", "uh", "like".

INTERRUPTIONS & REACTIONS — make it feel like a real back-and-forth:
- Occasionally have a host cut in mid-thought: "— wait, hold on." or
  "Sorry to jump in, but—"
- Hosts must react to what the other just said before making their own point.
  Good: "Oh wow, yeah, and on top of that..." Bad: starting a new monologue.
- Use short reactive turns often: "No way.", "Okay that makes sense.",
  "I was not expecting that.", "Yeah, same.", "Wait, say that again."

TANGENTS & PERSONALITY:
- Include at least one small tangent or joke that feels organic.
  e.g., comparing something in the material to everyday life, or making a
  self-deprecating aside before getting back on topic.
- Hosts should disagree occasionally: "I don't know if I fully buy that.",
  "Hmm, I see it differently actually.", "That's fair, but—"
- Ask each other questions and build on each other's answers.
- Host A opens and closes the episode casually, not formally.

STRUCTURE:
- Aim for 50-90 turns depending on content length.
- Stay faithful to the source material — no invented facts.
- Do NOT include music cues, sound effects, or narrator-style stage directions.
  The ONLY allowed stage direction-style text is laughter/pause markers like
  [laughs] or [pause], embedded inside a line string.

━━━ OUTPUT FORMAT ━━━

Output ONLY valid JSON — an array of objects with "host" and "line" keys:
[
  {"host": "HOST_A_NAME", "line": "Hey, welcome back — so today we're getting into something I've been kind of obsessed with lately."},
  {"host": "HOST_B_NAME", "line": "Same, honestly. I went down a rabbit hole last night reading about this."},
  {"host": "HOST_A_NAME", "line": "Wait, really? [laughs] Okay so you already know more than me, great."},
  ...
]
Replace HOST_A_NAME and HOST_B_NAME with the actual names given.
No markdown fences, no extra keys, nothing outside the JSON array.
"""


# ── Script generation helpers ──────────────────────────────────────────────────

def _generate_script_via_api(text: str, api_key: str,
                              host_a: str, host_b: str) -> list[dict]:
    client  = _anthropic.Anthropic(api_key=api_key)
    excerpt = text[:14000] + ("\n\n[… content truncated …]" if len(text) > 14000 else "")
    prompt  = SYSTEM_PROMPT.replace("HOST_A_NAME", host_a).replace("HOST_B_NAME", host_b)
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


def _generate_script_simple(sentences: list[str],
                             host_a: str, host_b: str) -> list[dict]:
    """Fallback: alternating split with natural-sounding connective tissue."""
    reactions_a = [
        "Okay yeah, and building on that —",
        "Right, exactly. So —",
        "Huh, interesting. I mean —",
        "Yeah, that tracks. And —",
        "Wait, so basically —",
    ]
    reactions_b = [
        "That's a good point. I'd add —",
        "Yeah. Honestly —",
        "Right, and on top of that —",
        "I mean, yeah. And —",
        "Okay, so —",
    ]
    intros = [
        (host_a, "Hey, welcome back. So today we are getting into something I've been kind of curious about."),
        (host_b, "Yeah same, honestly. I feel like this is one of those topics where the more you dig in, the more there is to it."),
        (host_a, "Exactly. Alright, let's just jump in."),
    ]
    outros = [
        (host_b, "Okay, I think that pretty much covers it."),
        (host_a, "Yeah. [pause] Lots to think about."),
        (host_b, "For sure. Thanks everyone for listening — see you next time."),
        (host_a, "Later!"),
    ]
    hosts = [host_a, host_b]
    reacts = [reactions_a, reactions_b]
    body: list[dict] = []
    for i, s in enumerate(sentences):
        host = hosts[i % 2]
        if i > 0 and i % 3 == 0:
            react = reacts[i % 2][i // 3 % len(reacts[i % 2])]
            line = f"{react} {s}"
        else:
            line = s
        body.append({"host": host, "line": line})
    return ([{"host": h, "line": l} for h, l in intros]
            + body
            + [{"host": h, "line": l} for h, l in outros])


# ── Subclass ───────────────────────────────────────────────────────────────────

class PodcastMaker(PDFSpeechReader):
    """
    Extends PDFSpeechReader with a slide-out Podcast panel.
    Click 🎙 Podcast in the header bar to open/close it.
    """

    def __init__(self, root: tk.Tk):
        # Must init before super().__init__ which calls _build_ui
        self._pod_host_a_name = tk.StringVar(value="Alex")
        self._pod_host_b_name = tk.StringVar(value="Jordan")
        self._pod_host_a_voice = tk.StringVar(value="Heart (AF)")
        self._pod_host_b_voice = tk.StringVar(value="George (BM)")
        self._pod_apikey_var   = tk.StringVar(value="")
        self._pod_status_var   = tk.StringVar(value="Load a PDF, then press Generate Script.")
        self._pod_progress     = tk.DoubleVar(value=0.0)
        self._pod_script: list[dict] = []
        self._pod_generating   = False
        self._pod_panel_visible = False

        super().__init__(root)
        self.root.title("PDF Voice Reader + Podcast  •  Kokoro")
        self.root.geometry("1200x760")

    # ── Header hook ───────────────────────────────────────────────────────────

    def _build_header_extras(self, hdr: tk.Frame):
        ttk.Button(hdr, text="🎙  Podcast",
                   command=self._toggle_podcast_panel,
                   style="Ghost.TButton").pack(side="right", padx=(0, 8), pady=10)
        self._pod_panel = tk.Frame(self.root, bg=P["bg2"], width=360)
        self._build_podcast_panel(self._pod_panel)

    def _toggle_podcast_panel(self):
        if self._pod_panel_visible:
            self._pod_panel.grid_remove()
            self._pod_panel_visible = False
        else:
            self._pod_panel.grid(row=1, column=2, rowspan=2, sticky="nsew")
            self._pod_panel_visible = True

    # ── Panel UI ──────────────────────────────────────────────────────────────

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

        # ── API key ───────────────────────────────────────────────────────────
        section("ANTHROPIC API KEY  (optional)")
        tk.Label(panel,
                 text="Leave blank to use a simple alternating split.\n"
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
        self._show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(apikey_frame, text="Show",
                        variable=self._show_key,
                        command=self._toggle_key_visibility,
                        style="TCheckbutton").pack(side="left")

        sep()

        # Ollama Model
        sep()
        section("OLLAMA MODEL (for Local AI)")
        ollama_frame = row_frame()
        tk.Label(ollama_frame, text="Model:", bg=P["bg2"], fg=P["fg2"], font=("Segoe UI", 9)
                 ).pack(side="left")
        ttk.Combobox(ollama_frame, textvariable=self._pod_ollama_model,
                     values=["llama3.2", "llama3.1", "mistral", "qwen2.5", "phi4", "gemma2"],
                     width=18, state="readonly").pack(side="left", padx=(8, 0))

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

    # ── Updated Script Generation ──────────────────────────────────────────────
    def _generate_script_async(self):
        if self._pod_generating:
            return
        if not self.pages:
            messagebox.showinfo("No PDF", "Open a PDF first.")
            return

        mode = self._pod_ai_mode.get()
        if mode == "Claude" and not self._pod_apikey_var.get().strip():
            messagebox.showwarning("Missing Key", "Please enter your Anthropic API key for Claude mode.")
            return
        if mode == "Ollama" and not _OLLAMA_AVAILABLE:
            messagebox.showerror("Ollama not installed", "Run: pip install ollama")
            return

        self._pod_generating = True
        self._pod_status_var.set(f"Generating script with {mode}...")
        self._pod_progress.set(10)

        threading.Thread(target=self._generate_script_worker, daemon=True).start()

    def _generate_script_worker(self):
        try:
            full_text = "\n".join(self.pages)
            mode = self._pod_ai_mode.get()
            host_a = self._pod_host_a_name.get().strip() or "Alex"
            host_b = self._pod_host_b_name.get().strip() or "Jordan"

            if mode == "Claude" and _ANTHROPIC_AVAILABLE:
                self.root.after(0, lambda: self._pod_status_var.set("Calling Claude..."))
                self._pod_script = _generate_script_via_claude(
                    full_text, self._pod_apikey_var.get().strip(), host_a, host_b)

            elif mode == "Ollama" and _OLLAMA_AVAILABLE:
                model = self._pod_ollama_model.get()
                self.root.after(0, lambda m=model: self._pod_status_var.set(f"Calling Ollama ({m})..."))
                self._pod_script = _generate_script_via_ollama(full_text, model, host_a, host_b)

            else:
                # Simple fallback
                if mode != "Simple":
                    self.root.after(0, lambda: self._pod_status_var.set("Falling back to Simple Split..."))
                    time.sleep(1)
                sentences = pdf_to_sentences(full_text)
                self._pod_script = _generate_script_simple(sentences, host_a, host_b)

            self.root.after(0, self._show_script_preview)
            self.root.after(0, lambda: self._pod_progress.set(100))
            self.root.after(0, lambda n=len(self._pod_script): self._pod_status_var.set(
                f"Script ready — {n} lines. Ready to preview or save."))

        except Exception as exc:
            self.root.after(0, lambda e=exc: (
                messagebox.showerror("Script Error", f"{type(e).__name__}: {e}"),
                self._pod_status_var.set("Error generating script.")
            ))
        finally:
            self._pod_generating = False

    # ── Synthesise a line with a specific voice ────────────────────────────────

    def _synthesise_as(self, text: str, voice_key: str) -> tuple[np.ndarray, int]:
        """Like _synthesise but uses a specific voice regardless of the main dropdown."""
        from kokoro import KPipeline
        voice_id, lang_code = VOICES.get(voice_key, ("af_heart", "a"))
        speed    = float(self.speed_var.get())
        pipeline = self.kokoro[lang_code]
        chunks   = []
        for _, _, audio in pipeline(text, voice=voice_id, speed=speed):
            chunks.append(audio)
        samples = np.concatenate(chunks) if chunks else np.zeros(1, dtype=np.float32)
        return samples, 24000

    # ── Preview playback ──────────────────────────────────────────────────────

    def _preview_podcast(self):
        if not self._pod_script:
            messagebox.showinfo("No script", "Generate a script first.")
            return
        if self.is_running:
            messagebox.showinfo("Busy", "Stop the reader first.")
            return
        self.is_running  = True
        self.should_stop = False
        self.paused      = False
        self._pause_evt.set()
        threading.Thread(target=self._podcast_play_worker,
                         args=(self._pod_script,), daemon=True).start()

    def _podcast_play_worker(self, script: list[dict]):
        try:
            if not self._ensure_kokoro():
                return

            host_a      = self._pod_host_a_name.get().strip() or "Alex"
            voice_a_key = self._pod_host_a_voice.get()
            voice_b_key = self._pod_host_b_voice.get()
            total       = len(script)

            for idx, item in enumerate(script):
                if self.should_stop:
                    break
                self._pause_evt.wait()
                if self.should_stop:
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
                if self.should_stop:
                    break

                sd.play(samples, samplerate=sr)
                sd.wait()

                gap = pause_after(line)
                if gap and not self.should_stop:
                    time.sleep(gap)

        finally:
            self.is_running = False
            self.root.after(0, lambda: (
                self._pod_status_var.set(
                    "Preview stopped." if self.should_stop else "Preview finished."),
                self._pod_progress.set(0),
            ))

    # ── Save ──────────────────────────────────────────────────────────────────

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
            if not self._ensure_kokoro():
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


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    root = tk.Tk()
    app = PodcastMaker(root)
    root.mainloop()
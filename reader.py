# reader.py
import tkinter as tk
from tkinter import filedialog, messagebox
import fitz
import threading
import os
import numpy as np
import sounddevice as sd
from config import VOICES, P
from utils import pdf_to_sentences
from tts import TTSHandler
from ui import UIBuilder

class PDFSpeechReader:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("PDF Voice Reader")
        self.root.geometry("1120x720")
        self.root.minsize(860, 560)
        self.root.configure(bg=P["bg"])
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.kokoro: dict | None = None   # dict of KPipeline keyed by lang_code
        self.pages:  list[str]    = []
        self.page_offsets: list[int] = []
        self.sentence_spans: list[tuple[int, int, str]] = []

        self.is_running   = False
        self.should_stop  = False
        self.paused       = False
        self._pause_evt   = threading.Event()
        self._pause_evt.set()

        self._current_sent_idx: int | None = None
        self._cursor_sent_idx:  int | None = None

        self.voice_var   = tk.StringVar(value="Heart (AF)")
        self.speed_var   = tk.StringVar(value="1.0")
        self.threads_var = tk.IntVar(value=min(4, os.cpu_count() or 4))
        self.device_var  = tk.StringVar(value="CPU")
        self.status_var  = tk.StringVar(value="Open a PDF to begin")
        self.file_var    = tk.StringVar(value="No file loaded")

        UIBuilder.apply_style(self.root)
        widgets = UIBuilder.build_ui(self.root, self.voice_var, self.speed_var, self.threads_var, self.device_var, 
                           self.status_var, self.file_var, self.open_pdf, self.read_from_cursor, 
                           self.read_selected, self.read_all, self.request_stop, self.toggle_pause)
        self.page_list = widgets["page_list"]
        self.text = widgets["text"]
        self.progress_var = widgets["progress_var"]
        self.device_combo = widgets["device_combo"]
        self.page_list.bind("<<ListboxSelect>>", self._on_page_select)
        self.page_list.bind("<Double-Button-1>", self._on_page_double)
        self.text.bind("<ButtonRelease-1>", self._on_text_click)
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_change)
        self.tts = TTSHandler(self.root, self.voice_var, self.speed_var, self.status_var, 
                              self.progress_var, self._highlight_current)

    def _build_header_extras(self, hdr: tk.Frame):
        """Hook for subclasses to add widgets to the header bar."""
        pass

    # ══════════════════════════════════════════════════════════════════════════
    #  Controls
    # ══════════════════════════════════════════════════════════════════════════

    def on_closing(self):
        self.request_stop()
        self.root.destroy()

    def request_stop(self):
        self.should_stop = True
        self.paused = False
        self._pause_evt.set()
        import sounddevice as sd
        sd.stop()
        self.status_var.set("Stopped.")

    def toggle_pause(self):
        if not self.is_running:
            return
        self.paused = not self.paused
        if self.paused:
            self._pause_evt.clear()
            self.status_var.set("Paused.")
        else:
            self._pause_evt.set()
            self.status_var.set("Resumed.")

    # ══════════════════════════════════════════════════════════════════════════
    #  PDF loading
    # ══════════════════════════════════════════════════════════════════════════

    def open_pdf(self):
        path = filedialog.askopenfilename(
            title="Open PDF",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            doc = fitz.open(path)
            self.pages = [page.get_text() for page in doc]
            doc.close()
            self.page_offsets = [0]
            for i, page in enumerate(self.pages):
                self.page_offsets.append(self.page_offsets[-1] + len(page))
            self.file_var.set(f"{os.path.basename(path)} ({len(self.pages)} pages)")
            self.status_var.set("PDF loaded. Select a page to view.")
            self._populate_display()
        except Exception as e:
            messagebox.showerror("PDF Error", f"Failed to open PDF: {e}")

    def _populate_display(self):
        self.page_list.delete(0, tk.END)
        for i, page in enumerate(self.pages):
            preview = page[:50].replace("\n", " ").strip()
            if len(page) > 50:
                preview += "…"
            self.page_list.insert(tk.END, f"{i+1:2d}. {preview}")
        if self.pages:
            self.page_list.selection_set(0)
            self._on_page_select()

    def _clean_page(self, raw: str) -> str:
        return raw.replace("\n", " ").replace("  ", " ").strip()

    # ══════════════════════════════════════════════════════════════════════════
    #  Interaction
    # ══════════════════════════════════════════════════════════════════════════

    def _on_page_select(self, _event=None):
        sel = self.page_list.curselection()
        if not sel:
            return
        idx = sel[0]
        raw = self.pages[idx]
        clean = self._clean_page(raw)
        self.text.config(state="normal")
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", clean)
        self.text.config(state="disabled")
        self.sentence_spans = []
        sentences = pdf_to_sentences(clean)
        char_offset = 0
        for sent in sentences:
            start = clean.find(sent, char_offset)
            if start == -1:
                continue
            end = start + len(sent)
            self.sentence_spans.append((start, end, sent))
            char_offset = end
        self._highlight_cursor(None)

    def _on_page_double(self, _event=None):
        self.read_all()

    def _on_text_click(self, event):
        idx = self.text.index(f"@{event.x},{event.y}")
        char_offset = self.text.count("1.0", idx, "chars")[0]
        sent_idx = self._sent_index_at(char_offset)
        if sent_idx is not None:
            self._cursor_sent_idx = sent_idx
            self._highlight_cursor(sent_idx)

    def _sent_index_at(self, char_offset: int) -> int | None:
        for i, (start, end, _) in enumerate(self.sentence_spans):
            if start <= char_offset < end:
                return i
        return None

    def _highlight_cursor(self, sent_idx: int):
        self.text.tag_remove("cursor_sent", "1.0", tk.END)
        if sent_idx is not None:
            start, end, _ = self.sentence_spans[sent_idx]
            self.text.tag_add("cursor_sent", f"1.0+{start}c", f"1.0+{end}c")

    def _highlight_current(self, sent_idx: int | None):
        self.text.tag_remove("current", "1.0", tk.END)
        if sent_idx is not None:
            start, end, _ = self.sentence_spans[sent_idx]
            self.text.tag_add("current", f"1.0+{start}c", f"1.0+{end}c")
            self.text.see(f"1.0+{start}c")

    # ══════════════════════════════════════════════════════════════════════════
    #  Reading control
    # ══════════════════════════════════════════════════════════════════════════

    def read_from_cursor(self):
        if self._cursor_sent_idx is None:
            messagebox.showinfo("No cursor", "Click in the text to set a cursor.")
            return
        sentences = [span[2] for span in self.sentence_spans]
        self._start_reading_from(self._cursor_sent_idx, sentences)

    def read_selected(self):
        try:
            sel = self.text.get(tk.SEL_FIRST, tk.SEL_LAST)
        except tk.TclError:
            messagebox.showinfo("No selection", "Select text to read.")
            return
        sentences = pdf_to_sentences(sel)
        self._start_reading_from(0, sentences)

    def read_all(self):
        sentences = [span[2] for span in self.sentence_spans]
        self._start_reading_from(0, sentences)

    def _start_reading_from(self, sent_idx: int, override_sents: list[str] | None = None):
        if self.is_running:
            return
        sentences = override_sents or [span[2] for span in self.sentence_spans[sent_idx:]]
        if not sentences:
            return
        self.is_running  = True
        self.should_stop = False
        self.paused      = False
        self._pause_evt.set()
        self._current_sent_idx = sent_idx
        threading.Thread(target=self.tts._reading_thread,
                         args=(sentences, 0, sent_idx), daemon=True).start()

    # ══════════════════════════════════════════════════════════════════════════
    #  Save audio
    # ══════════════════════════════════════════════════════════════════════════

    def save_audio(self):
        if not self.pages:
            messagebox.showinfo("No PDF", "Open a PDF first.")
            return
        out_path = filedialog.asksaveasfilename(
            title="Save audio as…",
            defaultextension=".wav",
            filetypes=[("WAV audio", "*.wav"), ("MP3 audio", "*.mp3"), ("All files", "*.*")],
        )
        if not out_path:
            return
        self.status_var.set("Synthesising…")
        threading.Thread(target=self._save_audio_worker,
                         args=(self.pages, out_path), daemon=True).start()

    def _save_audio_worker(self, pages: list[str], out_path: str):
        # Implement save logic here, similar to original
        pass

    def _find_ffmpeg(self) -> str | None:
        # Implement ffmpeg finding logic
        pass

    def _on_device_change(self, _event=None):
        # Handle device change
        pass

    # ══════════════════════════════════════════════════════════════════════════
    #  Kokoro model
    # ══════════════════════════════════════════════════════════════════════════

    def _ensure_kokoro(self) -> bool:
        # Delegate to TTSHandler
        return self.tts._ensure_kokoro()

    def _synthesise(self, text: str) -> tuple[np.ndarray, int]:
        # Delegate to TTSHandler
        return self.tts._synthesise(text)
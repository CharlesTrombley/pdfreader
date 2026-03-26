# tts.py
import threading
import time
from tkinter import messagebox
import numpy as np
import sounddevice as sd
from kokoro import KPipeline
from config import VOICES
from utils import pause_after

class TTSHandler:
    def __init__(self, root, voice_var, speed_var, device_var, status_var, progress_var, highlight_current):
        self.root = root
        self.voice_var = voice_var
        self.speed_var = speed_var
        self.device_var = device_var
        self.status_var = status_var
        self.progress_var = progress_var
        self._highlight_current = highlight_current
        self.kokoro = None  # dict of KPipeline keyed by lang_code
        self.is_running = False
        self.should_stop = False
        self._pause_evt = threading.Event()
        self._pause_evt.set()
        self.paused = False

    def _ensure_kokoro(self) -> bool:
        """Load KPipeline(s) for the selected device if not already loaded."""
        if self.kokoro is not None:
            return True

        device = self.device_var.get()
        torch_device = {"CPU": "cpu", "CUDA": "cuda"}.get(device, "cpu")

        self.root.after(0, lambda d=device: self.status_var.set(
            f"Loading Kokoro model on {d}… (first load downloads ~350 MB)"))
        try:
            import torch
            if torch_device == "cuda" and not torch.cuda.is_available():
                self.root.after(0, lambda: messagebox.showwarning(
                    "CUDA unavailable",
                    "CUDA was selected but PyTorch cannot find a CUDA-capable GPU.\n\n"
                    "Make sure you installed the GPU version of PyTorch:\n"
                    "  pip install torch --index-url https://download.pytorch.org/whl/cu121\n\n"
                    "Falling back to CPU."
                ))
                torch_device = "cpu"
                self.device_var.set("CPU")

            # KPipeline is created per lang_code — cache both
            self.kokoro = {
                "a": KPipeline(lang_code="a", device=torch_device),
                "b": KPipeline(lang_code="b", device=torch_device),
            }
            self.root.after(0, lambda d=torch_device: self.status_var.set(
                f"Kokoro ready on {d.upper()}."))
        except Exception as exc:
            self.root.after(0, lambda e=exc: messagebox.showerror(
                "Kokoro load error", str(e)))
            return False

        return True

    def _synthesise(self, text: str) -> tuple[np.ndarray, int]:
        """Synthesise text and return (samples_float32, sample_rate)."""
        voice_id, lang_code = VOICES.get(self.voice_var.get(), ("af_heart", "a"))
        speed    = float(self.speed_var.get())
        pipeline = self.kokoro[lang_code]

        chunks = []
        for _, _, audio in pipeline(text, voice=voice_id, speed=speed):
            chunks.append(audio)

        samples = np.concatenate(chunks) if chunks else np.zeros(1, dtype=np.float32)
        return samples, 24000

    def reset_kokoro(self):
        self.kokoro = None

    def synthesise_with_voice(self, text: str, voice_key: str) -> tuple[np.ndarray, int]:
        original_voice = self.voice_var.get()
        self.voice_var.set(voice_key)
        result = self._synthesise(text)
        self.voice_var.set(original_voice)
        return result

    def request_stop(self):
        self.should_stop = True
        self.paused = False
        self._pause_evt.set()
        sd.stop()
        self.status_var.set("Stopped.")

    def toggle_pause(self):
        if not self.is_running:
            return
        self.paused = not self.paused
        if self.paused:
            sd.stop()
            self._pause_evt.clear()
            self.status_var.set("Paused — press Pause to resume")
        else:
            self._pause_evt.set()
            self.status_var.set("Resuming…")
        try:
            if not self._ensure_kokoro():
                return

            voice_id, lang = VOICES.get(self.voice_var.get(), ("af_heart", "a"))
            speed = float(self.speed_var.get())
            total = len(sentences)

            for local_idx in range(start_idx, total):
                if self.should_stop:
                    break
                self._pause_evt.wait()
                if self.should_stop:
                    break

                sentence   = sentences[local_idx]
                global_idx = (global_start + (local_idx - start_idx)
                               if global_start is not None else None)
                n_done  = local_idx - start_idx + 1
                n_total = total - start_idx
                preview = sentence[:70] + ("…" if len(sentence) > 70 else "")

                self.root.after(0, lambda g=global_idx: self._highlight_current(g))
                self.root.after(0, lambda nd=n_done, nt=n_total, p=preview: (
                    self.status_var.set(f"[{nd}/{nt}]  {p}"),
                    self.progress_var.set(f"{int(nd/nt*100)}%" if nt > 0 else ""),
                ))

                try:
                    samples, sample_rate = self._synthesise(sentence)
                except Exception as exc:
                    self.root.after(0, lambda e=exc:
                        messagebox.showerror("TTS Error", str(e)))
                    break

                if self.should_stop:
                    break

                sd.play(samples, samplerate=sample_rate)
                sd.wait()

                gap = pause_after(sentence)
                if gap and not self.should_stop:
                    time.sleep(gap)

                self._pause_evt.wait()

        finally:
            self.is_running = False
            self.root.after(0, self._highlight_current, None)
            self.root.after(0, lambda: (
                self.status_var.set("Stopped." if self.should_stop else "Finished."),
                self.progress_var.set(""),
            ))
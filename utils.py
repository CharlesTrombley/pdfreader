import re

def pdf_to_sentences(raw: str) -> list[str]:
    """Repair PDF line-wrapping and split into natural sentences."""
    text = re.sub(r"-\n(\S)", r"\1", raw)
    text = re.sub(r"\n{2,}", "\x00", text)
    text = re.sub(r"\n", " ", text)
    text = text.replace("\x00", ".  ")
    text = re.sub(r" {2,}", " ", text).strip()
    parts = re.split(r'(?<=[.!?])\s+(?=[A-Z"\(\[])', text)
    return [s.strip() for s in parts if s.strip()]

def pause_after(s: str) -> float:
    """Return a short silence (seconds) appropriate for the sentence ending."""
    s = s.rstrip()
    if not s:           return 0.0
    if s[-1] in ".!?":  return 0.15
    if s[-1] in ",;:":  return 0.05
    return 0.0
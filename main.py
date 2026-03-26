# main.py
import tkinter as tk
from pdf_speech_reader import PDFSpeechReader

if __name__ == "__main__":
    root = tk.Tk()
    app = PDFSpeechReader(root)
    root.mainloop()
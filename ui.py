# ui.py
import tkinter as tk
from tkinter import ttk
from config import P, VOICES
import os

class UIBuilder:
    @staticmethod
    def apply_style(root):
        s = ttk.Style(root)
        s.theme_use("clam")
        s.configure(".",
            background=P["bg2"], foreground=P["fg"],
            fieldbackground=P["bg3"], bordercolor=P["border"],
            troughcolor=P["bg3"], selectbackground=P["accent"],
            selectforeground=P["bg"], font=("Segoe UI", 10),
        )
        s.configure("TFrame",      background=P["bg2"])
        s.configure("Main.TFrame", background=P["bg"])
        s.configure("Side.TFrame", background=P["bg2"])
        s.configure("Accent.TButton",
            background=P["accent"], foreground=P["bg"],
            font=("Segoe UI", 10, "bold"), padding=(14, 6), relief="flat", borderwidth=0,
        )
        s.map("Accent.TButton",
            background=[("active", P["accent2"]), ("disabled", P["border"])],
            foreground=[("disabled", P["fg2"])],
        )
        s.configure("Ghost.TButton",
            background=P["bg3"], foreground=P["fg"],
            font=("Segoe UI", 10), padding=(14, 6), relief="flat", borderwidth=0,
        )
        s.map("Ghost.TButton",
            background=[("active", P["border"]), ("disabled", P["bg2"])],
            foreground=[("disabled", P["fg2"])],
        )
        s.configure("Stop.TButton",
            background="#3a1a1a", foreground="#ff7070",
            font=("Segoe UI", 10, "bold"), padding=(14, 6), relief="flat", borderwidth=0,
        )
        s.map("Stop.TButton", background=[("active", "#4a2020")])
        s.configure("TLabel",
            background=P["bg2"], foreground=P["fg"], font=("Segoe UI", 10))
        s.configure("TCombobox",
            fieldbackground=P["bg3"], background=P["bg3"],
            foreground=P["fg"], arrowcolor=P["fg2"],
            selectbackground=P["bg3"], selectforeground=P["fg"],
        )
        s.map("TCombobox",
            fieldbackground=[("readonly", P["bg3"])],
            selectbackground=[("readonly", P["bg3"])],
            selectforeground=[("readonly", P["fg"])],
        )
        s.configure("Vertical.TScrollbar",
            background=P["bg3"], troughcolor=P["bg2"],
            arrowcolor=P["fg2"], bordercolor=P["border"],
        )
        s.configure("TSeparator", background=P["border"])

    @staticmethod
    def build_ui(root, voice_var, speed_var, threads_var, device_var, status_var, file_var,
                 on_open_pdf, on_read_from_cursor, on_read_selected, on_read_all, on_request_stop, on_toggle_pause, header_extras_callback=None):
        root.columnconfigure(0, weight=0)
        root.columnconfigure(1, weight=1)
        root.columnconfigure(2, weight=0)
        root.rowconfigure(1, weight=1)

        # Header
        hdr = tk.Frame(root, bg=P["bg2"], height=52)
        hdr.grid(row=0, column=0, columnspan=3, sticky="ew")
        hdr.grid_propagate(False)
        tk.Label(hdr, text="◈  PDF Voice Reader",
                 bg=P["bg2"], fg=P["fg"],
                 font=("Segoe UI", 13, "bold")).pack(side="left", padx=20)
        file_lbl = tk.Label(hdr, textvariable=file_var,
                            bg=P["bg2"], fg=P["accent"], font=("Segoe UI", 10))
        file_lbl.pack(side="left", padx=8)
        ttk.Button(hdr, text="Open PDF", command=on_open_pdf,
                   style="Accent.TButton").pack(side="right", padx=16, pady=10)
        if header_extras_callback:
            header_extras_callback(hdr)

        # Sidebar — pages
        side = tk.Frame(root, bg=P["bg2"], width=170)
        side.grid(row=1, column=0, sticky="ns")
        side.grid_propagate(False)
        tk.Label(side, text="PAGES", bg=P["bg2"], fg=P["fg2"],
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=14, pady=(14, 6))
        list_frame = tk.Frame(side, bg=P["bg2"])
        list_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        page_list = tk.Listbox(
            list_frame, bg=P["bg3"], fg=P["fg"], selectbackground=P["accent"],
            selectforeground=P["bg"], font=("Segoe UI", 10),
            borderwidth=0, highlightthickness=0, activestyle="none", relief="flat",
        )
        page_list.pack(side="left", fill="both", expand=True)
        page_scr = ttk.Scrollbar(list_frame, command=page_list.yview)
        page_scr.pack(side="right", fill="y")
        page_list.config(yscrollcommand=page_scr.set)

        # Main text area
        main = tk.Frame(root, bg=P["bg"])
        main.grid(row=1, column=1, sticky="nsew")
        main.rowconfigure(0, weight=1)
        main.columnconfigure(0, weight=1)
        txt_frame = tk.Frame(main, bg=P["bg"])
        txt_frame.grid(row=0, column=0, sticky="nsew")
        txt_frame.rowconfigure(0, weight=1)
        txt_frame.columnconfigure(0, weight=1)
        text = tk.Text(
            txt_frame, bg=P["bg"], fg=P["fg"], font=("Georgia", 12),
            wrap="word", padx=28, pady=20,
            insertbackground=P["accent"], selectbackground="#2a3a5a",
            selectforeground=P["fg"], relief="flat", borderwidth=0,
            highlightthickness=0, cursor="arrow",
        )
        text.grid(row=0, column=0, sticky="nsew")
        txt_scr = ttk.Scrollbar(txt_frame, command=text.yview)
        txt_scr.grid(row=0, column=1, sticky="ns")
        text.config(yscrollcommand=txt_scr.set)
        text.tag_configure("current",     background=P["hl_bg"], foreground=P["hl_fg"])
        text.tag_configure("cursor_sent", background=P["cue_bg"], foreground=P["cue_fg"])
        text.tag_configure("pod_current", background=P["hl_bg"], foreground=P["hl_fg"])
        text.tag_configure("page_sep",    foreground=P["fg2"],
                            font=("Segoe UI", 9), spacing1=12, spacing3=6)
        text.config(state="disabled")

        # Control bar
        ctrl = tk.Frame(root, bg=P["bg2"], height=62)
        ctrl.grid(row=2, column=0, columnspan=2, sticky="ew")
        ctrl.grid_propagate(False)
        pb = tk.Frame(ctrl, bg=P["bg2"])
        pb.pack(side="left", padx=16, pady=10)
        ttk.Button(pb, text="▶  Read from cursor",
                   command=on_read_from_cursor, style="Accent.TButton").pack(side="left", padx=(0, 8))
        ttk.Button(pb, text="Read all",
                   command=on_read_all, style="Ghost.TButton").pack(side="left", padx=(0, 8))
        ttk.Button(pb, text="Read selection",
                   command=on_read_selected, style="Ghost.TButton").pack(side="left", padx=(0, 8))
        tk.Frame(ctrl, bg=P["border"], width=1).pack(side="left", fill="y", pady=12, padx=8)
        ttk.Button(pb, text="⏸  Pause",
                   command=on_toggle_pause, style="Ghost.TButton").pack(side="left", padx=(0, 8))
        ttk.Button(pb, text="■  Stop",
                   command=on_request_stop, style="Stop.TButton").pack(side="left", padx=(0, 8))
        tk.Frame(ctrl, bg=P["border"], width=1).pack(side="left", fill="y", pady=12, padx=8)
        ttk.Button(pb, text="💾  Save audio",
                   command=lambda: None,  # Placeholder, implement in reader
                   style="Ghost.TButton").pack(side="left")
        vp = tk.Frame(ctrl, bg=P["bg2"])
        vp.pack(side="right", padx=16, pady=10)
        tk.Label(vp, text="Voice", bg=P["bg2"], fg=P["fg2"],
                 font=("Segoe UI", 9)).pack(side="left")
        ttk.Combobox(vp, textvariable=voice_var,
                     values=list(VOICES.keys()), width=16, state="readonly"
                     ).pack(side="left", padx=(0, 16))
        tk.Label(vp, text="Speed", bg=P["bg2"], fg=P["fg2"],
                 font=("Segoe UI", 9)).pack(side="left")
        ttk.Combobox(vp, textvariable=speed_var,
                     values=["0.6","0.7","0.8","0.9","1.0","1.1","1.2","1.3","1.5","1.8"],
                     width=6, state="readonly").pack(side="left", padx=(0, 16))
        tk.Label(vp, text="Device", bg=P["bg2"], fg=P["fg2"],
                 font=("Segoe UI", 9)).pack(side="left")
        device_combo = ttk.Combobox(vp, textvariable=device_var,
                     values=["CPU", "CUDA"],
                     width=8, state="readonly")
        device_combo.pack(side="left", padx=(0, 16))
        tk.Label(vp, text="Save threads", bg=P["bg2"], fg=P["fg2"],
                 font=("Segoe UI", 9)).pack(side="left")
        cpu_count = os.cpu_count() or 4
        thread_values = [v for v in [1, 2, 4, 6, 8, 12, 16, 24, 32] if v <= cpu_count * 2]
        ttk.Combobox(vp, textvariable=threads_var,
                     values=thread_values,
                     width=4, state="readonly").pack(side="left", padx=(0, 4))
        tk.Label(vp, text=f"(CPU has {cpu_count} cores)", bg=P["bg2"], fg=P["fg2"],
                 font=("Segoe UI", 8)).pack(side="left")

        # Status bar
        status_bar = tk.Frame(root, bg=P["bg"], height=26)
        status_bar.grid(row=3, column=0, columnspan=3, sticky="ew")
        status_bar.grid_propagate(False)
        tk.Label(status_bar, textvariable=status_var,
                 bg=P["bg"], fg=P["fg2"], font=("Segoe UI", 9),
                 anchor="w").pack(side="left", padx=16)
        progress_var = tk.StringVar(value="")
        tk.Label(status_bar, textvariable=progress_var,
                 bg=P["bg"], fg=P["fg2"], font=("Segoe UI", 9),
                 anchor="e").pack(side="right", padx=16)

        # Return key widgets for reader to use
        return {
            "page_list": page_list,
            "text": text,
            "progress_var": progress_var,
            "device_combo": device_combo,
        }

        # Main text area
        main = tk.Frame(self.root, bg=P["bg"])
        main.grid(row=1, column=1, sticky="nsew")
        main.rowconfigure(0, weight=1)
        main.columnconfigure(0, weight=1)
        txt_frame = tk.Frame(main, bg=P["bg"])
        txt_frame.grid(row=0, column=0, sticky="nsew")
        txt_frame.rowconfigure(0, weight=1)
        txt_frame.columnconfigure(0, weight=1)
        self.text = tk.Text(
            txt_frame, bg=P["bg"], fg=P["fg"], font=("Georgia", 12),
            wrap="word", padx=28, pady=20,
            insertbackground=P["accent"], selectbackground="#2a3a5a",
            selectforeground=P["fg"], relief="flat", borderwidth=0,
            highlightthickness=0, cursor="arrow",
        )
        self.text.grid(row=0, column=0, sticky="nsew")
        txt_scr = ttk.Scrollbar(txt_frame, command=self.text.yview)
        txt_scr.grid(row=0, column=1, sticky="ns")
        self.text.config(yscrollcommand=txt_scr.set)
        self.text.tag_configure("current",     background=P["hl_bg"], foreground=P["hl_fg"])
        self.text.tag_configure("cursor_sent", background=P["cue_bg"], foreground=P["cue_fg"])
        self.text.tag_configure("page_sep",    foreground=P["fg2"],
                                font=("Segoe UI", 9), spacing1=12, spacing3=6)
        self.text.bind("<ButtonRelease-1>", self._on_text_click)
        self.text.config(state="disabled")

        # Control bar
        ctrl = tk.Frame(self.root, bg=P["bg2"], height=62)
        ctrl.grid(row=2, column=0, columnspan=2, sticky="ew")
        ctrl.grid_propagate(False)
        pb = tk.Frame(ctrl, bg=P["bg2"])
        pb.pack(side="left", padx=16, pady=10)
        ttk.Button(pb, text="▶  Read from cursor",
                   command=self.read_from_cursor, style="Accent.TButton").pack(side="left", padx=(0, 8))
        ttk.Button(pb, text="Read all",
                   command=self.read_all, style="Ghost.TButton").pack(side="left", padx=(0, 8))
        ttk.Button(pb, text="Read selection",
                   command=self.read_selected, style="Ghost.TButton").pack(side="left", padx=(0, 8))
        tk.Frame(ctrl, bg=P["border"], width=1).pack(side="left", fill="y", pady=12, padx=8)
        ttk.Button(pb, text="⏸  Pause",
                   command=self.toggle_pause, style="Ghost.TButton").pack(side="left", padx=(0, 8))
        ttk.Button(pb, text="■  Stop",
                   command=self.request_stop, style="Stop.TButton").pack(side="left", padx=(0, 8))
        tk.Frame(ctrl, bg=P["border"], width=1).pack(side="left", fill="y", pady=12, padx=8)
        ttk.Button(pb, text="💾  Save audio",
                   command=self.save_audio, style="Ghost.TButton").pack(side="left")
        vp = tk.Frame(ctrl, bg=P["bg2"])
        vp.pack(side="right", padx=16, pady=10)
        tk.Label(vp, text="Voice", bg=P["bg2"], fg=P["fg2"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 4))
        ttk.Combobox(vp, textvariable=self.voice_var,
                     values=list(VOICES.keys()), width=16, state="readonly"
                     ).pack(side="left", padx=(0, 16))
        tk.Label(vp, text="Speed", bg=P["bg2"], fg=P["fg2"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 4))
        ttk.Combobox(vp, textvariable=self.speed_var,
                     values=["0.6","0.7","0.8","0.9","1.0","1.1","1.2","1.3","1.5","1.8"],
                     width=6, state="readonly").pack(side="left", padx=(0, 16))
        tk.Label(vp, text="Device", bg=P["bg2"], fg=P["fg2"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 4))
        self.device_combo = ttk.Combobox(vp, textvariable=self.device_var,
                     values=["CPU", "CUDA"],
                     width=8, state="readonly")
        self.device_combo.pack(side="left", padx=(0, 16))
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_change)
        tk.Label(vp, text="Save threads", bg=P["bg2"], fg=P["fg2"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 4))
        cpu_count = os.cpu_count() or 4
        thread_values = [v for v in [1, 2, 4, 6, 8, 12, 16, 24, 32] if v <= cpu_count * 2]
        ttk.Combobox(vp, textvariable=self.threads_var,
                     values=thread_values,
                     width=4, state="readonly").pack(side="left", padx=(0, 4))
        tk.Label(vp, text=f"(CPU has {cpu_count} cores)", bg=P["bg2"], fg=P["fg2"],
                 font=("Segoe UI", 8)).pack(side="left")

        # Status bar
        status_bar = tk.Frame(self.root, bg=P["bg"], height=26)
        status_bar.grid(row=3, column=0, columnspan=2, sticky="ew")
        status_bar.grid_propagate(False)
        tk.Label(status_bar, textvariable=self.status_var,
                 bg=P["bg"], fg=P["fg2"], font=("Segoe UI", 9),
                 anchor="w").pack(side="left", padx=16)
        self.progress_var = tk.StringVar(value="")
        tk.Label(status_bar, textvariable=self.progress_var,
                 bg=P["bg"], fg=P["fg2"], font=("Segoe UI", 9),
                 anchor="e").pack(side="right", padx=16)

    def _build_header_extras(self, hdr: tk.Frame):
        """Hook for subclasses to add widgets to the header bar."""
        pass

        pass
#!/usr/bin/env python3
"""Front end for atem2fcp: pick a .drp, adjust the options, preview, convert.

Usage: atem2fcp_gui.py [file.drp]

The options can be saved as the defaults used by the Finder Quick Action
(atem2fcp.py --use-settings).  Standard library only (tkinter).
"""

import argparse
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import atem2fcp  # noqa: E402

AUTO = "Automatic"

# ffprobe is the slow part of an analysis; cache it per file so previews stay quick
_probe = atem2fcp.probe
_probe_cache = {}


def cached_probe(path):
    try:
        st = os.stat(path)
        key = (path, st.st_mtime, st.st_size)
    except OSError:
        return _probe(path)
    if key not in _probe_cache:
        _probe_cache[key] = _probe(path)
    return _probe_cache[key]


atem2fcp.probe = cached_probe


class App:
    def __init__(self, root, drp=None):
        self.root = root
        root.title("ATEM to FCPXML")
        root.minsize(640, 560)
        saved = atem2fcp.load_settings()

        self.drp = tk.StringVar()
        self.output = tk.StringVar()
        self.media_root = tk.StringVar()
        self.program = tk.StringVar()
        self.title = tk.StringVar()
        self.transitions = tk.BooleanVar(value=not saved["no_transitions"])
        self.wavs = tk.BooleanVar(value=not saved["skip_wavs"])
        self.camera_wavs = tk.BooleanVar(value=saved["camera_wavs"])
        self.audio = tk.StringVar(value=saved["audio_angle"] or AUTO)
        self.status = tk.StringVar()
        self.output_is_auto = True
        self.generation = 0
        self.results = queue.Queue()
        self.pending = None

        self._build_ui()
        for var in (self.drp, self.media_root, self.program, self.title,
                    self.transitions, self.wavs, self.camera_wavs, self.audio):
            var.trace_add("write", lambda *_: self.schedule_preview())
        self.drp.trace_add("write", lambda *_: self._drp_changed())
        self.output.trace_add("write", lambda *_: self._output_edited())
        self.wavs.trace_add("write", lambda *_: self._sync_wav_checkbox())
        self._sync_wav_checkbox()
        root.after(100, self._poll)
        if drp:
            self.drp.set(os.path.abspath(drp))
        self.render(None)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        f = ttk.Frame(self.root, padding=14)
        f.grid(sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)
        r = 0

        def path_row(label, var, command, hint=None):
            nonlocal r
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", padx=(0, 10), pady=3)
            entry = ttk.Entry(f, textvariable=var)
            entry.grid(row=r, column=1, sticky="ew", pady=3)
            # keep the file name, not the start of a long path, in view
            var.trace_add("write", lambda *_: entry.after_idle(lambda: entry.xview_moveto(1)))
            entry.bind("<Configure>", lambda e: entry.xview_moveto(1))
            ttk.Button(f, text="Choose…", command=command).grid(row=r, column=2, padx=(8, 0), pady=3)
            r += 1
            if hint:
                ttk.Label(f, text=hint, foreground="gray").grid(row=r, column=1, columnspan=2, sticky="w")
                r += 1

        path_row("ATEM project (.drp)", self.drp, self.choose_drp)
        path_row("Save as", self.output, self.choose_output,
                 "Defaults to the .drp name, next to the .drp")
        ttk.Separator(f).grid(row=r, column=0, columnspan=3, sticky="ew", pady=10); r += 1
        path_row("Media folder", self.media_root, self.choose_media_root,
                 "Optional. Otherwise the .drp's folder, then the ATEM drive in /Volumes")
        path_row("Program recording", self.program, self.choose_program,
                 "Optional. Otherwise '<project> 01.mp4' if it's there")
        ttk.Label(f, text="Project title").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(f, textvariable=self.title).grid(row=r, column=1, columnspan=2, sticky="ew", pady=3); r += 1
        ttk.Label(f, text="Optional. Defaults to the ATEM project name",
                  foreground="gray").grid(row=r, column=1, columnspan=2, sticky="w"); r += 1
        ttk.Separator(f).grid(row=r, column=0, columnspan=3, sticky="ew", pady=10); r += 1

        ttk.Label(f, text="Audio from").grid(row=r, column=0, sticky="w", pady=3)
        self.audio_box = ttk.Combobox(f, textvariable=self.audio, state="readonly", values=[AUTO])
        self.audio_box.grid(row=r, column=1, sticky="w", pady=3); r += 1
        ttk.Checkbutton(f, text="Cross dissolves for ATEM mixes (otherwise cut at the midpoint)",
                        variable=self.transitions).grid(row=r, column=1, columnspan=2, sticky="w", pady=2); r += 1
        ttk.Checkbutton(f, text="Add the Mic and MADI WAVs as audio angles",
                        variable=self.wavs).grid(row=r, column=1, columnspan=2, sticky="w", pady=2); r += 1
        self.cam_wav_check = ttk.Checkbutton(f, text="Also add each camera's WAV",
                                             variable=self.camera_wavs)
        self.cam_wav_check.grid(row=r, column=1, columnspan=2, sticky="w", padx=(20, 0), pady=2); r += 1

        box = ttk.Frame(f)
        box.grid(row=r, column=0, columnspan=3, sticky="nsew", pady=(12, 8))
        f.rowconfigure(r, weight=1)
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.text = tk.Text(box, height=16, wrap="none", font=("Menlo", 11), borderwidth=1,
                            relief="solid", padx=8, pady=6, state="disabled")
        self.text.grid(row=0, column=0, sticky="nsew")
        ys = ttk.Scrollbar(box, orient="vertical", command=self.text.yview)
        ys.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=ys.set)
        self.text.tag_configure("error", foreground="#d0342c")
        self.text.tag_configure("warn", foreground="#b86e00")
        r += 1

        bar = ttk.Frame(f)
        bar.grid(row=r, column=0, columnspan=3, sticky="ew")
        bar.columnconfigure(1, weight=1)
        ttk.Button(bar, text="Save as Finder Defaults", command=self.save_defaults).grid(row=0, column=0)
        ttk.Label(bar, textvariable=self.status, foreground="gray").grid(row=0, column=1, sticky="w", padx=10)
        self.convert_btn = ttk.Button(bar, text="Convert", command=self.convert, default="active")
        self.convert_btn.grid(row=0, column=3)
        self.root.bind("<Return>", lambda e: self.convert())
        self.root.bind("<Command-o>", lambda e: self.choose_drp())

    def _sync_wav_checkbox(self):
        self.cam_wav_check.configure(state="normal" if self.wavs.get() else "disabled")

    def _drp_changed(self):
        if self.output_is_auto:
            drp = self.drp.get().strip()
            self._set_output(atem2fcp.default_output(drp) if drp else "")

    def _set_output(self, value):
        self._setting_output = True
        self.output.set(value)
        self._setting_output = False

    def _output_edited(self):
        if not getattr(self, "_setting_output", False):
            self.output_is_auto = not self.output.get().strip()

    # -------------------------------------------------------------- choosers
    def _initial_dir(self):
        drp = self.drp.get().strip()
        return os.path.dirname(drp) if drp else os.path.expanduser("~")

    def choose_drp(self):
        p = filedialog.askopenfilename(title="Choose an ATEM project", initialdir=self._initial_dir(),
                                       filetypes=[("ATEM project", "*.drp"), ("All files", "*")])
        if p:
            self.drp.set(p)

    def choose_output(self):
        drp = self.drp.get().strip()
        name = os.path.basename(atem2fcp.default_output(drp)) if drp else "timeline.fcpxml"
        p = filedialog.asksaveasfilename(title="Save FCPXML as", initialdir=self._initial_dir(),
                                         initialfile=name, defaultextension=".fcpxml",
                                         filetypes=[("FCPXML", "*.fcpxml")])
        if p:
            self.output_is_auto = False
            self._set_output(p)

    def choose_media_root(self):
        p = filedialog.askdirectory(title="Folder with the ATEM media", initialdir=self._initial_dir())
        if p:
            self.media_root.set(p)

    def choose_program(self):
        p = filedialog.askopenfilename(title="Program recording", initialdir=self._initial_dir(),
                                       filetypes=[("Video", "*.mp4 *.mov"), ("All files", "*")])
        if p:
            self.program.set(p)

    # ------------------------------------------------------------ building
    def args(self):
        audio = self.audio.get()
        return argparse.Namespace(
            drp=self.drp.get().strip(),
            media_root=self.media_root.get().strip() or None,
            program=self.program.get().strip() or None,
            title=self.title.get().strip() or None,
            audio_angle=None if audio.startswith(AUTO) else audio,
            audio_angle_is_preference=True,
            no_transitions=not self.transitions.get(),
            skip_wavs=not self.wavs.get(),
            camera_wavs=self.camera_wavs.get(),
        )

    def schedule_preview(self):
        if self.pending:
            self.root.after_cancel(self.pending)
        self.pending = self.root.after(300, self.preview)

    def preview(self):
        self.pending = None
        if not self.drp.get().strip():
            return
        self._run(write_to=None)

    def convert(self):
        if not self.drp.get().strip():
            self.choose_drp()
            if not self.drp.get().strip():
                return
        out = self.output.get().strip() or atem2fcp.default_output(self.drp.get().strip())
        if os.path.exists(out) and not messagebox.askyesno(
                "Replace file?", f"{os.path.basename(out)} already exists. Replace it?", parent=self.root):
            return
        self._run(write_to=out)

    def _run(self, write_to):
        if write_to and self.pending:
            self.root.after_cancel(self.pending)
            self.pending = None
        self.generation += 1
        gen, args = self.generation, self.args()
        self.status.set("Converting…" if write_to else "Reading media…")
        self.convert_btn.configure(state="disabled")

        def work():
            try:
                xml, summary = atem2fcp.build(args)
                if write_to:
                    with open(write_to, "w", encoding="utf-8") as fh:
                        fh.write(xml)
                self.results.put((gen, write_to, summary, None))
            except Exception as e:  # shown in the window
                self.results.put((gen, write_to, None, e))

        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                gen, written, summary, err = self.results.get_nowait()
                if gen != self.generation and not written:
                    continue  # a stale preview; conversions always report back
                self.convert_btn.configure(state="normal")
                if err:
                    self.status.set("")
                    self.render(None, error=str(err))
                    continue
                if written:
                    self.finished(written, summary)
                    return
                self.update_audio_choices(summary)
                self.render(summary)
                self.status.set("")
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def update_audio_choices(self, summary):
        names = [a.name for a in summary["angles"] if a.media.has_audio]
        current = self.audio.get()
        auto = f"{AUTO} ({summary['audio'].name})" if current.startswith(AUTO) else AUTO
        self.audio_box.configure(values=[auto] + names)
        if current.startswith(AUTO) and current != auto:
            self.audio.set(auto)  # re-triggers a preview; the result is the same, so it settles
        elif not current.startswith(AUTO) and current not in names:
            self.audio.set(AUTO)

    def render(self, summary, written=None, error=None):
        t = self.text
        t.configure(state="normal")
        t.delete("1.0", "end")
        if error:
            t.insert("end", f"Can't convert this file:\n\n{error}\n", "error")
        elif summary is None:
            t.insert("end", "Choose an ATEM .drp file to see what will be converted.\n")
        else:
            for line in atem2fcp.format_summary(summary, written).splitlines():
                tag = "warn" if line.lstrip().startswith(("Warning:", "Note:")) or "[NOT FOUND]" in line else ""
                t.insert("end", line + "\n", tag)
        t.configure(state="disabled")

    # ------------------------------------------------------------ actions
    def save_defaults(self):
        audio = self.audio.get()
        atem2fcp.save_settings({
            "no_transitions": not self.transitions.get(),
            "skip_wavs": not self.wavs.get(),
            "camera_wavs": self.camera_wavs.get(),
            "audio_angle": "" if audio.startswith(AUTO) else audio,
        })
        self.status.set("Saved. The Finder Quick Action will use these options.")

    def finished(self, written, summary):
        """Conversion done: close the window, after showing any warnings."""
        issues = summary["warnings"] + summary["notes"]
        if issues:
            self.root.withdraw()
            messagebox.showwarning(
                "FCPXML created",
                f"{os.path.basename(written)} was created, but:\n\n" + "\n\n".join(issues))
        self.root.destroy()


def bring_to_front(root):
    """Activate this process; launched from Finder, the window otherwise opens behind."""
    root.lift()
    root.focus_force()
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        import ctypes.util
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.restype = ctypes.c_void_p
        send = objc.objc_msgSend
        send.restype = ctypes.c_void_p
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        app = send(objc.objc_getClass(b"NSApplication"), objc.sel_registerName(b"sharedApplication"))
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
        send(app, objc.sel_registerName(b"activateIgnoringOtherApps:"), True)
    except (OSError, AttributeError):
        pass


def main():
    root = tk.Tk()
    App(root, sys.argv[1] if len(sys.argv) > 1 else None)
    root.after(50, lambda: bring_to_front(root))
    root.mainloop()


if __name__ == "__main__":
    main()

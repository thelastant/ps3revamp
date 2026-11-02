#!/usr/bin/env python3
"""
PS3revamp GUI -- Windows front-end.

Pick a decrypted EBOOT.elf, press Convert, wait, press Play. The GUI only
drives ps3revamp.py in a child process, so the pipeline is identical to the
command line and can be cancelled without leaving state behind.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "ps3revamp.py"


class App(tk.Tk):
    def __init__(self, initial: str | None = None):
        super().__init__()
        self.title("PS3revamp  \u2014  PS3 EBOOT to native PC")
        self.geometry("980x720")
        self.minsize(820, 600)

        self.proc: subprocess.Popen | None = None
        self.q: queue.Queue[str] = queue.Queue()
        self.port_dir: Path | None = None
        self.report: dict = {}
        self.started = 0.0

        self._build_style()
        self._build_ui()
        if initial:
            self.elf_var.set(initial)
            self._autofill_from_elf(initial)
        self.after(100, self._drain)

    # ---------------------------------------------------------------- style
    def _build_style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("vista")
        except Exception:
            pass
        st.configure("H1.TLabel", font=("Segoe UI Semibold", 16))
        st.configure("Sub.TLabel", foreground="#666")
        st.configure("Step.TLabel", font=("Segoe UI Semibold", 10))
        st.configure("Go.TButton", font=("Segoe UI Semibold", 12), padding=(18, 10))
        st.configure("Treeview", rowheight=22)

    # ------------------------------------------------------------------- ui
    def _build_ui(self):
        pad = {"padx": 16, "pady": 4}
        head = ttk.Frame(self)
        head.pack(fill="x", pady=(14, 4))
        ttk.Label(head, text="PS3revamp", style="H1.TLabel").pack(anchor="w", padx=16)
        ttk.Label(head,
                  text="Decrypted EBOOT.elf \u2192 native C++ project \u2192 Windows executable",
                  style="Sub.TLabel").pack(anchor="w", padx=16)

        box = ttk.LabelFrame(self, text=" 1  Input ")
        box.pack(fill="x", padx=16, pady=(12, 6))

        ttk.Label(box, text="EBOOT.elf / EBOOT.BIN").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        self.elf_var = tk.StringVar()
        ttk.Entry(box, textvariable=self.elf_var).grid(row=0, column=1, sticky="ew", pady=6)
        ttk.Button(box, text="Browse\u2026", command=self.pick_elf).grid(row=0, column=2, padx=8)

        ttk.Label(box, text="Game folder").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        self.game_var = tk.StringVar()
        ttk.Entry(box, textvariable=self.game_var).grid(row=1, column=1, sticky="ew", pady=6)
        ttk.Button(box, text="Browse\u2026", command=self.pick_game).grid(row=1, column=2, padx=8)

        ttk.Label(box, text="Output folder").grid(row=2, column=0, sticky="w", padx=8, pady=6)
        self.out_var = tk.StringVar()
        ttk.Entry(box, textvariable=self.out_var).grid(row=2, column=1, sticky="ew", pady=6)
        ttk.Button(box, text="Browse\u2026", command=self.pick_out).grid(row=2, column=2, padx=8)
        box.columnconfigure(1, weight=1)

        opt = ttk.LabelFrame(self, text=" 2  Options ")
        opt.pack(fill="x", padx=16, pady=6)
        self.spu_var = tk.BooleanVar(value=True)
        self.ida_var = tk.BooleanVar(value=False)
        self.build_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="Lift embedded SPU code (recommended)",
                        variable=self.spu_var).grid(row=0, column=0, sticky="w", padx=8, pady=4)
        ttk.Checkbutton(opt, text="Build the Windows executable",
                        variable=self.build_var).grid(row=0, column=1, sticky="w", padx=8)
        ttk.Checkbutton(opt, text="Use IDA Pro (idalib) function map",
                        variable=self.ida_var).grid(row=1, column=0, sticky="w", padx=8, pady=4)
        ttk.Label(opt, text="parallel jobs").grid(row=1, column=1, sticky="e", padx=(8, 4))
        self.jobs_var = tk.IntVar(value=max(1, (os.cpu_count() or 4)))
        ttk.Spinbox(opt, from_=1, to=64, width=5,
                    textvariable=self.jobs_var).grid(row=1, column=2, sticky="w", padx=4)

        act = ttk.Frame(self)
        act.pack(fill="x", padx=16, pady=(6, 2))
        self.go = ttk.Button(act, text="Convert to PC", style="Go.TButton",
                             command=self.start)
        self.go.pack(side="left")
        self.stop = ttk.Button(act, text="Cancel", command=self.cancel, state="disabled")
        self.stop.pack(side="left", padx=8)
        self.play = ttk.Button(act, text="\u25b6  Play", command=self.play_port, state="disabled")
        self.play.pack(side="right")
        self.open_btn = ttk.Button(act, text="Open folder", command=self.open_folder,
                                   state="disabled")
        self.open_btn.pack(side="right", padx=8)

        self.pb = ttk.Progressbar(self, mode="indeterminate")
        self.pb.pack(fill="x", padx=16, pady=(6, 2))
        self.status = ttk.Label(self, text="Ready.", style="Sub.TLabel")
        self.status.pack(anchor="w", padx=16)

        logf = ttk.LabelFrame(self, text=" 3  Log ")
        logf.pack(fill="both", expand=True, padx=16, pady=(6, 14))
        self.log = tk.Text(logf, wrap="none", font=("Consolas", 9),
                           bg="#111418", fg="#d6dde6", insertbackground="#d6dde6",
                           relief="flat")
        ys = ttk.Scrollbar(logf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=ys.set)
        self.log.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")

    # --------------------------------------------------------------- pickers
    def pick_elf(self):
        p = filedialog.askopenfilename(
            title="Select decrypted EBOOT.elf (or EBOOT.BIN)",
            filetypes=[("PS3 executable", "*.elf *.ELF *.bin *.BIN"), ("All files", "*.*")])
        if p:
            self.elf_var.set(p)
            self._autofill_from_elf(p)

    def pick_game(self):
        p = filedialog.askdirectory(title="Select the game folder (contains USRDIR)")
        if p:
            self.game_var.set(p)

    def pick_out(self):
        p = filedialog.askdirectory(title="Select output folder")
        if p:
            self.out_var.set(p)

    def _autofill_from_elf(self, p: str):
        elf = Path(p)
        for up in (elf.parent, elf.parent.parent, elf.parent.parent.parent):
            if (up / "PARAM.SFO").is_file() or (up / "USRDIR").is_dir():
                self.game_var.set(str(up))
                break
        if not self.game_var.get():
            self.game_var.set(str(elf.parent))
        if not self.out_var.get():
            self.out_var.set(str(HERE / "ports"))

    # --------------------------------------------------------------- convert
    def start(self):
        elf = self.elf_var.get().strip().strip('"')
        if not elf or not Path(elf).is_file():
            messagebox.showerror("PS3revamp", "Choose a decrypted EBOOT.elf first.")
            return
        if self.build_var.get() and not self.out_var.get():
            messagebox.showerror("PS3revamp", "Choose an output folder.")
            return

        env = os.environ.copy()
        # keep the GUI's interpreter for the pipeline deps
        env.setdefault("PYTHONUTF8", "1")
        cmd = [sys.executable, str(SCRIPT), "port", elf,
               "--analyzer", "ida" if self.ida_var.get() else "auto",
               "--jobs", str(self.jobs_var.get())]
        if self.game_var.get():
            cmd += ["--game", self.game_var.get().strip().strip('"')]
        if self.out_var.get():
            cmd += ["-o", self.out_var.get().strip().strip('"')]
        if self.spu_var.get():
            cmd += ["--spu"]
        if self.build_var.get():
            cmd += ["--build", "--test", "20", "--package"]

        self.log.delete("1.0", "end")
        self._say(" ".join(cmd) + "\n")
        self.play.configure(state="disabled")
        self.open_btn.configure(state="disabled")
        self.go.configure(state="disabled")
        self.stop.configure(state="normal")
        self.pb.start(12)
        self.started = time.time()
        self.status.configure(text="Working\u2026 first build takes the longest.")
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            bufsize=1, encoding="utf-8", errors="replace", env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc: subprocess.Popen):
        assert proc.stdout
        for line in proc.stdout:
            self.q.put(line.rstrip("\n"))
        proc.wait()
        self.q.put(f"__EXIT__{proc.returncode}")

    def cancel(self):
        if self.proc and self.proc.poll() is None:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                               capture_output=True)
            except Exception:
                self.proc.kill()
            self._say("\n[cancelled]")

    # ----------------------------------------------------------------- event
    def _drain(self):
        try:
            while True:
                line = self.q.get_nowait()
                if line.startswith("__EXIT__"):
                    self._finished(int(line.split("__EXIT__")[1]))
                    continue
                self._say(line + "\n")
        except queue.Empty:
            pass
        self.after(80, self._drain)

    def _say(self, s: str):
        self.log.insert("end", s)
        self.log.see("end")

    def _finished(self, rc: int):
        self.pb.stop()
        self.stop.configure(state="disabled")
        self.go.configure(state="normal")
        took = time.time() - self.started
        if rc != 0:
            self.status.configure(text=f"Failed (exit {rc}) after {took:.0f}s \u2014 see log.")
            return
        # find the port just written
        base = Path(self.out_var.get() or (HERE / "ports"))
        cands = sorted(base.glob("*/port.json"), key=lambda p: p.stat().st_mtime)
        if cands:
            self.port_dir = cands[-1].parent
            self.report = json.loads(cands[-1].read_text(encoding="utf-8"))
        exe = self.report.get("exe")
        self.open_btn.configure(state="normal")
        if exe and Path(exe).is_file():
            self.play.configure(state="normal")
            self.status.configure(
                text=f"Done in {took:.0f}s \u2014 native build ready. Press Play.")
        else:
            self.status.configure(
                text=f"Code generated in {took:.0f}s. Build it with: ps3revamp build "
                     f"\"{self.port_dir}\"")

    def play_port(self):
        exe = self.report.get("exe")
        elf = self.report.get("elf")
        if not exe or not elf:
            return
        # Prefer the packaged game layout (dist/<Title>.exe + PS3_GAME).
        if self.port_dir:
            man = self.port_dir / "dist" / "ps3revamp.game.json"
            if man.is_file():
                try:
                    g = json.loads(man.read_text(encoding="utf-8"))
                    if g.get("exe") and Path(g["exe"]).is_file():
                        exe = g["exe"]
                        elf = str(Path(g["exe"]).parent / "PS3_GAME" / "USRDIR" / "EBOOT.elf")
                except Exception:
                    pass
        env = os.environ.copy()
        if self.report.get("game_dir"):
            env["PS3_VFS_ROOT"] = self.report["game_dir"]
        t = self.report.get("title") or "ps3revamp"
        tid = (self.report.get("sfo") or {}).get("TITLE_ID")
        t = (f"{t} [{tid}]" if tid else t).encode("ascii", "ignore").decode().strip()
        env["PS3_TITLE"] = t or "ps3revamp"
        subprocess.Popen([exe, elf], cwd=str(Path(exe).parent), env=env)

    def open_folder(self):
        if self.port_dir:
            os.startfile(str(self.port_dir))


def main():
    initial = None
    for a in sys.argv[1:]:
        if a.lower().endswith((".elf", ".bin", ".self")) and Path(a).is_file():
            initial = a
            break
    App(initial).mainloop()


if __name__ == "__main__":
    main()


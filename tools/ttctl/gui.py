"""ttctl — Tkinter desktop GUI launcher for the train-ticket cluster.

A standalone window with three tabs (Boot / Pods / Repair) over the shared,
frontend-agnostic backend (cluster.py / model.py / boot.py).

Threading: Tk is single-threaded and every cluster.* call blocks on a subprocess,
so all work runs in daemon threads that post UI updates onto a queue drained by
`root.after`. Nothing touches a widget off the main thread.
"""

from __future__ import annotations

import asyncio
import queue
import subprocess
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from . import boot, cluster, model

# colour palette (GitHub-ish dark)
COLORS = {
    model.READY: "#2ea043",
    model.PENDING: "#d29922",
    model.CRASH: "#da3633",
    model.UNKNOWN: "#6e7681",
}
BG = "#0d1117"
FG = "#c9d1d9"


# --- helpers shared by frames -------------------------------------------------

def _deploy_of(pod_name: str) -> str:
    """Strip the replicaset/pod hash suffix to recover the deployment name."""
    parts = pod_name.rsplit("-", 2)
    return parts[0] if len(parts) == 3 else pod_name


def _row_order(pods: list[dict]) -> list[tuple[str, list[dict]]]:
    """Group pods into INFRA + the 6 batches, in display order."""
    by_name = {p["name"]: p for p in pods}
    rows: list[tuple[str, list[dict]]] = []

    infra = [by_name[n] for n in sorted(by_name)
             if any(n.startswith(p) for p in model.INFRA_PREFIXES)]
    rows.append(("INFRA", infra))
    for i, (_title, services) in enumerate(model.BATCHES, start=1):
        members = [by_name[n] for n in sorted(by_name)
                   if _deploy_of(n) in services]
        rows.append((f"BATCH{i}", members))

    classified = {p["name"] for _, ps in rows for p in ps}
    leftovers = [p for p in pods if p["name"] not in classified]
    if leftovers:
        rows.append(("OTHER", leftovers))
    return rows


class _LogPane:
    """A read-only scrolling Text with colour tags + thread-safe append."""

    def __init__(self, master, height=12):
        frame = ttk.Frame(master)
        self.frame = frame
        self.text = tk.Text(frame, height=height, bg=BG, fg=FG, wrap="word",
                            state="disabled", relief="flat", insertbackground=FG)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.text.tag_config("info", foreground=FG)
        self.text.tag_config("ok", foreground="#3fb950")
        self.text.tag_config("warn", foreground="#d29922")
        self.text.tag_config("error", foreground="#f85149")

    def pack(self, **kw):
        self.frame.pack(**kw)

    def write(self, line: str, tag: str = "info"):
        self.text.configure(state="normal")
        self.text.insert("end", line + "\n", tag)
        self.text.see("end")
        self.text.configure(state="disabled")

    def clear(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")


# =============================================================================
# Boot tab
# =============================================================================
STAGE_LABELS = {
    "minikube": "Cluster",
    "install": "Helm install",
    "deploy": "Deploy / images",
    "harden": "Harden",
    "park": "Park",
    "infra": "Infra",
    "wave-boot": "Wave-boot",
    "consign": "Consign fix",
    "verdict": "Verdict",
}
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class BootFrame(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=10)
        self.app = app
        self._loop = None
        self._task = None
        self._running = False
        self._start_ts = None
        self._active_stage = None
        self._spin_i = 0

        # header: spinner + headline + elapsed
        head = ttk.Frame(self)
        head.pack(fill="x")
        self.spinner = ttk.Label(head, text=" ", width=2,
                                font=("TkDefaultFont", 12, "bold"))
        self.spinner.pack(side="left")
        self.headline = ttk.Label(head, text="Idle — click Smart Start.",
                                 font=("TkDefaultFont", 11, "bold"))
        self.headline.pack(side="left", padx=(2, 0))
        self.elapsed = ttk.Label(head, text="")
        self.elapsed.pack(side="right")

        # per-stage progress rows
        panel = ttk.Frame(self)
        panel.pack(fill="x", pady=(8, 4))
        self.rows: dict[str, dict] = {}
        for r, st in enumerate(boot.STAGES):
            ttk.Label(panel, text=STAGE_LABELS.get(st, st), width=16,
                     anchor="w").grid(row=r, column=0, sticky="w", pady=1)
            bar = ttk.Progressbar(panel, length=240, maximum=100,
                                 mode="determinate")
            bar.grid(row=r, column=1, sticky="w", padx=6)
            mark = ttk.Label(panel, text="·", width=2)
            mark.grid(row=r, column=2)
            detail = ttk.Label(panel, text="", foreground="#8b949e", anchor="w")
            detail.grid(row=r, column=3, sticky="we", padx=(4, 0))
            self.rows[st] = {"bar": bar, "mark": mark, "detail": detail,
                            "pulsing": False}
        panel.columnconfigure(3, weight=1)

        self.log = _LogPane(self, height=12)
        self.log.pack(fill="both", expand=True, pady=6)

        ctl = ttk.Frame(self)
        ctl.pack(fill="x")
        self.btn_start = ttk.Button(ctl, text="▶  Smart Start",
                                   command=self.smart_start)
        self.btn_stop = ttk.Button(ctl, text="Stop", command=self.stop_boot,
                                   state="disabled")
        self.btn_ui = ttk.Button(ctl, text="Open UI", command=self.open_ui)
        for b in (self.btn_start, self.btn_stop, self.btn_ui):
            b.pack(side="left", padx=(0, 6))
        self.btn_clean = tk.Button(ctl, text="Master Cleanup", bg="#da3633",
                                  fg="white", activebackground="#b62324",
                                  activeforeground="white", command=self.master_cleanup)
        self.btn_clean.pack(side="right")

        # show a diagnosis on mount so the user knows what Smart Start will do
        self.app.submit(self._safe_diagnose, then=self._show_diagnosis)

    # --- diagnosis hint -------------------------------------------------------
    @staticmethod
    def _safe_diagnose():
        return cluster.diagnose()

    def _show_diagnosis(self, st):
        if isinstance(st, Exception):
            self.headline["text"] = f"Cluster unreachable: {st}"
            return
        self.headline["text"] = f"Smart Start will: {st.summary}"
        self.log.write(f"diagnosis → {st.action.value}: {st.summary}")

    # --- run ------------------------------------------------------------------
    def smart_start(self):
        if self._running:
            return
        # Fresh machine / deleted cluster → let the user confirm/adjust how much
        # of THIS host to allocate (auto-recommended, editable).
        if cluster.docker_state("minikube") is None:
            host = cluster.host_resources()
            cpus, mem = cluster.recommend_resources()
            chosen = ask_resources(self.app.root, cpus, mem, host)
            if chosen is None:
                return
            cluster.RESOURCE_OVERRIDE["cpus"] = chosen[0]
            cluster.RESOURCE_OVERRIDE["memory_gb"] = chosen[1]
        self.log.clear()
        self._reset_rows()
        self._running = True
        self._start_ts = time.monotonic()
        self._active_stage = None
        self.btn_start["state"] = "disabled"
        self.btn_clean["state"] = "disabled"
        self.btn_stop["state"] = "normal"
        self.headline["text"] = "Starting…"
        self._tick_elapsed()
        self._tick_spinner()
        threading.Thread(target=self._boot_thread, daemon=True).start()

    def _reset_rows(self):
        for st, w in self.rows.items():
            w["bar"].stop()
            w["bar"].config(mode="determinate", value=0)
            w["mark"]["text"] = "·"
            w["detail"]["text"] = ""
            w["pulsing"] = False

    def _boot_thread(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        controller = boot.BootController(self._emit)
        task = loop.create_task(controller.run_smart())
        self._task = task
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.app.post(lambda: self.log.write(f"aborted: {e}", "error"))
        finally:
            loop.close()
            self._loop = None
            self._task = None
            self.app.post(self._boot_done)

    def _emit(self, p: boot.Progress):
        self.app.post(lambda: self._apply_progress(p))

    def _apply_progress(self, p: boot.Progress):
        self._active_stage = p.phase
        # mark completed stages
        for st in p.done_phases:
            w = self.rows.get(st)
            if w:
                w["bar"].stop()
                w["bar"].config(mode="determinate", value=100)
                w["mark"]["text"] = "✓"
                w["pulsing"] = False
        # active stage bar
        w = self.rows.get(p.phase)
        if w and p.phase not in p.done_phases:
            w["mark"]["text"] = "▶"
            if p.fraction is not None:
                if w["pulsing"]:
                    w["bar"].stop()
                    w["bar"].config(mode="determinate")
                    w["pulsing"] = False
                w["bar"]["value"] = max(0, min(100, p.fraction * 100))
                w["detail"]["text"] = self._trim(p.message)
            else:
                if not w["pulsing"]:
                    w["bar"].config(mode="indeterminate")
                    w["bar"].start(60)
                    w["pulsing"] = True
                w["detail"]["text"] = self._trim(p.message)
        if p.phase in STAGE_LABELS:
            self.headline["text"] = f"{STAGE_LABELS[p.phase]}: {self._trim(p.message, 60)}"
        ts = time.strftime("%H:%M:%S")
        self.log.write(f"{ts} [{p.phase}] {p.message}", p.level)

    @staticmethod
    def _trim(s, n=44):
        s = s.strip()
        return s if len(s) <= n else s[: n - 1] + "…"

    def _boot_done(self):
        self._running = False
        for w in self.rows.values():
            if w["pulsing"]:
                w["bar"].stop()
                w["bar"].config(mode="determinate", value=0)
                w["pulsing"] = False
        self.spinner["text"] = " "
        self.btn_start["state"] = "normal"
        self.btn_clean["state"] = "normal"
        self.btn_stop["state"] = "disabled"
        self.headline["text"] = "Done."

    def stop_boot(self):
        loop, task = self._loop, self._task
        if loop and task:
            loop.call_soon_threadsafe(task.cancel)
        self.btn_stop["state"] = "disabled"

    def _tick_elapsed(self):
        if not self._running or self._start_ts is None:
            return
        secs = int(time.monotonic() - self._start_ts)
        self.elapsed["text"] = f"elapsed {secs // 60:02d}:{secs % 60:02d}"
        self.app.root.after(1000, self._tick_elapsed)

    def _tick_spinner(self):
        # animates independently of backend events → always-visible motion
        if not self._running:
            self.spinner["text"] = " "
            return
        self._spin_i = (self._spin_i + 1) % len(_SPINNER)
        self.spinner["text"] = _SPINNER[self._spin_i]
        self.app.root.after(120, self._tick_spinner)

    # --- master cleanup -------------------------------------------------------
    def master_cleanup(self):
        if self._running:
            return
        if not messagebox.askyesno(
            "Master Cleanup — DESTRUCTIVE",
            "Wipe EVERYTHING back to a fresh start?\n\n"
            "Runs: minikube delete  →  docker rm -f minikube  →  docker image "
            "prune -af.\n\nThe whole cluster and all downloaded images are removed; "
            "the next Smart Start rebuilds from scratch (slow — re-downloads "
            "images). Continue?",
            icon="warning", default="no",
        ):
            return
        self._running = True
        self.btn_start["state"] = "disabled"
        self.btn_clean["state"] = "disabled"
        self.btn_stop["state"] = "disabled"
        self._start_ts = time.monotonic()
        self._tick_elapsed()
        self._tick_spinner()
        self.log.clear()
        self.headline["text"] = "Master Cleanup running…"
        threading.Thread(target=self._cleanup_thread, daemon=True).start()

    def _cleanup_thread(self):
        def log(line, tag="info"):
            self.app.post(lambda: self.log.write(line, tag))
        log("$ minikube delete", "warn")
        cluster.stream_run(["minikube", "delete"],
                           lambda ln: log("  " + ln) if ln.strip() else None,
                           timeout=300)
        if cluster.docker_state("minikube") is not None:
            log("$ docker rm -f minikube")
            cluster.docker_rm("minikube")
        log("$ docker image prune -af", "warn")
        cluster.stream_run(["docker", "image", "prune", "-af"],
                           lambda ln: log("  " + ln) if ln.strip() else None,
                           timeout=300)
        log("✓ cleanup complete — Smart Start will rebuild from scratch.", "ok")
        self.app.post(self._boot_done)

    def open_ui(self):
        def then(ip):
            if isinstance(ip, Exception) or not ip:
                self.log.write("minikube IP unknown.", "warn")
                return
            url = f"http://{ip}:{model.UI_NODEPORT}"
            self.log.write(f"opening {url}", "ok")
            try:
                webbrowser.open(url)
            except Exception:
                pass
        self.app.submit(cluster.minikube_ip, then=then)


# =============================================================================
# Pods tab
# =============================================================================
class PodsFrame(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=10)
        self.app = app
        self.selected: str | None = None
        self.buttons: dict[str, tk.Button] = {}
        self._names: set[str] = set()
        self._pods: dict[str, dict] = {}
        self._log_proc: subprocess.Popen | None = None

        self.summary = ttk.Label(self, text="loading pods…")
        self.summary.pack(anchor="w")

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, pady=4)
        self.canvas = tk.Canvas(wrap, highlightthickness=0, bg=BG)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        ttk.Label(
            self,
            text="legend:  ● green=ready   ◐ yellow=pending   ● red=crash/error",
            foreground="#8b949e",
        ).pack(anchor="w")

        self.sel = ttk.Label(self, text="selected: (none)", foreground="#d29922")
        self.sel.pack(anchor="w", pady=(6, 2))

        act = ttk.Frame(self)
        act.pack(anchor="w")
        self.btn_logs = ttk.Button(act, text="View logs", command=self.view_logs,
                                  state="disabled")
        self.btn_restart = ttk.Button(act, text="Restart pod", command=self.restart,
                                     state="disabled")
        self.btn_desc = ttk.Button(act, text="Describe", command=self.describe,
                                  state="disabled")
        for b in (self.btn_logs, self.btn_restart, self.btn_desc):
            b.pack(side="left", padx=(0, 6))

        self.out = _LogPane(self, height=11)
        self.out.pack(fill="both", expand=True, pady=(6, 0))

        self.refresh()
        self._schedule()

    def _schedule(self):
        self.app.root.after(4000, self._auto)

    def _auto(self):
        self.refresh()
        self._schedule()

    def refresh(self):
        self.app.submit(cluster.list_pods, then=self._apply)

    def _apply(self, pods):
        if isinstance(pods, Exception):
            self.summary["text"] = f"cluster unavailable: {pods}"
            return
        by_name = {p["name"]: p for p in pods}
        counts = {model.READY: 0, model.PENDING: 0, model.CRASH: 0, model.UNKNOWN: 0}
        for p in pods:
            counts[p["state"]] = counts.get(p["state"], 0) + 1
        self.summary["text"] = (
            f"● {counts[model.READY]} ready     ◐ {counts[model.PENDING]} pending     "
            f"● {counts[model.CRASH]} crash     ○ {counts[model.UNKNOWN]} unknown"
        )
        names = set(by_name)
        if names != self._names:
            self._rebuild(pods)
            self._names = names
        else:
            for p in pods:
                b = self.buttons.get(p["name"])
                if b is not None:
                    self._style(b, p)
        self._pods = by_name
        if self.selected:
            self._render_selected(by_name.get(self.selected))

    def _rebuild(self, pods):
        for w in self.inner.winfo_children():
            w.destroy()
        self.buttons.clear()
        for label, members in _row_order(pods):
            if not members:
                continue
            row = ttk.Frame(self.inner)
            row.pack(fill="x", anchor="w", pady=1)
            tk.Label(row, text=label, width=8, anchor="w", bg=BG,
                    fg="#58a6ff").pack(side="left")
            for pod in members:
                b = tk.Button(row, text="", width=15, anchor="w", relief="raised",
                             borderwidth=1,
                             command=lambda n=pod["name"]: self.select(n))
                self._style(b, pod)
                b.pack(side="left", padx=2, pady=1)
                self.buttons[pod["name"]] = b

    def _style(self, b: tk.Button, pod: dict):
        color = COLORS[pod["state"]]
        deploy = _deploy_of(pod["name"])
        short = model.short_name(deploy) if deploy.startswith("ts-") else pod["name"]
        if deploy in model.KNOWN_BUGS:
            short = short.upper()
        glyph = model.STATUS_GLYPH[pod["state"]]
        b.config(text=f"{glyph} {short}", bg=color, fg="white",
                 activebackground=color, activeforeground="white")

    # --- selection + actions --------------------------------------------------
    def select(self, name: str):
        self.selected = name
        self._render_selected(self._pods.get(name))
        for b in (self.btn_logs, self.btn_restart, self.btn_desc):
            b["state"] = "normal"

    def _render_selected(self, pod: dict | None):
        if not pod:
            self.sel["text"] = "selected: (none)"
            return
        deploy = _deploy_of(pod["name"])
        line = (f"selected: {pod['name']}   {pod['ready']}/{pod['total']}  "
                f"{pod['phase']}/{pod['status']}  restarts={pod['restarts']}")
        if pod.get("last_terminated_reason"):
            line += f"  last={pod['last_terminated_reason']}"
        bug = model.KNOWN_BUGS.get(deploy)
        if bug:
            line += f"   ⚠ {bug}"
        self.sel["text"] = line

    def view_logs(self):
        if not self.selected:
            return
        self._kill_log()
        name = self.selected
        self.out.write(f"$ kubectl logs -f {name} --tail=200", "ok")
        cmd = cluster.logs_cmd(name, tail=200, follow=True)

        def run():
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True)
            except Exception as e:
                self.app.post(lambda: self.out.write(f"log error: {e}", "error"))
                return
            self._log_proc = proc
            assert proc.stdout is not None
            for line in proc.stdout:
                self.app.post(lambda ln=line.rstrip("\n"): self.out.write(ln))

        threading.Thread(target=run, daemon=True).start()

    def _kill_log(self):
        p = self._log_proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
        self._log_proc = None

    def restart(self):
        name = self.selected
        if not name:
            return
        if model.is_quorum(name):
            self.out.write(
                f"refused: {name} is a quorum StatefulSet member — won't delete "
                "it from here (risks quorum loss).", "warn")
            return
        self.out.write(f"$ kubectl delete pod {name}", "ok")
        self.out.write("⏳ deleting pod… it will be recreated by its deployment.")

        def done(r):
            self.out.write(self._fmt(r))
            self.out.write(f"✓ {name} deleted — watch it cycle yellow→green above.", "ok")
        self.app.submit(lambda: cluster.delete_pod(name), then=done)

    def describe(self):
        name = self.selected
        if not name:
            return
        self.out.write(f"$ kubectl describe pod {name}", "ok")
        self.app.submit(lambda: cluster.describe(name),
                        then=lambda r: self.out.write(r if isinstance(r, str) else str(r)))

    @staticmethod
    def _fmt(r) -> str:
        if isinstance(r, tuple) and len(r) == 3:
            rc, out, err = r
            return (out or err or f"rc={rc}").strip()
        return str(r)


# =============================================================================
# Repair tab
# =============================================================================
class RepairFrame(ttk.Frame):
    ACTIONS = [
        ("Fix consign-price (delete idx=0 + restart)", "_fix_consign"),
        ("Re-apply nacos armor (512m heap)", "_nacos_armor"),
        ("Re-assert CPU cap (nproc-4)", "_cpu_cap"),
        ("Re-sign service memory leases", "_sign_leases"),
        ("Park all ts-* (scale 0)", "_park"),
        ("Batch-restart all (re-register)", "_batch_restart"),
        ("Check MySQL leader election", "_mysql_status"),
        ("Fix MySQL election (re-elect leader)", "_fix_mysql_election"),
        ("Detect OOMKilled pods", "_detect_oom"),
    ]

    def __init__(self, master, app):
        super().__init__(master, padding=10)
        self.app = app

        self.banner = tk.Label(self, text="running diagnostics…", anchor="w",
                              bg=BG, fg=FG, padx=4, pady=4)
        self.banner.pack(fill="x")
        ttk.Label(
            self,
            text="Each action runs the documented kubectl/sql commands and streams "
                 "output below.", foreground="#8b949e",
        ).pack(anchor="w", pady=(2, 6))

        grid = ttk.Frame(self)
        grid.pack(fill="x")
        for i, (label, meth) in enumerate(self.ACTIONS):
            b = ttk.Button(grid, text=label,
                          command=lambda m=meth: self._spawn(getattr(self, m)))
            b.grid(row=i // 2, column=i % 2, sticky="ew", padx=4, pady=3)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        self.out = _LogPane(self, height=14)
        self.out.pack(fill="both", expand=True, pady=(8, 0))

        self._diag()
        self._schedule()

    # --- infra for actions ----------------------------------------------------
    def _spawn(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    def _log(self, text: str, tag: str = "info"):
        self.app.post(lambda: self.out.write(text, tag))

    # --- diagnostics banner ---------------------------------------------------
    def _schedule(self):
        self.app.root.after(8000, self._auto)

    def _auto(self):
        self._diag()
        self._schedule()

    def _diag(self):
        self.app.submit(self._diag_fetch, then=self._diag_apply)

    @staticmethod
    def _diag_fetch():
        pods = cluster.list_pods()
        problems = []
        oom = cluster.oomkilled(pods)
        if oom:
            problems.append("⚠ %d OOMKilled: %s" % (
                len(oom), ", ".join(p["name"] for p in oom[:3])))
        for p in pods:
            if p["state"] == model.CRASH and _deploy_of(p["name"]) in model.KNOWN_BUGS:
                problems.append(f"⚠ {p['name']} crashing — try Fix consign-price")
                break
        if any(p["name"].startswith("nacos-") and p["state"] != model.READY
               for p in pods):
            problems.append("⚠ a nacos member not ready — registry may split")
        return problems

    def _diag_apply(self, problems):
        if isinstance(problems, Exception):
            self.banner.config(text=f"cluster unavailable: {problems}", fg="#f85149")
        elif not problems:
            self.banner.config(text="no problems detected ✔", fg="#3fb950")
        else:
            self.banner.config(text="   ".join(problems), fg="#d29922")

    # --- actions (each runs in its own thread) --------------------------------
    def _fix_consign(self):
        self._log("\n— Fix consign-price —", "ok")
        leader = cluster.mysql_leader()
        if not leader:
            self._log("could not find tsdb-mysql leader.", "error")
            return
        self._log(f"DELETE FROM consign_price WHERE idx=0;  (on {leader})")
        rc, out, err = cluster.exec_sql(leader, "DELETE FROM consign_price WHERE idx=0;")
        self._log((out or err or f"rc={rc}").strip())
        cluster.delete_pods_by_label("app=ts-consign-price-service")
        self._log("waiting for availability (up to 5 min)...")
        ok = cluster.wait_available("ts-consign-price-service", 300)
        self._log("recovered (1/1)." if ok else "still down — check logs.",
                  "ok" if ok else "error")

    def _nacos_armor(self):
        self._log("\n— Re-apply nacos armor —", "ok")
        for label, (rc, out, err) in cluster.nacos_armor():
            self._log(f"  {label}: " + ("ok" if rc == 0 else f"FAILED {(err or out).strip()}"),
                      "info" if rc == 0 else "error")
        self._log("rollout restarts nacos members one at a time.")

    def _cpu_cap(self):
        cap = max(1, cluster.nproc() - 4)
        self._log(f"\n— CPU cap —\n$ docker update --cpus={cap} minikube", "ok")
        rc, out, err = cluster.set_cpu_cap()
        self._log("ok" if rc == 0 else (err or out).strip(),
                  "ok" if rc == 0 else "error")

    def _sign_leases(self):
        self._log("\n— Re-sign memory leases (all ts-*, ~1 min) —", "ok")
        results = cluster.sign_memory_leases()
        bad = [d for d, ok in results if not ok]
        self._log(f"done — {len(results) - len(bad)}/{len(results)} ok.",
                  "ok" if not bad else "warn")
        if bad:
            self._log("failed: " + ", ".join(bad), "warn")

    def _park(self):
        self._log("\n— Park all —", "ok")
        cluster.park_all()
        self._log("parked.", "ok")

    def _batch_restart(self):
        self._log("\n— Batch-restart all (re-register) —", "ok")
        for i, (title, services) in enumerate(model.BATCHES, start=1):
            for d in services:
                cluster.scale(d, 1)
            self._log(f"  batch {i}/6 ({title}) scaled to 1.")
        self._log("all batches re-released. Watch the Pods tab.", "ok")

    def _mysql_status(self):
        self._log("\n— MySQL election + read/write (both DBs) —", "ok")
        for sts in cluster.MYSQL_DBS:
            st = cluster.mysql_cluster_status(sts)
            ep = cluster.db_has_leader(sts)
            if not st["ok"]:
                self._log(f"{sts}: status unavailable ({st.get('error','')})", "error")
                continue
            for n in st["nodes"]:
                self._log(f"  {n['name']:18} raft={n['raft']:9} {n['rw']}")
            verdict = (f"{sts}: leaders={st['leaders']} readwrite={st['readwrite']} "
                       f"leader-endpoint={'present' if ep else 'EMPTY'}")
            if st["healthy"] and ep:
                self._log("  ✓ " + verdict + "  → healthy", "ok")
            elif st["all_followers"]:
                self._log("  ✗ " + verdict + "  → ALL FOLLOWERS, no leader!", "error")
            else:
                self._log("  ⚠ " + verdict, "warn")

    def _fix_mysql_election(self):
        self._log("\n— Fix MySQL election (both DBs) —", "ok")
        need = cluster.dbs_needing_leader()
        if not need:
            self._log("both tsdb-mysql and nacosdb-mysql have leaders — nothing to do.",
                      "ok")
            return
        self._log(f"no leader on: {', '.join(need)}. ⚠ restarting MySQL is heavy — "
                  "best with services parked. Proceeding…", "warn")
        any_fail = False
        for sts in need:
            ok = cluster.fix_mysql_election(sts, lambda ln: self._log("  " + ln))
            if ok:
                self._log(f"✓ {sts}: leader elected.", "ok")
            else:
                any_fail = True
                self._log(f"✗ {sts}: still no leader — may need PVC reset (Part 9).",
                          "error")
        if not any_fail:
            self._log("restarting any crash-looping ts-* services to reconnect…")
            for p in cluster.list_pods():
                if p["state"] == model.CRASH and p["name"].startswith("ts-"):
                    cluster.delete_pod(p["name"])
                    self._log(f"  restarted {p['name']}")
            self._log("done — watch the Status tab.", "ok")

    def _detect_oom(self):
        self._log("\n— Detect OOMKilled —", "ok")
        oom = cluster.oomkilled()
        if not oom:
            self._log("no OOMKilled pods.", "ok")
            return
        for p in oom:
            self._log(f"  {p['name']} last={p.get('last_terminated_reason')} "
                      f"restarts={p['restarts']}", "warn")
        self._log("hint: if a nacos member is here, re-apply nacos armor.")


# =============================================================================
# Power tab (stop / kill)
# =============================================================================
class PowerFrame(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=10)
        self.app = app

        ttk.Label(
            self,
            text="Stop / kill controls. Destructive actions ask for confirmation; "
                 "output streams below.", foreground="#8b949e",
        ).pack(anchor="w", pady=(0, 6))

        # --- graceful: park whole system or a single batch -------------------
        ttk.Label(self, text="Services", font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(2, 6))
        ttk.Button(top, text="Park all ts-* (scale 0)",
                  command=lambda: self._spawn(self._park_all)).pack(side="left", padx=(0, 6))

        batches = ttk.Frame(self)
        batches.pack(fill="x", pady=(0, 8))
        for i, (title, services) in enumerate(model.BATCHES, start=1):
            ttk.Button(
                batches, text=f"Stop B{i}: {title}",
                command=lambda s=services, t=title: self._spawn(
                    lambda: self._stop_batch(s, t)),
            ).grid(row=(i - 1) // 3, column=(i - 1) % 3, sticky="ew", padx=3, pady=3)
        for c in range(3):
            batches.columnconfigure(c, weight=1)

        # --- cluster power ---------------------------------------------------
        ttk.Label(self, text="Cluster", font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        power = ttk.Frame(self)
        power.pack(fill="x", pady=(2, 6))
        ttk.Button(power, text="Stop minikube", command=self._stop_minikube).pack(
            side="left", padx=(0, 6))
        ttk.Button(power, text="Pause minikube",
                  command=lambda: self._spawn(self._pause)).pack(side="left", padx=(0, 6))
        ttk.Button(power, text="Unpause minikube",
                  command=lambda: self._spawn(self._unpause)).pack(side="left", padx=(0, 6))
        # Delete is destructive → red tk.Button, confirmation-gated.
        tk.Button(power, text="Delete cluster", bg="#da3633", fg="white",
                 activebackground="#b62324", activeforeground="white",
                 command=self._delete_cluster).pack(side="right")

        self.out = _LogPane(self, height=12)
        self.out.pack(fill="both", expand=True, pady=(8, 0))

    # --- plumbing -------------------------------------------------------------
    def _spawn(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    def _log(self, text: str, tag: str = "info"):
        self.app.post(lambda: self.out.write(text, tag))

    def _stream_cmd(self, title, cmd, working, timeout=None) -> int:
        """Stream a command's output live to the log; return its exit code."""
        self._log(f"\n— {title} —", "warn")
        self._log(f"$ {' '.join(cmd)}")
        self._log(working)
        rc = cluster.stream_run(
            cmd, lambda ln: self._log("  " + ln) if ln.strip() else None,
            timeout=timeout)
        return rc

    # --- service-level (graceful) --------------------------------------------
    def _park_all(self):
        self._log("\n— Park all ts-* —", "warn")
        self._log("⏳ scaling every ts-* deployment to 0…")
        cluster.park_all()
        self._log("✓ parked all ts-* services. Un-park via Boot ▸ Smart Start.", "ok")

    def _stop_batch(self, services, title):
        self._log(f"\n— Stop batch: {title} —", "warn")
        self._log(f"⏳ scaling {len(services)} services to 0…")
        for d in services:
            cluster.scale(d, 0)
            self._log(f"  scaled {d} → 0")
        self._log(f"✓ batch '{title}' stopped ({len(services)} services).", "ok")

    # --- cluster power (confirmation on UI thread, then worker) --------------
    def _stop_minikube(self):
        if not messagebox.askyesno(
            "Stop minikube",
            "Halt the whole cluster?\n\nState and hardening survive; the next "
            "Boot ▸ Smart Start will start it again.",
        ):
            return
        self._spawn(self._do_stop_minikube)

    def _do_stop_minikube(self):
        rc = self._stream_cmd(
            "Stop minikube", ["minikube", "stop"],
            "⏳ stopping the cluster… live output below (can take ~30s).",
            timeout=180)
        if rc != 0:
            self._log(f"minikube stop returned rc={rc}.", "warn")
        # Verify the docker container actually halted — `minikube stop` SSHes into
        # the node and can hang/no-op when the node is wedged, leaving it Up.
        state = cluster.docker_state("minikube")
        self._log(f"docker container 'minikube' state: {state or 'absent'}")
        if state == "running":
            self._log("⚠ container still running — forcing `docker stop minikube`…",
                      "warn")
            self._stream_cmd("Force stop (docker)", ["docker", "stop", "minikube"],
                             "⏳ stopping the container directly…", timeout=90)
            state = cluster.docker_state("minikube")
        if state and state != "running":
            self._log(f"✓ container stopped (state={state}). "
                      "Boot ▸ Smart Start to bring it back.", "ok")
        elif state == "running":
            self._log("✗ container is STILL running — check `docker ps` / "
                      "`docker logs minikube` manually.", "error")
        else:
            self._log("✓ container is gone.", "ok")

    def _pause(self):
        rc = self._stream_cmd("Pause minikube", ["minikube", "pause"],
                              "⏳ freezing the cluster…", timeout=90)
        self._log("✓ minikube paused (frozen, ~no CPU). Use Unpause to resume."
                  if rc == 0 else f"✗ pause FAILED (rc={rc}).",
                  "ok" if rc == 0 else "error")

    def _unpause(self):
        rc = self._stream_cmd("Unpause minikube", ["minikube", "unpause"],
                              "⏳ resuming the cluster…", timeout=90)
        self._log("✓ minikube unpaused (running again)."
                  if rc == 0 else f"✗ unpause FAILED (rc={rc}).",
                  "ok" if rc == 0 else "error")

    def _delete_cluster(self):
        if not messagebox.askyesno(
            "Delete cluster — DESTRUCTIVE",
            "This runs `minikube delete`. It WIPES the cluster AND the Part-4 "
            "hardening (nacos armor + memory leases).\n\nYou'll need the full "
            "installation-guide Part 3–4 setup again. Continue?",
            icon="warning", default="no",
        ):
            return
        self._spawn(self._do_delete)

    def _do_delete(self):
        rc = self._stream_cmd(
            "Delete cluster", ["minikube", "delete"],
            "⏳ deleting the cluster (removes the container)… can take a minute.",
            timeout=300)
        state = cluster.docker_state("minikube")
        if rc == 0 and not state:
            self._log("✓ cluster deleted (container removed). Re-run "
                      "installation-guide Part 3–4 to rebuild + re-harden.", "ok")
        elif state == "running":
            self._log("⚠ container still present — forcing `docker stop` + remove…",
                      "warn")
            cluster.docker_stop("minikube")
            cluster.docker_rm("minikube")
            self._log("forced removal attempted; verify with `docker ps -a`.", "warn")
        else:
            self._log(f"delete finished (rc={rc}, container={state or 'absent'}).",
                      "ok" if rc == 0 else "error")


# =============================================================================
# Status tab (at-a-glance dot board)
# =============================================================================
class StatusTile:
    """A neutral tile with a coloured status dot + service name. Click jumps to Pods."""

    def __init__(self, master, deploy, on_click):
        self.deploy = deploy
        self.pod_name: str | None = None
        short = model.short_name(deploy) if deploy.startswith("ts-") else deploy
        if deploy in model.KNOWN_BUGS:
            short = short.upper()
        self.frame = tk.Frame(master, bg="#161b22", relief="ridge", bd=1,
                              padx=4, pady=2)
        self.dot = tk.Label(self.frame, text="●", fg=COLORS[model.UNKNOWN],
                           bg="#161b22", font=("TkDefaultFont", 11))
        self.name = tk.Label(self.frame, text=short, fg=FG, bg="#161b22", anchor="w")
        self.dot.pack(side="left")
        self.name.pack(side="left")
        for w in (self.frame, self.dot, self.name):
            w.bind("<Button-1>", lambda e: on_click(self))

    def set_state(self, state: str, pod_name: str | None):
        self.pod_name = pod_name
        self.dot.config(fg=COLORS[state])


class StatusFrame(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=10)
        self.app = app
        self.tiles: dict[str, StatusTile] = {}      # deploy -> tile (46 app tiles, fixed)
        self.infra_tiles: dict[str, StatusTile] = {}  # pod-name -> tile (rebuilt on change)
        self._infra_names: set[str] = set()

        self.summary = ttk.Label(self, text="loading…")
        self.summary.pack(anchor="w")
        ttk.Label(
            self,
            text="legend:  ● green=running   ● yellow=pending   ● red=crash   "
                 "● gray=not started   ·   click a tile → Pods tab for logs",
            foreground="#8b949e",
        ).pack(anchor="w", pady=(0, 4))

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, highlightthickness=0, bg=BG)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self._build_app_rows()
        self.refresh()
        self._schedule()

    # --- layout ---------------------------------------------------------------
    def _build_app_rows(self):
        """One fixed tile per expected service, grouped by batch. INFRA row added lazily."""
        self.infra_row = ttk.Frame(self.inner)
        self.infra_row.pack(fill="x", anchor="w", pady=2)
        tk.Label(self.infra_row, text="INFRA", width=8, anchor="w", bg=BG,
                fg="#58a6ff").pack(side="left")
        self.infra_holder = ttk.Frame(self.infra_row)
        self.infra_holder.pack(side="left", fill="x")

        for i, (title, services) in enumerate(model.BATCHES, start=1):
            row = ttk.Frame(self.inner)
            row.pack(fill="x", anchor="w", pady=2)
            tk.Label(row, text=f"BATCH{i}", width=8, anchor="w", bg=BG,
                    fg="#58a6ff").pack(side="left")
            for deploy in services:
                tile = StatusTile(row, deploy, self._on_click)
                tile.frame.pack(side="left", padx=2, pady=1)
                self.tiles[deploy] = tile

    # --- refresh --------------------------------------------------------------
    def _schedule(self):
        self.app.root.after(3000, self._auto)

    def _auto(self):
        self.refresh()
        self._schedule()

    def refresh(self):
        self.app.submit(cluster.list_pods, then=self._apply)

    def _apply(self, pods):
        if isinstance(pods, Exception):
            self.summary["text"] = f"cluster unavailable: {pods}"
            return
        if not pods:
            self.summary["text"] = ("no pods — cluster not installed. "
                                    "Use Boot ▸ Smart Start.")
            # fall through: all tiles will be set gray below
        # map app deploy -> its pod (if any)
        app_pod = {}
        infra_pods = {}
        for p in pods:
            if any(p["name"].startswith(pref) for pref in model.INFRA_PREFIXES):
                infra_pods[p["name"]] = p
            else:
                app_pod[_deploy_of(p["name"])] = p

        counts = {model.READY: 0, model.PENDING: 0, model.CRASH: 0, model.UNKNOWN: 0}
        for deploy, tile in self.tiles.items():
            p = app_pod.get(deploy)
            state = p["state"] if p else model.UNKNOWN  # no pod → gray (not started)
            tile.set_state(state, p["name"] if p else None)
            counts[state] += 1

        # infra row: rebuild only when the set of infra pod names changes
        names = set(infra_pods)
        if names != self._infra_names:
            for w in self.infra_holder.winfo_children():
                w.destroy()
            self.infra_tiles.clear()
            for name in sorted(names):
                tile = StatusTile(self.infra_holder, name, self._on_click)
                tile.frame.pack(side="left", padx=2, pady=1)
                self.infra_tiles[name] = tile
            self._infra_names = names
        for name, tile in self.infra_tiles.items():
            tile.set_state(infra_pods[name]["state"], name)

        if pods:  # keep the "not installed" hint when there are zero pods
            self.summary["text"] = (
                f"● {counts[model.READY]} running     "
                f"● {counts[model.PENDING]} pending     "
                f"● {counts[model.CRASH]} crash     "
                f"● {counts[model.UNKNOWN]} not-started "
                f"(of {model.SERVICE_COUNT} services)"
            )

    def _on_click(self, tile: StatusTile):
        self.app.show_pod(tile.pod_name)


# =============================================================================
# Resource bar (host + minikube gauges) + quick live RAM boost
# =============================================================================
def _heat(pct: float) -> str:
    if pct >= 90:
        return "#f85149"   # red
    if pct >= 75:
        return "#d29922"   # yellow
    return "#3fb950"       # green


class ResourceBar(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=(8, 2))
        self.app = app
        self._plan = (0, 0)

        ttk.Label(self, text="Host", foreground="#8b949e").pack(side="left")
        self.h_cpu = ttk.Label(self, text="CPU --%", width=9)
        self.h_cpu.pack(side="left", padx=(4, 2))
        self.h_ram = ttk.Label(self, text="RAM --%", width=9)
        self.h_ram.pack(side="left", padx=2)
        self.h_info = ttk.Label(self, text="", foreground="#8b949e")
        self.h_info.pack(side="left", padx=(2, 0))

        ttk.Separator(self, orient="vertical").pack(side="left", fill="y", padx=10)

        ttk.Label(self, text="train-ticket", foreground="#8b949e").pack(side="left")
        self.m_cpu = ttk.Label(self, text="CPU --", width=11)
        self.m_cpu.pack(side="left", padx=(4, 2))
        self.m_ram = ttk.Label(self, text="RAM --", width=22)
        self.m_ram.pack(side="left", padx=2)

        self.boost = ttk.Button(self, text="Boost RAM", command=self.boost_ram)
        self.boost.pack(side="right")

        self._poll()

    # --- live gauges ----------------------------------------------------------
    def _poll(self):
        self.app.submit(self._fetch, then=self._apply)
        self.app.root.after(3000, self._poll)

    @staticmethod
    def _fetch():
        return {
            "host": cluster.host_resources(),
            "mk": cluster.minikube_stats(),
            "limit": cluster.node_mem_limit_gb(),
            "plan": cluster.planned_resources(),
        }

    def _apply(self, d):
        if isinstance(d, Exception):
            return
        h = d["host"]
        self._plan = d["plan"]
        self.h_cpu.config(text=f"CPU {h['cpu_pct']:.0f}%", foreground=_heat(h["cpu_pct"]))
        self.h_ram.config(text=f"RAM {h['ram_pct']:.0f}%", foreground=_heat(h["ram_pct"]))
        self.h_info.config(
            text=f"({h['cpu_count']} cores, {h['ram_total_gb']:.0f} GB)")
        mk = d["mk"]
        if mk:
            self.m_cpu.config(text=f"CPU {mk['cpu']}")
            pct = 0.0
            try:
                pct = float(mk["mem_pct"].rstrip("%"))
            except ValueError:
                pass
            self.m_ram.config(text=f"RAM {mk['mem']} {mk['mem_pct']}",
                             foreground=_heat(pct))
        else:
            self.m_cpu.config(text="CPU —", foreground="#8b949e")
            self.m_ram.config(text="(node down)", foreground="#8b949e")

    # --- quick live boost -----------------------------------------------------
    def boost_ram(self):
        target = self._plan[1] or 0
        cur = cluster.node_mem_limit_gb() or 0
        if target <= cur:
            messagebox.showinfo(
                "Boost node RAM",
                f"Node already at {cur:.0f} GB; the host-recommended ceiling is "
                f"{target} GB, so there's nothing to add. (Recommendation leaves "
                "~8 GB for the host.)")
            return
        if not messagebox.askyesno(
            "Boost node RAM (live)",
            f"Raise the minikube node's memory ceiling from {cur:.0f} GB to "
            f"{target} GB now?\n\nThis is live (no restart) and only raises the "
            "OOM limit — the cluster uses more only as needed. Leaves ~8 GB for "
            "the host."):
            return
        self.boost.config(state="disabled")

        def work():
            return cluster.set_node_memory(target)

        def done(r):
            self.boost.config(state="normal")
            if isinstance(r, Exception) or (isinstance(r, tuple) and r[0] != 0):
                messagebox.showerror("Boost node RAM", f"Failed: {r}")
            else:
                messagebox.showinfo("Boost node RAM",
                                    f"Node memory ceiling raised to {target} GB.")
        self.app.submit(work, then=done)


def ask_resources(root, cpus, mem, host) -> tuple | None:
    """Modal dialog to confirm/adjust node resources before a fresh create.
    Pre-filled with the host-derived recommendation; returns (cpus, mem) or None."""
    win = tk.Toplevel(root)
    win.title("Allocate cluster resources")
    win.transient(root)
    win.grab_set()
    pad = {"padx": 10, "pady": 4}
    ttk.Label(win, text="New cluster — how much of this machine to give it?",
             font=("TkDefaultFont", 10, "bold")).grid(row=0, column=0, columnspan=2,
                                                       sticky="w", **pad)
    ttk.Label(win, text=f"This host: {host['cpu_count']} cores, "
              f"{host['ram_total_gb']:.0f} GB RAM").grid(row=1, column=0, columnspan=2,
                                                         sticky="w", **pad)
    ttk.Label(win, text="CPUs:").grid(row=2, column=0, sticky="e", **pad)
    cpu_var = tk.StringVar(value=str(cpus))
    ttk.Entry(win, textvariable=cpu_var, width=8).grid(row=2, column=1, sticky="w", **pad)
    ttk.Label(win, text="Memory (GB):").grid(row=3, column=0, sticky="e", **pad)
    mem_var = tk.StringVar(value=str(mem))
    ttk.Entry(win, textvariable=mem_var, width=8).grid(row=3, column=1, sticky="w", **pad)
    ttk.Label(win, text=f"(recommended: {cpus} CPU / {mem} GB — leaves ~8 GB for host)",
             foreground="#8b949e").grid(row=4, column=0, columnspan=2, sticky="w", **pad)

    result = {"val": None}

    def ok():
        try:
            result["val"] = (max(1, int(cpu_var.get())), max(4, int(mem_var.get())))
        except ValueError:
            result["val"] = (cpus, mem)
        win.destroy()

    btns = ttk.Frame(win)
    btns.grid(row=5, column=0, columnspan=2, pady=8)
    ttk.Button(btns, text="Create with these", command=ok).pack(side="left", padx=4)
    ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="left", padx=4)
    win.wait_window()
    return result["val"]


# =============================================================================
# App shell
# =============================================================================
class TtctlGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("ttctl — train-ticket launcher")
        self.root.geometry("1040x720")
        self.root.minsize(820, 560)
        self.q: queue.Queue = queue.Queue()

        self.header = ttk.Label(self.root, text="minikube: … checking",
                               font=("TkDefaultFont", 10, "bold"), padding=(8, 6))
        self.header.pack(fill="x")

        self.resbar = ResourceBar(self.root, self)
        self.resbar.pack(fill="x")
        ttk.Separator(self.root, orient="horizontal").pack(fill="x")

        self.nb = ttk.Notebook(self.root)
        nb = self.nb
        nb.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.boot_tab = BootFrame(nb, self)
        self.pods_tab = PodsFrame(nb, self)
        self.status_tab = StatusFrame(nb, self)
        self.repair_tab = RepairFrame(nb, self)
        self.power_tab = PowerFrame(nb, self)
        nb.add(self.boot_tab, text="Boot")
        nb.add(self.pods_tab, text="Pods")
        nb.add(self.status_tab, text="Status")
        nb.add(self.repair_tab, text="Repair")
        nb.add(self.power_tab, text="Power")

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(120, self._drain)
        self._poll_header()

    # --- navigation -----------------------------------------------------------
    def show_pod(self, name: str | None):
        """Switch to the Pods tab; if a pod name is given, select it there."""
        self.nb.select(self.pods_tab)
        if name:
            self.pods_tab.select(name)

    # --- threading plumbing ---------------------------------------------------
    def post(self, fn):
        """Schedule `fn` to run on the UI thread."""
        self.q.put(fn)

    def submit(self, fn, then=None):
        """Run blocking `fn` in a thread; post `then(result)` to the UI thread.
        On error, `then` receives the Exception instance."""
        def worker():
            try:
                res = fn()
            except Exception as e:  # noqa: BLE001 — surfaced to the callback
                res = e
            if then is not None:
                self.q.put(lambda: then(res))
        threading.Thread(target=worker, daemon=True).start()

    def _drain(self):
        try:
            while True:
                cb = self.q.get_nowait()
                try:
                    cb()
                except Exception:
                    pass
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    # --- header ---------------------------------------------------------------
    def _poll_header(self):
        def fetch():
            up = cluster.minikube_running()
            return (up, cluster.minikube_ip() if up else None)
        self.submit(fetch, then=self._header_apply)
        self.root.after(5000, self._poll_header)

    def _header_apply(self, res):
        up, ip = (False, None) if isinstance(res, Exception) else res
        if up and ip:
            self.header.config(
                text=f"minikube: ● up    IP {ip}    UI http://{ip}:{model.UI_NODEPORT}",
                foreground="#3fb950")
        elif up:
            self.header.config(text="minikube: ● up (API not ready)", foreground="#d29922")
        else:
            self.header.config(text="minikube: ● down — go to Boot ▸ Smart Start",
                              foreground="#f85149")

    def _on_close(self):
        try:
            self.pods_tab._kill_log()
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    TtctlGUI().run()


if __name__ == "__main__":
    main()

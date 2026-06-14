# ttctl — train-ticket launcher (desktop GUI)

A standalone **Tkinter desktop window** that wraps the boot/repair logic of
`start-train-ticket.sh` into an interactive control panel with three tabs.

```
┌─ ttctl — train-ticket launcher ──────────────────────────[_] [□] [X]─┐
│ minikube: ● up   IP 192.168.49.2   UI http://192.168.49.2:32677      │
│ [ Boot ]  [ Pods ]  [ Repair ]                                       │
└──────────────────────────────────────────────────────────────────────┘
```

- **Boot** — one **Smart Start** button. It runs `cluster.diagnose()` (docker
  container? minikube up? installed? hardened? infra healthy? crashing?) and a
  decision tree picks the right path automatically:
  - nothing there → create a correctly-sized node (`--memory=20g`, CPU-capped) +
    helm install + build (deploy Job) + harden + wave-boot
  - installed but un-hardened/broken → **repair in place** (park → nacos armor +
    memory leases → restart nacos → wave-boot), no reinstall
  - installed + hardened + parked → just wave-boot
  - all green → nothing
  Progress shows as a **per-stage panel** — one bar per stage (Cluster / Helm /
  Deploy / Harden / Park / Infra / Wave-boot / …) with a real percentage where one
  exists (minikube download %, deploy X/46, harden X/46, wave-boot X/46) or an
  animated pulse otherwise, plus an always-spinning indicator + elapsed so it's
  never blank. `Stop` cancels.
  **Master Cleanup** (red) wipes everything back to a fresh start: `minikube
  delete` → `docker rm -f minikube` → `docker image prune -af` (confirmation-gated).
- **Pods** — a color-coded grid, one button per pod (green=ready, yellow=pending,
  red=crash/OOM), auto-refreshing every ~4s. Click a pod to **View logs**,
  **Restart**, or **Describe**. Quorum members (nacos/mysql) are protected from
  restart-from-here.
- **Status** — an at-a-glance board: one tile per expected service (+ infra),
  each with a coloured dot (green=running, yellow=pending, red=crash,
  gray=not-started). Keyed on the full 46-service list, so during wave-boot the
  dots go gray → yellow → green batch by batch. Click a tile to jump to the Pods
  tab with that pod selected.
- **Repair** — one-click documented recovery actions (fix consign-price, re-apply
  nacos armor, re-assert CPU cap, re-sign memory leases, park, batch-restart,
  check MySQL election, detect OOMKilled). A top banner runs quick diagnostics.
- **Power** — stop/kill controls: park all ts-*, stop a single wave-boot batch,
  stop / pause / unpause minikube, and a confirmation-gated **Delete cluster**
  (`minikube delete`, which also wipes the Part-4 hardening). Long minikube
  commands **stream their output live** and end with a clear ✓/✗. **Stop minikube**
  then verifies the docker container actually halted and falls back to
  `docker stop minikube` if the node was wedged and `minikube stop` no-opped.

## Resource bar (always visible)

Below the header, a live bar shows **Host CPU/RAM %** (and core/RAM totals) and the
**train-ticket** (minikube container) **CPU % + RAM used/limit** — green/yellow/red by
load, refreshed every ~3s. A **Boost RAM** button live-raises the node's memory ceiling
(no restart) to the host-appropriate recommendation when there's room.

## Portability — resource allocation on a new machine

`minikube_start_cmd()` sizes a *freshly created* node from the host: `recommend_resources()`
gives `cpus = cores − 4` and `memory = min(28, host_total − 8) GB` (leaves ~8 GB for the
desktop). On a fresh cluster, **Smart Start** pops a dialog pre-filled with that recommendation
that you can accept (auto) or edit (decide). Env overrides win: `TT_MINIKUBE_CPUS`,
`TT_MINIKUBE_MEMORY` (e.g. `24` or `24g`).

## Requirements

- Python 3.10+ with **tkinter** (ships with CPython; if missing on Debian/Ubuntu:
  `sudo apt install python3-tk`). No `pip install` needed.
- A working `kubectl` pointed at the cluster, plus `minikube` and `docker` on PATH.
- A graphical display (X11/Wayland). On a headless box, run under `xvfb-run`.

## Run

```bash
cd tools/            # the package dir is tools/ttctl
python -m ttctl      # opens the window
```

## Relationship to start-train-ticket.sh

The orchestration is reimplemented in Python (`boot.py`, calling `cluster.py`),
so the GUI can show live per-pod progress. The bash script is unchanged and
remains a headless fallback — running it after a GUI boot should fast-exit
"already fully healthy". The batch lists, infra thresholds, health probes, and the
consign-price SQL all come from that script and `installation-guide.md` Part 4/7/9.

## Layout

| file | role |
|------|------|
| `model.py` | batches, infra prefixes, status→color, known bugs |
| `cluster.py` | every kubectl/curl/docker shell-out (the reusable core) |
| `boot.py` | async wave-boot state machine |
| `gui.py` | the whole Tkinter app: header + Notebook with Boot/Pods/Repair frames |

### Threading

Tkinter is single-threaded and every `cluster.*` call blocks on a subprocess, so
all work runs in daemon threads that post UI updates onto a `queue.Queue` drained
by `root.after`. The Boot tab runs `boot.BootController` inside an asyncio loop on
a worker thread; **Stop** cancels it via `loop.call_soon_threadsafe`.

"""Thin subprocess layer over kubectl / curl / docker.

Every shell-out the dashboard makes lives here, so widgets never call subprocess
directly. Sync helpers return parsed data; `stream()` is an async generator for
live log panes. Mirrors the probes in start-train-ticket.sh.
"""

from __future__ import annotations

import asyncio
import enum
import json
import os
import re
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

# The two xenon/Raft MySQL clusters: the app DB and the nacos registry DB.
MYSQL_DBS = ["tsdb-mysql", "nacosdb-mysql"]

from . import model

NS = model.NS


def repo_root() -> str:
    """Directory of the helm chart (= the train-ticket repo root).

    Env `TT_REPO` overrides; otherwise the package lives at
    <repo>/tools/ttctl/cluster.py, so the repo root is parents[2]."""
    env = os.environ.get("TT_REPO")
    if env:
        return env
    return str(Path(__file__).resolve().parents[2])


class ClusterError(RuntimeError):
    pass


# --- low-level run helpers ----------------------------------------------------

def _run(cmd: list[str], timeout: int = 15, check: bool = False) -> tuple[int, str, str]:
    """Run a command, return (returncode, stdout, stderr). Never raises on nonzero
    unless check=True."""
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s: {' '.join(cmd)}"
    except FileNotFoundError as e:
        return 127, "", str(e)
    if check and p.returncode != 0:
        raise ClusterError(p.stderr.strip() or f"command failed: {' '.join(cmd)}")
    return p.returncode, p.stdout, p.stderr


def _kubectl(args: list[str], timeout: int = 15, check: bool = False):
    return _run(["kubectl", "-n", NS, *args], timeout=timeout, check=check)


async def stream(cmd: list[str]) -> AsyncIterator[str]:
    """Yield stdout lines from a long-running command as they arrive."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    try:
        async for raw in proc.stdout:
            yield raw.decode(errors="replace").rstrip("\n")
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()


def stream_run(cmd: list[str], on_line, timeout: int | None = None,
               cwd: str | None = None) -> int:
    """Run `cmd`, calling on_line(str) for each output line as it arrives.

    Synchronous (meant to be called from a worker thread). Returns the exit code;
    124 on timeout, 127 if the binary is missing. on_line sees live output so the
    UI never sits in silence during long commands (minikube start/stop/delete,
    helm install). `cwd` runs the command from a directory (helm needs the chart)."""
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=cwd,
        )
    except FileNotFoundError as e:
        on_line(str(e))
        return 127
    assert proc.stdout is not None

    # Wall-clock watchdog: kill the process if it exceeds `timeout`. A plain
    # proc.wait(timeout=) is not enough — the read loop below blocks until stdout
    # closes, so a command that hangs without output (e.g. a wedged `minikube
    # stop`) would never time out without this.
    timed_out = {"v": False}
    timer = None
    if timeout:
        import threading

        def _kill():
            timed_out["v"] = True
            try:
                proc.kill()
            except Exception:
                pass

        timer = threading.Timer(timeout, _kill)
        timer.daemon = True
        timer.start()
    try:
        for line in proc.stdout:
            on_line(line.rstrip("\n"))
        proc.wait()
    finally:
        if timer is not None:
            timer.cancel()
    if timed_out["v"]:
        on_line(f"(timed out after {timeout}s — killed)")
        return 124
    return proc.returncode or 0


# --- cluster / API state ------------------------------------------------------

def api_up() -> bool:
    rc, _, _ = _run(["kubectl", "get", "ns", "--request-timeout=8s"], timeout=10)
    return rc == 0


def minikube_running() -> bool:
    rc, out, _ = _run(["minikube", "status", "-o", "json"], timeout=12)
    if rc != 0:
        return False
    try:
        data = json.loads(out)
        return data.get("Host") == "Running" and data.get("APIServer") == "Running"
    except (json.JSONDecodeError, AttributeError):
        return "Running" in out


def minikube_ip() -> str | None:
    rc, out, _ = _kubectl(
        ["get", "node", "minikube", "-o",
         'jsonpath={.status.addresses[?(@.type=="InternalIP")].address}'],
        timeout=10,
    )
    out = out.strip()
    return out or None


# Resource allocation for a freshly-created node. None = auto (host-derived).
# The GUI can set these (env overrides win) so a new machine is sized correctly.
RESOURCE_OVERRIDE: dict = {
    "cpus": int(os.environ["TT_MINIKUBE_CPUS"]) if os.environ.get("TT_MINIKUBE_CPUS") else None,
    "memory_gb": (int(str(os.environ["TT_MINIKUBE_MEMORY"]).rstrip("gG"))
                  if os.environ.get("TT_MINIKUBE_MEMORY") else None),
}


def recommend_resources() -> tuple[int, int]:
    """Auto-pick (cpus, memory_gb) for THIS host: give the cluster most of the box
    but leave headroom for the desktop. train-ticket wants ~22-25 GB to fit all 46
    services with breathing room."""
    h = host_resources()
    total = h["ram_total_gb"]
    # leave ~8GB for the host/desktop; cap at 28 (train-ticket needs ~24-26, no
    # point handing a big box's whole RAM to a workload that won't use it).
    mem = int(min(28, max(12, total - 8)))
    cpus = max(2, (h["cpu_count"] or 4) - 4)
    return cpus, mem


def planned_resources() -> tuple[int, int]:
    """What a fresh create will use: explicit override else the host recommendation."""
    cpus, mem = recommend_resources()
    if RESOURCE_OVERRIDE.get("cpus"):
        cpus = RESOURCE_OVERRIDE["cpus"]
    if RESOURCE_OVERRIDE.get("memory_gb"):
        mem = RESOURCE_OVERRIDE["memory_gb"]
    return cpus, mem


def minikube_start_cmd() -> list[str]:
    """`minikube start` argv. When the container is absent (fresh / after delete)
    create the node sized for this host (auto or override); minikube only honours
    --cpus/--memory at creation time, so a plain start on an existing cluster is
    fine and changing them needs a delete+recreate anyway."""
    if docker_state("minikube") is None:
        cpus, mem = planned_resources()
        return ["minikube", "start", "--driver=docker",
                f"--cpus={cpus}", f"--memory={mem}g"]
    return ["minikube", "start"]


def minikube_start() -> tuple[int, str, str]:
    return _run(minikube_start_cmd(), timeout=600)


def minikube_stop() -> tuple[int, str, str]:
    return _run(["minikube", "stop"], timeout=180)


def minikube_pause() -> tuple[int, str, str]:
    return _run(["minikube", "pause"], timeout=90)


def minikube_unpause() -> tuple[int, str, str]:
    return _run(["minikube", "unpause"], timeout=90)


def minikube_delete() -> tuple[int, str, str]:
    return _run(["minikube", "delete"], timeout=300)


def docker_state(name: str = "minikube") -> str | None:
    """Docker container state ('running'/'exited'/'created'/...) or None if absent."""
    rc, out, _ = _run(
        ["docker", "inspect", "-f", "{{.State.Status}}", name], timeout=15,
    )
    return out.strip() if rc == 0 and out.strip() else None


def docker_stop(name: str = "minikube") -> tuple[int, str, str]:
    """Hard-stop the container directly — works even when the node is wedged and
    `minikube stop` (which SSHes into the node) hangs."""
    return _run(["docker", "stop", name], timeout=90)


def docker_rm(name: str = "minikube") -> tuple[int, str, str]:
    """Force-remove the container (used as a last-resort after `minikube delete`)."""
    return _run(["docker", "rm", "-f", name], timeout=60)


def docker_image_prune() -> tuple[int, str, str]:
    """Reclaim disk by removing all unused images (Master Cleanup)."""
    return _run(["docker", "image", "prune", "-af"], timeout=300)


# --- resource gauges (host + minikube container) ------------------------------

def host_resources() -> dict:
    """This machine's live CPU% and RAM% (Linux, stdlib only)."""
    out = {"ram_total_gb": 0.0, "ram_pct": 0.0, "cpu_pct": 0.0,
           "cpu_count": os.cpu_count() or 0}
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if parts:
                    mem[parts[0].rstrip(":")] = int(parts[1])  # kB
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", mem.get("MemFree", 0))
        if total:
            out["ram_total_gb"] = total / 1024 / 1024
            out["ram_pct"] = 100.0 * (total - avail) / total
    except OSError:
        pass

    def _snap():
        with open("/proc/stat") as f:
            v = list(map(int, f.readline().split()[1:]))
        idle = v[3] + (v[4] if len(v) > 4 else 0)
        return idle, sum(v)
    try:
        i1, t1 = _snap()
        time.sleep(0.2)
        i2, t2 = _snap()
        if t2 > t1:
            out["cpu_pct"] = 100.0 * (1 - (i2 - i1) / (t2 - t1))
    except OSError:
        pass
    return out


def minikube_stats() -> dict | None:
    """The minikube container's live CPU% and memory (= all of train-ticket)."""
    rc, out, _ = _run(
        ["docker", "stats", "minikube", "--no-stream", "--format",
         "{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}"], timeout=15,
    )
    if rc != 0 or not out.strip():
        return None
    try:
        cpu, mem, mem_pct = out.strip().split("|")
        return {"cpu": cpu.strip(), "mem": mem.strip(), "mem_pct": mem_pct.strip()}
    except ValueError:
        return None


def node_mem_limit_gb() -> float | None:
    """Current memory ceiling of the minikube container, in GB."""
    rc, out, _ = _run(
        ["docker", "inspect", "-f", "{{.HostConfig.Memory}}", "minikube"], timeout=15,
    )
    if rc != 0 or not out.strip().isdigit():
        return None
    b = int(out.strip())
    return b / 1024 / 1024 / 1024 if b > 0 else None


def set_node_memory(gb: int) -> tuple[int, str, str]:
    """Live-raise the minikube container's memory ceiling (no restart). Gives the
    cluster real headroom immediately; minikube's k8s 'allocatable' stays as-is but
    the cgroup limit is what triggers OOM kills, so this stops them."""
    return _run(["docker", "update", f"--memory={gb}g",
                 f"--memory-swap={gb}g", "minikube"], timeout=30)


# --- install / helm (fresh-cluster setup) -------------------------------------

def namespace_exists() -> bool:
    rc, _, _ = _run(["kubectl", "get", "ns", NS], timeout=10)
    return rc == 0


def deploy_count() -> int:
    rc, out, _ = _kubectl(["get", "deploy", "--no-headers"], timeout=15)
    if rc != 0:
        return 0
    return len([ln for ln in out.splitlines() if ln.strip()])


def installed() -> bool:
    """True if the train-ticket workloads are present (namespace + ≥1 deployment).
    Distinguishes an installed cluster from a fresh/empty one."""
    return namespace_exists() and deploy_count() > 0


def helm_installed(release: str = "train-ticket") -> bool:
    rc, out, _ = _run(["helm", "list", "-n", NS, "-q"], timeout=20)
    if rc != 0:
        return False
    return release in out.split()


def helm_install_cmd(release: str = "train-ticket") -> list[str]:
    return ["helm", "install", release, ".", "-n", NS, "--create-namespace"]


def deploy_job_exists(job: str = "train-ticket-deploy") -> bool:
    rc, _, _ = _kubectl(["get", "job", job], timeout=10)
    return rc == 0


def wait_job_complete(job: str = "train-ticket-deploy", timeout: int = 2400) -> bool:
    rc, _, _ = _kubectl(
        ["wait", "--for=condition=complete", f"job/{job}", f"--timeout={timeout}s"],
        timeout=timeout + 15,
    )
    return rc == 0


def pod_total() -> int:
    """Total pods in the namespace (any phase) — 0 means nothing deployed yet."""
    try:
        return len(list_pods())
    except ClusterError:
        return 0


def is_hardened() -> bool:
    """True if Part-4 hardening is applied: a sample ts-* deployment has the
    700Mi memory cap AND nacos has resource limits (armored)."""
    rc, out, _ = _kubectl(
        ["get", "deploy", "ts-station-service", "-o",
         "jsonpath={.spec.template.spec.containers[0].resources.limits.memory}"],
        timeout=10,
    )
    svc_capped = rc == 0 and out.strip() == "700Mi"
    rc2, out2, _ = _kubectl(
        ["get", "statefulset", "nacos", "-o",
         "jsonpath={.spec.template.spec.containers[0].resources.limits.memory}"],
        timeout=10,
    )
    nacos_armored = rc2 == 0 and bool(out2.strip())
    return svc_capped and nacos_armored


# --- decision engine ----------------------------------------------------------

class Action(enum.Enum):
    CREATE = "create"        # no container — make it (sized) + install + harden + boot
    START = "start"          # container exists but stopped — start, then re-diagnose
    INSTALL = "install"      # up, nothing installed — helm install + deploy + harden + boot
    REPAIR = "repair"        # installed but unhardened/unhealthy — harden + fix + boot
    BOOT = "boot"            # installed + hardened, just parked — wave-boot
    HEALTHY = "healthy"      # nothing to do


@dataclass
class ClusterState:
    container: str | None
    minikube_up: bool
    api_up: bool
    helm_ok: bool
    installed: bool
    hardened: bool
    infra_ok: bool
    site_ok: bool
    running: int
    crash: int
    pending: int
    total: int
    action: Action
    summary: str


def diagnose() -> ClusterState:
    """One read-only sweep of the world → a recommended Action."""
    container = docker_state("minikube")
    if container is None:
        return ClusterState(None, False, False, False, False, False, False, False,
                            0, 0, 0, 0, Action.CREATE,
                            "No cluster container — will create + install from scratch.")

    up = minikube_running()
    api = api_up() if up else False
    if not up or not api:
        return ClusterState(container, up, api, False, False, False, False, False,
                            0, 0, 0, 0, Action.START,
                            "Cluster container exists but is stopped — will start it.")

    helm_ok = helm_installed()
    inst = deploy_count() > 0
    if not inst:
        return ClusterState(container, up, api, helm_ok, False, False, False, False,
                            0, 0, 0, 0, Action.INSTALL,
                            "Cluster up but nothing installed — will helm install + build.")

    try:
        pods = list_pods()
    except ClusterError:
        pods = []
    running = sum(1 for p in pods if p["state"] == model.READY)
    crash = sum(1 for p in pods if p["state"] == model.CRASH)
    pending = sum(1 for p in pods if p["state"] == model.PENDING)
    hardened = is_hardened()
    infra_ok, _ = infra_ready(pods)
    no_leader = dbs_needing_leader()   # both tsdb + nacosdb checked
    site_ok = site_healthy()

    if (hardened and infra_ok and not no_leader and site_ok
            and crash == 0 and pending == 0):
        action, summary = Action.HEALTHY, "All green — nothing to do."
    elif not hardened or not infra_ok or no_leader or crash > 0:
        action = Action.REPAIR
        why = []
        if not hardened:
            why.append("not hardened")
        if no_leader:
            why.append("no leader on " + "+".join(no_leader))
        if not infra_ok:
            why.append("infra not green")
        if crash:
            why.append(f"{crash} pod(s) crashing")
        summary = "Installed but " + ", ".join(why) + " — will repair in place."
    else:
        action, summary = Action.BOOT, "Installed + hardened — will wave-boot."

    return ClusterState(container, up, api, helm_ok, inst, hardened, infra_ok, site_ok,
                        running, crash, pending, len(pods), action, summary)


def nproc() -> int:
    return os.cpu_count() or 4


def set_cpu_cap() -> tuple[int, str, str]:
    cpus = max(1, nproc() - 4)
    return _run(["docker", "update", f"--cpus={cpus}", "minikube"], timeout=30)


def cpu_cap_value() -> int | None:
    """Docker's NanoCpus for the minikube container (0/None = uncapped)."""
    rc, out, _ = _run(
        ["docker", "inspect", "minikube", "--format", "{{.HostConfig.NanoCpus}}"],
        timeout=15,
    )
    if rc != 0:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


# --- pods ---------------------------------------------------------------------

def list_pods() -> list[dict]:
    """Return normalized pod dicts. Powers both the Pods grid and the boot counter."""
    rc, out, err = _kubectl(["get", "pods", "-o", "json"], timeout=15)
    if rc != 0:
        raise ClusterError(err.strip() or "kubectl get pods failed")
    data = json.loads(out)
    pods = []
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        st = item.get("status", {})
        cstatuses = st.get("containerStatuses", []) or []
        total = len(cstatuses)
        ready = sum(1 for c in cstatuses if c.get("ready"))
        restarts = sum(c.get("restartCount", 0) for c in cstatuses)

        # surface the most informative waiting/terminated reason
        status_reason = st.get("phase", "")
        last_term = None
        for c in cstatuses:
            state = c.get("state", {})
            if "waiting" in state and state["waiting"].get("reason"):
                status_reason = state["waiting"]["reason"]
            last = c.get("lastState", {}).get("terminated", {})
            if last.get("reason"):
                last_term = last["reason"]
            term = state.get("terminated", {})
            if term.get("reason") and term["reason"] != "Completed":
                status_reason = term["reason"]

        pod = {
            "name": meta.get("name", ""),
            "deploy": _owner_deploy(meta),
            "ready": ready,
            "total": total,
            "all_ready": total > 0 and ready == total,
            "phase": st.get("phase", ""),
            "status": status_reason,
            "restarts": restarts,
            "last_terminated_reason": last_term,
        }
        pod["state"] = model.classify(pod)
        pods.append(pod)
    return pods


def _owner_deploy(meta: dict) -> str:
    """Best-effort deployment name from pod labels (app=... or app.kubernetes.io/name)."""
    labels = meta.get("labels", {})
    return labels.get("app") or labels.get("app.kubernetes.io/name") or ""


def not_ready_count(pods: list[dict] | None = None) -> int:
    pods = pods if pods is not None else list_pods()
    return sum(
        1 for p in pods
        if p["phase"] != "Succeeded" and not p["all_ready"]
    )


def oomkilled(pods: list[dict] | None = None) -> list[dict]:
    pods = pods if pods is not None else list_pods()
    return [
        p for p in pods
        if p.get("last_terminated_reason") == "OOMKilled" or p.get("status") == "OOMKilled"
    ]


# --- scaling / waiting --------------------------------------------------------

def scale(deploy: str, replicas: int) -> tuple[int, str, str]:
    return _kubectl(["scale", f"deploy/{deploy}", f"--replicas={replicas}"], timeout=30)


def list_ts_deploys() -> list[str]:
    rc, out, _ = _kubectl(["get", "deploy", "-o", "name"], timeout=15)
    if rc != 0:
        return list(model.ALL_SERVICES)
    names = []
    for line in out.splitlines():
        name = line.split("/", 1)[-1].strip()
        if name.startswith("ts-"):
            names.append(name)
    return names or list(model.ALL_SERVICES)


def park_all() -> None:
    for d in list_ts_deploys():
        scale(d, 0)


def wait_available(deploy: str, timeout: int = 420) -> bool:
    rc, _, _ = _kubectl(
        ["wait", "--for=condition=available", f"deploy/{deploy}", f"--timeout={timeout}s"],
        timeout=timeout + 15,
    )
    return rc == 0


# --- single-pod actions -------------------------------------------------------

def describe(pod: str) -> str:
    rc, out, err = _kubectl(["describe", "pod", pod], timeout=20)
    return out if rc == 0 else (err or "describe failed")


def delete_pod(pod: str) -> tuple[int, str, str]:
    return _kubectl(["delete", "pod", pod], timeout=60)


def logs_cmd(pod: str, tail: int = 200, follow: bool = True) -> list[str]:
    cmd = ["kubectl", "-n", NS, "logs", pod, f"--tail={tail}"]
    if follow:
        cmd.append("-f")
    return cmd


# --- nacos / site health (mirror script lines 33-37, 118-119) -----------------

def nodeport(svc: str) -> int | None:
    rc, out, _ = _kubectl(
        ["get", "svc", svc, "-o", "jsonpath={.spec.ports[0].nodePort}"], timeout=10,
    )
    out = out.strip()
    return int(out) if out.isdigit() else None


def _http_ok(url: str, timeout: int = 8) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def _http_json(url: str, timeout: int = 8):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def site_healthy(ip: str | None = None) -> bool:
    ip = ip or minikube_ip()
    if not ip:
        return False
    return _http_ok(f"http://{ip}:{model.UI_NODEPORT}/api/v1/stationservice/stations")


def nacos_count(ip: str | None = None) -> int:
    ip = ip or minikube_ip()
    if not ip:
        return 0
    nport = nodeport("nacos")
    if not nport:
        return 0
    data = _http_json(
        f"http://{ip}:{nport}/nacos/v1/ns/service/list?pageNo=1&pageSize=100"
    )
    if isinstance(data, dict):
        return int(data.get("count", 0))
    return 0


# --- infra readiness (script lines 74-80) -------------------------------------

def infra_ready(pods: list[dict] | None = None) -> tuple[bool, dict]:
    pods = pods if pods is not None else list_pods()
    db = sum(
        1 for p in pods
        if "mysql" in p["name"] and p["ready"] == 3 and p["total"] == 3
        and p["phase"] == "Running"
    )
    nacos = sum(
        1 for p in pods
        if p["name"].startswith("nacos-") and p["all_ready"] and p["phase"] == "Running"
    )
    mq = sum(
        1 for p in pods
        if p["name"].startswith("rabbitmq") and p["all_ready"] and p["phase"] == "Running"
    )
    counts = {"mysql": db, "nacos": nacos, "rabbitmq": mq}
    return (db >= 6 and nacos >= 3 and mq >= 1), counts


# --- mysql / consign-price (script lines 100-112) -----------------------------

def mysql_leader() -> str | None:
    rc, out, _ = _kubectl(
        ["get", "pods", "-l", "app=tsdb-mysql", "-L", "role", "--no-headers"], timeout=15,
    )
    if rc != 0:
        return None
    for line in out.splitlines():
        cols = line.split()
        if cols and cols[-1] == "leader":
            return cols[0]
    return None


def db_has_leader(sts: str) -> bool:
    """True if the xenon MySQL cluster `sts` has an elected leader — checked via
    the `{sts}-leader` Service endpoint (empty == no leader, which makes
    write-dependent clients fail with 'Connection refused')."""
    rc, out, _ = _kubectl(
        ["get", "endpoints", f"{sts}-leader", "-o",
         "jsonpath={.subsets[*].addresses[*].ip}"], timeout=10,
    )
    return rc == 0 and bool(out.strip())


def tsdb_has_leader() -> bool:
    """Back-compat: leader check for the app DB."""
    return db_has_leader("tsdb-mysql")


def mysql_cluster_status(sts: str) -> dict:
    """Parse `xenoncli cluster status` for one MySQL cluster → per-node Raft state
    (LEADER/FOLLOWER/INVALID/CANDIDATE/IDLE) and read/write capability
    (RW=READWRITE, RO=READONLY). Creds-free (uses the xenon sidecar). Returns:
      {ok, nodes:[{name,raft,rw}], leaders, readwrite, all_followers, healthy}
    healthy == exactly one LEADER and exactly one READWRITE node."""
    rc, out, err = _kubectl(
        ["exec", f"{sts}-0", "-c", "xenon", "--",
         "xenoncli", "cluster", "status"], timeout=25,
    )
    if rc != 0:
        return {"ok": False, "error": (err or out).strip()[:200],
                "nodes": [], "leaders": 0, "readwrite": 0,
                "all_followers": False, "healthy": False}
    nodes = []
    for line in out.splitlines():
        if f"{sts}-" not in line or ":8801" not in line:
            continue
        cells = [c.strip() for c in line.split("|")]
        idcell = next((c for c in cells if c.startswith(sts)), "")
        name = idcell.split(".")[0]
        raftcell = next((c for c in cells if "@" in c), "")
        m = re.search(r"@(\w+)", raftcell)
        raft = m.group(1) if m else "?"
        rwcell = next((c for c in cells if "READONLY" in c or "READWRITE" in c), "")
        rw = "RW" if "READWRITE" in rwcell else ("RO" if "READONLY" in rwcell else "?")
        if name:
            nodes.append({"name": name, "raft": raft, "rw": rw})
    leaders = sum(1 for n in nodes if n["raft"] == "LEADER")
    readwrite = sum(1 for n in nodes if n["rw"] == "RW")
    return {
        "ok": True, "nodes": nodes, "leaders": leaders, "readwrite": readwrite,
        "all_followers": leaders == 0 and len(nodes) > 0,
        "healthy": leaders == 1 and readwrite == 1,
    }


def dbs_needing_leader() -> list[str]:
    """Which of the two MySQL clusters currently have NO leader (endpoint empty)."""
    return [sts for sts in MYSQL_DBS if not db_has_leader(sts)]


def fix_mysql_election(sts: str = "tsdb-mysql", on_line=None) -> bool:
    """Recover a wedged xenon Raft (all-followers / no leader) for cluster `sts`.

    Tries the gentle path (re-enable raft + propose a leader); if no leader
    appears, falls back to restarting the StatefulSet pods (data persists on
    PVCs) which re-bootstraps the election. Returns True if a leader exists at
    the end. IMPORTANT: the pod restart is CPU/memory heavy — only run it when
    services are parked (e.g. during the infra phase), never on a full node."""
    def log(msg):
        if on_line:
            on_line(msg)

    if db_has_leader(sts):
        log(f"{sts} already has a leader.")
        return True

    log(f"no {sts} leader — re-enabling raft on each node…")
    for n in (0, 1, 2):
        _kubectl(["exec", f"{sts}-{n}", "-c", "xenon", "--",
                  "xenoncli", "raft", "enable"], timeout=20)
    log(f"proposing {sts}-2 as leader…")
    _kubectl(["exec", f"{sts}-2", "-c", "xenon", "--",
              "xenoncli", "raft", "trytoleader"], timeout=20)
    for _ in range(4):
        time.sleep(4)
        if db_has_leader(sts):
            log("leader elected via raft re-enable.")
            return True

    log(f"still no leader — restarting {sts} pods to re-bootstrap raft…")
    delete_pods_by_label(f"app={sts}")
    _kubectl(["wait", "--for=condition=ready", "pod", "-l", f"app={sts}",
              "--timeout=180s"], timeout=200)
    for _ in range(8):
        time.sleep(5)
        if db_has_leader(sts):
            log("leader elected after restart.")
            return True
    log(f"{sts} still has no leader — may need PVC reset (guide Part 9).")
    return False


def exec_sql(pod: str, sql: str) -> tuple[int, str, str]:
    return _kubectl(
        ["exec", pod, "-c", "mysql", "--", "mysql",
         f"-u{model.MYSQL_USER}", f"-p{model.MYSQL_PASS}", "ts", "-e", sql],
        timeout=30,
    )


def delete_pods_by_label(selector: str) -> tuple[int, str, str]:
    return _kubectl(["delete", "pod", "-l", selector], timeout=60)


# --- hardening (installation-guide Part 4) ------------------------------------

def nacos_armor() -> list[tuple[str, tuple[int, str, str]]]:
    steps = []
    steps.append((
        "set env JVM heap",
        _kubectl(["set", "env", "statefulset/nacos",
                  "JVM_XMX=512m", "JVM_XMS=512m", "JVM_XMN=256m"], timeout=30),
    ))
    patch = (
        '[{"op":"replace","path":"/spec/template/spec/containers/0/resources",'
        '"value":{"requests":{"cpu":"1","memory":"1536Mi"},'
        '"limits":{"cpu":"1","memory":"1536Mi"}}}]'
    )
    steps.append((
        "patch resources",
        _kubectl(["patch", "statefulset", "nacos", "--type=json", "-p", patch], timeout=30),
    ))
    return steps


def sign_memory_leases(on_each=None) -> list[tuple[str, bool]]:
    """Cap heap + memory on every ts-* deployment. `on_each(i, total, deploy)` is
    called after each one so callers can show progress."""
    deploys = list_ts_deploys()
    total = len(deploys)
    results = []
    for i, d in enumerate(deploys, 1):
        rc1, _, _ = _kubectl(["set", "env", f"deploy/{d}",
                              "JAVA_TOOL_OPTIONS=-Xmx300m"], timeout=30)
        rc2, _, _ = _kubectl(["set", "resources", f"deploy/{d}",
                              "--requests=cpu=100m,memory=200Mi",
                              "--limits=memory=700Mi"], timeout=30)
        results.append((d, rc1 == 0 and rc2 == 0))
        if on_each:
            on_each(i, total, d)
    return results


def xenon_status() -> str:
    rc, out, err = _kubectl(
        ["exec", "nacosdb-mysql-0", "-c", "xenon", "--", "xenoncli", "cluster", "status"],
        timeout=20,
    )
    return out if rc == 0 else (err or "xenoncli failed")

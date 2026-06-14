"""Wave-boot + smart-start state machine.

`BootController.run_smart()` diagnoses the cluster and runs only the phases that
are needed (create / install / deploy / harden / wave-boot). Each phase reports
progress through an `emit(Progress)` callback that carries a `stage` and an
optional `fraction` (0..1) so the GUI can draw a per-stage percentage bar. Blocking
`cluster.*` calls run in threads; cancellation propagates via CancelledError.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Callable

from . import cluster, model

# Stages rendered as rows in the progress panel, in order.
STAGES = ["minikube", "install", "deploy", "harden",
          "park", "infra", "wave-boot", "consign", "verdict"]

# Back-compat aliases (older code referenced these).
PHASES = STAGES
SETUP_PHASES = ["install", "deploy", "harden"]

_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")


@dataclass
class Progress:
    phase: str                       # == stage key
    message: str
    ready: int = 0
    total: int = model.SERVICE_COUNT
    level: str = "info"              # info | ok | warn | error
    done_phases: list[str] = field(default_factory=list)
    fraction: float | None = None    # 0..1 for the active stage bar; None = pulse


EmitFn = Callable[[Progress], None]


async def _to_thread(fn, *args):
    return await asyncio.to_thread(fn, *args)


class BootController:
    def __init__(self, emit: EmitFn):
        self._emit = emit
        self._done: list[str] = []
        self._ready = 0

    def _say(self, phase, message, level="info", fraction=None):
        self._emit(Progress(
            phase=phase, message=message, ready=self._ready, level=level,
            done_phases=list(self._done), fraction=fraction,
        ))

    def _finish_phase(self, phase: str):
        if phase not in self._done:
            self._done.append(phase)

    # =========================================================================
    # Smart entry point: diagnose, then run only what's needed.
    # =========================================================================
    async def run_smart(self):
        try:
            await self._phase_minikube()
            state = await _to_thread(cluster.diagnose)
            self._say("minikube", f"Diagnosis: {state.summary}", "ok")

            if not state.installed:
                await self._phase_install()
                await self._phase_deploy()
            else:
                self._finish_phase("install")
                self._finish_phase("deploy")

            if (not state.hardened) or (not state.infra_ok) or state.crash > 0:
                await self._phase_harden()
            else:
                self._finish_phase("harden")

            await self._phase_park()
            await self._phase_infra()
            await self._phase_waveboot()
            await self._phase_consign()
            await self._phase_verdict()
        except asyncio.CancelledError:
            self._say("stopped", "Cancelled by user.", "warn")
            raise

    # =========================================================================
    # Phases
    # =========================================================================
    # --- minikube (create sized / restart) + cpu cap + API --------------------
    async def _phase_minikube(self):
        cmd = await _to_thread(cluster.minikube_start_cmd)
        creating = "--memory" in " ".join(cmd)
        if not await _to_thread(cluster.minikube_running):
            verb = "Creating cluster (sized)" if creating else "Starting minikube"
            self._say("minikube", f"{verb}: {' '.join(cmd)}", fraction=0.0)

            def on_line(ln):
                if not ln.strip():
                    return
                m = _PCT.search(ln)
                frac = float(m.group(1)) / 100.0 if m else None
                self._say("minikube", "  " + ln, fraction=frac)

            rc = await _to_thread(cluster.stream_run, cmd, on_line, 900, None)
            if rc != 0:
                self._say("minikube", f"minikube start failed (rc={rc}).", "error")
                raise RuntimeError("minikube start failed")
        else:
            self._say("minikube", "minikube already running.", fraction=0.9)

        await _to_thread(cluster.set_cpu_cap)
        self._say("minikube", f"CPU cap asserted ({max(1, cluster.nproc() - 4)} cores).")

        for i in range(60):
            if await _to_thread(cluster.api_up):
                break
            if i % 5 == 0:
                self._say("minikube", f"  …waiting for Kubernetes API ({i * 3}s)")
            await asyncio.sleep(3)
        if not await _to_thread(cluster.api_up):
            self._say("minikube", "API server did not come up.", "error")
            raise RuntimeError("API down")
        self._say("minikube", "Kubernetes API is up.", "ok", fraction=1.0)
        self._finish_phase("minikube")

    # --- install (helm) -------------------------------------------------------
    async def _phase_install(self):
        if await _to_thread(cluster.helm_installed):
            self._say("install", "helm release already present — skipping.", "ok",
                      fraction=1.0)
            self._finish_phase("install")
            return
        root = await _to_thread(cluster.repo_root)
        self._say("install", f"helm install train-ticket (chart: {root})", fraction=0.1)
        rc = await _to_thread(
            cluster.stream_run, cluster.helm_install_cmd(),
            lambda ln: self._say("install", "  " + ln) if ln.strip() else None,
            600, root,
        )
        if rc != 0:
            self._say("install", f"helm install failed (rc={rc}).", "error")
            raise RuntimeError("helm install failed")
        self._say("install", "chart installed.", "ok", fraction=1.0)
        self._finish_phase("install")

    # --- deploy (Job builds DBs + 46 deployments; images pull here) -----------
    async def _phase_deploy(self):
        job = "train-ticket-deploy"
        self._say("deploy", "Waiting for the deploy Job to appear…", fraction=0.0)
        for _ in range(60):
            if await _to_thread(cluster.deploy_job_exists, job):
                break
            await asyncio.sleep(5)
        self._say("deploy", "Building databases + deployments (images download here)…")
        # Key on workloads existing + infra pods present — NOT job 'complete'
        # (this chart's deploy Job stays active and never reports complete).
        for _ in range(360):  # up to ~60 min
            n = await _to_thread(cluster.deploy_count)
            pods = await _to_thread(cluster.list_pods)
            infra_seen = sum(
                1 for p in pods
                if any(p["name"].startswith(pref) for pref in model.INFRA_PREFIXES)
            )
            running = sum(1 for p in pods if p["state"] == model.READY)
            frac = min(1.0, n / model.SERVICE_COUNT) if n else 0.05
            self._say("deploy",
                      f"deployments {n}/{model.SERVICE_COUNT}, "
                      f"pods {running} running / {len(pods)} total, "
                      f"infra {infra_seen}",
                      fraction=frac)
            if n >= 40 and infra_seen >= 6:
                self._say("deploy",
                          f"workloads created ({n} deployments, {infra_seen} infra pods).",
                          "ok", fraction=1.0)
                self._finish_phase("deploy")
                return
            await asyncio.sleep(10)
        self._say("deploy", "deploy Job did not create workloads in time.", "error")
        raise RuntimeError("deploy timed out")

    # --- harden (Part 4: park + nacos armor + leases + restart nacos) ---------
    async def _phase_harden(self):
        self._say("harden", "Part 4 hardening — parking services…", fraction=0.05)
        await _to_thread(cluster.park_all)

        self._say("harden", "Armoring nacos (512m heap + guaranteed memory)…",
                  fraction=0.1)
        for label, (rc, out, err) in await _to_thread(cluster.nacos_armor):
            self._say("harden", f"  nacos {label}: "
                      + ("ok" if rc == 0 else f"FAILED {(err or out).strip()}"),
                      "info" if rc == 0 else "error")

        # restart nacos onto the armored spec so OOM-crashing members recover
        self._say("harden", "Restarting nacos members onto the armored spec…")
        await _to_thread(cluster.delete_pods_by_label, "app=nacos")

        self._say("harden", "Signing memory leases on all ts-* services…", fraction=0.2)

        def on_each(i, total, d):
            self._say("harden", f"  leased {d} ({i}/{total})",
                      fraction=0.2 + 0.8 * (i / total))

        results = await _to_thread(cluster.sign_memory_leases, on_each)
        bad = [d for d, ok in results if not ok]
        self._say("harden",
                  f"hardening done — {len(results) - len(bad)}/{len(results)} leases ok.",
                  "ok" if not bad else "warn", fraction=1.0)
        self._finish_phase("harden")

    # --- park -----------------------------------------------------------------
    async def _phase_park(self):
        self._say("park", "Parking all application services (scale 0)…")
        await _to_thread(cluster.park_all)
        self._say("park", "All ts-* services parked.", "ok", fraction=1.0)
        self._finish_phase("park")

    # --- infra ----------------------------------------------------------------
    async def _phase_infra(self):
        self._say("infra", "Waiting for infrastructure (MySQL x6, nacos x3, rabbitmq)…")
        election_fixed = False
        for i in range(90):
            pods = await _to_thread(cluster.list_pods)
            ok, counts = cluster.infra_ready(pods)
            ready_units = counts["mysql"] + counts["nacos"] + counts["rabbitmq"]
            self._say(
                "infra",
                f"MySQL {counts['mysql']}/6, nacos {counts['nacos']}/3, "
                f"rabbitmq {counts['rabbitmq']}/1…",
                fraction=min(1.0, ready_units / 10.0),
            )
            # Once MySQL pods exist, repair any DB (tsdb AND nacosdb) with no
            # elected leader. Safe here — services are parked (memory free).
            if not election_fixed and counts["mysql"] >= 6:
                need = await _to_thread(cluster.dbs_needing_leader)
                if need:
                    self._say("infra",
                              f"MySQL pods up but no leader on {'+'.join(need)} — "
                              "repairing xenon election…", "warn")
                    for sts in need:
                        await _to_thread(
                            cluster.fix_mysql_election, sts,
                            lambda ln: self._say("infra", "  " + ln))
                    election_fixed = True
            if ok and not await _to_thread(cluster.dbs_needing_leader):
                self._say("infra", "Infrastructure green (both DB leaders present).",
                          "ok", fraction=1.0)
                self._finish_phase("infra")
                return
            await asyncio.sleep(10)
        self._say("infra", "Infrastructure not green after 15 min.", "error")
        raise RuntimeError("infra not ready")

    # --- wave-boot ------------------------------------------------------------
    async def _phase_waveboot(self):
        for i, (title, services) in enumerate(model.BATCHES, start=1):
            self._say("wave-boot",
                      f"Batch {i}/6 ({title}): releasing {len(services)} services…",
                      fraction=self._ready / model.SERVICE_COUNT)
            for d in services:
                await _to_thread(cluster.scale, d, 1)
            for d in services:
                self._say("wave-boot", f"  waiting for {d}…",
                          fraction=self._ready / model.SERVICE_COUNT)
                ok = await _to_thread(cluster.wait_available, d, 420)
                if ok:
                    self._ready += 1
                    self._say("wave-boot", f"  {d} ✔ ({self._ready}/{model.SERVICE_COUNT})",
                              fraction=self._ready / model.SERVICE_COUNT)
                else:
                    self._say("wave-boot",
                              f"  {d} not ready in 7 min — continuing.", "warn")
            self._say("wave-boot", f"Batch {i}/6 done.", "ok")
        ip = await _to_thread(cluster.minikube_ip)
        nport = await _to_thread(cluster.nodeport, "nacos")
        gport = await _to_thread(cluster.nodeport, "ts-gateway-service")
        self._say("wave-boot",
                  f"ports: nacos={nport} gateway={gport} ui={model.UI_NODEPORT}",
                  fraction=1.0)
        self._finish_phase("wave-boot")

    # --- consign-price known bug ----------------------------------------------
    async def _phase_consign(self):
        svc = "ts-consign-price-service"
        if await _to_thread(cluster.wait_available, svc, 30):
            self._finish_phase("consign")
            return
        self._say("consign",
                  "ts-consign-price-service stuck — applying known-bug workaround.",
                  "warn")
        leader = await _to_thread(cluster.mysql_leader)
        if not leader:
            self._say("consign", "Could not find tsdb leader — fix manually.", "warn")
            self._finish_phase("consign")
            return
        await _to_thread(cluster.exec_sql, leader,
                         "DELETE FROM consign_price WHERE idx=0;")
        await _to_thread(cluster.delete_pods_by_label, "app=ts-consign-price-service")
        if await _to_thread(cluster.wait_available, svc, 300):
            self._say("consign", "consign-price recovered.", "ok")
        else:
            self._say("consign", "consign-price still down — check logs.", "warn")
        self._finish_phase("consign")

    # --- verdict --------------------------------------------------------------
    async def _phase_verdict(self):
        self._say("verdict", "Final health check…")
        ip = await _to_thread(cluster.minikube_ip)
        pods = await _to_thread(cluster.list_pods)
        not_ready = cluster.not_ready_count(pods)
        healthy = await _to_thread(cluster.site_healthy, ip)
        reg = await _to_thread(cluster.nacos_count, ip)

        if healthy and not_ready == 0 and reg > 30:
            self._say("verdict",
                      f"ALL GREEN — pods ready, registry has {reg} services, API serving. "
                      f"UI: http://{ip}:{model.UI_NODEPORT}", "ok", fraction=1.0)
        elif healthy:
            self._say("verdict",
                      f"Site serving, but {not_ready} pod(s) not ready / registry={reg}. "
                      "Likely still settling.", "warn", fraction=1.0)
        else:
            self._say("verdict", "Site NOT serving. Inspect pods / guide Part 9.",
                      "error", fraction=1.0)
        self._finish_phase("verdict")

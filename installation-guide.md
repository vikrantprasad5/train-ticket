# Train Ticket — Installation Guide (Battle-Tested)

This is the single, self-contained guide for deploying the Train Ticket system
(46 microservices) on a local Kubernetes cluster, **on any reasonably equipped
Linux machine** — distilled from a week of real deployments, three OOM storms, one
frozen desktop, a split service registry, and one genuine source-code bug. Every
command here has been run and verified; every fix exists because its failure mode
actually happened.

> **Tip:** open `installation-guide.html` in a browser for one-click copy buttons
> on every command block.

**What your machine needs:**

- **RAM: 32 GB strongly recommended** (the app's true working set is ~19 GB; the
  cluster gets 20 GB and your desktop keeps the rest). On 16 GB machines this
  deployment will not fit — don't try.
- **CPU: 8+ cores** (more is better; the cluster gets all but 4).
- **Disk: ~40 GB free** (≈10 GB of container images + database volumes).
- **OS:** Ubuntu 20.04/22.04+ (commands below use apt; adapt for other distros).
- **Network:** ~10 GB of downloads on first deploy — the first run is
  download-dominated, budget 1–2 hours mostly unattended.

**The flow:** install tools → create a capped cluster → helm install → let the
deploy Job build the databases → **harden before first launch** (this is the part
every other guide is missing) → wave-boot the services in 6 batches → verify →
operate.

---

## Part 1 — Install the tools (once per machine)

### 1a. Docker Engine

Kubernetes runs everything as containers, and minikube builds its entire "node"
inside one Docker container. Install from Docker's official repository:

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER
```

> **Then log out and back in** (or run `newgrp docker`) — the group change does
> not apply to existing sessions. Verify with `docker run hello-world`.

### 1b. kubectl

```bash
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.33/deb/Release.key | sudo gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
sudo chmod 644 /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.33/deb/ /' | sudo tee /etc/apt/sources.list.d/kubernetes.list
sudo apt-get update && sudo apt-get install -y kubectl
```

### 1c. minikube

```bash
curl -LO https://storage.googleapis.com/minikube/releases/latest/minikube_latest_amd64.deb
sudo dpkg -i minikube_latest_amd64.deb && rm minikube_latest_amd64.deb
```

### 1d. Helm — from the official binary release

> Do **not** use Helm's apt repository: its CDN (`baltocdn.com`) has served a
> broken signing key (the URL returns the text "OK" instead of a key). The binary
> release with checksum verification is the reliable path. Use the latest Helm 3
> (this chart is `apiVersion: v2`, Helm-3 era):

```bash
cd /tmp
curl -fsSLO https://get.helm.sh/helm-v3.21.0-linux-amd64.tar.gz
curl -fsSLO https://get.helm.sh/helm-v3.21.0-linux-amd64.tar.gz.sha256sum
sha256sum -c helm-v3.21.0-linux-amd64.tar.gz.sha256sum
tar -xzf helm-v3.21.0-linux-amd64.tar.gz
sudo install -m 0755 linux-amd64/helm /usr/local/bin/helm
rm -rf linux-amd64 helm-v3.21.0-linux-amd64.tar.gz*
cd -
```

Verify all four: `docker --version && kubectl version --client && helm version --short && minikube version`

---

## Part 2 — Create the cluster (with a real CPU wall)

The cluster gets all your cores except 4 (your desktop keeps those), and a hard
20 GB memory ceiling:

```bash
CPUS=$(( $(nproc) - 4 ))
minikube start --driver=docker --cpus=$CPUS --memory=20g
```

> **Critical gotcha:** with the docker driver, `--memory` is genuinely enforced
> but `--cpus` is **advisory only** — it tells the Kubernetes scheduler what to
> assume, while the container itself is created with *no* CPU limit and can seize
> every core on the host (verified: `NanoCpus: 0`; it froze a 20-core machine at
> load 1380). Enforce it for real:

```bash
docker update --cpus=$CPUS minikube
```

(Re-apply this after every `minikube delete` + recreate. A plain stop/start keeps it.)

Verify:

```bash
kubectl get nodes          # minikube: Ready
kubectl get storageclass   # standard (default) — this satisfies the app's PVCs
```

---

## Part 3 — Deploy the chart

Clone the fork that contains the required `.helmignore` fixes (upstream's chart
breaks if your editor drops index files in the repo — Helm refuses any file >5 MB):

```bash
git clone https://github.com/vikrantprasad5/train-ticket.git
cd train-ticket
helm install train-ticket . --namespace train-ticket --create-namespace
```

This is **one-time per cluster**: it writes the desired state, which survives every
stop/reboot. (Re-running it later errors with "cannot re-use a name" — harmless.)
The chart launches a deploy Job that builds two 3-replica MySQL clusters, a
3-member nacos registry, RabbitMQ, and then creates all 46 service deployments.

Watch it work, then wait for completion:

```bash
kubectl logs -n train-ticket job/train-ticket-deploy -f
```

```bash
kubectl wait --for=condition=complete -n train-ticket job/train-ticket-deploy --timeout=40m
```

> On a fresh cluster, the services the Job creates will sit in `ContainerCreating`
> downloading images — that's fine and expected; we're about to park them anyway.

Confirm the infrastructure is green (this gate matters — services booted against a
half-built database world just crash):

```bash
kubectl get pods -n train-ticket | grep -E "mysql|nacos|rabbitmq"
# expect: 6 pods at 3/3 Running, nacos-0/1/2 and rabbitmq at 1/1
```

---

## Part 4 — Harden BEFORE first launch (the part nobody tells you)

The deployment ships with two time bombs. Defuse both now, while nothing is
running.

### 4a. Park all application services

```bash
kubectl get deploy -n train-ticket -o name | grep "/ts-" | xargs -I{} kubectl scale {} --replicas=0 -n train-ticket
```

Within ~2 minutes only infrastructure pods remain (`kubectl get pods -n
train-ticket --no-headers | wc -l` → ~14).

### 4b. Armor the registry (nacos)

**Why:** nacos defaults to a 2 GB JVM heap per member with no container memory
limit. Once the node fills, the kernel's OOM killer executes the biggest
unprotected processes — the nacos members — wiping every service registration
(they're heartbeat leases, not records). Result: the gateway returns 503 for some
routes *while every pod shows Running*. A 512 MB heap is ample for this workload;
guaranteed resources make the OOM killer skip it entirely:

```bash
kubectl set env statefulset/nacos -n train-ticket JVM_XMX=512m JVM_XMS=512m JVM_XMN=256m
kubectl patch statefulset nacos -n train-ticket --type='json' -p='[{"op":"replace","path":"/spec/template/spec/containers/0/resources","value":{"requests":{"cpu":"1","memory":"1536Mi"},"limits":{"cpu":"1","memory":"1536Mi"}}}]'
kubectl rollout status statefulset/nacos -n train-ticket --timeout=10m
```

> 1536 Mi, not 1 Gi: nacos needs heap **plus** gRPC direct-memory buffers plus
> metaspace. At 1 Gi it OOMs against its own limit (verified the hard way). If the
> rollout sticks behind a crash-looping pod, `kubectl delete pod nacos-0 nacos-1
> nacos-2 -n train-ticket` unsticks it — the controller recreates them on the new
> spec.

### 4c. Sign memory leases for all 46 services

**Why:** the services ship with no heap setting and no memory limit. A JVM in an
unlimited container sizes its heap from the *node* (~25% of 20 GB ≈ 5 GB each — a
combined entitlement of ~230 GB). Heaps grow under use and never shrink, so the
node always fills eventually, and then any extra demand triggers the OOM cascade.
Measured: these services idle at ~250 MB. Cap them while they're parked (nothing
rolls):

```bash
for d in $(kubectl get deploy -n train-ticket -o name | grep "/ts-"); do
  kubectl set env -n train-ticket $d JAVA_TOOL_OPTIONS=-Xmx300m
  kubectl set resources -n train-ticket $d --requests=cpu=100m,memory=200Mi --limits=memory=700Mi
done
```

`JAVA_TOOL_OPTIONS` is read by every JVM automatically (the one nginx container,
ts-ui-dashboard, ignores it harmlessly). The 700 Mi hard limit means a misbehaving
service gets restarted alone at 700 Mi instead of dragging the node down — failure
stays contained.

---

## Part 5 — Wave-boot: release in 6 ordered batches

Never start all 46 JVMs at once on one node — simultaneous JVM boots saturate CPU,
health probes kill the slow, and the restart loop feeds itself (observed load:
1300+). Release ~8 at a time, dependency-ordered, waiting for each wave:

**Batch 1 — foundations:**

```bash
kubectl scale deploy ts-auth-service ts-user-service ts-verification-code-service ts-contacts-service ts-station-service ts-config-service ts-price-service ts-basic-service --replicas=1 -n train-ticket
kubectl wait --for=condition=available -n train-ticket deploy/ts-auth-service deploy/ts-user-service deploy/ts-verification-code-service deploy/ts-contacts-service deploy/ts-station-service deploy/ts-config-service deploy/ts-price-service deploy/ts-basic-service --timeout=20m
```

**Batch 2 — catalog and content:**

```bash
kubectl scale deploy ts-train-service ts-route-service ts-station-food-service ts-train-food-service ts-food-service ts-news-service ts-assurance-service ts-security-service --replicas=1 -n train-ticket
kubectl wait --for=condition=available -n train-ticket deploy/ts-train-service deploy/ts-route-service deploy/ts-station-food-service deploy/ts-train-food-service deploy/ts-food-service deploy/ts-news-service deploy/ts-assurance-service deploy/ts-security-service --timeout=20m
```

**Batch 3 — booking core:**

```bash
kubectl scale deploy ts-order-service ts-order-other-service ts-seat-service ts-travel-service ts-travel2-service ts-ticket-office-service ts-route-plan-service ts-travel-plan-service --replicas=1 -n train-ticket
kubectl wait --for=condition=available -n train-ticket deploy/ts-order-service deploy/ts-order-other-service deploy/ts-seat-service deploy/ts-travel-service deploy/ts-travel2-service deploy/ts-ticket-office-service deploy/ts-route-plan-service deploy/ts-travel-plan-service --timeout=20m
```

**Batch 4 — payment and booking flow:**

```bash
kubectl scale deploy ts-payment-service ts-inside-payment-service ts-preserve-service ts-preserve-other-service ts-cancel-service ts-rebook-service ts-execute-service ts-wait-order-service --replicas=1 -n train-ticket
kubectl wait --for=condition=available -n train-ticket deploy/ts-payment-service deploy/ts-inside-payment-service deploy/ts-preserve-service deploy/ts-preserve-other-service deploy/ts-cancel-service deploy/ts-rebook-service deploy/ts-execute-service deploy/ts-wait-order-service --timeout=20m
```

**Batch 5 — logistics and extras:**

```bash
kubectl scale deploy ts-consign-service ts-consign-price-service ts-delivery-service ts-food-delivery-service ts-notification-service ts-voucher-service ts-avatar-service ts-admin-basic-info-service --replicas=1 -n train-ticket
kubectl wait --for=condition=available -n train-ticket deploy/ts-consign-service deploy/ts-consign-price-service deploy/ts-delivery-service deploy/ts-food-delivery-service deploy/ts-notification-service deploy/ts-voucher-service deploy/ts-avatar-service deploy/ts-admin-basic-info-service --timeout=20m
```

**Batch 6 — admin and the front door:**

```bash
kubectl scale deploy ts-admin-order-service ts-admin-route-service ts-admin-travel-service ts-admin-user-service ts-gateway-service ts-ui-dashboard --replicas=1 -n train-ticket
kubectl wait --for=condition=available -n train-ticket deploy/ts-admin-order-service deploy/ts-admin-route-service deploy/ts-admin-travel-service deploy/ts-admin-user-service deploy/ts-gateway-service deploy/ts-ui-dashboard --timeout=20m
```

Safety net — release anything still parked:

```bash
kubectl get deploy -n train-ticket --no-headers | awk '$4==0 {print $1}' | xargs -r kubectl scale --replicas=1 -n train-ticket deploy
```

Notes: on a *fresh* cluster each batch first downloads its ~8 images, so waits are
long the first time — that's bandwidth, not failure. If one straggler in a batch
lags while the rest are green, proceed; it finishes alongside the next batch (but
see the consign-price known bug below).

---

## Part 6 — Verify end-to-end

Three layers, from "pods exist" to "the app answers":

```bash
# 6a. Pod summary — expect ~51 × "1/1 Running", 6 × "3/3 Running", 1 Completed
kubectl get pods -n train-ticket --no-headers | awk '{print $2, $3}' | sort | uniq -c
```

```bash
# 6b. Registry consistency — all three members must report the SAME count (~41)
for p in nacos-0 nacos-1 nacos-2; do
  kubectl exec -n train-ticket $p -- curl -s "http://localhost:8848/nacos/v1/ns/service/list?pageNo=1&pageSize=100" | python3 -c "import sys,json; print('$p:', json.load(sys.stdin)['count'], 'services')"
done
```

```bash
# 6c. Real requests through the gateway — both must return JSON, not 503
curl -s "http://$(minikube ip):32677/api/v1/stationservice/stations" | head -c 200; echo
curl -s -X POST "http://$(minikube ip):32677/api/v1/orderservice/order/refresh" -H "Content-Type: application/json" -d '{"loginId":"x","enableStateQuery":false,"enableTravelDateQuery":false,"enableBoughtDateQuery":false,"travelDateStart":null,"travelDateEnd":null,"boughtDateStart":null,"boughtDateEnd":null}'
```

Open the site:

```bash
minikube service ts-ui-dashboard -n train-ticket
```

Register a user, log in, search Shang Hai → Su Zhou, book a ticket. If 6a looks
healthy but 6c returns 503: the registry died — see Troubleshooting.

---

## Part 7 — Known bug: ts-consign-price-service crash-loops after reboots

**Symptom:** after any restart of an already-seeded system, every service comes up
except `ts-consign-price-service`, crash-looping with `Duplicate entry '0' for key
'UK_...'` then `Error starting ApplicationContext`.

**Root cause (source bug):** the service seeds a config row (`idx=0`) on every
boot, but `createAndModifyPrice()` in
`ts-consign-price-service/src/main/java/consignprice/service/ConsignPriceServiceImpl.java`
loads the existing row and then overwrites its primary key with a fresh random
UUID — turning the intended update into an insert that collides with the unique
`idx` constraint. Empty database: works. Seeded database: fails, every time.

**Workaround** — delete the seeded row; the next retry boots clean and re-seeds:

```bash
LEADER=$(kubectl get pods -n train-ticket -l app=tsdb-mysql -L role --no-headers | awk '$NF=="leader"{print $1}')
kubectl exec -n train-ticket $LEADER -c mysql -- mysql -uts -pTs_123456 ts -e "DELETE FROM consign_price WHERE idx=0;"
```

CrashLoopBackOff retries within ≤5 minutes; force an immediate retry with
`kubectl delete pod -n train-ticket -l app=ts-consign-price-service`.

**Real fix:** in `createAndModifyPrice()`, only set the id when creating a new
config object — never on the loaded one.

---

## Part 8 — Day-2 operations

- **One-command restart:** `./start-train-ticket.sh` (repo root) automates this
  whole section — minikube start, CPU cap, park, 6-batch wave-boot, the
  consign-price workaround, and a final health verdict. Idempotent: safe to run
  when already healthy. The notes below explain what it does and why.
- **Stop / start are safe.** `minikube stop` then `minikube start` — desired
  state, database volumes, and all the Part 4 hardening patches live in the
  cluster and survive. Never re-run `helm install` on an existing cluster. Only
  `minikube delete` destroys things — after which everything (including Part 4 and
  the CPU cap) must be redone.
- **After every cold start, repeat the park + wave-boot** (Parts 4a and 5 — the
  patches are already in place, so skip 4b/4c). With cached images each batch
  takes ~2–5 minutes. If you skip this, the kubelet launches all 46 at once; with
  the leases and CPU cap in place this self-heals through 20–30 minutes of
  crash-loop waves — survivable now, but the wave-boot is faster and calmer.
- **Never `kubectl rollout restart` all services at once** — a rolling restart
  keeps old pods alive until replacements are ready, briefly *doubling* the JVM
  count. Restart in batches of ~8 using the Part 5 groups.
- **Don't scale the database/nacos StatefulSets down on a live cluster.** They
  use raft-style elections; a lone survivor of 3 configured members can never win
  a majority vote and strands itself (MySQL read-only, "all followers"). Replica
  counts are an install-time decision.
- **And remember the consign-price workaround** (Part 7) after every reboot.

---

## Part 9 — Troubleshooting

- **Gateway 503s while every pod is Running** → registry wiped or split. Diagnose:
  run 6b — empty or inconsistent counts confirm it; also check
  `kubectl get pods -n train-ticket -o jsonpath='{range .items[*]}{.metadata.name}: {.status.containerStatuses[0].lastState.terminated.reason}{"\n"}{end}' | grep -v "^.*: $"`
  for `OOMKilled` on nacos. If the armor (4b) is applied this should no longer
  happen; if it does, re-check the armor survived (`kubectl get statefulset nacos
  -n train-ticket -o jsonpath='{.spec.template.spec.containers[0].resources}'`).
  Recovery: ensure nacos healthy, then batch-restart services so they re-register.
- **All MySQL pods report `role=follower`, no leader** → xenon can't elect.
  Check, in order: `kubectl exec -n train-ticket nacosdb-mysql-0 -c xenon --
  xenoncli cluster status` (who's eligible); the `mysql` container logs (a node
  with crashing mysqld can't be promoted); OOMKilled last-states (churn = elections
  never settle); and **stale PVCs** — `kubectl get pvc -n train-ticket` showing
  volumes older than the helm release means old data with old passwords; delete
  the release *and* the PVCs, reinstall.
- **`helm install` says "cannot re-use a name"** → the release already exists;
  this is a guardrail, not an error. Do nothing.
- **`helm install` says a chart file exceeds the 5 MB limit** → an editor/IDE
  dropped a large file (e.g. `.vscode/browse.vc.db`) into the repo, and the chart
  root is the repo root. Add the offending directory to `.helmignore` (this fork
  already ignores `.vscode/`, `.idea/`, `images/`, `tests/`).
- **`kubectl get pods -w` dies with "http2: client connection lost"** → your
  watch connection dropped under load; the cluster is fine. Re-run it.
- **Everything is `ContainerCreating` forever on first deploy** → images are
  downloading (~300 MB × 46 through your bandwidth). Watch progress:
  `kubectl get events -n train-ticket --sort-by=.lastTimestamp | grep -i pull | tail -5`.
- **Host load explodes / desktop freezes** → the CPU cap isn't applied (check
  `docker inspect minikube --format '{{.HostConfig.NanoCpus}}'` — `0` means
  uncapped). Apply `docker update --cpus=$(( $(nproc) - 4 )) minikube`. In a true
  emergency: `minikube stop` is always safe and always recovers.

---

## Timings to expect

- Fresh machine, first deploy: 1–2 h wall-clock (downloads dominate), ~20 min hands-on.
- Cold start of an existing cluster with park + wave-boot: ~15–25 min.
- Cold start without wave-boot (self-healing storm, post-hardening): 20–35 min, noisier.
- `minikube stop`: under a minute when idle; several minutes under load (let it finish).

Built and verified 2026-06-12 on Ubuntu 22.04, 20 cores / 31 GB RAM. This file
(`installation-guide.md`, with its browser twin `installation-guide.html`) is the
complete, self-contained procedure — every fix in it exists because its failure
mode actually happened.

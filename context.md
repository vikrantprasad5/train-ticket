# Train Ticket System — Context for Downstream Projects

> Copy this file into any project that observes, visualizes, or integrates with the
> Train Ticket deployment (e.g. a Kubernetes viewer, a traffic-visualization app).
> It describes what the underlying system is, how to reach every part of it, and the
> constraints an external tool must respect. Verified live on 2026-06-12.

## What this system is

A fork of FudanSELab's **Train Ticket** benchmark — a realistic train-ticket booking
platform built as **46 microservices** (mostly Java/Spring Boot + one nginx UI),
running on a **single-node minikube cluster** (docker driver) on a local Ubuntu
machine. It exists for learning, demonstration, and fault-injection experiments.
Source repo (with the battle-tested `installation-guide.md`):
`https://github.com/vikrantprasad5/train-ticket`

- Kubernetes namespace: **`train-ticket`** (everything lives here, except the
  monitoring stack in `kube-system`)
- Healthy steady state: ~51 service pods `1/1`, 6 database pods `3/3`, 1 `Completed`
  deploy job
- Node: capped at 16 CPUs (host has 20) and a hard 20 GB memory wall; true working
  set ≈ 19.5 GB — **the cluster is memory-tight by design; see Constraints**

## How to reach everything

The minikube node IP is **`192.168.49.2`** (stable for this cluster's lifetime; it
can change after `minikube delete`). All `NodePort` URLs below are
`http://192.168.49.2:<port>`.

- **Kubernetes API server:** `https://192.168.49.2:8443` (TLS + client certs from
  `~/.kube/config`, context `minikube`).
  **For web-based tools, the easiest bridge is `kubectl proxy --port=8001`**, which
  exposes the full REST API on `http://localhost:8001` with no auth/TLS — including
  the `watch=true` streaming endpoints a live viewer needs
  (e.g. `GET /api/v1/namespaces/train-ticket/pods?watch=true`).
- **Web UI (the booking site):** NodePort **32677** (pinned in manifests, survives
  rebuilds) → `http://192.168.49.2:32677`
- **API gateway (Spring Cloud Gateway):** all application REST traffic, routed as
  `/api/v1/{name}service/**` → service `ts-{name}-service`. Reachable through the
  UI's nginx proxy (`:32677/api/...`) or directly via its own NodePort (currently
  **30467** — auto-assigned, changes on rebuild; look up with
  `kubectl get svc ts-gateway-service -n train-ticket`).
- **Nacos service registry** (HTTP API, no auth): NodePort currently **31551**
  (auto-assigned — always look it up:
  `kubectl get svc nacos -n train-ticket -o jsonpath='{.spec.ports[0].nodePort}'`).
  Useful endpoints:
  `GET /nacos/v1/ns/service/list?pageNo=1&pageSize=100` (all registered services),
  `GET /nacos/v1/ns/instance/list?serviceName=ts-order-service` (live instances).
- **Prometheus:** NodePort **30003** (`kube-state-metrics` is also installed, so
  pod/deployment state metrics are available). **Grafana:** NodePort **31000**.
  These are the richest data sources for visualization projects.
- **MySQL (application data):** service `tsdb-mysql-leader:3306` (writes) /
  `tsdb-mysql-follower:3306` (reads), database **`ts`**, user **`ts`**, password
  **`Ts_123456`**. Not exposed outside the cluster — reach it via
  `kubectl port-forward svc/tsdb-mysql-leader -n train-ticket 3306:3306` or
  `kubectl exec` into a pod. (A second cluster, `nacosdb-mysql`, belongs to nacos —
  leave it alone.)
- **RabbitMQ:** service `rabbitmq:5672` (AMQP, in-cluster only). Used for async
  flows (e.g. order → notification).
- **flagd (feature flags / fault injection):** service `flagd:8013` (gRPC) /
  `:8016` (OFREP HTTP). Flag definitions live in the `flagd-config` ConfigMap —
  flags named `tt-feat-*` toggle injected faults in services.

## Service inventory (name : container port)

All services are `ClusterIP` on their own port, named `ts-<domain>-service`. The
gateway route for each is `/api/v1/<domain-without-dashes>service/**` (e.g.
`ts-order-service` → `/api/v1/orderservice/**`).

**Identity & users:** ts-auth-service:12340, ts-user-service:12342,
ts-verification-code-service:15678, ts-contacts-service:12347, ts-avatar-service:17001

**Master data:** ts-station-service:12345, ts-train-service:14567,
ts-route-service:11178, ts-price-service:16579, ts-config-service:15679,
ts-basic-service:15680 (composite lookups)

**Search & planning:** ts-travel-service:12346 (G/D high-speed trains),
ts-travel2-service:16346 (other trains), ts-travel-plan-service:14322,
ts-route-plan-service:14578, ts-ticket-office-service:16108

**Booking core:** ts-preserve-service:14568 (the main booking orchestrator),
ts-preserve-other-service:14569, ts-seat-service:18898, ts-order-service:12031,
ts-order-other-service:12032, ts-security-service:11188

**Payment:** ts-payment-service:19001, ts-inside-payment-service:18673

**Order lifecycle:** ts-cancel-service:18885, ts-rebook-service:18886,
ts-execute-service:12386 (ticket collection/entry), ts-wait-order-service (no
k8s Service — discovery-only, see Quirks)

**Food:** ts-food-service:18856, ts-station-food-service:18855,
ts-train-food-service:19999, ts-food-delivery-service (no k8s Service)

**Logistics:** ts-consign-service:16111, ts-consign-price-service:16110,
ts-delivery-service:18808

**Misc:** ts-notification-service:17853, ts-news-service:12862,
ts-assurance-service:18888, ts-voucher-service:16101

**Admin:** ts-admin-basic-info-service:18767, ts-admin-order-service:16112,
ts-admin-route-service:16113, ts-admin-travel-service:16114, ts-admin-user-service:16115

**Edge:** ts-gateway-service:18888 (NodePort 30467), ts-ui-dashboard:8080
(NodePort 32677, nginx serving the SPA and proxying `/api` to the gateway)

## How a request flows (the booking path)

Browser → `ts-ui-dashboard` (nginx) → `ts-gateway-service` (JWT auth check, route
lookup) → **Nacos** (resolve service name → pod IP) → `ts-preserve-service`, which
fans out to: contacts (passenger info) → basic/station/train/price (validation &
pricing) → seat (allocation) → order (record) → security (fraud checks) → then async
via **RabbitMQ** → notification. Data lands in **tsdb-mysql**. Cancel/rebook/payment
follow similar fan-out patterns. Inter-service calls are plain REST resolved through
Nacos (not Kubernetes DNS) — so **Nacos's registry is the live map of the system**,
and its instance-list API is the ground truth for "what can talk to what right now."

## Domain model & seed data (for visualization)

Core entities in `ts` database: users/contacts, stations, trains (types G/D = high
speed, others = slow), routes (ordered station lists with distances), travels
(train + route + schedule), orders (status lifecycle: not-paid → paid → collected →
used / canceled), seats, consigns, food orders. Seeded stations include shanghai,
suzhou, taiyuan, hangzhou, nanjing and more (query
`SELECT name FROM station` or `GET /api/v1/stationservice/stations`). The classic
demo flow: search Shang Hai → Su Zhou, book, pay, collect, enter station.
Admin UI login (upstream default): `admin@trainticket.com` / `222222`.

## Constraints external projects MUST respect

1. **Do not deploy workloads into this cluster.** Memory headroom is ~0.5 GB; any
   significant pod will trigger kernel OOM kills of application services. Run your
   viewer/game OUTSIDE the cluster (plain web app on the host) and observe via
   `kubectl proxy`, NodePorts, and Prometheus.
2. **Read, don't mutate.** kubectl access is admin-level; a viewer should treat the
   cluster as read-only. Anything that restarts services must follow the batch
   rules in `installation-guide.md` (never all at once; batches of ~8).
3. **Auto-assigned NodePorts change on rebuild** (nacos, gateway, grafana,
   prometheus). Only the UI's 32677 is pinned. Discover ports at startup via
   `kubectl get svc`, don't hardcode them.
4. **Expect restarts in pod history** — RESTARTS > 0 on databases/nacos is normal
   (storm history); a viewer should not flag these as current errors. Pod *ages*
   reset after the host reboots.
5. **The system self-heals slowly after cold starts** (20–30 min of
   CrashLoopBackOff waves if not wave-booted). A viewer during this window sees
   heavy churn — that's recovery, not an outage.
6. **No distributed tracing is deployed** (no Jaeger/Zipkin — a jaeger variant
   exists in the repo but is not installed). Request-flow visualization must be
   inferred from the static call graph + Nacos + metrics, or tracing must be added
   as its own (memory-budgeted!) project.

## Known bugs & quirks

- `ts-consign-price-service` crash-loops after any reboot of a seeded system
  (`Duplicate entry '0'` — source bug in its boot-time seeder; workaround in
  `installation-guide.md` Part 7).
- `ts-food-delivery-service` and `ts-wait-order-service` have Deployments but **no
  Kubernetes Service** — they register only in Nacos. Tools that enumerate
  `kubectl get svc` will miss them; tools that enumerate deployments or Nacos won't.
- Nacos registrations are heartbeat leases: if all 3 nacos pods restart, the
  registry empties until services re-register (gateway then 503s on uncached
  routes, while every pod still shows Running). Registry state ≠ pod state.
- All `ts-*` deployments carry `JAVA_TOOL_OPTIONS=-Xmx300m`, requests
  100m CPU / 200Mi, limits 700Mi memory; nacos runs guaranteed
  1 CPU / 1536Mi with a 512m heap. These are deliberate hardening patches — a
  viewer showing "limits" is showing intended state, and a rebuilt cluster needs
  them re-applied (installation-guide Part 4).

## Quick health probe (paste-ready)

```bash
kubectl get pods -n train-ticket --no-headers | awk '{print $2, $3}' | sort | uniq -c
curl -s "http://192.168.49.2:32677/api/v1/stationservice/stations" | head -c 120
NPORT=$(kubectl get svc nacos -n train-ticket -o jsonpath='{.spec.ports[0].nodePort}')
curl -s "http://192.168.49.2:$NPORT/nacos/v1/ns/service/list?pageNo=1&pageSize=100" | head -c 200
```

Healthy = ~51×`1/1 Running` + 6×`3/3 Running`, station JSON, and a ~41-service list.

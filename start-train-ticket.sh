#!/usr/bin/env bash
# start-train-ticket.sh — bring the train-ticket system from "stopped" to "serving"
# in one command: minikube start -> park -> 6-batch wave-boot -> consign-price fix
# -> health verdict. Idempotent: safe to run when already healthy (fast exit).
#
# Assumes the cluster was set up per installation-guide.md, INCLUDING the Part 4
# hardening (nacos armor + service memory leases). If you recreated the cluster
# with `minikube delete`, run the guide through Part 4 first — this script only
# boots, it does not re-apply patches.

set -uo pipefail

NS=train-ticket
CPUS=$(( $(nproc) - 4 ))

BATCHES=(
"ts-auth-service ts-user-service ts-verification-code-service ts-contacts-service ts-station-service ts-config-service ts-price-service ts-basic-service"
"ts-train-service ts-route-service ts-station-food-service ts-train-food-service ts-food-service ts-news-service ts-assurance-service ts-security-service"
"ts-order-service ts-order-other-service ts-seat-service ts-travel-service ts-travel2-service ts-ticket-office-service ts-route-plan-service ts-travel-plan-service"
"ts-payment-service ts-inside-payment-service ts-preserve-service ts-preserve-other-service ts-cancel-service ts-rebook-service ts-execute-service ts-wait-order-service"
"ts-consign-service ts-consign-price-service ts-delivery-service ts-food-delivery-service ts-notification-service ts-voucher-service ts-avatar-service ts-admin-basic-info-service"
"ts-admin-order-service ts-admin-route-service ts-admin-travel-service ts-admin-user-service ts-gateway-service ts-ui-dashboard"
)

log()  { printf '\033[1;34m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
ok()   { printf '\033[1;32m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
die()  { printf '\033[1;31m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; exit 1; }

api_up()     { kubectl get ns --request-timeout=8s >/dev/null 2>&1; }
minikube_ip(){ kubectl get node minikube -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null; }

site_healthy() {
  local ip; ip=$(minikube_ip) || return 1
  [ -n "$ip" ] || return 1
  curl -sf -m 8 -o /dev/null "http://$ip:32677/api/v1/stationservice/stations" 2>/dev/null
}

# ---------- fast path: already serving? ----------
if api_up && site_healthy; then
  NOT_READY=$(kubectl get pods -n $NS --no-headers 2>/dev/null | awk '$3!="Completed"{split($2,a,"/"); if (a[1]!=a[2]) c++} END{print c+0}')
  if [ "$NOT_READY" -eq 0 ]; then
    ok "System already fully healthy — nothing to do."
    ok "UI: http://$(minikube_ip):32677"
    exit 0
  fi
  warn "Site answers but $NOT_READY pod(s) not ready — continuing with recovery."
fi

# ---------- 1. cluster up ----------
log "Starting minikube (no-op if already running)..."
minikube start >/dev/null 2>&1 || die "minikube start failed — run 'minikube start' manually to see why."
docker update --cpus=$CPUS minikube >/dev/null 2>&1 && log "CPU cap asserted: $CPUS cores."

log "Waiting for the Kubernetes API..."
for _ in $(seq 1 60); do api_up && break; sleep 3; done
api_up || die "API server did not come up."

kubectl get deploy -n $NS >/dev/null 2>&1 || die "Namespace '$NS' missing — fresh cluster? Follow installation-guide.md from Part 3."

# ---------- 2. park everything ----------
log "Parking all application services..."
for _ in 1 2 3; do
  kubectl get deploy -n $NS -o name | grep "/ts-" | xargs -I{} kubectl scale {} --replicas=0 -n $NS >/dev/null 2>&1
  LEFT=$(kubectl get deploy -n $NS --no-headers 2>/dev/null | awk '$1 ~ /^ts-/ && $3+0 > 0' | wc -l)
  [ "$LEFT" = "0" ] && break
  sleep 5
done
ok "All ts-* services parked."

# ---------- 3. wait for infrastructure ----------
log "Waiting for infrastructure (MySQL x6 at 3/3, nacos x3, rabbitmq)... up to 15 min."
INFRA_OK=0
for _ in $(seq 1 90); do
  READY_DB=$(kubectl get pods -n $NS --no-headers 2>/dev/null | awk '/mysql/ && $2=="3/3" && $3=="Running"' | wc -l)
  READY_NACOS=$(kubectl get pods -n $NS --no-headers 2>/dev/null | awk '/^nacos-/ && $2=="1/1" && $3=="Running"' | wc -l)
  READY_MQ=$(kubectl get pods -n $NS --no-headers 2>/dev/null | awk '/^rabbitmq/ && $2=="1/1" && $3=="Running"' | wc -l)
  if [ "$READY_DB" -ge 6 ] && [ "$READY_NACOS" -ge 3 ] && [ "$READY_MQ" -ge 1 ]; then INFRA_OK=1; break; fi
  sleep 10
done
[ "$INFRA_OK" = 1 ] || die "Infrastructure not green after 15 min — inspect: kubectl get pods -n $NS"
ok "Infrastructure green."

# ---------- 4. wave-boot ----------
i=0
for b in "${BATCHES[@]}"; do
  i=$((i+1))
  log "Batch $i/6: releasing ${b%% *} +$(( $(wc -w <<<"$b") - 1 )) more..."
  kubectl scale deploy $b --replicas=1 -n $NS >/dev/null 2>&1
  for d in $b; do
    kubectl wait --for=condition=available -n $NS deploy/$d --timeout=420s >/dev/null 2>&1 \
      || warn "  $d not ready in 7 min — continuing (it may catch up; check later)."
  done
  ok "Batch $i/6 done."
done

# safety net: anything still parked
kubectl get deploy -n $NS --no-headers | awk '$4==0 {print $1}' | xargs -r kubectl scale --replicas=1 -n $NS deploy >/dev/null 2>&1

# ---------- 5. consign-price known bug ----------
if ! kubectl wait --for=condition=available -n $NS deploy/ts-consign-price-service --timeout=30s >/dev/null 2>&1; then
  warn "ts-consign-price-service stuck — applying the known-bug workaround (duplicate seed row)."
  LEADER=$(kubectl get pods -n $NS -l app=tsdb-mysql -L role --no-headers | awk '$NF=="leader"{print $1}')
  if [ -n "$LEADER" ]; then
    kubectl exec -n $NS "$LEADER" -c mysql -- mysql -uts -pTs_123456 ts -e "DELETE FROM consign_price WHERE idx=0;" 2>/dev/null
    kubectl delete pod -n $NS -l app=ts-consign-price-service >/dev/null 2>&1
    kubectl wait --for=condition=available -n $NS deploy/ts-consign-price-service --timeout=300s >/dev/null 2>&1 \
      && ok "consign-price recovered." || warn "consign-price still down — check its logs."
  else
    warn "Could not find tsdb leader — fix consign-price manually (installation-guide Part 7)."
  fi
fi

# ---------- 6. verdict ----------
log "Final health check..."
IP=$(minikube_ip)
PODS=$(kubectl get pods -n $NS --no-headers | awk '$3!="Completed"{split($2,a,"/"); if (a[1]!=a[2]) c++} END{print c+0}')
NPORT=$(kubectl get svc nacos -n $NS -o jsonpath='{.spec.ports[0].nodePort}' 2>/dev/null)
REG=$(curl -sf -m 8 "http://$IP:$NPORT/nacos/v1/ns/service/list?pageNo=1&pageSize=100" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['count'])" 2>/dev/null || echo 0)

if site_healthy && [ "$PODS" -eq 0 ] && [ "${REG:-0}" -gt 30 ]; then
  ok "ALL GREEN — pods ready, registry has $REG services, API serving."
  ok "UI: http://$IP:32677"
elif site_healthy; then
  warn "Site is serving, but $PODS pod(s) not ready / registry=$REG. Likely still settling — re-check in 5 min:"
  warn "  kubectl get pods -n $NS --no-headers | awk '{print \$2, \$3}' | sort | uniq -c"
else
  die "Site NOT serving. Inspect: kubectl get pods -n $NS  (and installation-guide.md Part 9)."
fi

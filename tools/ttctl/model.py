"""Static knowledge about the train-ticket cluster — single source of truth.

Mirrors start-train-ticket.sh (batch lists, infra thresholds) and the hardened
cluster state documented in installation-guide.md Part 4.
"""

NS = "train-ticket"
UI_NODEPORT = 32677  # pinned, stable across rebuilds
MYSQL_USER = "ts"
MYSQL_PASS = "Ts_123456"  # noqa: S105 — fixed local dev credential, same as the boot script

# The 6 wave-boot batches, verbatim from start-train-ticket.sh lines 16-23.
# (title, [deployment names]) — order matters, batches release sequentially.
BATCHES = [
    (
        "Foundations",
        [
            "ts-auth-service", "ts-user-service", "ts-verification-code-service",
            "ts-contacts-service", "ts-station-service", "ts-config-service",
            "ts-price-service", "ts-basic-service",
        ],
    ),
    (
        "Catalog & Content",
        [
            "ts-train-service", "ts-route-service", "ts-station-food-service",
            "ts-train-food-service", "ts-food-service", "ts-news-service",
            "ts-assurance-service", "ts-security-service",
        ],
    ),
    (
        "Booking Core",
        [
            "ts-order-service", "ts-order-other-service", "ts-seat-service",
            "ts-travel-service", "ts-travel2-service", "ts-ticket-office-service",
            "ts-route-plan-service", "ts-travel-plan-service",
        ],
    ),
    (
        "Payment & Booking Flow",
        [
            "ts-payment-service", "ts-inside-payment-service", "ts-preserve-service",
            "ts-preserve-other-service", "ts-cancel-service", "ts-rebook-service",
            "ts-execute-service", "ts-wait-order-service",
        ],
    ),
    (
        "Logistics & Extras",
        [
            "ts-consign-service", "ts-consign-price-service", "ts-delivery-service",
            "ts-food-delivery-service", "ts-notification-service", "ts-voucher-service",
            "ts-avatar-service", "ts-admin-basic-info-service",
        ],
    ),
    (
        "Admin & Front Door",
        [
            "ts-admin-order-service", "ts-admin-route-service", "ts-admin-travel-service",
            "ts-admin-user-service", "ts-gateway-service", "ts-ui-dashboard",
        ],
    ),
]

# Flat list of all application deployments, in boot order.
ALL_SERVICES = [svc for _, services in BATCHES for svc in services]
SERVICE_COUNT = len(ALL_SERVICES)  # 46

# Map a service name -> its batch index (1-based), for grouping the Pods grid.
SERVICE_BATCH = {
    svc: i + 1 for i, (_, services) in enumerate(BATCHES) for svc in services
}

# Infrastructure pod-name prefixes (not ts-*). Used to group the Pods grid and to
# gate the wave-boot on infra readiness (script lines 74-80).
INFRA_PREFIXES = ["tsdb-mysql", "nacosdb-mysql", "nacos", "rabbitmq", "flagd"]

# Raft/quorum StatefulSets — NEVER offer scale-down for these (memory: capacity rules).
QUORUM_PREFIXES = ["nacos", "tsdb-mysql", "nacosdb-mysql"]

# Services with documented known bugs (rendered specially in the grid).
KNOWN_BUGS = {
    "ts-consign-price-service": "Crash-loops on reboot (duplicate consign_price idx=0 seed row). "
    "Use Repair > Fix consign-price.",
}


def short_name(deploy: str) -> str:
    """ts-consign-price-service -> consign-price (for compact grid labels)."""
    name = deploy
    if name.startswith("ts-"):
        name = name[3:]
    if name.endswith("-service"):
        name = name[: -len("-service")]
    return name


def is_quorum(pod_or_deploy: str) -> bool:
    return any(pod_or_deploy.startswith(p) for p in QUORUM_PREFIXES)


# --- pod status -> Textual button variant / glyph -----------------------------
# Variants: "success" (green), "warning" (yellow), "error" (red), "default" (grey).

READY = "ready"
PENDING = "pending"
CRASH = "crash"
UNKNOWN = "unknown"

STATUS_VARIANT = {
    READY: "success",
    PENDING: "warning",
    CRASH: "error",
    UNKNOWN: "default",
}
STATUS_GLYPH = {READY: "●", PENDING: "◐", CRASH: "●", UNKNOWN: "○"}

_CRASH_REASONS = {
    "CrashLoopBackOff", "Error", "ImagePullBackOff", "ErrImagePull",
    "OOMKilled", "CreateContainerError", "RunContainerError",
}


def classify(pod: dict) -> str:
    """Bucket a pod dict (from cluster.list_pods) into READY/PENDING/CRASH/UNKNOWN."""
    status = pod.get("status", "")
    phase = pod.get("phase", "")
    if status in _CRASH_REASONS or pod.get("last_terminated_reason") in _CRASH_REASONS:
        # only crash if not currently fully ready
        if not pod.get("all_ready"):
            return CRASH
    if pod.get("all_ready") and phase == "Running":
        return READY
    if phase in ("Pending",) or status in (
        "ContainerCreating", "PodInitializing", "Pending",
    ):
        return PENDING
    if phase == "Running" and not pod.get("all_ready"):
        return PENDING
    if phase == "Succeeded":
        return READY  # Completed jobs
    return UNKNOWN

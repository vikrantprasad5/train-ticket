# Pre-Setup Guide: Running Train Ticket Locally

> **Tip:** for one-click copy buttons on every command, open [pre-setup.html](pre-setup.html)
> in a browser (e.g. `xdg-open pre-setup.html`). It renders this same guide with a
> **Copy** button on each code block. (On GitHub, this .md file gets copy buttons
> automatically.)

This guide installs everything needed to run the Train Ticket system (41 microservices)
on a **local Kubernetes cluster**, following the Helm-based quick start in the README.

**Target environment** (verified): Ubuntu 22.04.5 LTS, x86_64, 20 CPU cores, 31 GB RAM.
That is comfortably enough to run the full system locally.

**The dependency chain, in one line:**

> Docker (container runtime) → minikube (local Kubernetes cluster, runs inside Docker)
> → kubectl (talk to the cluster) + Helm (install the chart) → deploy Train Ticket.

Each step below explains *why* the tool is needed, gives the commands to run, and ends
with a quick verification. Run the commands one at a time, in order.

---

## Step 0 — Already present, nothing to do

`git` and `curl` are already installed. `curl` is used by several steps below to
download keys and binaries; `git` you already used to clone this repo.

---

## Step 1 — Docker Engine

**Why you need this:** Kubernetes runs applications as containers, and minikube (Step 3)
needs a container runtime to create its cluster in — with the `docker` driver, the
entire Kubernetes "node" is itself a Docker container, and every one of the 41
microservices runs as a container inside it. Without Docker, nothing else in this
guide works.

We install from Docker's official apt repository (not Ubuntu's outdated `docker.io`
package) so we get a current, supported version.

```bash
# 1a. Prerequisites for adding a third-party apt repository
sudo apt-get update
sudo apt-get install -y ca-certificates curl
```

```bash
# 1b. Add Docker's official GPG key (lets apt verify package signatures)
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
```

```bash
# 1c. Add the Docker apt repository for your Ubuntu release (jammy) and architecture
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
```

```bash
# 1d. Install Docker Engine + CLI
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

```bash
# 1e. Let your user run docker without sudo (required by minikube's docker driver,
#     which refuses to run as root)
sudo usermod -aG docker $USER
```

> **Important:** the group change only takes effect in a *new* session.
> Log out and back in (or run `newgrp docker` in the current shell) before continuing.

**Verify:**

```bash
docker run hello-world
```

You should see "Hello from Docker!" — this proves the daemon is running and your user
can talk to it without sudo.

---

## Step 2 — kubectl

**Why you need this:** `kubectl` is the standard CLI for talking to any Kubernetes
cluster. You'll use it constantly here: watching 41 pods come up, reading logs when a
service crashes, port-forwarding to reach the UI, and inspecting services/deployments.
Helm and minikube manage the cluster, but `kubectl` is how you *see* what's happening.

We install from the official Kubernetes apt repository (`pkgs.k8s.io`), pinned to a
stable minor version:

```bash
# 2a. Add the Kubernetes apt repository signing key
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.33/deb/Release.key | \
  sudo gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
sudo chmod 644 /etc/apt/keyrings/kubernetes-apt-keyring.gpg
```

```bash
# 2b. Add the repository itself
echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.33/deb/ /' | \
  sudo tee /etc/apt/sources.list.d/kubernetes.list
sudo chmod 644 /etc/apt/sources.list.d/kubernetes.list
```

```bash
# 2c. Install kubectl
sudo apt-get update
sudo apt-get install -y kubectl
```

**Verify:**

```bash
kubectl version --client
```

(Only `--client` for now — there is no cluster to talk to until Step 5.)

---

## Step 3 — minikube

**Why you need this:** you need an actual Kubernetes cluster, and minikube creates a
real single-node one on your machine (inside Docker). It was chosen over alternatives
(kind, k3s) for one Train-Ticket-specific reason: the deployment creates
**PersistentVolumeClaims** for its databases (MySQL), and minikube ships with a
built-in default StorageClass that satisfies PVCs automatically. This means you can
**skip the OpenEBS installation step from the README** — that step exists for clusters
without default storage.

```bash
# 3a. Download the latest minikube .deb package (x86_64)
curl -LO https://storage.googleapis.com/minikube/releases/latest/minikube_latest_amd64.deb
```

```bash
# 3b. Install it
sudo dpkg -i minikube_latest_amd64.deb
rm minikube_latest_amd64.deb
```

**Verify:**

```bash
minikube version
```

---

## Step 4 — Helm

**Why you need this:** this repository *is* a Helm chart ([Chart.yaml](Chart.yaml),
[values.yaml](values.yaml), [templates/](templates/)). Helm is Kubernetes' package
manager: it renders the chart's templates into Kubernetes manifests and installs them
in one command. In this fork, the chart installs a deploy Job that then brings up the
databases (MySQL, Nacos, RabbitMQ) and all 41 services — so Helm is the single entry
point for the whole deployment.

> **Note:** Helm's apt repository CDN (`baltocdn.com`) currently serves a broken
> signing key (the URL returns "OK" instead of the key), so we install the official
> binary release from `get.helm.sh` instead, verifying its checksum by hand — which
> is exactly what the apt GPG signature would have done for us. We use the latest
> Helm 3 release: this chart (`apiVersion: v2`) is from the Helm 3 era.

```bash
# 4a. Download the official Helm binary release and its checksum file
cd /tmp
curl -fsSLO https://get.helm.sh/helm-v3.21.0-linux-amd64.tar.gz
curl -fsSLO https://get.helm.sh/helm-v3.21.0-linux-amd64.tar.gz.sha256sum
```

```bash
# 4b. Verify the download wasn't corrupted or tampered with
sha256sum -c helm-v3.21.0-linux-amd64.tar.gz.sha256sum
```

```bash
# 4c. Unpack, install the single binary, and clean up
tar -xzf helm-v3.21.0-linux-amd64.tar.gz
sudo install -m 0755 linux-amd64/helm /usr/local/bin/helm
rm -rf linux-amd64 helm-v3.21.0-linux-amd64.tar.gz helm-v3.21.0-linux-amd64.tar.gz.sha256sum
cd ~/work/train-ticket
```

**Verify:**

```bash
helm version
```

---

## Step 5 — Start the cluster

**Why these flags matter:** by default minikube grabs only 2 CPUs / 2–4 GB RAM —
nowhere near enough for 41 mostly-JVM services plus databases. Your machine has
20 cores / 31 GB, so we give the cluster a generous slice while leaving room for the
host OS:

```bash
minikube start --driver=docker --cpus=12 --memory=20g
```

**Verify:**

```bash
# The node should be "Ready"
kubectl get nodes
```

```bash
# There should be a default StorageClass named "standard" — this is what
# lets us skip the README's OpenEBS step
kubectl get storageclass
```

---

## Step 6 — Deploy Train Ticket

**What happens here:** Helm installs the chart from the repo root. The chart creates a
deploy Job which, inside the cluster, installs the databases and then all the `ts-*`
services into the `train-ticket` namespace.

```bash
# From the repository root
helm install train-ticket . --namespace train-ticket --create-namespace
```

```bash
# Watch the pods come up (Ctrl-C to stop watching)
kubectl get pods -n train-ticket -w
```

> **Be patient:** the first deployment pulls dozens of container images and starts
> many JVMs. Expect 10–20+ minutes before everything is `Running`. A few restarts
> early on are normal — services crash-loop briefly until their databases are ready.

**Access the UI:** the dashboard is exposed as a NodePort service (port 32677).
The simplest way on minikube:

```bash
minikube service ts-ui-dashboard -n train-ticket
```

This prints (and opens) a URL to the Train Ticket web UI. Alternatively:

```bash
kubectl port-forward -n train-ticket svc/ts-ui-dashboard 8080:8080
# then open http://localhost:8080
```

---

## Cleanup — making experiments repeatable

```bash
# Remove the Train Ticket deployment but keep the cluster
helm uninstall train-ticket -n train-ticket
kubectl delete namespace train-ticket
```

```bash
# Stop the cluster (keeps its state; restart later with `minikube start`)
minikube stop
```

```bash
# Destroy the cluster entirely (fresh slate next time)
minikube delete
```

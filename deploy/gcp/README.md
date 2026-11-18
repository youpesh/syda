# Syda Platform — Google Cloud Platform (GCP) Deployment & Handoff Guide

This guide provides a turnkey, 1-command deployment of the **Syda Platform** on Google Cloud Platform. It provisions an automated Compute Engine VM running the production Docker Compose stack (FastAPI backend, React frontend, PostgreSQL database, and queue worker).

---

## 🚀 Quickstart: Deploy in 3 Minutes via Google Cloud Shell

The sponsor or administrator does not need to install local tools or share GCP credentials. Everything runs directly from **Google Cloud Shell** in the browser.

### Step 1: Open Google Cloud Shell
Visit: **[https://shell.cloud.google.com](https://shell.cloud.google.com)**

### Step 2: Run the Deployment Command
Paste and run the following command in the Cloud Shell terminal:

```bash
git clone https://github.com/youpesh/syda.git
cd syda
./deploy/gcp/deploy-vm.sh
```

### Step 3: Enter Your GCP Project ID
When prompted:
```text
Enter GCP Project ID [my-gcp-project]:
```
Press **Enter** to accept your active project, or type your preferred GCP Project ID.

---

## 🛠 What the Script Does Automatically

1. **Enables APIs:** Activates `compute.googleapis.com` in your GCP project.
2. **Configures Firewall:** Creates firewall rule `allow-syda-web` allowing ingress on ports `80`, `443`, and `3000`.
3. **Provisions VM:** Creates an `e2-medium` (2 vCPU, 4GB RAM) Ubuntu 22.04 LTS instance named `syda-platform` in `us-central1-a`.
4. **Installs Docker & Compose:** Automatically provisions Docker Engine and Compose plugin via startup script.
5. **Starts Production Stack:** Pulls pre-built production images from GitHub Container Registry:
   * `ghcr.io/youpesh/syda-frontend:latest` (Port 80 / 3000)
   * `ghcr.io/youpesh/syda-backend:latest` (Port 8000)
   * `ghcr.io/youpesh/syda-backend:latest` (`app.worker` queue)
   * `postgres:16-alpine` (Database)
6. **Outputs Live URL:** Prints the public IP address: `http://<EXTERNAL_IP>`

---

## 📋 Architecture & Estimated Costs

| Component | Resource | Description | Estimated Cost |
| :--- | :--- | :--- | :--- |
| **Compute VM** | Compute Engine `e2-medium` | 2 vCPU, 4GB RAM, 30GB Standard Persistent Disk | ~$24.50 / month *(covered by GCP $300 free trial)* |
| **Networking** | Standard Ingress / Egress | Port 80 / 443 web traffic | Free / minimal |
| **Database** | Dockerized PostgreSQL 16 | Persistent volume on VM disk | Included in VM disk |
| **Images** | GitHub Container Registry (GHCR) | Public production container images | Free |

---

## 🔍 Management & Verification

### Check Running Containers
SSH into the instance from Cloud Shell:
```bash
gcloud compute ssh syda-platform --zone=us-central1-a --command='docker ps'
```

### View Application Logs
```bash
# View backend logs
gcloud compute ssh syda-platform --zone=us-central1-a --command='docker logs -f syda-backend'

# View background worker logs
gcloud compute ssh syda-platform --zone=us-central1-a --command='docker logs -f syda-worker'
```

### Restart or Update Services
```bash
gcloud compute ssh syda-platform --zone=us-central1-a --command='cd /opt/syda && docker compose pull && docker compose up -d'
```

---

## 🗑 Cleanup / Teardown

If you ever wish to stop or delete the instance:

```bash
# Stop the VM (pauses compute billing)
gcloud compute instances stop syda-platform --zone=us-central1-a

# Delete the VM and disk entirely
gcloud compute instances delete syda-platform --zone=us-central1-a --quiet
```

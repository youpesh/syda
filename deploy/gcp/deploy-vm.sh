#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Syda Platform - Automated GCP Deployment Script
# Provisions a Google Compute Engine VM and deploys Syda using Docker Compose.
# Run this inside Google Cloud Shell (shell.cloud.google.com) or from any machine
# with gcloud CLI configured.
# ==============================================================================

echo "=================================================================="
echo "          Syda Platform — Google Cloud Deployment                "
echo "=================================================================="

# Check for gcloud
if ! command -v gcloud &> /dev/null; then
    echo "Error: gcloud CLI is not installed. Please run this in Google Cloud Shell:"
    echo "https://shell.cloud.google.com"
    exit 1
fi

# Detect or prompt for Project ID
CURRENT_PROJECT=$(gcloud config get-value project 2>/dev/null || true)
if [ -n "$CURRENT_PROJECT" ]; then
    read -rp "Enter GCP Project ID [$CURRENT_PROJECT]: " PROJECT_ID
    PROJECT_ID=${PROJECT_ID:-$CURRENT_PROJECT}
else
    read -rp "Enter GCP Project ID: " PROJECT_ID
fi

if [ -z "$PROJECT_ID" ]; then
    echo "Error: Project ID is required."
    exit 1
fi

gcloud config set project "$PROJECT_ID"

# Configuration variables
ZONE="us-central1-a"
VM_NAME="syda-platform"
MACHINE_TYPE="e2-medium"

echo ""
echo "Deployment Settings:"
echo "  Project ID:   $PROJECT_ID"
echo "  Zone:         $ZONE"
echo "  VM Name:      $VM_NAME"
echo "  Machine Type: $MACHINE_TYPE"
echo ""

# Enable Compute Engine API
echo "==> [1/4] Enabling Google Compute Engine API..."
gcloud services enable compute.googleapis.com --project="$PROJECT_ID"

# Create firewall rule for HTTP / Web traffic
echo "==> [2/4] Configuring Cloud Firewall rules for HTTP / HTTPS..."
if ! gcloud compute firewall-rules describe allow-syda-web --project="$PROJECT_ID" &>/dev/null; then
    gcloud compute firewall-rules create allow-syda-web \
        --project="$PROJECT_ID" \
        --direction=INGRESS \
        --priority=1000 \
        --network=default \
        --action=ALLOW \
        --rules=tcp:80,tcp:443,tcp:3000 \
        --source-ranges=0.0.0.0/0 \
        --target-tags=syda-web \
        --description="Allow public web traffic to Syda Platform"
    echo "Created firewall rule allow-syda-web."
else
    echo "Firewall rule allow-syda-web already exists."
fi

# Generate unique passwords for the instance
AUTH_SECRET=$(openssl rand -hex 32 2>/dev/null || python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null || echo "syda_production_auth_key_$(date +%s)")
POSTGRES_PASS=$(openssl rand -hex 16 2>/dev/null || python3 -c "import secrets; print(secrets.token_hex(16))" 2>/dev/null || echo "syda_pg_pass_$(date +%s)")

# Prepare VM Startup Script (runs automatically on VM boot)
STARTUP_SCRIPT=$(cat <<EOF
#!/bin/bash
set -ex

# Update and install Docker
apt-get update
apt-get install -y ca-certificates curl gnupg lsb-release
mkdir -p /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes
echo "deb [arch=\$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \$(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null

apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

systemctl enable docker
systemctl start docker

# Setup Syda directory
mkdir -p /opt/syda
cd /opt/syda

# Write production compose file
cat <<'COMPOSE' > /opt/syda/docker-compose.yml
services:
  postgres:
    image: postgres:16-alpine
    container_name: syda-postgres
    restart: unless-stopped
    environment:
      POSTGRES_DB: syda
      POSTGRES_USER: syda
      POSTGRES_PASSWORD: ${POSTGRES_PASS}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U syda -d syda"]
      interval: 5s
      timeout: 5s
      retries: 5

  backend:
    image: ghcr.io/youpesh/syda-backend:latest
    pull_policy: always
    container_name: syda-backend
    restart: unless-stopped
    environment:
      DATABASE_URL: postgresql://syda:${POSTGRES_PASS}@postgres:5432/syda
      AUTH_SECRET_KEY: ${AUTH_SECRET}
      COOKIE_SECURE: "false"
      CORS_ORIGINS: "*"
    volumes:
      - job_data:/app/platform/backend/data/jobs
      - settings_data:/app/platform/backend/data/settings
    depends_on:
      postgres:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]
      interval: 5s
      timeout: 5s
      retries: 30

  worker:
    image: ghcr.io/youpesh/syda-backend:latest
    pull_policy: always
    container_name: syda-worker
    restart: unless-stopped
    command: ["uv", "run", "python", "-m", "app.worker"]
    environment:
      DATABASE_URL: postgresql://syda:${POSTGRES_PASS}@postgres:5432/syda
      AUTH_SECRET_KEY: ${AUTH_SECRET}
    volumes:
      - job_data:/app/platform/backend/data/jobs
      - settings_data:/app/platform/backend/data/settings
    stop_grace_period: 65s
    depends_on:
      backend:
        condition: service_healthy

  frontend:
    image: ghcr.io/youpesh/syda-frontend:latest
    pull_policy: always
    container_name: syda-frontend
    restart: unless-stopped
    ports:
      - "80:3000"
      - "3000:3000"
    environment:
      PORT: 3000
      BACKEND_URL: http://backend:8000
    depends_on:
      - backend

volumes:
  postgres_data:
  job_data:
  settings_data:
COMPOSE

# Start services
docker compose -f /opt/syda/docker-compose.yml up -d
EOF
)

# Provision Compute Engine VM
echo "==> [3/4] Launching Compute Engine instance '$VM_NAME' in $ZONE..."
if gcloud compute instances describe "$VM_NAME" --zone="$ZONE" --project="$PROJECT_ID" &>/dev/null; then
    echo "Instance $VM_NAME already exists. Updating startup script and resetting..."
    gcloud compute instances add-metadata "$VM_NAME" \
        --zone="$ZONE" \
        --project="$PROJECT_ID" \
        --metadata=startup-script="$STARTUP_SCRIPT"
    gcloud compute instances reset "$VM_NAME" --zone="$ZONE" --project="$PROJECT_ID"
else
    gcloud compute instances create "$VM_NAME" \
        --project="$PROJECT_ID" \
        --zone="$ZONE" \
        --machine-type="$MACHINE_TYPE" \
        --image-family=ubuntu-2204-lts \
        --image-project=ubuntu-os-cloud \
        --boot-disk-size=30GB \
        --tags=syda-web,http-server,https-server \
        --metadata=startup-script="$STARTUP_SCRIPT"
fi

echo "==> [4/4] Fetching external IP address..."
sleep 5
EXTERNAL_IP=$(gcloud compute instances describe "$VM_NAME" \
    --zone="$ZONE" \
    --project="$PROJECT_ID" \
    --format='get(networkInterfaces[0].accessConfigs[0].natIP)')

echo ""
echo "=================================================================="
echo "          🎉 Syda Platform Successfully Deployed to GCP!          "
echo "=================================================================="
echo ""
echo "VM Name:     $VM_NAME"
echo "External IP: $EXTERNAL_IP"
echo ""
echo "Your platform is initializing containers (takes about 60-90 seconds)."
echo "Access the web application at:"
echo "  👉 http://$EXTERNAL_IP"
echo ""
echo "To check container logs on the VM:"
echo "  gcloud compute ssh $VM_NAME --zone=$ZONE --command='docker ps'"
echo "=================================================================="

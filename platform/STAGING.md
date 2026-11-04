# Staging Environment Setup & Deployment

This document describes the official, standard architecture for deploying Syda Platform to the **Staging** environment using **Dokploy**, **Tailscale**, **Cloudflare Tunnels**, and **GitHub Actions**.

---

## Architecture Overview

```
                      ┌───────────────────────────────────────────────┐
                      │              PUBLIC USERS / TESTERS           │
                      └──────────────────────┬────────────────────────┘
                                             │ HTTPS
                                             ▼
                                  https://staging.mlgate.ai
                                             │
                                    [Cloudflare Edge]
                                             │ (Encrypted Tunnel)
                                             ▼
┌─────────────────────────────────────── Dokploy Host ───────────────────────────────────────┐
│                                                                                            │
│   ┌──────────────────────────────── Staging Stack ─────────────────────────────────────┐   │
│   │                                                                                    │   │
│   │   [cloudflared] ──(internal http)──► [frontend:3000] ──► [backend:8000]            │   │
│   │                                                               │                    │   │
│   │                                                               ▼                    │   │
│   │                                                       [postgres:5432]              │   │
│   └────────────────────────────────────────────────────────────────────────────────────┘   │
│                                                                                            │
│   [Dokploy Management Panel] (Port 3000, private on Tailnet)                               │
│       ▲                                                                                    │
└───────┼────────────────────────────────────────────────────────────────────────────────────┘
        │
        │ Webhook POST (via Tailscale WireGuard)
        │
┌───────┴──────────────────────────────┐
│        GitHub Actions CI/CD          │
│  - Builds & pushes images to GHCR    │
│  - Connects to Tailscale             │
│  - Triggers Dokploy Deploy Webhook   │
└──────────────────────────────────────┘
```

### Key Highlights
1. **Public Ingress via Cloudflare Tunnel (`cloudflared`)**:
   - The application is publicly accessible at `https://staging.mlgate.ai`.
   - External users do **not** need Tailscale, VPN, or special accounts.
   - Zero ports (neither 80, 443, nor 3000) are opened on your router or host firewall.
   - Cloudflare provides free edge SSL termination and DDoS protection.
2. **Dokploy Native Multi-Environment**:
   - Inside Dokploy, Staging is a first-class environment within the `syda` project.
   - It runs completely isolated with its own Docker Compose stack, database, volumes, and passwords.
3. **CI/CD via GitHub Actions + Deploy Webhook**:
   - Runs manually on demand (`workflow_dispatch`) from any branch.
   - Builds production images on GitHub runners and pushes them to GitHub Container Registry (GHCR).
   - Connects to your private Tailscale network using the official `tailscale/github-action`.
   - Triggers Dokploy's native deployment webhook to pull images and update containers.

---

## Initial Setup Guide

### 1. Cloudflare DNS (One-time)
A Cloudflare Tunnel named `syda-staging` has already been created. Add a CNAME record in your Cloudflare dashboard for `mlgate.ai`:
* **Type:** `CNAME`
* **Name:** `staging` (or `staging.mlgate.ai`)
* **Target:** `b4f2d2d8-37f7-42ee-b1da-10340c2a19a1.cfargotunnel.com`
* **Proxy Status:** `Proxied` (Orange Cloud ON)

### 2. Dokploy Staging Service Setup
1. Log into your Dokploy dashboard.
2. Select your `syda` project (or create one).
3. Create an environment named **`staging`**.
4. Click **Create Service** → **Compose**.
5. Set Name: `syda-staging`.
6. Under **Source Type**, select **Raw** (or Git pointing to `platform/docker-compose.staging.yml`).
7. Paste the contents of `platform/docker-compose.staging.yml`.
8. In the **Environment** tab, set the required variables:
   ```env
   POSTGRES_PASSWORD=<generate-a-secure-random-password>
   AUTH_SECRET_KEY=<generate-a-secure-64-char-hex-key>
   CLOUDFLARE_TUNNEL_TOKEN=eyJhIjoiYjIxN2RjYzMxODY0ZjM5NGFkZWRlYTQ3N2Y2ODkxZmIiLCJ0IjoiYjRmMmQyZDgtMzdmNy00MmVlLWIxZGEtMTAzNDBjMmExOWExIiwicyI6Ik5kZjczdisyS1l4bER6cG9JRjZUeGN6Vm9FKzB2VWkydmtNeUxReEhNejQ9In0=
   ```
9. Under **Deployments**, copy the **Deploy Webhook URL**.

### 3. GitHub Secrets Configuration
In your GitHub repository, navigate to **Settings → Secrets and variables → Actions**:
Create the `staging` Environment (or set as repository secrets):

| Secret Name | Description |
|---|---|
| `DOKPLOY_STAGING_DEPLOY_WEBHOOK_URL` | The Deploy Webhook URL copied from Dokploy |
| `TS_OAUTH_CLIENT_ID` | Tailscale OAuth Client ID (with `auth_keys` permission and `tag:ci`) |
| `TS_OAUTH_SECRET` | Tailscale OAuth Client Secret |

---

## Triggering a Deployment

1. Go to **Actions → Deploy Staging**.
2. Click **Run workflow**.
3. Select the branch to deploy (defaults to `main`).
4. Click **Run workflow**.

When the workflow finishes, the updated version is live at **[https://staging.mlgate.ai](https://staging.mlgate.ai)**.

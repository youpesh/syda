# Manual staging deployments

## Architecture

Production remains the existing `syda-stack` raw Compose service in Dokploy
v0.30.6, on the same machine as the new staging stack. Production's Compose,
containers, data, domains and ngrok tunnel are not changed. Its current ngrok
endpoint forwards to the production frontend, not Dokploy.

Staging is a separate Dokploy project/environment/service, with its own database,
volumes, auth secret and network. GitHub-hosted runners build the selected commit;
Dokploy only pulls the finished images and starts containers. This avoids builds
competing with production for CPU/memory and reuses the repository's existing
backend/frontend Dockerfiles. The root Dockerfile builds docs, not the platform.

The workflow has four jobs:

1. **Resolve:** runs trusted controller code from the protected repository default
   branch (currently `feat/web-platform`), resolving a branch or open
   same-repository PR to a full commit SHA.
2. **Build:** checks out only that SHA, builds both images without deployment
   credentials or registry write access, and uploads a one-day image artifact.
   No selected repository script runs outside Docker's build sandbox.
3. **Publish:** on a fresh runner, loads the artifact and pushes it to GHCR using
   the short-lived `GITHUB_TOKEN`. It does not execute either image. Unique tags
   contain the SHA/run/attempt; the deployment receives immutable SHA256 digests.
4. **Deploy:** on another fresh runner, executes only trusted controller code.
   It selects the two digests, triggers staging through a restricted gateway,
   polls this run's Dokploy deployment record, and checks API/frontend health.

The deployment workflow has **no push, PR or merge triggers**. A separate
build-only PR check validates the controller/gateway and Docker images with a
read-only token, no package publishing and no deployment credentials. Its build
job is reused by the manual workflow. Repository writers can manually
select a branch or PR without deployment approvals. The GitHub `staging`
Environment permits only the exact default branch, has no required reviewers or
wait timer, and disallows admin bypass. Normal reviews for changes to the default
branch remain enforced. Public read access does not grant deployment permission.

PRs deploy their head SHA, not GitHub's synthetic merge commit. Fork PRs are
rejected. Later branch pushes do not change a previously resolved selection.
Every selection replaces one shared staging environment; separate PR databases,
domains or per-PR stacks are not provisioned.

## Shared-host security boundary

The staging service uses the **fixed, owner-installed**
`platform/docker-compose.staging.yml`. It has no builds, host filesystem mounts,
Docker socket, privileged containers or production credentials. App containers
run without root/capabilities, with read-only root filesystems, temporary writable
storage and resource limits. Staging jobs/settings volumes must be initialized
with UID/GID 10001 ownership. PostgreSQL uses a separate staging-only password.

A selected PR's Dockerfile runs on a disposable GitHub runner, never the server.
Its Compose file is not used. Selected application code does run in staging and
can read/modify staging data and any staging runtime credentials; all repository
writers must be trusted with those. Shared containers still share a kernel and
host resources. This is not VM isolation: a container/kernel escape or host-wide
outage can affect production. Resource limits reduce contention but do not remove
that shared-host risk.

A tiny standard-library gateway (`scripts/staging_gateway.py`) is necessary on
this Dokploy edition because its base member role also permits mount management.
Putting a normal member key directly in Actions would give the workflow more
control than selecting images and deploying. Instead:

- The real Dokploy key belongs to a dedicated non-admin automation account whose
  allowlist contains only the staging project, environment and service. It has
  no service-creation, Docker, SSH-key, Git-provider or production access.
- That key stays in a private server-side configuration file and is **never**
  stored in GitHub, passed to a build, or returned by the gateway.
- GitHub receives an independent random **staging deployment token**. The gateway
  accepts only four API operations: staging config read, filtered staging history,
  selection of two validated GHCR digests plus SHA, and a volume-preserving
  staging deployment. Arbitrary environment keys, Compose edits, mounts, other
  service IDs, redirects and volume resets are rejected.
- The gateway verifies the fixed Compose text and absence of extra mounts/custom
  commands before deployment. It merges image selection into Dokploy's existing
  staging environment server-side, keeping runtime secrets off Actions runners.
- It listens on loopback and forwards other HTTP requests to the staging frontend;
  the real Dokploy UI/API is not publicly proxied. It does not have Docker socket
  or host filesystem administration access.

GitHub Environments alone do not stop someone who can alter approved workflow
code from exfiltrating available secrets or requesting a broader repository
`GITHUB_TOKEN`. Protect the default branch and review **all** workflows/controller
changes. The constrained gateway token provides the production boundary even if
that token is stolen. Keep production deployment credentials and production image
packages inaccessible to this workflow. Repository/server administrators remain
trusted. If a Dokploy key is pasted into chat, revoke it in Dokploy; do not use it
as the permanent GitHub deployment credential.

## One-time setup

1. Merge the workflow/controller through the default branch's normal code review.
   GitHub must have this dispatched workflow on its default branch before the
   first full Actions run. PR #10 contains the implementation.
2. Create a separate Dokploy project `syda-staging`, environment `staging`, and raw
   Compose service with a stable app/project name. Install the fixed staging
   Compose file exactly. Disable **Auto Deploy** and configure no deployment
   webhooks, preview triggers or external deployment automation for this service.
   Do not convert, repoint, recreate or redeploy production to install staging.
3. Set `createEnvFile=true` and staging-only values in Dokploy:
   `STAGING_POSTGRES_PASSWORD`, `STAGING_AUTH_SECRET_KEY`, `STAGING_PUBLIC_URL`,
   `STAGING_BACKEND_IMAGE`, `STAGING_FRONTEND_IMAGE`, and `STAGING_SOURCE_SHA`.
   Use fresh generated database/auth secrets, not production copies. The controller
   changes only the image references and source SHA. Provider credentials, if
   required for functional testing, must also be staging-only.
4. Give the dedicated automation account access only to this new project,
   environment and Compose service. Enable API access, disable service/project/
   environment creation/deletion, Docker, SSH keys, Git providers and Traefik-file
   access. Verify production `compose.one` is denied and staging `compose.update`
   is denied. Default member mount permissions are the reason its API key remains
   private behind the gateway. Verify the gateway token also rejects production
   IDs, arbitrary Compose/mount APIs and extra environment keys. Configure an
   appropriate API-key request limit (installed defaults were only 10/day, too
   small for polling); the gateway key uses 120 requests per minute.
5. Run the gateway from reviewed controller code, under an unprivileged identity,
   with no capabilities, a read-only root filesystem and no Docker socket.
   `settings.json` is private (0600), containing `dokploy_url` (loopback API),
   `dokploy_api_key`, `deploy_token`, `compose_id`, `environment_id`,
   `compose_template` (exact fixed file contents), `app_name` (stable volume prefix), `repository`, `frontend_url`
   (`http://127.0.0.1:3011`) and `public_host` (public staging hostname/port).
   Start it with `python3 staging_gateway.py /config/settings.json /state`;
   give it a private writable `/state` directory for its durable deployment lock.
6. Expose the loopback gateway at `127.0.0.1:3012` using a **separate** HTTPS
   ingress. Keep the existing production ngrok tunnel unchanged. A separate ngrok
   account/domain or a dedicated Tailscale Funnel HTTPS port can serve staging.
   The server already has tailnet-only Tailscale Serve on port 443 for Dokploy:
   **never enable public Funnel on that existing port**, which would expose the
   admin UI. A separate Funnel port such as 8443 must target only the gateway.
7. GHCR image names are `ghcr.io/<owner>/<repo>-staging-backend` and
   `ghcr.io/<owner>/<repo>-staging-frontend`. Grant this repository's Actions access
   to those staging-only packages. Either make just these packages public, or
   configure a read-only registry credential server-side for Dokploy pulls. The
   publishing job uses `GITHUB_TOKEN`; no long-lived registry write credential is
   needed. Image artifacts and packages consume GitHub storage; retention is one
   day for intermediate image artifacts. For initial package creation, dispatch
   once with **Deploy after publishing** disabled. This tests the runner build
   and creates the packages without touching staging. Configure package visibility
   or server pull credentials, then dispatch with deployment enabled.
8. Initialize only the **new staging** job/settings volumes with UID/GID 10001
   ownership. Leave production volumes untouched. Keep the staging Compose app
   name stable so deploys retain the same database and volumes. PostgreSQL is not
   published; only the staging frontend binds to `127.0.0.1:3011`.

## GitHub Environment values

Configure these under **Settings → Environments → staging**:

| Kind | Name | Value |
| --- | --- | --- |
| Secret | `STAGING_DEPLOY_TOKEN` | Random gateway token; **not** a Dokploy API key |
| Variable | `STAGING_DOKPLOY_URL` | Gateway HTTPS origin, without `/api`, credentials or query |
| Variable | `STAGING_DOKPLOY_COMPOSE_ID` | New staging Compose ID |
| Variable | `STAGING_DOKPLOY_ENVIRONMENT_ID` | New staging Dokploy environment ID |
| Variable | `STAGING_API_HEALTH_URL` | Gateway HTTPS origin + `/api/health` |
| Variable | `STAGING_WEB_HEALTH_URL` | Gateway HTTPS origin + `/` |

The image digests and selected SHA are job outputs, not user-entered deployment
parameters. No production or Dokploy admin credential belongs in GitHub. The old
snapshot-branch ruleset and `STAGING_ISOLATION_CONFIRMED` setting are not needed
with runner builds and the constrained image-only gateway.

## Deploy and test

Open **Actions → Deploy staging → Run workflow**. Keep the workflow branch
selector on the **default branch**. Enter a source branch, or leave branch blank
and enter an open same-repository PR number. Both blank selects the default
branch. The run builds, publishes and deploys without an approval step.

A green run means the correlated Dokploy deployment reported `done`, the API
returned `status: ok` and `service: syda-platform-api`, and the frontend returned
an application page. Health checks do not receive deployment credentials. They
are liveness checks, not a full provider/database workflow or browser test.
The API probe uses `/api/health`; the current production frontend's `/health`
returned 404 during inspection. Full functional testing needs staging provider
configuration and application exercises after the first successful deployment.

A fixed staging branch is supported by entering that branch each time. For a
rollback, select a branch pointing at the previous commit and dispatch again;
there is no automatic data/migration rollback. No deployments happen when a PR
is opened, updated or merged. The separate read-only PR check only validates
builds for deployment-related changes. Existing docs/production behavior stays
unchanged.

## Failures and recovery

The workflow serializes shared staging through one concurrency group. GitHub can
supersede pending runs but never cancels an active run for a newer dispatch.
An atomic `staging-deployment-lock` Git ref survives runner loss/cancellation.
The gateway separately writes its in-flight record **before** calling Dokploy.
Neither lock is automatically discarded after an uncertain trigger response.

On cancellation, timeout or trigger/network failure, inspect Dokploy history and
queue for `github-staging-<run>-<attempt>-<sha>`. Wait for completion or explicitly
cancel and verify there are no remaining queued/running processes. The gateway
clears its in-flight record only after that exact title becomes terminal. If no
record exists and the outcome is uncertain, an owner must reconcile the queue
before manually clearing `/state/inflight.json` and deleting the GitHub
`staging-deployment-lock` branch. Then start a new dispatch, not an old rerun.
A terminal build/deploy failure or failed health check fails Actions clearly.
POST triggers are not retried automatically.

The gateway/runner omit API response bodies and credentials from logs. Inspect
Dokploy logs privately for build/runtime errors. `freshVolumes` is always false;
no workflow operation deletes/recreates the service, resets volumes, or modifies
production. Staging application code can still change staging data.

If ngrok is used for the separate staging ingress, requests send
`ngrok-skip-browser-warning: 1`; API responses must be JSON, HTTPS is verified,
and redirects are rejected. Free ngrok quotas/tunnel downtime can fail a run.
Polling uses one request every 15 seconds for up to 30 minutes, followed by up to
3 minutes for health checks. No ngrok authtoken is needed in GitHub.

## Validation and references

Run `python3 -m unittest discover -s scripts/tests -v`,
`actionlint .github/workflows/deploy-staging.yml .github/workflows/build-staging-images.yml`, and `docker compose -f
platform/docker-compose.staging.yml config --quiet` with dummy required values.
The controller/gateway tests simulate API/state failures and exercise HTTP
credential/target rejection; they do not constitute a live deployment test.

Contracts checked against installed Dokploy v0.30.6 and official references:

- [Dokploy Compose API](https://docs.dokploy.com/docs/api/compose): `compose.one`,
  `compose.saveEnvironment`, `compose.deploy`.
- [Dokploy deployment API](https://docs.dokploy.com/docs/api/deployment):
  `deployment.allByCompose`.
- [GitHub image publishing](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images)
  and [GHCR](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).
- [GitHub Environment protections](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments).
- [ngrok free plan](https://ngrok.com/docs/pricing-limits/free-plan-limits).

Current setup status: the separate staging project/environment/raw service,
dedicated automation account, private loopback gateway and new staging data
volumes have been created. The gateway token and staging IDs are configured in
the GitHub Environment. A separate ngrok account now exposes only the staging
gateway, with request inspection disabled, and staging HTTPS/health URL variables
are configured; production's tunnel is unchanged. The workflow remains
in PR #10 pending normal default-branch code review/merge. GHCR package pull
access must be configured when the first runner build publishes the packages.

Validation completed: 34 controller/gateway tests, workflow actionlint and Compose
configuration validation passed locally and in the read-only GitHub PR check.
Both backend and frontend images built successfully on a GitHub-hosted runner
([run 41696469018](https://github.com/youpesh/syda/actions/runs/41696469018)).
Live loopback and external HTTPS gateway config reads returned
200; invalid tokens returned 403, production IDs and arbitrary env writes were
rejected. The real staging-only Dokploy key was denied production reads and
Compose edits. Production frontend/API read-only probes still returned 200.
No image publication or actual staging deployment has yet been executed
successfully; the staging application is not running yet.

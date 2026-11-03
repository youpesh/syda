# Manual staging deployment

## Architecture and decision

Inspection on 2026-11-03 found `platform/docker-compose.yml` with PostgreSQL,
backend and frontend services, and named `postgres_data`, `job_data`, and
`settings_data` volumes. The backend builds from the repository root; the frontend
builds from `platform/frontend`. The root `Dockerfile` builds documentation, **not
the platform**. The checked-out branch contains only the GitHub Pages docs
workflow (pushes to `main`); it is unchanged. GitHub also lists CI and Generation
queue workflows, but their files are absent on both the default branch and main.
GitHub's current default branch is `feat/web-platform`, despite the local
`origin/HEAD` pointing at `main`. No staging branch, Dokploy configuration,
Actions environments, Actions secrets or variables were found during the initial
repository inspection.

Follow-up server inspection over Tailscale SSH found Dokploy **v0.30.6** and an
existing `syda-stack` service in the `syda` project's **production** environment.
It uses **raw Compose**, has Auto Deploy enabled, and builds from absolute host
paths under `/etc/dokploy/syda`. Its ngrok tunnel forwards to `frontend:3000`,
not the Dokploy API. No separate staging service or registered remote server was
found. The existing public frontend and `/api/health` responded successfully to
read-only probes; `/health` on that frontend returned 404. This is a health probe,
not a deployment test. The existing stack, tunnel and data were left unchanged.

GitHub setup completed: the `staging` Environment allows only the default branch,
has no required deployment reviewers/wait timer, and disallows administrator
bypass. The immutable snapshot ruleset is active with no bypass actors. No
Dokploy credentials or target variables have been configured. The workflow is in
PR #10 and still requires the default branch's normal code review before merge.
Provision or identify the intended staging target before enabling deployment;
do not use the existing production service ID. A Git-based staging service must
build from its cloned source, with relative contexts as in the platform Compose
file; copying the existing absolute host paths would build unrelated working-tree
content instead of the selected commit.

Use **one shared staging Compose service** with a selectable branch or open
same-repository PR. This reuses the existing stack and needs no registry, preview
database, additional deployment platform or engineer Dokploy accounts. Concurrent
PR environments would require unique Compose/container names, separate databases,
volumes, ports and domains; the existing fixed container names and published ports
are unsuitable for that. This implementation does not create preview environments.

The workflow runs only on `workflow_dispatch`, and only when its **Run workflow
branch selector is the repository default branch**. It resolves the input to a
full SHA, displays it in the run summary, then creates a unique branch
`staging-snapshots/<run>-<attempt>-<sha>` at that SHA. Active GitHub rules must
prohibit all updates and deletions of these snapshots. Dokploy's Git/GitHub clone
uses a branch name (`git clone --branch … --depth 1`); passing an arbitrary SHA
there is not a supported substitute. The workflow updates only the staging
service's source branch, calls `compose.deploy`, waits for the deployment record
with this run's unique title, and checks the API and frontend health URLs.

The selected source is never checked out or executed on the Actions runner. The
runner executes the controller from the workflow's trusted `github.sha`. Selected
Dockerfiles and Compose code **do execute on staging**, with access to staging
runtime secrets and data. Review the resolved SHA accordingly. A PR deploy uses
its head commit, not GitHub's synthetic merge commit; it does not test integration
with the base branch. New commits pushed after source resolution are excluded.
Any engineer with repository write access can dispatch staging without a separate
deployment review; public read access does not grant permission to deploy.

## One-time owner setup: security boundary

Complete these before enabling the workflow or setting the confirmation variable:

1. Keep staging on a Docker host/VM isolated from production. Do not share the
   Docker daemon/socket, host mounts, deployment SSH credentials, databases,
   production API keys or networks that permit production administration. A
   Dokploy project/environment alone does not sandbox hostile Compose code: it
   can request privileged containers or host mounts. If staging already has its
   own host, reuse it. Otherwise provision this isolation before deploying PRs.
   Do not copy production data or credentials into staging.
2. Have an owner create a dedicated Dokploy automation identity. Engineers need
   repository write access to dispatch, **not** Dokploy accounts. Grant the
   automation identity access only to the staging Compose service/project and
   environment. Grant service read/source-update and deployment read/create
   permissions as narrowly as the installed edition allows. Disable terminal,
   Docker/global settings, other servers, user management and production access.
   Generate its API key; do not use an owner/admin API key on a shared instance.
3. Verify scope on the **installed Dokploy version** before storing the key:
   staging `compose.one`, `compose.update` (same branch value), deployment history
   and deploy must be allowed; production reads must be denied. Verify update and
   deployment authorization using an expendable inaccessible test service, never
   by attempting a production deployment. Review the identity's service allowlist
   and role permissions. API authentication/authorization behavior varies by
   version/edition. If it cannot enforce this boundary, use a staging-only
   Dokploy instance on the isolated staging host with no production servers or
   credentials registered. An unrestricted shared-instance key is not acceptable.
4. Protect the repository default branch: require reviewed PRs, dismiss stale
   approvals, prevent force pushes/deletion, and limit bypass/admin access.
   Require trusted deployment owners to review changes to **all** workflows and
   `scripts/deploy_staging.py`, including through CODEOWNERS if desired. Configure
   actual required-review enforcement; a CODEOWNERS file by itself is insufficient.
   No CODEOWNERS identities are invented in this change.
5. Create the GitHub Environment **`staging`**. Its deployment branch policy must
   allow **only the exact default branch** (currently `feat/web-platform`), not
   all protected branches or all tags. Leave required reviewers empty and the
   wait timer disabled: deployment starts after a manual dispatch without an
   approval gate. Keep administrator bypass disabled. Repository write access
   controls who can dispatch; workflow/controller changes still require the
   default branch's normal code review protections.
6. Create an **active branch ruleset** matching
   `refs/heads/staging-snapshots/**/*` (or UI pattern `staging-snapshots/**/*`).
   Enable **Restrict updates** and **Restrict deletions**, with an **empty bypass
   list**, including no Actions app bypass. Allow creation (do not enable Restrict
   creations). Disable any exception allowing fetch-and-merge updates. Do not
   require PRs/status checks on snapshot creation. Leave
   `staging-deployment-lock` outside this ruleset. The controller checks the
   effective rules for each snapshot and fails before changing Dokploy if absent.
   Keep Actions `contents: write` permitted for this workflow. Its short-lived
   `GITHUB_TOKEN` creates refs; no PAT is needed. Snapshots remain for audit.

Environment secrets do **not** make arbitrary workflow changes safe. An engineer
who can alter an approved workflow can add a step to exfiltrate any secret that
job receives, remove its guards, or request broader `GITHUB_TOKEN` permissions.
The exact-branch environment restriction and reviewed default branch reduce that
risk; there is no per-deployment reviewer. Treat every repository writer as
authorized to run selected code with staging runtime secrets and data.
The Dokploy-side scope and host isolation enforce the
production boundary even if the staging key is stolen. Repository/environment
administrators remain trusted. Keep production secrets inaccessible to this
environment and production workflows protected independently.

## One-time owner setup: existing Dokploy and ngrok

1. Identify the actual existing **staging Compose service** and its environment
   ID. Check that it is not production. The workflow supports source type
   `github` or `git` pointing at this GitHub repository; it deliberately rejects
   raw Compose, GitLab and unrelated repositories. If the live deployment uses
   separate Dokploy Applications instead of Compose, do not repoint this workflow
   at one of their IDs: adapt it after inspecting that setup.
2. Keep the existing Compose path, app/project name, domain mappings, environment
   variables, mounts and volume identities. For a new platform staging service,
   the repository's platform Compose path is `platform/docker-compose.yml`, not
   the root Dockerfile. Review its development defaults (database password,
   published database port, CORS) and provide staging settings in Dokploy. Reuse
   existing staging overrides rather than replacing them with this development
   file. Do not rename/recreate an existing service or change its Compose project
   name to install this workflow; that can create new volumes. Back up staging
   data before the first deployment. Audit any Dokploy file patches: these apply
   after cloning and can alter source files. Pinning the Git commit does not make
   external image tags, package downloads or existing Dokploy patches reproducible.
3. Disable **Auto Deploy**, preview deployments, and any push/PR webhooks or
   external automation that deploy this staging service. Keep repository read
   access configured in Dokploy (GitHub App or read-only deploy key) so it can
   clone snapshot branches. Keep that integration's permissions limited to the
   needed repository. No workflow enables auto deployment or changes production.
4. Reuse the existing ngrok HTTPS origin forwarding to Dokploy. Keep the agent
   running as a service. Free ngrok supplies an assigned dev domain; if your
   actual forwarding URL changes, update the environment variable. No ngrok
   authtoken belongs in GitHub: Actions is an HTTP client, not a tunnel host.
   Ensure any ngrok access policy permits these API requests; browser OAuth/login
   redirects are rejected. Keep Dokploy authentication enabled. Consider ngrok
   request inspection/log retention because the tunnel carries the API key.
5. Supply an externally reachable staging API health URL and frontend page URL.
   The API URL must return JSON with `status: ok` and
   `service: syda-platform-api` (existing `/health` or `/api/health`). Use the
   actual public backend route; Vite's development proxy does not establish a
   production frontend proxy. Frontend health must return HTTP 2xx HTML. These
   are liveness checks, not database/provider readiness or proof of build content.

The client sends `x-api-key`, `Accept: application/json` and
`ngrok-skip-browser-warning: 1`, verifies HTTPS, rejects redirects and non-JSON API
responses, and never prints request/response bodies or credentials. Health checks
receive **no Dokploy credential**. The free tier currently allows 20,000 HTTP
requests and 1 GB outbound transfer monthly; quota exhaustion, agent downtime and
tunnel failures fail the run. There is no free-endpoint duration timeout. Polling
every 15 seconds uses about 120 history calls for a maximum 30-minute deployment.
Trigger POSTs are never retried automatically because a lost response may still
mean a deployment was queued.

## GitHub staging Environment configuration

Store these under **Settings → Environments → staging**, not as repository-wide
secrets. No production credential is required.

| Kind | Name | Value |
| --- | --- | --- |
| Secret | `STAGING_DOKPLOY_API_KEY` | Dedicated staging automation identity's API key |
| Variable | `STAGING_DOKPLOY_URL` | ngrok HTTPS origin, without `/api`, query or credentials |
| Variable | `STAGING_DOKPLOY_COMPOSE_ID` | Existing staging Compose ID |
| Variable | `STAGING_DOKPLOY_ENVIRONMENT_ID` | Expected Dokploy environment ID for that service |
| Variable | `STAGING_API_HEALTH_URL` | HTTPS staging API health endpoint |
| Variable | `STAGING_WEB_HEALTH_URL` | HTTPS staging frontend page |
| Variable | `STAGING_ISOLATION_CONFIRMED` | `true`, only after verifying the security boundary above |

URLs and IDs may be environment secrets instead if preferred; change the
corresponding workflow `vars.*` references to `secrets.*`. The confirmation is an
owner acknowledgement, not an automated proof of isolation or credential scope.

## Deploy from GitHub

1. Merge these files through review into the repository's default branch.
2. Open **Actions → Deploy staging → Run workflow**. Keep the workflow branch
   selector on the **default branch**, even when testing another branch.
3. Enter a source **branch**, or leave branch blank and enter an open
   same-repository **PR number**. Both blank deploys the current default branch.
   Fork PRs are rejected. Do not use an arbitrary SHA as the branch input.
4. The deployment starts without a review or approval step. The full resolved
   SHA appears in the resolve job summary and staging job name.
5. Wait for the deployment and both health checks. A green run means Dokploy
   reported this run's deployment `done` and both liveness checks passed. It does
   not mean a full end-to-end application test passed.

Pushing, opening/updating/merging PRs, and creating snapshots do not trigger this
workflow. The existing documentation workflow retains its existing behavior.
Shared staging is replaced by the most recent successful manual selection; PRs
share staging data. Inspect schema migrations for compatibility with that data.
For a conventional fixed staging branch, enter that branch each time; it is still
snapshotted to a SHA. To restore a prior revision, select a source branch pointing
at it and dispatch again. There is no automatic rollback or data rollback.

## Concurrency and recovery

One workflow concurrency group serializes all selections. GitHub may replace an
older pending dispatch with a newer one; running deployments are not cancelled.
An atomic Git ref `staging-deployment-lock` additionally prevents a later run from
changing the source after a runner cancellation or an uncertain API response.
The controller refuses existing active deployments and only removes its lock
after a known terminal result (or an error before submitting a deployment).
Owner/UI deploys and other automation do not honor the GitHub lock: reserve this
staging service for this workflow and coordinate any maintenance.

If the run is cancelled, times out, or loses contact after submitting a deploy,
**do not just rerun or delete the lock**. An owner must:

1. Inspect Dokploy's staging deployment history and queue. Match
   `github-staging-<run>-<attempt>-<sha>` to the Actions run. Wait for completion or
   explicitly cancel and verify the queue/processes have stopped. Do not assume
   an empty history means an unacknowledged trigger was never queued.
2. Verify no deployment is pending/running and confirm staging's source branch,
   volumes and service health. Resolve any build failure in Dokploy privately;
   API response/log bodies are intentionally not copied into public Actions logs.
3. Delete **only** the `staging-deployment-lock` branch in GitHub. This can also be
   done by an authorized engineer after the Dokploy owner confirms it is safe.
   Dispatch a new deployment; do not resume an uncertain old attempt.

Snapshot branches are retained, and Dokploy remains pointed at the selected
snapshot after deployment. Do not delete or move the current snapshot. For later
cleanup, an owner can temporarily adjust the snapshot ruleset during a deployment
maintenance window, remove unused snapshots, then re-enable it. Cleanup is never
automatic and never touches volumes. `freshVolumes` is explicitly false and the
controller never calls delete, recreate, environment-save or volume-reset APIs.
Selected application/Compose code can still change data; persistence is not a
security sandbox or a migration rollback mechanism.

## Validation and references

Local validation uses `python3 -m unittest discover -s scripts/tests -v` and
`actionlint .github/workflows/deploy-staging.yml`. Tests simulate the APIs and
failure paths; they do not contact Dokploy. Complete an owner-supervised first
deployment after configuration and verify the snapshot SHA in Dokploy's clone,
the deployment history, application pages, and retained staging data.

API contracts checked against current upstream documentation/source:

- [Dokploy Compose API](https://docs.dokploy.com/docs/api/compose): source update,
  service read, deploy with title and `freshVolumes`.
- [Dokploy deployment API](https://docs.dokploy.com/docs/api/deployment):
  `deployment.allByCompose`.
- [Dokploy GitHub clone implementation](https://github.com/Dokploy/dokploy/blob/canary/packages/server/src/utils/providers/github.ts)
  and [service authorization](https://github.com/Dokploy/dokploy/blob/canary/packages/server/src/services/permission.ts).
- [GitHub environment protections](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)
  and [effective branch rules API](https://docs.github.com/en/rest/repos/rules#get-rules-for-a-branch).
- [ngrok free-tier limits and warning bypass](https://ngrok.com/docs/pricing-limits/free-plan-limits).

Check these contracts against the installed Dokploy release. This change does
not install/upgrade Dokploy or ngrok, configure remote secrets/protections, or
claim that a live staging deployment has been tested.

Implementation validation: all 27 controller tests passed, actionlint 1.7.7
accepted the workflow, and `git diff --check` passed. Follow-up setup created the
GitHub staging Environment and immutable snapshot ruleset. Dokploy/server
inspection was read-only; no server credential or deployment was changed.

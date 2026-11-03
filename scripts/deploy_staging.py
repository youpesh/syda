"""Manual shared-staging deploy controller. Python standard library only.

Only this script from the trusted workflow revision runs on the Actions runner;
selected source is fetched and built by Dokploy, never imported here.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


LOCK_BRANCH = "staging-deployment-lock"
POLL_SECONDS = 15
DEPLOY_TIMEOUT = 1800
HEALTH_TIMEOUT = 180


class DeployError(Exception):
    pass


class HTTPFailure(DeployError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"HTTP {status}; check API access, tunnel availability and quotas")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an API key/token to a redirected host.
        return None


def https_url(value, origin=False):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or (origin and parsed.path not in ("", "/"))):
        raise DeployError("Configure an HTTPS URL without credentials, query or fragment")
    return value.rstrip("/") if origin else value


def request(url, headers, payload=None, method=None, json_response=True):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as response:
            body = response.read(4 * 1024 * 1024)
            if not json_response:
                return body
            if "application/json" not in response.headers.get("Content-Type", ""):
                raise DeployError("Expected API JSON; check the ngrok tunnel and browser warning")
    except urllib.error.HTTPError as exc:
        # Bodies, URLs, headers and upstream exception text may contain secrets.
        status = exc.code
        exc.close()
        raise HTTPFailure(status) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise DeployError("Network request failed; check tunnel/Dokploy availability") from None
    try:
        result = json.loads(body)
    except (ValueError, UnicodeError):
        raise DeployError("API returned invalid JSON") from None
    if isinstance(result, dict) and (result.get("error") or result.get("code")
                                     or result.get("success") is False):
        raise DeployError("API reported an error; inspect the service privately")
    return result


class GitHub:
    def __init__(self):
        self.repo = os.environ["GITHUB_REPOSITORY"]
        self.base = https_url(os.environ.get("GITHUB_API_URL", "https://api.github.com"), True)
        self.headers = {
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "syda-staging-deployer",
        }

    def call(self, path, payload=None, method=None):
        return request(f"{self.base}/repos/{self.repo}/{path}", self.headers, payload, method,
                       json_response=method != "DELETE")

    def create_ref(self, branch, sha):
        self.call("git/refs", {"ref": f"refs/heads/{branch}", "sha": sha})

    def ref_sha(self, branch):
        return self.call(f"git/ref/heads/{quote(branch)}")["object"]["sha"]


def quote(value):
    return urllib.parse.quote(value, safe="")


def valid_sha(sha):
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise DeployError("Expected a full Git commit SHA")
    return sha


def note(message):
    print(message, flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write(message + "\n\n")


def resolve(gh):
    branch = os.environ.get("INPUT_BRANCH", "").strip()
    pr = os.environ.get("INPUT_PR", "").strip()
    if pr and branch:
        raise DeployError("Select a branch OR a PR, not both")
    if pr:
        if not re.fullmatch(r"[1-9][0-9]*", pr):
            raise DeployError("PR must be a positive number")
        source = gh.call(f"pulls/{pr}")
        if (source["state"] != "open" or not source["head"].get("repo")
                or source["head"]["repo"]["full_name"].lower() != gh.repo.lower()):
            raise DeployError("Only open, same-repository PRs can deploy to shared staging")
        sha = valid_sha(source["head"]["sha"])
    else:
        branch = branch or os.environ["DEFAULT_BRANCH"]
        if branch.startswith("staging-snapshots/") or branch == LOCK_BRANCH:
            raise DeployError("Choose a source branch, not a deployment control branch")
        sha = valid_sha(gh.call(f"branches/{quote(branch)}")["commit"]["sha"])
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"sha={sha}\n")
    note(f"Selected staging commit: `{sha}`. Manual deployment proceeds without an approval gate.")


class Dokploy:
    def __init__(self):
        self.base = https_url(os.environ["DOKPLOY_URL"], True)
        self.compose_id = os.environ["DOKPLOY_COMPOSE_ID"]
        self.environment_id = os.environ["DOKPLOY_ENVIRONMENT_ID"]
        self.headers = {
            "x-api-key": os.environ["DOKPLOY_API_KEY"],
            "Content-Type": "application/json",
            "Accept": "application/json",
            "ngrok-skip-browser-warning": "1",
            "User-Agent": "syda-staging-deployer",
        }

    def call(self, endpoint, payload=None):
        return request(f"{self.base}/api/{endpoint}", self.headers, payload)

    def one(self):
        return self.call(f"compose.one?composeId={quote(self.compose_id)}")

    def history(self):
        rows = self.call(f"deployment.allByCompose?composeId={quote(self.compose_id)}")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise DeployError("Unexpected Dokploy deployment history schema")
        return rows

    def check_config(self, config, repo):
        if (config.get("composeId") != self.compose_id
                or config.get("environmentId") != self.environment_id):
            raise DeployError("Dokploy staging service/environment identity mismatch")
        if config.get("autoDeploy") is not False:
            raise DeployError("Disable Dokploy Auto Deploy for staging before proceeding")
        if config.get("composeStatus") == "running":
            raise DeployError("Dokploy staging is already running a deployment")
        source = config.get("sourceType")
        if source == "github":
            if f"{config.get('owner')}/{config.get('repository')}".lower() != repo.lower():
                raise DeployError("Staging source repository does not match GitHub")
            return "branch"
        if source == "git":
            urls = {f"https://github.com/{repo}", f"https://github.com/{repo}.git",
                    f"git@github.com:{repo}.git"}
            if config.get("customGitUrl", "").lower() not in {url.lower() for url in urls}:
                raise DeployError("Staging custom Git source does not match this GitHub repository")
            return "customGitBranch"
        raise DeployError("This workflow requires an existing Git/GitHub Dokploy Compose service")


def wait_for_deployment(dokploy, title, previous):
    deadline = time.monotonic() + DEPLOY_TIMEOUT
    while time.monotonic() < deadline:
        rows = dokploy.history()
        matches = [row for row in rows if row.get("title") == title
                   and row.get("deploymentId") not in previous]
        if len(matches) > 1:
            raise DeployError("Multiple deployments matched this run; retaining staging lock")
        if matches:
            row = matches[0]
            if row.get("composeId") != dokploy.compose_id:
                raise DeployError("Deployment identity mismatch")
            status = row.get("status")
            if status in ("done", "error", "cancelled"):
                return status
            if status != "running":
                raise DeployError("Unrecognized Dokploy deployment status")
        time.sleep(POLL_SECONDS)
    raise DeployError("Timed out waiting for this deployment; retaining staging lock")


def check_health(api_url, web_url):
    headers = {"Accept": "application/json", "ngrok-skip-browser-warning": "1",
               "User-Agent": "syda-staging-health"}
    deadline = time.monotonic() + HEALTH_TIMEOUT
    while time.monotonic() < deadline:
        try:
            api = request(api_url, headers)
            if api.get("status") != "ok" or api.get("service") != "syda-platform-api":
                raise DeployError("Staging API health response did not match Syda")
            page = request(web_url, {**headers, "Accept": "text/html"}, json_response=False)
            if b"<html" not in page.lower() or b"err_ngrok" in page.lower():
                raise DeployError("Staging frontend did not return an application page")
            return
        except (DeployError, AttributeError):
            time.sleep(POLL_SECONDS)
    raise DeployError("Deployment completed but staging API/frontend health checks failed")


def deploy(gh, dokploy):
    if os.environ.get("STAGING_ISOLATION_CONFIRMED") != "true":
        raise DeployError("Complete the staging isolation/security checklist in platform/STAGING.md")
    sha = valid_sha(os.environ["DEPLOY_SHA"])
    api_url = https_url(os.environ["STAGING_API_HEALTH_URL"])
    web_url = https_url(os.environ["STAGING_WEB_HEALTH_URL"])
    run = os.environ["GITHUB_RUN_ID"]
    attempt = os.environ["GITHUB_RUN_ATTEMPT"]
    if not re.fullmatch(r"[0-9]+", run) or not re.fullmatch(r"[0-9]+", attempt):
        raise DeployError("Invalid GitHub run identity")
    snapshot = f"staging-snapshots/{run}-{attempt}-{sha}"
    title = f"github-staging-{run}-{attempt}-{sha}"
    # Atomic, durable lock survives runner cancellation, timeouts and ambiguous
    # API failures. It is intentionally outside the immutable snapshot ruleset.
    try:
        gh.create_ref(LOCK_BRANCH, valid_sha(os.environ["GITHUB_SHA"]))
    except HTTPFailure as exc:
        if exc.status == 422:
            raise DeployError("Staging lock exists; see platform/STAGING.md recovery instructions") from None
        raise
    safe_to_unlock = True
    try:
        config = dokploy.one()
        branch_field = dokploy.check_config(config, gh.repo)
        history = dokploy.history()
        if any(row.get("status") not in ("done", "error", "cancelled") for row in history):
            raise DeployError("Staging has an active/unknown deployment; wait for it to finish")
        previous = {row["deploymentId"] for row in history}
        gh.create_ref(snapshot, sha)
        rules = gh.call(f"rules/branches/{quote(snapshot)}?per_page=100")
        immutable = {rule["type"] for rule in rules
                     if not (rule.get("parameters") or {}).get("update_allows_fetch_and_merge", False)}
        if not {"update", "deletion"}.issubset(immutable):
            raise DeployError("Snapshot branch requires active rules restricting updates AND deletions")
        if gh.ref_sha(snapshot) != sha:
            raise DeployError("Snapshot SHA mismatch")
        # Patch only the source pointer. Preserve env, domains, compose path,
        # project name, volumes, database and every other existing setting.
        dokploy.call("compose.update", {"composeId": dokploy.compose_id, branch_field: snapshot})
        if dokploy.one().get(branch_field) != snapshot:
            raise DeployError("Dokploy did not retain the selected source snapshot")
        note(f"Deploying staging commit `{sha}` using `{snapshot}`.")
        safe_to_unlock = False  # A failed POST may still have enqueued a deployment.
        dokploy.call("compose.deploy", {"composeId": dokploy.compose_id, "title": title,
                                       "description": f"Exact source commit {sha}",
                                       "freshVolumes": False})
        status = wait_for_deployment(dokploy, title, previous)
        safe_to_unlock = True  # This run is definitively terminal.
        if status != "done":
            raise DeployError(f"Dokploy staging deployment ended with status: {status}")
        if gh.ref_sha(snapshot) != sha or dokploy.one().get(branch_field) != snapshot:
            raise DeployError("Source configuration changed during deployment")
        check_health(api_url, web_url)
        note(f"Staging deployment completed for `{sha}`; API and frontend health checks passed.")
    finally:
        if safe_to_unlock:
            gh.call(f"git/refs/heads/{quote(LOCK_BRANCH)}", method="DELETE")
        else:
            note("Staging lock retained: deployment outcome is uncertain. An operator must reconcile "
                 "Dokploy before removing `staging-deployment-lock`; see platform/STAGING.md.")


def main():
    try:
        if sys.argv[1:] == ["resolve"]:
            resolve(GitHub())
        elif sys.argv[1:] == ["deploy"]:
            required = ("DOKPLOY_URL", "DOKPLOY_API_KEY", "DOKPLOY_COMPOSE_ID",
                        "DOKPLOY_ENVIRONMENT_ID", "STAGING_API_HEALTH_URL", "STAGING_WEB_HEALTH_URL")
            if any(not os.environ.get(key) for key in required):
                raise DeployError("Missing staging environment configuration; see platform/STAGING.md")
            deploy(GitHub(), Dokploy())
        else:
            raise DeployError("Usage: deploy_staging.py resolve|deploy")
    except (DeployError, KeyError, TypeError, ValueError) as exc:
        # Only our deliberately sanitized messages are safe to log.
        message = str(exc) if isinstance(exc, DeployError) else "Unexpected configuration/API schema"
        print(f"::error::{message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

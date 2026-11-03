import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error


spec = importlib.util.spec_from_file_location(
    "deploy_staging", Path(__file__).resolve().parents[1] / "deploy_staging.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
SHA = "a" * 40
CONTROL_SHA = "b" * 40
SNAPSHOT = f"staging-snapshots/12-1-{SHA}"
TITLE = f"github-staging-12-1-{SHA}"


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {
            "GITHUB_REPOSITORY": "owner/repo", "GH_TOKEN": "test-github-token",
            "GITHUB_SHA": CONTROL_SHA, "GITHUB_RUN_ID": "12", "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_OUTPUT": self.temp.name + "/output",
            "GITHUB_STEP_SUMMARY": self.temp.name + "/summary",
            "DEFAULT_BRANCH": "feat/web-platform", "INPUT_BRANCH": "", "INPUT_PR": "",
            "DEPLOY_SHA": SHA, "STAGING_ISOLATION_CONFIRMED": "true",
            "DOKPLOY_URL": "https://example.ngrok-free.app",
            "DOKPLOY_API_KEY": "test-dokploy-key", "DOKPLOY_COMPOSE_ID": "staging",
            "DOKPLOY_ENVIRONMENT_ID": "stage-env",
            "STAGING_API_HEALTH_URL": "https://api.example.test/health",
            "STAGING_WEB_HEALTH_URL": "https://web.example.test/",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = {
            "composeId": "staging", "environmentId": "stage-env", "sourceType": "github",
            "owner": "owner", "repository": "repo", "branch": "feat/web-platform",
            "autoDeploy": False, "composeStatus": "done", "env": "sensitive-runtime-env",
            "appName": "keep-volume-identity", "composePath": "existing/compose.yml",
        }
        self.gh = Mock(repo="owner/repo")
        self.gh.ref_sha.return_value = SHA
        self.gh.call.return_value = [{"type": "update"}, {"type": "deletion", "parameters": None}]
        self.dokploy = deploy.Dokploy()
        self.calls = []

        def call(endpoint, payload=None):
            self.calls.append((endpoint, payload))
            if endpoint.startswith("compose.one"):
                return dict(self.config)
            if endpoint.startswith("deployment.allByCompose"):
                return []
            if endpoint == "compose.update":
                self.config.update(payload)
            return True

        self.dokploy.call = Mock(side_effect=call)

    def run_deploy(self, status="done"):
        with patch.object(deploy, "wait_for_deployment", return_value=status), \
                patch.object(deploy, "check_health") as health:
            deploy.deploy(self.gh, self.dokploy)
        return health

    def test_success_preserves_configuration_and_uses_exact_snapshot(self):
        health = self.run_deploy()
        self.gh.create_ref.assert_any_call(SNAPSHOT, SHA)
        self.assertIn(("compose.update", {"composeId": "staging", "branch": SNAPSHOT}), self.calls)
        self.assertIn(("compose.deploy", {"composeId": "staging", "title": TITLE,
                      "description": f"Exact source commit {SHA}", "freshVolumes": False}), self.calls)
        self.assertEqual(self.config["env"], "sensitive-runtime-env")
        self.assertEqual(self.config["appName"], "keep-volume-identity")
        self.assertEqual(self.config["composePath"], "existing/compose.yml")
        health.assert_called_once()
        self.gh.call.assert_any_call("git/refs/heads/staging-deployment-lock", method="DELETE")

    def test_custom_git_source(self):
        self.config.update(sourceType="git", customGitUrl="git@github.com:owner/repo.git")
        self.run_deploy()
        self.assertIn(("compose.update", {"composeId": "staging", "customGitBranch": SNAPSHOT}), self.calls)

    def test_guard_rejections_never_deploy(self):
        for change in ({"environmentId": "production"}, {"composeId": "production"},
                       {"autoDeploy": True}, {"autoDeploy": None}, {"sourceType": "raw"},
                       {"repository": "other"}, {"composeStatus": "running"}):
            with self.subTest(change=change):
                original = dict(self.config)
                self.config.update(change)
                with self.assertRaises(deploy.DeployError):
                    self.run_deploy()
                self.config = original
        self.assertFalse(any(endpoint == "compose.deploy" for endpoint, _ in self.calls))

    def test_isolation_acknowledgement_required(self):
        os.environ["STAGING_ISOLATION_CONFIRMED"] = "false"
        with self.assertRaises(deploy.DeployError):
            self.run_deploy()
        self.gh.create_ref.assert_not_called()

    def test_snapshot_rules_required(self):
        self.gh.call.return_value = [{"type": "deletion"}]
        with self.assertRaisesRegex(deploy.DeployError, "Snapshot branch"):
            self.run_deploy()
        self.assertFalse(any(endpoint == "compose.update" for endpoint, _ in self.calls))

    def test_snapshot_rules_allowing_merges_rejected(self):
        self.gh.call.return_value = [
            {"type": "update", "parameters": {"update_allows_fetch_and_merge": True}},
            {"type": "deletion"}]
        with self.assertRaisesRegex(deploy.DeployError, "Snapshot branch"):
            self.run_deploy()

    def test_active_deployment_blocks_source_update(self):
        original = self.dokploy.call.side_effect

        def active(endpoint, payload=None):
            if endpoint.startswith("deployment.allByCompose"):
                return [{"deploymentId": "another", "status": "running"}]
            return original(endpoint, payload)

        self.dokploy.call.side_effect = active
        with self.assertRaisesRegex(deploy.DeployError, "active/unknown"):
            self.run_deploy()
        self.assertFalse(any(endpoint == "compose.update" for endpoint, _ in self.calls))

    def test_changed_snapshot_cannot_trigger_deploy(self):
        self.gh.ref_sha.return_value = CONTROL_SHA
        with self.assertRaisesRegex(deploy.DeployError, "SHA mismatch"):
            self.run_deploy()
        self.assertFalse(any(endpoint == "compose.deploy" for endpoint, _ in self.calls))

    def test_existing_lock_blocks_all_dokploy_access(self):
        self.gh.create_ref.side_effect = deploy.HTTPFailure(422)
        with self.assertRaisesRegex(deploy.DeployError, "lock exists"):
            self.run_deploy()
        self.dokploy.call.assert_not_called()

    def test_terminal_failure_unlocks_without_health(self):
        for status in ("error", "cancelled"):
            with self.subTest(status=status), patch.object(deploy, "check_health") as health:
                with self.assertRaisesRegex(deploy.DeployError, status):
                    self.run_deploy(status)
                health.assert_not_called()
        self.gh.call.assert_any_call("git/refs/heads/staging-deployment-lock", method="DELETE")

    def test_uncertain_trigger_keeps_lock_and_is_not_retried(self):
        original = self.dokploy.call.side_effect

        def fail_trigger(endpoint, payload=None):
            if endpoint == "compose.deploy":
                raise deploy.HTTPFailure(502)
            return original(endpoint, payload)

        self.dokploy.call.side_effect = fail_trigger
        with self.assertRaises(deploy.DeployError):
            self.run_deploy()
        self.assertEqual(sum(call.args[0] == "compose.deploy"
                             for call in self.dokploy.call.call_args_list), 1)
        self.assertFalse(any(call.kwargs.get("method") == "DELETE"
                             for call in self.gh.call.call_args_list))

    def test_poll_timeout_or_cancellation_keeps_lock(self):
        for failure in (deploy.DeployError("timeout"), KeyboardInterrupt()):
            self.gh.reset_mock()
            with self.subTest(failure=type(failure)), \
                    patch.object(deploy, "wait_for_deployment", side_effect=failure):
                with self.assertRaises(type(failure)):
                    deploy.deploy(self.gh, self.dokploy)
            self.assertFalse(any(call.kwargs.get("method") == "DELETE"
                                 for call in self.gh.call.call_args_list))

    def test_health_failure_after_terminal_result_unlocks(self):
        with patch.object(deploy, "wait_for_deployment", return_value="done"), \
                patch.object(deploy, "check_health", side_effect=deploy.DeployError("unhealthy")):
            with self.assertRaisesRegex(deploy.DeployError, "unhealthy"):
                deploy.deploy(self.gh, self.dokploy)
        self.gh.call.assert_any_call("git/refs/heads/staging-deployment-lock", method="DELETE")

    def test_default_and_selected_branch_resolution(self):
        self.gh.call.return_value = {"commit": {"sha": SHA}}
        deploy.resolve(self.gh)
        self.gh.call.assert_called_with("branches/feat%2Fweb-platform")
        self.assertEqual(Path(os.environ["GITHUB_OUTPUT"]).read_text(), f"sha={SHA}\n")
        os.environ["INPUT_BRANCH"] = "feature/test"
        deploy.resolve(self.gh)
        self.gh.call.assert_called_with("branches/feature%2Ftest")

    def test_pr_resolution_uses_head_sha(self):
        os.environ["INPUT_PR"] = "42"
        self.gh.call.return_value = {"state": "open", "merge_commit_sha": CONTROL_SHA,
                                     "head": {"sha": SHA, "repo": {"full_name": "owner/repo"}}}
        deploy.resolve(self.gh)
        self.assertEqual(Path(os.environ["GITHUB_OUTPUT"]).read_text(), f"sha={SHA}\n")

    def test_fork_and_closed_pr_rejected(self):
        os.environ["INPUT_PR"] = "42"
        for state, repo in (("open", "fork/repo"), ("closed", "owner/repo")):
            self.gh.call.return_value = {"state": state, "head": {"sha": SHA,
                                           "repo": {"full_name": repo}}}
            with self.assertRaises(deploy.DeployError):
                deploy.resolve(self.gh)

    def test_ambiguous_or_invalid_selection_rejected(self):
        for branch, pr in (("test", "42"), ("", "1; echo hacked"), ("staging-deployment-lock", "")):
            os.environ.update(INPUT_BRANCH=branch, INPUT_PR=pr)
            with self.assertRaises(deploy.DeployError):
                deploy.resolve(self.gh)

    def test_poll_matches_only_this_run(self):
        row = {"deploymentId": "new", "title": TITLE, "composeId": "staging", "status": "done"}
        self.dokploy.history = Mock(side_effect=[
            [{**row, "deploymentId": "old"}, {**row, "title": "another-run"}],
            [{**row, "status": "running"}], [row]])
        with patch.object(deploy.time, "sleep"):
            self.assertEqual(deploy.wait_for_deployment(self.dokploy, TITLE, {"old"}), "done")
        self.assertEqual(self.dokploy.history.call_count, 3)

    def test_poll_timeout(self):
        self.dokploy.history = Mock(return_value=[])
        with patch.object(deploy.time, "monotonic", side_effect=[0, 0, 1801]), \
                patch.object(deploy.time, "sleep"):
            with self.assertRaisesRegex(deploy.DeployError, "Timed out"):
                deploy.wait_for_deployment(self.dokploy, TITLE, set())

    def test_health_retries_and_receives_no_credentials(self):
        with patch.object(deploy, "request", side_effect=[
                deploy.HTTPFailure(503), {"status": "ok", "service": "syda-platform-api"},
                b"<!doctype html><html>Syda</html>"]) as request, patch.object(deploy.time, "sleep"):
            deploy.check_health("https://api.example/health", "https://web.example/")
        for call in request.call_args_list:
            self.assertNotIn("x-api-key", call.args[1])
            self.assertNotIn("Authorization", call.args[1])

    def test_health_wrong_application_fails(self):
        with patch.object(deploy, "request", return_value={"status": "ok", "service": "other"}), \
                patch.object(deploy.time, "monotonic", side_effect=[0, 0, 181]), \
                patch.object(deploy.time, "sleep"):
            with self.assertRaisesRegex(deploy.DeployError, "health checks failed"):
                deploy.check_health("https://api.example/health", "https://web.example/")

    def test_ngrok_frontend_warning_cannot_pass_health(self):
        with patch.object(deploy, "request", side_effect=[
                {"status": "ok", "service": "syda-platform-api"},
                b"<html>ngrok ERR_NGROK_6024 warning</html>"]), \
                patch.object(deploy.time, "monotonic", side_effect=[0, 0, 181]), \
                patch.object(deploy.time, "sleep"):
            with self.assertRaisesRegex(deploy.DeployError, "health checks failed"):
                deploy.check_health("https://api.example/health", "https://web.example/")

    def test_dokploy_headers_include_auth_and_ngrok_bypass(self):
        self.dokploy = deploy.Dokploy()
        with patch.object(deploy, "request") as request:
            self.dokploy.one()
        headers = request.call_args.args[1]
        self.assertEqual(headers["x-api-key"], "test-dokploy-key")
        self.assertEqual(headers["ngrok-skip-browser-warning"], "1")

    def test_urls_require_https_and_no_embedded_credentials(self):
        for url in ("http://host", "https://user:secret@host", "https://host/?key=secret",
                    "https://host/#token", "https://host/api"):
            with self.assertRaises(deploy.DeployError):
                deploy.https_url(url, origin=True)

    def test_http_errors_do_not_expose_credentials_or_body(self):
        error = urllib.error.HTTPError("https://secret-host", 403, "secret-key", {},
                                      io.BytesIO(b"secret response"))
        opener = Mock()
        opener.open.side_effect = error
        with patch.object(deploy.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(deploy.HTTPFailure) as caught:
                deploy.request("https://example.test", {"x-api-key": "secret-key"})
        self.assertNotIn("secret", str(caught.exception))

    def test_non_json_success_is_not_api_success(self):
        response = Mock()
        response.headers = {"Content-Type": "text/html"}
        response.read.return_value = b"<html>ngrok warning</html>"
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(deploy.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(deploy.DeployError, "Expected API JSON"):
                deploy.request("https://example.test", {})

    def test_redirects_are_not_followed(self):
        self.assertIsNone(deploy.NoRedirect().redirect_request(
            Mock(), None, 302, "redirect", {}, "https://attacker.test"))


if __name__ == "__main__":
    unittest.main()

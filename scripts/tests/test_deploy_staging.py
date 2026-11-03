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
BACKEND = "ghcr.io/owner/repo-staging-backend@sha256:" + "c" * 64
FRONTEND = "ghcr.io/owner/repo-staging-frontend@sha256:" + "d" * 64
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
            "DEPLOY_SHA": SHA, "STAGING_BACKEND_IMAGE": BACKEND, "STAGING_FRONTEND_IMAGE": FRONTEND,
            "DOKPLOY_URL": "https://example.ngrok-free.app",
            "STAGING_DEPLOY_TOKEN": "test-staging-token", "DOKPLOY_COMPOSE_ID": "staging",
            "DOKPLOY_ENVIRONMENT_ID": "stage-env",
            "STAGING_API_HEALTH_URL": "https://api.example.test/health",
            "STAGING_WEB_HEALTH_URL": "https://web.example.test/",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = {
            "gateway": deploy.GATEWAY_IDENTITY,
            "composeId": "staging", "environmentId": "stage-env", "sourceType": "raw",
            "composeFile": (Path(__file__).resolve().parents[2] / "platform/docker-compose.staging.yml").read_text(),
            "autoDeploy": False, "composeStatus": "done",
        }
        self.gh = Mock(repo="owner/repo")
        self.client = deploy.StagingGatewayClient()
        self.calls = []

        def call(endpoint, payload=None):
            self.calls.append((endpoint, payload))
            if endpoint.startswith("compose.one"):
                return dict(self.config)
            if endpoint.startswith("deployment.allByCompose"):
                return []
            if endpoint == "compose.saveEnvironment":
                values = dict(line.split("=", 1) for line in payload["env"].splitlines())
                self.config.update(sourceCommit=values["STAGING_SOURCE_SHA"],
                                   backendImage=values["STAGING_BACKEND_IMAGE"],
                                   frontendImage=values["STAGING_FRONTEND_IMAGE"])
            return True

        self.client.call = Mock(side_effect=call)

    def run_deploy(self, status="done"):
        with patch.object(deploy, "wait_for_deployment", return_value=status), \
                patch.object(deploy, "check_health") as health:
            deploy.deploy(self.client, "owner/repo")
        return health

    def test_success_selects_exact_digests(self):
        os.environ.pop("GH_TOKEN")  # Deployment must not require a GitHub API credential.
        health = self.run_deploy()
        self.assertEqual(self.config["backendImage"], BACKEND)
        self.assertEqual(self.config["frontendImage"], FRONTEND)
        self.assertIn(("compose.deploy", {"composeId": "staging", "title": TITLE,
                      "backendImage": BACKEND, "frontendImage": FRONTEND, "freshVolumes": False}), self.calls)
        self.assertIn(("compose.saveEnvironment", {"composeId": "staging", "env":
                      f"STAGING_BACKEND_IMAGE={BACKEND}\nSTAGING_FRONTEND_IMAGE={FRONTEND}\nSTAGING_SOURCE_SHA={SHA}"}), self.calls)
        health.assert_called_once()

    def test_guard_rejections_never_deploy(self):
        for change in ({"gateway": None}, {"environmentId": "production"}, {"composeId": "production"},
                       {"autoDeploy": True}, {"autoDeploy": None}, {"sourceType": "github"},
                       {"composeFile": "untrusted compose"}, {"composeStatus": "running"}):
            with self.subTest(change=change):
                original = dict(self.config)
                self.config.update(change)
                with self.assertRaises(deploy.DeployError):
                    self.run_deploy()
                self.config = original
        self.assertFalse(any(endpoint == "compose.deploy" for endpoint, _ in self.calls))

    def test_mutable_or_foreign_image_rejected(self):
        for image in ("ghcr.io/owner/repo-staging-backend:latest", FRONTEND,
                      "ghcr.io/other/repo-staging-backend@sha256:" + "a" * 64):
            with self.subTest(image=image):
                os.environ["STAGING_BACKEND_IMAGE"] = image
                with self.assertRaises(deploy.DeployError):
                    self.run_deploy()
        self.client.call.assert_not_called()

    def test_active_deployment_blocks_source_update(self):
        original = self.client.call.side_effect

        def active(endpoint, payload=None):
            if endpoint.startswith("deployment.allByCompose"):
                return [{"deploymentId": "another", "status": "running"}]
            return original(endpoint, payload)

        self.client.call.side_effect = active
        with self.assertRaisesRegex(deploy.DeployError, "active/unknown"):
            self.run_deploy()
        self.assertFalse(any(endpoint == "compose.saveEnvironment" for endpoint, _ in self.calls))

    def test_terminal_failure_does_not_check_health(self):
        for status in ("error", "cancelled"):
            with self.subTest(status=status), patch.object(deploy, "check_health") as health, \
                    patch.object(deploy, "wait_for_deployment", return_value=status):
                with self.assertRaisesRegex(deploy.DeployError, status):
                    deploy.deploy(self.client, "owner/repo")
                health.assert_not_called()

    def test_uncertain_trigger_is_not_retried(self):
        original = self.client.call.side_effect

        def fail_trigger(endpoint, payload=None):
            if endpoint == "compose.deploy":
                raise deploy.HTTPFailure(502)
            return original(endpoint, payload)

        self.client.call.side_effect = fail_trigger
        with self.assertRaises(deploy.DeployError):
            self.run_deploy()
        self.assertEqual(sum(call.args[0] == "compose.deploy"
                             for call in self.client.call.call_args_list), 1)
    def test_health_failure_is_reported(self):
        with patch.object(deploy, "wait_for_deployment", return_value="done"), \
                patch.object(deploy, "check_health", side_effect=deploy.DeployError("unhealthy")):
            with self.assertRaisesRegex(deploy.DeployError, "unhealthy"):
                deploy.deploy(self.client, "owner/repo")

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
        for branch, pr in (("test", "42"), ("", "1; echo hacked")):
            os.environ.update(INPUT_BRANCH=branch, INPUT_PR=pr)
            with self.assertRaises(deploy.DeployError):
                deploy.resolve(self.gh)

    def test_poll_matches_only_this_run(self):
        row = {"deploymentId": "new", "title": TITLE, "composeId": "staging", "status": "done"}
        self.client.history = Mock(side_effect=[
            [{**row, "title": "another-run"}],
            [{**row, "status": "running"}], [row]])
        with patch.object(deploy.time, "sleep"):
            self.assertEqual(deploy.wait_for_deployment(self.client, TITLE), "done")
        self.assertEqual(self.client.history.call_count, 3)

    def test_poll_timeout(self):
        self.client.history = Mock(return_value=[])
        with patch.object(deploy.time, "monotonic", side_effect=[0, 0, 1801]), \
                patch.object(deploy.time, "sleep"):
            with self.assertRaisesRegex(deploy.DeployError, "Timed out"):
                deploy.wait_for_deployment(self.client, TITLE)

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

    def test_gateway_headers_include_auth_and_ngrok_bypass(self):
        self.client = deploy.StagingGatewayClient()
        with patch.object(deploy, "request") as request:
            self.client.one()
        headers = request.call_args.args[1]
        self.assertEqual(headers["x-api-key"], "test-staging-token")
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

import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import staging_gateway as gateway


SHA = "a" * 40
BACKEND = "ghcr.io/owner/repo-staging-backend@sha256:" + "b" * 64
FRONTEND = "ghcr.io/owner/repo-staging-frontend@sha256:" + "c" * 64
TITLE = f"github-staging-12-1-{SHA}"


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = {"compose_id": "staging", "environment_id": "stage-env",
                         "compose_template": "trusted fixed compose", "repository": "owner/repo",
                         "deploy_token": "test-trigger-token", "public_host": "stage.example.test",
                         "frontend_url": "http://127.0.0.1:3011", "app_name": "fixed-staging"}
        self.gateway = gateway.Gateway(self.settings, self.temp.name)
        self.config = {"composeId": "staging", "environmentId": "stage-env", "sourceType": "raw",
                       "autoDeploy": False, "composeFile": "trusted fixed compose", "mounts": [],
                       "command": "", "createEnvFile": True, "appName": "fixed-staging",
                       "composeType": "docker-compose",
                       "env": "STAGING_POSTGRES_PASSWORD=private-db-secret\nSTAGING_AUTH_SECRET_KEY=private-auth-secret"}
        self.rows = []
        self.calls = []

        def upstream(endpoint, payload=None):
            self.calls.append((endpoint, payload))
            if endpoint.startswith("compose.one"):
                return self.config.copy()
            if endpoint.startswith("deployment.allByCompose"):
                return self.rows
            if endpoint == "compose.saveEnvironment":
                self.config["env"] = payload["env"]
            return True

        self.gateway.upstream = Mock(side_effect=upstream)

    def selection(self):
        return {"composeId": "staging", "env": f"STAGING_BACKEND_IMAGE={BACKEND}\nSTAGING_FRONTEND_IMAGE={FRONTEND}\nSTAGING_SOURCE_SHA={SHA}"}

    def trigger(self):
        return {"composeId": "staging", "title": TITLE, "description": "ignored",
                "freshVolumes": False}

    def test_config_hides_runtime_secrets_and_returns_selected_digests(self):
        self.gateway.control("compose.saveEnvironment", self.selection())
        result = self.gateway.control("compose.one", {})
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("env", result)
        self.assertEqual(result["backendImage"], BACKEND)
        self.assertEqual(result["sourceCommit"], SHA)
        self.assertIn("private-db-secret", self.config["env"])
        self.assertIn("private-auth-secret", self.config["env"])

    def test_no_environment_injection_or_mutable_images(self):
        for env in (self.selection()["env"] + "\nDOCKER_HOST=tcp://attacker",
                    self.selection()["env"].replace(BACKEND, "ghcr.io/owner/repo:latest"),
                    self.selection()["env"].replace(SHA, "not-a-sha")):
            with self.subTest(env=env), self.assertRaises(gateway.DeployError):
                self.gateway.control("compose.saveEnvironment", {"composeId": "staging", "env": env})
        self.assertFalse(any(e == "compose.saveEnvironment" for e, _ in self.calls))

    def test_extra_api_parameters_rejected(self):
        payload = self.selection()
        payload["createEnvFile"] = False
        with self.assertRaises(gateway.DeployError):
            self.gateway.control("compose.saveEnvironment", payload)

    def test_fixed_config_and_mounts_are_enforced(self):
        for change in ({"mounts": [{"hostPath": "/"}]}, {"composeFile": "untrusted"},
                       {"environmentId": "production"}, {"autoDeploy": True}, {"command": "other"},
                       {"createEnvFile": False}, {"appName": "production-volume-prefix"},
                       {"composeType": "stack"}):
            original = self.config.copy()
            self.config.update(change)
            with self.subTest(change=change), self.assertRaises(gateway.DeployError):
                self.gateway.control("compose.one", {})
            self.config = original

    def test_lost_trigger_response_retains_durable_lock(self):
        self.gateway.control("compose.saveEnvironment", self.selection())
        original = self.gateway.upstream.side_effect

        def lost(endpoint, payload=None):
            if endpoint == "compose.deploy":
                raise gateway.DeployError("lost response")
            return original(endpoint, payload)

        self.gateway.upstream.side_effect = lost
        with self.assertRaises(gateway.DeployError):
            self.gateway.control("compose.deploy", self.trigger())
        self.assertTrue(self.gateway.state.exists())
        with self.assertRaises(gateway.DeployError):
            self.gateway.control("compose.saveEnvironment", self.selection())

    def test_terminal_history_releases_only_matching_gateway_lock(self):
        self.gateway.state.write_text(json.dumps({"title": TITLE}))
        self.rows = [{"title": "other", "status": "done", "composeId": "staging"}]
        self.gateway.history()
        self.assertTrue(self.gateway.state.exists())
        self.rows = [{"title": TITLE, "status": "done", "composeId": "staging"}]
        self.gateway.history()
        self.assertFalse(self.gateway.state.exists())

    def test_trigger_commit_and_volume_reset_cannot_be_changed(self):
        self.gateway.control("compose.saveEnvironment", self.selection())
        for payload in ({**self.trigger(), "freshVolumes": True},
                        {**self.trigger(), "title": "github-staging-12-1-" + "d" * 40}):
            with self.assertRaises(gateway.DeployError):
                self.gateway.control("compose.deploy", payload)
        self.assertFalse(any(e == "compose.deploy" for e, _ in self.calls))

    def test_completed_deployment_title_cannot_be_replayed(self):
        self.gateway.control("compose.saveEnvironment", self.selection())
        self.rows = [{"deploymentId": "old", "title": TITLE, "status": "done", "composeId": "staging"}]
        with self.assertRaisesRegex(gateway.DeployError, "already been used"):
            self.gateway.control("compose.deploy", self.trigger())
        self.assertFalse(self.gateway.state.exists())
        self.assertFalse(any(endpoint == "compose.deploy" for endpoint, _ in self.calls))

    def test_unlisted_control_api_rejected(self):
        with self.assertRaises(gateway.DeployError):
            self.gateway.control("mounts.create", {})

    def serve(self):
        server = gateway.ThreadingHTTPServer(("127.0.0.1", 0), gateway.Handler)
        server.gateway = self.gateway
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_http_auth_and_production_id_denied(self):
        port = self.serve()
        for token, target in (("wrong", "staging"), ("test-trigger-token", "production")):
            connection = http.client.HTTPConnection("127.0.0.1", port)
            connection.request("GET", "/api/compose.one?composeId=" + target,
                               headers={"x-api-key": token})
            response = connection.getresponse()
            response.read()
            self.assertIn(response.status, (403, 409))
            connection.close()
        self.gateway.upstream.assert_not_called()

    def test_http_valid_request_does_not_return_secrets(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.serve())
        connection.request("GET", "/api/compose.one?composeId=staging",
                           headers={"x-api-key": "test-trigger-token"})
        response = connection.getresponse()
        body = response.read()
        self.assertEqual(response.status, 200)
        self.assertNotIn(b"private", body)
        self.assertEqual(json.loads(body)["gateway"], gateway.GATEWAY_IDENTITY)
        connection.close()

    def test_rejected_request_closes_connection_with_unread_body(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.serve())
        connection.putrequest("POST", "/api/compose.deploy")
        connection.putheader("Content-Length", "4096")
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        self.assertTrue(response.will_close)
        response.read()
        connection.close()

    def test_duplicate_content_lengths_are_rejected(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.serve())
        connection.putrequest("POST", "/api/compose.deploy")
        connection.putheader("x-api-key", "test-trigger-token")
        connection.putheader("Content-Length", "2")
        connection.putheader("Content-Length", "20")
        connection.endheaders(b"{}")
        response = connection.getresponse()
        self.assertEqual(response.status, 409)
        self.assertTrue(response.will_close)
        response.read()
        connection.close()
        self.gateway.upstream.assert_not_called()

    def test_unknown_api_goes_to_app_without_deployment_token(self):
        received = {}

        class App(gateway.BaseHTTPRequestHandler):
            def do_GET(self):
                received.update(self.headers.items())
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *_):
                pass

        app = gateway.ThreadingHTTPServer(("127.0.0.1", 0), App)
        threading.Thread(target=app.serve_forever, daemon=True).start()
        self.addCleanup(app.server_close)
        self.addCleanup(app.shutdown)
        self.settings["frontend_url"] = "http://127.0.0.1:" + str(app.server_address[1])
        connection = http.client.HTTPConnection("127.0.0.1", self.serve())
        connection.request("GET", "/api/mount.create", headers={
            "x-api-key": "test-trigger-token", "Proxy-Authorization": "private-proxy-credential",
            "Connection": "X-Private", "X-Private": "private-hop-header"})
        response = connection.getresponse()
        self.assertEqual(response.read(), b"ok")
        connection.close()
        self.assertFalse(any(key.lower() == "x-api-key" for key in received))
        self.assertFalse(any(key.lower() == "proxy-authorization" for key in received))
        self.assertFalse(any(key.lower() == "x-private" for key in received))
        self.gateway.upstream.assert_not_called()


if __name__ == "__main__":
    unittest.main()

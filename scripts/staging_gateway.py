"""Restricted staging-only facade over Dokploy's supported API and app proxy.

The real Dokploy key remains on the server. The Actions token cannot call other
Dokploy APIs, change Compose/mounts, or target another service. Standard library.
Run: python3 staging_gateway.py /config/settings.json /state
"""

import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sys
import threading
import urllib.parse

from deploy_staging import GATEWAY_IDENTITY, DeployError, image_reference, request, valid_sha


class Gateway:
    def __init__(self, settings, state_dir):
        self.settings = settings
        self.state = Path(state_dir) / "inflight.json"
        self.lock = threading.Lock()

    def upstream(self, endpoint, payload=None):
        return request(self.settings["dokploy_url"] + "/api/" + endpoint,
                       {"x-api-key": self.settings["dokploy_api_key"],
                        "Content-Type": "application/json", "Accept": "application/json"}, payload)

    def config(self):
        data = self.upstream("compose.one?composeId=" + self.settings["compose_id"])
        if (data.get("composeId") != self.settings["compose_id"]
                or data.get("environmentId") != self.settings["environment_id"]
                or data.get("sourceType") != "raw" or data.get("autoDeploy") is not False
                or data.get("composeType") != "docker-compose"
                or data.get("composeFile") != self.settings["compose_template"]
                or data.get("mounts") or data.get("command")
                or data.get("createEnvFile") is not True
                or data.get("appName") != self.settings["app_name"]):
            raise DeployError("Staging service differs from the approved fixed configuration")
        return data

    def history(self):
        rows = self.upstream("deployment.allByCompose?composeId=" + self.settings["compose_id"])
        if not isinstance(rows, list):
            raise DeployError("Invalid deployment history")
        if self.state.exists():
            inflight = json.loads(self.state.read_text())
            matches = [r for r in rows if r.get("title") == inflight["title"]]
            if len(matches) == 1 and matches[0].get("status") in ("done", "error", "cancelled"):
                self.state.unlink()
        return [{k: row.get(k) for k in ("deploymentId", "title", "status", "composeId")}
                for row in rows]

    def idle(self):
        rows = self.history()
        if self.state.exists() or any(r["status"] not in ("done", "error", "cancelled") for r in rows):
            raise DeployError("A staging deployment is active or uncertain; reconcile it first")
        return rows

    @staticmethod
    def values(env):
        return dict(line.split("=", 1) for line in (env or "").splitlines()
                    if line and not line.startswith("#") and "=" in line)

    def control(self, endpoint, payload):
        # The caller identity is checked by Handler; no caller-controlled URL,
        # service ID, arbitrary env key, Compose text or filesystem path is used.
        if endpoint == "compose.one":
            data = self.config()
            values = self.values(data.get("env"))
            return {"gateway": GATEWAY_IDENTITY,
                    **{k: data.get(k) for k in ("composeId", "environmentId", "sourceType",
                                               "autoDeploy", "composeStatus", "composeFile")},
                    "sourceCommit": values.get("STAGING_SOURCE_SHA"),
                    "backendImage": values.get("STAGING_BACKEND_IMAGE"),
                    "frontendImage": values.get("STAGING_FRONTEND_IMAGE")}
        if endpoint == "deployment.allByCompose":
            return self.history()
        if endpoint == "compose.saveEnvironment":
            if set(payload) != {"composeId", "env"}:
                raise DeployError("Only staging image references may be changed")
            values = self.values(payload["env"])
            keys = {"STAGING_BACKEND_IMAGE", "STAGING_FRONTEND_IMAGE", "STAGING_SOURCE_SHA"}
            if set(values) != keys or len(payload["env"].splitlines()) != 3:
                raise DeployError("Unexpected staging environment fields")
            image_reference(self.settings["repository"], "backend", values["STAGING_BACKEND_IMAGE"])
            image_reference(self.settings["repository"], "frontend", values["STAGING_FRONTEND_IMAGE"])
            valid_sha(values["STAGING_SOURCE_SHA"])
            self.idle()
            config = self.config()
            kept = [line for line in (config.get("env") or "").splitlines()
                    if line.split("=", 1)[0] not in keys]
            merged = "\n".join(kept + [f"{key}={values[key]}" for key in sorted(keys)])
            self.upstream("compose.saveEnvironment", {"composeId": self.settings["compose_id"],
                          "env": merged, "createEnvFile": config.get("createEnvFile", True)})
            return {"success": True}
        if endpoint == "compose.deploy":
            if set(payload) != {"composeId", "title", "backendImage", "frontendImage", "freshVolumes"}:
                raise DeployError("Invalid deployment request")
            title = payload["title"]
            if not re.fullmatch(r"github-staging-[0-9]+-[0-9]+-[0-9a-f]{40}", title):
                raise DeployError("Invalid deployment identity")
            config = self.config()
            selected = self.values(config.get("env"))
            sha = selected.get("STAGING_SOURCE_SHA")
            image_reference(self.settings["repository"], "backend", payload["backendImage"])
            image_reference(self.settings["repository"], "frontend", payload["frontendImage"])
            if (title.rsplit("-", 1)[-1] != sha or payload["freshVolumes"] is not False
                    or payload["backendImage"] != selected.get("STAGING_BACKEND_IMAGE")
                    or payload["frontendImage"] != selected.get("STAGING_FRONTEND_IMAGE")):
                raise DeployError("Deployment must match the selected images and preserve volumes")
            history = self.idle()
            if any(row["title"] == title for row in history):
                raise DeployError("This deployment identity has already been used")
            # Persist before the trigger: a lost response must never unlock it.
            temporary = self.state.with_suffix(".tmp")
            temporary.write_text(json.dumps({"title": title}))
            temporary.replace(self.state)
            self.upstream("compose.deploy", {"composeId": self.settings["compose_id"],
                          "title": title, "description": f"Exact source commit {sha}",
                          "freshVolumes": False})
            return {"success": True}
        raise DeployError("Endpoint is not permitted")


CONTROL_METHODS = {"compose.one": "GET", "deployment.allByCompose": "GET",
                   "compose.saveEnvironment": "POST", "compose.deploy": "POST"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *_):
        pass  # URLs, application bodies, headers and credentials never enter logs.

    def json_response(self, status, data):
        # Close on rejection instead of interpreting an unread body as the next
        # request on this HTTP/1.1 connection.
        self.close_connection = True
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def body_length(self, limit):
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) > 1 or self.headers.get("Transfer-Encoding"):
            raise DeployError("Ambiguous request body framing")
        value = lengths[0] if lengths else "0"
        if not re.fullmatch(r"[0-9]+", value) or int(value) > limit:
            raise DeployError("Invalid request size")
        return int(value)

    def handle_request(self):
        parsed = urllib.parse.urlsplit(self.path)
        endpoint = parsed.path.removeprefix("/api/")
        if endpoint not in CONTROL_METHODS:
            # Never accidentally expose Dokploy UI or an arbitrary API route.
            return self.proxy_app()
        token = self.headers.get("x-api-key", "")
        if not hmac.compare_digest(token.encode(), self.server.gateway.settings["deploy_token"].encode()):
            return self.json_response(403, {"error": "Forbidden"})
        if self.command != CONTROL_METHODS[endpoint]:
            return self.json_response(405, {"error": "Method not allowed"})
        try:
            size = self.body_length(4096)
            if self.command == "POST":
                if parsed.query:
                    raise DeployError("Query is not permitted")
                payload = json.loads(self.rfile.read(size))
            else:
                query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
                payload = {k: v[0] for k, v in query.items() if len(v) == 1}
                if set(payload) != {"composeId"}:
                    raise DeployError("Invalid query")
            if payload.get("composeId") != self.server.gateway.settings["compose_id"]:
                raise DeployError("Only the staging service is permitted")
            with self.server.gateway.lock:
                result = self.server.gateway.control(endpoint, payload)
            return self.json_response(200, result)
        except (DeployError, ValueError, TypeError, KeyError, AttributeError, OSError):
            return self.json_response(409, {"error": "Staging request rejected; inspect gateway configuration privately"})

    def proxy_app(self):
        if not self.path.startswith("/") or self.path.startswith("//"):
            return self.json_response(400, {"error": "Invalid path"})
        connection = None
        response_started = False
        try:
            length = self.body_length(32 * 1024 * 1024)
            upstream = urllib.parse.urlsplit(self.server.gateway.settings["frontend_url"])
            connection = http.client.HTTPConnection(upstream.hostname, upstream.port, timeout=300)
            excluded = {"connection", "host", "transfer-encoding", "x-api-key", "proxy-authorization",
                        "proxy-authenticate", "keep-alive", "te", "trailer", "upgrade",
                        "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto"}
            excluded.update(value.strip().lower()
                            for value in self.headers.get("Connection", "").split(","))
            headers = {k: v for k, v in self.headers.items() if k.lower() not in excluded}
            headers.update({"Host": self.server.gateway.settings["public_host"],
                            "X-Forwarded-Proto": "https"})
            connection.request(self.command, self.path, self.rfile.read(length), headers)
            response = connection.getresponse()
            self.send_response(response.status)
            response_started = True
            for key, value in response.getheaders():
                if key.lower() not in {"connection", "transfer-encoding", "keep-alive"}:
                    self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            while chunk := response.read1(64 * 1024):
                self.wfile.write(chunk)
                self.wfile.flush()
        except DeployError:
            self.json_response(400, {"error": "Invalid request body framing"})
        except (OSError, ValueError, http.client.HTTPException):
            self.close_connection = True
            if not response_started:
                self.json_response(502, {"error": "Staging application is not available"})
        finally:
            if connection:
                connection.close()

    do_GET = handle_request
    do_POST = handle_request
    do_PUT = handle_request
    do_DELETE = handle_request
    do_PATCH = handle_request
    do_OPTIONS = handle_request
    do_HEAD = handle_request


if __name__ == "__main__":
    settings = json.loads(Path(sys.argv[1]).read_text())
    gateway = Gateway(settings, sys.argv[2])
    server = ThreadingHTTPServer(("127.0.0.1", 3012), Handler)
    server.daemon_threads = True
    server.gateway = gateway
    server.serve_forever()

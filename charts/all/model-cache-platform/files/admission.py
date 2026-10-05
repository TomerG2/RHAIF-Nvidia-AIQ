"""Keep platform cache configuration and GPU tolerations during reconciliation.

No Kubernetes token or API permissions are required by this admission server.
It changes only the platform cache DaemonSet/config and cache-job tolerations.
"""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import ssl


def patches(request, config):
    obj = request["object"]
    if request.get("namespace") != config["modelCache"]["jobNamespace"]:
        return []
    name, kind = obj["metadata"].get("name"), request["kind"]["kind"]
    tolerations = config["serving"]["tolerations"]
    if kind == "DaemonSet" and name == "kserve-localmodelnode-agent":
        spec, path = obj["spec"]["template"]["spec"], "/spec/template/spec/tolerations"
    elif kind == "Pod":
        spec, path = obj["spec"], "/spec/tolerations"
        if not any(o.get("kind") in ("Job", "DaemonSet") for o in obj["metadata"].get("ownerReferences", [])):
            return []
        account = spec.get("serviceAccountName", "default")
        if account not in {"aiq-model-reader", "kserve-localmodelnode-agent", "kserve-localmodelnode-agent-permfix"}:
            return []
        if account == "aiq-model-reader":
            containers = spec.get("containers", [])
            if not any(c.get("image") == config["modelTools"]["image"] and c.get("name") == "storage-initializer"
                       for c in containers):
                return []
    elif kind == "ConfigMap" and name == "inferenceservice-config":
        local = json.loads(obj["data"]["localModel"])
        desired = {**local, "jobNamespace": config["modelCache"]["jobNamespace"],
                   "defaultJobImage": config["modelTools"]["image"],
                   "localModelAgentImage": config["rhoai"]["localModelAgentImage"]}
        return [] if local == desired else [{"op": "replace", "path": "/data/localModel", "value": json.dumps(desired)}]
    else:
        return []
    current = spec.get("tolerations", [])
    merged = current + [t for t in tolerations if t not in current]
    return [] if current == merged else [{"op": "add", "path": path, "value": merged}]


class Server(ThreadingHTTPServer):
    def get_request(self):
        sock, address = super().get_request()
        # Projected Secret files change atomically; load each new connection's
        # certificate instead of retaining an expiring SSLContext indefinitely.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain("/tls/tls.crt", "/tls/tls.key")
        try:
            sock.settimeout(10)
            return context.wrap_socket(sock, server_side=True), address
        except Exception:
            sock.close()
            raise


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 2 * 1024**2:
            self.send_error(413)
            return
        review = json.loads(self.rfile.read(length))
        request = review["request"]
        response = {"uid": request["uid"], "allowed": True}
        patch = patches(request, CONFIG)
        if patch:
            response.update(patchType="JSONPatch", patch=base64.b64encode(json.dumps(patch).encode()).decode())
        body = json.dumps({"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview", "response": response}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    CONFIG = json.loads(Path("/scripts/config.json").read_text())
    Server(("0.0.0.0", 9443), Handler).serve_forever()

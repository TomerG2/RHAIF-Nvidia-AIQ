"""Prometheus observations for publication, cache copies, and warm startup.

Download timings come from the verified downloader's final JSON log record.
An absent observation means no completed download was observed, never zero time.
"""
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import ssl
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from gate import api

observations = {}
snapshot = "aiq_model_metrics_collection_success 0\n"


def seconds(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def logs(namespace, pod, container):
    token = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text()
    path = f"/api/v1/namespaces/{namespace}/pods/{pod}/log?" + urlencode({"container": container, "tailLines": 20})
    request = Request("https://kubernetes.default.svc" + path, headers={"Authorization": "Bearer " + token})
    context = ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    with urlopen(request, context=context, timeout=10) as response:
        return response.read().decode()


def collect():
    global snapshot
    namespace, cache_name = os.environ["NAMESPACE"], os.environ["CACHE_NAME"]
    seen = set()
    while True:
        lines = []
        try:
            cache = api("/apis/serving.kserve.io/v1alpha1/localmodelcaches/" + cache_name)
            for state, count in cache.get("status", {}).get("copies", {}).items():
                lines.append(f'aiq_model_cache_copies{{state={json.dumps(state)}}} {int(count)}')
            for ns in {namespace, os.environ["CACHE_NAMESPACE"]}:
                jobs = api(f"/apis/batch/v1/namespaces/{ns}/jobs")["items"]
                pods = api(f"/api/v1/namespaces/{ns}/pods")["items"]
                for job in jobs:
                    uid = job["metadata"]["uid"]
                    labels = job["metadata"].get("labels", {})
                    publication = labels.get("aiq.rhai.redhat.com/stage") == "publication"
                    if (uid in seen or not job.get("status", {}).get("succeeded")
                            or not (publication or labels.get("model") == cache_name)):
                        continue
                    for pod in pods:
                        if not any(o["uid"] == uid for o in pod["metadata"].get("ownerReferences", [])):
                            continue
                        container = "publish" if publication else "storage-initializer"
                        for line in logs(ns, pod["metadata"]["name"], container).splitlines():
                            try:
                                event = json.loads(line)
                            except ValueError:
                                continue
                            if event.get("stage") in ("publication_ready", "cache_ready"):
                                key = ("publication" if publication else "cache", labels.get("node", "publisher"))
                                observations[key] = float(event["download_seconds"])
                        seen.add(uid)
                if ns == namespace:
                    for pod in pods:
                        if "aiq.rhai.redhat.com/serving" not in pod["metadata"].get("labels", {}):
                            continue
                        status = pod.get("status", {})
                        ready = next((c for c in status.get("conditions", []) if c["type"] == "Ready" and c["status"] == "True"), None)
                        name = json.dumps(pod["metadata"]["name"])
                        lines.append(f'aiq_serving_pod_ready{{pod={name}}} {1 if ready else 0}')
                        if ready and status.get("startTime"):
                            duration = seconds(ready["lastTransitionTime"]) - seconds(status["startTime"])
                            lines.append(f'aiq_warm_startup_seconds{{pod={name}}} {max(0, duration)}')
            lines.append("aiq_model_metrics_collection_success 1")
        except Exception:
            lines.append("aiq_model_metrics_collection_success 0")
        for (stage, node), duration in observations.items():
            lines.append(f'aiq_model_download_seconds{{stage={json.dumps(stage)},node={json.dumps(node)}}} {duration}')
        snapshot = "\n".join(lines) + "\n"
        time.sleep(30)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path in ("/metrics", "/health") else 404)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(snapshot.encode())

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    threading.Thread(target=collect, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 9090), Handler).serve_forever()

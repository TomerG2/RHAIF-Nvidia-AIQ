from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
GLOBAL = ROOT / "values-global.yaml"
CHART = ROOT / "charts/all/vllm-inference-service"


def render(tmp_path, values=None, profile="nvfp4", chart=CHART):
    override = tmp_path / "override.yaml"
    override.write_text(yaml.safe_dump(values or {}))
    command = ["helm", "template", "vllm-inference-service", str(chart), "-n", "aiq-inference",
               "-f", str(GLOBAL), "-f", str(ROOT / f"profiles/{profile}.yaml"), "-f", str(override)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise ValueError(result.stderr)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def kind(documents, name):
    return next(d for d in documents if d["kind"] == name)


@pytest.mark.parametrize("profile,gpus", [("nvfp4", 1), ("bf16", 1), ("bf16-tp4", 4)])
def test_profiles_generate_consistent_gpu_counts(tmp_path, profile, gpus):
    documents = render(tmp_path, profile=profile)
    model = kind(documents, "InferenceService")["spec"]["predictor"]["model"]
    args = kind(documents, "ServingRuntime")["spec"]["containers"][0]["args"]
    assert model["resources"]["requests"]["nvidia.com/gpu"] == str(gpus)
    assert model["resources"]["limits"]["nvidia.com/gpu"] == str(gpus)
    assert f"--tensor-parallel-size={gpus}" in args
    assert "--pipeline-parallel-size=1" in args
    assert model["storageUri"] == kind(documents, "LocalModelCache")["spec"]["sourceModelUri"]
    assert not any("pip install" in str(doc) for doc in documents)


def test_independent_replicas_are_spread_and_do_not_mount_legacy_pvc(tmp_path):
    documents = render(tmp_path, {"global": {"serving": {"replicas": 3}}})
    predictor = kind(documents, "InferenceService")["spec"]["predictor"]
    assert predictor["maxReplicas"] == predictor["minReplicas"] == 3
    assert predictor["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    volumes = kind(documents, "ServingRuntime")["spec"]["volumes"]
    assert not any("persistentVolumeClaim" in volume for volume in volumes)


@pytest.mark.parametrize("nodes,gpus", [(2, 1), (2, 2), (4, 8)])
def test_distributed_layout_has_explicit_leader_and_worker_startup(tmp_path, nodes, gpus):
    documents = render(tmp_path, {"global": {"serving": {
        "nodesPerReplica": nodes, "gpusPerNode": gpus, "tensorParallel": gpus, "pipelineParallel": nodes}}})
    assert not any(d["kind"] == "InferenceService" for d in documents)
    service = kind(documents, "LLMInferenceService")["spec"]
    assert service["parallelism"] == {"tensor": gpus, "pipeline": nodes}
    assert service["model"]["uri"] == kind(documents, "LocalModelCache")["spec"]["sourceModelUri"]
    config = kind(documents, "LLMInferenceServiceConfig")["spec"]
    for role in ("template", "worker"):
        container = config[role]["containers"][0]
        env = {v["name"]: v.get("value") for v in container["env"]}
        assert env["AIQ_NODES"] == str(nodes)
        assert env["AIQ_GPUS"] == str(gpus)
        assert "LWS_WORKER_INDEX" in container["command"][2]
        assert "--headless" in container["command"][2]
        assert "supports_pp" in container["command"][2]
        assert "startupProbe" in container and "readinessProbe" in container
        assert container["resources"]["limits"]["nvidia.com/gpu"] == str(gpus)
        assert all("persistentVolumeClaim" not in v for v in config[role]["volumes"])
    selector = kind(documents, "Service")["spec"]["selector"]
    assert selector["leaderworkerset.sigs.k8s.io/worker-index"] == "0"


@pytest.mark.parametrize("values,reason", [
    ({"global": {"serving": {"gpusPerNode": 2}}}, "must equal"),
    ({"global": {"serving": {"replicas": 0}}}, "positive integer"),
    ({"global": {"serving": {"gpusPerNode": 1.5}}}, "positive integer"),
    ({"global": {"serving": {"nodesPerReplica": 2, "tensorParallel": 2}}}, "pipelineParallel=nodesPerReplica"),
    ({"global": {"model": {"revision": "main"}}}, "immutable"),
    ({"vllmServingRuntime": {"args": ["--tensor-parallel-size=7"]}}, "managed"),
    ({"vllmServingRuntime": {"args": ["-tp", "7"]}}, "managed"),
    ({"vllmServingRuntime": {"args": ["-tp7"]}}, "managed"),
    ({"vllmServingRuntime": {"args": ["--served_model_name=wrong"]}}, "managed"),
    ({"global": {"serving": {"rdmaResources": {"nvidia.com/gpu": "8"}}}}, "extended device"),
    ({"global": {"serving": {"ncclEnv": {"LWS_WORKER_INDEX": "1"}}}}, "ncclEnv"),
    ({"global": {"modelStore": {"endpoint": "http://rustfs"}}}, "HTTPS"),
    ({"global": {"serving": {"runtimeImage": "vllm:latest"}}}, "digest"),
    ({"global": {"modelTools": {"transferConcurrency": 0}}}, "transferConcurrency"),
])
def test_invalid_configuration_is_rejected(tmp_path, values, reason):
    with pytest.raises(ValueError, match=reason):
        render(tmp_path, values)


def test_publication_cache_and_serving_order(tmp_path):
    documents = render(tmp_path)
    jobs = {d["metadata"]["name"]: d for d in documents if d["kind"] == "Job"}
    publication = next(d for name, d in jobs.items() if name.startswith("publish-"))
    wave = lambda d: int(d["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"])
    assert wave(jobs["aiq-model-preflight"]) < wave(publication)
    assert wave(publication) < wave(kind(documents, "LocalModelCache"))
    assert wave(kind(documents, "LocalModelCache")) < wave(jobs["aiq-model-cache"])
    assert wave(jobs["aiq-model-cache"]) < wave(jobs["aiq-model-switch"])
    assert wave(jobs["aiq-model-switch"]) < wave(kind(documents, "InferenceService"))
    assert wave(kind(documents, "InferenceService")) < wave(jobs["aiq-model-serving"])
    original = publication["metadata"]["name"]
    scratch = next(d for d in documents if d["kind"] == "PersistentVolumeClaim" and d["metadata"]["name"] == "aiq-model-publication-scratch")
    assert wave(scratch) == wave(publication)  # WaitForFirstConsumer must see the job in the same wave.
    assert publication["spec"]["template"]["spec"]["volumes"][-1]["persistentVolumeClaim"]["claimName"] == scratch["metadata"]["name"]
    assert "ephemeral-storage" not in publication["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]
    changed = render(tmp_path, {"global": {"serving": {"replicas": 2}}})
    assert original != next(d["metadata"]["name"] for d in changed if d["kind"] == "Job" and d["metadata"]["name"].startswith("publish-"))


def test_cache_revisions_and_disk_reservation(tmp_path):
    previous = {"hfRepo": "test/previous", "revision": "a" * 40, "size": "20Gi"}
    documents = render(tmp_path, {"global": {"modelCache": {"retainedRevisions": [previous]}}})
    caches = [d for d in documents if d["kind"] == "LocalModelCache"]
    assert len(caches) == 2 and len({d["metadata"]["name"] for d in caches}) == 2
    assert all("Prune=false" in d["metadata"]["annotations"]["argocd.argoproj.io/sync-options"] for d in caches)
    downloader = next(d for d in documents if d["kind"] == "ClusterStorageContainer" and d["spec"]["workloadType"] == "localModelDownloadJob")
    assert "shutil.disk_usage" in downloader["spec"]["container"]["command"][2]
    assert "tempfile.TemporaryFile" in downloader["spec"]["container"]["command"][2]
    assert next(e["value"] for e in downloader["spec"]["container"]["env"] if e["name"] == "DISK_RESERVE_BYTES") == "1073741824"
    publication = next(d for d in documents if d["kind"] == "Job" and d["metadata"]["name"].startswith("publish-"))
    for container in publication["spec"]["template"]["spec"]["containers"] + publication["spec"]["template"]["spec"]["initContainers"]:
        assert next(e["value"] for e in container["env"] if e["name"] == "DISK_RESERVE_BYTES") == "1073741824"


def test_duplicate_rollback_revision_is_rejected(tmp_path):
    model = yaml.safe_load((ROOT / "profiles/nvfp4.yaml").read_text())["global"]["model"]
    with pytest.raises(ValueError, match="must be distinct"):
        render(tmp_path, {"global": {"modelCache": {"retainedRevisions": [model]}}})


def test_legacy_pvc_keeps_its_recorded_capacity(tmp_path):
    documents = render(tmp_path, {"migration": {"legacyPVCSize": "150Gi"}})
    claim = kind(documents, "PersistentVolumeClaim")
    assert claim["spec"]["resources"]["requests"]["storage"] == "150Gi"
    assert "Prune=false" in claim["metadata"]["annotations"]["argocd.argoproj.io/sync-options"]


def test_rustfs_standalone_and_distributed(tmp_path):
    chart = ROOT / "charts/all/rustfs"
    documents = render(tmp_path, chart=chart)
    assert not any(d["kind"] == "Ingress" for d in documents)
    deployment = kind(documents, "Deployment")
    assert deployment["spec"]["replicas"] == 1
    pod = deployment["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsUser"] == 10001
    assert all("@sha256:" in c["image"] for c in pod["containers"] + pod["initContainers"])
    assert kind(documents, "SecurityContextConstraints")["users"] == ["system:serviceaccount:aiq-inference:rustfs"]
    overlay = yaml.safe_load((ROOT / "overrides/values-rustfs-distributed.yaml").read_text())
    distributed = render(tmp_path, overlay, chart=chart)
    statefulset = kind(distributed, "StatefulSet")["spec"]
    assert statefulset["replicas"] == 4
    assert len(statefulset["volumeClaimTemplates"]) == 1
    assert statefulset["template"]["spec"]["affinity"]["podAntiAffinity"]


def test_workflow_endpoint_switch_keeps_remote_deep_models(tmp_path):
    chart = ROOT / "charts/aiq-workflow-config"
    one = render(tmp_path, chart=chart)
    multi = render(tmp_path, {"global": {"serving": {"nodesPerReplica": 2}}}, chart=chart)
    parse = lambda docs: yaml.safe_load(kind(docs, "ConfigMap")["data"]["config_hybrid_lightning.yml"])
    old, new = parse(one), parse(multi)
    for name in ("nemotron_lightning_intent_llm", "nemotron_lightning_agent_llm"):
        assert "-distributed." in new["llms"][name]["base_url"]
        old["llms"][name]["base_url"] = new["llms"][name]["base_url"]
    assert old == new


def test_internal_worker_ports_are_isolated_from_http_clients(tmp_path):
    documents = render(tmp_path)
    policy = kind(documents, "NetworkPolicy")["spec"]
    peer = policy["ingress"][0]
    assert peer["from"] == [{"podSelector": {"matchLabels": {
        "aiq.rhai.redhat.com/serving": "vllm-inference-service"}}}]
    clients = policy["ingress"][1]
    assert clients["ports"] == [{"protocol": "TCP", "port": 8080}]
    assert any(p.get("namespaceSelector", {}).get("matchLabels", {}).get("kubernetes.io/metadata.name") == "aiq"
               for p in clients["from"])
    gates = [d for d in documents if d["kind"] == "Job" and d["metadata"]["name"].startswith("aiq-model-")]
    assert all(j["spec"]["template"]["metadata"]["labels"]["aiq.rhai.redhat.com/model-gate"] == "true" for j in gates)

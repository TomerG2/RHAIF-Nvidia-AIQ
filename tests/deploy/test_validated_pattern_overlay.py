# SPDX-FileCopyrightText: Copyright (c) 2026, Red Hat, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
OVERLAY_BASE_PATH = REPO_ROOT / "overrides" / "values-openshift-base.yaml"
OVERLAY_HYBRID_PATH = REPO_ROOT / "overrides" / "values-openshift-hybrid-lightning.yaml"
CHART_HYBRID_CONFIG = REPO_ROOT / "charts" / "aiq-workflow-config" / "files" / "config_hybrid_lightning.yml"
WORKFLOW_CONFIG_CHART = REPO_ROOT / "charts" / "aiq-workflow-config"
AIQ2_WEB_CHART = REPO_ROOT / "charts" / "aiq2-web"
VLLM_CHART = REPO_ROOT / "charts" / "all" / "vllm-inference-service"
NFD_CHART = REPO_ROOT / "charts" / "all" / "nfd-config"
NVIDIA_CONFIG_CHART = REPO_ROOT / "charts" / "all" / "nvidia-gpu-config"
RHODS_CHART = REPO_ROOT / "charts" / "all" / "rhods"
ESO_BINDINGS_CHART = REPO_ROOT / "charts" / "all" / "eso-bindings"
NVIDIA_AIQ_VALUES = REPO_ROOT / "charts" / "aiq2-web" / "helm-charts-k8s" / "aiq" / "values.yaml"
DEFAULT_LIGHTNING_BASE_URL = (
    "http://vllm-inference-service-predictor.aiq-inference.svc.cluster.local/v1"
)
VALUES_GLOBAL = REPO_ROOT / "values-global.yaml"
VALUES_SECRET_TEMPLATE = REPO_ROOT / "values-secret.yaml.template"
PROFILE_NVFP4 = REPO_ROOT / "profiles" / "nvfp4.yaml"
PROFILE_BF16 = REPO_ROOT / "profiles" / "bf16.yaml"
PROFILE_BF16_TP4 = REPO_ROOT / "profiles" / "bf16-tp4.yaml"
VARIANT_NVFP4 = REPO_ROOT / "variants" / "nvfp4" / "values-nvfp4.yaml"
VARIANT_BF16 = REPO_ROOT / "variants" / "bf16" / "values-bf16.yaml"
VARIANT_BF16_TP4 = REPO_ROOT / "variants" / "bf16-tp4" / "values-bf16-tp4.yaml"
VLLM_CHART_VALUES = REPO_ROOT / "charts" / "all" / "vllm-inference-service" / "values.yaml"
PROFILE_VALUE_FILE = "/profiles/{{ $.Values.global.hardwareProfile }}.yaml"
SERVED_MODEL_NAME = "nemotron-3.5-lightning-30b-a3b"
BF16_SERVED_MODEL_NAME = "nemotron-3.5-lightning-30b-a3b-bf16"
HF_REPO = "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4"
BF16_HF_REPO = "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16"


def _render_openshift_overlay(*extra_value_files: str):
    return _render_helm_chart(
        AIQ2_WEB_CHART,
        "aiq",
        "aiq",
        str(OVERLAY_BASE_PATH),
        str(extra_value_files[0]),
    )


def _render_helm_chart(chart_path: Path, release_name: str, namespace: str, *value_files: str) -> list[dict]:
    command = [
        "helm",
        "template",
        release_name,
        str(chart_path),
        "-n",
        namespace,
    ]
    for value_file in value_files:
        command.extend(["-f", value_file])
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _write_values_file(tmp_path: Path, values: dict) -> str:
    values_file = tmp_path / "values.yaml"
    values_file.write_text(yaml.safe_dump(values), encoding="utf-8")
    return str(values_file)


def _render_vllm_chart(tmp_path: Path) -> list[dict]:
    values_global = yaml.safe_load(VALUES_GLOBAL.read_text(encoding="utf-8"))
    return _render_helm_chart(
        VLLM_CHART,
        "vllm-inference-service",
        "aiq-inference",
        _write_values_file(tmp_path, values_global),
    )


def test_hybrid_openshift_overlay_files_exist():
    assert OVERLAY_BASE_PATH.is_file()
    assert OVERLAY_HYBRID_PATH.is_file()


def _workflow_config(*value_files: str) -> dict:
    manifests = _render_helm_chart(WORKFLOW_CONFIG_CHART, "aiq-workflow-config", "aiq", *value_files)
    configmap = next(manifest for manifest in manifests if manifest.get("kind") == "ConfigMap")
    return yaml.safe_load(configmap["data"]["config_hybrid_lightning.yml"])


def test_hybrid_config_chart_file_is_valid_yaml():
    assert CHART_HYBRID_CONFIG.is_file()
    assert CHART_HYBRID_CONFIG.stat().st_size > 0
    config = _workflow_config()
    assert config["general"]["front_end"]["_type"] == "aiq_api"
    assert config["llms"]["nemotron_lightning_intent_llm"]["_type"] == "openai"
    assert config["llms"]["nemotron_ultra_llm"]["_type"] == "nim"
    assert config["llms"]["nemotron_ultra_llm"]["model_name"] == "nvidia/nemotron-3-super-120b-a12b"
    assert config["llms"]["nemotron_ultra_writer_llm"]["model_name"] == "nvidia/nemotron-3-super-120b-a12b"
    assert config["functions"]["shallow_research_agent"]["llm"] == "nemotron_lightning_agent_llm"
    assert config["functions"]["clarifier_agent"]["llm"] == "nemotron_ultra_llm"
    assert config["llms"]["nemotron_lightning_agent_llm"]["max_tokens"] == 1536
    assert config["llms"]["nemotron_lightning_intent_llm"]["max_tokens"] == 1024
    intent_extra = config["llms"]["nemotron_lightning_intent_llm"]["extra_body"]
    agent_extra = config["llms"]["nemotron_lightning_agent_llm"]["extra_body"]
    assert intent_extra["chat_template_kwargs"]["enable_thinking"] is False
    assert agent_extra["chat_template_kwargs"]["enable_thinking"] is True
    assert agent_extra["thinking_token_budget"] == 512
    assert config["llms"]["nemotron_lightning_intent_llm"]["model_name"] == SERVED_MODEL_NAME
    intent_base_url = config["llms"]["nemotron_lightning_intent_llm"]["base_url"]
    agent_base_url = config["llms"]["nemotron_lightning_agent_llm"]["base_url"]
    assert intent_base_url == agent_base_url
    assert intent_base_url.startswith("${AIQ_LIGHTNING_BASE_URL:-")
    assert DEFAULT_LIGHTNING_BASE_URL in intent_base_url


def test_hybrid_workflow_model_name_matches_values_global():
    config = _workflow_config(str(VALUES_GLOBAL))
    values_global = yaml.safe_load(VALUES_GLOBAL.read_text(encoding="utf-8"))
    served_name = values_global["global"]["model"]["servedName"]
    assert config["llms"]["nemotron_lightning_intent_llm"]["model_name"] == served_name
    assert config["llms"]["nemotron_lightning_agent_llm"]["model_name"] == served_name


def test_workflow_config_chart_renders_hybrid_configmap():
    manifests = _render_helm_chart(WORKFLOW_CONFIG_CHART, "aiq-workflow-config", "aiq")
    configmaps = [manifest for manifest in manifests if manifest.get("kind") == "ConfigMap"]
    assert len(configmaps) == 1
    assert configmaps[0]["metadata"]["name"] == "aiq-workflow-config"
    assert "config_hybrid_lightning.yml" in configmaps[0]["data"]
    assert "nemotron_lightning_intent_llm" in configmaps[0]["data"]["config_hybrid_lightning.yml"]


def test_nfd_chart_renders_with_chart_defaults(tmp_path: Path):
    manifests = _render_helm_chart(NFD_CHART, "nfd-config", "openshift-nfd")
    kinds = {manifest["kind"] for manifest in manifests}
    assert "NodeFeatureDiscovery" in kinds


def test_gpu_stack_charts_render_expected_kinds(tmp_path: Path):
    values_global = yaml.safe_load(VALUES_GLOBAL.read_text(encoding="utf-8"))
    values_file = _write_values_file(tmp_path, values_global)

    nfd_kinds = {manifest["kind"] for manifest in _render_helm_chart(NFD_CHART, "nfd-config", "openshift-nfd", values_file)}
    nvidia_kinds = {
        manifest["kind"] for manifest in _render_helm_chart(NVIDIA_CONFIG_CHART, "nvidia-config", "nvidia-gpu-operator")
    }
    rhods_kinds = {manifest["kind"] for manifest in _render_helm_chart(RHODS_CHART, "openshift-ai", "redhat-ods-operator")}
    assert "NodeFeatureDiscovery" in nfd_kinds
    assert "ClusterPolicy" in nvidia_kinds
    assert "DataScienceCluster" in rhods_kinds


def test_secret_template_writes_vault_identities_without_target_namespaces():
    template = yaml.safe_load(VALUES_SECRET_TEMPLATE.read_text(encoding="utf-8"))
    secrets = {secret["name"]: secret for secret in template["secrets"]}
    aiq_fields = [field["name"] for field in secrets["aiq-credentials"]["fields"]]
    hf_fields = [field["name"] for field in secrets["huggingface-secret"]["fields"]]
    db_password = next(
        field for field in secrets["aiq-credentials"]["fields"] if field["name"] == "DB_USER_PASSWORD"
    )

    assert template["version"] == "2.0"
    assert template["backingStore"] == "vault"
    assert "dbPassword" in template["vaultPolicies"]
    for secret in template["secrets"]:
        assert "targetNamespaces" not in secret
    assert "override" not in db_password
    assert db_password["onMissingValue"] == "generate"
    assert db_password["vaultPolicy"] == "dbPassword"
    assert aiq_fields == [
        "DB_USER_NAME",
        "DB_USER_PASSWORD",
        "TAVILY_API_KEY",
        "NVIDIA_API_KEY",
        "VLLM_API_KEY",
    ]
    assert hf_fields == ["hftoken"]
    assert secrets["huggingface-secret"]["fields"][0]["value"] is None


def test_pattern_values_target_umbrella_chart_and_serving_stack():
    values_global = yaml.safe_load(VALUES_GLOBAL.read_text(encoding="utf-8"))

    assert values_global["global"]["singleArgoCD"] is True
    assert values_global["global"]["secretLoader"]["disabled"] is False
    assert values_global["global"]["secretStore"]["backend"] == "vault"
    assert values_global["global"]["hardwareProfile"] == "nvfp4"
    assert values_global["global"]["model"]["hfRepo"] == HF_REPO
    assert values_global["global"]["model"]["servedName"] == SERVED_MODEL_NAME
    assert values_global["global"]["rhoai"]["version"] == "3.5.1"
    assert values_global["global"]["rhoai"]["vllmImage"].startswith(
        "registry.redhat.io/rhaii/vllm-cuda-rhel9@sha256:"
    )
    assert values_global["global"]["inference"]["namespace"] == "aiq-inference"
    assert values_global["global"]["storageClass"] == ""
    assert values_global["main"]["variant"] == "nvfp4"
    assert "clusterGroupName" not in values_global["main"]

    applications = values_global["clusterGroup"]["applications"]
    namespaces = values_global["clusterGroup"]["namespaces"]
    subscriptions = values_global["clusterGroup"]["subscriptions"]

    assert values_global["clusterGroup"]["isHubCluster"] is True
    assert "aiq" in namespaces
    assert "aiq-inference" in namespaces
    assert "vault" in namespaces
    assert "golang-external-secrets" in namespaces
    assert applications["vault"]["chart"] == "hashicorp-vault"
    assert applications["vault"]["annotations"]["argocd.argoproj.io/sync-wave"] == "-30"
    assert applications["golang-external-secrets"]["chart"] == "golang-external-secrets"
    assert applications["golang-external-secrets"]["annotations"]["argocd.argoproj.io/sync-wave"] == "-20"
    assert applications["eso-aiq-credentials"]["name"] == "aiq-credentials"
    assert applications["eso-aiq-credentials"]["namespace"] == "aiq"
    assert applications["eso-aiq-credentials"]["path"] == "charts/all/eso-bindings"
    assert applications["eso-aiq-credentials"]["annotations"]["argocd.argoproj.io/sync-wave"] == "5"
    assert (
        applications["eso-aiq-credentials"]["annotations"]["argocd.argoproj.io/sync-options"]
        == "SkipDryRunOnMissingResource=true"
    )
    assert applications["eso-huggingface-secret"]["name"] == "huggingface-secret"
    assert applications["eso-huggingface-secret"]["namespace"] == "aiq-inference"
    assert applications["eso-huggingface-secret"]["path"] == "charts/all/eso-bindings"
    assert applications["eso-huggingface-secret"]["annotations"]["argocd.argoproj.io/sync-wave"] == "5"
    assert (
        applications["eso-huggingface-secret"]["annotations"]["argocd.argoproj.io/sync-options"]
        == "SkipDryRunOnMissingResource=true"
    )
    assert subscriptions["rhoai"]["name"] == "rhods-operator"
    assert applications["openshift-ai"]["path"] == "charts/all/rhods"
    assert applications["nfd-config"]["annotations"]["argocd.argoproj.io/sync-wave"] == "10"
    assert applications["openshift-ai"]["annotations"]["argocd.argoproj.io/sync-wave"] == "15"
    assert applications["vllm-inference-service"]["namespace"] == "aiq-inference"
    assert applications["aiq"]["path"] == "charts/aiq2-web"
    assert applications["aiq"]["namespace"] == "aiq"
    assert applications["aiq-workflow-config"]["path"] == "charts/aiq-workflow-config"
    assert applications["vllm-inference-service"]["extraValueFiles"] == [PROFILE_VALUE_FILE]
    assert applications["aiq-workflow-config"]["extraValueFiles"] == [PROFILE_VALUE_FILE]
    assert "/overrides/values-openshift-base.yaml" in applications["aiq"]["extraValueFiles"]
    assert "/overrides/values-openshift-hybrid-lightning.yaml" in applications["aiq"]["extraValueFiles"]

    for variant_path, profile_name in (
        (VARIANT_NVFP4, "nvfp4"),
        (VARIANT_BF16, "bf16"),
        (VARIANT_BF16_TP4, "bf16-tp4"),
    ):
        variant = yaml.safe_load(variant_path.read_text(encoding="utf-8"))
        assert variant["clusterGroup"]["name"] == profile_name
        assert variant["global"]["hardwareProfile"] == profile_name
        assert (REPO_ROOT / "profiles" / f"{profile_name}.yaml").is_file()


def test_vllm_chart_uses_published_cache_and_retains_legacy_pvc(tmp_path: Path):
    manifests = _render_vllm_chart(tmp_path)
    pvc = next(m for m in manifests if m["kind"] == "PersistentVolumeClaim")
    assert pvc["spec"]["resources"]["requests"]["storage"] == "80Gi"
    assert "Prune=false" in pvc["metadata"]["annotations"]["argocd.argoproj.io/sync-options"]
    service = next(m for m in manifests if m["kind"] == "InferenceService")
    cache = next(m for m in manifests if m["kind"] == "LocalModelCache")
    runtime = next(m for m in manifests if m["kind"] == "ServingRuntime")
    assert service["spec"]["predictor"]["model"]["storageUri"] == cache["spec"]["sourceModelUri"]
    assert HF_REPO in cache["spec"]["sourceModelUri"]
    assert "initContainers" not in service["spec"]["predictor"]
    assert all("persistentVolumeClaim" not in v for v in runtime["spec"]["volumes"])
    container = runtime["spec"]["containers"][0]
    assert f"--served-model-name={SERVED_MODEL_NAME}" in container["args"]
    assert "--model=/mnt/models" in container["args"]
    assert all(e["name"] != "HF_TOKEN" for e in container["env"])
    assert next(e["value"] for e in container["env"] if e["name"] == "HF_HUB_OFFLINE") == "1"


def test_openshift_overlay_mounts_hybrid_config_and_disables_nginx_ingress():
    manifests = _render_openshift_overlay(OVERLAY_HYBRID_PATH)
    deployments = {
        manifest["metadata"]["name"]: manifest for manifest in manifests if manifest.get("kind") == "Deployment"
    }
    ingresses = [manifest for manifest in manifests if manifest.get("kind") == "Ingress"]
    routes = {manifest["metadata"]["name"]: manifest for manifest in manifests if manifest.get("kind") == "Route"}
    pvcs = {
        manifest["metadata"]["name"]: manifest
        for manifest in manifests
        if manifest.get("kind") == "PersistentVolumeClaim"
    }

    backend = deployments["aiq-backend"]["spec"]["template"]["spec"]
    env = {item["name"]: item.get("value") for item in backend["containers"][0]["env"]}
    volume_names = {volume["name"] for volume in backend["volumes"]}
    config_maps = {volume["configMap"]["name"] for volume in backend["volumes"] if volume.get("configMap") is not None}

    assert env["CONFIG_FILE"] == "/app/configs/config_hybrid_lightning.yml"
    assert {"secretRef": {"name": "aiq-credentials"}} in backend["containers"][0]["envFrom"]
    assert "NAT_JOB_STORE_DB_URL" in env
    assert volume_names == {"postgres-init", "workflow-config"}
    assert config_maps == {"aiq-postgres-init", "aiq-workflow-config"}
    assert ingresses == []
    assert list(routes) == ["aiq-frontend"]
    frontend_route = routes["aiq-frontend"]
    assert frontend_route["apiVersion"] == "route.openshift.io/v1"
    assert frontend_route["spec"]["to"] == {
        "kind": "Service",
        "name": "aiq-frontend",
        "weight": 100,
    }
    assert frontend_route["spec"]["port"]["targetPort"] == 3000
    assert "host" not in frontend_route["spec"]
    assert frontend_route["spec"]["tls"]["termination"] == "edge"
    assert frontend_route["spec"]["tls"]["insecureEdgeTerminationPolicy"] == "Redirect"
    assert "storageClassName" not in pvcs["aiq-postgres-data"]["spec"]
    assert deployments["aiq-backend"]["metadata"]["namespace"] == "aiq"


def test_eso_bindings_render_identity_external_secrets():
    for release_name, namespace in (
        ("aiq-credentials", "aiq"),
        ("huggingface-secret", "aiq-inference"),
    ):
        manifests = _render_helm_chart(ESO_BINDINGS_CHART, release_name, namespace)
        assert len(manifests) == 1
        secret = manifests[0]
        assert secret["apiVersion"] == "external-secrets.io/v1beta1"
        assert secret["kind"] == "ExternalSecret"
        assert secret["metadata"]["name"] == release_name
        assert secret["spec"]["refreshInterval"] == "15s"
        assert secret["spec"]["secretStoreRef"] == {
            "name": "vault-backend",
            "kind": "ClusterSecretStore",
        }
        assert secret["spec"]["target"] == {
            "name": release_name,
            "creationPolicy": "Owner",
            "deletionPolicy": "Retain",
        }
        assert secret["spec"]["dataFrom"] == [
            {"extract": {"key": f"secret/data/hub/{release_name}"}}
        ]


def test_nvfp4_profile_args_match_chart_defaults():
    chart_values = yaml.safe_load(VLLM_CHART_VALUES.read_text(encoding="utf-8"))
    profile = yaml.safe_load(PROFILE_NVFP4.read_text(encoding="utf-8"))
    assert profile["vllmServingRuntime"]["args"] == chart_values["vllmServingRuntime"]["args"]
    assert profile["global"]["model"]["hfRepo"] == HF_REPO
    assert profile["global"]["model"]["servedName"] == SERVED_MODEL_NAME
    assert profile["workflowProfile"]["lightningAgent"]["maxTokens"] == 1536
    assert profile["workflowProfile"]["lightningAgent"]["thinkingTokenBudget"] == 512


def test_bf16_profile_renders_catalog_limits():
    manifests = _render_helm_chart(
        VLLM_CHART,
        "vllm-inference-service",
        "aiq-inference",
        str(VALUES_GLOBAL),
        str(PROFILE_BF16),
    )
    pvc = next(manifest for manifest in manifests if manifest["kind"] == "PersistentVolumeClaim")
    assert pvc["spec"]["resources"]["requests"]["storage"] == "150Gi"

    serving_runtime = next(manifest for manifest in manifests if manifest["kind"] == "ServingRuntime")
    container = serving_runtime["spec"]["containers"][0]
    args = container["args"]
    env = {item["name"]: item.get("value") for item in container["env"]}
    joined_args = " ".join(args)

    assert BF16_HF_REPO in next(m for m in manifests if m["kind"] == "LocalModelCache")["spec"]["sourceModelUri"]
    assert f"--served-model-name={BF16_SERVED_MODEL_NAME}" in args
    assert "--max-model-len=65536" in args
    assert "--max-num-batched-tokens=32768" in args
    assert "--enforce-eager" not in args
    assert "--moe-backend=marlin" not in joined_args
    assert "--kv-cache-dtype=fp8" not in args
    assert "--quantization" not in joined_args

    config = _workflow_config(str(PROFILE_BF16))
    agent = config["llms"]["nemotron_lightning_agent_llm"]
    assert agent["model_name"] == BF16_SERVED_MODEL_NAME
    assert agent["max_tokens"] == 32768
    assert "thinking_token_budget" not in agent["extra_body"]
    assert agent["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True
    assert config["llms"]["nemotron_lightning_intent_llm"]["model_name"] == BF16_SERVED_MODEL_NAME
    assert config["llms"]["nemotron_lightning_intent_llm"]["max_tokens"] == 1024
    assert config["llms"]["nemotron_ultra_llm"]["max_tokens"] == 16384


def test_bf16_tp4_profile_splits_across_four_gpus():
    manifests = _render_helm_chart(
        VLLM_CHART,
        "vllm-inference-service",
        "aiq-inference",
        str(VALUES_GLOBAL),
        str(PROFILE_BF16_TP4),
    )
    pvc = next(manifest for manifest in manifests if manifest["kind"] == "PersistentVolumeClaim")
    assert pvc["spec"]["resources"]["requests"]["storage"] == "150Gi"

    inference_service = next(manifest for manifest in manifests if manifest["kind"] == "InferenceService")
    limits = inference_service["spec"]["predictor"]["model"]["resources"]["limits"]
    assert limits["nvidia.com/gpu"] == "4"

    serving_runtime = next(manifest for manifest in manifests if manifest["kind"] == "ServingRuntime")
    container = serving_runtime["spec"]["containers"][0]
    args = container["args"]
    env = {item["name"]: item.get("value") for item in container["env"]}
    joined_args = " ".join(args)

    assert BF16_HF_REPO in next(m for m in manifests if m["kind"] == "LocalModelCache")["spec"]["sourceModelUri"]
    assert f"--served-model-name={BF16_SERVED_MODEL_NAME}" in args
    assert "--tensor-parallel-size=4" in args
    assert "--max-model-len=65536" in args
    assert "--max-num-batched-tokens=32768" in args
    assert "--quantization" not in joined_args

    config = _workflow_config(str(PROFILE_BF16_TP4))
    agent = config["llms"]["nemotron_lightning_agent_llm"]
    assert agent["model_name"] == BF16_SERVED_MODEL_NAME
    assert agent["max_tokens"] == 32768
    assert "thinking_token_budget" not in agent["extra_body"]


def test_nvfp4_profile_workflow_keeps_small_token_budget():
    config = _workflow_config(str(PROFILE_NVFP4))
    agent = config["llms"]["nemotron_lightning_agent_llm"]
    assert agent["model_name"] == SERVED_MODEL_NAME
    assert agent["max_tokens"] == 1536
    assert agent["extra_body"]["thinking_token_budget"] == 512


def test_nvidia_jwt_external_secrets_stay_disabled():
    nvidia_values = yaml.safe_load(NVIDIA_AIQ_VALUES.read_text(encoding="utf-8"))
    assert nvidia_values["externalSecrets"]["enabled"] is False


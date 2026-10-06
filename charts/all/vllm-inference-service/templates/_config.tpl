{{- define "aiq.validate" -}}
{{- $s := .Values.global.serving -}}
{{- range $key := list "replicas" "nodesPerReplica" "gpusPerNode" "tensorParallel" "pipelineParallel" -}}
{{- if not (regexMatch "^[1-9][0-9]*$" (toString (index $s $key))) -}}{{ fail (printf "global.serving.%s must be a positive integer" $key) }}{{- end -}}
{{- end -}}
{{- if ne (mul $s.nodesPerReplica $s.gpusPerNode) (mul $s.tensorParallel $s.pipelineParallel) -}}{{ fail "nodesPerReplica * gpusPerNode must equal tensorParallel * pipelineParallel" }}{{- end -}}
{{- if and (gt (int $s.nodesPerReplica) 1) (or (ne (int $s.pipelineParallel) (int $s.nodesPerReplica)) (ne (int $s.tensorParallel) (int $s.gpusPerNode))) -}}
{{- fail "Distributed startup requires pipelineParallel=nodesPerReplica and tensorParallel=gpusPerNode" -}}
{{- end -}}
{{- with $s.nodeNames -}}
{{- if lt (len (uniq .)) (int (mul $s.replicas $s.nodesPerReplica)) -}}{{ fail "serving.nodeNames must include enough distinct nodes for replicas * nodesPerReplica" }}{{- end -}}
{{- range . -}}
{{- if not (regexMatch "^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$" .) -}}{{ fail "serving.nodeNames must contain Kubernetes node names" }}{{- end -}}
{{- end -}}
{{- end -}}
{{- if not (regexMatch "^[0-9a-f]{40}$" .Values.global.model.revision) -}}{{ fail "Pin global.model.revision to a full immutable HF commit SHA" }}{{- end -}}
{{- if not .Values.global.modelCache.nodeSelector -}}{{ fail "Select cache nodes using global.modelCache.nodeSelector" }}{{- end -}}
{{- if not (hasPrefix "https://" .Values.global.modelStore.endpoint) -}}{{ fail "Model storage must use HTTPS" }}{{- end -}}
{{- if not (regexMatch "@sha256:[0-9a-f]{64}$" .Values.global.modelTools.image) -}}{{ fail "Pin global.modelTools.image by digest" }}{{- end -}}
{{- if not (regexMatch "@sha256:[0-9a-f]{64}$" (include "aiq.runtimeImage" .)) -}}{{ fail "Pin runtimeImage by digest" }}{{- end -}}
{{- range $arg := .Values.vllmServingRuntime.args -}}
{{- $normalized := replace "_" "-" $arg -}}
{{- if or (regexMatch "^--(config|model($|=)|served-model-name|port|host|tensor-parallel-size|pipeline-parallel-size|distributed-executor-backend|nnodes|node-rank|master-addr|master-port|headless|data-parallel|enable-expert-parallel|kv-transfer-config)" $normalized) (regexMatch "^-(tp|pp|dp|dpl|dpr|n|r|c)($|=|[0-9])" $normalized) -}}
{{- fail (printf "Argument %s is managed by global.serving or unsupported" $arg) -}}
{{- end -}}
{{- end -}}
{{- $uris := dict -}}
{{- range $model := prepend .Values.global.modelCache.retainedRevisions .Values.global.model -}}
{{- if not (regexMatch "^[0-9a-f]{40}$" $model.revision) -}}{{ fail "Pin every retained revision to a full immutable HF commit SHA" }}{{- end -}}
{{- $key := printf "%s/%s" $model.hfRepo $model.revision -}}
{{- if hasKey $uris $key -}}{{ fail "Current and retained model revisions must be distinct" }}{{- end -}}
{{- $_ := set $uris $key true -}}
{{- end -}}
{{- range $key, $value := $s.rdmaResources -}}
{{- if or (not (contains "/" $key)) (eq $key "nvidia.com/gpu") (not (regexMatch "^[1-9][0-9]*$" (toString $value))) -}}{{ fail "rdmaResources must contain positive extended device requests, excluding nvidia.com/gpu" }}{{- end -}}
{{- end -}}
{{- range $key, $value := $s.ncclEnv -}}
{{- if not (regexMatch "^(NCCL_|GLOO_|UCX_|FI_)" $key) -}}{{ fail "ncclEnv accepts NCCL_, GLOO_, UCX_, or FI_ variables only" }}{{- end -}}
{{- end -}}
{{- if or (lt (int .Values.global.modelTools.transferConcurrency) 1) (gt (int .Values.global.modelTools.transferConcurrency) 32) -}}{{ fail "transferConcurrency must be between 1 and 32" }}{{- end -}}
{{- if lt (int $s.startupSeconds) 10 -}}{{ fail "startupSeconds must be at least 10" }}{{- end -}}
{{- range $key, $value := $s.nodeSelector -}}
{{- if and (hasKey $.Values.global.modelCache.nodeSelector $key) (ne (index $.Values.global.modelCache.nodeSelector $key) $value) -}}{{ fail "Serving/cache node selectors conflict" }}{{- end -}}
{{- end -}}
{{- end -}}
{{- define "aiq.scriptsName" -}}
aiq-model-scripts-{{ printf "%s%s%s%s" (.Values.global | toJson) (.Files.Get "files/artifact_store.py") (.Files.Get "files/gate.py") (.Files.Get "files/metrics.py") | sha256sum | trunc 10 }}
{{- end -}}
{{- define "aiq.runtimeImage" -}}{{ default .Values.global.rhoai.vllmImage .Values.global.serving.runtimeImage }}{{- end -}}
{{- define "aiq.prefix" -}}{{ printf "%s/%s/%s" .Values.global.modelStore.prefix .Values.global.model.hfRepo .Values.global.model.revision }}{{- end -}}
{{- define "aiq.uri" -}}{{ printf "s3://%s/%s" .Values.global.modelStore.bucket (include "aiq.prefix" .) }}{{- end -}}
{{- define "aiq.cacheName" -}}{{ printf "aiq-%s" (include "aiq.uri" . | sha256sum | trunc 20) }}{{- end -}}
{{- define "aiq.resources" -}}
{{- range $type := list "requests" "limits" }}
{{ $type }}:
  cpu: {{ $.Values.global.serving.cpu | quote }}
  memory: {{ $.Values.global.serving.memory | quote }}
  nvidia.com/gpu: {{ $.Values.global.serving.gpusPerNode | quote }}
  {{- with $.Values.global.serving.rdmaResources }}
  {{- toYaml . | nindent 2 }}
  {{- end }}
{{- end }}
{{- end -}}
{{- define "aiq.placement" -}}
nodeSelector:
  {{- toYaml (mergeOverwrite (deepCopy .Values.global.modelCache.nodeSelector) .Values.global.serving.nodeSelector) | nindent 2 }}
tolerations:
  {{- toYaml .Values.global.serving.tolerations | nindent 2 }}
affinity:
  {{- with .Values.global.serving.nodeNames }}
  nodeAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      nodeSelectorTerms:
        {{- range . }}
        - matchFields:
            - key: metadata.name
              operator: In
              values:
                - {{ . | quote }}
        {{- end }}
  {{- end }}
  podAntiAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      - topologyKey: {{ .Values.global.serving.topologyKey }}
        labelSelector:
          matchLabels:
            aiq.rhai.redhat.com/serving: {{ include "vllm-inference-service.fullname" . }}
{{- end -}}
{{- define "aiq.args" -}}
- --model=/mnt/models
- {{ printf "--served-model-name=%s" .Values.global.model.servedName | quote }}
- --port=8080
- --distributed-executor-backend=mp
- {{ printf "--tensor-parallel-size=%d" (int .Values.global.serving.tensorParallel) }}
- {{ printf "--pipeline-parallel-size=%d" (int .Values.global.serving.pipelineParallel) }}
{{ toYaml .Values.vllmServingRuntime.args }}
{{- end -}}
{{- define "aiq.runtimeEnv" -}}
- {name: HF_HUB_OFFLINE, value: "1"}
- {name: HF_HOME, value: /tmp/huggingface}
- {name: HOME, value: /tmp}
- name: VLLM_HOST_IP
  valueFrom:
    fieldRef:
      fieldPath: status.podIP
{{- range $key, $value := .Values.global.serving.ncclEnv }}
- name: {{ $key }}
  value: {{ $value | quote }}
{{- end }}
{{- end -}}
{{- define "aiq.s3Env" -}}
- name: S3_ENDPOINT
  value: {{ .Values.global.modelStore.endpoint | quote }}
- name: AWS_DEFAULT_REGION
  value: {{ .Values.global.modelStore.region | quote }}
- name: AWS_CA_BUNDLE
  value: /etc/aiq-ca/ca.crt
- name: TRANSFER_CONCURRENCY
  value: {{ .Values.global.modelTools.transferConcurrency | quote }}
- name: DISK_RESERVE_BYTES
  value: {{ .Values.global.modelTools.diskReserveBytes | int64 | quote }}
{{- end -}}
{{- define "aiq.probes" -}}
startupProbe:
  httpGet: {path: /health, port: 8080}
  periodSeconds: 10
  failureThreshold: {{ div (int .Values.global.serving.startupSeconds) 10 }}
readinessProbe:
  httpGet: {path: /health, port: 8080}
  periodSeconds: 5
  timeoutSeconds: 3
livenessProbe:
  httpGet: {path: /health, port: 8080}
  periodSeconds: 15
  timeoutSeconds: 5
  failureThreshold: 5
{{- end -}}

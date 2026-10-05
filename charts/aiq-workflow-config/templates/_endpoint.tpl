{{- define "aiq.localEndpoint" -}}
http://vllm-inference-service-{{ if gt (int .Values.global.serving.nodesPerReplica) 1 }}distributed{{ else }}predictor{{ end }}.{{ .Values.global.inference.namespace }}.svc.cluster.local/v1
{{- end -}}

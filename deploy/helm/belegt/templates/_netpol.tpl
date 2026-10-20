{{/* NetworkPolicy 片段（Spec: 07 §2.2）。peer：include "belegt.peer" (list . "api" "app") */}}
{{- define "belegt.peer" -}}
{{- $root := index . 0 -}}
- namespaceSelector:
    matchLabels: { kubernetes.io/metadata.name: {{ include "belegt.ns" (list $root (index . 2)) }} }
  podSelector:
    matchLabels: { app.kubernetes.io/instance: {{ $root.Release.Name | quote }}, app.kubernetes.io/component: {{ index . 1 }} }
{{- end -}}
{{- define "belegt.pgPeer" -}}
{{- if .Values.postgresql.bundled.enabled -}}
{{ include "belegt.peer" (list . "postgresql" "data") }}
{{- else -}}
{{ toYaml .Values.networkPolicy.peers.postgres }}
{{- end -}}
{{- end -}}
{{- define "belegt.commonEgress" -}}
{{- $root := . -}}
- to:
    {{- include "belegt.pgPeer" $root | nindent 4 }}
  ports: [{ port: {{ $root.Values.postgresql.port }}, protocol: TCP }]
- to:
    {{- toYaml $root.Values.networkPolicy.peers.temporal | nindent 4 }}
  ports: [{ port: 7233, protocol: TCP }]
- to:
    {{- toYaml $root.Values.networkPolicy.peers.otelCollector | nindent 4 }}
  ports: [{ port: 4317, protocol: TCP }, { port: 4318, protocol: TCP }]
{{- end -}}
{{- define "belegt.egressGwEgress" -}}
{{- if .Values.egressGateway.enabled }}
- to:
    {{- include "belegt.peer" (list . "egress-gateway" "guard") | nindent 4 }}
  ports: [{ port: {{ .Values.egressGateway.port }}, protocol: TCP }]
{{- end }}
{{- end -}}
{{- define "belegt.vllmEgress" -}}
{{- if .Values.llm.vllm.enabled }}
- to:
    {{- include "belegt.peer" (list . "vllm" "trusted") | nindent 4 }}
  ports: [{ port: 8000, protocol: TCP }]
{{- end }}
{{- end -}}

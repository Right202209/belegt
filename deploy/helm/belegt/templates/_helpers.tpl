{{/* Spec: 10 §6.1–§6.2；07 §2.2（非 root、只读根文件系统、信任分区标签） */}}

{{- define "belegt.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 50 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 50 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/* 分区命名空间：zones.<zone>.namespace，空 = Release 命名空间。用法：include "belegt.ns" (list . "app") */}}
{{- define "belegt.ns" -}}
{{- $root := index . 0 -}}{{- $zone := index . 1 -}}
{{- $z := index $root.Values.zones $zone | default dict -}}
{{- default $root.Release.Namespace $z.namespace -}}
{{- end -}}

{{/* belegt.labels 不含 name/instance（与 belegt.selector 合用时避免重复键）；单独使用时用 belegt.metaLabels */}}
{{- define "belegt.metaLabels" -}}
app.kubernetes.io/name: belegt
app.kubernetes.io/instance: {{ .Release.Name | quote }}
{{ include "belegt.labels" . }}
{{- end -}}

{{- define "belegt.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
belegt.io/deploy-profile: {{ .Values.global.deployProfile | quote }}
{{- end -}}

{{/* 组件选择器标签：include "belegt.selector" (list . "api" "app") */}}
{{- define "belegt.selector" -}}
{{- $root := index . 0 -}}
app.kubernetes.io/name: belegt
app.kubernetes.io/instance: {{ $root.Release.Name | quote }}
app.kubernetes.io/component: {{ index . 1 }}
belegt.io/zone: {{ index . 2 }}
{{- end -}}

{{- define "belegt.image" -}}
{{- $root := index . 0 -}}{{- $name := index . 1 -}}
{{- printf "%s/%s:%s" $root.Values.global.imageRegistry $name (default $root.Chart.AppVersion $root.Values.image.tag) -}}
{{- end -}}

{{- define "belegt.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: {{ .Values.podSecurity.runAsUser }}
runAsGroup: {{ .Values.podSecurity.runAsGroup }}
fsGroup: {{ .Values.podSecurity.fsGroup }}
seccompProfile:
  type: {{ .Values.podSecurity.seccompProfile }}
{{- end -}}

{{- define "belegt.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
privileged: false
capabilities:
  drop: ["ALL"]
{{- end -}}

{{/* 调度：欧盟驻留节点区域约束 + 分区节点池。include "belegt.scheduling" (list . "quarantine") */}}
{{- define "belegt.scheduling" -}}
{{- $root := index . 0 -}}{{- $zone := index . 1 -}}
{{- $ns := index $root.Values.zones.nodeSelectors $zone | default dict -}}
{{- $tol := index $root.Values.zones.tolerations $zone | default list -}}
{{- if $ns }}
nodeSelector:
  {{- toYaml $ns | nindent 2 }}
{{- end }}
{{- if $tol }}
tolerations:
  {{- toYaml $tol | nindent 2 }}
{{- end }}
{{- if and $root.Values.residency.enforce $root.Values.residency.allowedRegions }}
affinity:
  nodeAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      nodeSelectorTerms:
        - matchExpressions:
            - key: topology.kubernetes.io/region
              operator: In
              values:
                {{- toYaml $root.Values.residency.allowedRegions | nindent 16 }}
{{- end }}
{{- end -}}

{{- define "belegt.pgHost" -}}
{{- if .Values.postgresql.bundled.enabled -}}
{{- printf "%s-postgresql.%s.svc" (include "belegt.fullname" .) (include "belegt.ns" (list . "data")) -}}
{{- else -}}
{{- required "postgresql.host is required when postgresql.bundled.enabled=false" .Values.postgresql.host -}}
{{- end -}}
{{- end -}}

{{- define "belegt.pgSslmode" -}}
{{- if .Values.postgresql.bundled.enabled -}}disable{{- else -}}{{ .Values.postgresql.sslmode }}{{- end -}}
{{- end -}}

{{- define "belegt.secretName" -}}
{{- if .Values.secrets.create -}}{{ include "belegt.fullname" . }}-secrets{{- else -}}{{ .Values.secrets.existingSecret }}{{- end -}}
{{- end -}}

{{- define "belegt.quarantineSecretName" -}}
{{- if .Values.secrets.create -}}{{ include "belegt.fullname" . }}-quarantine-secrets{{- else -}}{{ .Values.secrets.existingQuarantineSecret }}{{- end -}}
{{- end -}}

{{- define "belegt.egressProxy" -}}
{{- printf "http://%s-egress-gateway.%s.svc:%v" (include "belegt.fullname" .) (include "belegt.ns" (list . "guard")) .Values.egressGateway.port -}}
{{- end -}}

{{/* 公共环境变量（OTel 资源属性，10 §5.1） */}}
{{- define "belegt.commonEnv" -}}
- name: BELEGT_ENV
  value: {{ .Values.global.environment | quote }}
- name: BELEGT_DEPLOY_PROFILE
  value: {{ .Values.global.deployProfile | quote }}
- name: BELEGT_DATA_RESIDENCY
  value: {{ .Values.global.dataResidency | quote }}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ .Values.observability.otlpEndpoint | quote }}
- name: OTEL_RESOURCE_ATTRIBUTES
  value: {{ printf "deployment.environment=%s,belegt.deploy_profile=%s" .Values.global.environment .Values.global.deployProfile | quote }}
{{- end -}}

{{/* 可信区数据库与对象存储环境变量（API、可信区 worker） */}}
{{- define "belegt.trustedEnv" -}}
- name: BELEGT_DB_PASSWORD
  valueFrom: { secretKeyRef: { name: {{ include "belegt.secretName" . }}, key: app-db-password } }
- name: BELEGT_DB_URL
  value: {{ printf "postgresql+psycopg://%s:$(BELEGT_DB_PASSWORD)@%s:%v/%s?sslmode=%s" .Values.postgresql.appUser (include "belegt.pgHost" .) .Values.postgresql.port .Values.postgresql.database (include "belegt.pgSslmode" .) | quote }}
- name: BELEGT_PG_COMMIT
  value: {{ ternary "on" "off" .Values.postgresql.pgCommit | quote }}
- name: BELEGT_PG_WRITER_ROLE
  value: belegt_writer
- name: BELEGT_S3_ENDPOINT
  value: {{ .Values.objectStorage.endpoint | quote }}
- name: BELEGT_S3_BUCKET
  value: {{ .Values.objectStorage.bucket | quote }}
- name: BELEGT_S3_REGION
  value: {{ .Values.objectStorage.region | quote }}
- name: AWS_ACCESS_KEY_ID
  valueFrom: { secretKeyRef: { name: {{ include "belegt.secretName" . }}, key: s3-access-key-id, optional: true } }
- name: AWS_SECRET_ACCESS_KEY
  valueFrom: { secretKeyRef: { name: {{ include "belegt.secretName" . }}, key: s3-secret-access-key, optional: true } }
{{- end -}}

{{- define "belegt.proxyEnv" -}}
{{- if .Values.egressGateway.enabled }}
- name: HTTPS_PROXY
  value: {{ include "belegt.egressProxy" . | quote }}
- name: HTTP_PROXY
  value: {{ include "belegt.egressProxy" . | quote }}
- name: NO_PROXY
  value: ".svc,.cluster.local,localhost,127.0.0.1,10.0.0.0/8"
{{- end }}
{{- end -}}

{{- define "belegt.tmpVolume" -}}
- name: tmp
  emptyDir: { medium: Memory, sizeLimit: 256Mi }
{{- end -}}

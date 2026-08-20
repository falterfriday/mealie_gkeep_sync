{{/*
Chart name, overridable.
*/}}
{{- define "mealie-gkeep-sync.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name.
*/}}
{{- define "mealie-gkeep-sync.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "mealie-gkeep-sync.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "mealie-gkeep-sync.labels" -}}
helm.sh/chart: {{ include "mealie-gkeep-sync.chart" . }}
{{ include "mealie-gkeep-sync.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "mealie-gkeep-sync.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mealie-gkeep-sync.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Secret holding MEALIE_API_TOKEN and GOOGLE_MASTER_TOKEN: either one the user
manages out of band, or the one this chart renders.
*/}}
{{- define "mealie-gkeep-sync.secretName" -}}
{{- if .Values.secrets.existingSecret }}
{{- .Values.secrets.existingSecret }}
{{- else }}
{{- include "mealie-gkeep-sync.fullname" . }}
{{- end }}
{{- end }}

{{/*
PVC backing /data.
*/}}
{{- define "mealie-gkeep-sync.pvcName" -}}
{{- if .Values.persistence.existingClaim }}
{{- .Values.persistence.existingClaim }}
{{- else }}
{{- printf "%s-state" (include "mealie-gkeep-sync.fullname" .) }}
{{- end }}
{{- end }}

{{/*
Fail fast on configurations that would deploy but never work, or that would
quietly corrupt state. A clear error at install time beats a CrashLoopBackOff
or, worse, two syncers racing on one list.
*/}}
{{- define "mealie-gkeep-sync.validate" -}}
{{- if gt (int .Values.replicaCount) 1 }}
{{- fail "replicaCount must be 0 or 1: concurrent syncers race on the same list pair and the same ReadWriteOnce state volume. Use 0 to pause syncing." }}
{{- end }}
{{- if not .Values.mealie.baseUrl }}
{{- fail "mealie.baseUrl is required (the root URL of your Mealie instance)." }}
{{- end }}
{{- if and (not .Values.mealie.listId) (not .Values.mealie.listName) }}
{{- fail "Set either mealie.listId or mealie.listName to identify the shopping list to sync." }}
{{- end }}
{{- if not .Values.google.email }}
{{- fail "google.email is required (the account that owns the Keep list)." }}
{{- end }}
{{- if not .Values.google.keepListName }}
{{- fail "google.keepListName is required (the title of the Keep list to sync)." }}
{{- end }}
{{- if not .Values.secrets.existingSecret }}
{{- if not .Values.secrets.mealieApiToken }}
{{- fail "Provide secrets.mealieApiToken, or point secrets.existingSecret at a Secret containing MEALIE_API_TOKEN and GOOGLE_MASTER_TOKEN." }}
{{- end }}
{{- if not .Values.secrets.googleMasterToken }}
{{- fail "Provide secrets.googleMasterToken, or point secrets.existingSecret at a Secret containing MEALIE_API_TOKEN and GOOGLE_MASTER_TOKEN." }}
{{- end }}
{{- end }}
{{- if not (has .Values.sync.conflictStrategy (list "newest" "mealie" "keep")) }}
{{- fail "sync.conflictStrategy must be one of: newest, mealie, keep." }}
{{- end }}
{{- if not (has .Values.logging.format (list "json" "text")) }}
{{- fail "logging.format must be either json or text." }}
{{- end }}
{{- end }}

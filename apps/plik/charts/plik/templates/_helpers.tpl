{{/*
Expand the name of the chart.
*/}}
{{- define "plik.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "plik.fullname" -}}
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

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "plik.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "plik.labels" -}}
helm.sh/chart: {{ include "plik.chart" . }}
{{ include "plik.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "plik.selectorLabels" -}}
app.kubernetes.io/name: {{ include "plik.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "plik.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "plik.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Nome del Secret con le credenziali (PLIKD_DATA_BACKEND_CONFIG, PLIKD_METADATA_BACKEND_CONFIG).
Il chart non crea Secret: lo crea External Secrets. Se il nome manca, il render fallisce.
*/}}
{{- define "plik.secretName" -}}
{{- required "secrets.existingSecret è obbligatorio: il Secret con le credenziali lo crea External Secrets" .Values.secrets.existingSecret -}}
{{- end }}

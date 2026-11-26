# Chart Helm di Plik (task 1.9)

Il chart in `apps/plik/charts/plik` fa girare Plik su OpenShift: stateless, con i file su Garage (S3) e i metadati su PostgreSQL. La configurazione non segreta sta in una ConfigMap; le credenziali arrivano solo dal Secret `plik-backends` (External Secrets, task 1.4).

## Da dove viene

È il chart ufficiale di Plik, copiato nel repo e poi modificato:

```
helm repo add plik https://root-gg.github.io/plik
helm pull plik/plik --version 1.4.3 --untar --untardir apps/plik/charts
```

- `appVersion: 1.4.3`: la versione di Plik.
- `version: 1.4.3-ocp.1`: la nostra versione del chart (upstream 1.4.3, modifica OpenShift n. 1). Va incrementata a ogni modifica del chart.

Copiato e non scaricato a ogni build perché va modificato, perché tutto deve stare nel repo e perché Argo CD non deve dipendere da internet.

## Cosa abbiamo cambiato

| File | Modifica | Perché |
|---|---|---|
| `secret.yaml`, `pvc.yaml`, `ingress.yaml`, `CHANGELOG.md` | rimossi | Nessuna credenziale nei values; Plik è stateless (niente dischi); su OpenShift si usa la Route |
| `deployment.yaml` | solo `Deployment`, niente volumi `data`/`db` | Niente StatefulSet né dischi: i file sono su Garage |
| | `envFrom` da `plik-backends` **non** opzionale | Senza credenziali il pod non deve partire con una configurazione sbagliata |
| | `emptyDir` su `/tmp`, limite `tmpSizeLimit` (256Mi) | Il root filesystem è in sola lettura: `/tmp` è l'unica parte scrivibile |
| | annotazione `checksum/config` | Un cambio della ConfigMap fa ripartire i pod (Plik legge `plikd.cfg` solo all'avvio) |
| | `automountServiceAccountToken: false` | Plik non usa l'API Kubernetes |
| | probe dai values, porta `metrics` se `MetricsPort` > 0 | Vedi sotto |
| `_helpers.tpl` | `plik.secretName` con `required` | Se `secrets.existingSecret` è vuoto, il render si ferma |
| `NOTES.txt` | riscritto | L'originale usava `.Values.ingress`, che non esiste più |
| `route.yaml` | nuovo | Route TLS `edge` con redirect da HTTP, timeout del router a 10 minuti per gli upload grandi |
| `hpa.yaml`, `pdb.yaml`, `networkpolicy.yaml`, `servicemonitor.yaml` | nuovi, spenti di default | Attivabili dai values per ambiente |
| `service.yaml` | porta `metrics` se `MetricsPort` > 0 | Per il ServiceMonitor |

## Configurazione: ConfigMap e Secret

Plik legge `plikd.cfg` (ConfigMap) e le variabili `PLIKD_*`. Le mappe in JSON nelle variabili vengono **fuse** con quelle del file, quindi:

| Parametro | Dove |
|---|---|
| `DataBackend = "s3"`, `Endpoint = "garage:3900"`, `Bucket = "plik"`, `Location = "garage"`, `UseSSL = false`, `BucketLookup = "path"` | ConfigMap |
| `AccessKeyID`, `SecretAccessKey` | Secret, in `PLIKD_DATA_BACKEND_CONFIG` |
| `Driver = "postgres"`, `MaxOpenConns = 10`, `MaxIdleConns = 5` | ConfigMap |
| `ConnectionString` (contiene la password) | Secret, in `PLIKD_METADATA_BACKEND_CONFIG` |

Altri valori scelti: `AssumeHTTPS = true` (il TLS termina sulla Route), `SourceIpHeader = "X-Forwarded-For"` (IP del client dietro il router), `FeatureStream = "disabled"` (lo stream mode non funziona con più repliche).

## Sicurezza (SCC restricted-v2)

- `runAsNonRoot`, `seccompProfile: RuntimeDefault`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `readOnlyRootFilesystem: true`.
- **Nessun `runAsUser`/`fsGroup`**: l'UID lo assegna OpenShift dal range del namespace.
- ServiceAccount dedicato `plik`, senza token montato.

## Probe

| Probe | Endpoint | Se fallisce |
|---|---|---|
| startup | `/health`, fino a 2 minuti | pod riavviato; protegge la migrazione dello schema al primo avvio |
| readiness | `/health` | il pod esce da Service e Route, senza riavvio |
| liveness | `/version` | pod riavviato; non dipende dal database, così un problema di Postgres non riavvia tutti i pod |

## Funzioni attivabili dai values

| Values | Cosa crea | Note |
|---|---|---|
| `autoscaling.enabled` | HPA sulla CPU | Richiede `resources.requests.cpu`, altrimenti il render si ferma. Con l'HPA il Deployment non dichiara `replicas` |
| `podDisruptionBudget.enabled` | PDB con `minAvailable` o `maxUnavailable` | `minAvailable` deve essere minore delle repliche, o il drain del nodo si blocca |
| `networkPolicy.enabled` | NetworkPolicy in **uscita**: solo DNS, Pooler (5432), Garage (3900) | L'ingresso lo gestiscono le policy di namespace (task 1.5) |
| `serviceMonitor.enabled` | ServiceMonitor su `/metrics` | Richiede `plikd.MetricsPort` > 0; aprire quella porta in `allow-from-monitoring` (task 1.5) |
| `route.enabled` | Route | `route.host` vuoto = lo genera OpenShift |

## Values per ambiente

| | dev (`apps/plik/dev/values-plik.yaml`) | prod (`apps/plik/prod/values-plik.yaml`) |
|---|---|---|
| Repliche | 1 | HPA 3–6 al 70% CPU |
| PDB | no | `minAvailable: 2` |
| requests / limits | 50m 128Mi / 500m 512Mi | 200m 256Mi / 1 CPU 512Mi |
| NetworkPolicy in uscita | sì | sì |
| File massimo, TTL | 1GB, 1d (max 7d) | 10GB, 7d (max 30d) |

Le risorse stanno dentro ResourceQuota e LimitRange del namespace. Ogni upload usa fino a `PartUploadConcurrency × PartSize` = 64Mi di memoria.

## Collegamento a Kustomize e Argo CD

Negli overlay `apps/plik/dev` e `apps/plik/prod`:

```yaml
helmGlobals:
  chartHome: ../charts
helmCharts:
  - name: plik
    releaseName: plik
    namespace: plik-dev
    valuesFile: values-plik.yaml
```

- Kustomize renderizza il chart (`--enable-helm`, attivo in Argo CD) e mette il namespace.
- Una patch con `target: labelSelector: app.kubernetes.io/instance=plik` dà a tutte le risorse del chart `argocd.argoproj.io/sync-wave: "2"`: Plik parte dopo Postgres, Garage e il job di bootstrap (wave 0).
- L'immagine **non** si cambia nei values: si cambia nell'overlay con `images:` e il `digest:`, che è ciò che la pipeline scrive in dev e la promozione copia in prod.

## Contratto con gli altri componenti

| Cosa | Valore nel chart | Lo fornisce |
|---|---|---|
| Secret delle credenziali | `plik-backends` | External Secrets (task 1.4) |
| Database | `plik-db-pooler-rw:5432`, database `plik` | Pooler di CloudNativePG (task 1.6) |
| S3 | `garage:3900`, regione `garage`, bucket `plik` | Garage e job di bootstrap (task 1.7) |
| Label dei pod | `app.kubernetes.io/name: plik` | usata dalle NetworkPolicy del task 1.5 |

## Verifiche

```
helm lint apps/plik/charts/plik -f apps/plik/dev/values-plik.yaml
helm lint apps/plik/charts/plik -f apps/plik/prod/values-plik.yaml
helm template plik apps/plik/charts/plik --show-only templates/configmap.yaml | grep -E "^\s*(Password|SecretAccessKey|AccessKeyID|ConnectionString)\s*="
kustomize build --enable-helm apps/plik/dev | grep -E "^kind:"
```

Il terzo comando non deve stampare niente (nessun segreto nella ConfigMap).

Sul cluster:

```
HOST=$(oc get route plik -n plik-dev -o jsonpath='{.spec.host}')
curl -s -o /dev/null -w "%{http_code} %{redirect_url}\n" http://$HOST
curl -s --form "file=@prova.txt" https://$HOST
```

Il primo deve dare `302` verso `https://`; il secondo restituisce l'URL del file, da scaricare e confrontare con `shasum`.
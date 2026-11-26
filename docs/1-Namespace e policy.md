# Plik — namespace, quote e policy

Questo README descrive la **base dei namespace**: cosa la compone, come Kustomize e Argo CD la costruiscono e come verificarla.
È il primo pezzo dello stack: tutti gli altri componenti vengono deployati sopra.

## Struttura

```
apps/plik/
├── app-dev.yaml                  Application plik-dev  (branch develop, sync automatica)
├── app-prod.yaml                 Application plik-prod (branch main, sync manuale)
├── base/
│   ├── kustomization.yaml
│   ├── policies-quota.yaml       ResourceQuota (valori di dev)
│   ├── policies-limitrange.yaml  LimitRange (uguale per dev e prod)
│   └── policies-networkpolicy.yaml  default-deny + eccezioni della matrice dei flussi
├── dev/
│   ├── kustomization.yaml        namespace: plik-dev, nessuna patch
│   └── namespace.yaml
└── prod/
    ├── kustomization.yaml        namespace: plik-prod + patch
    ├── namespace.yaml
    └── patch-quota.yaml          quote dimensionate per prod
```

`apps/plik/` non ha un `kustomization.yaml`: nessuna Application punta lì. I file `app-*.yaml` li legge la root app (filtro `*/app*.yaml`), non Kustomize.

## Base e overlay

- **`base/`** contiene le risorse comuni ai due ambienti, scritte una volta sola e **senza namespace**.
- **`dev/` e `prod/`** sono gli overlay. Ognuno include `../base`, aggiunge il proprio `namespace.yaml` e imposta `namespace:`, che Kustomize scrive su tutte le risorse.
- La quota in base ha i valori di dev. Prod li sovrascrive con `patch-quota.yaml`, una strategic merge patch che trova la risorsa per `kind` + `name` (`ResourceQuota/plik-quota`).

```yaml
# apps/plik/prod/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: plik-prod
resources:
  - namespace.yaml     # risorsa da creare
  - ../base
patches:
  - path: patch-quota.yaml   # modifica a una risorsa già caricata dalla base
```

> `resources:` elenca oggetti da **creare**; `patches:` elenca **modifiche** a oggetti già caricati.
> Un file come `namespace.yaml` sotto `patches:` fa fallire la build.

### Cosa succede alla build

1. Kustomize legge l'overlay e carica in memoria `namespace.yaml` e tutto ciò che elenca `base/kustomization.yaml`.
2. Applica le patch alle risorse in memoria.
3. Mette `namespace: plik-<env>` su tutte le risorse.
4. Produce un unico elenco di YAML, con **una sola** `plik-quota`, già con i valori dell'ambiente.
5. Argo CD applica quell'elenco al cluster, nell'ordine delle sync wave.

Durante la lettura non viene applicato niente: il cluster riceve solo il risultato finale.

### Dove compare "namespace"

| Dove | Ruolo |
|---|---|
| `dev/namespace.yaml` (`kind: Namespace`) | **Crea** il namespace |
| `namespace:` nel kustomization dell'overlay | **Assegna** il namespace a tutte le risorse della base |
| `metadata.namespace: openshift-gitops` nell'Application | Dove vive l'oggetto Application (namespace di Argo CD) |
| `destination.namespace` nell'Application | Ripiego per le risorse senza namespace; va tenuto uguale a quello del kustomization |
| `namespaceSelector` nelle NetworkPolicy | Label che indicano **da quale** namespace arriva il traffico; Kustomize non le tocca |

## Ordine di applicazione (sync wave)

| Wave | Risorse |
|---|---|
| -3 | Namespace |
| -2 | ResourceQuota, LimitRange, NetworkPolicy |
| -1 | SecretStore, ExternalSecret |
| 0 | PostgreSQL, Garage |
| 1 | Job di bootstrap di Garage |
| 2 | Plik |

Quota, LimitRange e NetworkPolicy stanno nella stessa wave, così le regole sono già attive quando arrivano i primi pod.

## ResourceQuota

La quota limita quanto ogni ambiente può prendere dell'unico nodo. Somma le **requests e i limits dichiarati** da tutti i container del namespace (non il consumo reale) e rifiuta i pod che farebbero superare il tetto.

### Come è dimensionata

Stima per componente:

| Componente | requests CPU / mem | limits CPU / mem | PVC |
|---|---|---|---|
| Plik | 100m / 128Mi | 500m / 256Mi | – |
| PostgreSQL (istanza + sidecar) | 250m / 512Mi | 1 / 1Gi | 1 × 10Gi |
| Pooler PgBouncer | 50m / 64Mi | 200m / 128Mi | – |
| Garage | 100m / 256Mi | 500m / 512Mi | 2 (meta 1Gi + data 20Gi) |
| Job / CronJob | 50m / 64Mi | 250m / 128Mi | – |

Valori moltiplicati per il **numero massimo di pod che possono esistere insieme**, più un margine del 20-30%:

- in prod Plik va calcolato su **6 pod** (massimo dell'HPA), non su 3, altrimenti l'HPA non riesce a scalare;
- +1 pod per il rolling update (`maxSurge`);
- il sidecar del Barman Cloud Plugin in ogni istanza PostgreSQL;
- Job di bootstrap e, in prod, CronJob rclone.

| Chiave | dev (base) | prod (patch) |
|---|---|---|
| `requests.cpu` | 1 | 3 |
| `requests.memory` | 2Gi | 5Gi |
| `limits.cpu` | 3 | 10 |
| `limits.memory` | 3Gi | 8Gi |
| `persistentvolumeclaims` | 5 | 12 |
| `requests.storage` | 20Gi | 120Gi |
| `lvms-vg1.storageclass.storage.k8s.io/requests.storage` | 20Gi | 120Gi |
| `pods` | 10 | 20 |
| `services.loadbalancers` / `services.nodeports` | 0 | 0 |

> I valori sono una stima. Vanno confrontati con l'`allocatable` del nodo (`oc describe node`): la somma di dev + prod + Vault + operatori deve starci.

### `requests.storage` e `<storageclass>.storageclass.storage.k8s.io/requests.storage`

- `requests.storage` somma lo spazio di **tutte** le PVC del namespace.
- `lvms-vg1.storageclass.storage.k8s.io/requests.storage` somma solo le PVC della StorageClass `lvms-vg1`.

Oggi c'è una sola StorageClass, quindi le due chiavi contano la stessa cosa. Le teniamo entrambe perché il vincolo reale è lo spazio del volume group LVM del nodo: se in futuro si aggiunge un'altra StorageClass, il limite sul disco locale resta esplicito. Se il nome della StorageClass è sbagliato la quota non dà errore e non conta niente: verificarlo con `oc get storageclass`.

## LimitRange

Uguale in dev e prod: tra i due ambienti cambia il numero di pod, non la loro dimensione.

| Campo | Valore | Effetto |
|---|---|---|
| `defaultRequest` | 50m / 64Mi | requests assegnate ai container che non le dichiarano |
| `default` | 250m / 256Mi | limits assegnati ai container che non li dichiarano |
| `max` (Container) | 1 CPU / 1Gi | nessun container può chiedere di più |
| `max` (PersistentVolumeClaim) | 30Gi | nessuna singola PVC più grande |

Il LimitRange è necessario insieme alla quota: con `requests.cpu` o `limits.cpu` nella quota, un pod senza quei valori verrebbe rifiutato. I default servono per i container "dimenticati" (Job, sidecar); Plik, PostgreSQL e Garage dichiarano i propri valori.

Ordine dei controlli su ogni pod nuovo:
1. il **LimitRange** completa i valori mancanti e rifiuta i container sopra `max`;
2. la **ResourceQuota** somma i valori al totale del namespace e rifiuta il pod se sfora.

> Se un componente ha bisogno di più di 1 CPU o 1Gi per container, va alzato anche `max`, altrimenti il pod non viene creato e l'errore compare solo negli eventi del namespace.

## NetworkPolicy

`default-deny-ingress` blocca tutto il traffico **in ingresso** ai pod del namespace; ogni altra policy apre uno dei flussi previsti. L'uscita resta libera (backup verso lo storage esterno, Vault, DNS).

### Matrice dei flussi

| # | Da | A | Porta | Policy |
|---|---|---|---|---|
| 1 | Router OpenShift | Plik | 8080 | `allow-router-to-plik` |
| 2 | Plik | Pooler | 5432 | `allow-plik-to-pooler` |
| 3 | Pooler, altre istanze PG, operatore CNPG | PostgreSQL | 5432 | `allow-to-postgres` |
| 4 | Operatore CNPG | PostgreSQL | 8000 | `allow-to-postgres` |
| 5 | Plik, Job bootstrap, CronJob rclone | Garage | 3900 (S3) | `allow-to-garage` |
| 6 | Garage | Garage | 3901 (RPC) | `allow-to-garage` |
| 7 | Job bootstrap | Garage | 3903 (admin) | `allow-to-garage` |
| 8 | Monitoring di OpenShift | tutti | 9187, 9127, 3903, 8080 | `allow-from-monitoring` |

Le righe 3 e 4 servono a CloudNativePG: senza, le repliche non si collegano al primario e l'operatore non vede lo stato dei pod. La riga 8 serve alle dashboard della fase 3.

Selettori usati:

| Chi | Selettore |
|---|---|
| Router | `namespaceSelector` `network.openshift.io/policy-group: ingress` |
| Monitoring | `namespaceSelector` `network.openshift.io/policy-group: monitoring` |
| Operatore CNPG | `namespaceSelector` `kubernetes.io/metadata.name: openshift-operators` |
| Istanze PostgreSQL | `cnpg.io/cluster: plik-db`, `cnpg.io/podRole: instance` |
| Pooler | `cnpg.io/poolerName: plik-db-pooler-rw` |
| Plik / Garage / Job / CronJob | `app.kubernetes.io/name: plik` / `garage` / `garage-bootstrap` / `garage-backup` |

Da verificare quando i componenti sono deployati:
- le label dei pod (`oc get pods --show-labels`): se non coincidono, la policy non seleziona niente e il traffico resta bloccato senza errori visibili;
- il namespace dell'operatore CNPG (`oc get pods -A | grep cloudnative`);
- le porte delle metriche di Plik e Garage sulle versioni installate.

## Application di Argo CD

| | `plik-dev` | `plik-prod` |
|---|---|---|
| File | `app-dev.yaml` | `app-prod.yaml` |
| `targetRevision` | `develop` | `main` |
| `path` | `apps/plik/dev` | `apps/plik/prod` |
| `destination.namespace` | `plik-dev` | `plik-prod` |
| Sync | automatica (`prune`, `selfHeal`) | **manuale** (nessun blocco `automated`) |
| Sync wave | 10 | 10 |

Opzioni comuni:
- `SkipDryRunOnMissingResource=true`: serve per le risorse degli operatori (`Cluster`, `ExternalSecret`…) quando le CRD non sono ancora installate;
- `retry` (5 tentativi, backoff da 10s a 3m): ripete un sync fallito, non avvia sync da solo;
- niente `CreateNamespace=true`: il namespace lo crea `namespace.yaml` nella wave -3.

### Branch: root app e Application

| Cosa | Branch | Decide… |
|---|---|---|
| root app | `develop` | **come sono definite** le Application (`app-*.yaml`) |
| `plik-prod` | `main` | **cosa viene deployato** in prod |

Qualsiasi modifica a un file `app-*.yaml` deve stare sul branch letto dalla root app (oggi `develop`), altrimenti Argo CD non la vede.


### Aggiornare prod

1. Merge su `main`.
2. In Argo CD aprire `plik-prod` e premere **Sync**, oppure `argocd app sync plik-prod`.

La sync automatica si attiva o si disattiva **solo nel file su Git**. Se la si cambia dalla UI o con `argocd app set`, la root app ripristina la versione di Git.

## Verifica

Build locale (senza cluster):

```bash
kustomize build apps/plik/dev
kustomize build apps/plik/prod
diff <(kustomize build apps/plik/dev) <(kustomize build apps/plik/prod)
```

Quando c'è il chart di Plik vanno aggiunte le opzioni `--enable-helm --load-restrictor LoadRestrictionsNone`.

Sul cluster:

```bash
oc get application -n openshift-gitops
oc get application plik-prod -n openshift-gitops -o jsonpath='{.spec.syncPolicy}'; echo   # senza "automated"

oc describe quota plik-quota -n plik-dev        # colonne Used / Hard
oc describe limitrange plik-limits -n plik-dev
oc get networkpolicy -n plik-dev
```

Test della default-deny: un pod che non è nella matrice non deve raggiungere Garage.

```bash
oc run nettest -n plik-dev --rm -it --image=registry.access.redhat.com/ubi9/ubi -- \
  curl -m 3 http://garage-s3:3900     # deve andare in timeout
```
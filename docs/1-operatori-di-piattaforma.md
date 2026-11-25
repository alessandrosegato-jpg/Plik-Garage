# Plik-Garage – operator e platform con Argo CD

Gli operator che servono al progetto (OpenShift Pipelines, External Secrets, CloudNativePG) e la configurazione di cluster che li usa non si installano a mano dalla console: li installa Argo CD leggendo questo repo. Così l'installazione è descritta come codice, è ripetibile e ogni modifica passa da Git.

## Struttura

```
bootstrap/
├── argocd-config.yaml        patch dell'istanza Argo CD (applicata a mano, una volta)
└── root-app.yaml             root app (applicata a mano, una volta)
apps/
├── operators/                wave 1: installa gli operator
│   ├── app.yaml              Application "operators", letta dalla root
│   ├── kustomization.yaml    elenco dei file da applicare
│   ├── pipeline-subscription.yaml
│   ├── eso-subscription.yaml
│   └── cnpg-subscription.yaml
└── platform/                 wave 2: configura il cluster usando gli operator
    ├── app.yaml              Application "platform"
    ├── kustomization.yaml
    ├── external-secrets-config.yaml
    ├── garage-crd.yaml
    ├── cluster-monitoring-config.yaml
    └── barman-cloud-plugin/
        └── kustomization.yaml
```

## Cosa ho fatto

### 1. Configurato Argo CD

```bash
oc patch argocd openshift-gitops -n openshift-gitops --type merge \
  --patch-file bootstrap/argocd-config.yaml
```

È una patch dell'istanza `openshift-gitops` creata dall'operatore OpenShift GitOps, non una risorsa nuova: per questo si usa `oc patch` e non `oc apply`. Aggiunge due cose:

- `kustomizeBuildOptions`: permette a Kustomize di usare chart Helm e file fuori dalla cartella. Non serve agli operator, ma alle app che arriveranno dopo.
- un health check per le `Application`: senza, Argo CD non valuta la salute delle Application figlie e le sync wave tra un'app e l'altra non aspettano.

### 2. Applicato la root app

```bash
oc apply -f bootstrap/root-app.yaml
```

La root legge dal branch `main` solo i file `apps/*/app*.yaml` e crea un'Application per ognuno. Oggi trova solo `apps/operators/app.yaml`; quando aggiungeremo altre app basterà creare la loro cartella con il suo `app.yaml`.

### 3. L'Application `operators`

Definita in `apps/operators/app.yaml`. Argo CD legge `apps/operators` dal branch `main`, esegue `kustomize build` e applica il risultato.

| Impostazione | Perché |
|---|---|
| `sync-wave: "1"` | gli operator vanno installati prima di tutto il resto, perché le altre app usano i loro tipi di risorsa |
| `automated` + `selfHeal: true` | Argo CD applica da solo ogni modifica del repo e annulla le modifiche fatte a mano nel cluster |
| `prune: false` | se un file sparisce per errore dal repo, l'operator non viene disinstallato |
| nessun finalizer | cancellare l'Application non disinstalla gli operator |
| `ServerSideApply=true` | evita errori su risorse grandi come CRD e Subscription |
| `retry` | se un operator non è ancora pronto, Argo CD riprova invece di fermarsi |

Il `kustomization.yaml` elenca solo i tre file delle Subscription. Senza, Argo CD applicherebbe ogni `.yaml` della cartella, compreso `app.yaml`, e l'Application finirebbe per gestire sé stessa.

### 4. Gli operator

Ogni operator si installa con una **Subscription** (cosa installare e da quale catalogo), dentro un namespace che ha un **OperatorGroup** (quali namespace l'operator deve guardare). Poi OLM, il gestore degli operator di OpenShift, crea l'InstallPlan e installa l'operator.

| Operator | Namespace | Catalogo | Cosa c'è nel file |
|---|---|---|---|
| OpenShift Pipelines | `openshift-operators` | `redhat-operators` | solo la Subscription: il namespace e il suo OperatorGroup (`global-operators`) esistono già |
| External Secrets | `external-secrets-operator` | `redhat-operators` | Namespace, OperatorGroup, Subscription |
| CloudNativePG | `cnpg-system` | `certified-operators` | Namespace, OperatorGroup (`spec: {}`, cioè tutti i namespace), Subscription |

Nome del pacchetto e canale si verificano sul cluster prima di scriverli:

```bash
oc get packagemanifests -n openshift-marketplace | grep -Ei 'pipelines|external-secrets|cloudnative'
oc get packagemanifest <pacchetto> -n openshift-marketplace -o jsonpath='{.status.defaultChannel}{"\n"}'
```

### 5. L'Application `platform`

Installare un operator non basta: bisogna anche dirgli cosa fare. `platform` contiene questa configurazione, che vale per tutto il cluster e per tutti gli ambienti.

Ha `sync-wave: "2"`: la root la crea solo quando `operators` è Healthy, perché usa tipi di risorsa che esistono solo dopo l'installazione degli operator. Rispetto a `operators` ha anche `SkipDryRunOnMissingResource=true`, così la prova prima dell'apply non fallisce se un tipo non esiste ancora, e più tentativi (`retry` 10), perché gli operator appena installati ci mettono qualche minuto a essere pronti.

| File | Cosa fa | Perché |
|---|---|---|
| `external-secrets-config.yaml` | `ExternalSecretsConfig` chiamato `cluster` | avvia il controller di External Secrets nel namespace `external-secrets`: senza, l'operator è installato ma nessuno legge Vault |
| `garage-crd.yaml` | CRD `garagenodes.deuxfleurs.fr`, copiata dal repo ufficiale di Garage | i pod Garage la usano per trovarsi tra loro. È nel repo invece che in un URL, così la versione è fissa e Argo CD non dipende dal server di Garage |
| `cluster-monitoring-config.yaml` | `enableUserWorkload: true` in `openshift-monitoring` | attiva Prometheus anche per i namespace delle app. È un ConfigMap unico per il cluster: ogni altra impostazione del monitoring va aggiunta qui |
| `barman-cloud-plugin/` | manifest ufficiale del plugin Barman (v0.16.0) nel namespace `cnpg-system` | fa i backup di PostgreSQL. Deve stare nello stesso namespace dell'operator CloudNativePG e richiede cert-manager e CloudNativePG 1.30 o più recente |

Il manifest del plugin fissa l'utente del container (`runAsUser: 10001`). OpenShift, con la SCC `restricted-v2`, assegna a ogni namespace un intervallo di UID suo e rifiuta un UID fisso fuori intervallo. Invece di usare `anyuid`, una patch di Kustomize toglie `runAsUser` e `runAsGroup` e lascia scegliere l'UID a OpenShift.

## Verifica

```bash
oc get application -n openshift-gitops          # root, operators, platform: Synced, Healthy
oc get csv -n openshift-operators | grep -i pipelines
oc get csv -n external-secrets-operator
oc get csv -n cnpg-system                       # tutte in fase Succeeded
oc get pods -n external-secrets                 # controller di External Secrets Running
oc get crd garagenodes.deuxfleurs.fr
oc get pods -n openshift-user-workload-monitoring
oc get pods,certificate -n cnpg-system          # barman-cloud Running, certificati READY
```

Per vedere in locale cosa applicherà Argo CD: `kustomize build apps/platform`.
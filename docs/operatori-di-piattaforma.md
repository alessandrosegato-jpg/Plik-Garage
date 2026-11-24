# Plik-Garage – deploy degli operator con Argo CD


### 1. Configurato Argo CD

```bash
oc patch argocd openshift-gitops -n openshift-gitops --type merge \
  --patch-file bootstrap/argocd-config.yaml
```

È una patch dell'istanza `openshift-gitops` creata dall'operatore OpenShift GitOps. Aggiunge due cose:

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
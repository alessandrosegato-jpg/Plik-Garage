# External Secrets

I segreti (password, token, chiavi S3) **non stanno nel repo**. Nascono a mano in **Vault** e arrivano nei namespace come Secret Kubernetes grazie a **External Secrets Operator** (ESO). Ogni ambiente legge solo il proprio mount di Vault.

## I pezzi

| Pezzo | Dove | Chi lo crea | A cosa serve |
|---|---|---|---|
| Mount KV v2 `plik-dev`, `plik-prod` | Vault | a mano | Contengono i segreti, un mount per ambiente |
| Policy `plik-<env>-read` | Vault | a mano | Permette solo `read` su `plik-<env>/data/*` |
| Ruolo `eso-plik-<env>` | Vault (auth kubernetes) | a mano | Collega il ServiceAccount `vault-reader` del namespace giusto alla sua policy |
| Controller ESO | namespace `external-secrets` | operatore | Legge Vault e scrive i Secret |
| ServiceAccount `vault-reader` | `apps/plik/base/secrets-serviceaccount.yaml` | Argo CD | L'identità con cui ESO entra in Vault per quel namespace (nessun pod lo usa) |
| SecretStore `vault` | `apps/plik/base/secrets-secretstore.yaml` | Argo CD | Dice a ESO **dove** è Vault, **quale mount** leggere e **con quale ruolo** entrare |
| 4 ExternalSecret | `apps/plik/base/secrets-externalsecrets.yaml` | Argo CD | Dicono **quali chiavi** leggere e **in quale Secret** scriverle |
| Egress verso Vault | `apps/platform/external-secrets-config.yaml` | Argo CD + operatore | Permette al controller di uscire verso Vault:8200 |

## Il flusso

```
Vault plik-dev/db ──▶ controller ESO ──▶ Secret plik-db-credentials (in plik-dev) ──▶ pod
```

1. Il controller trova un'ExternalSecret in `plik-dev` e legge il SecretStore `vault` **dello stesso namespace**.
2. Chiede a Kubernetes un token del ServiceAccount `vault-reader` di `plik-dev`, con audience `vault`.
3. Fa login in Vault con il ruolo `eso-plik-dev`. Vault verifica il token con l'API server (TokenReview, permesso `system:auth-delegator`): nome, namespace e audience devono coincidere con il ruolo.
4. Vault restituisce un suo token (15 minuti) con la policy `plik-dev-read`.
5. Il controller legge le chiavi e scrive il Secret in `plik-dev`. Ripete ogni ora (`refreshInterval: 1h`) e ricrea il Secret se viene modificato o cancellato a mano.

Il flusso va **in una sola direzione**: ESO legge da Vault, non scrive. La policy è di sola lettura.

## Perché un SecretStore per namespace

Il controller è uno solo, ma per ogni namespace **si presenta a Vault con un'identità diversa** (`vault-reader` di quel namespace). L'isolamento lo garantisce Vault stesso:

- il ruolo `eso-plik-prod` accetta solo `vault-reader` di `plik-prod`: dev viene rifiutato al login;
- anche entrato con il suo ruolo, dev ha una policy che legge solo `plik-dev`: su `plik-prod` riceve `permission denied`.

Un `ClusterSecretStore` userebbe un'identità unica, capace di leggere tutti i mount: chiunque possa creare un'ExternalSecret in dev potrebbe chiedere i segreti di prod. Inoltre un'ExternalSecret può usare solo un SecretStore del suo namespace, e un pod può usare solo Secret del suo namespace.

## Una ExternalSecret per ogni Secret

Ogni ExternalSecret crea **un** Secret (`target.name`). I Secret sono divisi per **chi li usa e in che formato**, non per come sono organizzati i percorsi in Vault: una ExternalSecret può leggere più percorsi, e un percorso può finire in più Secret.

| Secret | Da Vault | Formato | Usato da |
|---|---|---|---|
| `plik-db-credentials` | `db` | `kubernetes.io/basic-auth` (`username`, `password`) | CloudNativePG |
| `garage-secrets` | `garage` | chiavi copiate così come sono (`dataFrom.extract`) | Garage, job di bootstrap |
| `garage-plik-key` | `s3` | chiavi copiate così come sono | Job di bootstrap di Garage |
| `plik-backends` | `db` + `s3` | template JSON nelle variabili `PLIKD_*_BACKEND_CONFIG` | Plik (`envFrom`) |

In `plik-backends` la password è dentro la stringa di connessione, quindi tutta la stringa sta nel Secret. Endpoint e bucket restano nella ConfigMap di Plik. Il host `plik-db-pooler-rw` è il Service del Pooler (task 1.6).

## Rete

La connessione verso Vault parte dal **controller**, nel namespace `external-secrets`, non dai namespace di Plik. Servono due permessi:

1. **Uscita dal controller.** L'operatore Red Hat isola i suoi pod con `eso-sys-deny-all-traffic` e apre solo API server e DNS. Le regole aggiuntive si dichiarano in `ExternalSecretsConfig` (`spec.controllerConfig.networkPolicies`), non con NetworkPolicy scritte a mano: l'operatore crea `eso-user-allow-egress-to-vault` (componente `ExternalSecretsCoreController`, namespace `vault`, TCP 8200). Senza questa regola il login va in timeout (`context deadline exceeded`).
2. **Ingresso in Vault.** La NetworkPolicy `vault-ingress` (`apps/vault`) accetta la porta 8200 dal namespace `external-secrets`.

## Configurazione di Vault (a mano, una volta)

Si lancia dentro il pod attivo: `oc exec -it -n vault vault-0 -- sh`, poi `vault login`. Per prod si ripetono mount, policy e ruolo sostituendo `dev` con `prod`.

```
vault secrets enable -path=plik-dev kv-v2
vault auth enable kubernetes
vault write auth/kubernetes/config kubernetes_host=https://kubernetes.default.svc:443
vault policy write plik-dev-read - <<EOF
path "plik-dev/data/*" {
  capabilities = ["read"]
}
EOF
vault write auth/kubernetes/role/eso-plik-dev bound_service_account_names=vault-reader bound_service_account_namespaces=plik-dev policies=plik-dev-read audience=vault ttl=15m
```

- `secrets enable`: crea il mount. KV v2 tiene le versioni dei valori; internamente i dati stanno sotto `plik-dev/data/`, per questo la policy usa quel percorso.
- `auth enable kubernetes` e `config`: login con il token di un ServiceAccount, verificato da Vault presso l'API server. Nessuna password salvata da nessuna parte. Si fanno una volta per tutti gli ambienti.
- `policy write`: cosa si può fare, solo `read` sul mount dell'ambiente.
- `role`: chi può entrare (`vault-reader` di `plik-dev`, token con audience `vault`) e quale policy riceve. L'audience impedisce di usare un token del ServiceAccount creato per un altro scopo.

I valori si creano con i comandi del foglio "Segreti Vault" della board (`vault kv put -mount=plik-dev db ...`, `s3`, `garage`). I segreti di prod si creano nel task 1.13, prima della prima sync manuale di `plik-prod`.

## Verifiche

```
oc get secretstore,externalsecret -n plik-dev
oc describe secret plik-db-credentials plik-backends garage-secrets garage-plik-key -n plik-dev
oc get networkpolicy -n external-secrets
```

Aggiornamento di un valore (non usare `db`: cambiarla in Vault non cambia la password nel database):

```
oc get secret garage-secrets -n plik-dev -o jsonpath='{.data.metrics_token}' | shasum
vault kv patch -mount=plik-dev garage metrics_token=$(openssl rand -hex 32)
oc annotate externalsecret garage-secrets -n plik-dev force-sync=$(date +%s) --overwrite
oc get secret garage-secrets -n plik-dev -o jsonpath='{.data.metrics_token}' | shasum
```

Il comando `vault kv patch` si lancia nel pod di Vault. Senza `force-sync` il valore arriva comunque entro un'ora.

Isolamento tra dev e prod (dal Mac; i token restano in variabili):

```
TOKEN=$(oc create token vault-reader -n plik-dev --audience vault)
oc exec -n vault vault-0 -- vault write auth/kubernetes/login role=eso-plik-prod jwt=$TOKEN
DEVTOKEN=$(oc exec -n vault vault-0 -- vault write -field=token auth/kubernetes/login role=eso-plik-dev jwt=$TOKEN)
oc exec -n vault vault-0 -- env VAULT_TOKEN=$DEVTOKEN vault kv get -mount=plik-dev -field=username db
oc exec -n vault vault-0 -- env VAULT_TOKEN=$DEVTOKEN vault read plik-prod/data/db
oc exec -n vault vault-0 -- env VAULT_TOKEN=$DEVTOKEN vault token revoke -self
```

Atteso: login con `eso-plik-prod` rifiutato, lettura di `plik-dev` riuscita (`plik`), lettura di `plik-prod` con `permission denied`.
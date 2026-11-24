# Vault in alta disponibilità su OpenShift

Per la [issue #50](https://github.com/alessandrosegato-jpg/Plik-Garage/issues/50) ho configurato il deploy di Vault in modalità HA tramite Argo CD in Plik-Garage. Vault servirà come fonte delle credenziali che External Secrets Operator (ESO) sincronizzerà nei namespace applicativi.

## Cosa ho aggiunto

I manifest sono in [`apps/vault/`](https://github.com/alessandrosegato-jpg/Plik-Garage/tree/develop/apps/vault).

| File | Ruolo |
| --- | --- |
| `app.yaml` | Definisce l’Application Argo CD `vault` alla sync wave 3. Usa il chart ufficiale HashiCorp `0.34.1` e i file di configurazione dal branch `develop`. |
| `values.yaml` | Configura Vault `2.0.4` con tre repliche, storage Raft e un volume persistente da `10 GiB` per pod sulla StorageClass `lvms-vg1`. |
| `route.yaml` | Espone Vault via HTTPS con terminazione TLS sul router OpenShift e inoltra le richieste al Service `vault-active`. |
| `networkpolicy.yaml` | Consente le connessioni tra i pod Vault e l’accesso all’API dal router OpenShift e dal namespace di ESO. |
| `kustomization.yaml` | Include la Route e la NetworkPolicy nel namespace `vault`. |

## Come avviene il deploy

La root Application cerca i file `apps/*/app*.yaml` su `develop`. Quando rileva `apps/vault/app.yaml`, Argo CD crea l’Application `vault` e distribuisce nel namespace `vault` le risorse generate da Helm e Kustomize. Le tre repliche usano Raft per replicare i dati. La Route punta al Service che indirizza le richieste alla replica attiva.

## Stato e collaudo

La [PR #56](https://github.com/alessandrosegato-jpg/Plik-Garage/pull/56) è stata unita in `develop`. Dopo il deploy, `vault-0`, `vault-1` e `vault-2` risultavano `Ready`.

Per completare la issue restano da confermare lo stato unsealed dei tre pod, i tre membri Raft e la risposta HTTPS della Route. La configurazione applicativa comprende i mount KV v2 `plik-dev`, `plik-prod` e `plik-ci`, l’autenticazione Kubernetes per ESO, le policy e i ruoli di sola lettura e i segreti dev `db`, `s3` e `garage`. Il collaudo si conclude con la prova del runbook da parte di un secondo operatore, la verifica che le quote di unseal siano custodite fuori dal repository e la revoca del root token iniziale dopo aver verificato l’accesso amministrativo.

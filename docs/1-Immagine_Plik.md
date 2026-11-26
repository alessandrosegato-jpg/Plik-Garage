# Immagine di Plik su OpenShift

Per la [issue #45](https://github.com/alessandrosegato-jpg/Plik-Garage/issues/45) ho verificato che l’immagine ufficiale di Plik funzioni su OpenShift con la SCC `restricted-v2`, un UID assegnato dal cluster e il filesystem radice in sola lettura.

## Come ho eseguito il test

Ho avviato il pod `plik-official` nel progetto di test `plik-image-test-tommaso` con l’immagine `docker.io/rootgg/plik:1.4.3`. Nel manifest ho richiesto la SCC `restricted-v2` e impostato `readOnlyRootFilesystem: true`. OpenShift ha assegnato l’UID dal range del namespace.

Per il test ho montato un volume temporaneo `emptyDir` su `/data`. Ho configurato Plik per salvare gli upload in `/data/files` e il database SQLite in `/data/plik.db`. In questo modo l’applicazione scrive i dati di prova sul volume mentre il filesystem dell’immagine rimane in sola lettura.

## Stato e collaudo

| Verifica | Risultato |
| --- | --- |
| SCC effettiva del pod | `openshift.io/scc: restricted-v2` |
| UID effettivo | `1001190000`, assegnato da OpenShift |
| Filesystem radice `/` | Montato in sola lettura (`ro`) |
| Volume `/data` | Montato in lettura e scrittura (`rw`) |
| Pod e health check | `Ready`, zero riavvii, `/health` risponde `ok` |
| Prova applicativa | Upload e primo download completati con `200 OK` |

Il digest dell’immagine verificata è `sha256:114ed0bd4339032af50aa578091ead8b9ecb25290b38775f7fb4948c376b905b`.

Per ripetere le verifiche sul pod di test:

```bash
TEST_NS=plik-image-test-tommaso
oc -n "$TEST_NS" get pod plik-official \
  -o jsonpath='{.metadata.annotations.openshift\.io\/scc}{"\n"}'
oc -n "$TEST_NS" exec plik-official -- id -u
oc -n "$TEST_NS" exec plik-official -- cat /proc/self/mountinfo |
  awk '$5 == "/" || $5 == "/data" {print $5, $6}'
oc -n "$TEST_NS" exec plik-official -- wget -qO- http://127.0.0.1:8080/health
```

## Decisione

Entrambi i criteri della issue #45 sono soddisfatti. La scelta è riutilizzare l’immagine ufficiale e farne il mirror nel registry interno durante l’integrazione con la [pipeline CI della issue #42](https://github.com/alessandrosegato-jpg/Plik-Garage/issues/42). Il collaudo è riuscito con l’immagine originale, quindi per questa task il mirror è sufficiente.

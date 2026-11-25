#!/usr/bin/env python3
"""Bootstrap idempotente di Garage tramite admin API v2 (Garage v2.x).

A ogni esecuzione legge lo stato e fa SOLO quello che manca:
  1. aspetta che i nodi Garage rispondano
  2. (più nodi) li collega tra loro
  3. assegna un ruolo ai nodi senza ruolo e applica il layout
  4. importa la chiave S3 di Plik, se non esiste già
  5. crea il bucket, se non esiste già
  6. dà alla chiave i permessi read/write/owner sul bucket

Solo libreria standard di Python: nessuna dipendenza da installare.
Se una risposta non ha il formato atteso, il job si ferma stampando la
risposta intera: così si vede subito quale campo dell'API è cambiato.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

ADMIN_TOKEN = os.environ["GARAGE_ADMIN_TOKEN"]
KEY_ID = os.environ["PLIK_ACCESS_KEY_ID"]
KEY_SECRET = os.environ["PLIK_SECRET_ACCESS_KEY"]
REPLICAS = int(os.environ.get("GARAGE_REPLICAS", "1"))
# Indirizzo admin di ogni nodo, tramite il Service headless: {i} = 0, 1, 2...
ADMIN_URL = os.environ.get("GARAGE_ADMIN_URL", "http://garage-{i}.garage-headless:3903")
RPC_PORT = int(os.environ.get("GARAGE_RPC_PORT", "3901"))
ZONE = os.environ.get("GARAGE_ZONE", "dc1")
CAPACITY = int(os.environ["GARAGE_CAPACITY_BYTES"])   # spazio offerto da ogni nodo
BUCKET = os.environ.get("GARAGE_BUCKET", "plik")
KEY_NAME = os.environ.get("GARAGE_KEY_NAME", "plik")
WAIT_SECONDS = int(os.environ.get("WAIT_SECONDS", "300"))


def log(msg):
    print(f"[garage-bootstrap] {msg}", flush=True)


class ApiError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


def api(method, path, body=None, node=0):
    """Chiama l'admin API del nodo `node`. Restituisce il JSON (o None)."""
    url = ADMIN_URL.format(i=node) + path
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {ADMIN_TOKEN}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, e.read().decode(errors="replace")) from None


def field(obj, name, what):
    """Legge un campo obbligatorio; se manca, si ferma mostrando la risposta."""
    if not isinstance(obj, dict) or name not in obj:
        log(f"ERRORE: campo '{name}' assente in {what}. Risposta: {json.dumps(obj)}")
        sys.exit(2)
    return obj[name]


def wait_for_nodes():
    """1. Aspetta che ogni nodo risponda all'admin API (non serve che sia Ready)."""
    deadline = time.time() + WAIT_SECONDS
    for i in range(REPLICAS):
        while True:
            try:
                api("GET", "/v2/GetClusterStatus", node=i)
                log(f"nodo {i} raggiungibile")
                break
            except (ApiError, OSError) as e:
                if time.time() > deadline:
                    log(f"ERRORE: nodo {i} non risponde dopo {WAIT_SECONDS}s: {e}")
                    sys.exit(1)
                log(f"attendo il nodo {i}... ({e})")
                time.sleep(5)


def own_node(status, i):
    """Trova il nodo i nel suo stesso GetClusterStatus: hostname = nome del pod."""
    pod = f"garage-{i}"
    for n in field(status, "nodes", "GetClusterStatus"):
        if n.get("hostname") == pod:
            return n
    log(f"ERRORE: nessun nodo con hostname {pod}. Risposta: {json.dumps(status)}")
    sys.exit(2)


def connect_nodes():
    """2. Con più nodi, presenta a garage-0 tutti gli altri (idempotente)."""
    if REPLICAS < 2:
        return
    peers = []
    for i in range(1, REPLICAS):
        node = own_node(api("GET", "/v2/GetClusterStatus", node=i), i)
        node_id = field(node, "id", "nodo in GetClusterStatus")
        peers.append(f"{node_id}@garage-{i}.garage-headless:{RPC_PORT}")
    res = api("POST", "/v2/ConnectClusterNodes", peers)
    log(f"ConnectClusterNodes: {json.dumps(res)}")
    # Aspetta che garage-0 veda tutti i nodi
    deadline = time.time() + WAIT_SECONDS
    while True:
        nodes = field(api("GET", "/v2/GetClusterStatus"), "nodes", "GetClusterStatus")
        up = [n for n in nodes if n.get("isUp")]
        if len(up) >= REPLICAS:
            log(f"{len(up)} nodi attivi nel cluster")
            return
        if time.time() > deadline:
            log(f"ERRORE: solo {len(up)}/{REPLICAS} nodi attivi")
            sys.exit(1)
        time.sleep(5)


def apply_layout():
    """3. Dà un ruolo ai nodi che non ce l'hanno e applica il nuovo layout."""
    nodes = field(api("GET", "/v2/GetClusterStatus"), "nodes", "GetClusterStatus")
    missing = [n for n in nodes if n.get("isUp") and not n.get("role")]
    layout = api("GET", "/v2/GetClusterLayout")
    staged = layout.get("stagedRoleChanges") or []
    if not missing and not staged:
        log(f"layout già applicato (versione {field(layout, 'version', 'GetClusterLayout')}): niente da fare")
        return
    if missing:
        roles = [{"id": field(n, "id", "nodo"), "zone": ZONE, "capacity": CAPACITY,
                  "tags": [n.get("hostname") or ""]} for n in missing]
        log(f"assegno il ruolo a {len(roles)} nodo/i (zona {ZONE}, capacità {CAPACITY} byte)")
        api("POST", "/v2/UpdateClusterLayout", {"roles": roles})
        layout = api("GET", "/v2/GetClusterLayout")
    new_version = field(layout, "version", "GetClusterLayout") + 1
    api("POST", "/v2/ApplyClusterLayout", {"version": new_version})
    log(f"layout applicato: versione {new_version}")


def ensure_key():
    """4. Importa la chiave di Plik (presa dal Secret s3) se Garage non la conosce."""
    try:
        api("GET", f"/v2/GetKeyInfo?id={KEY_ID}")
        log(f"chiave {KEY_ID} già presente: niente da fare")
        return
    except ApiError as e:
        if e.status not in (400, 404):
            raise
    api("POST", "/v2/ImportKey",
        {"accessKeyId": KEY_ID, "secretAccessKey": KEY_SECRET, "name": KEY_NAME})
    log(f"chiave {KEY_ID} importata")


def ensure_bucket():
    """5. Crea il bucket con alias globale, se non esiste. Restituisce l'id interno."""
    try:
        info = api("GET", f"/v2/GetBucketInfo?globalAlias={BUCKET}")
        log(f"bucket '{BUCKET}' già presente")
    except ApiError as e:
        if e.status not in (400, 404):
            raise
        info = api("POST", "/v2/CreateBucket", {"globalAlias": BUCKET})
        log(f"bucket '{BUCKET}' creato")
    return field(info, "id", "informazioni del bucket")


def ensure_permissions(bucket_id):
    """6. Permessi read/write/owner della chiave sul bucket (AllowBucketKey è idempotente)."""
    api("POST", "/v2/AllowBucketKey", {
        "bucketId": bucket_id,
        "accessKeyId": KEY_ID,
        "permissions": {"read": True, "write": True, "owner": True},
    })
    log(f"permessi read/write/owner di {KEY_ID} su '{BUCKET}' verificati")


def main():
    wait_for_nodes()
    connect_nodes()
    apply_layout()
    ensure_key()
    ensure_permissions(ensure_bucket())
    log("bootstrap completato")


if __name__ == "__main__":
    try:
        main()
    except ApiError as e:
        log(f"ERRORE dall'admin API: {e}")
        sys.exit(1)

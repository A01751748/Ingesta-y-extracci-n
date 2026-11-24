"""Hace de frontend + orquestador + validador para probar el flujo completo.

docker/run.sh python -m demo.simular datos/pares [--espera 600]
"""
import argparse
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path

import boto3
import psycopg

EP = "http://localhost:54566"
COLA = EP + "/000000000000/"
DSN = "postgresql://postgres:demo@localhost:55432/facturas"
CRED = dict(endpoint_url=EP, region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("entrada", type=Path)
    p.add_argument("--espera", type=int, default=600)
    a = p.parse_args()
    archivos = [a.entrada] if a.entrada.is_file() else [f for f in sorted(a.entrada.rglob("*")) if f.is_file()]
    if not archivos:
        p.error("no hay archivos")
    s3, sqs = boto3.client("s3", **CRED), boto3.client("sqs", **CRED)
    lote, nombres = f"lote-{uuid.uuid4().hex[:8]}", {}

    # --- Orquestador: sube, registra el documento y DESPUÉS encola
    for f in archivos:
        doc, datos = uuid.uuid4().hex, f.read_bytes()
        key, sha = f"{lote}/{doc}", hashlib.sha256(datos).hexdigest()
        s3.put_object(Bucket="facturas", Key=key, Body=datos)
        with psycopg.connect(DSN) as c:
            c.execute("INSERT INTO documentos (documento_id, lote_id, bucket, s3_key, sha256) VALUES (%s,%s,%s,%s,%s)",
                      (doc, lote, "facturas", key, sha))
        sqs.send_message(QueueUrl=COLA + "q-ingesta", MessageBody=json.dumps(
            {"documento_id": doc, "lote_id": lote, "bucket": "facturas", "key": key, "sha256": sha, "intento": 1}))
        nombres[doc] = f.name
    print(f"{lote}: {len(nombres)} documentos encolados")

    # --- Validador: lee q-validacion y la extracción de la base
    fin, vistos = time.time() + a.espera, set()
    while time.time() < fin and vistos != set(nombres):
        for msg in sqs.receive_message(QueueUrl=COLA + "q-validacion", WaitTimeSeconds=5,
                                       MaxNumberOfMessages=10).get("Messages", []):
            m = json.loads(msg["Body"])
            sqs.delete_message(QueueUrl=COLA + "q-validacion", ReceiptHandle=msg["ReceiptHandle"])
            if m["documento_id"] not in nombres:
                continue
            vistos.add(m["documento_id"])
            with psycopg.connect(DSN) as c:
                fila = c.execute("SELECT datos FROM extracciones_json WHERE documento_id=%s AND intento=%s",
                                 (m["documento_id"], m["intento"])).fetchone()
                if m["estado_extraccion"] == "ok":  # aquí van las reglas de Sofía
                    c.execute("UPDATE documentos SET estado='listo' WHERE documento_id=%s AND estado='validando'",
                              (m["documento_id"],))
            d = fila[0] if fila else {}
            print(f"  {nombres[m['documento_id']][:45]:45} {m['estado_extraccion']:5} "
                  f"{m['codigo_motivo'] or d.get('ruta')}")
    with psycopg.connect(DSN) as c:
        print("avance:", c.execute("SELECT total, pendientes FROM v_avance_lote WHERE lote_id=%s", (lote,)).fetchone())
    return 0 if vistos == set(nombres) else 1


if __name__ == "__main__":
    sys.exit(main())

"""Contra LocalStack y PostgreSQL de docker-compose (solo `postgres localstack`, sin workers).
INGESTA_TEST=1 ... ver INTEGRACION.md. Sin eso se omiten."""
import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("INGESTA_TEST_PG"), reason="requiere docker compose")

import boto3  # noqa: E402
import psycopg  # noqa: E402

from ingesta.adaptadores import lambda_ingesta, worker  # noqa: E402
from ingesta.adaptadores.aws import Aws  # noqa: E402
from ingesta.adaptadores.base import Base  # noqa: E402
from ingesta.adaptadores.procesador import BORRAR, REINTENTAR, Procesador  # noqa: E402
from ingesta.config import Config  # noqa: E402
from ingesta.errores import ErrorTransitorio  # noqa: E402
from ingesta.mensajes import MensajeDocumento, MensajeResultado  # noqa: E402
from ingesta.modelo import Respuesta  # noqa: E402
from tests.test_ingesta import FACTURA, FIX, OK, U1, Falso, hacer_pdf, leido  # noqa: E402

ADMIN = "postgresql://postgres:demo@localhost:55432/facturas"
XML = (FIX / "cfdi40.xml").read_bytes()


@pytest.fixture
def e(monkeypatch):
    for l in (Path(__file__).parent.parent / ".env.ejemplo").read_text().splitlines():
        if "=" in l and not l.startswith("#"):
            monkeypatch.setenv(*l.split("=", 1))
    # Colas propias por prueba: no hay mezcla ni esperas de purga.
    sqs = boto3.client("sqs", endpoint_url=os.environ["INGESTA_AWS_ENDPOINT"], region_name="us-east-1",
                       aws_access_key_id="test", aws_secret_access_key="test")
    suf = uuid.uuid4().hex[:8]
    for var, q in (("Q_INGESTA", "i"), ("Q_GPU", "g"), ("Q_VALIDACION", "v")):
        attrs = {}
        if q != "v":
            dlq = sqs.create_queue(QueueName=f"{q}-dlq-{suf}")["QueueUrl"]
            arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
            attrs = {"RedrivePolicy": json.dumps({"deadLetterTargetArn": arn, "maxReceiveCount": "3"})}
            monkeypatch.setenv(f"INGESTA_{var}_DLQ", dlq)
        monkeypatch.setenv(f"INGESTA_{var}", sqs.create_queue(QueueName=f"{q}-{suf}", Attributes=attrs)["QueueUrl"])
    with psycopg.connect(ADMIN) as c:
        c.execute("UPDATE outbox_ingesta SET enviado_en=now() WHERE enviado_en IS NULL")

    class E:
        cfg = Config.de_entorno()
        base, aws = Base(cfg.dsn), Aws(cfg)

        def alta(self, datos, sha=None, subir=True):
            d = uuid.uuid4().hex
            m = MensajeDocumento(documento_id=d, lote_id="l", bucket="facturas", key=d,
                                 sha256=sha or hashlib.sha256(datos).hexdigest())
            if subir:
                self.aws.s3.put_object(Bucket="facturas", Key=d, Body=datos)
            with psycopg.connect(ADMIN) as c:
                c.execute("INSERT INTO documentos (documento_id,lote_id,bucket,s3_key,sha256) VALUES (%s,'l','facturas',%s,%s)",
                          (d, d, m.sha256))
            return m

        def proc(self, etapa="cpu", **k):
            return Procesador(etapa, self.cfg, self.base, self.aws, **k)

        def doc(self, m, cols="estado"):
            with psycopg.connect(ADMIN) as c:
                r = c.execute(f"SELECT {cols} FROM documentos WHERE documento_id=%s", (m.documento_id,)).fetchone()
            return r[0] if len(r) == 1 else r

        def sql(self, q, *a):
            with psycopg.connect(ADMIN) as c:
                return c.execute(q, a).fetchall() if q.startswith("SELECT") else c.execute(q, a).rowcount

        def recibir(self, url):
            return [x["Body"] for x in sqs.receive_message(
                QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=1).get("Messages", [])]
    return E()


def test_xml_completo(e):
    m = e.alta(XML)
    assert e.proc().procesar(m.model_dump_json()) == BORRAR
    assert e.doc(m) == "validando"
    r = MensajeResultado.model_validate_json(e.recibir(e.cfg.q_validacion)[0])
    assert (r.documento_id, r.estado_extraccion) == (m.documento_id, "ok")
    d = e.sql("SELECT datos FROM extracciones_json WHERE documento_id=%s", m.documento_id)[0][0]
    assert d["campos"]["total"]["valor"] == "1060.00"
    assert d["lote_id"] == "l" and d["recibido_en"].endswith(("Z", "+00:00"))

    assert e.sql("SELECT valor FROM extracciones WHERE documento_id=%s AND campo='total'", m.documento_id) == [("1060.00",)]

def test_recibido_en_no_cambia_al_reprocesar(e):
    m = e.alta(XML)
    e.sql("UPDATE documentos SET recibido_en='2026-10-01T06:00:00Z' WHERE documento_id=%s", m.documento_id)
    for n in (1, 2):
        e.sql("UPDATE documentos SET estado='recibido', etapa='cpu', intento=%s WHERE documento_id=%s", n, m.documento_id)
        e.proc().procesar(m.model_copy(update={"intento": n, "lote_id": "otro"}).model_dump_json())
    ds = [d for (d,) in e.sql("SELECT datos FROM extracciones_json WHERE documento_id=%s", m.documento_id)]
    assert len(ds) == 2 and {d["recibido_en"] for d in ds} == {"2026-10-01T06:00:00Z"}
    assert {d["lote_id"] for d in ds} == {"l"}  # manda la base


def test_pdf_va_a_gpu_y_termina(e):
    m = e.alta(hacer_pdf(FACTURA))
    e.proc().procesar(m.model_dump_json())
    assert e.doc(m, "estado, etapa") == ("extrayendo", "gpu")
    cuerpo = e.recibir(e.cfg.q_gpu)[0]
    falso = Falso(Respuesta(leido(uuid=U1, total="115.20"), OK))
    assert e.proc("gpu", modelo=falso).procesar(cuerpo) == BORRAR
    assert e.doc(m) == "validando"


def test_mensaje_repetido_no_duplica(e):
    m = e.alta(XML)
    for _ in range(3):
        assert e.proc().procesar(m.model_dump_json()) == BORRAR
    assert len(e.recibir(e.cfg.q_validacion)) == 1


def test_repetido_de_cpu_despues_de_pasar_a_gpu(e):
    m = e.alta(hacer_pdf(FACTURA))
    e.proc().procesar(m.model_dump_json())
    assert e.proc().procesar(m.model_dump_json()) == BORRAR
    assert len(e.recibir(e.cfg.q_gpu)) == 1


def test_dos_workers_a_la_vez(e):
    m, res = e.alta(XML), []
    hilos = [threading.Thread(target=lambda: res.append(e.base.tomar(m, "cpu", 60)[0])) for _ in range(8)]
    [h.start() for h in hilos]; [h.join() for h in hilos]
    assert sum(r is not None for r in res) == 1


def test_worker_muerto_se_recupera(e):
    m = e.alta(XML)
    viejo = e.base.tomar(m, "cpu", 60)[0].id
    assert e.proc().procesar(m.model_dump_json()) == REINTENTAR  # sigue vivo
    e.sql("UPDATE documentos SET arrendado_hasta=now()-interval '1 s' WHERE documento_id=%s", m.documento_id)
    assert e.proc().procesar(m.model_dump_json()) == BORRAR
    assert e.base.renovar(m, viejo, 60) is False


def test_worker_lento_no_pisa_tiempo_agotado(e):
    m = e.alta(hacer_pdf(FACTURA))
    e.proc().procesar(m.model_dump_json())

    class Lento(Falso):
        def extraer(self, *a):
            e.sql("UPDATE documentos SET estado='error', codigo_motivo='TIEMPO_AGOTADO' WHERE documento_id=%s", m.documento_id)
            return super().extraer(*a)
    assert e.proc("gpu", modelo=Lento(Respuesta(leido()))).procesar(m.model_dump_json()) == BORRAR
    assert e.doc(m, "estado, codigo_motivo") == ("error", "TIEMPO_AGOTADO")
    assert e.recibir(e.cfg.q_validacion) == []


def test_error_permanente_avisa(e):
    m = e.alta(b"hola")
    assert e.proc().procesar(m.model_dump_json()) == BORRAR
    assert e.doc(m, "estado, codigo_motivo") == ("error", "TIPO_NO_SOPORTADO")
    assert json.loads(e.recibir(e.cfg.q_validacion)[0])["codigo_motivo"] == "TIPO_NO_SOPORTADO"


@pytest.mark.parametrize("k", [{"sha": "0" * 64}, {"subir": False}])
def test_sha_distinto_o_archivo_ausente(e, k):
    m = e.alta(XML, **k)
    assert e.proc().procesar(m.model_dump_json()) == BORRAR
    assert e.doc(m) == "error"


def test_bucket_ajeno(e):
    m = e.alta(XML).model_copy(update={"bucket": "otro"})
    e.proc().procesar(m.model_dump_json())
    assert e.doc(m) == "error"


def test_modelo_caido_agota_reintentos_y_dlq_cierra(e, monkeypatch):
    m = e.alta(hacer_pdf(FACTURA))
    e.proc().procesar(m.model_dump_json())
    p = e.proc("gpu", modelo=Falso(error=ErrorTransitorio("PROCESAMIENTO_FALLIDO", "x")))
    real = e.aws.sqs.change_message_visibility
    monkeypatch.setattr(e.aws.sqs, "change_message_visibility", lambda **k: real(**{**k, "VisibilityTimeout": 0}))
    for _ in range(3):
        assert worker.atender(e.aws.sqs, e.cfg.q_gpu, p.procesar, 30, 1) == 1
    assert worker.atender(e.aws.sqs, e.cfg.q_gpu, p.procesar, 30, 1) == 0
    assert worker.atender(e.aws.sqs, e.cfg.q_gpu_dlq, lambda c, _: p.fallido(c), 30, 2) == 1
    assert e.doc(m, "estado, codigo_motivo") == ("error", "PROCESAMIENTO_FALLIDO")
    assert json.loads(e.recibir(e.cfg.q_validacion)[0])["estado_extraccion"] == "error"


def test_dlq_no_pisa_documento_vivo_ni_terminado(e):
    m = e.alta(XML)
    e.base.tomar(m, "cpu", 60)
    assert e.proc().fallido(m.model_dump_json()) == REINTENTAR
    n = e.alta(XML)
    e.proc().procesar(n.model_dump_json())
    assert e.proc().fallido(n.model_dump_json()) == BORRAR and e.doc(n) == "validando"


def test_mensaje_veneno(e):
    assert e.proc().procesar("{no es json") == REINTENTAR
    assert e.proc().fallido("{no es json") == BORRAR


def test_mensaje_antes_que_el_documento(e):
    m = MensajeDocumento(documento_id=uuid.uuid4().hex, lote_id="l", bucket="facturas", key="x", sha256="0" * 64)
    assert e.proc().procesar(m.model_dump_json()) == REINTENTAR


def test_sqs_caido_el_aviso_no_se_pierde(e, monkeypatch):
    m = e.alta(XML)
    with monkeypatch.context() as mp:
        mp.setattr(e.aws, "enviar", lambda *a: (_ for _ in ()).throw(ConnectionError()))
        assert e.proc().procesar(m.model_dump_json()) == BORRAR
    assert e.doc(m) == "validando" and e.recibir(e.cfg.q_validacion) == []
    e.proc().despachar()
    assert len(e.recibir(e.cfg.q_validacion)) == 1


def test_base_falla_al_guardar_suelta_el_arriendo(e, monkeypatch):
    m = e.alta(XML)
    with monkeypatch.context() as mp:
        mp.setattr(e.base, "_avisar", lambda *a: (_ for _ in ()).throw(RuntimeError()))
        with pytest.raises(RuntimeError):
            e.proc().procesar(m.model_dump_json())
    assert e.sql("SELECT 1 FROM extracciones_json WHERE documento_id=%s", m.documento_id) == []
    assert e.proc().procesar(m.model_dump_json()) == BORRAR


def test_reproceso_con_nuevo_intento(e):
    m = e.alta(b"hola")
    e.proc().procesar(m.model_dump_json())
    e.sql("UPDATE documentos SET estado='recibido', etapa='cpu', intento=2, codigo_motivo=NULL WHERE documento_id=%s", m.documento_id)
    assert e.proc().procesar(m.model_dump_json()) == BORRAR and e.doc(m) == "recibido"  # intento viejo
    e.proc().procesar(m.model_copy(update={"intento": 2}).model_dump_json())
    assert e.doc(m) == "error"


def test_latido_alarga_el_arriendo(e):
    import dataclasses
    m = e.alta(hacer_pdf(FACTURA))
    e.proc().procesar(m.model_dump_json())
    p, n = e.proc("gpu"), []
    p.cfg = dataclasses.replace(e.cfg, visibilidad_s=2)

    class Lento(Falso):
        def extraer(self, *a):
            time.sleep(4)
            return super().extraer(*a)
    p.modelo = Lento(Respuesta(leido()))
    assert p.procesar(m.model_dump_json(), lambda: n.append(1)) == BORRAR
    assert e.doc(m) == "validando" and len(n) >= 3


def test_lambda_reporta_solo_las_fallas(e):
    lambda_ingesta._proc = None
    m = e.alta(XML)
    r = lambda_ingesta.handler({"Records": [{"messageId": "a", "body": m.model_dump_json()},
                                            {"messageId": "b", "body": "veneno"}]}, None)
    lambda_ingesta._proc = None
    assert r == {"batchItemFailures": [{"itemIdentifier": "b"}]} and e.doc(m) == "validando"


def test_permisos_minimos(e):
    with psycopg.connect(e.cfg.dsn) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("DELETE FROM documentos")

"""PostgreSQL. El arriendo evita que dos workers procesen a la vez y que uno lento
pise lo que ya decidió otro (o el TIEMPO_AGOTADO del cron)."""
import json
import uuid
from datetime import datetime
from typing import NamedTuple, Optional

import psycopg

from ..contrato import Extraccion
from ..mensajes import MensajeDocumento, MensajeResultado

# Conocido y de más: se borra el mensaje. Ocupado o desconocido: se reintenta después.
VIEJO, OCUPADO, DESCONOCIDO = "viejo", "ocupado", "desconocido"


class Arriendo(NamedTuple):
    id: str
    lote_id: str            # de la base: es la fuente de verdad, no el mensaje
    recibido_en: datetime


class Base:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def _con(self):
        return psycopg.connect(self.dsn, connect_timeout=10)

    def tomar(self, m: MensajeDocumento, etapa: str, segundos: int) -> tuple[Optional[Arriendo], Optional[str]]:
        """(Arriendo, None) si se puede procesar; (None, motivo) si no."""
        arriendo = str(uuid.uuid4())
        with self._con() as c:
            fila = c.execute(
                """UPDATE documentos SET estado='extrayendo', arriendo_id=%s, actualizado_en=now(),
                       arrendado_hasta=now() + make_interval(secs => %s)
                   WHERE documento_id=%s AND intento=%s AND etapa=%s
                     AND (estado='recibido' OR (estado='extrayendo' AND
                          (arrendado_hasta IS NULL OR arrendado_hasta < now())))
                   RETURNING lote_id, recibido_en""", (arriendo, segundos, m.documento_id, m.intento, etapa)).fetchone()
            if fila:
                return Arriendo(arriendo, *fila), None
            doc = c.execute("SELECT estado, intento, etapa FROM documentos WHERE documento_id=%s",
                            (m.documento_id,)).fetchone()
        if doc is None or doc[1] < m.intento:
            return None, DESCONOCIDO  # la transacción del orquestador quizá no ha terminado
        if doc == ("extrayendo", m.intento, etapa):
            return None, OCUPADO
        return None, VIEJO

    def renovar(self, m: MensajeDocumento, arriendo: str, segundos: int) -> bool:
        with self._con() as c:
            return c.execute(
                """UPDATE documentos SET arrendado_hasta=now() + make_interval(secs => %s)
                   WHERE documento_id=%s AND arriendo_id=%s AND estado='extrayendo'""",
                (segundos, m.documento_id, arriendo)).rowcount == 1

    def soltar(self, m: MensajeDocumento, arriendo: str) -> None:
        with self._con() as c:
            c.execute("""UPDATE documentos SET arriendo_id=NULL, arrendado_hasta=NULL
                         WHERE documento_id=%s AND arriendo_id=%s AND estado='extrayendo'""",
                      (m.documento_id, arriendo))

    def pasar_a_gpu(self, m: MensajeDocumento, arriendo: str) -> bool:
        with self._con() as c:
            ok = c.execute(
                """UPDATE documentos SET etapa='gpu', arriendo_id=NULL, arrendado_hasta=NULL, actualizado_en=now()
                   WHERE documento_id=%s AND arriendo_id=%s AND estado='extrayendo'""",
                (m.documento_id, arriendo)).rowcount == 1
            if ok:
                self._avisar(c, "q_gpu", m)
        return ok

    def guardar(self, m: MensajeDocumento, arriendo: str, r: Extraccion) -> bool:
        """False si se perdió el arriendo: el resultado se descarta."""
        error = r.error
        with self._con() as c:
            ok = c.execute(
                """UPDATE documentos SET estado=%s, codigo_motivo=%s, detalle_motivo=%s, tipo=%s,
                       arriendo_id=NULL, arrendado_hasta=NULL, actualizado_en=now()
                   WHERE documento_id=%s AND arriendo_id=%s AND estado='extrayendo'""",
                ("error" if error else "validando", error and error.codigo, error and error.detalle,
                 r.tipo_detectado, m.documento_id, arriendo)).rowcount == 1
            if not ok:
                return False
            c.execute("INSERT INTO extracciones_json (documento_id, intento, datos) VALUES (%s,%s,%s)",
                      (m.documento_id, m.intento, r.model_dump_json()))
            for nombre in type(r.campos).model_fields:
                f = getattr(r.campos, nombre)
                if f.valor is None and not f.evidencia:
                    continue
                c.execute(
                    """INSERT INTO extracciones (documento_id, intento, campo, valor, origen, confianza,
                           pagina, bbox, evidencia) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (m.documento_id, m.intento, nombre, f.valor, f.origen, f.confianza, f.pagina,
                     json.dumps(f.bbox) if f.bbox else None, f.evidencia))
            self._avisar(c, "q_validacion", MensajeResultado(
                documento_id=m.documento_id, lote_id=m.lote_id, intento=m.intento,
                estado_extraccion=r.estado_extraccion, codigo_motivo=error.codigo if error else None))
        return True

    def marcar_fallido(self, m: MensajeDocumento) -> str:
        """Desde la DLQ. 'hecho', 'viejo' u 'ocupado' (otro worker lo tiene vivo)."""
        with self._con() as c:
            ok = c.execute(
                """UPDATE documentos SET estado='error', codigo_motivo='PROCESAMIENTO_FALLIDO',
                       detalle_motivo='reintentos agotados', arriendo_id=NULL, arrendado_hasta=NULL,
                       actualizado_en=now()
                   WHERE documento_id=%s AND intento=%s AND estado IN ('recibido','extrayendo')
                     AND (arrendado_hasta IS NULL OR arrendado_hasta < now())""",
                (m.documento_id, m.intento)).rowcount == 1
            if ok:
                self._avisar(c, "q_validacion", MensajeResultado(
                    documento_id=m.documento_id, lote_id=m.lote_id, intento=m.intento,
                    estado_extraccion="error", codigo_motivo="PROCESAMIENTO_FALLIDO"))
                return "hecho"
            doc = c.execute("SELECT estado, intento FROM documentos WHERE documento_id=%s",
                            (m.documento_id,)).fetchone()
        return OCUPADO if doc == ("extrayendo", m.intento) else VIEJO

    @staticmethod
    def _avisar(c, cola: str, cuerpo) -> None:
        c.execute("INSERT INTO outbox_ingesta (cola, cuerpo) VALUES (%s,%s)", (cola, cuerpo.model_dump_json()))

    def despachar(self, enviar, limite: int = 50) -> int:
        """Manda avisos pendientes. Puede repetir uno si muere tras enviar: el consumidor es idempotente."""
        n = 0
        with self._con() as c:
            filas = c.execute(
                """SELECT id, cola, cuerpo FROM outbox_ingesta WHERE enviado_en IS NULL
                   ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED""", (limite,)).fetchall()
            for id_, cola, cuerpo in filas:
                enviar(cola, json.dumps(cuerpo))
                c.execute("UPDATE outbox_ingesta SET enviado_en=now() WHERE id=%s", (id_,))
                n += 1
        return n

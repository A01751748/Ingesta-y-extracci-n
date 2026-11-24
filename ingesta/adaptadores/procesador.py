"""Lo común a Lambda y workers: un mensaje entra, sale una decisión."""
import hashlib
import json
import logging
import threading
from typing import Callable, Optional

from ..contrato import ErrorDoc, Extraccion
from ..errores import ErrorExtraccion
from ..mensajes import MensajeDocumento
from ..pipeline import extraer, extraer_sin_modelo
from .base import VIEJO

log = logging.getLogger("ingesta")
BORRAR, REINTENTAR = "borrar", "reintentar"


def registrar(evento: str, **datos) -> None:
    """JSON por renglón para CloudWatch. Solo ids y códigos: nunca RFC, montos ni contenido."""
    log.info(json.dumps({"evento": evento, **datos}))


class Latido:
    """Mientras se procesa, alarga el arriendo y la visibilidad del mensaje."""

    def __init__(self, cada: float, *acciones: Callable):
        self._fin, self._cada, self._acciones = threading.Event(), cada, acciones
        self._hilo = threading.Thread(target=self._correr, daemon=True)

    def _correr(self):
        while not self._fin.wait(self._cada):
            for accion in self._acciones:
                try:
                    accion()
                except Exception as e:
                    registrar("latido_fallido", error=type(e).__name__)

    def __enter__(self):
        self._hilo.start()

    def __exit__(self, *_):
        self._fin.set()
        self._hilo.join(timeout=30)


class Procesador:
    def __init__(self, etapa: str, cfg, base, aws, modelo=None, ocr=None):
        self.etapa, self.cfg, self.base, self.aws = etapa, cfg, base, aws
        self.modelo, self.ocr = modelo, ocr

    def procesar(self, cuerpo: str, extender: Optional[Callable] = None) -> str:
        try:
            m = MensajeDocumento.model_validate_json(cuerpo)
        except ValueError:
            registrar("mensaje_invalido", etapa=self.etapa)
            return REINTENTAR  # termina en la DLQ, donde alguien lo ve
        ids = dict(documento_id=m.documento_id, lote_id=m.lote_id, etapa=self.etapa, intento=m.intento)
        ttl = self.cfg.visibilidad_s
        tomado, motivo = self.base.tomar(m, self.etapa, ttl)
        if tomado is None:
            registrar("no_tomado", motivo=motivo, **ids)
            return BORRAR if motivo == VIEJO else REINTENTAR
        arriendo = tomado.id
        m = m.model_copy(update={"lote_id": tomado.lote_id})
        try:
            acciones = [lambda: self.base.renovar(m, arriendo, ttl)] + ([extender] if extender else [])
            with Latido(ttl / 3, *acciones):
                r = self._extraer(m, tomado.recibido_en)
            if r is None:
                guardado = self.base.pasar_a_gpu(m, arriendo)
                registrar("a_gpu", guardado=guardado, **ids)
            elif r.error and r.error.reintentable:
                registrar("reintentable", codigo=r.error.codigo, detalle=r.error.detalle, **ids)
                self.base.soltar(m, arriendo)
                return REINTENTAR
            else:
                guardado = self.base.guardar(m, arriendo, r)
                registrar("extraido", guardado=guardado, estado=r.estado_extraccion, ruta=r.ruta,
                          codigo=r.error.codigo if r.error else None, ms=r.duracion_ms, **ids)
        except Exception:
            self.base.soltar(m, arriendo)
            raise
        self.despachar()
        return BORRAR

    def _extraer(self, m: MensajeDocumento, recibido_en) -> Optional[Extraccion]:
        try:
            datos = self.aws.leer(m)
        except ErrorExtraccion as e:
            return Extraccion(documento_id=m.documento_id, lote_id=m.lote_id, sha256=m.sha256,
                              recibido_en=recibido_en,
                              estado_extraccion="error", error=ErrorDoc(
                                  codigo=e.codigo, detalle=e.detalle, reintentable=e.reintentable))
        if self.etapa == "cpu":
            return extraer_sin_modelo(datos, m.documento_id, m.lote_id, recibido_en)
        return extraer(datos, m.documento_id, m.lote_id, modelo=self.modelo,
                       recibido_en=recibido_en,
                       **({"ocr": self.ocr} if self.ocr else {}))

    def despachar(self) -> None:
        """Si falla no importa: el aviso quedó en la base y lo manda el rol `outbox`."""
        try:
            self.base.despachar(self.aws.enviar)
        except Exception as e:
            registrar("despacho_fallido", error=type(e).__name__)

    def fallido(self, cuerpo: str) -> str:
        try:
            m = MensajeDocumento.model_validate_json(cuerpo)
        except ValueError:
            registrar("dlq_mensaje_invalido", sha256=hashlib.sha256(cuerpo.encode()).hexdigest())
            return BORRAR
        res = self.base.marcar_fallido(m)
        registrar("dlq", resultado=res, documento_id=m.documento_id, lote_id=m.lote_id)
        self.despachar()
        return REINTENTAR if res == "ocupado" else BORRAR

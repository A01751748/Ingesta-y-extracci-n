"""Etapa CPU en Lambda. El event source mapping debe tener ReportBatchItemFailures."""
import logging

from ..config import Config
from .aws import Aws
from .base import Base
from .procesador import BORRAR, Procesador, registrar

logging.getLogger("ingesta").setLevel(logging.INFO)
_proc = None


def handler(evento, contexto):
    global _proc
    if _proc is None:
        cfg = Config.de_entorno()
        _proc = Procesador("cpu", cfg, Base(cfg.dsn), Aws(cfg))
    fallas = []
    for r in evento["Records"]:
        try:
            ok = _proc.procesar(r["body"]) == BORRAR
        except Exception as e:
            registrar("error_inesperado", error=type(e).__name__, mensaje=r["messageId"])
            ok = False
        if not ok:
            fallas.append({"itemIdentifier": r["messageId"]})
    return {"batchItemFailures": fallas}

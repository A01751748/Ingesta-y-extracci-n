"""python -m ingesta.adaptadores.worker {cpu|gpu|dlq|outbox}"""
import argparse
import logging
import signal
import sys
import threading

from ..config import Config
from .aws import Aws
from .base import Base
from .procesador import BORRAR, Procesador, registrar

_fin = threading.Event()


def atender(sqs, url: str, accion, visibilidad: int, espera: int = 20) -> int:
    """Un mensaje a la vez. Devuelve cuántos atendió."""
    r = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=1, WaitTimeSeconds=espera,
                            VisibilityTimeout=visibilidad, AttributeNames=["ApproximateReceiveCount"])
    for msg in r.get("Messages", []):
        recibo = msg["ReceiptHandle"]

        def extender():
            sqs.change_message_visibility(QueueUrl=url, ReceiptHandle=recibo, VisibilityTimeout=visibilidad)
        try:
            if _fin.is_set():  # llegó SIGTERM durante la espera: se devuelve sin tocar
                sqs.change_message_visibility(QueueUrl=url, ReceiptHandle=recibo, VisibilityTimeout=0)
                return 0
            ok = accion(msg["Body"], extender) == BORRAR
        except Exception as e:
            registrar("error_inesperado", error=type(e).__name__, mensaje=msg["MessageId"])
            ok = False
        if ok:
            sqs.delete_message(QueueUrl=url, ReceiptHandle=recibo)
        else:
            veces = int(msg.get("Attributes", {}).get("ApproximateReceiveCount", 1))
            sqs.change_message_visibility(QueueUrl=url, ReceiptHandle=recibo,
                                          VisibilityTimeout=min(15 * 2 ** (veces - 1), 600))
        return 1
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="worker")
    p.add_argument("rol", choices=["cpu", "gpu", "dlq", "outbox"])
    rol = p.parse_args(argv).rol
    logging.basicConfig(level=logging.WARNING, format="%(message)s", stream=sys.stdout)
    logging.getLogger("ingesta").setLevel(logging.INFO)
    # Termina el documento en curso y sale. Si lo matan antes, el arriendo vence y SQS lo reentrega.
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: _fin.set())
    cfg = Config.de_entorno()
    base, aws = Base(cfg.dsn), Aws(cfg)
    proc = Procesador("gpu" if rol == "gpu" else "cpu", cfg, base, aws)
    registrar("inicio", rol=rol)
    while not _fin.is_set():
        try:
            if rol in ("cpu", "gpu"):
                atender(aws.sqs, cfg.q_ingesta if rol == "cpu" else cfg.q_gpu, proc.procesar, cfg.visibilidad_s)
            elif rol == "dlq":
                for url in (cfg.q_ingesta_dlq, cfg.q_gpu_dlq):
                    atender(aws.sqs, url, lambda c, _: proc.fallido(c), 30, espera=5)
            else:
                proc.despachar()
                _fin.wait(5)
        except Exception as e:  # SQS o la base caídos: no morir en bucle
            registrar("servicio_caido", rol=rol, error=type(e).__name__)
            _fin.wait(5)
    registrar("fin", rol=rol)
    return 0


if __name__ == "__main__":
    sys.exit(main())

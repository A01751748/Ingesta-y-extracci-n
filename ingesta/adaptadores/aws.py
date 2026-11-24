import hashlib
import os

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from ..config import Config
from ..errores import ErrorPermanente
from ..mensajes import MensajeDocumento


def _cliente(servicio: str):
    # INGESTA_AWS_ENDPOINT solo se usa con LocalStack.
    return boto3.client(servicio, endpoint_url=os.environ.get("INGESTA_AWS_ENDPOINT") or None,
                        config=BotoConfig(retries={"max_attempts": 3, "mode": "standard"},
                                          s3={"addressing_style": "path"}
                                          if os.environ.get("INGESTA_AWS_ENDPOINT") else None))


class Aws:
    def __init__(self, cfg: Config):
        self.cfg, self.s3, self.sqs = cfg, _cliente("s3"), _cliente("sqs")

    def leer(self, m: MensajeDocumento) -> bytes:
        if m.bucket != self.cfg.bucket:  # un mensaje no puede hacernos leer otro bucket
            raise ErrorPermanente("PROCESAMIENTO_FALLIDO", "bucket no permitido")
        try:
            o = self.s3.get_object(Bucket=m.bucket, Key=m.key)
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                raise ErrorPermanente("PROCESAMIENTO_FALLIDO", "el archivo no está en S3") from e
            raise
        if o["ContentLength"] > self.cfg.max_bytes:
            raise ErrorPermanente("PROCESAMIENTO_FALLIDO", "archivo demasiado grande")
        datos = o["Body"].read()
        if hashlib.sha256(datos).hexdigest() != m.sha256:
            raise ErrorPermanente("PROCESAMIENTO_FALLIDO", "el SHA-256 no coincide con el registrado")
        return datos

    def enviar(self, cola: str, cuerpo: str) -> None:
        self.sqs.send_message(QueueUrl=getattr(self.cfg, cola), MessageBody=cuerpo)

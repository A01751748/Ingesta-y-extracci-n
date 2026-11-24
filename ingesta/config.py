"""Configuración por entorno. Los secretos llegan como variables (Secrets Manager -> entorno)."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    bucket: str
    q_ingesta: str
    q_gpu: str
    q_validacion: str
    q_ingesta_dlq: str
    q_gpu_dlq: str
    dsn: str
    visibilidad_s: int = 120
    max_bytes: int = 25 * 1024 * 1024

    @classmethod
    def de_entorno(cls) -> "Config":
        nombres = {"bucket": "INGESTA_BUCKET", "q_ingesta": "INGESTA_Q_INGESTA", "q_gpu": "INGESTA_Q_GPU",
                   "q_validacion": "INGESTA_Q_VALIDACION", "q_ingesta_dlq": "INGESTA_Q_INGESTA_DLQ",
                   "q_gpu_dlq": "INGESTA_Q_GPU_DLQ", "dsn": "INGESTA_DSN"}
        faltan = [v for v in nombres.values() if not os.environ.get(v)]
        if faltan:
            raise SystemExit("faltan variables de entorno: " + ", ".join(faltan))
        return cls(**{k: os.environ[v] for k, v in nombres.items()},
                   visibilidad_s=int(os.environ.get("INGESTA_VISIBILIDAD_S", "120")),
                   max_bytes=int(os.environ.get("INGESTA_MAX_BYTES", str(25 * 1024 * 1024))))

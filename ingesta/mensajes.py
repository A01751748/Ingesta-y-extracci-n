"""Mensajes de SQS. Llevan referencias; nunca el archivo ni datos fiscales."""
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .contrato import SCHEMA_VERSION


class MensajeDocumento(BaseModel):
    """Entra a q-ingesta (lo manda el orquestador). q-gpu usa el mismo formato."""
    model_config = ConfigDict(extra="ignore")  # se pueden agregar campos sin romper
    documento_id: str = Field(min_length=1, max_length=200)
    lote_id: str = Field(min_length=1, max_length=200)
    bucket: str
    key: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern="^[0-9a-f]{64}$")
    intento: int = Field(1, ge=1, description="Sube en 1 solo si alguien pide reprocesar")


class MensajeResultado(BaseModel):
    """Sale a q-validacion: uno por documento terminado, también si terminó en error.
    La extracción se lee de `extracciones_json`."""
    model_config = ConfigDict(extra="ignore")
    documento_id: str
    lote_id: str
    intento: int
    schema_version: str = SCHEMA_VERSION
    estado_extraccion: Literal["ok", "error"]
    codigo_motivo: Optional[str] = None

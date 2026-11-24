"""Contrato 1 (propuesta v1): lo que el frente 4 entrega y el validador consume.

- Ausente es null; nunca "" ni 0. Aquí no se calculan valores faltantes.
- Importes, tasas y cantidades son strings decimales, para no perder centavos.
- bbox va normalizado 0-1, [x0, y0, x1, y1], origen arriba a la izquierda.
"""
from typing import Literal, Optional

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.1.0"
CRITICOS = ("uuid", "rfc_emisor", "rfc_receptor", "fecha_emision", "subtotal", "total")

Origen = Literal["xml", "qr", "cadena", "texto_pdf", "ocr", "modelo"]
Tipo = Literal["xml", "pdf_texto", "pdf_escaneado", "pdf_xml_embebido", "imagen", "desconocido"]
Ruta = Literal["xml", "modelo_texto", "modelo_ocr", "ninguna"]
Codigo = Literal["ILEGIBLE", "PDF_CIFRADO", "TIPO_NO_SOPORTADO", "XML_INVALIDO", "NO_ES_CFDI",
                 "VARIAS_FACTURAS", "PROCESAMIENTO_FALLIDO"]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _vacio_es_null(cls, d):
        """También en XML: Descuento="" es ausente."""
        if isinstance(d, dict):
            return {k: None if isinstance(v, str) and not v.strip() else v for k, v in d.items()}
        return d


class Advertencia(_Base):
    codigo: Literal["VALOR_NO_IMPRESO", "VALORES_EN_CONFLICTO", "POSIBLE_NOTA_DE_CREDITO"]
    campo: str = Field(description="p. ej. moneda, conceptos[0].descuento, traslados[1].importe")
    detalle: str
    valor_descartado: Optional[str] = Field(None, description="Lo que propuso el modelo; no usar como dato")


class Campo(_Base):
    valor: Optional[str] = None
    origen: Optional[Origen] = None
    confianza: float = Field(0.0, ge=0, le=1)
    pagina: Optional[int] = Field(None, ge=1)
    bbox: Optional[tuple[float, float, float, float]] = None
    evidencia: Optional[str] = Field(None, description="Por qué se asignó esa confianza")


class Impuesto(_Base):
    impuesto: Optional[str] = None
    tipo_factor: Optional[str] = None
    tasa_o_cuota: Optional[str] = None
    base: Optional[str] = None
    importe: Optional[str] = None
    origen: Optional[Origen] = None
    confianza: float = Field(0.0, ge=0, le=1)


class ImpuestoLocal(_Base):
    clase: Literal["traslado", "retencion"]
    nombre: Optional[str] = None
    tasa: Optional[str] = None
    importe: Optional[str] = None


class Relacionado(_Base):
    tipo_relacion: Optional[str] = None
    uuid: Optional[str] = None


class Concepto(_Base):
    cantidad: Optional[str] = None
    clave_prod_serv: Optional[str] = None
    clave_unidad: Optional[str] = None
    unidad: Optional[str] = None
    descripcion: Optional[str] = None
    objeto_imp: Optional[str] = None
    valor_unitario: Optional[str] = None
    importe: Optional[str] = None
    descuento: Optional[str] = None
    traslados: list[Impuesto] = []
    retenciones: list[Impuesto] = []
    origen: Optional[Origen] = None
    confianza: float = Field(0.0, ge=0, le=1)


class Campos(_Base):
    uuid: Campo = Campo()
    rfc_emisor: Campo = Campo()
    rfc_receptor: Campo = Campo()
    fecha_emision: Campo = Campo()
    subtotal: Campo = Campo()
    total: Campo = Campo()
    moneda: Campo = Campo()
    tipo_cambio: Campo = Campo()
    descuento: Campo = Campo()
    serie: Campo = Campo()
    folio: Campo = Campo()
    tipo_comprobante: Campo = Campo()
    nombre_emisor: Campo = Campo()
    nombre_receptor: Campo = Campo()
    regimen_emisor: Campo = Campo()
    regimen_receptor: Campo = Campo()
    domicilio_receptor: Campo = Campo()
    lugar_expedicion: Campo = Campo()
    uso_cfdi: Campo = Campo()
    metodo_pago: Campo = Campo()
    forma_pago: Campo = Campo()
    fecha_timbrado: Campo = Campo()
    sello_ultimos8: Campo = Campo()


class ErrorDoc(_Base):
    codigo: Codigo
    detalle: str
    reintentable: bool


class InfoModelo(_Base):
    nombre: str
    version_prompt: str


class Extraccion(_Base):
    schema_version: Literal["1.1.0"] = SCHEMA_VERSION
    documento_id: str
    lote_id: Optional[str] = Field(None, description="Siempre presente en el flujo por colas; null solo en la CLI")
    recibido_en: Optional[AwareDatetime] = Field(
        None, description="Cuándo llegó la factura, con zona. No cambia al reprocesar; null solo en la CLI")
    sha256: str
    tipo_detectado: Tipo = "desconocido"
    ruta: Ruta = "ninguna"
    version_cfdi: Optional[str] = None
    estado_extraccion: Literal["ok", "error"]
    error: Optional[ErrorDoc] = None
    paginas: Optional[int] = None
    modelo: Optional[InfoModelo] = None
    duracion_ms: int = 0
    advertencias: list[Advertencia] = []
    campos: Campos = Campos()
    conceptos: list[Concepto] = []
    traslados: list[Impuesto] = []
    retenciones: list[Impuesto] = []
    impuestos_locales: list[ImpuestoLocal] = []
    cfdi_relacionados: list[Relacionado] = []

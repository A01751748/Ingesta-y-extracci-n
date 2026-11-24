"""Regenera contratos/*.schema.json. Una prueba avisa si quedaron viejos."""
import json
from pathlib import Path

from .contrato import Extraccion
from .mensajes import MensajeDocumento, MensajeResultado

CARPETA = Path(__file__).parent.parent / "contratos"
ARCHIVOS = {"extraccion.v1.schema.json": Extraccion,
            "mensaje_documento.v1.schema.json": MensajeDocumento,
            "mensaje_resultado.v1.schema.json": MensajeResultado}


def generar(modelo) -> str:
    return json.dumps(modelo.model_json_schema(), ensure_ascii=False, indent=2) + "\n"


if __name__ == "__main__":
    for nombre, modelo in ARCHIVOS.items():
        (CARPETA / nombre).write_text(generar(modelo), encoding="utf-8")

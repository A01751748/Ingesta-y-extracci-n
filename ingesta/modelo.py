"""Interfaz del modelo. Ollama en desarrollo; vLLM entra con la misma forma."""
import base64
import io
import json
import math
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

from PIL import Image
from pydantic import BaseModel, ConfigDict, ValidationError

from .errores import ErrorTransitorio

VERSION_PROMPT = "v1"
PROMPT = (Path(__file__).parent / "prompts" / f"{VERSION_PROMPT}.md").read_text(encoding="utf-8")
T = Optional[str]


class _B(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImpuestoLeido(_B):
    impuesto: T
    tipo_factor: T
    tasa_o_cuota: T
    importe: T


class ConceptoLeido(_B):
    cantidad: T
    clave_prod_serv: T
    clave_unidad: T
    descripcion: T
    objeto_imp: T
    valor_unitario: T
    importe: T
    descuento: T


class Leido(_B):
    """Sin valores por defecto: el esquema obliga a decidir valor o null."""
    uuid: T
    rfc_emisor: T
    nombre_emisor: T
    regimen_emisor: T
    rfc_receptor: T
    nombre_receptor: T
    regimen_receptor: T
    domicilio_receptor: T
    uso_cfdi: T
    fecha_emision: T
    lugar_expedicion: T
    serie: T
    folio: T
    tipo_comprobante: T
    moneda: T
    tipo_cambio: T
    metodo_pago: T
    forma_pago: T
    conceptos: list[ConceptoLeido]
    subtotal: T
    descuento: T
    traslados: list[ImpuestoLeido]
    retenciones: list[ImpuestoLeido]
    total: T


@dataclass
class Respuesta:
    datos: Leido
    probabilidad: dict[str, float] = field(default_factory=dict)  # vacío si no hay logprobs


class Modelo(Protocol):
    nombre: str

    def extraer(self, texto: str, imagenes: list[Image.Image]) -> Respuesta: ...


def probabilidades(crudo: bytes, logprobs: list[dict]) -> dict[str, float]:
    """Menor probabilidad entre los tokens del valor de cada llave de primer nivel."""
    piezas = [bytes(t.get("bytes") or t["token"].encode()) for t in logprobs]
    if b"".join(piezas) != crudo:
        return {}  # no se pueden alinear: mejor sin señal que con una falsa
    rangos, pos = [], 0
    for p, t in zip(piezas, logprobs):
        rangos.append((pos, pos + len(p), t["logprob"]))
        pos += len(p)
    # Quita el contenido anidado conservando posiciones, para no confundir
    # conceptos[].descuento con el descuento de la factura.
    plano = re.sub(rb"\[.*?\]", lambda m: b" " * len(m[0]), crudo, flags=re.S)
    salida = {}
    for m in re.finditer(rb'"(\w+)"\s*:\s*"((?:[^"\\]|\\.)*)"', plano):
        lps = [lp for a, b, lp in rangos if a < m.end(2) and b > m.start(2)]
        if lps:
            salida[m[1].decode()] = math.exp(min(lps))
    return salida


def _b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _post(url: str, cuerpo: dict, timeout: int = 300) -> dict:
    cab = {"Content-Type": "application/json"}
    if clave := os.environ.get("INGESTA_VLLM_API_KEY"):
        cab["Authorization"] = f"Bearer {clave}"
    req = urllib.request.Request(url, json.dumps(cuerpo).encode(), cab)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise ErrorTransitorio("PROCESAMIENTO_FALLIDO", f"servidor de modelos: {e}"[:300]) from e


def _ollama(cuerpo: dict) -> dict:
    host = os.environ.get("OLLAMA_HOST") or "http://localhost:11434"
    return _post(("" if "://" in host else "http://") + host + "/api/chat", cuerpo)


class _Base:
    nombre: str

    def _pedir(self, mensajes: list[dict], imagenes: list[Image.Image]) -> tuple[str, list[dict]]:
        raise NotImplementedError

    def extraer(self, texto: str, imagenes: list[Image.Image]) -> Respuesta:
        # Sellos y cadenas en base64 gastan contexto y no se extraen con el modelo.
        limpio = "\n".join(l for l in texto.splitlines() if not re.search(r"[^\s]{60,}", l))
        base = [{"role": "user", "content": PROMPT + limpio}]
        mensajes, error = base, None
        for _ in range(2):
            crudo, logprobs = self._pedir(mensajes, imagenes)
            try:
                return Respuesta(Leido.model_validate_json(crudo),
                                 probabilidades(crudo.encode(), logprobs))
            except ValidationError as e:
                error = e
                mensajes = base + [
                    {"role": "assistant", "content": crudo},
                    {"role": "user", "content": f"JSON inválido: {str(e)[:500]}. Corrígelo."}]
        # Sin el contenido: el mensaje de Pydantic puede traer datos de la factura.
        raise ErrorTransitorio("PROCESAMIENTO_FALLIDO",
                               f"salida inválida del modelo ({error.error_count()} errores)")


class ModeloOllama(_Base):
    def __init__(self, nombre: Optional[str] = None):
        self.nombre = nombre or os.environ.get("INGESTA_MODELO") or "qwen3.5:2b"

    def _pedir(self, mensajes, imagenes):
        if imagenes:
            mensajes = [{**mensajes[0], "images": [_b64(i) for i in imagenes]}, *mensajes[1:]]
        r = _ollama({
            "model": self.nombre, "messages": mensajes, "stream": False, "think": False,
            "logprobs": True, "format": Leido.model_json_schema(),
            # El Modelfile de qwen3.5 trae presence_penalty 1.5: castiga dígitos repetidos.
            "options": {"temperature": 0, "presence_penalty": 0, "num_ctx": 16384,
                        "num_predict": 4096},
        })
        return r["message"]["content"], r.get("logprobs") or []


def _openai(modelo: str, mensajes: list, imagenes: list, **extra) -> dict:
    """API compatible con OpenAI: vLLM en AWS."""
    if imagenes:
        partes = [{"type": "text", "text": mensajes[0]["content"]}] + [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_b64(i)}"}}
            for i in imagenes]
        mensajes = [{"role": "user", "content": partes}, *mensajes[1:]]
    url = (os.environ.get("INGESTA_VLLM_URL") or "http://localhost:8000").rstrip("/")
    r = _post(url + "/v1/chat/completions", {
        "model": modelo, "messages": mensajes, "temperature": 0, "presence_penalty": 0,
        "max_tokens": 4096, **extra})
    try:
        eleccion = r["choices"][0]
        if not isinstance(eleccion["message"]["content"], str):
            raise TypeError("content vacío")
        return eleccion
    except (KeyError, IndexError, TypeError) as e:
        raise ErrorTransitorio("PROCESAMIENTO_FALLIDO", "respuesta inesperada del servidor") from e


class ModeloVLLM(_Base):
    def __init__(self, nombre: Optional[str] = None):
        self.nombre = nombre or os.environ["INGESTA_MODELO"]

    def _pedir(self, mensajes, imagenes):
        e = _openai(
            self.nombre, mensajes, imagenes, logprobs=True,
            chat_template_kwargs={"enable_thinking": False}, reasoning_effort="none",
            response_format={"type": "json_schema", "json_schema": {
                "name": "factura", "strict": True, "schema": Leido.model_json_schema()}})
        return e["message"]["content"], (e.get("logprobs") or {}).get("content") or []


def crear_modelo() -> Modelo:
    return ModeloVLLM() if os.environ.get("INGESTA_BACKEND") == "vllm" else ModeloOllama()


def ocr(img: Image.Image) -> str:
    if os.environ.get("INGESTA_BACKEND") == "vllm":
        return _openai(os.environ["INGESTA_OCR"], [{"role": "user", "content": "Text Recognition:"}],
                       [img])["message"]["content"].strip()
    return ocr_ollama(img)


def ocr_ollama(img: Image.Image) -> str:
    """glm-ocr da texto sin cajas; las cajas las pone Tesseract en cajas.py."""
    r = _ollama({
        "model": os.environ.get("INGESTA_OCR") or "glm-ocr:latest", "stream": False,
        "messages": [{"role": "user", "content": "Text Recognition:", "images": [_b64(img)]}],
        "options": {"temperature": 0, "num_ctx": 16384, "num_predict": 4096},
    })
    return r["message"]["content"].strip()

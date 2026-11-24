"""Datos leídos sin modelo: QR del SAT y cadena original del timbre."""
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import unquote

import zxingcpp
from PIL import Image

from .errores import ErrorPermanente
from .normaliza import RFC, UUID, decimal_str


@dataclass(frozen=True)
class Ancla:
    valor: str
    origen: str
    pagina: Optional[int] = None


def parsear_qr(crudo: str) -> dict[str, str]:
    """No se usa parse_qs: convierte '+' en espacio (sello) y corta RFC con '&'."""
    s = unquote(crudo)
    uuid = re.search(rf"[?&]id=({UUID})", s, re.I)
    re_, rr = re.search(rf"[?&]re=({RFC})", s, re.I), re.search(rf"[?&]rr=({RFC})", s, re.I)
    if not (uuid and re_ and rr):  # otros QR (Carta Porte, liga del emisor)
        return {}
    d = {"uuid": uuid[1].upper(), "rfc_emisor": re_[1].upper(), "rfc_receptor": rr[1].upper()}
    if tt := re.search(r"[?&]tt=([\d.]+)", s):
        if v := decimal_str(tt[1]):
            entero, _, dec = v.partition(".")
            d["total"] = f"{entero.lstrip('0') or '0'}.{dec.rstrip('0').ljust(2, '0')}"
    if fe := re.search(r"[?&]fe=([A-Za-z0-9+/=]{8})", s):
        d["sello_ultimos8"] = fe[1]
    return d


def parsear_cadena(texto: str) -> list[dict[str, str]]:
    """||1.1|UUID|FechaTimbrado|... El texto puede venir cortado en renglones."""
    junto = re.sub(r"\s+", "", texto)
    patron = rf"\|\|1\.[01]\|({UUID})\|(\d{{4}}-\d\d-\d\dT\d\d:\d\d:\d\d)\|"
    return [{"uuid": u.upper(), "fecha_timbrado": f} for u, f in re.findall(patron, junto)]


def leer(imagenes: list[Image.Image], texto: str) -> tuple[dict[str, Ancla], set[str]]:
    """Devuelve (anclas, campos en conflicto entre QR y cadena)."""
    anclas: dict[str, Ancla] = {}
    qrs = []
    for pagina, img in enumerate(imagenes, 1):
        for codigo in zxingcpp.read_barcodes(img):
            if d := parsear_qr(codigo.text):
                qrs.append((pagina, d))
    if len({d["uuid"] for _, d in qrs}) > 1:
        raise ErrorPermanente("VARIAS_FACTURAS", "hay QR de CFDI distintos en el archivo")
    for pagina, d in qrs:
        for k, v in d.items():
            anclas.setdefault(k, Ancla(v, "qr", pagina))

    cadenas = parsear_cadena(texto)
    if len({c["uuid"] for c in cadenas}) > 1:
        raise ErrorPermanente("VARIAS_FACTURAS", "hay cadenas originales de timbres distintos")
    conflictos = set()
    for c in cadenas[:1]:
        for k, v in c.items():
            if k in anclas and anclas[k].valor != v:
                conflictos.add(k)
            anclas.setdefault(k, Ancla(v, "cadena"))
    return anclas, conflictos

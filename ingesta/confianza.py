"""Cruza lo leído por el modelo con el texto y las anclas. Umbrales iniciales, sin calibrar."""
import re
from datetime import datetime
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Optional

from .anclas import Ancla
from .contrato import Campo
from .impresos import _NUM
from .normaliza import IMPORTES, RFC, UUID, decimal_str, iguales

# Opcionales numéricos: un "0" suelto siempre aparece en algún lado del documento.
NO_IMPRESO = "descartado: el documento no lo imprime"
SIN_RELLENO = {"uuid", "rfc_emisor", "rfc_receptor", "fecha_emision", "subtotal", "total", "descuento",
               "moneda", "tipo_cambio", "metodo_pago", "forma_pago", "uso_cfdi"}
EXIGE_ETIQUETA = {"tipo_cambio", "descuento"}
PROB_ALTA = 0.80
TOPE_SIN_LOGPROB = 0.89

ETIQUETAS = {
    "uuid": r"folio\s+fiscal",
    "fecha_emision": r"(expedici[oó]n|emisi[oó]n)",
    "subtotal": r"sub\s*-?\s*total",
    "total": r"(?<!sub)(?<!sub )(?<!sub-)\btotal\b",
    "descuento": r"descuento",
    "tipo_cambio": r"(tipo\s+de\s+cambio|\bt\.?\s?c\.)",
    "rfc_emisor": r"(emisor|rfc)",
    "rfc_receptor": r"(receptor|rfc)",
    "moneda": r"moneda",
}
# Un renglón con estas palabras no respalda al campo aunque contenga el valor.
EXCLUYE = {
    "fecha_emision": r"(timbrad|certificaci)",
    "rfc_emisor": r"(proveedor|certificaci|\bpac\b)",
    "rfc_receptor": r"(proveedor|certificaci|\bpac\b)",
    "uuid": r"(trip|viaje|relacionad|pedido)",
}
_FECHA = r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d"


def _ocurrencias(nombre: str, valor: str, linea: str) -> list[re.Match]:
    if nombre in IMPORTES:
        return [m for m in re.finditer(_NUM, linea)
                if (d := decimal_str(m[0])) is not None and Decimal(d) == Decimal(valor)]
    patron = re.escape(valor).replace("T", "[T ]", 1) if nombre.startswith("fecha") else re.escape(valor)
    return list(re.finditer(rf"(?<!\w){patron}(?!\w)", linea, re.I))


def _respaldo(nombre: str, valor: str, paginas: list[str]) -> tuple[Optional[int], bool]:
    """(página donde aparece, si alguna aparición tiene su etiqueta)."""
    primera = None
    etiqueta, excluye = ETIQUETAS.get(nombre), EXCLUYE.get(nombre)
    for n, texto in enumerate(paginas, 1):
        lineas = [l for l in texto.splitlines() if l.strip()]
        for i, linea in enumerate(lineas):
            for m in _ocurrencias(nombre, valor, linea):
                primera = primera or n
                if not etiqueta or (excluye and re.search(excluye, linea, re.I)):
                    continue
                antes = linea[:m.start()]
                if re.search(etiqueta, antes, re.I) or (
                        i and not antes.strip() and re.search(etiqueta, lineas[i - 1], re.I)):
                    return n, True
    return primera, False


def _rivales(nombre: str, valor: str, paginas: list[str]) -> bool:
    patron = {"uuid": UUID, "fecha_emision": _FECHA, "rfc_emisor": RFC, "rfc_receptor": RFC}.get(nombre)
    if not patron:
        return False
    vistos = {m.upper().replace(" ", "T") for t in paginas for m in re.findall(patron, t, re.I)}
    return bool(vistos - {valor.upper()})


def _formato_ok(nombre: str, v: str) -> bool:
    if nombre == "uuid":
        return bool(re.fullmatch(UUID, v))
    if nombre in ("rfc_emisor", "rfc_receptor"):
        return bool(re.fullmatch(RFC, v))
    if nombre == "fecha_emision":
        try:
            datetime.strptime(v, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return False
    return True


def buscar_bbox(valor: str, palabras: list) -> Optional[tuple]:
    for x0, y0, x1, y1, s in palabras:
        limpio = s.strip("$:;,()")
        if limpio.casefold() == valor.casefold() or (
                "." in valor and (d := decimal_str(limpio)) and Decimal(d) == Decimal(valor)
                if decimal_str(valor) else False):
            return tuple(min(1.0, max(0.0, round(c, 5))) for c in (x0, y0, x1, y1))
    # En escaneos Tesseract puede fallar un carácter. Solo UUID y RFC: en fechas e importes
    # un carácter distinto es otro dato.
    if re.fullmatch(f"{UUID}|{RFC}", valor):
        mejor = max(((SequenceMatcher(None, valor.upper(), p[4].strip("$:;,()").upper()).ratio(), p)
                     for p in palabras), key=lambda t: t[0], default=(0, None))
        if mejor[0] >= 0.85:
            return tuple(min(1.0, max(0.0, round(c, 5))) for c in mejor[1][:4])
    return None


def evaluar(nombre: str, valor: Optional[str], prob: Optional[float], paginas: list[str],
            ancla: Optional[Ancla], conflicto: bool, origen_texto: str) -> Campo:
    if conflicto:
        return Campo(valor=ancla.valor, origen=ancla.origen, confianza=0.0, pagina=ancla.pagina,
                     evidencia="el QR y la cadena original se contradicen")
    if ancla:
        pagina, _ = _respaldo(nombre, ancla.valor, paginas)
        if valor is not None and iguales(valor, ancla.valor):
            nota = f"coincide con {ancla.origen}"
        elif pagina:  # el documento imprime lo mismo que el ancla: se equivocó el modelo
            nota = f"{ancla.origen} y texto coinciden; el modelo leyó {valor!r}"
        elif valor is None:
            return Campo(valor=ancla.valor, origen=ancla.origen, confianza=0.85, pagina=ancla.pagina,
                         evidencia=f"solo en {ancla.origen}; no se encontró en el texto")
        else:
            return Campo(valor=valor, origen="modelo", confianza=0.0,
                         evidencia=f"contradice {ancla.origen}: {ancla.valor!r}")
        return Campo(valor=ancla.valor, origen=ancla.origen, confianza=0.99,
                     pagina=pagina or ancla.pagina, evidencia=nota)
    if valor is None:
        return Campo()
    if not _formato_ok(nombre, valor):
        return Campo(valor=valor, origen="modelo", confianza=0.30, evidencia="formato inválido")
    pagina, etiquetado = _respaldo(nombre, valor, paginas)
    if pagina is None and nombre in SIN_RELLENO:
        return Campo(evidencia=NO_IMPRESO)
    if pagina is None:
        return Campo(valor=valor, origen="modelo", confianza=0.30, evidencia="no aparece en el texto")
    base = dict(valor=valor, origen=origen_texto, pagina=pagina)
    if not etiquetado and nombre in EXIGE_ETIQUETA:
        return Campo(evidencia=NO_IMPRESO)
    if not etiquetado:
        rival = _rivales(nombre, valor, paginas)
        return Campo(**base, confianza=0.60 if rival else 0.80,
                     evidencia="aparece sin su etiqueta" + (" y hay candidatos rivales" if rival else ""))
    if prob is None:
        return Campo(**base, confianza=TOPE_SIN_LOGPROB, evidencia="con etiqueta; sin logprobs")
    if prob < PROB_ALTA:
        return Campo(**base, confianza=0.80, evidencia=f"con etiqueta; probabilidad {prob:.2f}")
    return Campo(**base, confianza=0.95, evidencia=f"con etiqueta; probabilidad {prob:.2f}")

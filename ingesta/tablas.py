"""Conceptos desde la geometría de la tabla del PDF, sin modelo."""
import re
import unicodedata
from decimal import Decimal
from typing import Optional

import pymupdf

from .contrato import Concepto, Impuesto
from .normaliza import decimal_str
from .normaliza import texto as nfkc

if hasattr(pymupdf, "no_recommend_layout"):
    pymupdf.no_recommend_layout()  # si no, imprime publicidad en stdout y rompe el JSON

# Orden: lo específico antes que lo general ("clave unidad" antes que "unidad").
_ENCABEZADOS = [
    ("clave_prod_serv", r"clave.*(prod|serv)|prod.*serv|clave sat"),
    ("clave_unidad", r"(clave|cve).*unidad"),
    ("objeto_imp", r"objeto"),
    ("valor_unitario", r"(valor|v\.?|precio|p\.?) ?unit|precio"),
    ("descuento", r"descuento|desc\.$"),
    ("cantidad", r"cantidad|cant\b"),
    ("impuestos", r"^impuestos?$"),
    ("importe", r"^importe"),
    ("descripcion", r"descripcion|concepto"),
    ("unidad", r"unidad|^u\.?m\.?$"),
]


def _plano(s) -> str:
    s = unicodedata.normalize("NFD", nfkc(s or ""))
    return " ".join("".join(c for c in s if not unicodedata.combining(c)).lower().split())


def _columnas(fila: list) -> dict[str, int]:
    col = {}
    for i, celda in enumerate(fila):
        for nombre, patron in _ENCABEZADOS:
            if celda and re.search(patron, _plano(celda)):
                col.setdefault(nombre, i)
                break
    return col


def _una(s: Optional[str]) -> Optional[str]:
    return " ".join(nfkc(s).split()) or None if s else None


def impuestos_de(s: Optional[str]) -> tuple[list[Impuesto], list[Impuesto]]:
    """'002-IVA 16.00%\\n$ 1.80' -> traslado. Sin dato impreso queda null."""
    traslados, retenciones = [], []
    plano = _una(s) or ""
    # La clase rotulada (Trasladado/Retenido) vale hasta que aparece otro rótulo.
    for m in re.finditer(
            r"\b(00[123])\s*-\s*(?:IVA|ISR|IEPS)\b\s*(?:([\d.]+)\s*%)?\s*(?:\$\s*([\d,]+\.\d+))?", plano, re.I):
        rotulos = re.findall(r"traslad|reten", plano[:m.start()], re.I)
        resto = plano[m.end():m.end() + 40]
        factor = "Exento" if re.match(r".{0,20}exento", resto, re.I) else "Tasa" if m[2] else None
        tasa = format((Decimal(m[2]) / 100).normalize(), "f") if m[2] else None
        imp = Impuesto(impuesto=m[1], tipo_factor=factor, tasa_o_cuota=tasa,
                       importe=decimal_str(m[3]), origen="texto_pdf", confianza=0.80)
        retenido = rotulos[-1].lower() == "reten" if rotulos else m[1] == "001"  # ISR solo se retiene
        (retenciones if retenido else traslados).append(imp)
    return traslados, retenciones


def _fila(v: dict) -> Optional[Concepto]:
    importe = decimal_str(v.get("importe"))
    desc = nfkc(v.get("descripcion") or "").strip()
    if importe is None or not desc:
        return None
    # Algunos emisores meten los impuestos dentro de la descripción.
    partes = re.split(r"(?im)^\s*(?=impuesto\s+(?:traslad|reten))", desc, maxsplit=1)
    desc, imp_en_desc = partes[0], partes[1] if len(partes) > 1 else ""
    clave = _una(v.get("clave_prod_serv"))
    if not clave and (m := re.match(r"(\d{8})\s*[-–]\s*", desc)):
        clave, desc = m[1], desc[m.end():]
    unidad, cve = _una(v.get("unidad")), _una(v.get("clave_unidad"))
    if unidad and not cve and (m := re.match(r"([A-Z0-9]{2,3})\s*[-–]\s*(.+)", unidad)):
        cve, unidad = m[1], m[2]
    objeto = re.match(r"0[1-8]\b", _una(v.get("objeto_imp")) or "")
    tras, ret = impuestos_de("\n".join(filter(None, (v.get("impuestos"), imp_en_desc))))
    return Concepto(
        cantidad=decimal_str(v.get("cantidad")), importe=importe, descripcion=_una(desc),
        clave_prod_serv=clave if clave and re.fullmatch(r"\d{8}", clave) else None,
        clave_unidad=cve if cve and re.fullmatch(r"[A-Z0-9]{2,3}", cve) else None, unidad=unidad,
        objeto_imp=objeto[0] if objeto else None, valor_unitario=decimal_str(v.get("valor_unitario")),
        descuento=decimal_str(v.get("descuento")), traslados=tras, retenciones=ret,
        origen="texto_pdf")


def _cantidad_fuera(palabras, tabla, enc, fila) -> Optional[str]:
    """PyMuPDF a veces deja fuera la primera columna si no tiene borde."""
    rotulo = [w for w in palabras if w[2] <= tabla.bbox[0] and enc.bbox[1] <= (w[1] + w[3]) / 2 <= enc.bbox[3]
              and re.fullmatch(r"cant(idad)?\.?", _plano(w[4]))]
    if not rotulo:
        return None
    x0, x1 = rotulo[0][0], rotulo[0][2]
    hallado = [w[4] for w in palabras if w[0] < x1 and w[2] > x0 and w[2] <= tabla.bbox[0]
               and fila.bbox[1] <= (w[1] + w[3]) / 2 <= fila.bbox[3]]
    return hallado[0] if len(hallado) == 1 else None


def conceptos(pagina) -> list[Concepto]:
    salida: list[Concepto] = []
    palabras = pagina.get_text("words")
    for tabla in pagina.find_tables().tables:
        filas = tabla.extract()
        i = next((i for i, f in enumerate(filas)
                  if {"descripcion", "importe"} <= (c := _columnas(f)).keys() and len(c) >= 4), None)
        if i is None:
            continue
        col, enc = _columnas(filas[i]), tabla.rows[i]
        for fila, geo in zip(filas[i + 1:], tabla.rows[i + 1:]):
            v = {n: fila[j] for n, j in col.items()}
            if "cantidad" not in col:
                v["cantidad"] = _cantidad_fuera(palabras, tabla, enc, geo)
            c = _fila(v)
            if c is not None:
                salida.append(c)
            elif salida and (extra := _una(v.get("descripcion"))) and not any(
                    _una(x) for n, x in v.items() if n != "descripcion"):
                salida[-1].descripcion = f"{salida[-1].descripcion} {extra}"
            elif any(_una(x) for x in fila):
                break  # renglón de totales: terminó la tabla de conceptos
    return salida


def calificar(lista: list[Concepto], subtotal: Optional[str], alta: float, baja: float) -> None:
    """La aritmética es señal de lectura; no corrige valores (eso es del validador)."""
    def d(v):
        return Decimal(v) if v is not None else None
    cuadra = bool(lista) and subtotal is not None and all(c.importe for c in lista) and \
        abs(sum(Decimal(c.importe) for c in lista) - Decimal(subtotal)) <= Decimal("0.01")
    for c in lista:
        cant, unit, imp = d(c.cantidad), d(c.valor_unitario), d(c.importe)
        renglon = None not in (cant, unit, imp) and abs(cant * unit - imp) <= Decimal("0.01")
        c.confianza = alta if cuadra and renglon else baja

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Optional

UUID = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
RFC = r"[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}"
IMPORTES = {"subtotal", "total", "descuento", "tipo_cambio"}
CODIGOS = {"tipo_comprobante", "moneda", "uso_cfdi", "metodo_pago", "forma_pago",
           "regimen_emisor", "regimen_receptor", "lugar_expedicion"}
TIPOS = {"INGRESO": "I", "EGRESO": "E", "TRASLADO": "T", "PAGO": "P", "NOMINA": "N", "NÓMINA": "N"}
FORMAS = {"tipo_comprobante": "[IETPN]", "moneda": "[A-Z]{3}", "uso_cfdi": r"[A-Z]{1,2}\d{2}",
          "metodo_pago": "PUE|PPD", "forma_pago": r"\d{2}", "regimen_emisor": r"6\d{2}",
          "regimen_receptor": r"6\d{2}", "lugar_expedicion": r"\d{5}"}


def texto(t: str) -> str:
    """NFKC deshace ligaduras de PDF (b3ﬀd92 -> b3ffd92), que romperían un UUID."""
    return _FECHA.sub(_iso, unicodedata.normalize("NFKC", t))


# 2026/07/28 05:53:36 p. m.  |  28/07/2026 17:53:36  (en México, día primero)
_FECHA = re.compile(r"(?<!\d)(?:(\d{4})[/-](\d\d)[/-](\d\d)|(\d\d)[/-](\d\d)[/-](\d{4}))"
                    r"[ T]+(\d{1,2}):(\d\d):(\d\d)(?!\d)(?:\s*([ap])\s?\.?\s?m\b\.?)?", re.I)


def _iso(m: re.Match) -> str:
    a, me, d, d2, me2, a2, h, mi, se, ap = m.groups()
    a, me, d, h = a or a2, me or me2, d or d2, int(h)
    if int(me) > 12 or h > (12 if ap else 23):
        return m[0]  # ambigua o imposible: no se adivina
    if ap:
        h = h % 12 + (12 if ap.lower() == "p" else 0)
    return f"{a}-{me}-{d}T{h:02}:{mi}:{se}"


def decimal_str(v) -> Optional[str]:
    """'$ 1,370.00' -> '1370.00'. Devuelve None si no es un número inequívoco."""
    if v is None:
        return None
    s = re.sub(r"(?i)mxn|usd|eur|[\s$%]", "", str(v))
    if not re.fullmatch(r"-?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?", s):
        return None
    try:
        return format(Decimal(s.replace(",", "")), "f")
    except InvalidOperation:
        return None


def iguales(a: Optional[str], b: Optional[str]) -> bool:
    if a is None or b is None:
        return a is b
    da, db = decimal_str(a), decimal_str(b)
    if da is not None and db is not None and ("." in a or "." in b):
        return Decimal(da) == Decimal(db)
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def campo(nombre: str, v) -> Optional[str]:
    if v is None:
        return None
    s = " ".join(texto(str(v)).split())
    if not s or s.casefold() in {"null", "none", "n/a"}:
        return None
    if nombre in IMPORTES:
        return decimal_str(s)
    if nombre in {"uuid"}:
        return s.upper()
    if nombre in {"rfc_emisor", "rfc_receptor"}:
        return s.upper().replace(" ", "").replace("-", "")
    if nombre in {"fecha_emision", "fecha_timbrado"}:
        return s.replace(" ", "T", 1)
    if nombre in CODIGOS:
        m = re.match(r"([A-Za-z0-9]+)\s*[-–:]\s", s)
        cod = (m.group(1) if m else s).upper()
        cod = TIPOS.get(cod, cod) if nombre == "tipo_comprobante" else cod
        return cod if re.fullmatch(FORMAS.get(nombre, ".*"), cod) else None
    if nombre in {"serie", "folio"} and (len(s) > 40 or re.fullmatch(rf"\d{{20}}|{UUID}", s)):
        return None  # 20 dígitos es un número de certificado; un UUID es el folio fiscal
    return s

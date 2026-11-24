"""Nada entra al JSON solo porque el modelo lo dijo: debe estar impreso en el documento."""
import re
from decimal import Decimal
from typing import Optional

from .contrato import Advertencia, Campo, Impuesto
from .normaliza import TIPOS, decimal_str

_NUM = r"(?<![\w.,-])-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![\w]|[.,]\d)"
_TIPO = (r"(?i:(?:tipo|efecto)\s+del?\s+comprobante)\s*:?\s*"
         r"([IETPN](?![\w])|(?i:ingreso|egreso|traslado|pago|n[oó]mina)\b)")
_NOTA = r"nota\s+de\s+cr[eé]dito"


def numero(valor: Optional[str], texto: str) -> bool:
    return valor is not None and any(
        (d := decimal_str(m)) is not None and Decimal(d) == Decimal(valor)
        for m in re.findall(_NUM, texto))


def palabra(valor: Optional[str], texto: str) -> bool:
    return bool(valor) and re.search(rf"(?<!\w){re.escape(valor)}(?!\w)", texto, re.I) is not None


def tipo_comprobante(leido: Optional[str], paginas: list[str], origen: str) -> tuple[Campo, list[Advertencia]]:
    """Solo del rótulo impreso: confundir E con I sumaría una nota de crédito."""
    av = []
    vistos = {}
    for n, t in enumerate(paginas, 1):
        for m in re.finditer(_TIPO, t):
            vistos.setdefault(TIPOS.get(m[1].upper(), m[1].upper()), n)

    def avisar(codigo, detalle):
        av.append(Advertencia(codigo=codigo, campo="tipo_comprobante", detalle=detalle,
                              valor_descartado=leido if leido not in vistos or len(vistos) > 1 else None))
    if len(vistos) != 1:
        if vistos:
            avisar("VALORES_EN_CONFLICTO", "hay más de un tipo de comprobante impreso")
        elif leido:
            avisar("VALOR_NO_IMPRESO", "el documento no imprime el tipo de comprobante")
        return Campo(), av
    valor, pagina = next(iter(vistos.items()))
    campo = Campo(valor=valor, origen=origen, confianza=0.95, pagina=pagina, evidencia="rótulo impreso")
    if leido and leido != valor:
        avisar("VALORES_EN_CONFLICTO", f"el rótulo impreso dice {valor} y el modelo leyó otro")
        campo.confianza = 0.80
    if valor == "I" and any(re.search(_NOTA, t, re.I) for t in paginas):
        avisar("POSIBLE_NOTA_DE_CREDITO", "dice I, pero el documento menciona nota de crédito")
        campo.confianza = 0.50
    return campo, av


def limpiar_concepto(d: dict, texto: str) -> list[str]:
    """Pone null en lo no impreso y devuelve qué quitó."""
    fuera = []
    for k in ("cantidad", "valor_unitario", "importe", "descuento"):
        # Un descuento 0 por renglón no se puede respaldar sin tabla: el "0.00" impreso
        # suele ser el descuento global, y el modelo lo copia a cada concepto.
        sin_respaldo = k == "descuento" and d[k] is not None and (
            Decimal(d[k]) == 0 or not re.search("descuento", texto, re.I))
        if d[k] is not None and (sin_respaldo or not numero(d[k], texto)):
            fuera.append(k)
            d[k] = None
    return fuera


def limpiar_impuesto(i: Impuesto, texto: str) -> list[str]:
    fuera = []
    nombre = {"001": "ISR", "002": "IVA", "003": "IEPS"}.get(i.impuesto or "", "")
    pct = format((Decimal(i.tasa_o_cuota) * 100), "f") if i.tasa_o_cuota else None
    ok = {"impuesto": palabra(i.impuesto, texto) or palabra(nombre, texto),
          "tipo_factor": palabra(i.tipo_factor, texto) or bool(i.tipo_factor == "Tasa" and "%" in texto),
          "tasa_o_cuota": numero(i.tasa_o_cuota, texto) or numero(pct, texto.replace("%", " ")),
          "importe": numero(i.importe, texto)}
    for k, impreso in ok.items():
        if getattr(i, k) is not None and not impreso:
            fuera.append(k)
            setattr(i, k, None)
    return fuera

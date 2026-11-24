"""Núcleo: bytes -> Extraccion. No conoce AWS ni el sistema de archivos."""
import hashlib
import re
import time
from datetime import datetime
from decimal import Decimal
from typing import Callable, Optional

from . import anclas as mod_anclas
from . import cajas, cfdi_xml, impresos, pdf, tablas
from .confianza import NO_IMPRESO, buscar_bbox, evaluar
from .contrato import Advertencia, Campos, Concepto, ErrorDoc, Extraccion, Impuesto, InfoModelo
from .detector import detectar
from .errores import ErrorExtraccion, ErrorPermanente
from .modelo import VERSION_PROMPT, Modelo, crear_modelo
from .modelo import ocr as ocr_por_defecto
from .normaliza import campo as normalizar
from .normaliza import decimal_str
from .normaliza import texto as normalizar_texto

_FORMAS = {"clave_prod_serv": r"\d{8}", "clave_unidad": r"[A-Z0-9]{2,3}", "objeto_imp": r"0[1-8]"}
_CONF_LISTA = 0.60  # conceptos e impuestos del modelo: sin señal por campo todavía



class NecesitaModelo(Exception):
    """El documento no se resuelve sin modelo: va a la cola de GPU."""


def _no_impreso(campo: str, valor: Optional[str]) -> Advertencia:
    return Advertencia(codigo="VALOR_NO_IMPRESO", campo=campo, valor_descartado=valor,
                       detalle="lo propuso el modelo y no se encontró en el documento")


def _concepto(c, texto: str, donde: str, av: list) -> Concepto:
    """Códigos con forma imposible o que no están impresos se descartan (null)."""
    d = c.model_dump()
    for k, forma in _FORMAS.items():
        v = d[k].strip().upper() if d[k] else ""
        impreso = re.search(rf"(?<!\w){re.escape(v)}(?!\w)", texto, re.I) if v else None
        d[k] = v if impreso and re.fullmatch(forma, v) else None
    for k in ("cantidad", "valor_unitario", "importe", "descuento"):
        d[k] = decimal_str(d[k])
    original = dict(d)
    av += [_no_impreso(f"{donde}.{k}", original[k]) for k in impresos.limpiar_concepto(d, texto)]
    d = {k: (v.strip() or None) if isinstance(v, str) else v for k, v in d.items()}
    return Concepto(**d, origen="modelo", confianza=_CONF_LISTA)


def _impuesto(i, texto: str, donde: str, av: list) -> Optional[Impuesto]:
    d = i.model_dump()
    d["importe"] = decimal_str(d["importe"])
    # "16.00%" -> "0.16", la misma unidad que TasaOCuota del XML
    tasa = d["tasa_o_cuota"]
    d["tasa_o_cuota"] = decimal_str(tasa)
    if tasa and "%" in tasa and d["tasa_o_cuota"]:
        d["tasa_o_cuota"] = format((Decimal(d["tasa_o_cuota"]) / 100).normalize(), "f")
    imp = Impuesto(**d, origen="modelo", confianza=_CONF_LISTA)
    original = imp.model_copy()
    av += [_no_impreso(f"{donde}.{k}", getattr(original, k)) for k in impresos.limpiar_impuesto(imp, texto)]
    # Sin importe, tasa ni factor no queda impuesto que reportar.
    return imp if imp.importe or imp.tasa_o_cuota or imp.tipo_factor else None


def _con_modelo(doc: pdf.Documento, modelo: Modelo, ocr: Callable) -> dict:
    a_ocr = [i for i, t in enumerate(doc.textos) if not t]
    textos = list(doc.textos)
    for i in a_ocr:
        textos[i] = normalizar_texto(ocr(doc.imagenes[i]))
        doc.palabras[i] = cajas.palabras(doc.imagenes[i])
    todo = "\n".join(textos)
    anclas, conflictos = mod_anclas.leer(doc.imagenes, todo)
    if not todo.strip():
        raise ErrorPermanente("ILEGIBLE", "no se encontró texto en el documento")
    r = modelo.extraer(todo, [doc.imagenes[i] for i in a_ocr])
    leido = r.datos.model_dump()

    campos, av = {}, []
    for n in Campos.model_fields:
        valor = normalizar(n, leido.get(n))
        if n == "tipo_comprobante":
            c, nuevas = impresos.tipo_comprobante(valor, textos, "ocr" if a_ocr else "texto_pdf")
            av += nuevas
            campos[n] = c
            continue
        c = evaluar(n, valor, r.probabilidad.get(n), textos, anclas.get(n), n in conflictos,
                    "ocr" if a_ocr else "texto_pdf")
        if valor is not None and (c.evidencia == NO_IMPRESO or c.evidencia == "no aparece en el texto"):
            av.append(_no_impreso(n, valor))
        if c.valor and c.pagina:
            c.bbox = buscar_bbox(c.valor, doc.palabras[c.pagina - 1])
        campos[n] = c
    # Con páginas escaneadas la tabla puede estar incompleta: ahí manda el modelo.
    if doc.conceptos and not a_ocr:
        conceptos = doc.conceptos
        tablas.calificar(conceptos, campos["subtotal"].valor, alta=0.95, baja=0.50)
    else:
        conceptos = [_concepto(c, todo, f"conceptos[{i}]", av) for i, c in enumerate(r.datos.conceptos)]
        tablas.calificar(conceptos, campos["subtotal"].valor, alta=0.80, baja=0.40)
    # Índices: los de la lista del modelo, antes de quitar impuestos vacíos.
    impuestos = {g: [x for i, t in enumerate(getattr(r.datos, g))
                     if (x := _impuesto(t, todo, f"{g}_leidos[{i}]", av))]
                 for g in ("traslados", "retenciones")}
    m = re.search(r"\b(?:CFDI\s*)?[Vv]ersi[oó]n\s*:?\s*(3\.3|4\.0)\b", todo)
    return dict(
        campos=Campos(**campos), conceptos=conceptos,
        advertencias=av, **impuestos,
        version_cfdi=m[1] if m else None, ruta="modelo_ocr" if a_ocr else "modelo_texto",
        modelo=InfoModelo(nombre=modelo.nombre, version_prompt=VERSION_PROMPT))


def extraer(datos: bytes, documento_id: str, lote_id: Optional[str] = None,
            modelo: Optional[Modelo] = None, ocr: Callable = ocr_por_defecto,
            permitir_modelo: bool = True,
            recibido_en: Optional[datetime] = None) -> Extraccion:
    inicio = time.monotonic()
    sobre = dict(documento_id=documento_id, lote_id=lote_id, recibido_en=recibido_en,
                 sha256=hashlib.sha256(datos).hexdigest())
    extra: dict = {}
    estado, error = "ok", None
    try:
        tipo = detectar(datos)
        if tipo == "desconocido":
            raise ErrorPermanente("TIPO_NO_SOPORTADO", "no es XML, PDF, PNG, JPG ni TIFF")
        if tipo == "xml":
            extra["tipo_detectado"] = "xml"
            extra.update(cfdi_xml.parsear(datos), ruta="xml")
        else:
            doc = pdf.abrir_pdf(datos) if tipo == "pdf" else pdf.abrir_imagen(datos)
            extra["paginas"] = len(doc.textos)
            embebido = None
            for x in doc.xml_embebidos:
                try:
                    embebido = cfdi_xml.parsear(x)
                    break
                except ErrorPermanente:
                    continue  # adjunto que no es CFDI
            if embebido:
                extra.update(embebido, tipo_detectado="pdf_xml_embebido", ruta="xml")
            else:
                extra["tipo_detectado"] = ("imagen" if tipo == "imagen" else
                                           "pdf_texto" if all(doc.textos) else "pdf_escaneado")
                if not permitir_modelo:
                    raise NecesitaModelo
                extra.update(_con_modelo(doc, modelo or crear_modelo(), ocr))
    except NecesitaModelo:
        raise
    except ErrorExtraccion as e:
        estado = "error"
        error = ErrorDoc(codigo=e.codigo, detalle=e.detalle, reintentable=e.reintentable)
    except Exception as e:
        # Solo el tipo: el mensaje puede traer datos de la factura.
        estado = "error"
        error = ErrorDoc(codigo="PROCESAMIENTO_FALLIDO", reintentable=True,
                         detalle=f"error inesperado: {type(e).__name__}")
    return Extraccion(**sobre, **extra, estado_extraccion=estado, error=error,
                      duracion_ms=int((time.monotonic() - inicio) * 1000))


def extraer_sin_modelo(datos: bytes, documento_id: str,
                       lote_id: Optional[str] = None,
                       recibido_en: Optional[datetime] = None) -> Optional[Extraccion]:
    """Etapa CPU. None = hay que mandarlo a la cola de GPU."""
    try:
        return extraer(datos, documento_id, lote_id, permitir_modelo=False, recibido_en=recibido_en)
    except NecesitaModelo:
        return None

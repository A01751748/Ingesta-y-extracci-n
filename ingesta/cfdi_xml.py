"""Parser determinista de CFDI 3.3 y 4.0. Sin modelo, confianza 1.0."""
from xml.etree.ElementTree import ParseError

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

from .contrato import Campo, Campos, Concepto, Extraccion, Impuesto, ImpuestoLocal, Relacionado
from .errores import ErrorPermanente
from .normaliza import UUID

NS = {"http://www.sat.gob.mx/cfd/3": "3.3", "http://www.sat.gob.mx/cfd/4": "4.0"}
TFD = "{http://www.sat.gob.mx/TimbreFiscalDigital}TimbreFiscalDigital"
LOC = "{http://www.sat.gob.mx/implocal}"


def _campo(v) -> Campo:
    v = v.strip() if v else None
    return Campo(valor=v, origen="xml", confianza=1.0) if v else Campo()


def _impuestos(nodo, q) -> dict:
    """Solo el hijo directo: los de cada concepto no se mezclan con los globales."""
    salida = {"traslados": [], "retenciones": []}
    raiz = nodo.find(q + "Impuestos")
    if raiz is None:
        return salida
    for grupo, hoja, clave in (("Traslados", "Traslado", "traslados"),
                               ("Retenciones", "Retencion", "retenciones")):
        for n in raiz.findall(f"{q}{grupo}/{q}{hoja}"):
            salida[clave].append(Impuesto(
                impuesto=n.get("Impuesto"), tipo_factor=n.get("TipoFactor"),
                tasa_o_cuota=n.get("TasaOCuota"), base=n.get("Base"), importe=n.get("Importe"),
                origen="xml", confianza=1.0))
    return salida


def parsear(datos: bytes) -> dict:
    """Devuelve los campos de `Extraccion` que salen del XML."""
    try:
        r = ET.fromstring(datos, forbid_dtd=True)
    except (ParseError, DefusedXmlException, ValueError, LookupError) as e:
        raise ErrorPermanente("XML_INVALIDO", f"{type(e).__name__}: {e}"[:300]) from e

    ns, _, nombre = r.tag.rpartition("}")
    ns = ns.lstrip("{")
    if nombre != "Comprobante" or ns not in NS:
        raise ErrorPermanente("NO_ES_CFDI", f"raíz <{nombre}> con espacio de nombres '{ns}'"[:300])
    version = r.get("Version")
    if version != NS[ns]:
        raise ErrorPermanente("XML_INVALIDO", f"versión de CFDI no soportada: {version}")

    q = "{" + ns + "}"

    def hijo(tag):
        n = r.find(q + tag)
        return n if n is not None else {}

    em, rec = hijo("Emisor"), hijo("Receptor")
    timbre = r.find(f"{q}Complemento/{TFD}")
    t = timbre if timbre is not None else {}
    uuid, sello = t.get("UUID"), r.get("Sello")

    campos = Campos(
        uuid=_campo(uuid.upper() if uuid else None),
        rfc_emisor=_campo(em.get("Rfc")), rfc_receptor=_campo(rec.get("Rfc")),
        fecha_emision=_campo(r.get("Fecha")),
        subtotal=_campo(r.get("SubTotal")), total=_campo(r.get("Total")),
        moneda=_campo(r.get("Moneda")), tipo_cambio=_campo(r.get("TipoCambio")),
        descuento=_campo(r.get("Descuento")),
        serie=_campo(r.get("Serie")), folio=_campo(r.get("Folio")),
        tipo_comprobante=_campo(r.get("TipoDeComprobante")),
        nombre_emisor=_campo(em.get("Nombre")), nombre_receptor=_campo(rec.get("Nombre")),
        regimen_emisor=_campo(em.get("RegimenFiscal")),
        regimen_receptor=_campo(rec.get("RegimenFiscalReceptor")),
        domicilio_receptor=_campo(rec.get("DomicilioFiscalReceptor")),
        lugar_expedicion=_campo(r.get("LugarExpedicion")), uso_cfdi=_campo(rec.get("UsoCFDI")),
        metodo_pago=_campo(r.get("MetodoPago")), forma_pago=_campo(r.get("FormaPago")),
        fecha_timbrado=_campo(t.get("FechaTimbrado")),
        sello_ultimos8=_campo(sello[-8:] if sello else None),
    )
    conceptos = [Concepto(
        cantidad=c.get("Cantidad"), clave_prod_serv=c.get("ClaveProdServ"),
        clave_unidad=c.get("ClaveUnidad"), unidad=c.get("Unidad"),
        descripcion=c.get("Descripcion"), objeto_imp=c.get("ObjetoImp"),
        valor_unitario=c.get("ValorUnitario"), importe=c.get("Importe"),
        descuento=c.get("Descuento"), origen="xml", confianza=1.0, **_impuestos(c, q),
    ) for c in r.findall(f"{q}Conceptos/{q}Concepto")]

    locales = [ImpuestoLocal(clase="retencion", nombre=n.get("ImpLocRetenido"),
                             tasa=n.get("TasadeRetencion"), importe=n.get("Importe"))
               for n in r.iter(LOC + "RetencionesLocales")]
    locales += [ImpuestoLocal(clase="traslado", nombre=n.get("ImpLocTrasladado"),
                              tasa=n.get("TasadeTraslado"), importe=n.get("Importe"))
                for n in r.iter(LOC + "TrasladosLocales")]
    relacionados = [Relacionado(tipo_relacion=g.get("TipoRelacion"), uuid=n.get("UUID"))
                    for g in r.findall(q + "CfdiRelacionados")
                    for n in g.findall(q + "CfdiRelacionado")]
    return dict(version_cfdi=version, campos=campos, conceptos=conceptos,
                impuestos_locales=locales, cfdi_relacionados=relacionados, **_impuestos(r, q))

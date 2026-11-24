import io
import json
from pathlib import Path

import jsonschema
import pymupdf
import pytest
from PIL import Image

from ingesta import anclas, modelo as mod
from ingesta.anclas import Ancla
from ingesta.confianza import evaluar
from ingesta.contrato import Extraccion
from ingesta.detector import detectar
from ingesta.errores import ErrorPermanente, ErrorTransitorio
from ingesta.exportar_contrato import ARCHIVOS, CARPETA, generar
from ingesta.modelo import Leido, Respuesta
from ingesta.normaliza import campo, decimal_str, texto
from ingesta.pipeline import extraer

FIX = Path(__file__).parent / "fixtures"
U1 = "ABCD0707-7C06-4408-AA09-D8E8C6BFF720"
TRIP = "12345678-A81E-4674-90B1-FA549B3FFD92"
OK = {"uuid": 0.99, "fecha_emision": 0.99, "subtotal": 0.99, "total": 0.99}


def leido(**k):
    base = {n: None for n in Leido.model_fields}
    return Leido(**{**base, "conceptos": [], "traslados": [], "retenciones": [], **k})


class Falso:
    nombre = "falso"

    def __init__(self, r=None, error=None):
        self.r, self.error, self.texto = r, error, None

    def extraer(self, texto, imagenes):
        self.texto, self.imagenes = texto, imagenes
        if self.error:
            raise self.error
        return self.r


def hacer_pdf(*paginas, **guardar):
    d = pymupdf.open()
    for t in paginas:
        d.new_page().insert_text((40, 60), t)
    return d.tobytes(**guardar)


FACTURA = (f"Fecha de Expedicion: 2017-12-04T12:23:37\nTRIP UUID: {TRIP}\nFolio Fiscal: {U1}\n"
           "Subtotal $ 99.31\nTotal $ 115.20\nFecha de timbrado: 2017-12-04T12:23:38")


# ---- XML
@pytest.mark.parametrize("version", ["3.3", "4.0"])
def test_xml(version):
    r = extraer((FIX / f"cfdi{version.replace('.', '')}.xml").read_bytes(), "d")
    assert (r.estado_extraccion, r.ruta, r.version_cfdi) == ("ok", "xml", version)
    c = r.campos
    assert c.uuid.valor == "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
    assert c.fecha_emision.valor == "2026-10-05T10:00:00"
    assert (c.total.valor, c.tipo_cambio.valor, c.sello_ultimos8.valor) == ("1060.00", "18.5000", "bcd+/1==")
    assert c.descuento.valor is None and c.uuid.confianza == 1.0
    assert len(r.traslados) == 1 and r.retenciones[0].importe == "100.00"
    assert len(r.conceptos[0].traslados) == 1
    assert r.impuestos_locales[0].nombre == "ISH" and r.cfdi_relacionados[0].tipo_relacion == "04"


@pytest.mark.parametrize("datos,codigo", [
    (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>', "XML_INVALIDO"),
    (b'<!DOCTYPE x [<!ENTITY a "a"><!ENTITY b "&a;&a;&a;">]><x>&b;</x>', "XML_INVALIDO"),
    (b"<html><body/></html>", "NO_ES_CFDI"),
    (b"<roto", "XML_INVALIDO"),
    (b"hola", "TIPO_NO_SOPORTADO"),
    (b"", "TIPO_NO_SOPORTADO"),
    (b"%PDF-1.4 basura", "ILEGIBLE"),
    (b"\x89PNG\r\n\x1a\nbasura", "ILEGIBLE"),
])
def test_entradas_malas(datos, codigo):
    r = extraer(datos, "d", modelo=Falso())
    assert (r.estado_extraccion, r.error.codigo, r.error.reintentable) == ("error", codigo, False)


def test_detector_ignora_extension():
    assert detectar(b"\xef\xbb\xbf\n <?xml?>") == "xml"
    assert detectar(hacer_pdf("x")) == "pdf"


# ---- PDF
def test_pdf_cifrado():
    d = hacer_pdf("x" * 80, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="a", owner_pw="b")
    assert extraer(d, "d", modelo=Falso()).error.codigo == "PDF_CIFRADO"


def test_pdf_con_xml_embebido_no_usa_modelo():
    d = pymupdf.open(); d.new_page()
    d.embfile_add("f.xml", (FIX / "cfdi40.xml").read_bytes(), filename="f.xml")
    r = extraer(d.tobytes(), "d", modelo=Falso(error=AssertionError()))
    assert r.error is None and (r.tipo_detectado, r.ruta) == ("pdf_xml_embebido", "xml")


def test_todas_las_paginas_llegan_al_modelo():
    m = Falso(Respuesta(leido()))
    r = extraer(hacer_pdf("primera " * 10, "segunda " * 10), "d", modelo=m)
    assert "primera" in m.texto and "segunda" in m.texto and r.paginas == 2 and m.imagenes == []


def test_pagina_sin_texto_va_a_ocr():
    m = Falso(Respuesta(leido(total="115.20"), OK))
    r = extraer(hacer_pdf(FACTURA, ""), "d", modelo=m, ocr=lambda i: "texto ocr")
    assert r.tipo_detectado == "pdf_escaneado" and len(m.imagenes) == 1 and "texto ocr" in m.texto


def test_imagen_blanca_ilegible():
    b = io.BytesIO(); Image.new("RGB", (50, 50), "white").save(b, "PNG")
    assert extraer(b.getvalue(), "d", modelo=Falso(), ocr=lambda i: "").error.codigo == "ILEGIBLE"


def test_modelo_caido_es_reintentable():
    r = extraer(hacer_pdf(FACTURA), "d", modelo=Falso(error=ErrorTransitorio("PROCESAMIENTO_FALLIDO", "x")))
    assert r.error.reintentable


def test_trip_uuid_va_a_revision_aun_sin_qr():
    r = extraer(hacer_pdf(FACTURA), "d", modelo=Falso(Respuesta(leido(uuid=TRIP), OK)))
    assert r.campos.uuid.confianza < 0.90


def test_folio_y_fecha_correctos():
    r = extraer(hacer_pdf(FACTURA), "d", modelo=Falso(Respuesta(
        leido(uuid=U1, fecha_emision="2017-12-04T12:23:37", subtotal="99.31", total="115.20"), OK)))
    for n in OK:
        assert getattr(r.campos, n).confianza >= 0.90, n
        assert getattr(r.campos, n).bbox or n == "x"


def test_fecha_de_timbrado_no_pasa():
    r = extraer(hacer_pdf(FACTURA), "d", modelo=Falso(Respuesta(
        leido(fecha_emision="2017-12-04T12:23:38"), OK)))
    assert r.campos.fecha_emision.confianza < 0.90


def test_tipo_cambio_inventado_se_descarta():
    r = extraer(hacer_pdf(FACTURA + "\nDescuento 0.00"), "d", modelo=Falso(Respuesta(leido(tipo_cambio="0"))))
    assert r.campos.tipo_cambio.valor is None


# ---- Anclas y confianza
def test_qr_33_y_40():
    d = anclas.parsear_qr(f"?re=XYZA800101AB1&rr=CCC800101CC1&tt=0000000115.200000&id={U1}")
    assert d == {"uuid": U1, "rfc_emisor": "XYZA800101AB1", "rfc_receptor": "CCC800101CC1", "total": "115.20"}
    d = anclas.parsear_qr(f"https://x/default.aspx?id={U1}&re=A%26B010101AAA&rr=CCC800101CC1&tt=0.0&fe=ab%2Bd/1==")
    assert (d["rfc_emisor"], d["total"], d["sello_ultimos8"]) == ("A&B010101AAA", "0.00", "ab+d/1==")
    assert anclas.parsear_qr("https://tienda.mx") == {}


def test_cadena_partida_en_renglones():
    assert anclas.parsear_cadena(f"||1.1|{U1[:20]}\n{U1[20:]}|2017-12-04T12:23:38|abc")[0]["uuid"] == U1


def test_dos_cadenas_son_varias_facturas():
    t = "".join(f"||1.1|{u}|2017-12-04T12:23:38|x" for u in (U1, TRIP))
    with pytest.raises(ErrorPermanente, match="VARIAS_FACTURAS"):
        anclas.leer([], t)


def test_ancla_corrige_al_modelo():
    c = evaluar("uuid", TRIP, 0.99, [FACTURA], Ancla(U1, "qr"), False, "texto_pdf")
    assert (c.valor, c.confianza) == (U1, 0.99)


def test_ancla_que_no_esta_impresa_fuerza_revision():
    c = evaluar("total", "115.20", 0.99, [FACTURA], Ancla("999.00", "qr"), False, "texto_pdf")
    assert c.confianza == 0.0


def test_conflicto_qr_cadena():
    assert evaluar("uuid", U1, 0.99, [FACTURA], Ancla(U1, "qr"), True, "texto_pdf").confianza == 0.0


def test_total_no_se_confunde_con_subtotal():
    c = evaluar("total", "99.31", 0.99, [FACTURA], None, False, "texto_pdf")
    assert c.confianza < 0.90


def test_sin_logprobs_va_a_revision():
    assert evaluar("total", "115.20", None, [FACTURA], None, False, "texto_pdf").confianza == 0.89


def test_probabilidades_no_mezclan_conceptos():
    toks = ['{"conceptos":[{"total":"', "1", '"}],"total":"', "9", '"}']
    lps = [{"token": t, "logprob": lp, "bytes": list(t.encode())} for t, lp in zip(toks, [0, -9, 0, -.1, 0])]
    p = mod.probabilidades("".join(toks).encode(), lps)
    assert p["total"] > 0.9


# ---- Normalización
@pytest.mark.parametrize("a,b", [("$ 1,370.00", "1370.00"), ("0.00", "0.00"), ("1.370,00", None),
                                 ("12,34", None), ("abc", None), (None, None), ("-5.5", "-5.5")])
def test_decimal(a, b):
    assert decimal_str(a) == b


def test_ligaduras_y_codigos():
    assert texto("b3ﬀd92") == "b3ffd92"
    assert campo("uso_cfdi", "G03 - Gastos") == "G03" and campo("folio", " ") is None


@pytest.mark.parametrize("a,b", [
    ("2026/07/28 05:53:36 p. m.", "2026-07-28T17:53:36"), ("28/07/2026 12:00:01 a.m.", "2026-07-28T00:00:01"),
    ("2017-12-04T12:23:37", "2017-12-04T12:23:37"), ("07/28/2026 10:00:00", "07/28/2026 10:00:00")])
def test_fechas(a, b):
    assert texto(a) == b


# ---- Contrato
def test_schema_publicado_al_dia():
    for nombre, modelo in ARCHIVOS.items():
        assert (CARPETA / nombre).read_text(encoding="utf-8") == generar(modelo), nombre


def test_salidas_cumplen_schema():
    s = json.loads(generar(Extraccion))
    for datos in ((FIX / "cfdi40.xml").read_bytes(), b"hola"):
        jsonschema.validate(json.loads(extraer(datos, "d").model_dump_json()), s)
    assert Extraccion.model_fields["schema_version"].default == "1.1.0"


# ---- Conceptos desde la tabla
def tabla_pdf(filas, anchos=(50, 80, 170, 70, 90, 70)):
    d = pymupdf.open(); p = d.new_page()
    p.insert_text((40, 40), f"Folio Fiscal: {U1}"); y = 60
    for f in filas:
        x = 30
        for t, w in zip(f, anchos):
            r = pymupdf.Rect(x, y, x + w, y + 36); p.draw_rect(r)
            p.insert_textbox(r + (3, 3, -3, -3), t, fontsize=8); x += w
        y += 36
    return d.tobytes()


ENC = ("Cantidad", "Unidad", "Descripción", "V. Unitario", "Impuestos", "Importe")
FILAS = [("2", "H87 - Pieza", "43211500 - Computadoras", "$ 1,000.00", "002-IVA 16.00%\n$ 320.00", "$ 2,000.00"),
         ("1", "E48 - Servicio", "84111506 - Asesoria", "$ 500.00", "001-ISR 10.00%\n$ 50.00", "$ 500.00")]
TOT = ("", "", "", "", "Subtotal", "$ 2,500.00")


def con_tabla(filas, subtotal="2500.00"):
    return extraer(tabla_pdf(filas), "d", modelo=Falso(Respuesta(leido(subtotal=subtotal), OK))).conceptos


def test_tabla_de_conceptos():
    a, b = con_tabla([ENC, *FILAS, TOT])
    assert (a.cantidad, a.clave_prod_serv, a.clave_unidad, a.unidad, a.descripcion) == \
        ("2", "43211500", "H87", "Pieza", "Computadoras")
    assert (a.valor_unitario, a.importe, a.descuento, a.confianza) == ("1000.00", "2000.00", None, 0.95)
    assert (a.traslados[0].tasa_o_cuota, a.traslados[0].importe) == ("0.16", "320.00")
    assert b.retenciones[0].impuesto == "001" and not b.traslados


def test_conceptos_que_no_suman_bajan_confianza():
    cs = con_tabla([ENC, *FILAS, TOT], subtotal="9999.00")
    assert [c.importe for c in cs] == ["2000.00", "500.00"] and {c.confianza for c in cs} == {0.50}


def test_sin_tabla_se_usa_el_modelo():
    c = dict.fromkeys(mod.ConceptoLeido.model_fields)
    c.update(cantidad="1", descripcion="algo", valor_unitario="99.31", importe="99.31", clave_unidad="E48")
    r = extraer(hacer_pdf(FACTURA + "\n1 algo 99.31"), "d", modelo=Falso(Respuesta(leido(subtotal="99.31", conceptos=[c]), OK)))
    k = r.conceptos[0]
    assert (k.origen, k.confianza, k.clave_unidad) == ("modelo", 0.80, None)  # E48 no está impreso


def test_impuesto_retenido_y_exento():
    from ingesta.tablas import impuestos_de
    t, r = impuestos_de("Impuesto Trasladado IMPUESTO: 002 - IVA TIPO FACTOR: Exento")
    assert t[0].tipo_factor == "Exento" and t[0].importe is None and not r
    t, r = impuestos_de("Impuesto Retenido 002 - IVA 10.6667% $ 5.00")
    assert r[0].tasa_o_cuota == "0.106667" and not t


# ---- Etapas y servidor de modelos
def test_etapa_cpu():
    from ingesta.pipeline import extraer_sin_modelo
    assert extraer_sin_modelo((FIX / "cfdi40.xml").read_bytes(), "d").ruta == "xml"
    assert extraer_sin_modelo(hacer_pdf(FACTURA), "d") is None
    assert extraer_sin_modelo(b"hola", "d").error.codigo == "TIPO_NO_SOPORTADO"


def test_error_inesperado_no_filtra_contenido():
    r = extraer(hacer_pdf(FACTURA), "d", modelo=Falso(error=RuntimeError("RFC SECRETO")))
    assert r.error.reintentable and "SECRETO" not in r.error.detalle


def test_vllm_formato_y_reintento(monkeypatch):
    cuerpos = []
    buena = leido(uuid=U1).model_dump_json()

    def post(url, cuerpo, timeout=300):
        cuerpos.append(cuerpo)
        c = "{}" if len(cuerpos) == 1 else buena
        return {"choices": [{"message": {"content": c}, "logprobs": {
            "content": [{"token": c, "logprob": -0.01, "bytes": list(c.encode())}]}}]}
    monkeypatch.setattr(mod, "_post", post)
    r = mod.ModeloVLLM("qwen").extraer("texto", [Image.new("RGB", (4, 4))])
    assert r.datos.uuid == U1 and r.probabilidad["uuid"] > 0.9 and len(cuerpos) == 2
    assert cuerpos[0]["response_format"]["type"] == "json_schema"
    assert cuerpos[1]["messages"][0]["content"][1]["type"] == "image_url"


def test_vllm_respuesta_rara_es_transitoria(monkeypatch):
    monkeypatch.setattr(mod, "_post", lambda *a, **k: {"choices": []})
    with pytest.raises(ErrorTransitorio):
        mod.ModeloVLLM("qwen").extraer("x", [])


# ---- Lo que el modelo inventa no llega como dato (pedido del validador)
def con(texto_, **k):
    return extraer(hacer_pdf(FACTURA + "\n" + texto_), "d",
                   modelo=Falso(Respuesta(leido(**k), {n: 0.99 for n in k})))


@pytest.mark.parametrize("campo_,valor", [
    ("moneda", "MXN"), ("tipo_cambio", "1"), ("tipo_comprobante", "I"), ("descuento", "0.0"),
    ("total", "0.0"), ("subtotal", "0.00"), ("rfc_emisor", "AAA010101AAA"), ("metodo_pago", "PUE")])
def test_inventado_es_null_con_advertencia(campo_, valor):
    r = con("", **{campo_: valor})
    assert getattr(r.campos, campo_).valor is None
    a = [x for x in r.advertencias if x.campo == campo_]
    assert len(a) == 1 and a[0].codigo == "VALOR_NO_IMPRESO" and a[0].valor_descartado


def test_null_del_modelo_no_genera_advertencia():
    r = con("")
    assert r.campos.moneda.valor is None and r.advertencias == []


def test_cero_impreso_si_llega():
    c = con("Descuento 0.00", descuento="0.00").campos.descuento
    assert c.valor == "0.00" and c.confianza >= 0.90


def test_moneda_impresa_llega():
    assert con("Moneda: USD", moneda="USD").campos.moneda.valor == "USD"


@pytest.mark.parametrize("impreso,leido_,esperado,codigo", [
    ("Tipo de Comprobante: E - Egreso", "I", "E", "VALORES_EN_CONFLICTO"),
    ("Tipo de comprobante:   Ingreso", "I", "I", None),
    ("Efecto de comprobante: Egreso", None, "E", None),
    ("Tipo de comprobante: I\nNota de credito", "I", "I", "POSIBLE_NOTA_DE_CREDITO"),
    ("Tipo de comprobante: I\nTipo de comprobante: E", "I", None, "VALORES_EN_CONFLICTO"),
    ("NOTA DE CREDITO", "I", None, "VALOR_NO_IMPRESO"),
])
def test_tipo_de_comprobante(impreso, leido_, esperado, codigo):
    r = con(impreso, tipo_comprobante=leido_)
    assert r.campos.tipo_comprobante.valor == esperado
    assert [a.codigo for a in r.advertencias] == ([codigo] if codigo else [])
    if codigo:
        assert r.campos.tipo_comprobante.confianza < 0.90


def test_conceptos_e_impuestos_inventados():
    c = dict.fromkeys(mod.ConceptoLeido.model_fields)
    c.update(cantidad="7", descripcion="algo", importe="99.31", descuento="0.00")
    i = dict(impuesto="002", tipo_factor="Tasa", tasa_o_cuota="16%", importe="15.89")
    r = con("", conceptos=[c], traslados=[i])
    k = r.conceptos[0]
    assert (k.cantidad, k.importe, k.descuento) == (None, "99.31", None) and r.traslados == []
    assert {"conceptos[0].cantidad", "conceptos[0].descuento"} <= {a.campo for a in r.advertencias}
    t = con("IVA 16.00% $ 15.89", traslados=[i]).traslados[0]
    assert (t.impuesto, t.tasa_o_cuota, t.importe) == ("002", "0.16", "15.89")


def test_descuento_global_no_se_copia_a_conceptos():
    c = dict.fromkeys(mod.ConceptoLeido.model_fields)
    c.update(descripcion="algo", importe="99.31", descuento="0.00")
    r = con("Descuento 0.00", descuento="0.00", conceptos=[c])
    assert r.campos.descuento.valor == "0.00" and r.conceptos[0].descuento is None


def test_xml_sin_moneda_ni_descuento_es_null():
    x = (FIX / "cfdi40.xml").read_text().replace('Moneda="USD" TipoCambio="18.5000"', 'Descuento=""')
    r = extraer(x.replace('Unidad="', 'Unidad="').encode(), "d")
    assert (r.campos.moneda.valor, r.campos.tipo_cambio.valor, r.campos.descuento.valor) == (None,) * 3


def test_contrato_convierte_vacio_en_null():
    from ingesta.contrato import Concepto, Impuesto
    assert Concepto(descuento="", unidad="  ").model_dump(include={"descuento", "unidad"}) == {"descuento": None, "unidad": None}
    assert Impuesto(importe="").importe is None

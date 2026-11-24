import io
from dataclasses import dataclass, field

import pymupdf
from PIL import Image, ImageOps

from . import tablas
from .contrato import Concepto
from .errores import ErrorPermanente
from .normaliza import texto as nfkc

DPI = 200
MAX_PAGINAS = 20
MIN_CARACTERES = 40


@dataclass
class Documento:
    textos: list[str]          # "" si la página necesita OCR
    palabras: list[list]       # (x0, y0, x1, y1, palabra) normalizadas, por página
    imagenes: list[Image.Image]
    xml_embebidos: list[bytes]
    conceptos: list[Concepto] = field(default_factory=list)


def abrir_pdf(datos: bytes) -> Documento:
    try:
        doc = pymupdf.open(stream=datos, filetype="pdf")
    except Exception as e:
        raise ErrorPermanente("ILEGIBLE", f"PDF dañado: {e}"[:300]) from e
    with doc:
        if doc.needs_pass:
            raise ErrorPermanente("PDF_CIFRADO", "el PDF pide contraseña")
        if not 0 < len(doc) <= MAX_PAGINAS:
            raise ErrorPermanente("ILEGIBLE", f"{len(doc)} páginas (permitido: 1 a {MAX_PAGINAS})")
        try:
            textos, palabras, imagenes, conceptos = [], [], [], []
            for p in doc:
                t = nfkc(p.get_text(sort=True))
                textos.append(t if len("".join(t.split())) >= MIN_CARACTERES else "")
                if textos[-1]:
                    conceptos += tablas.conceptos(p)
                w, h = p.rect.width, p.rect.height
                palabras.append([(a / w, b / h, c / w, d / h, nfkc(s))
                                 for a, b, c, d, s, *_ in p.get_text("words", sort=True)])
                png = p.get_pixmap(dpi=DPI).tobytes("png")
                imagenes.append(Image.open(io.BytesIO(png)).convert("RGB"))
            xmls = [doc.embfile_get(i) for i in range(doc.embfile_count())
                    if doc.embfile_info(i).get("filename", "").lower().endswith(".xml")]
        except Exception as e:
            raise ErrorPermanente("ILEGIBLE", f"no se pudo leer el PDF: {e}"[:300]) from e
    return Documento(textos, palabras, imagenes, xmls, conceptos)


def abrir_imagen(datos: bytes) -> Documento:
    try:
        img = Image.open(io.BytesIO(datos))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception as e:
        raise ErrorPermanente("ILEGIBLE", f"imagen dañada: {e}"[:300]) from e
    return Documento([""], [[]], [img], [])

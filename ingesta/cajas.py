"""Coordenadas de palabras en escaneos, con Tesseract. Solo para resaltar en el panel:
si no está instalado o falla, el campo queda sin bbox y la extracción sigue."""
import csv
import io
import shutil
import subprocess

from PIL import Image

from .normaliza import texto


def palabras(img: Image.Image) -> list[tuple]:
    if not shutil.which("tesseract"):
        return []
    buf = io.BytesIO()
    img.save(buf, "PNG")
    try:
        r = subprocess.run(["tesseract", "-", "-", "-l", "spa", "--psm", "6", "tsv"],
                           input=buf.getvalue(), capture_output=True, timeout=60, check=True)
    except (subprocess.SubprocessError, OSError):
        return []
    w, h = img.size
    salida = []
    for f in csv.DictReader(io.StringIO(r.stdout.decode("utf-8", "replace")),
                            delimiter="\t", quoting=csv.QUOTE_NONE):
        if (t := (f.get("text") or "").strip()):
            x, y, a, b = (int(f[k]) for k in ("left", "top", "width", "height"))
            salida.append((x / w, y / h, (x + a) / w, (y + b) / h, texto(t)))
    return salida

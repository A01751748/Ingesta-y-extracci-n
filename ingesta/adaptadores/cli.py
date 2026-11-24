import argparse
import sys
from pathlib import Path

from ..contrato import CRITICOS
from ..pipeline import extraer

EXT = {".xml", ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ingesta", description="Extrae CFDI a JSON (contrato v1)")
    p.add_argument("entrada", type=Path, help="archivo o carpeta")
    p.add_argument("-o", "--salida", type=Path, help="carpeta para los JSON")
    a = p.parse_args(argv)
    if not a.entrada.exists():
        p.error(f"no existe: {a.entrada}")
    archivos = [a.entrada] if a.entrada.is_file() else sorted(
        f for f in a.entrada.rglob("*") if f.suffix.lower() in EXT)
    fallas = 0
    for f in archivos:
        r = extraer(f.read_bytes(), documento_id=f.name)
        js = r.model_dump_json(indent=2)
        if a.salida:
            a.salida.mkdir(parents=True, exist_ok=True)
            (a.salida / f"{f.name}.json").write_text(js, encoding="utf-8")
        else:
            print(js)
        if r.error:
            fallas += 1
            print(f"{f.name}: ERROR {r.error.codigo} {r.error.detalle}", file=sys.stderr)
        else:
            bajos = [c for c in CRITICOS if getattr(r.campos, c).confianza < 0.90]
            print(f"{f.name}: ok ruta={r.ruta} {r.duracion_ms} ms; a revisión: "
                  f"{', '.join(bajos) or 'nada'}", file=sys.stderr)
    return 1 if fallas else 0

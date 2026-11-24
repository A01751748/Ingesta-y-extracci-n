"""Exactitud contra el XML. El XML da la verdad y nunca entra al pipeline del PDF.

python -m eval datos/pares   (cada .pdf/.png/.jpg con un .xml del mismo nombre)
"""
import sys
from collections import Counter
from pathlib import Path

from ingesta.cfdi_xml import parsear
from ingesta.contrato import CRITICOS, Campos
from ingesta.normaliza import campo, iguales
from ingesta.pipeline import extraer

carpeta = Path(sys.argv[1] if len(sys.argv) > 1 else "datos/pares")
bien, total, peligro, docs = Counter(), Counter(), [], 0
for x in sorted(carpeta.glob("*.xml")):
    for ext in (".pdf", ".png", ".jpg", ".jpeg"):
        f = x.with_suffix(ext)
        if not f.exists():
            continue
        verdad, r = parsear(x.read_bytes()), extraer(f.read_bytes(), f.name)
        docs += 1
        if r.error:
            print(f"{f.name}: {r.error.codigo} {r.error.detalle}")
        for n in Campos.model_fields:
            v, c = getattr(verdad["campos"], n).valor, getattr(r.campos, n)
            ok = iguales(campo(n, v), c.valor)
            total[n] += 1
            bien[n] += ok
            if not ok:
                print(f"  {n}: esperado={v!r} leído={c.valor!r} conf={c.confianza} ({c.evidencia})")
                if n in CRITICOS and c.confianza >= 0.90:
                    peligro.append((f.name, n))
        print(f"  conceptos: esperados={len(verdad['conceptos'])} leídos={len(r.conceptos)}")
        for a, b in zip(verdad["conceptos"], r.conceptos):
            for k in ("cantidad", "clave_prod_serv", "clave_unidad", "descripcion", "objeto_imp",
                      "valor_unitario", "importe"):
                total["concepto." + k] += 1
                igual = iguales(getattr(a, k), getattr(b, k))
                bien["concepto." + k] += igual
                if not igual:
                    print(f"  concepto.{k}: esperado={getattr(a, k)!r} leído={getattr(b, k)!r}")
print(f"\nDocumentos: {docs}")
for n in total:
    print(f"{'*' if n in CRITICOS else ' '} {n:28} {bien[n]}/{total[n]}")
print(f"Críticos erróneos con confianza >= 0.90: {len(peligro)} {peligro or ''}")
sys.exit(1 if peligro or not docs else 0)

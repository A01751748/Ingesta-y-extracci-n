"""Tipo de archivo por contenido; la extensión no cuenta."""


def detectar(datos: bytes) -> str:
    cabeza = datos[:1024]
    if b"%PDF-" in cabeza:
        return "pdf"
    if cabeza.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"II*\x00", b"MM\x00*")):
        return "imagen"
    limpia = cabeza.removeprefix(b"\xef\xbb\xbf").lstrip()
    if limpia.startswith(b"<") or cabeza.startswith((b"\xff\xfe<\x00", b"\xfe\xff\x00<")):
        return "xml"
    return "desconocido"

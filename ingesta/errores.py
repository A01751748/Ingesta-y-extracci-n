"""Errores del frente 4. `reintentable` decide si SQS debe volver a intentarlo."""


class ErrorExtraccion(Exception):
    reintentable = False

    def __init__(self, codigo: str, detalle: str):
        super().__init__(f"{codigo}: {detalle}")
        self.codigo = codigo
        self.detalle = detalle


class ErrorPermanente(ErrorExtraccion):
    """El mismo archivo fallará siempre: pasa a `error` sin gastar reintentos."""


class ErrorTransitorio(ErrorExtraccion):
    """Servicio caído, límite de tiempo o salida inválida del modelo."""

    reintentable = True

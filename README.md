# Ingesta y extracción de CFDI (frente 4)

Archivo (XML, PDF o imagen) → JSON con esquema fijo, origen y confianza por campo.

**Para conectarse: [INTEGRACION.md](INTEGRACION.md)**

Se integra por S3, SQS y PostgreSQL; también se puede usar solo desde la línea de comandos.

Extraer un archivo sin servicios (Ollama en localhost):

    docker/run.sh python -m ingesta datos/factura.pdf

Las facturas reales van en `datos/`, que git ignora. Nunca versionarlas.

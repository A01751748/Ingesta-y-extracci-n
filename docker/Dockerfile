# El servidor de modelos (vLLM/Ollama) es quien usa la GPU; este proceso no necesita CUDA.
FROM python:3.12-slim AS base
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-spa \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
RUN python -c "import tomllib;print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/req.txt \
    && pip install --no-cache-dir -r /tmp/req.txt
ENV PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# Worker (cpu | gpu | dlq | outbox) sin herramientas de prueba
FROM base AS prod
COPY ingesta ./ingesta
RUN useradd --create-home app
USER app
ENTRYPOINT ["python", "-m", "ingesta.adaptadores.worker"]

FROM base AS dev
RUN pip install --no-cache-dir "pytest>=8,<10" "jsonschema>=4.21,<5"

#!/bin/sh
# Ejecuta un comando en la imagen de desarrollo con el código montado.
# Uso: docker/run.sh pytest -q   |   docker/run.sh python -m ingesta datos/factura.pdf
cd "$(dirname "$0")/.." || exit 1
docker image inspect ingesta:dev-mvp >/dev/null 2>&1 || \
  docker build -q -f docker/Dockerfile --target dev -t ingesta:dev-mvp . >/dev/null || exit 1
exec docker run --rm --network host --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -e OLLAMA_HOST -e INGESTA_MODELO -e INGESTA_OCR -e INGESTA_BACKEND -e INGESTA_VLLM_URL \
  -e INGESTA_TEST_AWS -e INGESTA_TEST_PG -v "$PWD":/app --entrypoint "" ingesta:dev-mvp "$@"

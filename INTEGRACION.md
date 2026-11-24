# Ingesta y extracción: cómo funciona y cómo conectarse

Frente 4. Convierte una factura (XML, PDF o imagen) en un JSON con formato fijo, con origen y
confianza por campo. No decide si la factura es válida ni contesta preguntas.

> **Estado:** MVP probado en local (LocalStack, PostgreSQL, Ollama). **No se ha probado en AWS ni
> con vLLM real.** Los contratos, las tablas y los nombres son propuestas hasta que cada dueño los apruebe.

## Cómo funciona

```
 Orquestador                                                    Validador
   │ 1. sube a S3  2. INSERT documentos  3. mensaje                 ▲
   ▼                                                                │ 6. mensaje
 q-ingesta ─→ worker CPU / Lambda ── XML ──────────────┐        q-validacion
                    │ PDF o imagen                     ▼            ▲
                    └──→ q-gpu ─→ worker GPU ─→ PostgreSQL ─→ outbox ┘
                                     │        (extracción + estado)
                                     └─ vLLM (Qwen + OCR)
```

1. **Detecta el tipo** por contenido, no por extensión.
2. **XML** (CFDI 3.3 y 4.0, también adjunto en un PDF): se lee sin modelo, confianza 1.0.
3. **PDF o imagen:** texto (OCR si hace falta), QR del SAT y cadena original, tabla de conceptos y Qwen.
4. **El modelo propone y el código comprueba:** cada valor se cruza con el QR, la cadena y su etiqueta
   impresa. De ahí sale la confianza. Un campo crítico por debajo de 0.90 debe ir a revisión.
5. Guarda el resultado y avisa. Resultado y aviso se guardan en la misma transacción, así no se pierden.

Nadie llama a nuestro código: **la conexión es por S3, SQS y PostgreSQL.**

## Probarlo en 5 minutos (Linux, Docker, Ollama con `qwen3.5:2b` y `glm-ocr`)

```
docker compose up -d --build --wait
docker/run.sh python -m demo.simular datos/
docker compose logs -f worker-gpu
docker compose stop
```

`demo/simular.py` hace de frontend, orquestador y validador: **es el ejemplo a copiar**.

## Por equipo

### Orquestador (Santiago)
En este orden, por archivo:
1. El archivo ya está en S3.
2. `INSERT` en `documentos` con estado `recibido`, `lote_id` (obligatorio) y `recibido_en` (por defecto, la hora del alta), y **commit**. Los duplicados por SHA-256 no se encolan.
3. Mensaje a `q-ingesta`:

```json
{"documento_id": "d-123", "lote_id": "l-9", "bucket": "facturas", "key": "l-9/d-123",
 "sha256": "<64 hex en minúsculas>", "intento": 1}
```

- **Avance del lote:** `SELECT pendientes FROM v_avance_lote WHERE lote_id = …`; 0 = terminó.
  No usar un contador: SQS repite mensajes y se descuadra.
- **Reprocesar:** `UPDATE documentos SET estado='recibido', etapa='cpu', intento=intento+1,
  codigo_motivo=NULL, detalle_motivo=NULL, arriendo_id=NULL, arrendado_hasta=NULL`, y mensaje con el nuevo `intento`.
- **Tiempo agotado:** pueden poner `estado='error'`, `codigo_motivo='TIEMPO_AGOTADO'`; un worker lento no lo pisa.
  Contarlo desde que se empieza a extraer, no desde que entra a la cola: el GPU procesa uno a la vez.

### Validador (Sofía)
Consume `q-validacion`. Llega **un aviso por documento terminado, también si falló**
(`estado_extraccion: "error"`), para que el lote pueda cerrar:

```json
{"documento_id": "d-123", "lote_id": "l-9", "intento": 1, "schema_version": "1.1.0",
 "estado_extraccion": "ok", "codigo_motivo": null}
```

Luego lee la extracción: `SELECT datos FROM extracciones_json WHERE documento_id=… AND intento=…`
(no existe si se agotaron los reintentos). Formato: `contratos/extraccion.v1.schema.json`;
ejemplo: `contratos/ejemplos/ok_xml.json`.

- Puede llegar repetido: actualizar con `WHERE estado='validando' AND intento=…`.
- Ausente es `null`, nunca `0` ni `""`; un `"0.00"` viene del XML o está impreso. Importes como texto decimal (`"1370.00"`).
- **El modelo no rellena.** En los campos críticos, moneda, tipo_cambio, tipo_comprobante, descuento, uso_cfdi y
  método/forma de pago, lo que no está impreso llega `null`, con una entrada en `advertencias`; también en los números de
  conceptos e impuestos. Excepción: un valor que contradice al QR se conserva, con confianza 0.0.
  Otros textos (nombres, serie, régimen) pueden llegar con confianza 0.30 y advertencia.
- `advertencias[].valor_descartado` es lo que propuso el modelo: útil para el panel, **no usarlo como dato**.

| Advertencia | Significa |
|---|---|
| `VALOR_NO_IMPRESO` | El modelo lo propuso y no está en el documento |
| `VALORES_EN_CONFLICTO` | Hay dos tipos de comprobante impresos, o el rótulo contradice al modelo |
| `POSIBLE_NOTA_DE_CREDITO` | Dice `I`, pero menciona "nota de crédito" |

- `tipo_comprobante` en PDF sale solo del rótulo impreso. **No es uno de los 6 campos críticos:** si llega `null` o con
  confianza menor a 0.90, mandarlo a revisión. Recomendado lo mismo para `moneda`.
- `recibido_en` trae zona (UTC) y no cambia al reprocesar. `fecha_emision` **no trae zona** (hora local del emisor):
  convertir antes de comparar, si no, una factura de esta tarde parece futura o vieja por horas.
- `lote_id` y `recibido_en` van en la raíz del JSON, tomados de la tabla `documentos`.

**¿Me llaman o yo leo?** Ninguna de las dos por HTTP: llega un mensaje a `q-validacion` y con ese id lees la extracción en
PostgreSQL. No hay estado `extraido`: el documento queda en `validando`. Como respaldo, se pueden buscar los que lleven
mucho tiempo en `validando`. Necesitas `SELECT` en `documentos`, `extracciones_json` y `extracciones`, y permiso para cambiar el
estado (pedirlo a Adrián).
- Los conceptos traen su propia confianza y pueden fallar: revisar también los menores a 0.90.
- Impuestos locales vienen aparte (`impuestos_locales`): sumarlos en K1. Ojo con tipos P (total 0) y E (resta).
- El PDF y el XML de una misma factura llegan como documentos distintos y en cualquier orden. Propuesta: manda el XML.

### Backend (Adrián)
- `sql/001_frente4.sql` es nuestro esquema provisional. Si ya existe `documentos`, agregar las columnas que falten
  (`etapa`, `arriendo_id` y `arrendado_hasta` son internas nuestras).
- Escribimos: `documentos` (solo tipo, estado, motivo y las internas), `extracciones`, `extracciones_json`, `outbox_ingesta`.
- **No** escribimos `facturas`, `conceptos`, `revisiones` ni `auditoria`: propuesta, que se llenen tras validar.
- Rol `ingesta_escritura` con permisos mínimos. Falta darle acceso; la conexión usa `INGESTA_DSN` (IAM de RDS no está implementado).

| Estado | Lo pone |
|---|---|
| `recibido`, `duplicado` | Orquestador |
| `extrayendo`, `validando`, `error` (de extracción) | **Nosotros** |
| `listo`, `en_revision`, `error` (de validación) | Validador |
| `rechazado`, `segunda_firma` | API de revisión |

### Infraestructura (Luis David)

| Recurso | Configuración |
|---|---|
| `q-ingesta`, `q-gpu` | `maxReceiveCount` 3 y una DLQ cada una. Visibilidad: 6 × el timeout de la Lambda en `q-ingesta`; 120 s en `q-gpu` |
| `q-validacion` | La configuración la define el validador |
| Lambda CPU | `docker/Dockerfile.lambda`, ≥1 GB, timeout 60 s, `ReportBatchItemFailures`, lote 1–5. O bien el worker `cpu` en contenedor |
| Worker GPU | `docker/Dockerfile` (target `prod`), argumento `gpu`. Puede vivir en la misma instancia que vLLM |
| Workers `dlq` y `outbox` | Misma imagen, 1 réplica de cada uno, sin GPU |
| Escalado de GPU | Por mensajes visibles en `q-gpu` por instancia. Avisar con SIGTERM y esperar 110 s |
| vLLM | API compatible con OpenAI, con Qwen (visión) y el modelo de OCR en la misma URL |
| Red | Los workers llegan a S3, SQS, RDS y vLLM; no necesitan internet |

| Rol | Permisos |
|---|---|
| Lambda/worker CPU | `s3:GetObject`; recibir, borrar y cambiar visibilidad en `q-ingesta`; enviar a `q-gpu` y `q-validacion` |
| Worker GPU | `s3:GetObject`; lo mismo sobre `q-gpu`; enviar a `q-gpu` y `q-validacion` |
| DLQ | Recibir, borrar y cambiar visibilidad en ambas DLQ; enviar a `q-gpu` y `q-validacion` |
| Outbox | Enviar a `q-gpu` y `q-validacion` |

Todos envían a ambas colas porque cualquiera puede despachar avisos pendientes. `s3:ListBucket` hace falta
para distinguir "no existe" de "sin permiso"; con KMS, también `kms:Decrypt` (y `GenerateDataKey` si las colas usan KMS).
Variables en `.env.ejemplo`: en AWS, quitar `INGESTA_AWS_ENDPOINT` y las llaves, y poner `INGESTA_BACKEND=vllm`,
`INGESTA_VLLM_URL`, `INGESTA_MODELO`, `INGESTA_OCR`.

Alarmas útiles: mensajes en las DLQ, antigüedad en `q-gpu`, filas de `outbox_ingesta` sin enviar por más de 5 min.
Los logs son JSON y no llevan RFC ni montos.

### Frontend (Israel, Saúl)
- Subir con checksum SHA-256 para que S3 lo verifique; si no coincide con el registrado, el documento va a `error`. Máximo 25 MB y 20 páginas.
- Panel de revisión: tabla `extracciones` (valor, origen, confianza, `evidencia`, `pagina`, `bbox`).
- `bbox` = `[x0, y0, x1, y1]` de 0 a 1, origen arriba a la izquierda. Multiplicar por el ancho y alto de la página.
  Puede ser `null`: mostrar la página sin resaltar.

### Reportes
No se conectan con nosotros; usan las vistas del backend.

## Errores

| Código | Significa | ¿Se reintenta? |
|---|---|---|
| `ILEGIBLE` | Dañado, vacío o con demasiadas páginas | No |
| `PDF_CIFRADO` | Pide contraseña | No |
| `TIPO_NO_SOPORTADO` | No es XML, PDF ni imagen | No |
| `XML_INVALIDO`, `NO_ES_CFDI` | XML roto o que no es CFDI 3.3/4.0 | No |
| `VARIAS_FACTURAS` | Más de una factura en el archivo | No |
| `PROCESAMIENTO_FALLIDO` | Modelo caído o reintentos agotados; también archivo ausente, de más de 25 MB o con otro SHA-256 | El primer caso, 3 veces; los demás no |

## Qué está probado y qué no

| Probado | Sin probar |
|---|---|
| Mensajes repetidos, worker muerto, worker lento, mensaje veneno, SQS caído, DLQ, reproceso | AWS real: IAM, KMS, VPC, RDS |
| Lote de 7 archivos con Ollama, matando el worker GPU a la mitad | Lote de 30 y carga |
| Cliente OpenAI contra Ollama | vLLM real |
| Imagen Lambda: arranca y responde; el handler, contra LocalStack | Lambda desplegada |
| 2 formatos de factura y 1 escaneo simulado | Otros emisores y fotos reales |

**La exactitud no está medida en serio:** esas facturas también sirvieron para desarrollar. Los umbrales de confianza no están calibrados.

## Límites conocidos
- GPU: un documento a la vez por worker (unos 15–25 s por PDF y 56 s el escaneo, en una RTX 5070 Ti compartida).
- Tablas que siguen en otra página sin repetir encabezado pierden renglones; la confianza baja.
- Campos secundarios (serie, folio, nombres) fallan más con `qwen3.5:2b`.

## Pruebas

```
docker/run.sh pytest -q -p no:cacheprovider                      # sin servicios
docker compose up -d --wait postgres localstack
INGESTA_TEST_PG=1 docker/run.sh pytest -q -p no:cacheprovider    # integración
docker/run.sh python -m eval datos/pares                         # exactitud contra XML
```

## Para cerrar entre equipos
1. Contrato de extracción y mensajes (Santiago, Sofía).
2. Tabla `documentos` y quién cambia cada estado (Adrián).
3. Dos colas, CPU y GPU, en lugar de una (Luis David).
4. Lambda o contenedor para CPU: ambos están listos.

Si cambia un contrato: editar `ingesta/contrato.py` o `mensajes.py`, correr
`docker/run.sh python -m ingesta.exportar_contrato` y avisar. Un cambio incompatible sube la versión.

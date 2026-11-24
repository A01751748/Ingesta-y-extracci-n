# Guía de integración: ingesta y extracción (frente 4)

Convertimos una factura (XML, PDF o imagen) en un JSON con formato fijo: cada dato con su origen y su confianza.
No decidimos si la factura es válida ni contestamos preguntas.

> **Estado:** MVP probado en local (LocalStack, PostgreSQL, Ollama). **Sin probar en AWS ni con vLLM real.**
> Contratos, tablas y nombres son **propuestas** hasta que su dueño las apruebe (sección 10).

**Busca tu sección:** [Orquestador](#4-orquestador) · [Validador](#5-validador) · [Backend](#6-backend-y-base-de-datos) ·
[Infraestructura](#7-infraestructura) · [Frontend](#8-frontend) · [Reportes](#9-reportes-y-capa-semántica).
Todos deben leer antes "Empieza aquí" y las secciones 1 a 3.

## Empieza aquí

### La idea en un minuto
Alguien sube una factura. El orquestador nos **deja un mensaje en una cola** diciendo dónde está. Nosotros la leemos, **guardamos
el resultado en la base** y **dejamos otro mensaje** para que el validador la revise. No hay ningún endpoint nuestro que llamar.

### Qué consume y qué entrega cada quien

| Equipo | Consume (entrada) | De dónde | Entrega (salida) | En dónde |
|---|---|---|---|---|
| **Frontend** | URL firmada | Respuesta de la API | El archivo | S3 |
| **Orquestador** | Lista de archivos subidos | API y S3 | 1 fila por archivo, y 1 mensaje por archivo | Tabla `documentos`, y cola `q-ingesta` |
| **Frente 4 (nosotros)** | 1 mensaje por archivo | Cola `q-ingesta` (y el archivo, de S3) | La extracción y el estado, y 1 aviso por archivo | Tablas `extracciones_json`, `extracciones` y `documentos`, y cola `q-validacion` |
| **Validador** | 1 aviso por archivo | Cola `q-validacion` (y la extracción, de `extracciones_json`) | Resultado de reglas y estado final | Tablas `validaciones` y `documentos` |
| **Orquestador** (al final) | Cuántos faltan | Vista `v_avance_lote` | Respuesta al usuario | Chat |
| **Frontend** (revisión) | Campos, confianza y recuadros | API, que lee `extracciones` | Decisión de la persona | API, que escribe `revisiones` |
| **Reportes** | Facturas en `listo` | Vistas `v_*` del backend | Reporte | S3 / chat |
| **Backend** | — | — | Tablas, permisos y la API | PostgreSQL |
| **Infraestructura** | Nuestras imágenes Docker | Este repo | Bucket, colas, red, roles y vLLM | AWS |

### Qué se "llama"

| Quiero… | Hago… | NO hago… |
|---|---|---|
| Que extraigan una factura | `INSERT` en `documentos` y luego mensaje a `q-ingesta` | Llamar una API o importar `ingesta` |
| Saber si ya terminó un documento | Leer `documentos.estado` | Esperar una respuesta del mensaje |
| Saber si ya terminó un lote | Leer `v_avance_lote` | Contar mensajes |
| Leer lo extraído | Leer `extracciones_json` con `documento_id` e `intento` | Buscarlo en el mensaje: no viene |
| Que lo vuelvan a extraer | El SQL de reprocesar (sección 4) y mensaje con el nuevo `intento` | Reenviar el mensaje viejo: se ignora |

### Palabras que se usan aquí

| Palabra | Qué es |
|---|---|
| **Cola (SQS)** | Buzón de mensajes. Uno los deja, otro los recoge cuando puede |
| **Consumir** | Recoger un mensaje, hacer el trabajo y **borrarlo**. Si no se borra, vuelve a aparecer |
| **Visibilidad** | Tiempo que el mensaje queda oculto mientras lo procesas. Si se acaba y no lo borraste, reaparece |
| **DLQ** | Cola de los mensajes que fallaron varias veces |
| **Worker** | Programa que consume una cola |
| **Lote** | Los archivos que se subieron juntos |
| **`intento`** | Cuántas veces se ha pedido extraer ese archivo. No cuenta los reintentos automáticos |
| **Commit** | Momento en que un cambio queda guardado en la base y los demás lo ven |
| **Outbox** | Tabla donde guardamos los avisos hasta enviarlos, para no perderlos |
| **Estado condicionado** | `UPDATE … WHERE estado = 'x'`: solo cambia si nadie lo cambió antes |
| **CFDI** | La factura electrónica mexicana. El XML es el original; el PDF, su impresión |
| **UUID** | El folio fiscal: identifica a la factura. `documento_id` identifica al **archivo**; no son lo mismo |
| **vLLM / Qwen** | El servidor que corre el modelo / el modelo que lee el documento |

### Un documento, de principio a fin
1. Suben `factura.pdf`. El frontend la deja en S3, en `l-9/d-123`.
2. El orquestador inserta `d-123` en `documentos` (`recibido`) y manda el mensaje 3.1 a `q-ingesta`.
3. Nosotros la tomamos: `extrayendo`. Como es PDF, pasa por dentro a `q-gpu` (ustedes no hacen nada).
4. Guardamos la extracción, ponemos `validando` y mandamos el mensaje 3.2 a `q-validacion`.
5. El validador lee la extracción, corre sus reglas y pone `en_revision`, porque no se leyó la moneda.
6. `pendientes` del lote llega a 0: el orquestador reanuda el chat y pregunta qué hacer con las dudosas.
7. Una persona captura la moneda en el panel. La API guarda la corrección y pone `validando`; se valida otra vez y queda `listo`.

## 0. Si eres un agente de código

1. Lee "Empieza aquí", las secciones 1 a 3, la de tu equipo y la 10. Implementa solo lo de tu equipo.
2. Si algo se contradice, manda en este orden: `contratos/*.schema.json` → `sql/001_frente4.sql` → esta guía.
3. **DEBE** es requisito para que la conexión funcione. **PROPUESTA** se puede cambiar, avisando. **NO** es un error conocido.
4. No edites `ingesta/` ni `contratos/`, y no inventes campos, estados ni códigos. Si falta algo, pregunta al frente 4.
5. Lo que la sección 10 marque como pendiente necesita que una persona decida: no lo resuelvas solo.
6. Ejemplos listos para tus pruebas: `contratos/ejemplos/`. Código a imitar: `demo/simular.py`. No uses facturas reales.

## 1. Flujo y responsables

```
 Frontend ──sube──→ S3
 Orquestador     ── 1. INSERT documentos (recibido) ──→ PostgreSQL
                 └─ 2. mensaje ──→ q-ingesta
                                      │
        ┌───────────── FRENTE 4 ──────▼──────────────────────────────┐
        │ worker CPU / Lambda ── XML ─────────────┐                  │
        │        │ PDF o imagen                   ▼                  │
        │        └→ q-gpu → worker GPU + vLLM → PostgreSQL → outbox ─┼─→ q-validacion
        │ worker DLQ: cierra lo que falló 3 veces                    │        │
        └────────────────────────────────────────────────────────────┘        ▼
                              listo / en_revision / error ←──────────── Validador
                              Lote sin pendientes → el orquestador reanuda el chat
```

**Nadie llama a nuestro código por HTTP.** La única conexión es S3, SQS y PostgreSQL.

| # | Paso | Responsable | Señal de que terminó |
|---|---|---|---|
| 1 | Subir archivo | Frontend | Objeto en S3 |
| 2 | Registrar | Orquestador (nodo `ingestar`) | Fila en `documentos`, estado `recibido`, con commit |
| 3 | Encolar | Orquestador (nodo `ingestar`) | Mensaje en `q-ingesta` |
| 4 | Extraer | **Frente 4** | Estado `validando` o `error` |
| 5 | Avisar | **Frente 4** | Mensaje en `q-validacion` |
| 6 | Validar | Validador | Estado `listo`, `en_revision`, `duplicado` o `error` |
| 7 | Cerrar lote | **Sin dueño (P1)** | `pendientes = 0` en `v_avance_lote` |

## 2. Reglas que aplican a todos

1. **Los mensajes se repiten.** SQS entrega al menos una vez y en cualquier orden. Todo consumidor DEBE tolerarlo.
2. **Cambia estados con condición**, incluyendo el estado anterior y el intento. Si afecta 0 filas, otro ya lo hizo: borra el mensaje.
3. **Borra el mensaje después del commit**, nunca antes.
4. **`null` = no se leyó.** `"0.00"` = cero real. Nunca los conviertas uno en otro.
5. **Importes, tasas y cantidades son texto decimal.** Usa `Decimal`/`numeric`, NO `float`.
6. **Los mensajes no llevan datos fiscales**, solo ids. Los logs tampoco deben llevarlos.
7. Un `documento_id` es un archivo. El PDF y el XML de la misma factura son **dos** documentos.

### Estados

| Desde | Hacia | Quién |
|---|---|---|
| (nuevo) | `recibido`, `duplicado` (mismo SHA-256) | Orquestador |
| `recibido` | `extrayendo` | **Frente 4** |
| `extrayendo` | `validando` | **Frente 4** |
| `recibido`, `extrayendo` | `error` | **Frente 4** (o `TIEMPO_AGOTADO`, el orquestador) |
| `validando` | `listo`, `en_revision`, `duplicado`, `error` | Validador |
| `en_revision` | `listo`, `validando`, `rechazado`, `segunda_firma` | API de revisión |
| `segunda_firma` | `listo`, `en_revision` | API de revisión |
| cualquiera terminado | `recibido` con `intento + 1` | Quien pida reprocesar |

No existe el estado `extraido`: equivale a `validando`.

## 3. Contratos

### 3.1 Mensaje de entrada: `q-ingesta` (lo manda el orquestador)
Esquema: `contratos/mensaje_documento.v1.schema.json`

```json
{"documento_id": "d-123", "lote_id": "l-9", "bucket": "facturas", "key": "l-9/d-123",
 "sha256": "0000000000000000000000000000000000000000000000000000000000000000", "intento": 1}
```

| Campo | Regla |
|---|---|
| `documento_id` | El mismo de `documentos`; texto de 1 a 200 caracteres |
| `lote_id` | Obligatorio. Si difiere de la base, manda la base |
| `bucket` | DEBE ser el configurado en `INGESTA_BUCKET` |
| `key` | Igual a `documentos.s3_key` |
| `sha256` | **Hexadecimal en minúsculas**, 64 caracteres, de los bytes del archivo |
| `intento` | Igual a `documentos.intento`. Empieza en 1 |

### 3.2 Mensaje de salida: `q-validacion` (lo mandamos nosotros, lo recibe el validador)
Esquema: `contratos/mensaje_resultado.v1.schema.json`

```json
{"documento_id": "d-123", "lote_id": "l-9", "intento": 1, "schema_version": "1.1.0",
 "estado_extraccion": "ok", "codigo_motivo": null}
```

El cuerpo del mensaje SQS (`Body`) es **exactamente ese JSON como texto**, sin envoltura de SNS ni atributos. Igual en 3.1.

Se envía por cada documento que **nosotros** terminamos, también si falló (`"error"` con `codigo_motivo`).
No se envía si otro lo cerró antes (por ejemplo `TIEMPO_AGOTADO`).

### 3.3 Extracción: tabla `extracciones_json.datos`
Esquema: `contratos/extraccion.v1.schema.json`. Ejemplos en `contratos/ejemplos/`:
`ok_xml.json`, `ok_pdf.json` (con coordenadas y una advertencia) y `error.json`.

| En la raíz | Contenido |
|---|---|
| `documento_id`, `lote_id`, `sha256` | Identidad del archivo |
| `recibido_en` | Cuándo llegó, **con zona (UTC)**. No cambia al reprocesar |
| `tipo_detectado` | `xml`, `pdf_texto`, `pdf_escaneado`, `pdf_xml_embebido`, `imagen`, `desconocido` |
| `ruta` | `xml` (sin modelo), `modelo_texto`, `modelo_ocr`, `ninguna` |
| `estado_extraccion`, `error` | `ok`, o `error` con `codigo`, `detalle` y `reintentable` |
| `campos` | Un objeto por campo (abajo). Siempre están todas las llaves |
| `conceptos` | Renglones con sus impuestos, `origen` y `confianza` por renglón |
| `traslados`, `retenciones` | Totales de impuestos de la factura |
| `impuestos_locales` | Aparte: **no** vienen en `traslados` ni `retenciones` |
| `cfdi_relacionados` | Solo en XML |
| `advertencias` | Lo que el modelo propuso y se descartó, o contradicciones |
| `version_cfdi`, `paginas`, `modelo`, `duracion_ms`, `schema_version` | Informativos |

Cada campo:

```json
{"valor": "2770.00", "origen": "texto_pdf", "confianza": 0.95, "pagina": 1,
 "bbox": [0.129, 0.429, 0.201, 0.447], "evidencia": "con etiqueta; probabilidad 0.99"}
```

- **Críticos (6):** `uuid`, `rfc_emisor`, `rfc_receptor`, `fecha_emision`, `subtotal`, `total`.
- **Otros:** `moneda`, `tipo_cambio`, `descuento`, `serie`, `folio`, `tipo_comprobante`, `nombre_emisor`, `nombre_receptor`,
  `regimen_emisor`, `regimen_receptor`, `domicilio_receptor`, `lugar_expedicion`, `uso_cfdi`, `metodo_pago`, `forma_pago`,
  `fecha_timbrado`, `sello_ultimos8`.
- **Para leer un valor:** `datos["campos"]["total"]["valor"]`. En la base, también `extracciones` tiene un renglón por campo
  (no aparecen los campos no leídos; excepto si hay `evidencia`).
- `fecha_emision` **no trae zona** (hora local del emisor), formato `AAAA-MM-DDTHH:MM:SS`.
- `origen`: `xml`, `qr`, `cadena`, `texto_pdf`, `ocr`, `modelo`.
- `bbox`: `[x0, y0, x1, y1]` de 0 a 1, origen arriba a la izquierda. Puede ser `null`.

| Confianza | Significa |
|---|---|
| 1.0 | Viene del XML |
| 0.99 | Respaldado por el QR del SAT o la cadena original |
| 0.95 | Está junto a su etiqueta impresa y el modelo estaba seguro |
| 0.80–0.89 | Respaldo parcial |
| 0.30–0.60 | Respaldo débil, o hay otros candidatos |
| 0.0 | No se leyó (`valor: null`) **o** contradice al QR (con valor): siempre a revisión |

Umbral propuesto: **menos de 0.90 va a revisión**. Los valores aún no están calibrados.

| `advertencias[].codigo` | Significa |
|---|---|
| `VALOR_NO_IMPRESO` | El modelo lo propuso y no está en el documento |
| `VALORES_EN_CONFLICTO` | Hay dos tipos de comprobante impresos, o el rótulo contradice al modelo |
| `POSIBLE_NOTA_DE_CREDITO` | Dice `I`, pero menciona "nota de crédito" |

`valor_descartado` es lo que propuso el modelo: mostrarlo como sugerencia está bien, **NO usarlo como dato**.
En críticos, `moneda`, `tipo_cambio`, `tipo_comprobante`, `descuento`, `uso_cfdi`, método y forma de pago, lo no impreso
llega `null`. Otros textos (nombres, serie, régimen) pueden llegar con 0.30 y advertencia.

### 3.4 Códigos de error

| Código | Significa | ¿Lo reintentamos? |
|---|---|---|
| `ILEGIBLE` | Dañado, vacío o más de 20 páginas | No |
| `PDF_CIFRADO` | Pide contraseña | No |
| `TIPO_NO_SOPORTADO` | No es XML, PDF, PNG, JPG ni TIFF | No |
| `XML_INVALIDO`, `NO_ES_CFDI` | XML roto o no es CFDI 3.3/4.0 | No |
| `VARIAS_FACTURAS` | Más de una factura en el archivo | No |
| `PROCESAMIENTO_FALLIDO` | Modelo caído o error inesperado | Sí, 3 veces; luego `error` |
| `PROCESAMIENTO_FALLIDO` | No está en S3, más de 25 MB, otro bucket o SHA-256 distinto | No |

## 4. Orquestador
**Dueño:** Santiago (con la API de Adrián).

| | |
|---|---|
| **Consumes** | Los archivos ya subidos a S3 (te los informa la API). Al final, la vista `v_avance_lote` |
| **Entregas** | Una fila en `documentos` y, después del commit, un mensaje JSON a `q-ingesta`, por archivo |
| **Cómo** | SQL y `sqs.send_message` (código abajo) |
| **Cómo sabes que terminamos** | No hay respuesta directa: consulta `documentos.estado` o `v_avance_lote` |
| **Permisos que necesitas** | `sqs:SendMessage` en `q-ingesta`, `s3:GetObject` (para confirmar con `HEAD`) y el rol de base de la sección 6 |
| **No te toca** | Detectar el tipo de archivo, leer el XML, hablar con `q-gpu` ni con `q-validacion` |

### Debes
- [ ] Por archivo, **en este orden**: confirmar que está en S3 → `INSERT` y commit → mensaje a `q-ingesta`.
      Si encolas antes del commit, gastas reintentos y puede acabar en la DLQ.
- [ ] No encolar duplicados por SHA-256: registrarlos con estado `duplicado`.
- [ ] Consultar el avance con `v_avance_lote`. **NO** usar un contador: los repetidos lo descuadran.
- [ ] Esperar a que **todas** las filas del lote existan antes de creer que `pendientes = 0` significa terminado.
- [ ] Poner límite de tiempo contado **desde que empieza `extrayendo`**, no desde que se encola: la GPU atiende uno a la vez.
- [ ] Retirar `extraer_datos_factura` si la usaban como tool: la extracción ya no es una tool del modelo.

```sql
-- Alta (recibido_en se llena solo con la hora del alta)
INSERT INTO documentos (documento_id, lote_id, bucket, s3_key, sha256)
VALUES (:documento_id, :lote_id, :bucket, :key, :sha256);

-- Avance
SELECT total, pendientes FROM v_avance_lote WHERE lote_id = :lote_id;

-- Tiempo agotado: un worker lento ya no puede pisarlo
UPDATE documentos SET estado = 'error', codigo_motivo = 'TIEMPO_AGOTADO', actualizado_en = now()
WHERE documento_id = :documento_id AND intento = :intento AND estado = 'extrayendo';

-- Reprocesar: después del commit, mandar el mensaje con el intento que devuelve
UPDATE documentos SET estado = 'recibido', etapa = 'cpu', intento = intento + 1, codigo_motivo = NULL,
       detalle_motivo = NULL, arriendo_id = NULL, arrendado_hasta = NULL, actualizado_en = now()
WHERE documento_id = :documento_id AND intento = :intento AND estado IN ('error', 'rechazado')
RETURNING intento;
```

```python
import json


def encolar(sqs, url_q_ingesta, doc):
    """Llamar DESPUÉS del commit del INSERT. `doc` es la fila de documentos."""
    cuerpo = {"documento_id": doc["documento_id"], "lote_id": doc["lote_id"], "bucket": doc["bucket"],
              "key": doc["s3_key"], "sha256": doc["sha256"], "intento": doc["intento"]}
    sqs.send_message(QueueUrl=url_q_ingesta, MessageBody=json.dumps(cuerpo))
```

Si mueres entre el `INSERT` y el mensaje, el documento se queda en `recibido`: revisa periódicamente los viejos y vuelve a encolarlos (repetir es inofensivo).

### Queda listo cuando
- [ ] Un lote de XML, PDF y un archivo falso termina con `pendientes = 0`.
- [ ] Mandar dos veces el mismo mensaje no cambia nada.
- [ ] Un archivo que falla no bloquea el lote.

## 5. Validador
**Dueña:** Sofía.

| | |
|---|---|
| **Consumes** | Un aviso JSON por documento desde `q-validacion`, y la extracción desde `extracciones_json` |
| **Entregas** | `validaciones` (tu tabla) y `documentos.estado` = `listo`, `en_revision`, `duplicado` o `error` |
| **Cómo** | `sqs.receive_message` → SQL → commit → `sqs.delete_message` (código abajo). En Lambda: ver nota al final |
| **Permisos que necesitas** | Recibir, borrar y cambiar visibilidad en `q-validacion`; y el rol de base de la sección 6 |
| **No te toca** | Leer archivos de S3 para validar, corregir valores ni llamar a nuestro código |

### Cómo te llega
Ni te llamamos ni tienes que buscar en la base: consumes `q-validacion` y, con `documento_id` e `intento`, lees la extracción.

```sql
SELECT datos FROM extracciones_json WHERE documento_id = :documento_id AND intento = :intento;
```

Puede no haber fila si se agotaron los reintentos. Pasa solo con `estado_extraccion = "error"`.

### Debes
- [ ] Si `estado_extraccion` es `"error"`: no validar. El documento ya está en `error`; solo revisar el cierre del lote.
- [ ] Si es `"ok"`: aplicar reglas, guardar `validaciones` y cambiar el estado **en la misma transacción**:

```sql
UPDATE documentos SET estado = :nuevo, codigo_motivo = :codigo, detalle_motivo = :detalle, actualizado_en = now()
WHERE documento_id = :documento_id AND intento = :intento AND estado = 'validando';
-- 0 filas: mensaje repetido o viejo. Deshacer y borrar el mensaje.
```

- [ ] Buscar periódicamente documentos que lleven mucho en `validando`, como respaldo.

```python
import json


def atender(sqs, conectar, url_q_validacion, validar):
    """Procesa hasta 10 avisos. `validar(datos)` es tuya y devuelve (estado, codigo, detalle).
    `conectar()` abre una conexión psycopg 3. Un aviso que falla no se borra: SQS lo entrega otra vez."""
    r = sqs.receive_message(QueueUrl=url_q_validacion, MaxNumberOfMessages=10, WaitTimeSeconds=20)
    for msg in r.get("Messages", []):
        try:
            aviso = json.loads(msg["Body"])
            llave = (aviso["documento_id"], aviso["intento"])
            if aviso["estado_extraccion"] == "ok":
                with conectar() as con:  # una transacción: commit al salir, rollback si hay excepción
                    datos = con.execute("SELECT datos FROM extracciones_json WHERE documento_id=%s AND intento=%s",
                                        llave).fetchone()[0]
                    nuevo, codigo, detalle = validar(datos)
                    cur = con.execute(
                        "UPDATE documentos SET estado=%s, codigo_motivo=%s, detalle_motivo=%s, actualizado_en=now()"
                        " WHERE documento_id=%s AND intento=%s AND estado='validando'",
                        (nuevo, codigo, detalle, *llave))
                    if cur.rowcount == 1:
                        pass  # aquí, con la misma `con`: INSERT en tu tabla validaciones
            # Aquí: revisar el cierre del lote (P1), también cuando estado_extraccion es "error"
        except Exception:
            continue  # registrar solo el id, sin datos fiscales
        sqs.delete_message(QueueUrl=url_q_validacion, ReceiptHandle=msg["ReceiptHandle"])
```

**Si lo haces en Lambda:** los avisos llegan en `event["Records"]`, el cuerpo en `record["body"]` y no llamas a `delete_message`.
Devuelve `{"batchItemFailures": [{"itemIdentifier": record["messageId"]}]}` con los que fallaron y activa `ReportBatchItemFailures`.

### Qué campo usa cada regla

| Regla | De dónde | Cuidado |
|---|---|---|
| C1 | Críticos y `moneda` con `valor` `null` | XML → `error`; PDF → `en_revision` |
| V1 | `uuid`, `rfc_emisor`, `rfc_receptor` | RFC con `Ñ` y `&` son válidos |
| V2 | `moneda`, `tipo_cambio` | `XXX` en tipo P no lleva tipo de cambio |
| V3 | `confianza` de los críticos < 0.90 | **Incluir `tipo_comprobante` y `moneda`** (no son críticos) y los `conceptos` < 0.90 |
| K1 | `subtotal`, `descuento`, `traslados`, `retenciones`, `total` | **Sumar `impuestos_locales`**. `descuento` `null`: en XML significa "sin descuento"; en PDF puede ser "no hay" o "no se leyó". Úsalo como 0 para la cuenta; si no cuadra, `en_revision` |
| K2 | `conceptos[].importe` contra `subtotal` | No aplica a tipo P (total 0) |
| K3 | `rfc_receptor` | Comparar con la lista configurada de RFC del banco |
| U1 | `uuid` | Restricción única en la base: dos pueden validarse a la vez. PDF y XML llegan en cualquier orden (P3) |
| U2 | `rfc_emisor`, `folio`, `fecha_emision`, `total` | `folio` puede ser `null` |
| O1 | `fecha_emision` contra `recibido_en` | Convertir `fecha_emision` (local, sin zona) antes de comparar |
| S1 | `uuid`, RFC, `total`, `sello_ultimos8` | `sello_ultimos8` puede ser `null` |

- **Tipo de comprobante:** `E` resta. Si llega `null` o con confianza menor a 0.90, mandar a revisión.
- **Conceptos:** un impuesto del PDF puede traer `base` o `importe` en `null`.

### Queda listo cuando
- [ ] `ok_xml.json`, `ok_pdf.json` y `error.json` se procesan sin excepción.
- [ ] `ok_pdf.json` **no** queda en `listo`: su `moneda` es `null`.
- [ ] El mismo mensaje dos veces produce un solo resultado.

## 6. Backend y base de datos
**Dueño:** Adrián. El contrato 2 es de Santiago.

| | |
|---|---|
| **Consumes** | Nuestro SQL como base. Para el panel, la API lee `extracciones` y `extracciones_json` |
| **Entregas** | Tablas y permisos en PostgreSQL; endpoints para el frontend |
| **Cómo** | Migración SQL. La API **no** llama a nuestro código |
| **No te toca** | Ejecutar la extracción ni llenar `extracciones*` |

### Debes
- [ ] Tomar `sql/001_frente4.sql` como base, o agregar a tu `documentos` las columnas que falten con **los mismos nombres y tipos**.
- [ ] Dar acceso al rol `ingesta_escritura` (el script lo crea sin login y con permisos mínimos).
- [ ] NO escribir ni reutilizar `etapa`, `arriendo_id`, `arrendado_hasta` ni `outbox_ingesta`: son internas nuestras
      (excepción: reprocesar, con el SQL de la sección 4).
- [ ] Dar `recibido_en` y `lote_id` como `NOT NULL`.
- [ ] Crear los permisos de los demás. PROPUESTA:

```sql
GRANT SELECT, INSERT ON documentos TO orquestador;
GRANT UPDATE (estado, etapa, intento, codigo_motivo, detalle_motivo, arriendo_id, arrendado_hasta, actualizado_en)
    ON documentos TO orquestador;
GRANT SELECT ON v_avance_lote TO orquestador;
GRANT SELECT ON documentos, extracciones_json, extracciones, v_avance_lote TO validador;
GRANT UPDATE (estado, codigo_motivo, detalle_motivo, actualizado_en) ON documentos TO validador;
```

### Quién escribe qué

| Tabla | Frente 4 | Los demás |
|---|---|---|
| `documentos` | Estado, motivo, tipo e internas | Alta y sus cambios de estado |
| `extracciones_json`, `extracciones` | Solo inserta | Solo leen |
| `outbox_ingesta` | Sí | No |
| `facturas`, `conceptos`, `validaciones`, `revisiones`, `auditoria` | **No** | PROPUESTA: `facturas` y `conceptos` se llenan al quedar `listo` |

- **Correcciones humanas:** NO modificar `extracciones_json`; es lo que se leyó. Guardarlas en `revisiones` y, al volver a
  `validando`, la API manda a `q-validacion` un mensaje como el 3.2. El validador debe aplicar las correcciones encima (P4).
- `numeric` para importes. NO usar `COALESCE(tipo_cambio, 1)` si la moneda no es MXN: convierte dólares en pesos.
- Conexión: `INGESTA_DSN` con usuario y contraseña (en RDS, agregar `sslmode=require`). IAM de RDS no está implementado.

### Queda listo cuando
- [ ] `ingesta_escritura` puede procesar un documento y **no** puede hacer `DELETE` en `documentos`.
- [ ] `v_avance_lote` da el mismo número que contar a mano.

## 7. Infraestructura
**Dueño:** Luis David.

| | |
|---|---|
| **Consumes** | Este repo: `docker/Dockerfile` y `docker/Dockerfile.lambda` |
| **Entregas** | Bucket, 5 colas para nosotros (`q-ingesta`, `q-gpu`, `q-validacion` y dos DLQ), procesos corriendo, vLLM y las variables |
| **Le pasas a** | Orquestador: URL de `q-ingesta` y bucket. Validador: URL de `q-validacion`. Frente 4: acceso a logs |
| **No te toca** | El código de los workers ni el contenido de los mensajes |

### Debes crear
- [ ] Bucket y colas estándar (no FIFO):

| Cola | Visibilidad | DLQ | Consume |
|---|---|---|---|
| `q-ingesta` | 6 × timeout de la Lambda (360 s); 120 s si es contenedor | `q-ingesta-dlq`, `maxReceiveCount` 3 | Lambda o worker `cpu` |
| `q-gpu` | 120 s | `q-gpu-dlq`, `maxReceiveCount` 3 | Worker `gpu` |
| `q-validacion` | La define Sofía | La define Sofía | Validador |

**Dos colas, no una:** SQS no enruta por tipo, y la GPU debe escalar solo por PDF e imágenes.
Retención de las DLQ mayor que la de su cola (por ejemplo 14 días).

- [ ] Procesos:

| Proceso | Imagen | Comando | Cantidad |
|---|---|---|---|
| CPU, opción A | `docker/Dockerfile.lambda` | (ya incluido) | Lambda: ≥1 GB, 60 s, lote 1–5, `ReportBatchItemFailures` |
| CPU, opción B | `docker/Dockerfile`, target `prod` | `cpu` | ≥1 |
| GPU | la misma | `gpu` | 0 a N según mensajes visibles en `q-gpu` |
| DLQ | la misma | `dlq` | 1 |
| Outbox | la misma | `outbox` | 1 |

- [ ] El worker `gpu` **no usa la GPU**: la usa vLLM. Al apagar, mandar SIGTERM y esperar 110 s. Si se corta antes, otro worker retoma el documento.
- [ ] vLLM con API compatible con OpenAI. **Cada proceso vLLM sirve un modelo base, y usamos dos** (Qwen con visión y OCR) con una
      sola URL: poner un enrutador delante que elija por el campo `model`, o usar el mismo modelo en ambas variables (sin probar).
      Debe aceptar `response_format: json_schema`, `logprobs` e imágenes; contexto ≥16 384.
- [ ] Red: los workers llegan a S3, SQS, RDS y vLLM. No necesitan internet.

### Permisos

| Rol | S3 | SQS |
|---|---|---|
| CPU | `GetObject`, `ListBucket` | Recibir, borrar, cambiar visibilidad y `GetQueueAttributes` en `q-ingesta`; enviar a `q-gpu` y `q-validacion` |
| GPU | `GetObject`, `ListBucket` | Lo mismo sobre `q-gpu`; enviar a `q-gpu` y `q-validacion` |
| DLQ | — | Recibir, borrar y cambiar visibilidad en ambas DLQ; enviar a `q-gpu` y `q-validacion` |
| Outbox | — | Enviar a `q-gpu` y `q-validacion` |

Todos envían a ambas colas porque cualquiera puede despachar avisos pendientes. `ListBucket` distingue "no existe" de "sin permiso".
Con KMS: `kms:Decrypt`, y `kms:GenerateDataKey` para enviar a colas cifradas.

### Variables

| Variable | Valor |
|---|---|
| `INGESTA_BUCKET` | Nombre del bucket |
| `INGESTA_Q_INGESTA`, `INGESTA_Q_GPU`, `INGESTA_Q_VALIDACION`, `INGESTA_Q_INGESTA_DLQ`, `INGESTA_Q_GPU_DLQ` | **URL** de cada cola (no ARN). Todas en todos los procesos |
| `INGESTA_DSN` | Conexión PostgreSQL, desde Secrets Manager |
| `AWS_DEFAULT_REGION` | Región |
| `INGESTA_BACKEND` | `vllm` |
| `INGESTA_VLLM_URL` | URL base, sin `/v1` |
| `INGESTA_MODELO`, `INGESTA_OCR` | Nombres de los modelos en vLLM |
| `INGESTA_VLLM_API_KEY` | Opcional |
| `INGESTA_VISIBILIDAD_S` | Opcional, 120. Duración del apartado de un documento |
| `INGESTA_MAX_BYTES` | Opcional, 26214400 (25 MB) |

NO definir `INGESTA_AWS_ENDPOINT` ni llaves de AWS: usar roles. Las variables de vLLM solo importan en `gpu`.

### Alarmas
Mensajes en las DLQ; antigüedad en `q-gpu`; filas de `outbox_ingesta` con `enviado_en IS NULL` por más de 5 min; logs con `evento` = `servicio_caido` o `dlq`.

### Queda listo cuando
- [ ] Un XML de prueba llega a `validando` y su aviso a `q-validacion`.
- [ ] Un PDF despierta la GPU y termina.
- [ ] Con vLLM apagado, el PDF acaba en `error` con `PROCESAMIENTO_FALLIDO` y el aviso llega.

## 8. Frontend
**Dueños:** Israel y Saúl.

| | |
|---|---|
| **Consumes** | URL firmadas para subir; y, en revisión, los campos que devuelve la API |
| **Entregas** | El archivo en S3; y las decisiones humanas a la API |
| **Cómo** | HTTP contra la API de Adrián. **Nunca** contra SQS, PostgreSQL ni el frente 4 |
| **Formato del endpoint** | Lo define el OpenAPI de la API (P9). PROPUESTA: devolver `extracciones_json.datos` completo, más una URL firmada del original |
| **No te toca** | Encolar, calcular confianza ni decidir estados |

### Subida
- [ ] Máximo 25 MB y 20 páginas; XML, PDF, PNG, JPG o TIFF. Avisar antes de subir.
- [ ] Subir con checksum SHA-256 para que S3 lo verifique. **S3 lo usa en base64; `documentos` y el mensaje, en hexadecimal
      minúsculo.** Son los mismos bytes: convertir. Usar subida simple, no multipart (ahí el checksum es compuesto).

```python
import base64


def hex_a_base64(sha256_hex):  # documentos y q-ingesta -> cabecera x-amz-checksum-sha256 de S3
    return base64.b64encode(bytes.fromhex(sha256_hex)).decode()
```

- [ ] No mandar el archivo por la API: va directo a S3 con URL firmada.

### Panel de revisión
- [ ] Pedir los datos a la API (que lee `extracciones` por `documento_id` e `intento`), no a la base.
- [ ] Mostrar `valor`, `origen`, `confianza` y `evidencia` junto al original.
- [ ] Resaltar con `bbox`: `x = x0 × ancho`, `y = y0 × alto`, en la `pagina` indicada (empieza en 1). Si es `null`, mostrar la página sin resaltar.
- [ ] `valor: null` se muestra como "no se leyó", **nunca** como 0.
- [ ] Mostrar `advertencias` (están en `extracciones_json.datos`); `valor_descartado` como sugerencia, sin prellenar el campo.
- [ ] Mostrar los documentos en `error` con su código; los textos pueden salir de 3.4.

### Queda listo cuando
- [ ] `ok_pdf.json` se ve con su recuadro en el total y `moneda` como "no se leyó".
- [ ] El hash del archivo coincide con el que calcula el backend.

## 9. Reportes y capa semántica
Sin conexión directa con nosotros. Usar solo facturas en `listo`, por las vistas del backend. **NO** leer `extracciones_json`:
contiene lecturas sin validar. `E` resta, y `P` no se suma.

## Si algo no funciona

| Ves esto | Causa probable | Qué hacer |
|---|---|---|
| Se queda en `recibido` | No se mandó el mensaje, a otra cola, o el worker CPU está apagado | Revisar `q-ingesta` y reenviar |
| `error` con "bucket no permitido" | `bucket` distinto de `INGESTA_BUCKET` | Corregir el mensaje y **reprocesar** |
| `error` con "el SHA-256 no coincide" | Se guardó en base64, o es de otro archivo | Guardar hexadecimal minúsculo y **reprocesar** |
| `error` con "el archivo no está en S3" | Se encoló antes de terminar la subida, o `key` mal | Subir y **reprocesar** |
| Se queda en `extrayendo` | GPU o vLLM apagado, o cola larga | Revisar `q-gpu` y sus logs |
| Se queda en `validando` | El validador no consume o falla | Revisar `q-validacion` |
| `error` con "reintentos agotados" | Modelo caído, o no existía la fila al encolar | Arreglar la causa y **reprocesar** |
| El mensaje se ignora | `intento` distinto al de la base, o ya se procesó | Usar el `intento` de `documentos` |
| El mensaje va a la DLQ y el documento no cambia | JSON inválido (falta un campo, hash en mayúsculas) | Validar contra el esquema 3.1 |
| `pendientes` nunca llega a 0 | Algún documento en `recibido`, `extrayendo` o `validando` | Buscarlo por `lote_id` |

## 10. Decisiones pendientes

| # | Decisión | Propuesta | Decide |
|---|---|---|---|
| P1 | Quién cierra el lote y reanuda el chat | El validador, al terminar cada documento, revisa `pendientes`; un cron de respaldo | Santiago |
| P2 | Aprobar contratos 3.1 a 3.3 | Versión 1.1.0 | Santiago y Sofía |
| P3 | PDF y XML con el mismo UUID | El XML siempre manda y reemplaza al PDF, también entre lotes | Sofía |
| P4 | Revalidar tras una corrección | Correcciones en `revisiones`, aplicadas encima de la extracción | Adrián y Sofía |
| P5 | Esquema de `documentos` | `sql/001_frente4.sql` | Adrián y Santiago |
| P6 | CPU en Lambda o contenedor | Ambos están listos | Luis David |
| P7 | Quién opera vLLM y cómo sirve dos modelos | Enrutador delante | Luis David y frente 4 |
| P8 | Umbral de 0.90 | Empezar así y calibrar con más facturas | Sofía y frente 4 |
| P9 | Formato de los endpoints de revisión | Devolver `extracciones_json.datos` y URL del original | Adrián y frontend |

## 11. Qué está probado

| Probado en local | Sin probar |
|---|---|
| Mensajes repetidos, worker muerto o lento, mensaje inválido, SQS caído, DLQ, reproceso | AWS real: IAM, KMS, VPC, RDS |
| Lote de 7 archivos; y, antes de los últimos ajustes, matando el worker GPU a la mitad | Lote de 30 y carga |
| Cliente OpenAI contra Ollama | vLLM real |
| Imagen Lambda: arranca y responde; su código, contra LocalStack | Lambda desplegada |
| 2 formatos de factura y 1 escaneo simulado | Otros emisores y fotos reales |

**La exactitud no está medida en serio:** esas facturas también sirvieron para desarrollar.

Límites: un documento a la vez por worker GPU (unos 5–25 s por PDF, 18–56 s el escaneo, en una RTX 5070 Ti); tablas que siguen en
otra página sin encabezado pierden renglones (baja la confianza); serie, folio y nombres fallan más.

## 12. Para el frente 4

```
docker/run.sh pytest -q -p no:cacheprovider                      # sin servicios
docker compose up -d --wait postgres localstack
INGESTA_TEST_PG=1 docker/run.sh pytest -q -p no:cacheprovider    # integración
docker compose up -d --build --wait                              # todo; requiere Ollama
docker/run.sh python -m demo.simular datos/                      # simula a los demás
docker/run.sh python -m eval datos/pares                         # exactitud contra XML
```

Cambiar un contrato: editar `contrato.py` o `mensajes.py`, correr `docker/run.sh python -m ingesta.exportar_contrato`,
subir `SCHEMA_VERSION`, actualizar ejemplos y avisar. El esquema rechaza llaves desconocidas: **agregar un campo también
obliga a los consumidores estrictos a actualizarlo.**

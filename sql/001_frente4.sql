-- Esquema PROVISIONAL del frente 4. El dueño del contrato 2 es Santiago; lo implementa Adrián.
-- Si él crea `documentos`, debe tener al menos estas columnas.
CREATE TABLE IF NOT EXISTS documentos (
    documento_id    text PRIMARY KEY,
    lote_id         text NOT NULL,
    bucket          text NOT NULL,
    s3_key          text NOT NULL,
    sha256          text NOT NULL,
    tipo            text,
    -- Lo fija el alta del documento y no cambia al reprocesar (regla de oportunidad).
    recibido_en     timestamptz NOT NULL DEFAULT now(),
    estado          text NOT NULL DEFAULT 'recibido' CHECK (estado IN
        ('recibido','duplicado','extrayendo','validando','listo','en_revision','segunda_firma','rechazado','error')),
    codigo_motivo   text,
    detalle_motivo  text,
    intento         int  NOT NULL DEFAULT 1,
    -- Internas del frente 4: nadie más las escribe.
    etapa           text NOT NULL DEFAULT 'cpu' CHECK (etapa IN ('cpu','gpu')),
    arriendo_id     uuid,
    arrendado_hasta timestamptz,
    actualizado_en  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS documentos_lote ON documentos (lote_id, estado);

-- Extracción completa según el contrato 1.
CREATE TABLE IF NOT EXISTS extracciones_json (
    documento_id text NOT NULL REFERENCES documentos,
    intento      int  NOT NULL,
    datos        jsonb NOT NULL,
    creado_en    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (documento_id, intento)
);

-- Un renglón por campo, para el panel de revisión.
CREATE TABLE IF NOT EXISTS extracciones (
    documento_id text NOT NULL,
    intento      int  NOT NULL,
    campo        text NOT NULL,
    valor        text,
    origen       text,
    confianza    numeric NOT NULL,
    pagina       int,
    bbox         jsonb,
    evidencia    text,
    PRIMARY KEY (documento_id, intento, campo),
    FOREIGN KEY (documento_id, intento) REFERENCES extracciones_json
);

-- Avisos pendientes: se guardan en la misma transacción que el resultado.
CREATE TABLE IF NOT EXISTS outbox_ingesta (
    id         bigserial PRIMARY KEY,
    cola       text NOT NULL CHECK (cola IN ('q_gpu','q_validacion')),
    cuerpo     jsonb NOT NULL,
    creado_en  timestamptz NOT NULL DEFAULT now(),
    enviado_en timestamptz
);
CREATE INDEX IF NOT EXISTS outbox_pendiente ON outbox_ingesta (id) WHERE enviado_en IS NULL;

-- Avance del lote por conteo de estados: un contador se descuadra con mensajes repetidos.
CREATE OR REPLACE VIEW v_avance_lote AS
SELECT lote_id, count(*) AS total,
       count(*) FILTER (WHERE estado IN ('recibido','extrayendo','validando')) AS pendientes
FROM documentos GROUP BY lote_id;

DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ingesta_escritura') THEN
        CREATE ROLE ingesta_escritura NOLOGIN;  -- Adrián le da acceso (IAM o contraseña)
    END IF;
END $$;
GRANT SELECT ON documentos TO ingesta_escritura;
GRANT UPDATE (tipo, estado, codigo_motivo, detalle_motivo, etapa, arriendo_id, arrendado_hasta, actualizado_en)
    ON documentos TO ingesta_escritura;
GRANT SELECT, INSERT ON extracciones_json, extracciones TO ingesta_escritura;
GRANT SELECT, INSERT, UPDATE ON outbox_ingesta TO ingesta_escritura;
GRANT USAGE ON SEQUENCE outbox_ingesta_id_seq TO ingesta_escritura;

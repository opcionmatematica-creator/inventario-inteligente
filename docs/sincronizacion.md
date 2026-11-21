# Contrato de sincronización v1

## Envío de métricas

El Core publica hechos en su outbox local; el gateway los agrupa por producto y
día del comercio. La unidad de envío es un **delta de un rango de secuencias
contiguo**, no un total diario que se deba volver a sumar al reconectar. Un SKU/día
puede aparecer en varios lotes; sumar sus deltas una vez por lote aceptado.

Contrato formal: [JSON Schema](../schemas/sync-batch.schema.json).
Ejemplo completo: [sync-batch.json](../examples/sync-batch.json).

```json
{
  "schema_version": 1,
  "batch_id": "d96e1940-a071-4ea6-b2a2-e43a9b67f265",
  "stream_id": "a2865f8e-15f8-4ed4-8af4-52bf2bfebc0b",
  "created_at": "2026-09-28T05:05:00+00:00",
  "timezone": "America/Bogota",
  "currency": "COP",
  "money_scale": 2,
  "first_seq": 1,
  "last_seq": 3,
  "event_count": 3,
  "rows": [{
    "product_ref": "29b2465092604893a89de84e82c260fe",
    "day": "2026-09-27",
    "event_count": 3,
    "received_units": 10,
    "sold_units": 6,
    "waste_units": 1,
    "revenue_minor": 3000000,
    "cogs_minor": 2100000,
    "waste_minor": 350000,
    "stock_after": 3,
    "last_seq": 3
  }]
}
```

`money_scale=2`: 3.000.000 unidades menores representan COP 30.000,00. No mezclar
monedas ni convertir con un tipo de cambio implícito. Cantidades enteras en v1;
pesables requerirán escala y unidad explícitas en una versión compatible nueva.
`stock_after` es una observación tras el último movimiento de esa fila, **no un
delta ni necesariamente el saldo al cierre del día**. Nunca sumar saldos; para
stock observado actual elegir la mayor secuencia del stream entre todos los días.

La fecha corresponde al negocio, no al día UTC. No enviar nombres, códigos de
barras, nombres de empleados, clientes, tickets ni horas exactas de venta. La
referencia genera `product_ref` con HMAC y una clave persistida local; un hash
simple de código de barras permitiría ataques de diccionario. Los agregados
vinculados a un comercio se consideran **seudonimizados**, no anónimos de forma
irreversible. Identidad del tenant y sucursal se resuelve por autenticación del
dispositivo; `stream_id` no es una credencial.

### Anulaciones y devoluciones (lote v2)

Implementado el 21 de noviembre de 2026. Las filas diarias **no cambian**: siguen llevando totales
brutos sin negativos (entradas, ventas, mermas). Una anulación o una devolución es un movimiento
compensatorio en el Core (nunca se borra ni se edita un evento) y viaja en la lista `adjustments`;
un lote que la contiene usa `schema_version: 2`. Sin compensaciones el lote sigue siendo v1.

| Campo | Contenido |
|---|---|
| `product_ref`, `day` | Seudónimo HMAC y día al que se aplica: el del movimiento original en una anulación (el error «no ocurrió»), el de la devolución en una devolución. |
| `kind` | `void` (anulación) o `return` (devolución de un cliente). |
| `metric`, `units` | Métrica de unidades afectada (`received_units`, `sold_units`, `waste_units`) y delta **con signo**. |
| `revenue_minor`, `cogs_minor`, `waste_minor` | Deltas con signo (una devolución resta ingreso y costo al precio cobrado). |
| `seq`, `stock_after` | Secuencia del evento y stock resultante. |

El receptor suma cada ajuste a la fila `(stream, product_ref, day)` (la crea en cero si no existe)
y cuenta el evento. La idempotencia del lote no cambia: el mismo `batch_id` devuelve el mismo ACK.
`event_count` = eventos de las filas + ajustes; las secuencias de filas y ajustes no se repiten.
No viajan SKU, `command_id`, `ref_seq` ni el motivo escrito por el usuario. JSON Schema actualizado
en `schemas/sync-batch.schema.json` (v1 sin `adjustments`, v2 con ellos).

### Extensión para aprendizaje completo

Este v1 ejecutable lleva movimientos y costos, pero **no contiene por sí solo
toda la información para entrenar el modelo descrito**. El PMV añadirá mensajes
tipados con esquema propio, antes de habilitar entrenamiento cloud:

| Mensaje | Campos | Semántica |
|---|---|---|
| `daily_exposure` | SKU, día, minutos abierto/con stock, completitud, cierre, revisión. | Snapshot diario por revisión, reemplazar solo si revisión es mayor; emitir también días con cero venta. |
| `supply_summary` | SKU/proveedor seudónimo, día, muestras y estadísticos de lead time, entradas pendientes y ETA. | Revisión absoluta por período; conservar suficiente distribución para variabilidad. |
| `lot_risk` | SKU, días hasta vencer, cantidades/costos por bucket. | Snapshot FEFO agregado, no sumar entre revisiones. |
| `catalog_policy` | SKU, categoría, unidad, empaque, MOQ, mínimo y política de servicio. | Configuración versionada; no enviar etiquetas identificables innecesarias. |
| `hourly_profile` | SKU/categoría, semana, 24 buckets de ventas. | Optativo, suficiente volumen; sin tickets ni horas individuales. |
| `adjustments` | Reverso/devolución/ajuste, referencia de evento, unidades e importes. | Delta compensatorio idempotente; nunca modificar lote histórico aceptado. |

Su ausencia se marca como desconocida: una fila ausente no significa un día con
demanda cero. No entrenar un modelo supuestamente consciente de quiebres o lead
time con un payload que no trae esas variables. Los fixtures de la demo del
algoritmo son sintéticos y no se extraen del lote de sincronización.

## Transporte y protocolo de ACK

Propuesta HTTP: `POST /v1/telemetry/batches`, HTTPS y token de dispositivo de
alcance limitado; cabecera `Idempotency-Key: batch_id`. El servidor deriva tenant
y sucursal del token y verifica propiedad del stream. Un stream es una época de
escritura de un solo dispositivo; el receptor productivo incluye tenant en todas
las claves de deduplicación y datos. El simulador local es monocomercio.

1. Core confirma movimiento y outbox juntos.
2. Gateway consume el log por IPC, persiste el lote exacto y su cursor; solo un
   lote en vuelo por stream. Si cae en este punto, recupera ese mismo lote.
3. Al existir ruta Wi-Fi permitida, envía el lote. El detector no abre redes desde
   el Core. Captive portal/timeout no implica sincronización exitosa.
4. Receptor valida esquema, límites, stream y rango; deduplica `batch_id` y hash
   dentro de la misma transacción que agrega métricas y avanza cursor.
5. Responde ACK después de commit. Si recibe el mismo ID con contenido distinto:
   `409 conflict`, sin aplicar nada. Un rango ya aplicado bajo otro ID también se
   rechaza. Huecos de secuencia se rechazan indicando el siguiente esperado.
6. Gateway comprueba identidad del servidor, ID, stream, digest y última secuencia;
   persiste ACK antes de preparar otro lote. No elimina el registro de ventas.

```json
{
  "batch_id": "d96e1940-a071-4ea6-b2a2-e43a9b67f265",
  "stream_id": "a2865f8e-15f8-4ed4-8af4-52bf2bfebc0b",
  "last_seq": 3,
  "sha256": "<64 caracteres hex del JSON canónico recibido>",
  "status": "persisted"
}
```

Para la referencia, JSON canónico = claves ordenadas recursivamente, separadores
`,`/`:`, UTF-8 sin escapar Unicode, sin NaN/Infinity; todos los números del lote
son enteros. El digest detecta discrepancias, no sustituye TLS ni firma.
En clientes de otro lenguaje acordar formalmente canonicalización y límites de
enteros antes de interoperar. El transporte no está implementado en este repo.

Reintento con jitter completo: `uniforme(0, min(15 min, 2^intento * 2 s))`;
persistir próximo intento. `429` respeta `Retry-After`; `5xx`/timeout reintentan;
`401` renueva credencial solo por gateway; `400/422` pone lote en cuarentena para
corregir contrato sin saltar silenciosamente eventos. Reiniciar backoff tras ACK.
Una petición caída nunca bloquea caja. Corte de Wi-Fi pausa, no vacía la cola.

Objetivo de envío: cada 15 minutos si hay cambios, máximo 500 eventos fuente y
256 KiB sin comprimir por lote; permitir gzip negociado. Rate limit conservador
y presupuesto diario configurable, preservando la cola. No enviar polling por
producto; agrupar. Ejemplo de dimensionamiento: 500 filas diarias a unos 300 B
equivalen a ~150 KB sin comprimir más envoltura; el tráfico real aumenta si se
divide el día en muchos deltas. Medir en piloto, no prometer un tamaño fijo.

El receptor limita también tamaño descomprimido y filas para evitar presión de
memoria. La referencia valida tamaño, conteos, continuidad, claves y tipos. El
historial de ventas no se purga automáticamente; compactar proyecciones y archivar
solo tras ACK y una política de respaldo/retención verificada.

## Descarga del modelo y recomendaciones

Respuesta propuesta por canal independiente del ACK de telemetría:

```json
{
  "schema_version": 1,
  "model_version": "store-ewma-2026w40.1",
  "trained_through": "2026-09-27",
  "valid_until": "2026-10-11T05:00:00Z",
  "feature_schema": "daily-exposure-v2",
  "minimum_client_version": "0.2.0",
  "parameters": {"alpha": 0.2, "review_days": 7, "service_z": 1.645},
  "quality": {"method": "rolling_origin", "windows": 8},
  "artifact_sha256": "<digest del artefacto>",
  "signature": "<firma del emisor confiable>"
}
```

Este es un contrato propuesto, no una firma válida ni un modelo entrenado. El
gateway verifica tamaño, firma con clave de confianza provisionada, vigencia,
schema y versión; guarda staging y notifica al Core por inbox. Core valida rangos
de parámetros, activa atómicamente y conserva versión anterior. No aceptar pickle,
scripts ni SQL del modelo. Si caduca, mostrar edad y volver al baseline local;
las alertas de mínimo y las ventas continúan. Las recomendaciones se recalculan
con stock actual al mostrarlas o aprobar compras, aunque el pronóstico sea cloud.

Restauración: no reutilizar secuencias desde un backup atrasado dentro del mismo
stream. Crear una época nueva mediante procedimiento autorizado y reconciliar
el historial recuperado; no reenviar hechos ya aceptados como ventas nuevas.
El prototipo no automatiza restauración ni cambio de época.

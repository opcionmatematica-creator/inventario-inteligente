# Contexto operativo del proyecto

Estado verificado: 20 de noviembre de 2026. Esta guía aplica al repositorio
completo; actualizarla cuando cambien infraestructura, comandos o configuración.

Producto comercial (licencia PolyForm Noncommercial + licencia comercial, ver
`LICENCIA-COMERCIAL.md`). No emite factura electrónica DIAN: no prometerlo.

## Mapa de infraestructura

Aplicación local: un servidor HTTP de la biblioteca estándar en el PC del comercio,
SQLite como única base, interfaz web servida por ese servidor y clientes (ventana
de escritorio, navegador del celular, app Android WebView) en la red local.

| Componente | Implementación | Ejecución y conexiones |
|---|---|---|
| Core | `inventario/edge.py`, clase `Edge` | Catálogo, stock, movimientos, tickets, conteos, outbox y recibos en SQLite. Único escritor del stock. |
| Analítica | `inventario/analytics.py` (reglas) e `inventario/insights.py` (sobre el historial real) | Demanda, compras, rotación, alertas y tablero. Caché por versión de datos. |
| CSV | `inventario/csvio.py` | Importación con vista previa y exportación. |
| Servidor | `inventario/server.py` | `0.0.0.0:8470`; API `/api/v1/*` y web. Una conexión escritora con `App.lock` y otra lectora para la analítica (`insights_lock`). Respaldo diario al arrancar. |
| Escritorio | `inventario/app.py` | Arranca el servidor y abre Edge/Chrome/Brave/Chromium en modo `--app` con perfil propio. |
| Interfaz | `inventario/web/` | HTML/CSS/JS sin dependencias ni compilación; PWA mínima. |
| Android | `movil/android/` | Java + WebView, pantalla de conexión; ver su README. |
| Sincronización | `inventario/sync.py` (`Gateway`, `Receiver`) | Referencia del protocolo de lotes/ACK; el servidor aún no la expone (pendiente: gateway cloud). |
| Demo | `inventario/demo.py`, `inventario/ejemplo.py` | Protocolo en un proceso; datos ficticios para demostraciones. |

## Comandos clave y logs

Desde la raíz, usar Python 3.11 o posterior con SQLite y datos de zona IANA
disponibles para `zoneinfo`. Demo y pruebas usan solo la biblioteca estándar;
no requieren instalar dependencias ni conectarse a internet.

```bash
# Verificar intérprete.
python3 --version

# Ejecutar el entorno de demostración local.
python3 -m inventario.demo

# Ejecutar las pruebas (68, sin red ni dependencias).
python3 -m unittest discover -s tests -v

# Abrir la app de escritorio / solo el servidor / datos de ejemplo.
python3 -m inventario.app
python3 -m inventario.server --puerto 8470 [--datos CARPETA] [--solo-local]
python3 -m inventario.ejemplo /tmp/inventario-demo

# App Android (JDK 21 con javac).
cd movil/android && ANDROID_HOME=~/Android/Sdk ./gradlew --offline assembleDebug

# Revisar cambios sin crear commits.
git status --short
git diff --check
```

No hay pipeline CI/CD ni despliegue en la nube. `Receiver` es un simulador, no un
servidor público. El servidor solo escribe en el registro errores (traceback) y,
con `INVENTARIO_LOG=1`, cada petición; nunca datos de negocio.

La demo imprime JSON en `stdout`; errores y tracebacks aparecen en `stderr`.
Las pruebas muestran su informe en la terminal. No hay archivos de log
automáticos, logging centralizado, CloudWatch ni Cloud Logging configurados.
Para guardar manualmente la salida, reemplazando el archivo si ya existe:

```bash
python3 -m inventario.demo > /tmp/inventario-demo.log 2>&1
cat /tmp/inventario-demo.log
```

El historial durable del Core está en la tabla `events`, no en un log de texto.
Las bases temporales de la demo no conservan ese historial entre ejecuciones.

## Puntos de entrada y archivos de configuración

| Ruta | Responsabilidad |
|---|---|
| `README.md` | Inicio rápido e índice de entregables. |
| `inventario/demo.py` | Arranque, fixtures y rutas temporales. |
| `inventario/edge.py` | Constructor, configuración y DDL SQLite del Core. |
| `inventario/sync.py` | DDL del receptor, validación, `MAX_EVENTS=500`, `MAX_BYTES=262144`. |
| `inventario/analytics.py` | `Demand`, `PurchasePolicy` y parámetros de las reglas predictivas. |
| `schemas/sync-batch.schema.json` | JSON Schema 2020-12 del lote v1. |
| `examples/sync-batch.json` | Payload de ejemplo. |
| `tests/test_analytics.py` | Pruebas de demanda, compras, rotación y margen. |
| `tests/test_edge_sync.py` | Pruebas de persistencia, concurrencia y sincronización. |
| `tests/test_app.py` | Catálogo, tickets, conteos, migración, analítica sobre historial, CSV, equivalencia de la demanda en forma cerrada. |
| `tests/test_server.py` | API HTTP real: flujo completo, seguridad (Host, cabecera, acceso remoto con clave), precios cambiados, reloj, respaldos. |
| `inventario/server.py` | Puerto, límites (`MAX_BODY`, `MAX_FUTURE`, `MAX_PAST`, `KEEP_BACKUPS`), carpeta de datos, rutas de la API. |
| `pyproject.toml`, `instalar/` | Paquete instalable y accesos de escritorio (Linux, Windows). |
| `LICENSE.md`, `LICENCIA-COMERCIAL.md` | Licencia pública no comercial y condiciones comerciales. |
| `docs/arquitectura.md` | Diseño objetivo, roadmap y monetización. |
| `docs/algoritmo.md` | Fórmulas y supuestos. |
| `docs/sincronizacion.md` | Contrato, ACK y protocolo propuesto. |
| `docs/validacion.md` | Evidencia de validación y pendientes. |
| `.gitignore` | Excluye `.env`, `.venv/`, cachés, logs y SQLite/WAL/SHM. |

No existen Terraform, Serverless, Dockerfiles, Compose ni Kubernetes. Sin
dependencias de terceros (`pyproject.toml` no declara ninguna). El DDL usa
`CREATE TABLE IF NOT EXISTS` y `ALTER TABLE` condicionales (`PRODUCT_MIGRATIONS`,
`EVENT_MIGRATIONS` en `edge.py`); aún no hay migraciones versionadas.

## Reglas, variables de entorno, nombres y puertos

- Variables de entorno, todas opcionales: `INVENTARIO_DATOS` (carpeta de datos),
  `INVENTARIO_PUERTO` (8470), `INVENTARIO_NAVEGADOR` (ventana), `INVENTARIO_LOG`.
  No hay `.env` ni credenciales cloud.
- Puerto: TCP 8470 en todas las interfaces. Desde 127.0.0.1 sin clave (Host debe ser
  localhost); otros dispositivos solo con «acceso remoto» activado y la clave
  (cabecera `X-Clave` o cookie `inv_clave`). Toda la API exige `X-Inventario: 1`
  salvo las descargas GET (`/exportar/*`, `/respaldo/<archivo>`).
- Fechas que manda un dispositivo: se rechazan si están a más de 10 min en el
  futuro o más de 30 días en el pasado (`MAX_FUTURE`, `MAX_PAST`).
- Caja: el cliente envía `expected_total_minor`; si los precios cambiaron, 409 con
  los precios actuales y no se registra nada. Las ventas guardadas sin conexión se
  reenvían con el mismo `ticket_id` y con el precio cobrado en cada línea.
- Zona predeterminada: `America/Bogota`. `Edge(path, timezone=...)` la persiste
  y rechaza una zona diferente al reabrir la base. Timestamps ISO 8601 con zona;
  reportes con intervalos semiabiertos `[inicio, fin)` en fecha local.
- `stream_id` y clave HMAC `product_key` se generan y guardan en SQLite. No
  incluir claves ni datos reales de clientes en logs, fixtures o telemetría.
- Código: módulos, funciones, variables y claves JSON en `snake_case`; clases
  en `PascalCase`; constantes en `UPPER_SNAKE_CASE`. Mantener identificadores
  técnicos en inglés y documentación en español.
- Pruebas: `tests/test_*.py`, `unittest.TestCase` y métodos `test_*`. No hay
  formatter ni linter configurado actualmente.
- Dinero: enteros en centavos COP, `money_scale=2`. Usar sufijo `_minor` en
  contratos y nuevas interfaces; el catálogo actual usa `price` y `cost` con
  esa misma escala. Cantidades v1 en unidades enteras.
- IDs: `command_id` estable en reintentos; contenido diferente con el mismo ID
  es error. `batch_id` y `stream_id` son UUID canónicos. `product_ref` es un
  seudónimo HMAC hexadecimal de 32 caracteres, no el SKU en claro.
- Mantener saldo, evento/outbox y recibo en una transacción; impedir stock
  negativo. Ventas y alertas deben funcionar sin internet. El diseño reserva
  la salida a red al gateway y la autoridad del stock al Core.
- Reenviar el lote pendiente sin cambios hasta recibir su ACK válido. No
  duplicar deltas ni sumar `stock_after` como si fuera un movimiento.
- Anulaciones y devoluciones: movimientos compensatorios (`void`, `return`) con `ref_seq` al
  original y motivo obligatorio (`note`); nunca editar ni borrar eventos. Una anulación se aplica
  al día del original; una devolución, al día en que ocurre y al precio cobrado. Una venta con
  devoluciones solo anula lo pendiente; no se anula una anulación. `sold_delta` (columna) da la
  venta neta que usan resumen, demanda y «más vendidos». En sincronización viajan como
  `adjustments` (lote v2, ver `docs/sincronizacion.md`).
- Usuarios (opcionales, tabla `users`): sin usuarios activos la app no pide PIN; con ellos, toda la
  API salvo `GET /estado`, `/sesion` y crear el primer dueño exige sesión (cookie `inv_sesion`),
  también desde 127.0.0.1. Roles `dueno` (todo) y `cajero` (`CASHIER_ROUTES` en `server.py`: caja,
  productos sin campos de costo, ventas; devolver y anular con `pin_dueno`). `Edge.actor` guarda
  quién hizo y quién autorizó cada evento (`user_id`, `authorized_by`); no viaja al sincronizar.
  PIN de 4–8 dígitos, no triviales, PBKDF2-SHA256 200.000 iteraciones; nunca registrar PIN.
  Recuperación solo con acceso al equipo: `python3 -m inventario.usuarios restablecer NOMBRE`.
- El servidor descarta siempre el cuerpo no leído de una petición (`dispatch`): si quedara en la
  conexión persistente, corrompería la siguiente petición.
- Las recomendaciones son borradores con razones; no enviar órdenes a
  proveedores automáticamente.
- No realizar commits, pushes, merges ni publicaciones salvo instrucción
  explícita posterior del usuario. Preservar los cambios existentes.
- Al modificar comportamiento, ejecutar las pruebas pertinentes. Para cambios
  documentales, verificar rutas y comandos. Si cambia el payload, actualizar
  contrato, ejemplo, pruebas y documentación conjuntamente.
- Mantener esta guía alineada con el código y distinguir infraestructura
  implementada de propuestas futuras.

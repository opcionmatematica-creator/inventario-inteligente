# Arquitectura de inventario inteligente

## A. Servicios y responsabilidades

La unidad de despliegue inicial es una sucursal con un equipo local. Se propone
un núcleo modular con límites de servicio claros y **dos procesos principales**:
Core y Smart-Sync. Desplegar un contenedor por cada función en un pequeño comercio
añadiría mantenimiento y puntos de fallo sin beneficio inicial. Los límites
permiten extraer servicios conforme crezca la carga.

```text
   EQUIPO DEL COMERCIO — sigue funcionando sin WAN ni Wi-Fi
  +---------------------------------------------------------------+
  | UI responsive empaquetada / lector USB-HID / cámara local      |
  |                | puente nativo o IPC autenticado               |
  |                v                                              |
  | Core Checkout & Stock Engine     [sin acceso a internet]       |
  | catálogo/precios, ventas, entradas, merma, alertas, margen      |
  |                | una transacción                              |
  |                v                                              |
  | SQLite: movimientos + saldo + recibo idempotente + outbox      |
  |                |                                              |
  | Local Event Bus: log durable + cursor por consumidor          |
  |        |                         |                            |
  |        v                         v                            |
  | reglas y reportes locales   Smart-Sync / Wi-Fi Gateway         |
  | último modelo válido       agregador + cola persistente       |
  |        ^                    + credenciales + adaptador SO     |
  |        +---- inbox validada <-----+                            |
  +----------------------------------|----------------------------+
                 LÍMITE DE EGRESO     | Wi-Fi verificada + HTTPS
                   solo gateway      | lotes/reintentos/ACK
                                     v
  NUBE (puede estar caída)    +-------------------------------+
                             | Ingesta idempotente por tenant|
                             | PostgreSQL + trabajos         |
                             | entrenamiento/evaluación      |
                             | versiones de recomendaciones  |
                             +-------------------------------+
```

| Componente | Responsabilidad y datos propios | Interfaz / fallo |
|---|---|---|
| Core Checkout & Stock | Catálogo, precios, lotes, movimientos, saldos, usuarios locales, órdenes borrador. Único escritor del stock. | Comandos IPC versionados; transacción corta. Si no puede persistir, no confirma la venta. |
| Local Event Bus / Storage | SQLite WAL, outbox en la misma transacción del movimiento, consumidores con cursor, inbox. | Notificaciones opcionales por IPC; el log durable permite recuperar notificaciones perdidas. |
| Analítica local | Mínimos, margen base, reporte semanal, reglas de reorden y modelo previamente descargado. | Proyección reconstruible; falla sin impedir vender. |
| Smart Analytics & Wi-Fi Gateway | Detectar transporte, consolidar métricas, gestionar reintentos/ACK, descargar parámetros/modelos. Cola propia. | Único cliente HTTP externo; no modifica directamente inventario. |
| Ingesta cloud | Validar identidad/contrato/cuotas, deduplicar lotes, guardar revisiones. | ACK únicamente después de persistencia; tenant derivado del token. |
| Inteligencia cloud | Evaluar modelos por categoría/SKU, producir recomendaciones y resumen enriquecido. | Trabajos asíncronos; artefactos versionados, horizonte y calidad explícitos. |

### Garantía offline y aislamiento real

**Independiente de internet** significa que una instalación ya provisionada vende,
consulta precios, escanea códigos y calcula alertas con recursos locales. Incluye
fuentes, scripts, catálogo, sesión/PIN local y reglas: no CDN, login cloud ni
verificación de licencia en la ruta de caja. No significa funcionar sin energía,
disco utilizable o un dispositivo provisionado; la instalación y restauración
pueden requerir un paquete firmado transferible por USB.

El perfil estricto usa UI web empaquetada en un contenedor nativo y comunicación
por IPC/Unix socket. Separar usuarios del sistema y permisos de archivos; Core y
UI no tienen credenciales cloud ni sockets de red externos. Aplicar sandbox del
SO que deniegue redes IP a esos procesos, permita IPC local y habilite egreso solo
al gateway. El gateway consulta la interfaz/ruta efectiva del SO, usa Wi-Fi no
medida y verifica HTTPS contra su destino; vuelve a comprobar la ruta tras cada
cambio. En móviles se necesita un adaptador nativo de conectividad. DNS, TLS y
tráfico de actualizaciones de la aplicación pasan por el gateway. Esta política
cubre los componentes de la aplicación, no todo el tráfico del equipo.

No confundir Wi-Fi con internet disponible: puede haber portal cautivo o DNS
averiado. Si el transporte es desconocido o cambia a celular, la política
`wifi_only` suspende envíos. Ethernet puede habilitarse explícitamente. No intentar
inferir Wi-Fi a partir de velocidad o de `navigator.onLine`.

Una PWA con IndexedDB y Service Worker es una variante de despliegue más sencilla,
pero su separación de red es lógica y tiene restricciones de almacenamiento y
ejecución en segundo plano. No cumple por sí sola el aislamiento estricto: la API
de tipo de conexión tiene disponibilidad limitada en navegadores.
[Fuente: MDN, NetworkInformation.type](https://developer.mozilla.org/en-US/docs/Web/API/NetworkInformation/type).
La app responsive móvil puede llegar en el PMV; la garantía estricta requiere el
contenedor/adaptador local, no basta publicar una página.

### Persistencia y consistencia

SQLite WAL en disco local permite lecturas mientras se escribe, con un escritor
a la vez; no alojar el archivo en una carpeta de red compartida.
[Fuentes: aislamiento](https://www.sqlite.org/isolation.html),
[WAL](https://www.sqlite.org/wal.html).
Usar claves foráneas, `BEGIN IMMEDIATE`, timeout acotado y persistencia durable.

Cada comando lleva `command_id` estable. Dentro de una transacción:

1. Verificar si existe el recibo del comando; devolverlo si su contenido coincide.
2. Validar SKU, permisos, cantidad y disponibilidad; impedir stock negativo.
3. Registrar movimientos inmutables y sus costos históricos; actualizar saldo.
4. Guardar evento de outbox y recibo idempotente.
5. Confirmar y devolver ticket/alertas sin esperar al bus ni a la nube.

Un reintento con el mismo ID y otro contenido se rechaza. Una devolución o
corrección crea movimientos compensatorios referenciando la operación original;
no borra la historia. FEFO asigna lotes por vencimiento; la valoración usa costo
histórico del lote (no el precio de compra más reciente). Moneda en enteros de
unidad menor; productos pesables necesitan cantidades de precisión fija y unidad
de medida explícita. IVA, descuentos y costos se separan para comparar bases
compatibles; el margen aproximado no equivale a utilidad neta contable.

Entidades PMV: `products`, `lots`, `movements`, `sales/sale_lines`, `suppliers`,
`purchase_orders/lines`, `command_receipts`, `outbox`, `consumer_offsets`,
`daily_metrics`, `sync_batches`, `model_versions`, `local_users`, `audit_log`.
Índices por código único, producto/fecha, lote/vencimiento y secuencia pendiente.
La referencia ejecutable simplifica lotes y tickets; su alcance está inventariado
en [validación](validacion.md).

### Fallos y recuperación

| Situación | Comportamiento esperado |
|---|---|
| Sin red durante semanas | Caja y alertas activas; reglas locales y último modelo; cola en disco con edad visible. |
| Gateway detenido | Outbox crece; reinicio recupera cursor; venta no depende de su salud. |
| Timeout después de recepción cloud | Reenvío del mismo lote; receptor devuelve el mismo ACK sin volver a sumar. |
| Reinicio en mitad de venta | Todo el comando confirmado o nada; reintento con ID original. |
| Disco lleno | Aviso previo por umbral; no borrar ventas pendientes ni confirmar una venta no persistida. |
| Modelo inválido/antiguo | Rechazo y fallback local, con motivo visible; nunca ejecutar código arbitrario descargado. |
| Reloj incorrecto | Orden local por secuencia monotónica; conservar hora local, zona y hora de recepción, señalar anomalías. |
| Equipo perdido | Restaurar copia cifrada verificada; nuevo `stream_id`; reconciliar movimientos posteriores no respaldados. |

Copias diarias con API de backup de SQLite (no copiar solo el `.db` abierto con
WAL), rotación local en otro medio y copia remota optativa por gateway. Objetivos
iniciales a validar: RPO ≤24 h con backup diario; RTO ≤2 h con equipo de reemplazo.
La sincronización agregada **no es backup del registro de ventas**.

Roles: propietario, cajero, encargado; PIN almacenado con hash resistente,
sesiones locales, auditoría de ajustes y precios. Cifrado de equipo y backups,
credenciales del gateway en almacén seguro, TLS y aislamiento por tenant cloud.
No incluir información de compradores en telemetría. Retención configurable
según necesidad del comercio y obligaciones aplicables, sin fijar aquí plazos
legales ni implementar facturación tributaria por suposición.

Observabilidad local: latencia de venta, errores de persistencia, disco libre,
edad de outbox, último ACK y calidad/versión del modelo. Logs sin datos personales.
Metas de piloto, no mediciones actuales: confirmación p95 <150 ms en equipo
objetivo, consulta de código p95 <50 ms y siete días de desconexión sin pérdida.

## C. Roadmap técnico

| Semana | Alcance | Criterio de aceptación |
|---|---|---|
| 1 | Catálogo, importación CSV/Excel con vista previa, unidades, proveedores y SQLite; UI responsive local. | Reimportación identificada, códigos duplicados rechazados, errores por fila; saldo inicial auditado. |
| 2 | Entradas, ventas multirrenglón, FEFO, devoluciones y merma; roles locales, outbox y alertas. | Venta atómica aun al reiniciar; siete días sin WAN; no sobreventa en pruebas concurrentes. |
| 3 | Analítica local, reglas de compras/rotación/margen; órdenes borrador y reporte semanal. | Casos de tienda, perecedero y ferretería revisados por comerciante; razones visibles por recomendación. |
| 4 | Gateway nativo Wi-Fi, ingesta cloud, ACK/reintentos, parámetros firmados y respaldo. | Cortar conexión antes/después de ACK, duplicar lotes y restaurar backup sin doble cómputo. |
| 5 | Piloto, UX en móvil y teclado, rendimiento, instalación/actualización y recuperación. | Operador completa venta y cierre offline; egreso bloqueado salvo gateway; métricas del piloto documentadas. |

Estimación para alcance limitado y equipo con experiencia; presupone hardware de
piloto disponible. La base de este repositorio precede esas cinco semanas.
Cloud PMV: una API y un trabajador asíncrono con PostgreSQL; cola más elaborada y
servicios separados solo cuando el volumen o los equipos lo justifiquen.

**Escala:** lectores USB-HID (pueden anticiparse al PMV al simular teclado), cámara
offline con decodificador embebido, pedidos por proveedor y recepción parcial;
integración de facturación electrónica mediante adaptador cloud del gateway,
estados pendiente/aceptada/rechazada separados de la venta local. Diseñar esa
integración contra la jurisdicción y proveedor elegidos en una fase específica.

Multicaja en una sucursal: escritor local único por servidor LAN. Eso tolera caída
de internet, pero no caída de la LAN entre cajas. Si se exige vender con cada caja
aislada, asignar cupos de stock por dispositivo o aceptar sobreventa y
reconciliación explícita; no prometer consistencia global fuerte durante
particiones. Multisucursal: cada sucursal posee su stock, eventos por stream,
transferencias con estados reservado/en tránsito/recibido y recepción idempotente.
No usar «última escritura gana» para cantidades. Catálogos y precios usan revisiones
con detección de conflicto; disponibilidad global siempre incluye antigüedad.

Restaurantes requieren recetas/BOM, rendimiento, conversiones y consumo de
ingredientes en la misma transacción de venta. El modelo inicial funciona con
productos inventariables; el PMV de restaurante solo se ofrece cuando se implemente
esa extensión, no se equipara un plato a una unidad de materia prima.

## D. Monetización técnica (hipótesis de producto)

| Plan | Productos activos | Usuarios locales | Sucursales | Capacidad |
|---|---:|---:|---:|---|
| Esencial | 500 | 2 | 1 | Caja offline, mínimos, margen, CSV y resumen semanal local. |
| Inteligente | 5.000 | 5 | 1 | Reorden predictivo, lotes/vencimientos, baja rotación, reportes avanzados y respaldo cloud optativo. |
| Negocio | 25.000 por sucursal | 20 por sucursal | 3 incluidas | Consolidación, transferencias, permisos avanzados y conectores contratados. |

Límites ilustrativos, no precios ni capacidad ya medida. Parametrizar mediante
`entitlements` firmados y cacheados por dispositivo, con versión y fecha de gracia.
Evaluación local: al alcanzar cupo, bloquear nuevas altas, permitir venta,
consulta, devolución, exportación y edición de registros existentes. Un downgrade
no borra productos ni usuarios ni historial: impide crecimiento hasta ajustarse.
Caducidad offline o mora nunca bloquean caja; después de la gracia suspenden
nuevos entrenamientos cloud y funciones premium conforme al contrato del plan.

Medición cloud de usuarios/sucursales y cuotas de entrenamiento; conciliación al
reconectar. No cobrar por evento de venta ni detener outbox al exceder una cuota.
Separar límites comerciales de capacidad técnica; la retención del historial
básico y la exportación no se usan como barreras de salida. Evaluar costos de
almacenamiento, tráfico y entrenamientos antes de fijar precios.

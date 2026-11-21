# Validación y alcance de la referencia

## Ejecutado

Verificación local del 27 de septiembre de 2026, Python 3.12.3:

```bash
python3 -m unittest discover -s tests -v
python3 -m inventario.demo
git diff --check
```

Resultado: **27 pruebas aprobadas**, demostración completada y sin errores de
whitespace en el diff. Las pruebas usan archivos SQLite temporales; no necesitan
internet, servicios externos, credenciales ni dependencias de terceros.

| Área | Evidencia automatizada |
|---|---|
| Venta offline | Venta y alerta de mínimo; rechazo de falta de stock sin efectos. |
| Integridad | Falla inyectada en outbox revierte el saldo; dos conexiones venden concurrentemente sin sobreventa. |
| Idempotencia | Misma venta devuelve recibo original; ID con contenido diferente se rechaza. |
| Reinicio | Reabrir SQLite conserva recibo, identidad y lote pendiente. |
| Sincronización | ACK perdido y reenvío no duplican métricas; siguiente delta se suma una vez. |
| Contrato | Rechazo de huecos, rangos repetidos, ACK incorrecto, conteos inconsistentes y campos extra. |
| Compras | Caso calculado a mano, revisión periódica, reservas/en tránsito, presupuesto, MOQ, empaque y vida útil. |
| Calidad | Días desconocidos no son cero, estado incremental recuperable, fallback sin historia. |
| Rotación | Umbrales por categoría, intervalos de venta, novedad y riesgo acumulado de lotes. |
| Margen | Merma y cargos descontados una vez, ingreso cero sin división inválida. |
| Tiempo | Conversión UTC a día local y cierre semanal semiabierto. |
| Privacidad del payload | No expone SKU en claro, command_id ni hora de cada venta. |

La demo vende seis unidades a COP 5.000, con costo COP 3.500 cada una y merma de
una unidad. Resultado semanal: ventas COP 30.000, costo vendido COP 21.000,
merma COP 3.500 y contribución COP 5.500 (18,33%). Genera una propuesta de 42
unidades por COP 147.000 con un presupuesto asignado de COP 150.000; indica que
el presupuesto impide cubrir toda la demanda objetivo. El lote se recibe dos
veces en el simulador y se aplica una sola vez.

El ejemplo JSON se valida por la función de contrato en la suite. El JSON Schema
2020-12 puede validarse adicionalmente con `jsonschema` si está instalado:

```bash
python3 -c 'import json; from jsonschema import Draft202012Validator, FormatChecker; s=json.load(open("schemas/sync-batch.schema.json")); b=json.load(open("examples/sync-batch.json")); Draft202012Validator.check_schema(s); Draft202012Validator(s, format_checker=FormatChecker()).validate(b); print("Contrato válido")'
```

## Implementado frente a propuesto

| Implementado en este repositorio | Pendiente para PMV/producción |
|---|---|
| Core Python + SQLite con transacciones y outbox | UI responsive, empaquetado e instalación/actualización local. |
| Productos, entradas, venta y merma por comando | Tickets multirrenglón, lotes FEFO en caja, costos por recepción, devoluciones y ajustes auditados. |
| Costo unitario fijo por producto y costo de cada movimiento guardado | Costeo real por lote y tratamiento completo de impuestos/descuentos. |
| EWMA incremental, compra borrador como resultado, rotación y margen | Persistir/agrupación de órdenes por proveedor, aprobación y recepción parcial. |
| Cálculo con demanda/historia sintética en demo | Proyecciones reales con exposición, calendario, pedidos, recetas e historia suficiente. |
| Resumen financiero local por intervalo | Informe ejecutivo completo, comparaciones, acciones y programación semanal. |
| Lote agregado persistente, ACK y receptor SQLite simulado | HTTP/TLS, autenticación, multitenancy, rate limits y PostgreSQL cloud. |
| Código sin llamadas de red | Procesos separados, IPC, política de egreso del SO y detector nativo de Wi-Fi. |
| HMAC de producto con clave local | Custodia de claves en almacén seguro y ciclo de rotación. |
| Contrato propuesto de modelos | Entrenamiento cloud, evaluación temporal real, firma, descarga y rollback. |
| Roadmap, límites comerciales y políticas de degradación | Usuarios/roles, entitlements firmados, cobro y conectores. |

La demo comparte una conexión entre Core y Gateway para mostrar el protocolo;
esto **no implementa el aislamiento de procesos** del diseño. No desplegar el
simulador como servidor público. Tampoco implica que se haya medido exactitud
predictiva, rendimiento en hardware de caja ni resistencia a cortes físicos de
energía. No se han instalado dependencias ni realizado commits.

## Pruebas de aceptación pendientes del PMV

1. Perfil de despliegue real: bloquear egreso en Core/UI, confirmar que solo el
   gateway sale y suspende al cambiar de Wi-Fi a celular/desconocido.
2. Siete días de operación sin WAN, reinicios y backlog con volumen representativo;
   comprobar ventas, consultas, mínimos y escáner sin recursos externos.
3. Caída de energía en hardware objetivo, disco lleno y corrupción/restauración;
   verificar objetivos de backup y no reutilizar streams atrasados.
4. Fallos de HTTP/TLS/autenticación, límites de payload, reconexión, ACK perdido y
   aislamiento entre dos comercios con identificadores manipulados.
5. FEFO, devoluciones, pesables, costos cambiantes, varias líneas y recetas antes
   de ofrecer esas capacidades a comercios reales.
6. Backtesting temporal por categoría, con cierres/quiebres y variabilidad de
   proveedor; revisar errores, capital, merma y faltantes frente al baseline.
7. Piloto de UX con propietario/cajero y medición de las metas de latencia, sin
   convertir los objetivos documentados en garantías antes de medirlos.

## Aplicación PC + móvil (20 de noviembre de 2026)

Ejecutado en Linux, Python 3.12.3:

- `python3 -m unittest discover -s tests -v`: **54 pruebas aprobadas** (las 27 originales sin
  cambios, más catálogo, tickets, conteos, migración de bases antiguas, analítica sobre el
  historial, CSV, equivalencia de la demanda en forma cerrada con la repetición día a día y la API
  HTTP real, incluido el acceso remoto por la IP de la red local).
- Interfaz con Playwright + Brave sin ventana contra un servidor con datos de ejemplo: 20 pasos
  (escáner, ticket, cobro con F2, productos, entrada, conteo, alta, alertas, resumen, ajustes,
  acceso remoto), 390 px sin desbordes, modo oscuro, sin errores de consola. Recorridos de riesgo:
  venta sin red guardada y reenviada sin duplicar, respuesta perdida + reintento = una venta, doble
  clic = una venta, precio cambiado antes de cobrar (corregido: 409 y no se cobra otra cifra).
- Rendimiento con bases sintéticas (5.000 productos / 1 año y 25.000 / 2 años): venta 1–3 ms;
  ficha de producto 22–34 ms (antes 449–1.482 ms); tablero en frío 0,45–1,35 s y con caché
  38–160 ms; con el tablero recalculándose sin parar en paralelo, una venta por HTTP tarda
  20 ms de mediana y 216 ms en p95.
- Lanzador de escritorio con un navegador simulado: abre, reutiliza el servidor en la segunda
  apertura, se detiene al cerrar la ventana y deja el respaldo diario.
- App Android: `assembleDebug` y `assembleRelease` (sin firmar) compilan.
- **Celular real** (Redmi 25062RN2DL, Android 16, HyperOS), app instalada por adb, contra un servidor
  con datos de ejemplo: conexión por cable USB (`adb reverse`) y por Wi-Fi con clave (clave incorrecta →
  mensaje claro; correcta → caja); venta táctil cobrada desde la barra inferior y verificada en el
  servidor; ticket conservado al cerrar la app; venta con el PC apagado guardada en el celular y
  reenviada sola al encenderlo; ficha de producto, entrada de mercancía y conteo físico; exportar CSV
  a «Descargas»; importar ese CSV con el selector del sistema (vista previa + importar); por Wi-Fi no
  se muestran la clave ni la carpeta de datos; página de error con «Reintentar» si el PC está apagado.
- Fallos encontrados en el celular y corregidos: el teclado se abría tras cada toque en la caja y al
  entrar en Productos; Atrás cerraba la app (Android 16 ya no llama a `onBackPressed` con
  `targetSdk 36`); `DownloadManager` no ejecutaba la descarga en HyperOS (ahora la app descarga y
  guarda con MediaStore); el CSV descargado salía atenuado en el selector (tipo con parámetros) y el
  selector filtraba CSV de otras apps; «null» en la vista previa; la tabla de la vista previa
  desbordaba la pantalla y su botón «Importar» quedaba dentro de una caja con desplazamiento; venta
  diaria «2.252» (se leía como miles); plurales «día(s)»; confirmación al registrar 1.000 o más.

Pendiente antes de vender a comercios: probar en Windows real; devoluciones y anulaciones (hoy un error
de tecleo solo se corrige con un conteo, que queda como merma);
usuarios con PIN y permisos (cajero/propietario); escaneo con cámara; instalador sin Python
previo; integración con facturación electrónica DIAN o con el POS autorizado del comercio;
licencias/planes en el software; piloto con comercios reales.

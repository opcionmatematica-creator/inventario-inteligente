# Inventario Inteligente

Caja e inventario para pequeños comercios (tiendas de barrio, minimercados, perecederos,
ferreterías) que **funciona sin internet**: qué comprar, qué se está quedando quieto y dónde
se pierde dinero, con explicaciones claras en cada sugerencia.

- **PC:** el equipo del comercio guarda los datos (SQLite) y abre la app en una ventana.
- **Celular o tableta:** otra caja del mismo comercio por la red Wi-Fi local (navegador o app Android).
- **Sin nube obligatoria:** ninguna venta espera a internet; los datos no salen del comercio.

> **Licencia:** código público con licencia [PolyForm Noncommercial 1.0.0](LICENSE.md). Probarlo y
> estudiarlo es gratis; **usarlo en un negocio requiere una licencia comercial**: ver
> [LICENCIA-COMERCIAL.md](LICENCIA-COMERCIAL.md).
>
> **No emite factura electrónica** ni el documento equivalente POS electrónico de la DIAN.

## Qué hace hoy

| Área | Funciones |
|---|---|
| Caja | Lector de código de barras USB (escanear y listo), búsqueda por nombre, más vendidos, ticket de varias líneas, cobro con F2, ventas sin conexión que se reenvían solas sin duplicarse, aviso si un precio cambió antes de cobrar. |
| Productos | Alta y edición (precio, costo, mínimo, código de barras, tipo, empaque, pedido mínimo, días de entrega), entradas de mercancía, merma, conteo físico, historial. |
| Alertas y compras | Sin stock, bajo mínimo, punto de pedido, capital quieto (sin movimiento), exceso de stock, pedido sugerido con costo estimado y razones. Son borradores: nada se envía a proveedores. |
| Resumen | Ventas, costo de lo vendido, merma, ganancia bruta y margen del día y de la semana. |
| Ajustes | Acceso de celulares con clave, importar catálogo desde CSV/Excel con vista previa, exportar productos y movimientos, respaldos diarios automáticos. |

La analítica (demanda EWMA, punto de pedido, stock de seguridad, rotación y margen) está
documentada con fórmulas en [docs/algoritmo.md](docs/algoritmo.md).

## Instalar y abrir

Requiere **Python 3.11 o posterior** (no necesita instalar dependencias) y, para la ventana de
aplicación, Microsoft Edge, Google Chrome, Brave o Chromium (si no hay ninguno, usa el navegador
predeterminado).

```bash
python3 -m inventario.app            # abre la app (servidor local + ventana)
```

- **Linux:** `bash instalar/linux/instalar.sh` crea el acceso en el menú de aplicaciones.
- **Windows:** instale Python (`winget install Python.Python.3.12`) y ejecute
  `powershell -ExecutionPolicy Bypass -File instalar\windows\instalar.ps1` (acceso en Inicio y
  en el escritorio). También sirve `instalar\windows\Inventario.bat`. *No probado aún en un Windows real.*
- **Como paquete:** `pipx install .` instala los comandos `inventario-inteligente` y `inventario-servidor`.

Datos: `~/.local/share/inventario-inteligente` (Linux), `%LOCALAPPDATA%\InventarioInteligente`
(Windows), `~/Library/Application Support/InventarioInteligente` (macOS); se cambia con
`INVENTARIO_DATOS`. Puerto: 8470 (`INVENTARIO_PUERTO`).

Para una demostración con datos ficticios (una tienda con cinco semanas de ventas):

```bash
python3 -m inventario.ejemplo /tmp/inventario-demo
INVENTARIO_DATOS=/tmp/inventario-demo python3 -m inventario.app
```

## Celulares y tabletas

1. En el PC: **Ajustes → Celulares y otros equipos → Permitir el acceso desde otros dispositivos**.
2. En el celular (misma red Wi-Fi), abra el enlace que aparece (incluye la clave). Funciona en el
   navegador y se puede «añadir a la pantalla de inicio».
3. O instale la app Android de [`movil/android`](movil/android/README.md) y pegue ese enlace.

El PC debe estar encendido con Inventario abierto. Si el celular pierde la conexión en plena
venta, la venta queda guardada en el celular y se envía sola al volver.

## Seguridad

- Desde el propio PC no se pide clave, pero el servidor exige `Host: localhost` (contra *DNS
  rebinding*) y una cabecera propia en la API (contra peticiones de otras webs).
- Otros dispositivos solo entran si se activa el acceso y presentan la clave (cookie `HttpOnly`).
  La clave solo se muestra y se cambia desde el propio PC.
- Interfaz con CSP estricta y sin `innerHTML` con datos; el servidor nunca devuelve trazas.

## Desarrollo

```bash
python3 -m unittest discover -s tests -v     # 54 pruebas, sin red ni dependencias
python3 -m inventario.server --puerto 8470   # solo el servidor
python3 -m inventario.demo                   # demo del protocolo de sincronización
```

Mapa del código, reglas y decisiones: [AGENTS.md](AGENTS.md). Arquitectura y hoja de ruta:
[docs/arquitectura.md](docs/arquitectura.md). Qué está probado y qué falta:
[docs/validacion.md](docs/validacion.md).

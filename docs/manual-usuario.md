# Manual de usuario — Inventario Inteligente 0.2

Inventario Inteligente lleva la caja, el inventario y las cuentas de un comercio pequeño **sin
depender de internet**. Los datos se guardan en el computador del negocio; los celulares y tabletas
de la misma red Wi-Fi funcionan como cajas adicionales.

> Importante: el programa **no emite factura electrónica** ni el documento equivalente POS electrónico
> de la DIAN. Si su negocio está obligado a facturar, úselo junto con un sistema autorizado.

## 1. Instalar y abrir

1. Instale Python 3.11 o posterior (en Windows: `winget install Python.Python.3.12`).
2. Descargue el programa y, en su carpeta, ejecute:
   - **Windows:** `instalar\windows\instalar.ps1` (crea accesos en el escritorio y en Inicio).
   - **Linux:** `bash instalar/linux/instalar.sh` (crea el acceso en el menú).
3. Abra **Inventario Inteligente**. Se abre una ventana; cerrarla detiene el programa.

Para probarlo con datos de ejemplo: `python3 -m inventario.ejemplo CARPETA` y abrirlo con
`INVENTARIO_DATOS=CARPETA`.

## 2. Primeros pasos

1. **Ajustes → Comercio:** escriba el nombre del negocio.
2. **Cargar productos:** uno por uno (**Productos → Nuevo producto**) o todos de una vez desde Excel:
   **Ajustes → Importar productos (CSV)** → *Descargar plantilla*, llénela (sku, nombre, precio,
   costo, stock inicial…), guárdela como CSV y elíjala. Verá una vista previa antes de importar.
3. **Proteger con PIN (recomendado):** **Ajustes → Usuarios y PIN**. Desde ese momento cada persona
   entra con su nombre y PIN.

## 3. Vender (Caja)

- Escanee el código de barras con el lector USB (se añade solo) o busque por nombre. En la pantalla
  aparecen los más vendidos para tocarlos directamente.
- Ajuste cantidades con − y +. Cobre con **Cobrar** (o la tecla **F2** / Enter con la búsqueda vacía).
- Si cambió un precio mientras armaba el ticket, el programa avisa y no cobra hasta que revise el
  total.
- Sin conexión con el computador principal (desde un celular), la venta se guarda en el celular y se
  envía sola al reconectar, sin duplicarse.

## 4. Devoluciones y anulaciones

**Caja → Ventas de hoy · devolver o anular** (o elija otro día):

- **Devolver:** cuando un cliente trae productos. Elija las unidades y escriba el motivo. El stock
  vuelve y se descuenta lo cobrado.
- **Anular venta:** cuando una venta se registró por error. Se revierte completa.
- **Anular** en la ficha de un producto: para una entrada o merma equivocada.

Nada se borra: todo queda en el historial con su motivo, quién lo hizo y quién lo autorizó. Un cajero
necesita el **PIN del dueño** para devolver o anular.

## 5. Productos e inventario

En la ficha de cada producto puede registrar **entradas de mercancía**, **mermas** (dañado, vencido,
perdido) y **conteos físicos** (si cuenta 28 y el sistema dice 30, el programa registra la diferencia).
Para cantidades de 1.000 o más pide confirmación.

## 6. Alertas, compras y resumen

- **Alertas:** sin stock, bajo el mínimo, en el punto de pedido, productos sin movimiento (capital
  quieto) y exceso de stock.
- **Pedido sugerido:** cuánto comprar de cada producto y el costo aproximado, con la razón. Es un
  borrador: no se envía a ningún proveedor.
- **Resumen:** ventas, costo de lo vendido, merma, ganancia bruta y margen del día y de la semana.
  La ganancia bruta no incluye arriendo, nómina ni impuestos.

## 7. Usuarios

| Tipo | Puede |
|---|---|
| Dueño | Todo. |
| Caja | Vender, consultar productos (sin costos), ver ventas; devolver o anular con el PIN del dueño. |

Si el dueño olvida su PIN, en el computador principal, con el programa cerrado:
`python3 -m inventario.usuarios restablecer NOMBRE`.

## 8. Celulares y tabletas

1. **Ajustes → Celulares y otros equipos → Permitir el acceso desde otros dispositivos.**
2. En el celular, abra el enlace que aparece (lleva la clave) o instale la app Inventario y péguelo.
3. El computador principal debe estar encendido y en la misma red Wi-Fi.

## 9. Respaldos y exportación

- Cada día que abre el programa se hace un respaldo automático (se guardan los últimos 14).
  **Copie alguno a una memoria USB con frecuencia.**
- **Ajustes → Exportar:** productos y movimientos en CSV para Excel. Sus datos son suyos.

## 10. Licencia

Sin licencia el programa funciona en **modo evaluación** (hasta 100 productos y 2 usuarios), que
sirve para probarlo o para uso no comercial. Para usarlo en un negocio active una licencia en
**Ajustes → Licencia** (pegue el texto recibido y acepte el contrato). Al llegar al límite del plan
solo se impiden altas nuevas: vender, consultar, devolver y exportar siguen funcionando siempre.

## 11. Problemas frecuentes

| Problema | Solución |
|---|---|
| El celular dice «No se pudo conectar» | El computador debe estar encendido, con el programa abierto y en la misma Wi-Fi. |
| «Clave incorrecta» en el celular | Revise que el acceso esté activado en Ajustes y use el enlace actual (cambia si se cambia la clave). |
| «La hora de este dispositivo no coincide» | Corrija la fecha y hora del celular o del computador. |
| No deja crear productos | Llegó al límite del plan: desactive productos que ya no vende o amplíe la licencia. |

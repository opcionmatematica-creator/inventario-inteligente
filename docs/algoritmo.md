# B. Algoritmo incremental y explicable

## Datos y ciclo de aprendizaje

Por SKU y día local: unidades vendidas y devueltas por separado, minutos abierto y
con stock, ingreso neto, costo histórico de lo vendido, descuentos, merma y motivo,
existencias por lote/vencimiento. Por proveedor: pedido, recepción, faltantes y
lead time observado. Capturar histogramas horarios locales para horas pico;
sincronizarlos solo si son necesarios y hay suficiente agregación.

Usar días de calendario con calendario de apertura explícito. Un día cerrado no
es demanda cero; un día con stock disponible y sin venta sí lo es. Ventas limitadas
por quiebre de stock censuran la demanda; no entrenar un cero como desinterés.
El baseline de referencia admite `None` para día cerrado/incompleto/censurado y
no actualiza el estado con ese día. Ignorar días censurados también puede sesgar
la muestra: mostrar baja confianza y mejorar luego con exposición y demanda
perdida estimada, sin presentar esa estimación como venta realizada.

1. Capturar localmente en transacciones; cerrar proyecciones diarias reproducibles.
2. Actualizar baseline local al disponer de un día válido; mantener resultados
   aunque nunca vuelva internet.
3. Gateway envía agregados y revisiones; nube reconstruye series por SKU/día.
4. Evaluar candidatos con cortes temporales y horizonte `L + R`, sin usar datos
   posteriores al corte ni atributos conocidos solo tras recibir un pedido.
5. Publicar parámetros/modelo solo si mejora el baseline y las métricas de negocio;
   validar versión, firma, tamaño, fechas y compatibilidad antes de activarlo.
6. Comparar recomendación con decisión humana y resultado posterior. Una compra
   aceptada no es por sí sola evidencia de un buen pronóstico.

La evaluación temporal sigue ventanas cuyo entrenamiento precede a la prueba;
comparar también contra pronósticos simples como media y naïve estacional.
[Validación temporal](https://otexts.com/fpp3/tscv.html),
[baselines](https://otexts.com/fpp3/simple-methods.html).

## 1. Demanda y baja rotación dinámica

Baseline local: media exponencial por día observado, `alpha = 0,2` configurable:

```text
mu_nueva  = alpha * venta_dia + (1-alpha) * mu_anterior
var_nueva = (1-alpha) * (var_anterior + alpha * (venta_dia-mu_anterior)^2)
```

Inicializar `mu` con la primera observación y varianza cero; menos de 14 días
válidos se considera información insuficiente. Es una heurística operativa,
no un umbral estadístico universal. La confianza de la referencia es un indicador
de cobertura de datos, no probabilidad de acierto.

Con más historia, probar patrones por día de semana; una estacionalidad anual
requiere varios ciclos y no se aprende en cinco semanas. Demanda intermitente de
repuestos: evaluar Croston corregido/SBA o TSB frente a baseline, con cuantiles
empíricos de demanda acumulada y backtesting. No usar automáticamente una normal
para series muy esporádicas. Cloud inicial propone candidatos; no promete un
modelo complejo donde faltan datos.

**Rotación:** `cobertura_dias = stock_util / mu` si `mu > 0`, y sin demanda estimada
si `mu = 0`. Separar novedad, exceso y congelado:

- Sin historia suficiente: «datos insuficientes», no «congelado».
- Umbral sin venta: `max(base_categoria, 3 * mediana_intervalo_entre_ventas)`.
  Valores iniciales configurables: perecederos 7 días, tienda 30, ferretería 90.
  Contar días abiertos con stock; excluir cierres/quiebres. Estos valores son
  hipótesis de piloto, no verdades sectoriales.
- Congelado: hay stock, historia suficiente y días elegibles sin venta ≥ umbral.
  Capital inmovilizado = suma de cantidad por costo de cada lote afectado.
- Exceso: cobertura mayor que el horizonte objetivo de categoría (propuesta
  inicial: dos veces el umbral anterior). Mostrar unidades de exceso; un repuesto
  estratégico puede quedar excluido mediante una política explícita.
- Vencimiento: prioridad mayor que rotación. Simular FEFO por fecha; para cada lote
  acumulado, comparar cantidad con demanda vendible hasta su fecha de vencimiento.
  `riesgo_acumulado = max(0, stock_acumulado - mu * dias_hasta_vencer)`.
  La referencia implementa esta aproximación determinista; producción simula
  calendario, incertidumbre, nuevas llegadas y reservas. No sumar los riesgos
  acumulados: tomar el máximo para evitar duplicar unidades.

Acción: detener reposición, revisar exhibición, devolver/transferir o proponer
promoción preservando margen. No rematar automáticamente. «Capital en riesgo»
es costo de inventario, no pérdida realizada.

## 2. Compra predictiva

Variables en unidades del producto y días de calendario:

La referencia numérica presupone apertura los siete días de la semana. Si existen
cierres programados, el PMV debe proyectar un vector diario con demanda cero en
esos cierres y sumar sus días abiertos dentro de `L` y `L+R`. No multiplicar una
media aprendida por día abierto por un horizonte calendario sin esa adaptación.
La vida útil siempre transcurre en días calendario, incluso cuando la tienda
está cerrada; para vencimientos se suma demanda solo en días vendibles. El
calendario futuro todavía no está implementado en la referencia.

```text
mu       = demanda diaria prevista
sigma²   = variabilidad diaria estimada
L        = lead time esperado del proveedor
sigmaL²  = variabilidad del lead time
R        = intervalo entre revisiones de compra
z        = factor de servicio (por ejemplo 1,645 para aproximación normal 95%)
SS(H)    = z * sqrt(H * sigma² + mu² * sigmaL²)
ROP      = ceil(mu * L       + SS(L))
objetivo = ceil(mu * (L + R) + SS(L + R))
IP       = stock_util + pedidos_confirmados_que_llegan_a_tiempo - reservas
```

La fórmula de seguridad supone independencia aproximada y demanda no demasiado
intermitente; el nivel de servicio es por ciclo, no garantía ni fill rate. No
contar pedidos atrasados sin fecha fiable. Stock útil excluye vencidos,
cuarentena y unidades que previsiblemente vencerán antes de poder venderse.
No restar dos veces reservas ya excluidas del saldo disponible.

**Política elegida: revisión periódica.** En cada revisión programada ordenar hasta
el objetivo: `q = max(0, objetivo-IP)`. ROP sirve para alerta urgente entre
revisiones; no condicionar la compra periódica a `IP <= ROP`, pues se omitiría
demanda hasta la siguiente revisión. Fuera de revisión, solo generar propuesta
urgente cuando `IP <= ROP`.

```text
si datos insuficientes:
    objetivo = minimo_manual
    q = max(0, objetivo - IP)
    motivo = "fallback, revisar manualmente"
si no:
    calcular ROP y objetivo
    q = objetivo - IP si es_revision o IP <= ROP, de otro modo 0
si q > 0:
    q = redondear_arriba(max(q, MOQ), multiplo_empaque)
    limite = min(capacidad_libre, presupuesto_asignado / costo_unitario)
    si perecedero:
        limite = min(limite, capacidad_de_venta_del_lote_antes_de_vencer)
    q = min(q, redondear_abajo(limite, multiplo_empaque))
    si q < MOQ: q = 0; marcar conflicto para revisión
generar borrador con q, costo, ROP, demanda, razones y versión del modelo
```

La vida útil usada es la **remanente a la recepción**, no desde hoy. En la
referencia: stock esperado a la llegada = `max(0, IP-mu*L)` y límite perecedero =
`max(0, floor(mu*vida_util_recepcion-stock_esperado_llegada))`. Es conservador y
requiere un `IP` depurado de lotes no vendibles; producción usa simulación FEFO.
Si no hay evidencia de demanda y el producto es perecedero, no comprar
automáticamente por una mínima manual sin revisar fecha y cantidad.

Ejemplo: mu=4/día, sigma=1, L=3, R=7, sigmaL=0 y z=1,645 → ROP=15,
objetivo=46. Con 8 útiles y 6 en tránsito a tiempo: IP=14, comprar 32;
empaque de 6 → 36 antes de aplicar límites de presupuesto/vida útil.

El presupuesto global se asigna una sola vez entre SKUs por riesgo de quiebre,
contribución esperada y criticidad. No pasar el presupuesto completo a cada
producto: la referencia recibe presupuesto **ya asignado a ese SKU**. En escala,
optimizar costo de mantener stock + faltantes + merma + costo de pedido bajo
presupuesto, empaques, MOQ, fechas y capacidad. Preferir una heurística auditable
antes de un optimizador complejo.

Las órdenes se agrupan por proveedor y fecha, con identificador de corrida para
evitar duplicar borradores. Revalidar existencias y precios al aprobar; la orden
no aumenta stock hasta recibir mercadería y nunca se envía al proveedor sin la
acción explícita del responsable.

## 3. Fuga de margen

Trabajar con ingresos netos de descuentos y devoluciones, costos históricos de
ventas netos de devoluciones recuperables e impuestos tratados consistentemente.
Un retorno no vendible genera merma además de la devolución.

```text
contribucion_real = ingreso_neto - costo_vendido - costo_merma - cargos_variables
margen_real      = contribucion_real / ingreso_neto, solo si ingreso_neto > 0
contribucion_objetivo = ingreso_neto * margen_objetivo
brecha           = max(0, contribucion_objetivo - contribucion_real)
```

Separar efectos: costo de compra creciente a precio estable, descuentos,
merma/vencimientos, comisiones y diferencias de conteo físico. Identificar
descuentos requiere además precio de lista y cantidad; no atribuir causalidad a
una brecha agregada sin esos datos. Si faltan costos, etiquetar «margen no
confiable». Si ingreso ≤0, porcentaje no disponible; conservar importes.
Brecha contra objetivo y costo de merma se solapan, por lo que no deben sumarse
como dos pérdidas independientes. Gastos fijos, impuestos y costo de oportunidad
quedan fuera de este margen aproximado.

## 4. Resumen semanal inteligente

Semana lunes 00:00 a lunes siguiente 00:00 en zona del comercio, intervalo
semiabierto. Generación local al abrir después del cierre, aun sin internet;
canales externos opcionales salen por gateway con suscripciones configuradas.
Identificador `(comercio, sucursal, inicio_semana, revision)`; revisiones por
movimientos tardíos, sin duplicar informes en cada reconexión.

| Bloque | Contenido y acción |
|---|---|
| Calidad | Período, zona, cierre, días completos/censurados, datos faltantes, sincronización y modelo. |
| Resultado | Ventas netas, costo vendido, contribución, margen y variación vs semana comparable. |
| Comprar | Top productos, unidades sugeridas, proveedor, presupuesto y motivo/urgencia. |
| Recuperar capital | Stock congelado/exceso y costo inmovilizado, con acción concreta. |
| Evitar desperdicio | Lotes a vencer y unidades/costo en riesgo, separados de merma realizada. |
| Corregir margen | SKU con mayor brecha y evidencia (descuento, costo o merma). |
| Siguiente paso | Hasta tres acciones priorizadas, responsable y estado del borrador. |

Usar plantillas deterministas; no hace falta un LLM para redactar estos hechos.
Las recomendaciones incluyen razones y calidad, nunca beneficios garantizados.

## Criterios para promover modelos

Mínimo varias ventanas de validación, mismo horizonte que abastecimiento y
comparación con reglas actuales. MAE/MASE cuando sus denominadores existen,
sesgo, error por horizonte y pérdida por cuantiles; evitar MAPE con ceros. WAPE
solo si venta total >0. Simular tasa de quiebre, merma, capital y costo total;
tratar días censurados como incertidumbre, no demanda conocida. Inicialmente
proponer mejora ≥5% de costo simulado sin empeorar merma/quiebres, umbral ajustable
tras piloto. Canary, monitoreo de deriva y rollback a última versión válida.
La referencia implementa baseline incremental; no incluye entrenamiento cloud,
selección automática de candidatos ni una evaluación con datos reales.

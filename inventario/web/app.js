"use strict";

// Inventario Inteligente — interfaz web (sin dependencias ni compilación).
// Todo el contenido se construye con nodos DOM (h); nunca innerHTML con datos.

const API = "/api/v1";
const vista = document.getElementById("vista");
let estado = null;          // /estado
let alSalir = [];           // limpiezas de la vista actual

// ---------------------------------------------------------
// Utilidades
// ---------------------------------------------------------

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key === "value") el.value = value;
    // CSSOM en vez del atributo: la CSP (style-src 'self') bloquea style="…".
    else if (key === "style") el.style.cssText = value;
    else if (key === "checked") el.checked = Boolean(value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function pagina(titulo, acciones, ...contenido) {
  return h("div", { class: "pagina" },
    h("div", { class: "cabecera" }, h("h1", {}, titulo), acciones || null),
    contenido);
}

class ErrorApi extends Error {
  constructor(status, message, datos = null) { super(message); this.status = status; this.datos = datos; }
}

async function api(ruta, { method = "GET", body, raw } = {}) {
  const headers = { "X-Inventario": "1" };
  let payload;
  if (raw !== undefined) { payload = raw; headers["Content-Type"] = "text/csv"; }
  else if (body !== undefined) { payload = JSON.stringify(body); headers["Content-Type"] = "application/json"; }
  let respuesta;
  try {
    respuesta = await fetch(API + ruta, { method, headers, body: payload, credentials: "same-origin" });
  } catch (error) {
    marcarConexion(false);
    throw new ErrorApi(0, "No hay conexión con el equipo del inventario.");
  }
  marcarConexion(true);
  const tipo = respuesta.headers.get("Content-Type") || "";
  const datos = tipo.includes("application/json") ? await respuesta.json() : await respuesta.text();
  if (!respuesta.ok) {
    const detalle = (datos && datos.detail) || `Error ${respuesta.status}`;
    if (respuesta.status === 401 && datos && datos.login && ruta !== "/sesion") {
      estado = { ...(estado || {}), user: null };
      setTimeout(() => cargarEstado().then(mostrar).catch(() => {}), 0);
    }
    throw new ErrorApi(respuesta.status, detalle.charAt(0).toUpperCase() + detalle.slice(1), datos);
  }
  return datos;
}

function aviso(texto, error = false) {
  const el = h("div", { class: "aviso" + (error ? " error" : ""), role: error ? "alert" : "status" }, texto);
  document.getElementById("avisos").append(el);
  setTimeout(() => el.remove(), error ? 7000 : 4000);
}

let relojDesfasado = false;

function marcarConexion(ok) {
  const el = document.getElementById("estado-conexion");
  const pendientes = ventasPendientes().length;
  el.classList.toggle("error", !ok || relojDesfasado);
  el.textContent = !ok ? "Sin conexión con el equipo"
    : relojDesfasado ? "La hora de este dispositivo no coincide con la del equipo principal"
    : pendientes ? `${plural(pendientes, "venta", "ventas")} por enviar` : "";
}

function revisarReloj() {
  const servidor = Date.parse(estado.time);
  relojDesfasado = Number.isFinite(servidor) && Math.abs(servidor - Date.now()) > 5 * 60 * 1000;
  marcarConexion(true);
}

function plural(n, uno, varios) { return `${n} ${n === 1 ? uno : varios}`; }
function unidades(n, unidad) {
  return unidad === "unidad" ? plural(n, "unidad", "unidades") : `${n} ${unidad}`;
}

const pesos = new Intl.NumberFormat("es-CO", { maximumFractionDigits: 0 });
function dinero(minor) {
  if (minor === null || minor === undefined) return "—";
  const negativo = minor < 0;
  const abs = Math.abs(minor);
  const centavos = abs % 100;
  const texto = "$ " + pesos.format(Math.floor(abs / 100)) + (centavos ? "," + String(centavos).padStart(2, "0") : "");
  return negativo ? "−" + texto : texto;
}

// Mismas reglas que csvio.parse_money: «5.000», «5000», «5.000,50», «5000.5».
function leerDinero(texto) {
  let v = String(texto || "").replace(/[\s$]|COP/gi, "");
  if (!v) throw new Error("Escriba un importe");
  if (!/^\d[\d.,]*$/.test(v)) throw new Error(`Importe inválido: ${texto}`);
  if (v.includes(".") && v.includes(",")) {
    const decimal = v.lastIndexOf(",") > v.lastIndexOf(".") ? "," : ".";
    v = v.split(decimal === "," ? "." : ",").join("").replace(decimal, ".");
  } else {
    const sep = v.includes(".") ? "." : v.includes(",") ? "," : "";
    if (sep) {
      const partes = v.split(sep);
      if (partes.length > 2 || (partes.length === 2 && partes[1].length === 3)) {
        if (!partes.slice(1).every((p) => p.length === 3)) throw new Error(`Importe inválido: ${texto}`);
        v = partes.join("");
      } else v = v.replace(sep, ".");
    }
  }
  const [entero, cent = ""] = v.split(".");
  if (cent.length > 2) throw new Error(`Máximo 2 decimales: ${texto}`);
  return Number(entero || "0") * 100 + Number((cent + "00").slice(0, 2));
}

function leerEntero(texto, nombre, minimo = 0) {
  const v = String(texto ?? "").trim();
  if (!/^\d+$/.test(v)) throw new Error(`${nombre}: escriba un número entero`);
  const n = Number(v);
  if (n < minimo) throw new Error(`${nombre}: mínimo ${minimo}`);
  return n;
}

function pesosEditables(minor) {
  return minor % 100 ? `${Math.floor(minor / 100)},${String(minor % 100).padStart(2, "0")}` : String(minor / 100);
}

function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;
  const x = [...b].map((n) => n.toString(16).padStart(2, "0")).join("");
  return `${x.slice(0, 8)}-${x.slice(8, 12)}-${x.slice(12, 16)}-${x.slice(16, 20)}-${x.slice(20)}`;
}

function ahora() {
  const d = new Date();
  const p = (n) => String(Math.trunc(Math.abs(n))).padStart(2, "0");
  const off = -d.getTimezoneOffset();
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:` +
    `${p(d.getSeconds())}${off >= 0 ? "+" : "-"}${p(off / 60)}:${p(off % 60)}`;
}

const DIAS = ["dom", "lun", "mar", "mié", "jue", "vie", "sáb"];
function dia(iso, largo = false) {
  const d = new Date(iso + "T12:00:00");
  return largo ? d.toLocaleDateString("es-CO", { weekday: "long", day: "numeric", month: "long" })
    : `${DIAS[d.getDay()]} ${d.getDate()}`;
}

const memoria = {
  leer(clave, defecto) { try { return JSON.parse(localStorage.getItem(clave)) ?? defecto; } catch { return defecto; } },
  guardar(clave, valor) { try { localStorage.setItem(clave, JSON.stringify(valor)); } catch { /* sin espacio */ } },
};

const CATEGORIAS = { tienda: "Tienda / abarrotes", perecedero: "Perecedero", ferreteria: "Ferretería / repuestos" };
const TIPOS = { receive: "Entrada", sale: "Venta", waste: "Merma", void: "Anulación", return: "Devolución" };

// Formulario en la página para anular o devolver: el motivo es obligatorio y queda en el historial.
// (Sin prompt(): en la WebView de Android no es fiable.)
function esCajero() { return Boolean(estado && estado.user && estado.user.role === "cajero"); }

function formularioMotivo({ titulo, boton, maximo, alConfirmar, alCancelar }) {
  const motivo = h("input", { maxlength: "200", autocomplete: "off", placeholder: "Motivo (obligatorio)" });
  // Un cajero necesita que el dueño autorice escribiendo su PIN.
  const pinDueno = esCajero() ? h("input", { type: "password", inputmode: "numeric", autocomplete: "off",
    maxlength: "8", class: "pin", "aria-label": "PIN del dueño" }) : null;
  const cantidad = maximo ? h("input", { inputmode: "numeric", value: "1", "aria-label": "Unidades a devolver" }) : null;
  const confirmar = h("button", { class: "boton primario", type: "submit" }, boton);
  const form = h("form", { class: "motivo", onsubmit: async (e) => {
    e.preventDefault();
    const texto = motivo.value.trim();
    if (texto.length < 3) { aviso("Escriba el motivo (al menos 3 letras).", true); motivo.focus(); return; }
    let n = null;
    if (cantidad) {
      try { n = leerEntero(cantidad.value, "Unidades", 1); } catch (error) { aviso(error.message, true); return; }
      if (n > maximo) { aviso(`Como máximo ${maximo}.`, true); return; }
    }
    if (pinDueno && !/^\d{4,8}$/.test(pinDueno.value)) { aviso("El dueño debe escribir su PIN para autorizar.", true); pinDueno.focus(); return; }
    confirmar.disabled = true;
    try { await alConfirmar(texto, n, pinDueno ? pinDueno.value : undefined); }
    catch (error) { aviso(error.message, true); confirmar.disabled = false; }
  } },
    h("p", { class: "pequeno" }, h("strong", {}, titulo)),
    cantidad ? campo(`Unidades (máximo ${maximo})`, cantidad) : null,
    campo("Motivo", motivo),
    pinDueno ? campo("PIN del dueño (autoriza)", pinDueno) : null,
    h("div", { class: "acciones" }, confirmar, h("button", { class: "boton", type: "button", onclick: alCancelar }, "Cancelar")));
  setTimeout(() => (cantidad || motivo).focus(), 0);
  return form;
}

function hora(iso) { return iso.slice(11, 16); }

function campo(etiqueta, control, ayuda) {
  const id = control.id || (control.id = "c-" + Math.random().toString(36).slice(2, 9));
  return h("div", { class: "campo" }, h("label", { for: id }, etiqueta), control, ayuda ? h("small", {}, ayuda) : null);
}

function cargando() { vista.replaceChildren(h("div", { class: "pagina" }, h("p", { class: "tenue" }, "Cargando…"))); }

function errorVista(error) {
  vista.replaceChildren(pagina("No se pudo cargar", null,
    h("div", { class: "tarjeta" }, h("p", {}, error.message),
      h("button", { class: "boton", onclick: () => mostrar() }, "Reintentar"))));
}

// ---------------------------------------------------------
// Ventas pendientes (sin conexión): se reenvían con el mismo ticket_id,
// así un reintento nunca duplica la venta.
// ---------------------------------------------------------

function ventasPendientes() { return memoria.leer("ventas_pendientes", []); }

async function enviarPendientes() {
  const pendientes = ventasPendientes();
  if (!pendientes.length) return;
  const quedan = [];
  for (const venta of pendientes) {
    try {
      await api("/ventas", { method: "POST", body: venta });
      aviso(`Se envió una venta guardada (${venta.occurred_at.slice(11, 16)}).`);
    } catch (error) {
      if (error.status === 0) { quedan.push(venta); continue; }
      aviso(`Una venta guardada fue rechazada: ${error.message}`, true);
      memoria.guardar("ventas_rechazadas", [...memoria.leer("ventas_rechazadas", []), { ...venta, error: error.message }]);
    }
  }
  memoria.guardar("ventas_pendientes", quedan);
  marcarConexion(true);
}

// ---------------------------------------------------------
// Caja
// ---------------------------------------------------------

// En pantallas táctiles, enfocar el buscador abre el teclado y tapa media pantalla:
// el foco automático solo se usa con ratón/teclado (lector de códigos USB).
const TACTIL = window.matchMedia("(pointer: coarse)").matches;
function enfocar(el) { if (!TACTIL) el.focus(); }

function vistaCaja() {
  let carrito = memoria.leer("carrito", []);
  let resultados = [];
  let temporizador = null;
  let cobrando = false;

  const buscador = h("input", {
    type: "search", class: "grande", placeholder: "Escanee el código o escriba el nombre",
    autocomplete: "off", "aria-label": "Buscar producto", enterkeyhint: "search",
  });
  const lista = h("div", { class: "resultados", role: "list" });
  const lineas = h("ul", { class: "lineas", "aria-label": "Productos del ticket" });
  const total = h("strong", {}, dinero(0));
  const cobrar = h("button", { class: "boton primario grande", onclick: () => cobrarTicket() }, "Cobrar");
  // Móvil: barra fija con el total, para cobrar sin desplazarse hasta el ticket.
  const barraTotal = h("span", {});
  const barraCobrar = h("button", { class: "boton primario", onclick: () => cobrarTicket() }, "Cobrar");
  const barra = h("div", { class: "barra-cobro", hidden: true },
    h("button", { class: "boton", onclick: () => lineas.scrollIntoView({ behavior: "smooth", block: "start" }) }, barraTotal),
    barraCobrar);
  const vaciar = h("button", { class: "boton peligro", onclick: () => { if (confirm("¿Vaciar el ticket?")) { carrito = []; pintarTicket(); } } }, "Vaciar");

  function guardar() { memoria.guardar("carrito", carrito); }

  function agregar(producto) {
    if (!producto.active) { aviso(`${producto.name} está desactivado.`, true); return; }
    const linea = carrito.find((l) => l.sku === producto.sku);
    const cantidad = (linea ? linea.quantity : 0) + 1;
    if (cantidad > producto.stock) aviso(`Solo hay ${producto.stock} de ${producto.name}.`, true);
    if (linea) linea.quantity = cantidad;
    else carrito.push({ sku: producto.sku, name: producto.name, price: producto.price, stock: producto.stock, quantity: 1 });
    pintarTicket();
    buscador.value = "";
    enfocar(buscador);
    masVendidos();
  }

  function pintarResultados(items) {
    resultados = items;
    lista.replaceChildren(...items.slice(0, 24).map((p) => h("button", {
      class: "producto-boton", role: "listitem", onclick: () => agregar(p),
      "aria-disabled": p.stock === 0 ? "true" : null,
    }, h("strong", {}, p.name), h("span", { class: "precio" }, dinero(p.price)),
      h("span", { class: "tenue pequeno" }, p.stock === 0 ? "Sin stock" : unidades(p.stock, p.unit)))));
    if (buscador.value.trim() && !items.length) lista.replaceChildren(h("p", { class: "tenue" }, "Ningún producto coincide."));
  }

  async function masVendidos() {
    try {
      const items = await api("/productos?top=1");
      if (!buscador.value.trim()) {
        pintarResultados(items);
        if (items.length) lista.prepend(h("p", { class: "tenue pequeno", style: "grid-column:1/-1;margin:0" }, "Más vendidos (30 días)"));
      }
    } catch { /* sin conexión: la caja sigue con el buscador */ }
  }

  async function buscar() {
    const texto = buscador.value.trim();
    if (!texto) { masVendidos(); return; }
    try { pintarResultados(await api(`/productos?q=${encodeURIComponent(texto)}`)); }
    catch (error) { aviso(error.message, true); }
  }

  buscador.addEventListener("input", () => { clearTimeout(temporizador); temporizador = setTimeout(buscar, 150); });
  buscador.addEventListener("keydown", async (evento) => {
    if (evento.key !== "Enter") return;
    evento.preventDefault();
    clearTimeout(temporizador);
    const texto = buscador.value.trim();
    if (!texto) { if (carrito.length) cobrarTicket(); return; }
    try {
      agregar(await api(`/buscar?codigo=${encodeURIComponent(texto)}`));
    } catch (error) {
      if (error.status !== 404) { aviso(error.message, true); return; }
      await buscar();
      if (resultados.length === 1) agregar(resultados[0]);
      else if (!resultados.length) aviso(`No hay ningún producto con «${texto}».`, true);
    }
  });

  function pintarTicket() {
    guardar();
    let suma = 0;
    lineas.replaceChildren(...carrito.map((linea, i) => {
      suma += linea.price * linea.quantity;
      const cantidad = h("input", {
        type: "number", min: "1", inputmode: "numeric", value: linea.quantity, "aria-label": `Cantidad de ${linea.name}`,
        onchange: (e) => {
          const n = Number(e.target.value);
          if (Number.isInteger(n) && n > 0) { linea.quantity = n; } else e.target.value = linea.quantity;
          pintarTicket();
        },
      });
      return h("li", { class: "linea" },
        h("span", { class: "nombre" }, linea.name),
        h("span", { class: "importe" }, dinero(linea.price * linea.quantity)),
        h("span", { class: "cantidad" },
          h("button", { class: "boton icono", "aria-label": `Quitar uno de ${linea.name}`,
            onclick: () => { linea.quantity > 1 ? linea.quantity-- : carrito.splice(i, 1); pintarTicket(); } }, "−"),
          cantidad,
          h("button", { class: "boton icono", "aria-label": `Añadir uno de ${linea.name}`,
            onclick: () => { linea.quantity++; pintarTicket(); } }, "+")),
        h("span", { class: "tenue pequeno" }, `${dinero(linea.price)} c/u` +
          (linea.quantity > linea.stock ? ` · solo hay ${linea.stock}` : "")));
    }));
    if (!carrito.length) lineas.replaceChildren(h("li", { class: "vacio" }, "El ticket está vacío. Escanee o busque un producto."));
    total.textContent = dinero(suma);
    cobrar.disabled = !carrito.length || cobrando;
    vaciar.disabled = !carrito.length;
    cobrar.textContent = carrito.length ? `Cobrar ${dinero(suma)}` : "Cobrar";
    const piezas = carrito.reduce((s, l) => s + l.quantity, 0);
    barra.hidden = !carrito.length;
    barraTotal.textContent = `Ticket (${piezas}) · ${dinero(suma)}`;
    barraCobrar.disabled = cobrando;
  }

  async function cobrarTicket() {
    if (!carrito.length || cobrando) return;
    cobrando = true;
    const venta = { ticket_id: uuid(), occurred_at: ahora(),
      lines: carrito.map((l) => ({ sku: l.sku, quantity: l.quantity })),
      expected_total_minor: carrito.reduce((s, l) => s + l.price * l.quantity, 0) };
    pintarTicket();
    try {
      const recibo = await api("/ventas", { method: "POST", body: venta });
      aviso(`Venta registrada: ${dinero(recibo.total_minor)}.`);
      if (recibo.low_stock.length) {
        const nombres = carrito.filter((l) => recibo.low_stock.includes(l.sku)).map((l) => l.name);
        aviso(`Stock bajo: ${nombres.join(", ")}.`, true);
      }
      carrito = [];
    } catch (error) {
      if (error.status === 0) {
        // Se guarda con el precio que realmente se cobró en cada línea: si luego cambia
        // un precio, la venta se registra igual por lo cobrado.
        const { expected_total_minor, ...guardada } = venta;
        guardada.lines = carrito.map((l) => ({ sku: l.sku, quantity: l.quantity, unit_price_minor: l.price }));
        memoria.guardar("ventas_pendientes", [...ventasPendientes(), guardada]);
        aviso("Sin conexión: la venta quedó guardada en este dispositivo y se enviará sola al reconectar.", true);
        carrito = [];
      } else if (error.status === 409 && error.datos && error.datos.prices) {
        for (const l of carrito) if (l.sku in error.datos.prices) l.price = error.datos.prices[l.sku];
        aviso(`Los precios cambiaron. El total correcto es ${dinero(error.datos.total_minor)}: revíselo y cobre de nuevo.`, true);
      } else aviso(error.message, true);
    } finally {
      cobrando = false;
      pintarTicket();
      if (!buscador.value.trim()) masVendidos();  // stock actualizado en los botones
      enfocar(buscador);
    }
  }

  function atajo(evento) { if (evento.key === "F2") { evento.preventDefault(); cobrarTicket(); } }
  document.addEventListener("keydown", atajo);
  alSalir.push(() => document.removeEventListener("keydown", atajo));

  vista.replaceChildren(h("div", { class: "pagina" },
    h("div", { class: "caja" },
      h("section", { "aria-label": "Buscar productos" }, buscador,
        // Arriba y visible también en el móvil (el ticket queda debajo de los productos).
        h("div", { class: "fila-caja" }, h("a", { class: "boton", href: "#/ventas" }, "Ventas de hoy · devolver o anular")),
        h("p", { class: "tenue pequeno solo-teclado" }, "Con un lector de código de barras basta con escanear. Enter añade el producto; Enter con la búsqueda vacía o F2 cobra."),
        lista),
      h("aside", { class: "tarjeta ticket", "aria-label": "Ticket" },
        h("h2", {}, "Ticket"), lineas,
        h("div", { class: "total" }, h("span", {}, "Total"), total),
        cobrar, h("div", { class: "acciones" }, vaciar))), barra));
  pintarTicket();
  masVendidos();
  enfocar(buscador);
}

// ---------------------------------------------------------
// Productos
// ---------------------------------------------------------

function etiquetaStock(p) {
  if (p.stock === 0) return h("span", { class: "etiqueta-estado alta" }, "Sin stock");
  if (p.stock <= p.minimum) return h("span", { class: "etiqueta-estado media" }, "Bajo mínimo");
  return h("span", { class: "etiqueta-estado ok" }, "Bien");
}

async function vistaProductos() {
  const buscador = h("input", { type: "search", placeholder: "Buscar por nombre, SKU o código", "aria-label": "Buscar productos" });
  const inactivos = h("input", { type: "checkbox", id: "ver-inactivos" });
  const cuerpo = h("tbody");
  const resumen = h("p", { class: "tenue pequeno" });
  let temporizador;

  async function cargar() {
    const q = encodeURIComponent(buscador.value.trim());
    const items = await api(`/productos?q=${q}${inactivos.checked ? "&inactivos=1" : ""}`);
    resumen.textContent = plural(items.length, "producto", "productos") + (items.length === 500 ? " (se muestran los primeros 500)" : "");
    const abrir = (p) => { location.hash = `#/producto/${encodeURIComponent(p.sku)}`; };
    cuerpo.replaceChildren(...items.map((p) => h("tr", esCajero() ? {} : {
      class: "clic", tabindex: "0", onclick: () => abrir(p), onkeydown: (e) => { if (e.key === "Enter") abrir(p); },
    },
      h("td", {}, h("strong", {}, p.name), p.active ? null : h("span", { class: "tenue" }, " (desactivado)"),
        h("div", { class: "tenue pequeno" }, p.sku, p.barcode ? ` · ${p.barcode}` : "")),
      h("td", { class: "num" }, `${p.stock}`), h("td", { class: "num ocultar-movil" }, `${p.minimum}`),
      h("td", { class: "num" }, dinero(p.price)), h("td", {}, etiquetaStock(p)))));
    if (!items.length) cuerpo.replaceChildren(h("tr", {}, h("td", { colspan: "5", class: "vacio" },
      buscador.value ? "Ningún producto coincide." : "Todavía no hay productos. Cree el primero o importe su lista desde un CSV.")));
  }

  buscador.addEventListener("input", () => { clearTimeout(temporizador); temporizador = setTimeout(() => cargar().catch((e) => aviso(e.message, true)), 200); });
  inactivos.addEventListener("change", () => cargar().catch((e) => aviso(e.message, true)));
  vista.replaceChildren(pagina("Productos",
    h("div", { class: "acciones", "data-dueno": "" },
      h("a", { class: "boton", href: "#/ajustes" }, "Importar CSV"),
      h("a", { class: "boton primario", href: "#/nuevo" }, "Nuevo producto")),
    h("div", { class: "tarjeta" },
      h("div", { class: "filtros" }, buscador,
        h("label", { class: "casilla", for: "ver-inactivos" }, inactivos, "Ver desactivados")),
      resumen,
      h("div", { class: "tabla-desplazable" }, h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "Producto"), h("th", { class: "num" }, "Stock"),
          h("th", { class: "num ocultar-movil" }, "Mínimo"), h("th", { class: "num" }, "Precio"), h("th", {}, "Estado"))),
        cuerpo)))));
  await cargar();
  enfocar(buscador);
}

function formularioProducto(producto, alGuardar) {
  const p = producto || { name: "", sku: "", barcode: "", category: "tienda", unit: "unidad", price: 0, cost: 0,
    minimum: 0, lead_days: 3, pack: 1, moq: 1, active: 1 };
  const nuevo = !producto;
  const c = {
    name: h("input", { value: p.name, required: true, maxlength: "120", autocomplete: "off" }),
    sku: h("input", { value: p.sku, required: true, maxlength: "100", autocomplete: "off" }),
    barcode: h("input", { value: p.barcode || "", maxlength: "64", autocomplete: "off", inputmode: "numeric" }),
    category: h("select", {}, Object.entries(CATEGORIAS).map(([v, t]) => h("option", { value: v, selected: v === p.category }, t))),
    unit: h("input", { value: p.unit, maxlength: "20" }),
    price: h("input", { value: nuevo ? "" : pesosEditables(p.price), inputmode: "decimal", required: true }),
    cost: h("input", { value: nuevo ? "" : pesosEditables(p.cost), inputmode: "decimal", required: true }),
    minimum: h("input", { value: p.minimum, inputmode: "numeric" }),
    initial: h("input", { value: "0", inputmode: "numeric" }),
    lead_days: h("input", { value: p.lead_days, inputmode: "numeric" }),
    pack: h("input", { value: p.pack, inputmode: "numeric" }),
    moq: h("input", { value: p.moq, inputmode: "numeric" }),
    active: h("input", { type: "checkbox", checked: Boolean(p.active), id: "producto-activo" }),
  };
  if (!nuevo) c.sku.disabled = true;
  const margen = h("small", { class: "tenue" });
  function verMargen() {
    try {
      const precio = leerDinero(c.price.value), costo = leerDinero(c.cost.value);
      margen.textContent = precio > 0 ? `Margen bruto: ${dinero(precio - costo)} (${Math.round((precio - costo) * 100 / precio)} %)` : "";
    } catch { margen.textContent = ""; }
  }
  c.price.addEventListener("input", verMargen);
  c.cost.addEventListener("input", verMargen);
  verMargen();
  const boton = h("button", { class: "boton primario", type: "submit" }, nuevo ? "Crear producto" : "Guardar cambios");
  const form = h("form", {
    onsubmit: async (evento) => {
      evento.preventDefault();
      let datos;
      try {
        datos = {
          name: c.name.value.trim(), barcode: c.barcode.value.trim() || null, category: c.category.value,
          unit: c.unit.value.trim() || "unidad", price: leerDinero(c.price.value), cost: leerDinero(c.cost.value),
          minimum: leerEntero(c.minimum.value, "Mínimo"), lead_days: leerEntero(c.lead_days.value, "Días de entrega"),
          pack: leerEntero(c.pack.value, "Empaque", 1), moq: leerEntero(c.moq.value, "Pedido mínimo", 1),
        };
        if (!datos.name) throw new Error("Escriba el nombre del producto");
        if (nuevo) {
          datos.sku = c.sku.value.trim();
          if (!datos.sku) throw new Error("Escriba el SKU (código interno)");
          datos.initial_stock = leerEntero(c.initial.value, "Stock inicial");
        } else datos.active = c.active.checked;
      } catch (error) { aviso(error.message, true); return; }
      boton.disabled = true;
      try {
        const guardado = nuevo ? await api("/productos", { method: "POST", body: datos })
          : await api(`/productos/${encodeURIComponent(p.sku)}`, { method: "PATCH", body: datos });
        aviso(nuevo ? "Producto creado." : "Cambios guardados.");
        alGuardar(guardado);
      } catch (error) { aviso(error.message, true); }
      finally { boton.disabled = false; }
    },
  },
    campo("Nombre", c.name),
    h("div", { class: "fila-campos" },
      campo("SKU (código interno)", c.sku, nuevo ? "No se puede cambiar después." : null),
      campo("Código de barras", c.barcode, "Opcional. Escanéelo aquí.")),
    h("div", { class: "fila-campos" },
      campo("Precio de venta", c.price, "En pesos, p. ej. 5.000"), campo("Costo", c.cost)),
    margen,
    h("div", { class: "fila-campos" },
      campo("Tipo de producto", c.category, "Cambia cuándo se considera «sin movimiento»."),
      campo("Unidad", c.unit)),
    h("div", { class: "fila-campos" },
      campo("Stock mínimo", c.minimum, "Avisa al bajar de aquí."),
      nuevo ? campo("Stock inicial", c.initial) : null),
    h("details", {}, h("summary", {}, "Compras al proveedor (opcional)"),
      h("div", { class: "fila-campos" },
        campo("Días de entrega", c.lead_days), campo("Empaque (unidades)", c.pack), campo("Pedido mínimo", c.moq))),
    nuevo ? null : h("label", { class: "casilla", for: "producto-activo" }, c.active, "Activo (desmárquelo para ocultarlo en la caja)"),
    h("div", { class: "acciones" }, boton));
  return form;
}

function filaMovimiento(m, recargar) {
  const acciones = h("td", { class: "acciones-fila" });
  const fila = h("tr", { class: m.voided ? "anulado" : null },
    h("td", {}, dia(m.day)),
    h("td", {}, TIPOS[m.kind], m.voided ? h("span", { class: "etiqueta-estado media" }, " anulado") : null,
      m.note ? h("div", { class: "tenue pequeno" }, m.note) : null,
      m.user ? h("div", { class: "tenue pequeno" }, `por ${m.user}` + (m.authorized_by && m.authorized_by !== m.user ? ` · autorizó ${m.authorized_by}` : "")) : null),
    h("td", { class: "num" }, (m.stock_delta >= 0 ? "+" : "−") + Math.abs(m.stock_delta)),
    h("td", { class: "num ocultar-movil" }, m.stock_after), acciones);
  if (m.kind === "sale" && m.ticket_id) {
    acciones.append(h("a", { class: "pequeno", href: `#/ventas/${m.day}` }, "Ver venta"));
  } else if ((m.kind === "receive" || m.kind === "waste" || m.kind === "return") && !m.voided) {
    acciones.append(h("button", { class: "boton", onclick: () => {
      const celda = h("td", { colspan: "5" });
      const extra = h("tr", {}, celda);
      celda.append(formularioMotivo({
        titulo: `Anular ${TIPOS[m.kind].toLowerCase()} de ${Math.abs(m.stock_delta)} (${dia(m.day)}). Queda en el historial.`,
        boton: "Anular", alCancelar: () => extra.remove(),
        alConfirmar: async (motivo) => {
          const r = await api("/anulaciones", { method: "POST", body: { command_id: uuid(), seq: m.seq, reason: motivo, occurred_at: ahora() } });
          aviso(`Anulado. Stock: ${r.stock}.`);
          actualizarContador();
          await recargar();
        },
      }));
      fila.after(extra);
    } }, "Anular"));
  }
  return fila;
}

// ---------------------------------------------------------
// Ventas: historial de tickets, devoluciones y anulaciones
// ---------------------------------------------------------

async function vistaVentas(diaElegido) {
  const hoy = estado.today;
  const elegido = diaElegido || hoy;
  const tickets = await api(`/ventas?dia=${encodeURIComponent(elegido)}&limite=200`);
  const selector = h("input", { type: "date", value: elegido, max: hoy, "aria-label": "Día" });
  selector.addEventListener("change", () => { if (selector.value) location.hash = `#/ventas/${selector.value}`; });
  const estados = { vigente: ["ok", "Vigente"], con_devoluciones: ["media", "Con devoluciones"], anulado: ["alta", "Anulada"] };
  const recargar = () => vistaVentas(elegido);

  const tarjetas = tickets.map((t) => {
    const zona = h("div", {});
    const activas = t.lines.filter((l) => l.active > 0);
    const [nivel, texto] = estados[t.status];
    return h("section", { class: "tarjeta" },
      h("div", { class: "cabecera", style: "margin-bottom:6px" },
        h("h2", { style: "margin:0;flex:1" }, `${hora(t.occurred_at)} · ${dinero(t.net_minor)}`,
          t.user ? h("span", { class: "tenue pequeno" }, ` · ${t.user}`) : null),
        h("span", { class: "etiqueta-estado " + nivel }, texto)),
      t.net_minor !== t.total_minor ? h("p", { class: "tenue pequeno" }, `Cobrado al vender: ${dinero(t.total_minor)}`) : null,
      h("ul", { class: "lineas" }, t.lines.map((l) => h("li", { class: "linea" },
        h("span", { class: "nombre" }, `${l.quantity} × ${l.name}`),
        h("span", { class: "importe" }, dinero(l.unit_price_minor * l.quantity)),
        h("span", { class: "tenue pequeno" }, l.voided ? "Anulada" : l.returned ? `Devueltas ${l.returned}` : `${dinero(l.unit_price_minor)} c/u`),
        l.active > 0 ? h("span", {}, h("button", { class: "boton", onclick: () => {
          zona.replaceChildren(formularioMotivo({
            titulo: `Devolver ${l.name}`, boton: "Registrar devolución", maximo: l.active,
            alCancelar: () => zona.replaceChildren(),
            alConfirmar: async (motivo, n, pin) => {
              const r = await api("/devoluciones", { method: "POST", body: { command_id: uuid(), seq: l.seq, quantity: n, reason: motivo, occurred_at: ahora(), pin_dueno: pin } });
              aviso(`Devolución registrada: ${dinero(-r.revenue_minor)} a devolver al cliente. Stock: ${r.stock}.`);
              await recargar();
            },
          }));
        } }, "Devolver")) : h("span", {})))),
      activas.length ? h("div", { class: "acciones" }, h("button", { class: "boton peligro", onclick: () => {
        zona.replaceChildren(formularioMotivo({
          titulo: "Anular la venta completa (para ventas registradas por error). Para un cliente que trae productos, use «Devolver».",
          boton: "Anular venta", alCancelar: () => zona.replaceChildren(),
          alConfirmar: async (motivo, _n, pin) => {
            const r = await api("/anulaciones/ticket", { method: "POST", body: { command_id: uuid(), ticket_id: t.ticket_id, reason: motivo, occurred_at: ahora(), pin_dueno: pin } });
            aviso(`Venta anulada (${dinero(-r.revenue_minor)}).`);
            actualizarContador();
            await recargar();
          },
        }));
      } }, "Anular venta")) : null,
      zona);
  });
  const total = tickets.reduce((s, t) => s + t.net_minor, 0);
  vista.replaceChildren(pagina("Ventas", h("div", { class: "acciones", style: "margin:0" }, selector,
      h("a", { class: "boton", href: "#/caja" }, "Ir a la caja")),
    h("p", { class: "tenue" }, tickets.length
      ? `${plural(tickets.length, "venta", "ventas")} el ${dia(elegido, true)} · neto ${dinero(total)}`
      : `No hay ventas el ${dia(elegido, true)}.`),
    h("div", { class: "rejilla" }, tarjetas)));
}

function vistaNuevo() {
  vista.replaceChildren(pagina("Nuevo producto", h("a", { class: "boton", href: "#/productos" }, "Volver"),
    h("div", { class: "tarjeta" }, formularioProducto(null, (p) => { location.hash = `#/producto/${encodeURIComponent(p.sku)}`; }))));
  vista.querySelector("input").focus();
}

async function vistaProducto(sku) {
  const datos = await api(`/productos/${encodeURIComponent(sku)}`);
  const p = datos.product;
  const compra = datos.purchase, rot = datos.rotation, dem = datos.demand;

  const tipo = h("select", {}, h("option", { value: "receive" }, "Entrada de mercancía"),
    h("option", { value: "waste" }, "Merma o pérdida"), h("option", { value: "count" }, "Conteo físico (corregir stock)"));
  const cantidad = h("input", { inputmode: "numeric", autocomplete: "off" });
  const nota = h("input", { maxlength: "200", autocomplete: "off", placeholder: "Opcional: proveedor, motivo…" });
  const etiquetaCantidad = h("label", { for: "mov-cantidad" }, "Cantidad");
  cantidad.id = "mov-cantidad";
  tipo.addEventListener("change", () => {
    etiquetaCantidad.textContent = tipo.value === "count" ? "Unidades contadas" : "Cantidad";
    nota.disabled = tipo.value === "count";
  });
  const registrar = h("button", { class: "boton primario", type: "submit" }, "Registrar");
  const formMov = h("form", {
    onsubmit: async (e) => {
      e.preventDefault();
      let n;
      try { n = leerEntero(cantidad.value, "Cantidad", tipo.value === "count" ? 0 : 1); } catch (error) { aviso(error.message, true); return; }
      // Un dedo de más (24 → 2424) no se deshace fácil: se confirma lo que parece un error.
      if (n >= 1000 && !confirm(`¿Seguro? Va a registrar ${pesos.format(n)} ${p.unit === "unidad" ? "unidades" : p.unit}.`)) return;
      registrar.disabled = true;
      try {
        if (tipo.value === "count") {
          const r = await api("/conteos", { method: "POST", body: { command_id: uuid(), sku: p.sku, counted: n, occurred_at: ahora() } });
          aviso(r.changed ? `Stock corregido a ${r.product.stock}.` : "El conteo coincide: no hubo cambios.");
        } else {
          const r = await api("/movimientos", { method: "POST", body: { command_id: uuid(), sku: p.sku, kind: tipo.value,
            quantity: n, occurred_at: ahora(), note: nota.value.trim() || null } });
          aviso(`${TIPOS[tipo.value]} registrada. Stock: ${r.stock}.`);
        }
        actualizarContador();
        await vistaProducto(sku);
      } catch (error) { aviso(error.message, true); registrar.disabled = false; }
    },
  }, h("div", { class: "fila-campos" }, campo("Tipo", tipo),
    h("div", { class: "campo" }, etiquetaCantidad, cantidad)), campo("Nota", nota), registrar);

  const nivelRot = { normal: "ok", sin_stock: "alta", vencido: "alta", riesgo_vencimiento: "media",
    datos_insuficientes: "", congelado: "media", exceso: "baja" }[rot.status];

  vista.replaceChildren(pagina(p.name, h("a", { class: "boton", href: "#/productos" }, "Volver"),
    h("div", { class: "cifras" },
      h("div", { class: "tarjeta cifra" }, h("div", { class: "valor" }, unidades(p.stock, p.unit)), h("div", { class: "etiqueta" }, "En stock "), etiquetaStock(p)),
      h("div", { class: "tarjeta cifra" }, h("div", { class: "valor" }, dinero(p.price)), h("div", { class: "etiqueta" }, `Precio · costo ${dinero(p.cost)}`)),
      h("div", { class: "tarjeta cifra" }, h("div", { class: "valor" }, dinero(p.stock * p.cost)), h("div", { class: "etiqueta" }, "Capital en stock (a costo)")),
      h("div", { class: "tarjeta cifra" }, h("div", { class: "valor" }, dem.observed_days ? dem.daily_mean.toLocaleString("es-CO", { maximumFractionDigits: 1 }) : "—"),
        h("div", { class: "etiqueta" }, `Venta diaria estimada · ${plural(dem.observed_days, "día", "días")} con datos`))),
    h("div", { class: "rejilla dos" },
      h("section", { class: "tarjeta" }, h("h2", {}, "Registrar movimiento"), formMov),
      h("section", { class: "tarjeta" }, h("h2", {}, "Análisis"),
        h("p", {}, h("strong", {}, compra.quantity ? `Conviene pedir ${unidades(compra.quantity, p.unit)}` : "No hace falta pedir ahora"),
          compra.quantity ? ` (≈ ${dinero(compra.estimated_cost_minor)})` : "",
          h("span", { class: "etiqueta-estado" + (compra.confidence === "media" ? " ok" : "") }, ` confianza ${compra.confidence}`)),
        h("ul", { class: "tenue pequeno" }, compra.reason_text.map((r) => h("li", {}, r))),
        h("p", {}, "Rotación: ", h("span", { class: "etiqueta-estado " + nivelRot }, rot.status_text),
          ` · ${plural(rot.days_without_sale, "día abierto", "días abiertos")} sin vender` +
          (rot.coverage_days !== null ? ` · stock para ${plural(Math.round(rot.coverage_days), "día", "días")}` : "")),
        rot.frozen_capital_minor ? h("p", { class: "pequeno" }, `Capital quieto: ${dinero(rot.frozen_capital_minor)}. Considere promoción o devolución al proveedor.`) : null,
        h("p", { class: "tenue pequeno" }, "Las sugerencias son borradores para revisar; no se envían a ningún proveedor."))),
    h("section", { class: "tarjeta", style: null }, h("h2", {}, "Datos del producto"),
      formularioProducto(p, () => vistaProducto(sku))),
    h("section", { class: "tarjeta" }, h("h2", {}, "Últimos movimientos"),
      datos.movements.length ? h("div", { class: "tabla-desplazable" }, h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "Día"), h("th", {}, "Tipo"), h("th", { class: "num" }, "Cantidad"),
          h("th", { class: "num ocultar-movil" }, "Stock después"), h("th", {}, ""))),
        h("tbody", {}, datos.movements.map((m) => filaMovimiento(m, () => vistaProducto(sku))))))
        : h("p", { class: "tenue" }, "Sin movimientos todavía."))));
  for (const s of vista.querySelectorAll("section.tarjeta")) s.style.marginTop = "14px";
}

// ---------------------------------------------------------
// Alertas y compras
// ---------------------------------------------------------

async function vistaAlertas() {
  const [alertas, compras] = await Promise.all([api("/alertas"), api("/compras")]);
  const nivel = { alta: "Urgente", media: "Revisar", baja: "Sugerencia" };
  vista.replaceChildren(pagina("Alertas y compras", h("button", { class: "boton no-imprimir", onclick: () => print() }, "Imprimir"),
    h("div", { class: "rejilla dos" },
      h("section", { class: "tarjeta" }, h("h2", {}, `Alertas (${alertas.length})`),
        alertas.length ? alertas.map((a) => h("div", { class: "alerta" },
          h("span", { class: "etiqueta-estado " + a.level }, nivel[a.level]),
          h("a", { href: `#/producto/${encodeURIComponent(a.sku)}` }, a.text,
            a.capital_minor ? ` Capital quieto: ${dinero(a.capital_minor)}.` : "")))
          : h("p", { class: "vacio" }, "Todo en orden: no hay alertas.")),
      h("section", { class: "tarjeta" }, h("h2", {}, "Pedido sugerido (borrador)"),
        compras.lines.length ? [
          h("div", { class: "tabla-desplazable" }, h("table", {},
            h("thead", {}, h("tr", {}, h("th", {}, "Producto"), h("th", { class: "num" }, "Hay"),
              h("th", { class: "num" }, "Pedir"), h("th", { class: "num" }, "Costo aprox."))),
            h("tbody", {}, compras.lines.map((l) => h("tr", {},
              h("td", {}, h("a", { href: `#/producto/${encodeURIComponent(l.sku)}` }, l.name),
                h("div", { class: "tenue pequeno" }, l.reason_text[0] || "")),
              h("td", { class: "num" }, l.stock), h("td", { class: "num" }, h("strong", {}, l.quantity)),
              h("td", { class: "num" }, dinero(l.estimated_cost_minor))))))),
          h("div", { class: "total" }, h("span", {}, "Total estimado"), h("strong", {}, dinero(compras.total_minor))),
          h("p", { class: "tenue pequeno" }, compras.note)]
          : h("p", { class: "vacio" }, "No hay compras sugeridas por ahora.")))));
}

// ---------------------------------------------------------
// Resumen
// ---------------------------------------------------------

async function vistaResumen(desde) {
  const [tablero, semana] = await Promise.all([api("/tablero"), api(`/resumen${desde ? `?desde=${desde}` : ""}`)]);
  const hoy = tablero.today_summary;
  const maximo = Math.max(1, ...semana.by_day.map((d) => d.revenue_minor));
  const mover = (dias) => {
    const d = new Date(semana.start_day + "T12:00:00");
    d.setDate(d.getDate() + dias);
    vistaResumen(d.toISOString().slice(0, 10)).catch(errorVista);
  };
  const fin = new Date(semana.end_day_exclusive + "T12:00:00"); fin.setDate(fin.getDate() - 1);
  const margen = semana.margin_ratio === null ? "—" : `${(semana.margin_ratio * 100).toFixed(1)} %`;
  vista.replaceChildren(pagina("Resumen", null,
    h("h2", {}, "Hoy"),
    h("div", { class: "cifras" },
      h("div", { class: "tarjeta cifra" }, h("div", { class: "valor" }, dinero(hoy.revenue_minor)), h("div", { class: "etiqueta" }, `Ventas · ${hoy.sold_units} unidades`)),
      h("div", { class: "tarjeta cifra" }, h("div", { class: "valor" }, dinero(hoy.contribution_minor)), h("div", { class: "etiqueta" }, "Ganancia bruta (ventas − costo − merma)")),
      h("div", { class: "tarjeta cifra" }, h("div", { class: "valor" }, dinero(tablero.stock_value_minor)), h("div", { class: "etiqueta" }, `Inventario a costo · ${tablero.products} productos`)),
      h("a", { class: "tarjeta cifra", href: "#/alertas", style: "text-decoration:none;color:inherit" },
        h("div", { class: "valor" }, String(tablero.alert_count)), h("div", { class: "etiqueta" }, "Alertas abiertas"))),
    h("section", { class: "tarjeta" },
      h("div", { class: "cabecera" }, h("h2", { style: "margin:0;flex:1" }, `Semana del ${dia(semana.start_day)} al ${dia(fin.toISOString().slice(0, 10))}`),
        h("div", { class: "acciones", style: "margin:0" },
          h("button", { class: "boton", onclick: () => mover(-7), "aria-label": "Semana anterior" }, "← Anterior"),
          h("button", { class: "boton", onclick: () => mover(7), "aria-label": "Semana siguiente",
            disabled: semana.end_day_exclusive > tablero.today }, "Siguiente →"))),
      h("div", { class: "cifras" },
        h("div", { class: "cifra" }, h("div", { class: "valor" }, dinero(semana.revenue_minor)), h("div", { class: "etiqueta" }, "Ventas")),
        h("div", { class: "cifra" }, h("div", { class: "valor" }, dinero(semana.cogs_minor)), h("div", { class: "etiqueta" }, "Costo de lo vendido")),
        h("div", { class: "cifra" }, h("div", { class: "valor" }, dinero(semana.waste_minor)), h("div", { class: "etiqueta" }, "Merma")),
        h("div", { class: "cifra" }, h("div", { class: "valor" }, dinero(semana.contribution_minor)), h("div", { class: "etiqueta" }, `Ganancia bruta · margen ${margen}`))),
      h("div", { class: "barras", role: "img", "aria-label": "Ventas por día de la semana" },
        semana.by_day.map((d) => h("div", { class: "dia", title: `${dia(d.start_day, true)}: ${dinero(d.revenue_minor)}` },
          h("small", {}, d.revenue_minor ? pesos.format(Math.round(d.revenue_minor / 100000)) + " mil" : ""),
          h("div", { class: "barra-dia", style: `height:${Math.round(d.revenue_minor * 100 / maximo)}%` }),
          h("small", {}, dia(d.start_day))))),
      h("p", { class: "tenue pequeno" }, "La ganancia bruta no incluye arriendo, nómina ni impuestos: no es la utilidad contable."))));
}

// ---------------------------------------------------------
// Ajustes
// ---------------------------------------------------------

async function vistaAjustes() {
  const [ajustes, respaldos] = await Promise.all([api("/ajustes"), api("/respaldo")]);
  const nombre = h("input", { value: ajustes.store_name, maxlength: "80" });
  const tarjetas = [];

  tarjetas.push(h("section", { class: "tarjeta" }, h("h2", {}, "Comercio"),
    h("form", { onsubmit: async (e) => {
      e.preventDefault();
      try { await api("/ajustes", { method: "PATCH", body: { store_name: nombre.value } }); aviso("Guardado."); await cargarEstado(); }
      catch (error) { aviso(error.message, true); }
    } }, campo("Nombre del comercio", nombre), h("button", { class: "boton primario", type: "submit" }, "Guardar"))));

  tarjetas.push(await tarjetaLicencia());
  tarjetas.push(await tarjetaUsuarios());

  if (estado.local) {
    const remoto = h("input", { type: "checkbox", id: "remoto", checked: ajustes.remote_enabled });
    remoto.addEventListener("change", async () => {
      try { await api("/ajustes", { method: "PATCH", body: { remote_enabled: remoto.checked } }); await vistaAjustes(); }
      catch (error) { aviso(error.message, true); remoto.checked = !remoto.checked; }
    });
    const enlaces = ajustes.addresses.map((url) => `${url}/?clave=${encodeURIComponent(ajustes.access_key)}`);
    tarjetas.push(h("section", { class: "tarjeta" }, h("h2", {}, "Celulares y otros equipos"),
      h("p", { class: "tenue" }, "Los celulares y tabletas en la misma red Wi-Fi pueden usar la caja y ver el inventario. Este equipo guarda los datos: debe estar encendido."),
      h("label", { class: "casilla", for: "remoto" }, remoto, "Permitir el acceso desde otros dispositivos"),
      ajustes.remote_enabled ? [
        h("p", {}, "Abra este enlace en el celular (o péguelo en la app Inventario):"),
        enlaces.length ? enlaces.map((enlace) => h("div", { class: "acciones" },
          h("div", { class: "clave", style: "flex:1" }, enlace),
          h("button", { class: "boton", onclick: () => copiar(enlace) }, "Copiar")))
          : h("p", { class: "tenue" }, "No se detectó una red local. Conecte el equipo al Wi-Fi."),
        h("p", { class: "tenue pequeno" }, `Clave: `, h("span", { class: "clave" }, ajustes.access_key)),
        h("button", { class: "boton peligro", onclick: async () => {
          if (!confirm("Los dispositivos conectados tendrán que volver a entrar con la clave nueva. ¿Cambiarla?")) return;
          await api("/ajustes", { method: "PATCH", body: { new_key: true } }); await vistaAjustes();
        } }, "Cambiar la clave")] : null));
  }

  // Importar
  const archivo = h("input", { type: "file", accept: ".csv,text/csv", id: "archivo-csv" });
  const previa = h("div", { class: "vista-previa" });
  archivo.addEventListener("change", async () => {
    if (!archivo.files[0]) return;
    previa.replaceChildren(h("p", { class: "tenue" }, "Leyendo…"));
    try {
      const plan = await api("/importar", { method: "POST", raw: await archivo.files[0].arrayBuffer() });
      const accion = { nuevo: "Nuevo", actualizar: "Actualizar", conflicto: "No se importará" };
      // replaceChildren pintaría «null» como texto: se filtran los vacíos.
      previa.replaceChildren(...[
        h("p", {}, `${plural(plan.new, "nuevo", "nuevos")}, ${plan.update} a actualizar, ${plan.conflicts + plan.errors.length} con problemas.`),
        plan.errors.length ? h("ul", { class: "pequeno" }, plan.errors.slice(0, 50).map((e) =>
          h("li", {}, `Fila ${e.line}${e.sku ? ` (${e.sku})` : ""}: ${e.error}`))) : null,
        h("p", { class: "tenue pequeno" }, "Al actualizar, el stock no cambia: use el conteo físico de cada producto."),
        h("div", { class: "acciones" }, h("button", { class: "boton primario", disabled: !(plan.new + plan.update), onclick: async (e) => {
          e.target.disabled = true;
          try {
            const r = await api(`/importar?aplicar=1&token=${encodeURIComponent(plan.token)}`, { method: "POST" });
            aviso(`Importación lista: ${plural(r.created, "creado", "creados")}, ${plural(r.updated, "actualizado", "actualizados")}.`);
            previa.replaceChildren(); archivo.value = "";
          } catch (error) { aviso(error.message, true); e.target.disabled = false; }
        } }, "Importar"), h("button", { class: "boton", onclick: () => { previa.replaceChildren(); archivo.value = ""; } }, "Cancelar")),
        h("div", { class: "tabla-desplazable" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", { class: "ocultar-movil" }, "Fila"), h("th", {}, "Producto"), h("th", { class: "num" }, "Precio"),
          h("th", { class: "num ocultar-movil" }, "Stock inicial"), h("th", {}, "Acción"))),
        h("tbody", {}, plan.rows.slice(0, 200).map((r) => h("tr", {}, h("td", { class: "ocultar-movil" }, r.line),
          h("td", {}, r.name, h("div", { class: "tenue pequeno" }, r.sku)), h("td", { class: "num" }, dinero(r.price)),
          h("td", { class: "num ocultar-movil" }, r.action === "nuevo" ? r.initial_stock : "—"),
          h("td", {}, r.error ? `${accion[r.action]}: ${r.error}` : accion[r.action]))))))
      ].filter(Boolean));
    } catch (error) { previa.replaceChildren(h("p", { class: "etiqueta-estado alta" }, error.message)); }
  });
  tarjetas.push(h("section", { class: "tarjeta", id: "importar" }, h("h2", {}, "Importar productos (CSV)"),
    h("p", { class: "tenue pequeno" }, "Columnas obligatorias: sku, precio y costo. Se acepta coma o punto y coma, y precios como 5.000 o 5000. Verá una vista previa antes de importar."),
    h("div", { class: "acciones" }, h("label", { class: "boton", for: "archivo-csv" }, "Elegir archivo…"),
      h("a", { class: "boton", download: "plantilla-productos.csv", href: `${API}/exportar/plantilla-productos.csv` }, "Descargar plantilla")),
    h("div", { class: "oculto" }, archivo), previa));

  tarjetas.push(h("section", { class: "tarjeta" }, h("h2", {}, "Exportar"),
    h("p", { class: "tenue pequeno" }, "Sus datos son suyos: puede llevarlos a Excel cuando quiera."),
    h("div", { class: "acciones" },
      h("a", { class: "boton", href: `${API}/exportar/productos.csv`, download: "productos.csv" }, "Productos (CSV)"),
      h("a", { class: "boton", href: `${API}/exportar/movimientos.csv`, download: "movimientos.csv" }, "Movimientos (CSV)"))));

  tarjetas.push(h("section", { class: "tarjeta" }, h("h2", {}, "Respaldos"),
    h("p", { class: "tenue pequeno" }, "Se hace un respaldo automático cada día al abrir el programa (se guardan los últimos 14). Copie alguno a una memoria USB de vez en cuando.",
      ajustes.data_dir ? ` Carpeta de datos: ${ajustes.data_dir}` : ""),
    h("div", { class: "acciones" }, h("button", { class: "boton", onclick: async () => {
      try { const r = await api("/respaldo", { method: "POST" }); aviso(`Respaldo creado: ${r.file}`); await vistaAjustes(); }
      catch (error) { aviso(error.message, true); }
    } }, "Crear respaldo ahora")),
    h("ul", { class: "pequeno" }, respaldos.slice(0, 10).map((r) => h("li", {},
      h("a", { href: `${API}/respaldo/${encodeURIComponent(r.file)}`, download: r.file }, r.file),
      h("span", { class: "tenue" }, ` · ${(r.bytes / 1024).toFixed(0)} KB`))))));

  tarjetas.push(h("section", { class: "tarjeta" }, h("h2", {}, "Acerca de"),
    h("p", { class: "tenue pequeno" }, `Inventario Inteligente ${estado.version}. Funciona sin internet: los datos se guardan en este equipo.`),
    h("p", { class: "pequeno" }, h("strong", {}, "Importante: "), "este programa no emite factura electrónica ni el documento equivalente POS electrónico de la DIAN. Si su negocio está obligado a facturar, úselo junto con su sistema de facturación autorizado. Consulte a su contador."),
    h("p", { class: "tenue pequeno" }, `Plan: ${estado.plan.name}${estado.plan.licensed ? "" : " (sin licencia comercial: uso no comercial o prueba)"}. `,
      "Documentos: ", h("a", { href: "/legal/CONTRATO-LICENCIA.md", target: "_blank" }, "contrato de licencia"), " · ",
      h("a", { href: "/legal/POLITICA-DATOS.md", target: "_blank" }, "tratamiento de datos"), " · ",
      h("a", { href: "/legal/TERMINOS-VENTA.md", target: "_blank" }, "términos de venta"), ".")));

  vista.replaceChildren(pagina("Ajustes", null, h("div", { class: "rejilla" }, tarjetas)));
}

// Licencia comercial y límites del plan.
async function tarjetaLicencia() {
  const info = await api("/licencia");
  const barra = (usado, limite, texto) => h("div", { class: "uso" },
    h("div", { class: "uso-texto" }, h("span", {}, texto), h("span", { class: "tenue" }, `${pesos.format(usado)} de ${pesos.format(limite)}`)),
    h("div", { class: "uso-barra" }, h("div", { style: `width:${Math.min(100, Math.round(usado * 100 / limite))}%`,
      class: usado >= limite ? "lleno" : "" })));
  const texto = h("textarea", { rows: "3", placeholder: "Pegue aquí la licencia (empieza por INV1.)", autocomplete: "off", spellcheck: "false" });
  const acepto = h("input", { type: "checkbox", id: "acepto-contrato" });
  const activar = h("button", { class: "boton primario", type: "submit" }, info.licensed ? "Cambiar licencia" : "Activar licencia");
  const c = info.claims;
  return h("section", { class: "tarjeta" }, h("h2", {}, "Licencia"),
    h("p", {}, h("strong", {}, `Plan ${info.name}`),
      info.licensed && c ? ` · ${c.titular}${c.vence ? ` · vence el ${c.vence}` : " · perpetua"}` : " · sin licencia comercial"),
    info.warning ? h("p", { class: "etiqueta-estado media" }, info.warning) : null,
    barra(info.usage.products, info.products, "Productos activos"),
    barra(info.usage.users, info.users, "Usuarios activos"),
    h("p", { class: "tenue pequeno" }, "Al llegar a un límite solo se impiden altas nuevas: vender, consultar, devolver y exportar funcionan siempre.",
      info.licensed ? "" : " El modo evaluación sirve para probar el programa o para uso no comercial; en un negocio se requiere una licencia comercial."),
    h("form", { onsubmit: async (e) => {
      e.preventDefault();
      if (!texto.value.trim()) { aviso("Pegue el texto de la licencia.", true); return; }
      if (!acepto.checked) { aviso("Para activar la licencia hay que aceptar el contrato.", true); return; }
      activar.disabled = true;
      try {
        const r = await api("/licencia", { method: "POST", body: { license: texto.value, accept_terms: true } });
        aviso(`Licencia activada: plan ${r.name}.`);
        await cargarEstado(); await vistaAjustes();
      } catch (error) { aviso(error.message, true); activar.disabled = false; }
    } }, campo("Licencia", texto),
      h("label", { class: "casilla", for: "acepto-contrato" }, acepto,
        h("span", {}, "He leído y acepto el ", h("a", { href: "/legal/CONTRATO-LICENCIA.md", target: "_blank" }, "contrato de licencia"),
          ` (versión ${info.terms_version})`)),
      h("div", { class: "acciones" }, activar)),
    info.terms_accepted ? h("p", { class: "tenue pequeno" },
      `Contrato aceptado: versión ${info.terms_accepted.version}, ${info.terms_accepted.at.slice(0, 16).replace("T", " ")}` +
      (info.terms_accepted.user ? ` por ${info.terms_accepted.user}` : "") + ".") : null,
    h("p", { class: "tenue pequeno" }, "Identificador de esta instalación (para licencias ligadas a un equipo): ", h("code", {}, info.installation)));
}

// Usuarios y PIN (solo el dueño, o cualquiera mientras no haya usuarios).
async function tarjetaUsuarios() {
  const pinNuevo = (etiqueta) => h("input", { type: "password", inputmode: "numeric", autocomplete: "new-password",
    maxlength: "8", "aria-label": etiqueta });
  const leerPin = (a, b) => {
    if (!/^\d{4,8}$/.test(a.value)) throw new Error("El PIN debe tener de 4 a 8 dígitos.");
    if (a.value !== b.value) throw new Error("Los dos PIN no coinciden.");
    return a.value;
  };
  if (!estado.users_enabled) {
    const nombre = h("input", { maxlength: "40", autocomplete: "off", placeholder: "Su nombre" });
    const pin = pinNuevo("PIN"), pin2 = pinNuevo("Repita el PIN");
    return h("section", { class: "tarjeta" }, h("h2", {}, "Usuarios y PIN"),
      h("p", { class: "tenue pequeno" }, "Hoy cualquiera que abra el programa puede cambiar precios, ver costos o anular ventas. Al activar el PIN, cada persona entra con el suyo; los cajeros venden, pero para devolver o anular necesitan el PIN del dueño, y queda registrado quién hizo cada cosa."),
      h("form", { onsubmit: async (e) => {
        e.preventDefault();
        try {
          const valor = leerPin(pin, pin2);
          if (!nombre.value.trim()) throw new Error("Escriba su nombre.");
          await api("/usuarios", { method: "POST", body: { name: nombre.value.trim(), role: "dueno", pin: valor } });
          aviso("PIN activado. Ya puede crear los usuarios de caja.");
          await cargarEstado(); await vistaAjustes();
        } catch (error) { aviso(error.message, true); }
      } }, h("div", { class: "fila-campos" }, campo("Nombre del dueño", nombre), campo("PIN (4 a 8 dígitos)", pin),
        campo("Repita el PIN", pin2)),
        h("button", { class: "boton primario", type: "submit" }, "Proteger con PIN")));
  }
  const usuarios = await api("/usuarios");
  const lista = h("ul", { class: "lineas" }, usuarios.map((u) => {
    const zona = h("div", {});
    return h("li", { class: "linea" },
      h("span", { class: "nombre" }, u.name, h("span", { class: "tenue pequeno" }, u.role === "dueno" ? " · dueño" : " · caja"),
        u.active ? null : h("span", { class: "etiqueta-estado media" }, " inactivo")),
      h("span", { class: "acciones", style: "margin:0" },
        h("button", { class: "boton", onclick: () => {
          const a = pinNuevo("PIN nuevo"), b = pinNuevo("Repita el PIN");
          zona.replaceChildren(h("form", { class: "motivo", onsubmit: async (e) => {
            e.preventDefault();
            try { await api(`/usuarios/${u.id}`, { method: "PATCH", body: { pin: leerPin(a, b) } }); aviso(`PIN de ${u.name} cambiado.`); zona.replaceChildren(); }
            catch (error) { aviso(error.message, true); }
          } }, h("div", { class: "fila-campos" }, campo("PIN nuevo", a), campo("Repita el PIN", b)),
            h("div", { class: "acciones" }, h("button", { class: "boton primario", type: "submit" }, "Guardar PIN"),
              h("button", { class: "boton", type: "button", onclick: () => zona.replaceChildren() }, "Cancelar"))));
          enfocar(a);
        } }, "Cambiar PIN"),
        h("button", { class: "boton" + (u.active ? " peligro" : ""), onclick: async () => {
          try { await api(`/usuarios/${u.id}`, { method: "PATCH", body: { active: !u.active } }); await vistaAjustes(); }
          catch (error) { aviso(error.message, true); }
        } }, u.active ? "Desactivar" : "Activar")),
      h("div", { style: "grid-column:1/-1" }, zona));
  }));
  const nombre = h("input", { maxlength: "40", autocomplete: "off" });
  const rol = h("select", {}, h("option", { value: "cajero" }, "Caja (vende; devolver o anular con PIN del dueño)"),
    h("option", { value: "dueno" }, "Dueño (todo)"));
  const pin = pinNuevo("PIN"), pin2 = pinNuevo("Repita el PIN");
  return h("section", { class: "tarjeta" }, h("h2", {}, "Usuarios y PIN"), lista,
    h("form", { style: "margin-top:12px", onsubmit: async (e) => {
      e.preventDefault();
      try {
        const valor = leerPin(pin, pin2);
        await api("/usuarios", { method: "POST", body: { name: nombre.value.trim(), role: rol.value, pin: valor } });
        aviso("Usuario creado."); await vistaAjustes();
      } catch (error) { aviso(error.message, true); }
    } }, h("h2", {}, "Nuevo usuario"),
      h("div", { class: "fila-campos" }, campo("Nombre", nombre), campo("Tipo", rol)),
      h("div", { class: "fila-campos" }, campo("PIN (4 a 8 dígitos)", pin), campo("Repita el PIN", pin2)),
      h("button", { class: "boton primario", type: "submit" }, "Crear usuario")),
    h("p", { class: "tenue pequeno" }, "Si el dueño olvida su PIN: en este equipo, con el programa cerrado, ejecute ",
      h("code", {}, "python3 -m inventario.usuarios restablecer NOMBRE"), "."));
}

async function copiar(texto) {
  try { await navigator.clipboard.writeText(texto); aviso("Copiado."); }
  catch { prompt("Copie el enlace:", texto); }
}

// ---------------------------------------------------------
// Navegación
// ---------------------------------------------------------

const RUTAS = {
  caja: () => vistaCaja(),
  productos: () => vistaProductos(),
  nuevo: () => vistaNuevo(),
  producto: (sku) => vistaProducto(sku),
  ventas: (diaElegido) => vistaVentas(diaElegido),
  alertas: () => vistaAlertas(),
  resumen: () => vistaResumen(),
  ajustes: () => vistaAjustes(),
};
const SECCION = { nuevo: "productos", producto: "productos", ventas: "caja" };

const SOLO_DUENO = new Set(["alertas", "resumen", "ajustes", "producto", "nuevo"]);

async function mostrar() {
  for (const limpiar of alSalir.splice(0)) limpiar();
  if (estado && estado.users_enabled && !estado.user) { vistaEntrada(); return; }
  const [ruta, ...resto] = (location.hash.replace(/^#\/?/, "") || "caja").split("/");
  const nombre = RUTAS[ruta] && !(esCajero() && SOLO_DUENO.has(ruta)) ? ruta : "caja";
  const seccion = SECCION[nombre] || nombre;
  for (const a of document.querySelectorAll(".menu a")) {
    if (a.dataset.seccion === seccion) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  cargando();
  actualizarContador();
  try {
    await RUTAS[nombre](...resto.map(decodeURIComponent));
    document.title = `${vista.querySelector("h1")?.textContent || "Caja"} · Inventario`;
    // Lectores de pantalla y teclado: el foco va al contenido nuevo (la caja enfoca su buscador).
    if (nombre !== "caja" && nombre !== "nuevo" && !vista.contains(document.activeElement)) vista.focus({ preventScroll: true });
  } catch (error) { errorVista(error); }
}

async function actualizarContador() {
  if (esCajero() || (estado && estado.users_enabled && !estado.user)) return;
  try {
    const tablero = await api("/tablero");
    const el = document.getElementById("contador-alertas");
    el.hidden = !tablero.alert_count;
    el.textContent = tablero.alert_count > 99 ? "99+" : String(tablero.alert_count);
    el.setAttribute("aria-label", `${tablero.alert_count} alertas`);
  } catch { /* el estado de conexión ya lo indica */ }
}

async function cargarEstado() {
  estado = await api("/estado");
  document.getElementById("nombre-comercio").textContent = estado.store_name;
  const sinSesion = estado.users_enabled && !estado.user;
  document.body.classList.toggle("sin-sesion", sinSesion);
  document.body.classList.toggle("rol-cajero", esCajero());
  const usuario = document.getElementById("usuario-actual");
  usuario.hidden = !estado.user;
  if (estado.user) {
    usuario.replaceChildren(h("span", {}, estado.user.name, esCajero() ? h("span", { class: "rol" }, " (caja)") : null),
      h("button", { class: "boton", onclick: async () => {
        await api("/sesion/salir", { method: "POST", body: {} }).catch(() => {});
        await cargarEstado(); location.hash = "#/caja"; await mostrar();
      } }, "Salir"));
  }
}

// ---------------------------------------------------------
// Entrada con usuario y PIN
// ---------------------------------------------------------

function vistaEntrada() {
  const zona = h("div", {});
  const elegir = () => zona.replaceChildren(
    h("p", {}, "¿Quién atiende?"),
    h("div", { class: "usuarios-entrada" }, estado.login_users.map((u) => h("button", { class: "boton", onclick: () => pedirPin(u) },
      h("strong", {}, u.name), h("span", { class: "tenue pequeno" }, u.role === "dueno" ? "Dueño" : "Caja")))));
  function pedirPin(u) {
    const pin = h("input", { type: "password", inputmode: "numeric", autocomplete: "off", maxlength: "8", class: "pin grande",
      "aria-label": `PIN de ${u.name}` });
    const entrar = h("button", { class: "boton primario grande", type: "submit" }, "Entrar");
    const teclas = [..."123456789"].map((d) => h("button", { class: "boton", type: "button", onclick: () => { pin.value += d; } }, d));
    zona.replaceChildren(h("form", { onsubmit: async (e) => {
      e.preventDefault();
      entrar.disabled = true;
      try {
        await api("/sesion", { method: "POST", body: { user_id: u.id, pin: pin.value } });
        await cargarEstado();
        location.hash = "#/caja";
        await mostrar();
      } catch (error) { aviso(error.message, true); pin.value = ""; entrar.disabled = false; enfocar(pin); }
    } },
      h("p", {}, h("strong", {}, u.name), " · escriba su PIN"),
      pin,
      h("div", { class: "teclado-pin" }, teclas,
        h("button", { class: "boton", type: "button", onclick: elegir, "aria-label": "Volver" }, "←"),
        h("button", { class: "boton", type: "button", onclick: () => { pin.value += "0"; } }, "0"),
        h("button", { class: "boton", type: "button", onclick: () => { pin.value = pin.value.slice(0, -1); }, "aria-label": "Borrar" }, "⌫")),
      entrar));
    enfocar(pin);
  }
  elegir();
  vista.replaceChildren(h("div", { class: "pagina entrada" }, h("h1", {}, estado.store_name), h("div", { class: "tarjeta" }, zona)));
  document.title = "Entrar · Inventario";
}

async function iniciar() {
  try { await cargarEstado(); }
  catch (error) {
    vista.replaceChildren(pagina("Sin conexión", null, h("div", { class: "tarjeta" }, h("p", {}, error.message),
      h("button", { class: "boton", onclick: () => location.reload() }, "Reintentar"))));
    return;
  }
  window.addEventListener("hashchange", mostrar);
  await mostrar();
  enviarPendientes();
  revisarReloj();
  if (estado.plan && estado.plan.warning && !esCajero() && !(estado.users_enabled && !estado.user)) aviso(estado.plan.warning, true);
  setInterval(() => { cargarEstado().then(() => { revisarReloj(); return enviarPendientes(); }).catch(() => {}); }, 15000);
  // Contador de alertas: al cambiar de sección y cada minuto (no tras cada venta: con
  // catálogos grandes el cálculo cuesta y la caja no lo necesita en ese instante).
  setInterval(actualizarContador, 60000);
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
}

iniciar();

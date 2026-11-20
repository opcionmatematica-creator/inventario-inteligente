// Pantalla de conexión. Habla con la app mediante navegaciones a "inv-app://...",
// que MainActivity intercepta (no hay puente JavaScript -> Java).
(function () {
  'use strict';
  var form = document.getElementById('form');
  var dir = document.getElementById('direccion');
  var clave = document.getElementById('clave');
  var boton = document.getElementById('conectar');
  var usb = document.getElementById('usb');
  var msg = document.getElementById('msg');

  function mostrar(texto, tipo) {
    msg.textContent = texto || '';
    msg.className = texto ? tipo : '';
  }

  function ocupado(si) {
    boton.disabled = si;
    usb.disabled = si;
    boton.textContent = si ? 'Comprobando…' : 'Conectar';
  }

  function enviar(direccion, claveTexto) {
    mostrar('Conectando con el PC…', 'info');
    ocupado(true);
    // Si la app no responde en unos segundos, se libera el botón.
    window.setTimeout(function () {
      if (boton.disabled) {
        ocupado(false);
        mostrar('No hay respuesta. Inténtalo de nuevo.', 'err');
      }
    }, 12000);
    window.location.href = 'inv-app://conectar?direccion=' + encodeURIComponent(direccion) +
      '&clave=' + encodeURIComponent(claveTexto);
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    enviar(dir.value, clave.value);
  });

  usb.addEventListener('click', function () {
    dir.value = 'http://127.0.0.1:8470';
    clave.value = '';
    enviar(dir.value, '');
  });

  // La app rellena los campos con los datos guardados.
  window.invIniciar = function (datos) {
    if (datos.direccion) dir.value = datos.direccion;
    if (datos.clave) clave.value = datos.clave;
  };

  // La app devuelve el resultado de la comprobación (solo llega si falló;
  // si va bien, la app carga directamente la web del PC).
  window.invResultado = function (r) {
    ocupado(false);
    mostrar(r.ok ? '' : r.mensaje, 'err');
  };
})();

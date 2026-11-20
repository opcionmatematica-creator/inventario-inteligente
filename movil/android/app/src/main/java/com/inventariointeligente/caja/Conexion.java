package com.inventariointeligente.caja;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.net.URL;
import java.net.URLDecoder;

import org.json.JSONObject;

/** Interpretación de la dirección escrita por el usuario y comprobación contra el servidor del PC. */
final class Conexion {

    static final int PUERTO_POR_DEFECTO = 8470;
    private static final int TIMEOUT_MS = 4000;

    private Conexion() {}

    /** Dirección base (esquema://host:puerto) y clave ya normalizadas. */
    static final class Destino {
        final String base;
        final String clave;

        Destino(String base, String clave) {
            this.base = base;
            this.clave = clave;
        }
    }

    /** Resultado de la comprobación con GET /api/v1/estado. */
    static final class Resultado {
        final boolean ok;
        final String mensaje;
        final String nombre;

        Resultado(boolean ok, String mensaje, String nombre) {
            this.ok = ok;
            this.mensaje = mensaje;
            this.nombre = nombre;
        }
    }

    /**
     * Acepta una dirección suelta (192.168.1.20, 192.168.1.20:8470, http://...) o el
     * enlace completo que muestra el PC (http://IP:8470/?clave=XXXX). Si el campo de
     * clave está relleno, tiene prioridad sobre la del enlace.
     *
     * @throws IllegalArgumentException con un mensaje listo para mostrar al usuario.
     */
    static Destino interpretar(String direccion, String clave) {
        String texto = direccion == null ? "" : direccion.trim();
        if (texto.isEmpty()) {
            throw new IllegalArgumentException("Escribe la dirección del PC, por ejemplo http://192.168.1.20:8470");
        }
        if (!texto.contains("://")) {
            texto = "http://" + texto;
        }
        URI uri;
        try {
            uri = new URI(texto);
        } catch (Exception e) {
            throw new IllegalArgumentException("La dirección no es válida. Ejemplo: http://192.168.1.20:8470");
        }
        String esquema = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase();
        String host = uri.getHost();
        if ((!esquema.equals("http") && !esquema.equals("https")) || host == null || host.isEmpty()) {
            throw new IllegalArgumentException("La dirección no es válida. Ejemplo: http://192.168.1.20:8470");
        }
        int puerto = uri.getPort();
        if (puerto == -1 && esquema.equals("http")) {
            puerto = PUERTO_POR_DEFECTO;
        }
        String base = esquema + "://" + host + (puerto == -1 ? "" : ":" + puerto);

        String claveFinal = clave == null ? "" : clave.trim();
        if (claveFinal.isEmpty()) {
            claveFinal = claveDeLaConsulta(uri.getRawQuery());
        }
        return new Destino(base, claveFinal);
    }

    private static String claveDeLaConsulta(String consulta) {
        if (consulta == null) return "";
        for (String par : consulta.split("&")) {
            int i = par.indexOf('=');
            if (i > 0 && par.substring(0, i).equals("clave")) {
                try {
                    return URLDecoder.decode(par.substring(i + 1), "UTF-8").trim();
                } catch (Exception e) {
                    return "";
                }
            }
        }
        return "";
    }

    /** Comprueba el servidor. Bloquea: llamar siempre fuera del hilo principal. */
    static Resultado probar(Destino destino) {
        HttpURLConnection con = null;
        try {
            con = (HttpURLConnection) new URL(destino.base + "/api/v1/estado").openConnection();
            con.setConnectTimeout(TIMEOUT_MS);
            con.setReadTimeout(TIMEOUT_MS);
            con.setInstanceFollowRedirects(false);
            con.setRequestProperty("X-Inventario", "1");
            con.setRequestProperty("Accept", "application/json");
            if (!destino.clave.isEmpty()) {
                con.setRequestProperty("X-Clave", destino.clave);
            }
            int codigo = con.getResponseCode();
            if (codigo == 401) {
                return new Resultado(false,
                        "Clave incorrecta, o el acceso desde otros dispositivos está desactivado en el PC (Ajustes).",
                        "");
            }
            if (codigo != 200) {
                return new Resultado(false,
                        "El servidor respondió con el código " + codigo + ". Comprueba que la dirección y el puerto son los de Inventario.",
                        "");
            }
            String nombre = "";
            try {
                JSONObject json = new JSONObject(leer(con.getInputStream()));
                nombre = json.optString("store_name", "");
            } catch (Exception e) {
                return new Resultado(false,
                        "La dirección responde, pero no parece ser Inventario. Comprueba la dirección y el puerto.",
                        "");
            }
            return new Resultado(true, "", nombre);
        } catch (IOException e) {
            return new Resultado(false,
                    "No hay respuesta del PC. Revisa que el PC esté encendido, con Inventario abierto y en la misma red Wi-Fi.",
                    "");
        } finally {
            if (con != null) con.disconnect();
        }
    }

    private static String leer(InputStream in) throws IOException {
        try (InputStream entrada = in) {
            ByteArrayOutputStream sal = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n;
            int total = 0;
            while ((n = entrada.read(buf)) > 0 && total < 65536) {
                sal.write(buf, 0, n);
                total += n;
            }
            return sal.toString("UTF-8");
        }
    }
}

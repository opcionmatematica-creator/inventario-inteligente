package com.inventariointeligente.caja;

import android.app.Activity;
import android.app.DownloadManager;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Bundle;
import android.os.Environment;
import android.view.ViewGroup;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.CookieManager;
import android.webkit.URLUtil;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.Toast;

import java.net.URLEncoder;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import org.json.JSONObject;

/**
 * Contenedor mínimo: pantalla local de conexión y, después, la web del PC en una WebView.
 *
 * Las pantallas locales (assets) hablan con esta clase mediante navegaciones al esquema
 * "inv-app://", que se interceptan aquí. No se usa addJavascriptInterface.
 */
public class MainActivity extends Activity {

    private static final String ASSET = "file:///android_asset/";
    private static final String ESQUEMA = "inv-app";
    private static final String ACCION_CAMBIAR_PC = "com.inventariointeligente.caja.CAMBIAR_PC";

    private static final String PREFS = "inventario";
    private static final String P_URL = "url";
    private static final String P_CLAVE = "clave";

    private WebView web;
    private SharedPreferences prefs;
    private final ExecutorService hilo = Executors.newSingleThreadExecutor();
    /** Origen (esquema://host:puerto) de la web cargada; los enlaces a otros orígenes salen al navegador. */
    private String origen = "";
    private boolean limpiarHistorial;
    private boolean destruida;
    /** Respuesta pendiente del selector de archivos (<input type=file>, p. ej. importar CSV). */
    private ValueCallback<Uri[]> archivosPendientes;
    private static final int PEDIR_ARCHIVO = 41;

    @Override
    protected void onCreate(Bundle estado) {
        super.onCreate(estado);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);

        FrameLayout raiz = new FrameLayout(this);
        raiz.setBackgroundColor(getResources().getColor(R.color.fondo, getTheme()));
        // Con edge-to-edge (Android 15+) hay que dejar sitio a las barras del sistema y al teclado.
        raiz.setOnApplyWindowInsetsListener((v, insets) -> {
            v.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(),
                    insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom());
            return insets;
        });
        web = new WebView(this);
        raiz.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(raiz);

        configurarWebView();

        if (estado != null) {
            web.restoreState(estado);
            origen = estado.getString("origen", "");
        } else {
            arrancar(getIntent());
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        if (ACCION_CAMBIAR_PC.equals(intent.getAction())) {
            mostrarConexion();
        }
    }

    @Override
    protected void onSaveInstanceState(Bundle salida) {
        super.onSaveInstanceState(salida);
        web.saveState(salida);
        salida.putString("origen", origen);
    }

    private void arrancar(Intent intent) {
        String url = prefs.getString(P_URL, "");
        if (url.isEmpty() || (intent != null && ACCION_CAMBIAR_PC.equals(intent.getAction()))) {
            mostrarConexion();
        } else {
            cargarWebDelPc(url, prefs.getString(P_CLAVE, ""));
        }
    }

    // ---------------------------------------------------------------- WebView

    private void configurarWebView() {
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        s.setTextZoom(100);
        s.setAllowFileAccess(false);        // los assets (file:///android_asset) siguen accesibles
        s.setAllowContentAccess(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);

        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true);
        cookies.setAcceptThirdPartyCookies(web, false);

        web.setBackgroundColor(getResources().getColor(R.color.fondo, getTheme()));
        web.setWebViewClient(new Cliente());
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView vista, ValueCallback<Uri[]> respuesta,
                                             FileChooserParams parametros) {
                if (archivosPendientes != null) archivosPendientes.onReceiveValue(null);
                archivosPendientes = respuesta;
                try {
                    startActivityForResult(parametros.createIntent(), PEDIR_ARCHIVO);
                } catch (Exception e) {
                    archivosPendientes = null;
                    Toast.makeText(MainActivity.this, R.string.sin_selector, Toast.LENGTH_LONG).show();
                    return false;
                }
                return true;
            }
        });
        web.setDownloadListener((url, agente, disposicion, mime, longitud) ->
                descargar(url, agente, disposicion, mime));
    }

    private final class Cliente extends WebViewClient {

        @Override
        public boolean shouldOverrideUrlLoading(WebView vista, WebResourceRequest peticion) {
            Uri uri = peticion.getUrl();
            String esquema = uri.getScheme() == null ? "" : uri.getScheme();

            if (esquema.equals(ESQUEMA)) {
                // Solo las pantallas locales pueden dar órdenes a la app.
                String actual = vista.getUrl();
                if (actual != null && actual.startsWith(ASSET)) {
                    ordenDePantallaLocal(uri);
                }
                return true;
            }
            if (uri.toString().startsWith(ASSET)) {
                return false;
            }
            if (esquema.equals("http") || esquema.equals("https")) {
                if (origenDe(uri).equals(origen)) {
                    return false;
                }
            }
            abrirFuera(uri);
            return true;
        }

        @Override
        public void onPageFinished(WebView vista, String url) {
            CookieManager.getInstance().flush();
            if (limpiarHistorial) {
                limpiarHistorial = false;
                vista.clearHistory();
            }
            if (url != null && url.startsWith(ASSET + "connect.html")) {
                rellenarConexion();
            }
        }

        @Override
        public void onReceivedError(WebView vista, WebResourceRequest peticion, WebResourceError error) {
            if (peticion.isForMainFrame() && !peticion.getUrl().toString().startsWith(ASSET)) {
                mostrarError("No se pudo cargar Inventario. Revisa que el PC esté encendido, con Inventario abierto y en la misma red Wi-Fi.");
            }
        }

        @Override
        public void onReceivedHttpError(WebView vista, WebResourceRequest peticion, WebResourceResponse resp) {
            if (!peticion.isForMainFrame() || peticion.getUrl().toString().startsWith(ASSET)) return;
            int codigo = resp.getStatusCode();
            if (codigo == 401) {
                mostrarError("Clave incorrecta, o el acceso desde otros dispositivos está desactivado en el PC (Ajustes).");
            } else if (codigo >= 500) {
                mostrarError("El servidor del PC respondió con un error (" + codigo + "). Inténtalo de nuevo.");
            }
        }
    }

    private static String origenDe(Uri uri) {
        int puerto = uri.getPort();
        return (uri.getScheme() + "://" + uri.getHost() + (puerto == -1 ? "" : ":" + puerto)).toLowerCase();
    }

    private void abrirFuera(Uri uri) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri).addCategory(Intent.CATEGORY_BROWSABLE));
        } catch (Exception e) {
            // Sin aplicación para abrirlo: se ignora.
        }
    }

    // ------------------------------------------------------- pantallas locales

    private void ordenDePantallaLocal(Uri uri) {
        String orden = uri.getHost() == null ? "" : uri.getHost();
        switch (orden) {
            case "conectar":
                conectar(uri.getQueryParameter("direccion"), uri.getQueryParameter("clave"));
                break;
            case "reintentar":
                String url = prefs.getString(P_URL, "");
                if (url.isEmpty()) mostrarConexion();
                else cargarWebDelPc(url, prefs.getString(P_CLAVE, ""));
                break;
            case "cambiar":
                mostrarConexion();
                break;
            default:
                break;
        }
    }

    private void mostrarConexion() {
        limpiarHistorial = true;
        web.loadUrl(ASSET + "connect.html");
    }

    private void mostrarError(String mensaje) {
        limpiarHistorial = true;
        web.loadUrl(ASSET + "error.html?m=" + Uri.encode(mensaje));
    }

    /** Pasa a la pantalla de conexión los datos guardados. */
    private void rellenarConexion() {
        JSONObject datos = new JSONObject();
        try {
            datos.put("direccion", prefs.getString(P_URL, ""));
            datos.put("clave", prefs.getString(P_CLAVE, ""));
        } catch (Exception ignorada) {
            return;
        }
        web.evaluateJavascript("window.invIniciar && window.invIniciar(" + datos + ")", null);
    }

    private void respuestaAConexion(boolean ok, String mensaje) {
        JSONObject r = new JSONObject();
        try {
            r.put("ok", ok);
            r.put("mensaje", mensaje);
        } catch (Exception ignorada) {
            return;
        }
        web.evaluateJavascript("window.invResultado && window.invResultado(" + r + ")", null);
    }

    private void conectar(String direccion, String clave) {
        final Conexion.Destino destino;
        try {
            destino = Conexion.interpretar(direccion, clave);
        } catch (IllegalArgumentException e) {
            respuestaAConexion(false, e.getMessage());
            return;
        }
        hilo.execute(() -> {
            Conexion.Resultado r = Conexion.probar(destino);
            runOnUiThread(() -> {
                if (destruida) return;
                if (!r.ok) {
                    respuestaAConexion(false, r.mensaje);
                    return;
                }
                prefs.edit().putString(P_URL, destino.base).putString(P_CLAVE, destino.clave).apply();
                cargarWebDelPc(destino.base, destino.clave);
            });
        });
    }

    private void cargarWebDelPc(String base, String clave) {
        origen = origenDe(Uri.parse(base));
        limpiarHistorial = true;
        String url = base + "/";
        if (!clave.isEmpty()) {
            try {
                url += "?clave=" + URLEncoder.encode(clave, "UTF-8");
            } catch (Exception ignorada) {
                // UTF-8 siempre existe.
            }
        }
        web.loadUrl(url);
    }

    // --------------------------------------------------------------- descargas

    private void descargar(String url, String agente, String disposicion, String mime) {
        Uri uri = Uri.parse(url);
        String esquema = uri.getScheme() == null ? "" : uri.getScheme();
        if (!esquema.equals("http") && !esquema.equals("https")) {
            Toast.makeText(this, R.string.descarga_fallida, Toast.LENGTH_LONG).show();
            return;
        }
        try {
            DownloadManager.Request pet = new DownloadManager.Request(uri);
            String cookie = CookieManager.getInstance().getCookie(url);
            if (cookie != null) pet.addRequestHeader("Cookie", cookie);
            pet.addRequestHeader("User-Agent", agente);
            pet.addRequestHeader("X-Inventario", "1");
            String nombre = URLUtil.guessFileName(url, disposicion, mime);
            pet.setTitle(nombre);
            pet.setMimeType(mime);
            pet.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            pet.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, nombre);
            ((DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE)).enqueue(pet);
            Toast.makeText(this, R.string.descargando, Toast.LENGTH_SHORT).show();
        } catch (Exception e) {
            Toast.makeText(this, R.string.descarga_fallida, Toast.LENGTH_LONG).show();
        }
    }

    @Override
    protected void onActivityResult(int peticion, int resultado, Intent datos) {
        super.onActivityResult(peticion, resultado, datos);
        if (peticion == PEDIR_ARCHIVO && archivosPendientes != null) {
            archivosPendientes.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(resultado, datos));
            archivosPendientes = null;
        }
    }

    // ------------------------------------------------------------ ciclo de vida

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onPause() {
        CookieManager.getInstance().flush();
        web.onPause();
        super.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        web.onResume();
    }

    @Override
    protected void onDestroy() {
        destruida = true;
        hilo.shutdownNow();
        web.stopLoading();
        ((ViewGroup) web.getParent()).removeView(web);
        web.destroy();
        super.onDestroy();
    }
}

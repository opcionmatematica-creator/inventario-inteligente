# Inventario Inteligente - app Android

Contenedor nativo mínimo (Java + WebView, sin dependencias externas) para usar
Inventario Inteligente desde el celular. El celular es otra «caja» del comercio en la
red local: no necesita internet. La app muestra una pantalla de conexión y, después,
la interfaz web que sirve el PC (`http://IP:8470/`) dentro de una WebView.

- Paquete: `com.inventariointeligente.caja` · nombre visible «Inventario»
- Versión 0.2.0 (`versionCode 1`) · `minSdk 26`, `targetSdk 36`

## Cómo conectar

**Por Wi-Fi (misma red que el PC)**

1. En el PC, abre Inventario y activa «acceso desde otros dispositivos» en Ajustes.
   El PC muestra la dirección y la clave (o un enlace `http://IP:8470/?clave=XXXX`).
2. En la app escribe la «Dirección del PC» (p. ej. `http://192.168.1.20:8470`) y la
   «Clave», o pega el enlace completo en la dirección: se separan solos.
3. Pulsa «Conectar». La app comprueba `GET /api/v1/estado` y, si va bien, guarda los
   datos y abre la web. Las siguientes veces entra directamente.

El tráfico va en HTTP sin cifrar dentro de tu red local (también la clave).

**Por cable USB**

1. Activa la depuración USB en el celular y conéctalo al PC.
2. En el PC: `adb reverse tcp:8470 tcp:8470`
3. En la app pulsa «Conectar por cable USB» (`http://127.0.0.1:8470`). No hace falta
   clave: el servidor confía en 127.0.0.1.

**Cambiar de PC**: si el PC no responde, la pantalla de error ofrece «Reintentar» y
«Cambiar de PC». También se puede mantener pulsado el icono de la app y elegir
«Cambiar de PC».

## Compilar

Requiere el Android SDK y un JDK 21 **completo** (con `javac`).

    export ANDROID_HOME=~/Android/Sdk
    export JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64   # debe incluir javac
    ./gradlew --offline assembleDebug

(`--offline` si las dependencias ya están en la caché de Gradle.) El APK queda en
`app/build/outputs/apk/debug/app-debug.apk`. Para instalarlo: `adb install -r` sobre ese archivo.

## Firmar el release

El release **no** se firma con la clave de depuración. Crea `keystore.properties` (está
ignorado por git) junto a este README:

    storeFile=/ruta/a/inventario.jks
    storePassword=...
    keyAlias=inventario
    keyPassword=...

y compila con `./gradlew assembleRelease`. Si `keystore.properties` no existe, el APK de
release se genera **sin firmar** y no se podrá instalar hasta firmarlo (por ejemplo con
`apksigner`). Para crear una clave: `keytool -genkeypair -v -keystore inventario.jks -alias inventario -keyalg RSA -keysize 2048 -validity 10000`.
Guarda la clave y sus contraseñas fuera del repositorio.

## Notas técnicas

- Las pantallas locales (`app/src/main/assets/`) se comunican con la app mediante
  navegaciones a `inv-app://...`, que solo se aceptan si la página actual es un asset
  local. No se usa `addJavascriptInterface`.
- Los enlaces a otros orígenes se abren en el navegador externo.
- Las descargas (CSV, respaldos) usan `DownloadManager` con la cookie de sesión y
  se guardan en la carpeta Descargas.

"""Dibuja el icono de la app (caja blanca sobre verde, como web/icon.svg) y lo guarda como .ico.

    python icono.py SALIDA.ico     (requiere Pillow; construir_instalador.sh lo ejecuta con uvx)
"""

import sys

from PIL import Image, ImageDraw

VERDE = (15, 118, 110, 255)
BLANCO = (255, 255, 255, 255)


def dibujar(tamano: int = 256) -> Image.Image:
    escala = 4  # se dibuja en grande y se reduce: bordes suaves
    lado = tamano * escala
    imagen = Image.new("RGBA", (lado, lado), (0, 0, 0, 0))
    d = ImageDraw.Draw(imagen)
    d.rounded_rectangle((0, 0, lado - 1, lado - 1), radius=int(lado * 0.22), fill=VERDE)
    u = lado / 64  # mismas coordenadas que icon.svg (viewBox 64)
    p = lambda x, y: (x * u, y * u)  # noqa: E731
    ancho = max(1, int(4 * u))
    contorno = [p(32, 13), p(49, 21.5), p(49, 42.5), p(32, 51), p(15, 42.5), p(15, 21.5)]
    centro = p(32, 30)
    trazos = [contorno + [contorno[0]], [contorno[5], centro, contorno[1]], [centro, contorno[3]]]
    for trazo in trazos:
        d.line(trazo, fill=BLANCO, width=ancho)
    # Uniones redondeadas, como stroke-linejoin="round" del SVG: un círculo en cada vértice.
    radio = ancho / 2
    for x, y in contorno + [centro]:
        d.ellipse((x - radio, y - radio, x + radio, y + radio), fill=BLANCO)
    return imagen.resize((tamano, tamano), Image.LANCZOS)


if __name__ == "__main__":
    base = dibujar(256)
    base.save(sys.argv[1], format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    base.save(sys.argv[1].replace(".ico", ".png"))

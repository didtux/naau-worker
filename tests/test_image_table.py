"""
La lectura de una planilla fotografiada.

── Contra qué se mide, y por qué eso es lo único honesto ───────────────────

Contra la MISMA planilla leída del PDF.

Un OCR no se puede probar contra valores escritos a mano en el test: quien los
escribe los copia del documento, y entonces el test dice «el OCR leyó lo que yo
leí», que es exactamente lo que no hace falta comprobar. Acá la referencia es
`parse_pdf` sobre el archivo original, donde el texto es texto y no hay nada
que reconocer, y la imagen es ese mismo archivo renderizado. Si los dos caminos
coinciden, coinciden en un documento de obra real y no en una maqueta.

── La propiedad que se protege ─────────────────────────────────────────────

No es «el OCR lee todo». Es **«el OCR no inventa»**.

Una celda que se pierde queda en blanco y se ve: la fila entra a la pantalla de
revisión con una medida vacía, y encima el motor recalcula y no cierra contra
lo que el papel afirma. Una celda MAL LEÍDA no se ve por ningún lado — un 8
donde decía 6 es una fila perfectamente verosímil que termina en fierro mal
cortado.

Por eso los tests de las fotos degradadas no exigen un porcentaje de acierto:
exigen que ni un solo valor leído sea distinto del que dice el papel. Medido
sobre el render limpio y cuatro degradaciones, ese número es cero.
"""

from __future__ import annotations

import io

import pytest

from conftest import PLANILLA

pytest.importorskip("cv2", reason="el lector de imágenes necesita las dependencias de OCR")
pytest.importorskip("rapidocr_onnxruntime", reason="falta el motor de OCR")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from pdf_parser import CorruptImageError, NoTableError, parse_imagen, parse_pdf  # noqa: E402

# A cuántos puntos por pulgada se renderiza el PDF para simular el escaneo.
#
# 200 es lo que da un escáner de oficina en su ajuste normal y lo que sale de
# una foto de celular a un metro de la hoja. No se elige 300 a propósito: probar
# con más resolución de la que va a llegar es probar otra cosa.
DPI = 200


def _render(pagina: int, dpi: int = DPI) -> np.ndarray:
    """Una página de la planilla piloto, como imagen."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(PLANILLA))
    imagen = pdf[pagina - 1].render(scale=dpi / 72).to_pil().convert("RGB")
    return np.array(imagen)


def _png(arreglo: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(arreglo).save(buffer, "PNG")
    return buffer.getvalue()


def _como_foto(arreglo: np.ndarray, grados: float, calidad: int, desenfoque: int) -> bytes:
    """
    El render, degradado como lo degrada un teléfono.

    Las cuatro cosas que le pasan a una foto de una planilla sobre un
    escritorio: está girada, tiene la sombra de quien la saca, el enfoque no es
    perfecto y el archivo sale en JPEG comprimido.
    """
    import cv2

    alto, ancho = arreglo.shape[:2]
    giro = cv2.getRotationMatrix2D((ancho / 2, alto / 2), grados, 1.0)
    salida = cv2.warpAffine(
        arreglo, giro, (ancho, alto), flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255)
    )

    # Un degradado de luz del 100 % al 62 %, que es una sombra franca.
    sombra = np.linspace(1.0, 0.62, ancho)[None, :] * np.linspace(1.0, 0.80, alto)[:, None]
    salida = np.clip(salida * sombra[:, :, None], 0, 255).astype(np.uint8)

    if desenfoque:
        salida = cv2.GaussianBlur(salida, (desenfoque, desenfoque), 0)

    ruido = np.random.RandomState(7).normal(0, 4, salida.shape)
    salida = np.clip(salida.astype(np.int16) + ruido, 0, 255).astype(np.uint8)

    buffer = io.BytesIO()
    Image.fromarray(salida).save(buffer, "JPEG", quality=calidad)
    return buffer.getvalue()


def _campos(fila) -> dict[str, float | int | None]:
    """Los valores NUMÉRICOS de una fila: lo que termina en fierro cortado."""
    valores: dict[str, float | int | None] = {
        "diametro": fila.diameter_mm,
        "cantidad": fila.quantity,
        "parcial": fila.claimed.unit_length,
        "total": fila.claimed.total_length,
        "con_perdida": fila.claimed.total_with_loss,
        "barras": fila.claimed.bars,
        "peso": fila.claimed.total_weight,
    }
    for medida in fila.dimensions:
        valores[f"medida.{medida.name}"] = medida.value
    return valores


@pytest.fixture(scope="module")
def del_pdf(planilla_bytes: bytes):
    """La primera página de la planilla, leída del PDF: la verdad de referencia."""
    return [f for f in parse_pdf(planilla_bytes).rows if f.page == 1]


@pytest.fixture(scope="module")
def pagina1() -> np.ndarray:
    if not PLANILLA.exists():
        pytest.skip(f"falta la planilla piloto en {PLANILLA}")
    return _render(1)


def _comparar(leido, referencia) -> tuple[int, int, list[str]]:
    """Aciertos, celdas perdidas y —lo que importa— celdas mal leídas."""
    aciertos = perdidas = 0
    mal: list[str] = []
    for esperada, obtenida in zip(referencia, leido.rows):
        campos_esperados, campos_obtenidos = _campos(esperada), _campos(obtenida)
        for nombre, valor in campos_esperados.items():
            leido_ = campos_obtenidos.get(nombre)
            if leido_ == valor:
                aciertos += 1
            elif leido_ is None:
                perdidas += 1
            else:
                mal.append(f"{esperada.code}.{nombre}: el papel dice {valor!r} y se leyó {leido_!r}")
    return aciertos, perdidas, mal


def test_un_escaneo_limpio_se_lee_igual_que_el_pdf(pagina1, del_pdf) -> None:
    """
    La prueba de fondo: el mismo papel por los dos caminos, mismo resultado.

    Sin tolerancia y sin porcentaje. Un escaneo derecho de una planilla impresa
    no tiene por qué perder una sola celda, y el día que empiece a perderlas hay
    algo roto que conviene ver acá y no en una obra.
    """
    leido = parse_imagen(_png(pagina1))

    assert leido.source == "image"
    assert len(leido.rows) == len(del_pdf)
    assert [f.code for f in leido.rows] == [f.code for f in del_pdf]

    aciertos, perdidas, mal = _comparar(leido, del_pdf)
    assert mal == []
    assert perdidas == 0
    assert aciertos == sum(len(_campos(f)) for f in del_pdf)


def test_el_resultado_dice_que_vino_de_una_imagen(pagina1) -> None:
    """
    `source` y el aviso, que son lo que la pantalla necesita para avisar.

    Sin esto la planilla llegaría al editor idéntica a una importada de un PDF
    digital, y quien revisa no tendría cómo saber que cada número de esa tabla
    es una lectura falible.
    """
    leido = parse_imagen(_png(pagina1))

    assert leido.source == "image"
    assert any("reconocimiento de caracteres" in aviso for aviso in leido.warnings)


@pytest.mark.parametrize(
    ("grados", "calidad", "desenfoque"),
    [(0.0, 92, 0), (1.5, 85, 3), (-3.0, 75, 3), (2.5, 60, 5)],
    ids=["derecha", "poco-girada", "girada", "girada-y-borrosa"],
)
def test_una_foto_degradada_pierde_celdas_pero_no_inventa_ninguna(
    pagina1, del_pdf, grados: float, calidad: int, desenfoque: int
) -> None:
    """
    La propiedad que hace que esto se pueda usar en una obra.

    Con sombra, ruido, giro y compresión, el OCR deja de ver celdas. Lo que NO
    hace —y lo que este test fija— es leer una celda distinta de lo que dice el
    papel. Una medida en blanco se ve; una medida cambiada, no.

    El giro se topa en tres grados porque el enderezado está topado ahí: más que
    eso no es una planilla torcida, es una foto mal sacada, y corregirla a
    partir de líneas sueltas produce basura con mucha confianza.
    """
    leido = parse_imagen(_como_foto(pagina1, grados, calidad, desenfoque))

    # La ESTRUCTURA sobrevive siempre: la regla impresa es mucho más robusta
    # que el texto, y por eso las celdas se delimitan con ella y no agrupando
    # el texto por coordenadas.
    assert [f.code for f in leido.rows] == [f.code for f in del_pdf]

    aciertos, _, mal = _comparar(leido, del_pdf)
    assert mal == [], "el OCR leyó un valor distinto del que dice el papel"
    assert aciertos > 0


def test_una_imagen_sin_tabla_no_se_lee(pagina1) -> None:
    """
    Sin líneas de regla no hay lectura, y el mensaje dice qué hacer.

    Es la misma regla que en el PDF y por la misma razón: agrupar el texto por
    coordenadas en una foto torcida mezcla dos columnas angostas y devuelve una
    planilla que parece bien leída con las medidas cambiadas de lugar.
    """
    blanco = np.full((1200, 1600, 3), 255, dtype=np.uint8)

    with pytest.raises(NoTableError) as e:
        parse_imagen(_png(blanco))

    assert "regla" in str(e.value)


def test_una_imagen_rota_lo_dice_con_su_propio_codigo() -> None:
    """
    Distinto de «no es una imagen» y distinto de «no tiene tabla».

    Lo que hay que hacer también es distinto: acá la foto se cortó al subirla y
    hay que volver a sacarla, no mejorar la luz.
    """
    with pytest.raises(CorruptImageError):
        parse_imagen(b"\x89PNG\r\n\x1a\n" + b"basura que no es un PNG")


def test_la_pagina_de_continuacion_explica_por_que_no_se_puede_sola() -> None:
    """
    Una imagen es una página, y el encabezado está sólo en la primera.

    Sin este mensaje, quien fotografíe la segunda hoja de una planilla va a
    probar con más luz y de más cerca un problema que no es de nitidez.
    """
    if not PLANILLA.exists():
        pytest.skip(f"falta la planilla piloto en {PLANILLA}")

    from pdf_parser import NoHeaderError

    with pytest.raises(NoHeaderError) as e:
        parse_imagen(_png(_render(2)))

    assert "varias páginas" in str(e.value)

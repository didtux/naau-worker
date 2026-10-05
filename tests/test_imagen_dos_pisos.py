"""
Una planilla de DOS PISOS leída de una imagen.

── Por qué hay un dibujo en las pruebas ────────────────────────────────────

Porque lo que se prueba es la reconstrucción de la GEOMETRÍA de la tabla, y para
eso hace falta controlar la geometría. La planilla piloto sirve para medir
cuánto acierta el reconocimiento de caracteres —eso está en
`test_image_table.py`, contra la lectura exacta del PDF— y no sirve para esto:
no tiene encabezado de dos pisos.

El dibujo reproduce la estructura de un documento de obra real, «PLANILLA DE
FIERROS - GRADA TIPO 1», y en particular la línea que lo rompía: la que separa
«DIMENSIONES (cm.)» de la fila «a b c d e» y que existe SÓLO debajo de esas
cinco columnas, cruzando el 15 % del ancho de la tabla.

Los NÚMEROS son los del papel, no inventados, porque la planilla se verifica a
sí misma: la longitud es la suma de las medidas y el total es la longitud por la
cantidad por las veces. Si una celda se leyera de la columna vecina, esas
cuentas no dan — es la planilla haciendo de oráculo de su propia lectura.
"""

from __future__ import annotations

import io

import pytest

pytest.importorskip("cv2", reason="el lector de imágenes necesita las dependencias de OCR")
pytest.importorskip("rapidocr_onnxruntime", reason="falta el motor de OCR")

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

import image_table as it  # noqa: E402
from pdf_parser import NoHeaderError, parse_imagen  # noqa: E402

# ── El documento ───────────────────────────────────────────────────────────

ANCHOS = [52, 40, 50, 46, 46, 50, 46, 42, 62, 56, 58, 72, 78, 330]
PISO_1 = [
    "POS.", "Ø", "TIPO", "DIMENSIONES (cm.)", None, None, None, None,
    "LONG.\n(m.)", "CANT.", "VECES", "LONG.\nTOT.(m.)", "PESO +7%\n(Kg.)", "ESQUEMA",
]
PISO_2 = [None, None, None, "a", "b", "c", "d", "e", None, None, None, None, None, None]

# Las catorce filas del papel. Las rayas son las del papel.
FILAS = [
    ["1", "12", "J", "15", "90", "435", "45", "——", "5.85", "9", "1", "52.65", "50.03"],
    ["2", "10", "J", "15", "95", "410", "55", "——", "5.75", "8", "1", "46.00", "30.37"],
    ["3", "8", "O", "2x24", "2x105", "2x1", "——", "——", "2.60", "3", "1", "7.80", "3.30"],
    ["4", "12", "S", "10", "125", "65", "——", "——", "2.00", "9", "1", "18.00", "17.10"],
    ["5", "12", "U", "65", "55", "——", "——", "——", "1.20", "8", "1", "9.60", "9.12"],
    ["6", "8", "E", "115", "——", "——", "——", "——", "1.15", "94", "1", "108.10", "45.69"],
    ["7", "16", "L", "15", "140", "——", "——", "——", "1.55", "2", "2", "6.20", "10.47"],
    ["8", "12", "L", "15", "140", "——", "——", "——", "1.55", "2", "2", "6.20", "5.89"],
    ["9", "10", "I", "110", "——", "——", "——", "——", "1.10", "2", "2", "4.40", "2.90"],
    ["10", "6", "O", "2x19", "2x19", "2x2", "——", "——", "0.80", "12", "1", "17.60", "4.56"],
    ["11", "12", "X", "15", "70", "335", "140", "10", "5.70", "9", "1", "51.30", "48.74"],
    ["12", "10", "X", "15", "80", "335", "120", "10", "5.60", "8", "1", "44.80", "29.58"],
    ["13", "12", "C", "65", "155", "10", "——", "——", "2.30", "8", "1", "18.40", "17.48"],
    ["14", "12", "S", "10", "95", "65", "——", "——", "1.70", "9", "1", "15.30", "14.54"],
]

# Las filas cuyas medidas vienen en notación «2x24» —dos lados de 24, de un
# estribo cerrado—. El lector NO las interpreta: decir que 2x24 son 48 daría
# bien la longitud total y mal la figura, porque 24 es el lado y no la medida.
# Lo que hace es decir que no las pudo leer, con la celda a la vista.
FILAS_CON_MULTIPLICADOR = {"3", "10"}

# La fila 10 no se usa como oráculo del total, y vale explicar por qué.
#
# Sus propias dos columnas de resultado no se ponen de acuerdo: con 17,60 m de
# φ6 el peso con el 7 % da 4,18 kg y la planilla dice 4,56, que es el peso de
# 19,20 m — que a su vez es 0,80 × 12 × 2. O la planilla tiene ahí un error, o
# la transcripción de esa fila a esta prueba se equivocó en un dígito: se copió
# de una captura del documento, no del archivo.
#
# En cualquiera de los dos casos no sirve para verificar una lectura, y dejarla
# sería fijar como correcto algo que no se pudo confirmar. Lo que sí hace el
# importador con una fila así es lo que corresponde: recalcula, no le cierra, y
# la muestra entre los desacuerdos de lectura.
SIN_ORACULO_DEL_TOTAL = {"10"}

ESCALA = 2
MARGEN = 30


def _fuente(px: int):
    for nombre in ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(nombre, px)
        except OSError:
            continue
    return ImageFont.load_default()


#: Las dos columnas que hacen que la planilla se pueda verificar a sí misma:
#: la longitud declarada y el total declarado. Con ellas, el importador
#: recalcula cada fila y la que no cierra queda bloqueada a la vista.
COLUMNAS_DEL_ORACULO = frozenset({8, 11})


def _dibujar(
    con_linea_del_segundo_piso: bool = True,
    sin_columnas: frozenset[int] = frozenset(),
) -> bytes:
    """
    La planilla, dibujada.

    `con_linea_del_segundo_piso=False` dibuja el mismo documento SIN la línea
    corta, que es el único caso en que los dos pisos son de verdad una sola
    celda y no hay nada que reconstruir.

    `sin_columnas` quita columnas del papel. Sirve para dibujar la misma
    planilla sin las columnas de resultado, que es la que NO se puede verificar
    a sí misma. Sólo admite índices a la derecha de las medidas: las de la
    izquierda sostienen la geometría del encabezado de dos pisos.
    """
    assert all(i >= 8 for i in sin_columnas), "las medidas y sus rótulos no se quitan"

    def quitar(fila: list) -> list:
        return [v for i, v in enumerate(fila) if i not in sin_columnas]

    anchos, piso_1, piso_2 = quitar(ANCHOS), quitar(PISO_1), quitar(PISO_2)
    filas_del_papel = [quitar(f) for f in FILAS]

    xs = [MARGEN]
    for ancho in anchos:
        xs.append(xs[-1] + ancho * ESCALA)

    y0 = MARGEN + 26 * ESCALA
    ys = [y0, y0 + 30 * ESCALA, y0 + 50 * ESCALA]
    for _ in FILAS:
        ys.append(ys[-1] + 26 * ESCALA)

    img = Image.new("RGB", (xs[-1] + MARGEN, ys[-1] + MARGEN), "white")
    d = ImageDraw.Draw(img)
    AZUL = (0, 82, 155)
    grosor = max(1, ESCALA // 2)
    f_titulo, f_cabeza, f_dato = _fuente(9 * ESCALA), _fuente(7 * ESCALA), _fuente(8 * ESCALA)

    d.text(((xs[0] + xs[-1]) / 2, MARGEN + 6), "PLANILLA DE FIERROS - GRADA TIPO 1  N.: + 3.675",
           font=f_titulo, fill=AZUL, anchor="ma")

    for y in (ys[0], ys[2], *ys[3:]):
        d.line([(xs[0], y), (xs[-1], y)], fill=AZUL, width=grosor)

    # LA línea: sólo debajo de «DIMENSIONES (cm.)», de la columna 3 a la 8.
    if con_linea_del_segundo_piso:
        d.line([(xs[3], ys[1]), (xs[8], ys[1])], fill=AZUL, width=grosor)

    for i, x in enumerate(xs):
        # Las que separan las medidas entre sí arrancan en el segundo piso,
        # porque arriba de ellas está la celda combinada del grupo.
        desde = ys[1] if 3 < i < 8 else ys[0]
        d.line([(x, desde), (x, ys[-1])], fill=AZUL, width=grosor)

    def centrar(texto, columna, arriba, abajo, fuente):
        if not texto:
            return
        cx = (xs[columna] + xs[columna + 1]) / 2
        lineas = texto.split("\n")
        paso = (abajo - arriba) / (len(lineas) + 1)
        for k, linea in enumerate(lineas):
            d.text((cx, arriba + paso * (k + 0.7)), linea, font=fuente, fill=AZUL, anchor="ma")

    for i, texto in enumerate(piso_1):
        centrar(texto, i, ys[0], ys[1] if i == 3 else ys[2], f_cabeza)
    for i, texto in enumerate(piso_2):
        centrar(texto, i, ys[1], ys[2], f_cabeza)
    for r, fila in enumerate(filas_del_papel):
        for i, texto in enumerate(fila):
            centrar(texto, i, ys[2 + r], ys[3 + r], f_dato)

    buffer = io.BytesIO()
    img.save(buffer, "PNG")
    return buffer.getvalue()


#: La leyenda del papel: nueve figuras, cada una con su nombre en un círculo y
#: los rótulos de sus tramos alrededor del dibujo.
LEYENDA = [
    ("J", ["a", "b", "c", "d"]),
    ("E", ["a"]),
    ("O", ["a", "b", "c"]),
    ("L", ["a", "b"]),
    ("S", ["a", "b", "c"]),
    ("X", ["a", "b", "c", "d", "e"]),
    ("U", ["a", "b"]),
    ("C", ["a", "b", "c"]),
]


def _dibujar_con_la_leyenda() -> bytes:
    """
    La planilla con la columna ESQUEMA como está en el papel: UNA celda alta.

    En `_dibujar` las líneas de fila cruzan toda la tabla, así que ESQUEMA son
    catorce celdas vacías — y por eso el dibujo no reproducía lo que el
    documento real le hacía al lector. En el papel esas líneas se cortan antes:
    la columna de dibujos es una sola celda con los nueve croquis adentro, y el
    reconocimiento devuelve los rótulos de todos los tramos pegados en una
    cadena sola.
    """
    base = _dibujar()
    img = Image.open(io.BytesIO(base)).convert("RGB")
    d = ImageDraw.Draw(img)

    xs = [MARGEN]
    for ancho in ANCHOS:
        xs.append(xs[-1] + ancho * ESCALA)
    x_esquema, x_fin = xs[-2], xs[-1]

    y0 = MARGEN + 26 * ESCALA
    ys = [y0, y0 + 30 * ESCALA, y0 + 50 * ESCALA]
    for _ in FILAS:
        ys.append(ys[-1] + 26 * ESCALA)

    # Borrar las líneas de fila de adentro de ESQUEMA: es una celda sola.
    d.rectangle([x_esquema + 2, ys[2] + 2, x_fin - 2, ys[-1] - 2], fill="white")

    AZUL = (0, 82, 155)
    fuente = _fuente(7 * ESCALA)
    ancho, alto = x_fin - x_esquema, ys[-1] - ys[2]
    grosor = max(1, ESCALA // 2)
    for k, (nombre, tramos) in enumerate(LEYENDA):
        cx = x_esquema + ancho * (0.28 if k % 2 == 0 else 0.72)
        cy = ys[2] + alto * (0.10 + 0.22 * (k // 2))
        r = 6 * ESCALA
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=AZUL, width=grosor)
        d.text((cx, cy - r * 0.8), nombre, font=fuente, fill=AZUL, anchor="ma")
        for j, tramo in enumerate(tramos):
            d.text((cx - r * 2.4 + j * 5 * ESCALA, cy + r * 1.2), tramo,
                   font=fuente, fill=AZUL, anchor="ma")
        d.line([cx - r * 2.6, cy + r * 2.4, cx + r * 2.6, cy + r * 1.6], fill=AZUL, width=grosor)

    buffer = io.BytesIO()
    img.save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture(scope="module")
def imagen() -> bytes:
    return _dibujar()


@pytest.fixture(scope="module")
def leido(imagen: bytes):
    return parse_imagen(imagen)


# ── La estructura ──────────────────────────────────────────────────────────


def test_la_linea_que_cruza_cinco_columnas_de_catorce_es_una_regla(imagen: bytes) -> None:
    """
    El defecto de fondo, aislado de todo lo demás.

    El detector de regla pedía que una horizontal cruzara un tercio del ancho de
    la tabla. La que separa los dos pisos del encabezado cruza el 15 %, así que
    se descartaba, los dos pisos quedaban en una sola celda y el documento
    entero se caía con NO_HEADER.

    El criterio ya no es el largo sino que la línea EMPIECE Y TERMINE en un
    borde de columna, que es lo que distingue una regla de un subrayado.
    """
    gris = it.enderezar(it.abrir_imagen(imagen))
    binaria = it._binarizar(gris)
    horizontal, vertical = it._mascaras_de_regla(binaria)
    bloque = it._bloques_de_tabla(horizontal, vertical, gris.shape)[0]

    xs = it._cortes_verticales(vertical, bloque)
    ys = it._cortes_horizontales(horizontal, bloque, xs)

    assert len(xs) - 1 == len(ANCHOS), f"{len(xs) - 1} columnas"
    # Dos pisos de encabezado más catorce filas.
    assert len(ys) - 1 == 2 + len(FILAS), f"{len(ys) - 1} filas"


def test_los_dos_pisos_del_encabezado_se_leen_los_dos(leido) -> None:
    columnas = leido.columns

    assert set(columnas.dimension_columns) == {"a", "b", "c", "d", "e"}
    assert columnas.mapped["code"] == 0
    assert columnas.mapped["diameter"] == 1
    assert columnas.mapped["unit_length"] == 8
    assert columnas.mapped["quantity"] == 9
    assert columnas.mapped["elements"] == 10
    assert columnas.mapped["total_length"] == 11

    assert [f.code for f in leido.rows] == [f[0] for f in FILAS]


def test_las_dos_unidades_se_leen_las_dos(leido) -> None:
    assert leido.unit == "cm"
    assert leido.claim_unit == "m"


def test_las_cuentas_del_papel_cierran_despues_de_leerlo(leido) -> None:
    """
    El oráculo. Catorce filas, y en cada una:

        la longitud declarada × 100 == la suma de las medidas en cm
        el total declarado      == la longitud × cantidad × veces

    Ninguna de las dos cierra si una celda se leyó de la columna de al lado, si
    un dígito se perdió o si las veces se confundieron con la cantidad.
    """
    comprobadas = 0
    for fila in leido.rows:
        assert fila.claimed.unit_length is not None, fila.code

        if fila.code not in SIN_ORACULO_DEL_TOTAL:
            piezas = (fila.quantity or 0) * (fila.elements or 1)
            assert fila.claimed.total_length == pytest.approx(
                fila.claimed.unit_length * piezas, abs=0.02
            ), fila.code
            comprobadas += 1

        if fila.code in FILAS_CON_MULTIPLICADOR:
            continue
        suma = sum(m.value for m in fila.dimensions)
        assert suma == pytest.approx(fila.claimed.unit_length * 100, abs=0.5), fila.code

    # Que el bucle haya corrido de verdad: un oráculo que no comprueba nada
    # pasa siempre, y es la forma más fácil de que una prueba mienta.
    assert comprobadas == len(FILAS) - len(SIN_ORACULO_DEL_TOTAL)


def test_los_diametros_y_las_veces_salen_enteros(leido) -> None:
    """
    El diámetro es el dato sin el cual una fila no se puede cargar, y su rótulo
    es un glifo —«Ø»— que el reconocedor no tiene en su diccionario: lo lee como
    «O», con 0,40 de puntaje. Sin aceptar eso, la columna quedaba sin rótulo y
    sus valores se descartaban en silencio.
    """
    por_codigo = {f.code: f for f in leido.rows}

    assert [por_codigo[f[0]].diameter_mm for f in FILAS] == [float(f[1]) for f in FILAS]
    assert [por_codigo[f[0]].quantity for f in FILAS] == [int(f[9]) for f in FILAS]
    assert [por_codigo[f[0]].elements for f in FILAS] == [int(f[10]) for f in FILAS]


def test_las_rayas_no_ensucian_las_filas(leido) -> None:
    """
    Cinco columnas de medida por catorce filas: sin tratar «——» como celda
    vacía, el documento llegaba con medio centenar de problemas inventados
    tapando los dos que son de verdad.
    """
    con_problemas = {f.code for f in leido.rows if f.issues}
    assert con_problemas == FILAS_CON_MULTIPLICADOR, [
        (f.code, f.issues) for f in leido.rows if f.issues
    ]


def test_una_medida_que_no_se_pudo_leer_lo_dice_con_la_celda_a_la_vista(leido) -> None:
    """
    «2x24» son dos lados de 24 de un estribo cerrado. Leerlo como 48 daría bien
    la longitud total y MAL la figura, porque 24 es el lado.

    Así que no se interpreta — y no se calla: el problema nombra la celda tal
    como está impresa, que es lo que deja a quien revisa cargarla a mano.
    """
    p3 = next(f for f in leido.rows if f.code == "3")

    assert any("2x24" in problema for problema in p3.issues)
    assert p3.dimensions == []
    # Y la fila sigue llegando: su diámetro, su cantidad y lo que el papel
    # afirma están leídos, que es casi todo el trabajo de cargarla.
    assert (p3.diameter_mm, p3.quantity, p3.claimed.total_length) == (8.0, 3, 7.8)


# ── Lo que el reconocimiento NO tiene que hacer ────────────────────────────


def test_no_se_inventa_ni_un_caracter(leido) -> None:
    """
    La pasada que lee una celda sola usa un umbral de detección mucho más bajo
    que la de la página, y eso tiene un precio que se midió: a 0,15 el
    reconocedor empezó a devolver ideogramas leídos de celdas VACÍAS.

    En una planilla boliviana un ideograma no es una lectura difícil: es una
    invención, y este módulo no inventa. Lo que no entra en el alfabeto de una
    planilla se descarta.
    """
    textos = [h for h in leido.columns.headers_raw if h]
    for fila in leido.rows:
        textos += [fila.code, fila.sketch or "", *fila.cells.values(), *fila.unmapped.values()]

    for texto in (t for t in textos if t):
        assert it._es_texto_plausible(texto), repr(texto)


@pytest.mark.parametrize(
    ("texto", "plausible"),
    [
        ("POS.", True),
        ("LONG. TOT.(m.)", True),
        ("PESO +7% (Kg.)", True),
        ("Ø", True),
        ("2x24", True),
        ("——", True),
        ("DIÁMETRO", True),
        # Lo que devuelve el reconocedor cuando se lo fuerza sobre una celda
        # vacía: el carácter que más se parece a la mancha que encontró.
        ("一", False),
        ("中文", False),
        ("POS.一", False),
    ],
)
def test_el_alfabeto_de_una_planilla(texto: str, plausible: bool) -> None:
    assert it._es_texto_plausible(texto) is plausible


# ── El piso de resolución ──────────────────────────────────────────────────


def _reducir(data: bytes, ancho: int) -> bytes:
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img = img.resize((ancho, round(img.height * ancho / img.width)), Image.LANCZOS)
    buffer = io.BytesIO()
    img.save(buffer, "PNG")
    return buffer.getvalue()


def _es_subsecuencia(chicos: list[float], grandes: list[float]) -> bool:
    """Si `chicos` aparece dentro de `grandes` en el mismo orden."""
    quedan = iter(grandes)
    return all(any(x == y for y in quedan) for x in chicos)


def test_a_ninguna_resolucion_se_desordena_ni_se_cambia_una_medida(imagen: bytes) -> None:
    """
    La garantía de este módulo, y la única que no se negocia.

    Las medidas son los TRAMOS de una figura y se consumen en orden, así que lo
    que hay que cuidar no es cómo se llaman sino en qué orden salen. La
    secuencia leída tiene que ser una SUBSECUENCIA de la del papel:

      - perder un tramo está permitido — queda en blanco, se ve en la pantalla
        de revisión, y el recálculo contra la longitud declarada no cierra;
      - reordenarlos no, y cambiar un valor tampoco. Las dos cosas rompen la
        subsecuencia, y las dos producen una fila verosímil y falsa.

    ── Lo que esta prueba encontró ────────────────────────────────────────

    Que el lector SÍ desordenaba. Las medidas se armaban recorriendo las
    columnas ordenadas por LETRA, y a baja resolución el «b» del encabezado se
    lee «d»: la fila 13, que en el papel dice 65, 155, 10, salía 65, 10, 155.
    Los tres suman lo mismo, así que la longitud cerraba, el total cerraba y el
    peso cerraba — sin una sola señal, salvo la barra doblada mal en la obra.

    El arreglo es ordenar por ÍNDICE DE COLUMNA, que es el orden del papel y no
    depende de haber leído bien un glifo. Con eso, medido sobre esta planilla y
    sobre la misma sin sus columnas de resultado, en seis anchos entre 1600 y
    900 px: ninguna medida desordenada y ninguna cambiada, en ningún ancho.
    """
    from pdf_parser import ParseError

    esperadas = {
        f[0]: [float(v) for v in f[3:8] if v != "——"]
        for f in FILAS
        if f[0] not in FILAS_CON_MULTIPLICADOR
    }
    aceptadas = comprobadas = 0

    for ancho in (1400, 1200, 1100, 1000):
        try:
            leido = parse_imagen(_reducir(imagen, ancho))
        except ParseError:
            # Negarse es una respuesta correcta: lo que no se admite es
            # devolver la planilla con las medidas corridas.
            continue

        aceptadas += 1
        for fila in leido.rows:
            # A 1000 px un «2» de la columna de posición sale «00». Una posición
            # con un código que no está en el papel no se puede comprobar contra
            # el papel, y tampoco hace falta: el código es un rótulo, se ve en la
            # pantalla y no mueve ninguna medida de lugar.
            if fila.code not in esperadas:
                continue
            papel = esperadas[fila.code]
            leidas = [m.value for m in fila.dimensions]
            assert _es_subsecuencia(leidas, papel), (
                f"a {ancho} px la fila {fila.code} salió {leidas} y el papel dice "
                f"{papel}: hay una medida cambiada o fuera de orden"
            )
            comprobadas += 1

    # Que el bucle haya tenido algo que comprobar: si todos los anchos se
    # rechazaran, la prueba pasaría sin haber mirado una sola medida.
    assert aceptadas >= 3, f"sólo {aceptadas} de 4 anchos se leyeron"
    assert comprobadas > 20, f"sólo {comprobadas} filas comprobadas"


def test_un_rotulo_de_medida_mal_leido_se_detecta() -> None:
    """
    Las letras de una planilla son una tira seguida desde «a», así que un
    encabezado que dice «a d c e» está mal leído — y se puede afirmar sin saber
    nada de la imagen.

    No es un rechazo: con las medidas ordenadas por columna el rótulo mal leído
    es un cartel equivocado, no una figura equivocada. Es un aviso, y sirve para
    dos cosas — que el nombre que muestra la pantalla no se tome por el del
    papel, y que se sepa que puede faltar la columna cuyo rótulo quedó repetido.
    """
    from pdf_parser import _problema_de_las_letras

    # Lo que salió de verdad a 1088 px: «b» leída como «d», y la «d» perdida.
    assert _problema_de_las_letras({"a": 3, "d": 4, "c": 5, "e": 7}) is not None
    # Un hueco, sin desorden: «b» no se leyó y las demás sí.
    assert _problema_de_las_letras({"a": 3, "c": 5, "d": 6}) is not None
    # Y lo que sí es un encabezado de planilla.
    assert _problema_de_las_letras({"a": 3, "b": 4, "c": 5, "d": 6, "e": 7}) is None
    assert _problema_de_las_letras({"a": 3}) is None
    assert _problema_de_las_letras({}) is None


def test_una_imagen_justa_se_lee_y_lo_avisa(imagen: bytes) -> None:
    """
    Entre los dos umbrales la planilla SÍ se devuelve, con un aviso.

    Es la banda donde se pierden celdas y no se cambian ninguna: a 1400 px de
    ancho —unos 68 px por columna— las dos planillas medidas perdieron celdas y
    no cambiaron un solo valor. Negarse ahí sería tirar una planilla que se
    puede revisar; callarse sería dejar a alguien buscando por qué faltan
    medidas que el papel tiene.
    """
    leido = parse_imagen(_reducir(imagen, 1400))

    assert [f.code for f in leido.rows] == [f[0] for f in FILAS]
    assert any("resolución" in aviso for aviso in leido.warnings), leido.warnings


def test_a_resolucion_completa_no_avisa_de_resolucion(leido) -> None:
    """El aviso tiene que aparecer cuando corresponde y no siempre."""
    assert not any("resolución" in aviso for aviso in leido.warnings)


def test_por_debajo_del_minimo_se_lee_si_la_planilla_puede_verificarse(imagen: bytes) -> None:
    """
    Por debajo del piso de resolución la imagen NO se rechaza por ser chica.

    Se rechazaba, y estaba mal: este mismo documento de obra —unos 51 píxeles
    por columna— es el que el usuario tiene, y negarse le dejaba cero filas
    donde antes tenía la planilla entera para revisar.

    Lo que sostiene la lectura no es el tamaño sino que la planilla declara su
    longitud y su total: el importador recalcula la longitud desde las medidas y
    el total desde la longitud por la cantidad por las veces, y la fila que no
    cierra queda bloqueada a la vista. Un dígito mal leído rompe una de las dos
    cuentas; el único cambio que las dos toleraban era la permutación de letras,
    y ésa ya se corta en el encabezado.

    Así que se lee, se avisa con el número, y se recomienda el PDF.
    """
    leido = parse_imagen(_reducir(imagen, 1000))

    assert leido.rows, "no devolvió ninguna fila"
    aviso = [a for a in leido.warnings if "resoluci" in a]
    assert len(aviso) == 1, f"se esperaba un solo aviso de resolución: {leido.warnings}"
    assert str(it.PX_POR_COLUMNA_MINIMO) in aviso[0]
    assert "PDF" in aviso[0]


def test_por_debajo_del_minimo_y_sin_con_que_verificar_se_rechaza() -> None:
    """
    El complemento, y el que sostiene la decisión de arriba.

    La misma planilla dibujada SIN las columnas de longitud y de total: a esa
    resolución se devolverían medidas que nadie puede desmentir, porque no hay
    ninguna cuenta que las contradiga. Ahí sí se niega, y dice por qué.

    Si esta prueba se pusiera verde sola, la de arriba dejaría de ser una
    decisión medida y pasaría a ser «leer siempre».
    """
    from pdf_parser import ImageTooSmallError

    sin_oraculo = _dibujar(sin_columnas=COLUMNAS_DEL_ORACULO)

    with pytest.raises(ImageTooSmallError) as e:
        parse_imagen(_reducir(sin_oraculo, 1000))

    mensaje = str(e.value)
    assert str(it.PX_POR_COLUMNA_MINIMO) in mensaje
    assert "longitud" in mensaje
    # Y dice lo único que resuelve el problema de verdad.
    assert "PDF" in mensaje


def test_sin_la_linea_corta_los_dos_pisos_son_de_verdad_una_celda() -> None:
    """
    El complemento del primer test: cuando la línea NO está dibujada, los dos
    pisos son una sola celda — el rótulo «a» y «DIMENSIONES (cm.)» están en la
    misma celda del papel, y no hay nada que reconstruir.

    Lo que esto confirma es que el detector encuentra una línea que EXISTE en vez
    de inventar una que convenga: sin dibujarla, tiene que haber una fila menos.

    ── Por qué ya no se comprueba con NO_HEADER ───────────────────────────

    Porque el documento ya no se cae: sin encabezado legible, el lector deduce
    qué es cada columna de las cuentas del propio papel. Eso está bien y es lo
    que se pidió, pero deja de servir para probar el detector de líneas — una
    prueba que pasa por dos mecanismos distintos no dice cuál de los dos
    funciona. Así que la geometría se comprueba en la geometría, y acá abajo se
    comprueba lo otro: que el rescate ocurre y que se avisa.
    """
    sin_linea = _dibujar(con_linea_del_segundo_piso=False)

    gris = it.enderezar(it.abrir_imagen(sin_linea))
    binaria = it._binarizar(gris)
    horizontal, vertical = it._mascaras_de_regla(binaria)
    bloque = it._bloques_de_tabla(horizontal, vertical, gris.shape)[0]
    xs = it._cortes_verticales(vertical, bloque)
    ys = it._cortes_horizontales(horizontal, bloque, xs)

    assert len(xs) - 1 == len(ANCHOS), f"{len(xs) - 1} columnas"
    # Un piso de encabezado en lugar de dos, y las catorce filas.
    assert len(ys) - 1 == 1 + len(FILAS), f"{len(ys) - 1} filas"

    # Y la planilla se lee igual, por la otra vía, diciéndolo.
    leido = parse_imagen(sin_linea)
    assert [f.code for f in leido.rows] == [f[0] for f in FILAS]
    assert any("dedujo" in a for a in leido.warnings), leido.warnings


def test_la_columna_de_dibujos_no_se_toma_por_seccion_ni_por_croquis() -> None:
    """
    Lo que ensuciaba las catorce filas del documento del usuario.

    Su planilla trae las dos cosas a la vez: una columna «TIPO» con la letra de
    la figura y una «ESQUEMA» con los dibujos, que en el papel es UNA celda alta
    con los nueve croquis adentro. Leída de una imagen, esa celda devuelve los
    rótulos de todos los tramos pegados:

        «abcd-- a ahc ah- abG- abcde-- ahc. ab»

    Y de ahí salían dos cosas mal, las dos visibles en la pantalla de revisión:

      1. Esa cadena aparecía como NOMBRE DE SECCIÓN en las catorce filas. La
         columna se reasignaba a sección cuando su contenido «parecía una
         etiqueta», y «abcd» y «ahc» son palabras de tres letras para cualquier
         criterio que mire el texto: no hay forma de distinguirlas de «VIG» o
         «COL», que son secciones de verdad. Ahora lo decide el ENCABEZADO, y
         «ESQUEMA» nombra un dibujo y nunca una sección.

      2. «TIPO» quedaba sin reconocer, porque era un sinónimo de croquis y el
         campo se lo llevaba «ESQUEMA». Ya no lo es: la letra de la figura no es
         un rótulo de tramo — contarla decía «esta figura usa una medida» en una
         fila que trae cuatro.

    Las dos columnas viajan crudas entre las no reconocidas, que es lo que
    corresponde con una columna que el lector todavía no interpreta: mostrar que
    está, sin inventarle un significado.
    """
    leido = parse_imagen(_dibujar_con_la_leyenda())

    assert [f.section for f in leido.rows] == [None] * len(leido.rows)

    sueltas = {leido.columns.headers_raw[i] for i in leido.columns.unmapped}
    assert any("ESQUEMA" in h for h in sueltas), sueltas
    assert any("TIPO" in h for h in sueltas), sueltas
    assert "section" not in leido.columns.mapped
    assert "sketch" not in leido.columns.mapped

    # Y la planilla se sigue leyendo: el arreglo suelta dos columnas, no rompe
    # las demás.
    assert [f.code for f in leido.rows] == [f[0] for f in FILAS]
    assert set(leido.columns.dimension_columns) == {"a", "b", "c", "d", "e"}


# ── A qué escala se le presenta la tabla al reconocedor ────────────────────


def _en_un_cuadro_mas_grande(data: bytes, veces: float) -> bytes:
    """
    La misma tabla, con papel alrededor: una foto sacada de lejos.

    Los píxeles de la tabla son EXACTAMENTE los mismos; lo único que cambia es
    cuánto cuadro hay alrededor. Es la forma de aislar la pregunta, porque una
    foto de verdad sacada de más lejos tendría además menos detalle.
    """
    tabla = Image.open(io.BytesIO(data)).convert("RGB")
    ancho, alto = tabla.size
    W, H = int(ancho * veces), int(alto * veces)
    fondo = Image.new("RGB", (W, H), (208, 205, 198))
    d = ImageDraw.Draw(fondo)
    # Algo de textura: un fondo plano perfecto no existe en una foto.
    for k in range(0, W, 37):
        d.line([(k, 0), (k + 11, H)], fill=(198, 195, 188))
    fondo.paste(tabla, ((W - ancho) // 2, (H - alto) // 2))
    buffer = io.BytesIO()
    fondo.save(buffer, "PNG")
    return buffer.getvalue()


def test_una_imagen_chica_se_amplia_antes_de_leerla() -> None:
    """
    Una imagen por debajo del tamaño de trabajo se AMPLÍA, y eso recupera celdas.

    El guardarraíl decía «por debajo de mil píxeles no se agranda: no hay
    información que recuperar». Lo primero es cierto y lo segundo no viene al
    caso — el reconocedor tiene una altura de texto preferida, y presentarle la
    misma información a esa altura no inventa nada.

    El efecto era que el guardarraíl se activaba justo en el documento que más
    ayuda necesita: el de obra que trajo el usuario mide exactamente 1000 px.
    Medido sobre la réplica a esa resolución, 9 filas y 8 cantidades sin ampliar
    contra 12 y 11 ampliando.
    """
    chica = _reducir(_dibujar(), 1000)
    gris = it.abrir_imagen(chica)

    assert max(gris.shape) > 1000, "una imagen de 1000 px tiene que ampliarse"
    # Y no sin tope: interpolar de más sólo agrega borrosidad.
    assert max(gris.shape) <= 1000 * it.MAX_AMPLIACION + 2


def test_la_ampliacion_tiene_tope_y_reducir_no() -> None:
    """
    Los dos casos son distintos y el código los trata distinto.

    Reducir descarta detalle que sobra, y el tope de abajo es dónde el
    reconocedor deja de leer más por mirar más grande. Ampliar no agrega
    detalle: agranda lo que hay. Medido, la misma tabla de 1000 px leída a 1400
    daba 13 filas de 14 y leída a 2400 daba 11 — pasado el óptimo, ampliar
    empeora.
    """
    import numpy as np

    chica = np.full((300, 1000), 255, dtype=np.uint8)
    grande = np.full((1200, 4000), 255, dtype=np.uint8)

    assert max(it._a_tamano_de_trabajo(chica).shape) <= 1000 * it.MAX_AMPLIACION + 2
    assert max(it._a_tamano_de_trabajo(grande).shape) == it.LADO_LARGO_OBJETIVO


def test_una_tabla_que_es_parte_del_cuadro_se_recorta_sola() -> None:
    """
    La respuesta a «¿tengo que recortar la foto a la tabla?»: no.

    La imagen entera se llevaba al tamaño de trabajo ANTES de buscar la tabla,
    así que una tabla que ocupa un tercio del cuadro se quedaba con un tercio de
    los píxeles que el reconocedor iba a mirar. Con esta misma tabla dentro de un
    cuadro 2,5 veces más grande —una foto sacada de lejos— el documento no se
    leía: se caía sin reconocer el encabezado.

    Y no hace falta pedirle a nadie que recorte, porque el lector ya encontró la
    tabla él mismo con las líneas de la regla. Así que se acerca a ella y vuelve
    a mirar.
    """
    tabla_sola = _reducir(_dibujar(), 1000)
    de_lejos = _en_un_cuadro_mas_grande(tabla_sola, 2.5)

    leido = parse_imagen(de_lejos)

    # Se lee entera, con los mismos píxeles de tabla que antes no alcanzaban.
    assert [f.code for f in leido.rows] == [f[0] for f in FILAS]
    assert set(leido.columns.dimension_columns) == {"a", "b", "c", "d", "e"}

    # Y ninguna medida cambiada ni fuera de orden, que es la condición de
    # siempre: acercarse tiene que recuperar celdas, no inventarlas.
    esperadas = {
        f[0]: [float(v) for v in f[3:8] if v != "——"]
        for f in FILAS
        if f[0] not in FILAS_CON_MULTIPLICADOR
    }
    for fila in leido.rows:
        if fila.code not in esperadas:
            continue
        assert _es_subsecuencia([m.value for m in fila.dimensions], esperadas[fila.code]), (
            f"fila {fila.code}: {[m.value for m in fila.dimensions]} "
            f"contra {esperadas[fila.code]}"
        )


def test_una_tabla_que_llena_el_cuadro_no_se_recorta() -> None:
    """
    El complemento: cuando la tabla ya se lleva los píxeles no hay nada que
    ganar, y volver a escalarla sería pasarla dos veces por la interpolación.

    Es el caso de una planilla escaneada, y también el de la foto que el usuario
    subió — su tabla ocupa casi todo el ancho de la imagen.
    """
    tabla_sola = _reducir(_dibujar(), 1400)
    gris = it.enderezar(it.abrir_imagen(tabla_sola))
    binaria = it._binarizar(gris)
    horizontal, vertical = it._mascaras_de_regla(binaria)
    bloques = it._bloques_de_tabla(horizontal, vertical, gris.shape)

    assert it._recortar_a_la_tabla(gris, bloques, ampliacion_ya_usada=1.0) is None


# ── A qué escala se le presenta la tabla al reconocedor ────────────────────


def _en_un_cuadro_mas_grande(data: bytes, veces: float) -> bytes:
    """
    La misma tabla, con papel alrededor: una foto sacada de lejos.

    Los píxeles de la tabla son EXACTAMENTE los mismos; lo único que cambia es
    cuánto cuadro hay alrededor. Es la forma de aislar la pregunta, porque una
    foto de verdad sacada de más lejos tendría además menos detalle.
    """
    tabla = Image.open(io.BytesIO(data)).convert("RGB")
    ancho, alto = tabla.size
    W, H = int(ancho * veces), int(alto * veces)
    fondo = Image.new("RGB", (W, H), (208, 205, 198))
    d = ImageDraw.Draw(fondo)
    # Algo de textura: un fondo plano perfecto no existe en una foto.
    for k in range(0, W, 37):
        d.line([(k, 0), (k + 11, H)], fill=(198, 195, 188))
    fondo.paste(tabla, ((W - ancho) // 2, (H - alto) // 2))
    buffer = io.BytesIO()
    fondo.save(buffer, "PNG")
    return buffer.getvalue()


def test_una_imagen_chica_se_amplia_antes_de_leerla() -> None:
    """
    Una imagen por debajo del tamaño de trabajo se AMPLÍA, y eso recupera celdas.

    El guardarraíl decía «por debajo de mil píxeles no se agranda: no hay
    información que recuperar». Lo primero es cierto y lo segundo no viene al
    caso — el reconocedor tiene una altura de texto preferida, y presentarle la
    misma información a esa altura no inventa nada.

    El efecto era que el guardarraíl se activaba justo en el documento que más
    ayuda necesita: el de obra que trajo el usuario mide exactamente 1000 px.
    Medido sobre la réplica a esa resolución, 9 filas y 8 cantidades sin ampliar
    contra 12 y 11 ampliando, sin una sola medida cambiada.
    """
    chica = _reducir(_dibujar(), 1000)
    gris = it.abrir_imagen(chica)

    assert max(gris.shape) > 1000, "una imagen de 1000 px tiene que ampliarse"
    # Y no sin tope: interpolar de más sólo agrega borrosidad.
    assert max(gris.shape) <= 1000 * it.MAX_AMPLIACION + 2


def test_la_ampliacion_tiene_tope_y_reducir_no() -> None:
    """
    Los dos casos son distintos y el código los trata distinto.

    Reducir descarta detalle que sobra, y `LADO_LARGO_OBJETIVO` es donde el
    reconocedor deja de leer más por mirar más grande. Ampliar no agrega detalle:
    agranda lo que hay. Medido, la misma tabla de 1000 px leída a 1400 daba 13
    filas de 14 y leída a 2400 daba 11 — pasado el óptimo, ampliar empeora.
    """
    chica = np.full((300, 1000), 255, dtype=np.uint8)
    grande = np.full((1200, 4000), 255, dtype=np.uint8)

    assert max(it._a_tamano_de_trabajo(chica).shape) <= 1000 * it.MAX_AMPLIACION + 2
    assert max(it._a_tamano_de_trabajo(grande).shape) == it.LADO_LARGO_OBJETIVO


def test_una_tabla_que_es_parte_del_cuadro_se_recorta_sola() -> None:
    """
    La respuesta a «¿tengo que recortar la foto a la tabla?»: no.

    La imagen entera se llevaba al tamaño de trabajo ANTES de buscar la tabla,
    así que una tabla que ocupa un tercio del cuadro se quedaba con un tercio de
    los píxeles que el reconocedor iba a mirar. Con esta misma tabla dentro de un
    cuadro 2,5 veces más grande —una foto sacada de lejos— el documento no se
    leía: se caía sin reconocer el encabezado.

    Y no hace falta pedirle a nadie que recorte, porque el lector ya encontró la
    tabla él mismo con las líneas de la regla. Así que se acerca a ella y vuelve
    a mirar.
    """
    tabla_sola = _reducir(_dibujar(), 1000)
    de_lejos = _en_un_cuadro_mas_grande(tabla_sola, 2.5)

    leido = parse_imagen(de_lejos)

    # Se lee entera, con los mismos píxeles de tabla que antes no alcanzaban.
    assert [f.code for f in leido.rows] == [f[0] for f in FILAS]
    assert set(leido.columns.dimension_columns) == {"a", "b", "c", "d", "e"}

    # Y ninguna medida cambiada ni fuera de orden, que es la condición de
    # siempre: acercarse tiene que recuperar celdas, no inventarlas.
    esperadas = {
        f[0]: [float(v) for v in f[3:8] if v != "——"]
        for f in FILAS
        if f[0] not in FILAS_CON_MULTIPLICADOR
    }
    for fila in leido.rows:
        if fila.code not in esperadas:
            continue
        assert _es_subsecuencia([m.value for m in fila.dimensions], esperadas[fila.code]), (
            f"fila {fila.code}: {[m.value for m in fila.dimensions]} "
            f"contra {esperadas[fila.code]}"
        )


def test_una_tabla_que_llena_el_cuadro_no_se_recorta() -> None:
    """
    El complemento: cuando la tabla ya se lleva los píxeles no hay nada que
    ganar, y volver a escalarla sería pasarla dos veces por la interpolación.

    Medido: una tabla de 1000 px dentro de un cuadro de 1600 se leía en 14 filas
    con una sola ampliación y en 12 con dos, para el mismo tamaño final.
    Interpolar lo interpolado no agrega detalle y sí agrega borrosidad.

    Es el caso de una planilla escaneada, y también el de la foto que el usuario
    subió — su tabla ocupa casi todo el ancho de la imagen.
    """
    tabla_sola = _reducir(_dibujar(), 1400)
    gris = it.enderezar(it.abrir_imagen(tabla_sola))
    binaria = it._binarizar(gris)
    horizontal, vertical = it._mascaras_de_regla(binaria)
    bloques = it._bloques_de_tabla(horizontal, vertical, gris.shape)

    assert it._recortar_a_la_tabla(gris, bloques, ampliacion_ya_usada=1.0) is None

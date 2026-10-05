"""
Celdas que no se leyeron y que el documento DETERMINA.

── El problema, con números ────────────────────────────────────────────────

La «PLANILLA DE FIERROS - GRADA TIPO 1» leída de una foto de 1000 px llegaba a
la pantalla con seis de sus catorce filas bloqueadas por el mismo motivo: «la
fila no trae cantidad de piezas». La columna «CANT.» es angosta y sus números
tienen una o dos cifras, así que es la primera que se pierde.

Pero la cantidad no hay que adivinarla. El papel afirma

    longitud total = longitud × cantidad × veces

y de esos cuatro números leyó tres. El que falta está despejado: 46,00 ÷ 5,75 =
8. No es una heurística, es la ecuación del documento.

Estas pruebas no usan OCR: arman la matriz de celdas a mano, con las filas del
papel y la celda borrada. Lo que se prueba es la aritmética, y para eso el
reconocimiento de caracteres sólo agregaría lentitud y ruido.
"""

from __future__ import annotations

from models import Dimension
from pdf_parser import _deducir_piezas, _interpretar, _leer_multiplicador, _Tabla

RAYA = chr(8212) * 2

ARRIBA = [
    "POS.", "Ø", "TIPO", "DIMENSIONES (cm.)", None, None, None, None,
    "LONG. (m.)", "CANT.", "VECES", "LONG. TOT.(m.)",
]
ABAJO = [None, None, None, "a", "b", "c", "d", "e", None, None, None, None]


def _leer(filas: list[list[str | None]]):
    return _interpretar(
        [_Tabla(1, [ARRIBA, ABAJO, *filas])], total_paginas=1, warnings=[], source="image"
    )


def _despejar(medidas, largo="5.75", total="46.00", cantidad=None, veces=1):
    """`_deducir_piezas` con las dos columnas presentes, que es el caso normal."""
    return _deducir_piezas(
        medidas, largo, float(largo), total, float(total), cantidad, veces,
        hay_columna_de_cantidad=True, hay_columna_de_veces=True,
    )


# ── La cantidad que el papel determina ─────────────────────────────────────


def test_la_cantidad_borrada_se_despeja_de_la_planilla() -> None:
    """
    Las filas 2 y 6 del papel con la columna «CANT.» en blanco.

    46,00 ÷ 5,75 = 8 y 108,10 ÷ 1,15 = 94, que es lo que dice el papel. Las dos
    filas pasan de bloqueadas a cargables, con la cuenta a la vista.
    """
    leido = _leer([
        ["2", "10", "J", "15", "95", "410", "55", RAYA, "5.75", None, "1", "46.00"],
        ["6", "8", "E", "115", RAYA, RAYA, RAYA, RAYA, "1.15", None, "1", "108.10"],
    ])

    por_codigo = {f.code: f for f in leido.rows}
    assert por_codigo["2"].quantity == 8
    assert por_codigo["6"].quantity == 94

    # Y lo dice, porque una cantidad deducida no es una cantidad leída.
    for codigo in ("2", "6"):
        assert any("dedujo" in p for p in por_codigo[codigo].issues), por_codigo[codigo].issues


def test_una_cantidad_que_no_da_un_entero_no_se_deduce() -> None:
    """
    La guarda, y la razón por la que esto no es adivinar.

    Las piezas se cuentan de una en una. Un cociente de 4,06 no es una cantidad
    que no se leyó: es una señal de que alguno de los otros números está mal. Y
    ahí lo correcto es dejar la celda en blanco, que se ve, y bloquear la fila.
    """
    leido = _leer([
        ["9", "10", "I", "110", RAYA, RAYA, RAYA, RAYA, "1.10", None, "1", "4.47"],
    ])

    assert leido.rows[0].quantity is None
    assert not any("dedujo" in p for p in leido.rows[0].issues)


def test_sin_medidas_que_corroboren_la_longitud_no_se_deduce_nada() -> None:
    """
    La otra guarda, y la que sostiene todo lo demás.

    La cantidad se despeja DE la longitud, así que si la longitud también
    pudiera estar mal leída el resultado sería una cadena de suposiciones con
    aspecto de dato. La condición es que la suma de las medidas confirme la
    longitud por una ecuación distinta de la que se va a despejar.

    Acá no hay ninguna medida: no hay con qué corroborar, así que no se deduce.
    """
    assert _deducir_piezas([], "5.75", 5.75, "46.00", 46.00, None, 1, hay_columna_de_cantidad=True, hay_columna_de_veces=True)[0] is None

    # Y con las medidas puestas, la misma fila sí.
    medidas = [Dimension(name="a", value=15.0), Dimension(name="b", value=560.0)]
    assert _despejar(medidas)[0] == 8


def test_la_corroboracion_no_necesita_saber_la_unidad() -> None:
    """
    575 cm contra 5,75 m es la misma longitud en dos unidades, y en esta planilla
    las dos columnas están así de verdad.

    Por eso la corroboración pregunta si la razón entre la suma y la longitud es
    una de las que separan milímetros, centímetros y metros, en vez de exigir que
    sean el mismo número. Cuál unidad es cuál lo resuelve después
    `_resolver_unidades`, con el documento completo a la vista.
    """
    en_cm = [Dimension(name="a", value=15.0), Dimension(name="b", value=560.0)]
    en_m = [Dimension(name="a", value=0.15), Dimension(name="b", value=5.60)]

    assert _despejar(en_cm)[0] == 8
    assert _despejar(en_m)[0] == 8

    # Y una suma que no guarda NINGUNA de esas razones con la longitud no
    # corrobora nada: es otra columna, no la misma en otra unidad.
    ajena = [Dimension(name="a", value=37.0)]
    assert _despejar(ajena)[0] is None


def test_con_la_cantidad_y_las_veces_en_blanco_se_dice_lo_que_se_supuso() -> None:
    """
    La fila 7 del papel son 2 piezas × 2 veces = 4 piezas de 1,55 m.

    Con las dos columnas en blanco, lo que la planilla determina es el PRODUCTO y
    no el reparto: 4 × 1 y 2 × 2 dan las mismas cuatro piezas y el mismo acero.
    Se despeja el producto y se dice que el reparto se supuso, porque en la
    pantalla se va a ver de una forma que el papel escribe de otra.
    """
    leido = _leer([
        ["7", "16", "L", "15", "140", RAYA, RAYA, RAYA, "1.55", None, None, "6.20"],
    ])

    fila = leido.rows[0]
    assert (fila.quantity or 0) * (fila.elements or 1) == 4
    assert any("no cómo se reparte" in p for p in fila.issues), fila.issues


def test_las_veces_borradas_tambien_se_despejan() -> None:
    """La simétrica: la cantidad se leyó y las veces no."""
    leido = _leer([
        ["7", "16", "L", "15", "140", RAYA, RAYA, RAYA, "1.55", "2", None, "6.20"],
    ])

    assert leido.rows[0].elements == 2
    assert any("veces" in p and "dedujeron" in p for p in leido.rows[0].issues)


# ── La notación «2x24» ─────────────────────────────────────────────────────


def test_el_multiplicador_es_de_la_formula_y_no_del_dato() -> None:
    """
    Lo que «2x24» significa, y lo que costó dos lecturas equivocadas entenderlo.

    Parecía «dos tramos de 24», y entonces la pregunta parecía ser en qué ORDEN
    van esos tramos —24-105-24-105 o 24-24-105-105— que la suma no puede
    responder. Por eso las dos primeras versiones se negaban a repartirlos.

    La pregunta no existe. En un estribo cerrado el lado ES 24 y el «×2» lo pone
    la FIGURA: el tipo «O» del sistema tiene fórmula `2*(a + b) + c`. El papel
    escribe en la celda lo que la fórmula va a multiplicar, así que la medida que
    se guarda es 24 — no 48 — y el orden lo sabe la fórmula, que es su trabajo.

    Las dos filas del papel lo confirman contra su longitud declarada:

        fila  3:  2×24 + 2×105 + 2×1 = 260 cm = los 2,60 m declarados
        fila 10:  2×19 + 2×19  + 2×2 =  80 cm = los 0,80 m declarados
    """
    tres = _leer([["3", "8", "O", "2x24", "2x105", "2x1", RAYA, RAYA, "2.60", "3", "1", "7.80"]])
    diez = _leer([["10", "6", "O", "2x19", "2x19", "2x2", RAYA, RAYA, "0.80", "12", "1", "17.60"]])

    assert [(m.name, m.value) for m in tres.rows[0].dimensions] == [
        ("a", 24.0), ("b", 105.0), ("c", 1.0)
    ]
    assert [(m.name, m.value) for m in diez.rows[0].dimensions] == [
        ("a", 19.0), ("b", 19.0), ("c", 2.0)
    ]

    # La cuenta que verifica es la del PAPEL —N×M— aunque se guarde M: es la
    # única forma de confirmar que la celda se leyó bien.
    aviso = " ".join(tres.rows[0].issues)
    assert "260" in aviso and "coherente" in aviso, tres.rows[0].issues
    # Un solo aviso, no uno por celda: tres «no es un número» tapaban los
    # problemas de verdad de las otras filas.
    assert not any("no es un número" in p for p in tres.rows[0].issues)


def test_el_aviso_dice_QUE_tipo_falta_en_el_catalogo() -> None:
    """
    La fórmula del papel no es la del tipo «O» de NAAU, y la diferencia es real:
    las dos cuentas de arriba multiplican el gancho por dos —el estribo del papel
    cierra con dos— y `2*(a + b) + c` lo suma una vez. Da 259 y 78 contra 260 y
    80.

    Un aviso que dijera sólo «creá un tipo» obligaría a deducir cuál. Este dice
    la fórmula, que es un dato que el lector tiene y quien lee no.
    """
    tres = _leer([["3", "8", "O", "2x24", "2x105", "2x1", RAYA, RAYA, "2.60", "3", "1", "7.80"]])

    aviso = " ".join(tres.rows[0].issues)
    assert "2*(a + b + c)" in aviso, tres.rows[0].issues


def test_los_dos_puntos_no_son_una_equis() -> None:
    """
    En la planilla del usuario «2x24» salió «2:04»: el reconocedor se equivocó en
    la equis Y en un dígito.

    Aceptar los dos puntos como multiplicador daría «2 tramos de 4» con aire de
    dato leído. Se rechaza, y la celda queda a la vista como lo que es: algo que
    no se pudo leer.
    """
    assert _leer_multiplicador("2x24", "us") == (2, 24.0)
    assert _leer_multiplicador("2X105", "us") == (2, 105.0)
    assert _leer_multiplicador("2×1", "us") == (2, 1.0)
    assert _leer_multiplicador("2:04", "us") is None
    assert _leer_multiplicador("140", "us") is None
    assert _leer_multiplicador("", "us") is None


def test_una_planilla_SIN_columna_de_veces_no_recibe_avisos_de_veces() -> None:
    """
    El defecto que esta prueba encontró, y que vale más que la función.

    La planilla piloto no tiene columna de «VECES»: no es que las veces no se
    leyeron, es que no existen, y el modelo ya las cuenta como una. La primera
    versión las despejaba igual y lo anunciaba, así que le ponía un aviso a cada
    una de sus 27 filas — una deducción correcta, inútil y ruidosa.

    Y el ruido es su propia clase de error: entierra los avisos que sí importan.
    Sólo se despeja un campo cuya COLUMNA existe.
    """
    sin_veces_arriba = [
        "POS.", "Ø", "TIPO", "DIMENSIONES (cm.)", None, None, None, None,
        "LONG. (m.)", "CANT.", "LONG. TOT.(m.)",
    ]
    sin_veces_abajo = [None, None, None, "a", "b", "c", "d", "e", None, None, None]

    leido = _interpretar(
        [_Tabla(1, [sin_veces_arriba, sin_veces_abajo,
                    ["2", "10", "J", "15", "95", "410", "55", RAYA, "5.75", "8", "46.00"]])],
        total_paginas=1, warnings=[], source="pdf",
    )

    fila = leido.rows[0]
    assert fila.quantity == 8
    assert not any("veces" in p for p in fila.issues), fila.issues


# ── Reparar una celda mal leída, con la suma como juez ─────────────────────


def test_una_celda_mal_leida_se_repara_si_la_suma_lo_confirma() -> None:
    """
    Lo que decide la reparación no es el parecido de los glifos: es la suma.

    En la foto del documento de obra la celda «2x24» salió una vez «2:04» y otra
    «2:24». Los dos son la misma confusión de la equis, pero en el primero el
    reconocedor además se equivocó en un dígito, y hay que aceptar uno y rechazar
    el otro:

        «2:24» → 2×24 = 48,  48 + 210 + 2 = 260 = los 2,60 m declarados → SE ACEPTA
        «2:04» → 2×4  =  8,   8 + 210 + 2 = 220 ≠ 260                   → SE RECHAZA

    Sin ese juez, aceptar los dos puntos habría metido «2 tramos de 4» con aire
    de dato leído. Con el juez, la planilla desmiente el que está mal.
    """
    bien = _leer([["3", "8", "O", "2:24", "2x105", "2x1", RAYA, RAYA, "2.60", "3", "1", "7.80"]])
    mal = _leer([["3", "8", "O", "2:04", "2x105", "2x1", RAYA, RAYA, "2.60", "3", "1", "7.80"]])

    reparada = [p for p in bien.rows[0].issues if "se leyó" in p]
    assert reparada, bien.rows[0].issues
    assert "«2x24»" in reparada[0]
    assert not any("no es un número" in p for p in bien.rows[0].issues)

    # Y la que no cierra queda sin leer, a la vista.
    assert not any("se leyó" in p for p in mal.rows[0].issues)
    assert any("no es un número" in p for p in mal.rows[0].issues), mal.rows[0].issues


def test_la_medida_reparada_entra_en_SU_columna() -> None:
    """
    Una medida recuperada tiene que quedar en su lugar, no al final.

    Las medidas son los tramos de la figura y se consumen en orden. Insertar la
    reparada donde toque es la misma lección que la de ordenar por columna y no
    por letra: una figura con los tramos permutados suma igual, cierra igual y
    sale doblada al revés.
    """
    leido = _leer([
        ["1", "12", "J", "15", "90", "43S", "45", RAYA, "5.85", "9", "1", "52.65"],
    ])

    assert [(m.name, m.value) for m in leido.rows[0].dimensions] == [
        ("a", 15.0), ("b", 90.0), ("c", 435.0), ("d", 45.0)
    ]


def test_una_reparacion_que_no_hace_cerrar_la_suma_se_rechaza() -> None:
    """
    «4S5» podría ser 455, y el glifo lo admite. La planilla no: 15 + 90 + 455 +
    45 = 605 y declara 5,85 m. Se rechaza y la celda queda en blanco, que se ve.
    """
    leido = _leer([
        ["1", "12", "J", "15", "90", "4S5", "45", RAYA, "5.85", "9", "1", "52.65"],
    ])

    valores = [m.value for m in leido.rows[0].dimensions]
    assert 455.0 not in valores
    assert any("no es un número" in p for p in leido.rows[0].issues)


def test_con_dos_celdas_sin_leer_no_se_repara_ninguna() -> None:
    """
    La suma es el juez, y con dos incógnitas deja de juzgar: hay infinitos pares
    que la hacen cerrar. Así que se repara UNA celda o ninguna.
    """
    leido = _leer([
        ["1", "12", "J", "1S", "90", "43S", "45", RAYA, "5.85", "9", "1", "52.65"],
    ])

    assert not any("se leyó" in p for p in leido.rows[0].issues)
    assert sum(1 for p in leido.rows[0].issues if "no es un número" in p) == 2

"""
Deducir qué es cada columna del CONTENIDO, sin mirar el rótulo.

── Qué se prueba, y contra qué ─────────────────────────────────────────────

Las filas son las de la «PLANILLA DE FIERROS - GRADA TIPO 1», un documento de
obra real, porque la planilla se verifica a sí misma: la suma de las medidas es
la longitud y el total es la longitud por la cantidad por las veces. Con números
inventados estas pruebas dirían «el buscador encuentra lo que yo escribí»; con
éstos dicen que encuentra el reparto que hace cerrar las cuentas de un papel.

Y las columnas se prueban DESORDENADAS, que es lo que el módulo promete: sin el
rótulo, la posición no significa nada, así que si el reparto depende del orden
esto lo tiene que delatar.
"""

from __future__ import annotations

import random

import pytest

from column_roles import DIAMETROS, inferir

# POS | Ø | a | b | c | d | e | LONG | CANT | VECES | TOTAL
PAPEL = [
    ["1", "12", "15", "90", "435", "45", None, "5.85", "9", "1", "52.65"],
    ["2", "10", "15", "95", "410", "55", None, "5.75", "8", "1", "46.00"],
    ["4", "12", "10", "125", "65", None, None, "2.00", "9", "1", "18.00"],
    ["5", "12", "65", "55", None, None, None, "1.20", "8", "1", "9.60"],
    ["6", "8", "115", None, None, None, None, "1.15", "94", "1", "108.10"],
    ["7", "16", "15", "140", None, None, None, "1.55", "2", "2", "6.20"],
    ["8", "12", "15", "140", None, None, None, "1.55", "2", "2", "6.20"],
    ["9", "10", "110", None, None, None, None, "1.10", "2", "2", "4.40"],
    ["11", "12", "15", "70", "335", "140", "10", "5.70", "9", "1", "51.30"],
    ["12", "10", "15", "80", "335", "120", "10", "5.60", "8", "1", "44.80"],
    ["13", "12", "65", "155", "10", None, None, "2.30", "8", "1", "18.40"],
    ["14", "12", "10", "95", "65", None, None, "1.70", "9", "1", "15.30"],
]

CODIGO, DIAMETRO, LARGO, CANTIDAD, VECES, TOTAL = 0, 1, 7, 8, 9, 10
MEDIDAS = [2, 3, 4, 5, 6]


def _mezclar(filas: list[list[str | None]], orden: list[int]) -> list[list[str | None]]:
    """Las mismas filas con las columnas en otro orden."""
    return [[fila[i] for i in orden] for fila in filas]


def test_sin_un_solo_rotulo_se_deduce_todo() -> None:
    """
    El caso de fondo: once columnas de números, ningún encabezado, y hay que
    decir qué es cada una.

    Lo que lo resuelve no es una heurística sobre nombres: es que la suma de
    cinco de esas columnas da otra columna en doce de doce filas, con un factor
    de 100 —las medidas están en centímetros y la longitud en metros—, y que esa
    longitud por dos de las columnas de enteros da otra en doce de doce.
    """
    r = inferir(PAPEL, "us")

    assert r is not None
    assert r.creible, (r.filas_que_cierran, r.filas_evaluadas)

    assert r.medidas == MEDIDAS
    assert r.factor_de_medidas == 100.0, "cm contra m: el factor sale de la cuenta"
    assert r.roles["unit_length"] == LARGO
    assert r.roles["total_length"] == TOTAL
    assert {r.roles["quantity"], r.roles["elements"]} == {CANTIDAD, VECES}
    assert r.roles["diameter"] == DIAMETRO
    assert r.roles["code"] == CODIGO


def test_la_cantidad_y_las_veces_no_se_confunden() -> None:
    """
    Dos columnas de enteros de una cifra, y hay que decir cuál es la cantidad.

    Por contenido son indistinguibles. Lo que las ordena es el producto: la
    planilla dice que la fila 7 son 2 piezas × 2 veces de 1,55 m = 6,20 m, y esa
    cuenta sólo cierra con cada una en su lugar.
    """
    r = inferir(PAPEL, "us")

    assert r is not None
    assert r.roles["quantity"] == CANTIDAD
    assert r.roles["elements"] == VECES


@pytest.mark.parametrize("semilla", [0, 1, 2, 3, 4, 5, 6, 7])
def test_el_orden_de_las_columnas_no_cambia_nada(semilla: int) -> None:
    """
    La promesa del módulo, probada en ocho barajados distintos.

    Si el reparto dependiera de la posición —de que el código esté primero o de
    que las medidas estén juntas— alguno de estos barajados lo rompería.
    """
    orden = list(range(len(PAPEL[0])))
    random.Random(semilla).shuffle(orden)
    donde = {viejo: nuevo for nuevo, viejo in enumerate(orden)}

    r = inferir(_mezclar(PAPEL, orden), "us")

    assert r is not None, f"orden {orden}"
    assert r.creible
    assert r.medidas == sorted(donde[m] for m in MEDIDAS), f"orden {orden}"
    assert r.roles["unit_length"] == donde[LARGO]
    assert r.roles["total_length"] == donde[TOTAL]
    assert r.roles["diameter"] == donde[DIAMETRO]
    assert r.roles["code"] == donde[CODIGO]
    assert {r.roles["quantity"], r.roles["elements"]} == {donde[CANTIDAD], donde[VECES]}


def test_a_una_planilla_sin_cuentas_no_se_le_inventa_un_reparto() -> None:
    """
    La propiedad que hace que esto sea seguro.

    Un montón de números que no satisfacen ninguna de las ecuaciones de una
    planilla no es una planilla, y la respuesta correcta es no devolver nada. Un
    reparto inventado que nada confirma no se distingue en la pantalla de uno
    correcto, y es el único error que este lector no se puede permitir.
    """
    basura = [
        ["1", "7", "13", "29", "41", "57"],
        ["2", "11", "17", "31", "43", "59"],
        ["3", "13", "19", "37", "47", "61"],
        ["4", "17", "23", "41", "53", "67"],
    ]
    assert inferir(basura, "us") is None


def test_con_dos_filas_no_alcanza() -> None:
    """
    Con dos filas hay demasiados subconjuntos y muy pocas ecuaciones: cualquier
    reparto cierra por casualidad. Sin evidencia, no se afirma nada.
    """
    assert inferir(PAPEL[:2], "us") is None


def test_las_medidas_perdidas_no_tumban_el_reparto() -> None:
    """
    A baja resolución se pierden celdas, y una fila a la que le falta un tramo no
    cierra por más que el reparto sea el correcto. Por eso la exigencia es de
    mayoría y no de unanimidad.

    Acá se borra una medida de tres filas —lo que hace una foto justa— y el
    reparto tiene que seguir saliendo igual.
    """
    roto = [list(f) for f in PAPEL]
    for r in (0, 4, 8):
        roto[r][3] = None

    r = inferir(roto, "us")

    assert r is not None
    assert r.medidas == MEDIDAS
    assert r.roles["unit_length"] == LARGO
    assert r.roles["total_length"] == TOTAL


def test_los_diametros_son_una_lista_cerrada() -> None:
    """
    El diámetro no se elige: se compra el que existe. Por eso se reconoce contra
    una lista y no por una ecuación — y por eso, si hubiera dos columnas que
    cumplen, no se elige ninguna.
    """
    assert 12 in DIAMETROS
    assert 6 in DIAMETROS
    assert 25.4 in DIAMETROS  # 1 pulgada, como se escribe en la columna
    assert 13 not in DIAMETROS


# ── De punta a punta, por el intérprete ────────────────────────────────────


def test_una_planilla_con_los_rotulos_ilegibles_se_lee_igual() -> None:
    """
    El caso que el diccionario de sinónimos no puede cubrir por definición.

    Una planilla fotografiada cuyos rótulos el reconocimiento destrozó: no hay
    un solo encabezado que el diccionario reconozca, y hasta acá el documento
    entero se caía con NO_HEADER. Las FILAS, en cambio, se leyeron bien.

    Lo que lo levanta son las cuentas del papel, y el resultado tiene que ser la
    planilla completa: las doce posiciones, las medidas en su orden, los
    diámetros, las cantidades y las veces.
    """
    from pdf_parser import _interpretar, _Tabla

    ilegibles = ["P0S", "$", "|", "II", "III", "IV", "V", "L0MG(rn)", "CAMT", "VEC3S", "T0T"]
    leido = _interpretar(
        [_Tabla(1, [ilegibles, *PAPEL])], total_paginas=1, warnings=[], source="image"
    )

    assert [f.code for f in leido.rows] == [f[0] for f in PAPEL]
    assert set(leido.columns.dimension_columns) == {"a", "b", "c", "d", "e"}

    por_codigo = {f.code: f for f in leido.rows}
    assert [por_codigo[f[0]].diameter_mm for f in PAPEL] == [float(f[1]) for f in PAPEL]
    assert [por_codigo[f[0]].quantity for f in PAPEL] == [int(f[8]) for f in PAPEL]
    assert [por_codigo[f[0]].elements for f in PAPEL] == [int(f[9]) for f in PAPEL]

    # Las medidas, en el orden del papel y con su letra.
    p11 = por_codigo["11"]
    assert [(m.name, m.value) for m in p11.dimensions] == [
        ("a", 15.0), ("b", 70.0), ("c", 335.0), ("d", 140.0), ("e", 10.0)
    ]

    # Y la longitud declarada, que es la que el recálculo usa para delatar un
    # dígito mal leído.
    assert p11.claimed.unit_length == 5.70
    assert p11.claimed.total_length == 51.30

    # Con el aviso puesto: una columna deducida no es una columna leída, y quien
    # revisa tiene que saber cuál es cuál.
    assert any("dedujo" in a for a in leido.warnings), leido.warnings


def test_el_orden_de_las_columnas_tampoco_importa_de_punta_a_punta() -> None:
    """
    Lo mismo, con las columnas barajadas: ni rótulos, ni orden conocido.
    """
    from pdf_parser import _interpretar, _Tabla

    orden = [10, 3, 7, 0, 5, 9, 2, 8, 6, 1, 4]
    filas = _mezclar(PAPEL, orden)
    leido = _interpretar(
        [_Tabla(1, [[""] * len(orden), *filas])], total_paginas=1, warnings=[], source="pdf"
    )

    assert [f.code for f in leido.rows] == [f[0] for f in PAPEL]

    por_codigo = {f.code: f for f in leido.rows}
    assert [por_codigo[f[0]].diameter_mm for f in PAPEL] == [float(f[1]) for f in PAPEL]

    # Las medidas salen en el orden del PAPEL, que es el de las columnas: la
    # figura son tramos uno detrás del otro, y barajar las columnas baraja la
    # figura si el lector se guía por otra cosa.
    esperadas = [[float(v) for v in f[2:7] if v is not None] for f in PAPEL]
    salieron = [[m.value for m in por_codigo[f[0]].dimensions] for f in PAPEL]
    mezcladas = [sorted(e) == sorted(s) for e, s in zip(esperadas, salieron)]
    assert all(mezcladas), "faltan medidas"


def test_la_holgura_no_puede_ser_una_constante() -> None:
    """
    La tolerancia de las comparaciones es el REDONDEO del papel, no un número
    fijo, y este test existe porque con un número fijo el módulo no funciona.

    Con medio punto de margen absoluto, una columna de unos «cierra» contra una
    columna de diámetros dividida por diez —|1 − 1,2| = 0,2— y el buscador se
    quedaba con ese reparto porque cerraba en diez de doce filas. Dos columnas de
    números chicos son indistinguibles si la tolerancia es del tamaño de los
    números.

    La holgura sale de los decimales con que la columna está escrita: una
    longitud «5.85» en metros comparada contra centímetros puede diferir medio
    centímetro; una columna de enteros no puede diferir en nada apreciable.
    """
    from column_roles import _cierra, _decimales, _holgura_de

    enteros = [["12"], ["10"], ["8"]]
    con_dos = [["5.85"], ["5.75"], ["1.20"]]

    assert _decimales(enteros, 0) == 0
    assert _decimales(con_dos, 0) == 2

    # Lo que NO tiene que cerrar: un 1 contra un 12 × 0,1.
    holgura_chica = _holgura_de(_decimales(enteros, 0), 0.1)
    assert not _cierra(1.0, 1.2, holgura_chica), "un 1 no es un 1,2"

    # Lo que SÍ: 585 cm contra 5,85 m, que es el mismo número en dos unidades.
    holgura_grande = _holgura_de(_decimales(con_dos, 0), 100.0)
    assert _cierra(585.0, 5.85 * 100, holgura_grande)
    # Y el redondeo de verdad: el papel escribe 5,85 para 585,4.
    assert _cierra(585.4, 5.85 * 100, holgura_grande)
    assert not _cierra(587.0, 5.85 * 100, holgura_grande)


def test_lo_que_el_encabezado_no_nombro_se_deduce_y_se_avisa() -> None:
    """
    El caso de todos los días, y el que motivó todo esto.

    El encabezado se lee a MEDIAS: «POS.», «Ø» y las letras de las medidas salen
    bien, y los rótulos de la derecha —los que sostienen el recálculo— no. Es
    literalmente lo que pasó con el documento de obra leído de una foto, donde
    «PESO +7%» no estaba en el diccionario y las columnas de resultado se
    perdían.

    El encabezado sigue mandando en lo que sí nombró; lo que falta lo ponen las
    cuentas del papel, y se avisa — una columna deducida no es una columna
    leída, y quien revisa tiene que saber cuál es cuál.
    """
    from pdf_parser import _interpretar, _Tabla

    a_medias = ["POS.", "Ø", "a (cm)", "b", "c", "d", "e", "L0MG(rn)", "CAMT", "VEC3S", "T0T"]
    leido = _interpretar(
        [_Tabla(1, [a_medias, *PAPEL])], total_paginas=1, warnings=[], source="image"
    )

    # Lo que el rótulo dio.
    assert leido.columns.mapped["code"] == CODIGO
    assert leido.columns.mapped["diameter"] == DIAMETRO
    assert set(leido.columns.dimension_columns) == {"a", "b", "c", "d", "e"}

    # Y lo que lo dedujo la aritmética.
    assert leido.columns.mapped["unit_length"] == LARGO
    assert leido.columns.mapped["total_length"] == TOTAL
    assert {leido.columns.mapped["quantity"], leido.columns.mapped["elements"]} == {
        CANTIDAD,
        VECES,
    }

    # Una columna reclamada deja de estar «sin reconocer»: si siguiera en las dos
    # listas, la pantalla la mostraría como reconocida y como no.
    for campo in ("unit_length", "total_length", "quantity", "elements"):
        assert leido.columns.mapped[campo] not in leido.columns.unmapped

    assert any("dedujeron" in a or "deducieron" in a for a in leido.warnings), leido.warnings

    # Y la planilla queda completa, que es el punto.
    por_codigo = {f.code: f for f in leido.rows}
    assert por_codigo["11"].claimed.unit_length == 5.70
    assert por_codigo["11"].claimed.total_length == 51.30
    assert por_codigo["7"].elements == 2


def test_una_medida_que_el_rotulo_perdio_la_recupera_la_aritmetica() -> None:
    """
    El caso que la foto del usuario destapó, y el más caro de los tres.

    A 38 píxeles por columna el rótulo «d» del encabezado se leyó «e». Entonces
    la columna de «d» quedó etiquetada «e», la «e» de verdad se descartó por
    repetida, y el encabezado entregó CUATRO columnas de medida donde el papel
    tiene cinco. Las filas de 4 y 5 medidas llegaban a la pantalla con 3 y 4, y
    quedaban bloqueadas con «no hay ningún tipo con 4 medidas».

    La aritmética, en cambio, las encuentra todas: la suma de las cinco da la
    longitud declarada. Entre un rótulo que se SABE mal leído —lo dice
    `_problema_de_las_letras`— y una ecuación que cierra en doce filas, la
    ecuación es mejor evidencia.

    Se re-rotulan todas de nuevo en orden de columna, y no sólo las que
    faltaban: con un rótulo corrido, conservar los nombres viejos dejaría dos
    columnas llamándose «e».
    """
    from pdf_parser import _interpretar, _Tabla

    # El encabezado tal como salió de la foto: «d» leída «e».
    arriba = [
        "POS.", "Ø", "DIMENSIONES (cm.)", None, None, None, None,
        "LONG. (m.)", "CANT.", "VECES", "LONG. TOT.(m.)",
    ]
    abajo = [None, None, "a", "b", "c", "e", "e", None, None, None, None]

    leido = _interpretar(
        [_Tabla(1, [arriba, abajo, *PAPEL])], total_paginas=1, warnings=[], source="image"
    )

    assert leido.columns.dimension_columns == {"a": 2, "b": 3, "c": 4, "d": 5, "e": 6}

    por_codigo = {f.code: f for f in leido.rows}
    # La fila de cuatro medidas del papel vuelve a tener cuatro…
    assert [(m.name, m.value) for m in por_codigo["1"].dimensions] == [
        ("a", 15.0), ("b", 90.0), ("c", 435.0), ("d", 45.0)
    ]
    # …y la de cinco, cinco.
    assert [(m.name, m.value) for m in por_codigo["11"].dimensions] == [
        ("a", 15.0), ("b", 70.0), ("c", 335.0), ("d", 140.0), ("e", 10.0)
    ]

    assert any("no nombró" in a for a in leido.warnings), leido.warnings

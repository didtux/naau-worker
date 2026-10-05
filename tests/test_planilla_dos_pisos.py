"""
Una planilla con el encabezado en DOS PISOS y dos unidades a la vez.

── De dónde sale ───────────────────────────────────────────────────────────

De un documento de obra real —«PLANILLA DE FIERROS - GRADA TIPO 1»— que el
lector rechazaba entero con NO_HEADER. Su encabezado es así:

    POS. │ Ø │ TIPO │   DIMENSIONES (cm.)   │ LONG. │ CANT. │ VECES │ LONG. TOT.
         │   │      │  a │ b │ c │ d │ e    │ (m.)  │       │       │   (m.)

y rompía el lector en cuatro lugares distintos a la vez:

  1. «POS.» está en el piso de arriba y las letras de las medidas en el de
     abajo, así que ninguna fila sola tiene las dos cosas que hacen un
     encabezado válido.
  2. La unidad viene escrita «(cm.)», con el punto de la abreviatura adentro
     del paréntesis, y el reconocedor de unidades pedía «(cm)» exacto.
  3. Las medidas están en CENTÍMETROS y las longitudes en METROS, las dos
     rotuladas. El modelo tenía una sola unidad para todo el documento.
  4. Las celdas sin medida traen «——», y cada una salía como un problema de
     lectura: catorce filas por cinco columnas es medio centenar de problemas
     inventados tapando los de verdad.

── Por qué las filas son las del papel y no inventadas ─────────────────────

Porque la planilla se verifica a sí misma, igual que la piloto: la longitud es
la suma de las medidas y el total es la longitud por la cantidad **por las
veces**. Con números inventados el test diría «el lector lee lo que yo escribí»;
con éstos dice que las cuentas del papel cierran después de leerlo.
"""

from __future__ import annotations

import pytest

from pdf_parser import _interpretar, _Tabla

# El encabezado, en sus dos pisos, tal como está impreso.
ARRIBA = [
    "POS.", "Ø", "TIPO", "DIMENSIONES (cm.)", None, None, None, None,
    "LONG. (m.)", "CANT.", "VECES", "LONG. TOT.(m.)", "PESO +7% (Kg.)", "ESQUEMA",
]
ABAJO = [None, None, None, "a", "b", "c", "d", "e", None, None, None, None, None, None]

# Seis filas del documento, transcritas. Las rayas son las del papel.
FILAS = [
    ["1", "12", "J", "15", "90", "435", "45", "——", "5.85", "9", "1", "52.65", "50.03", None],
    ["2", "10", "J", "15", "95", "410", "55", "——", "5.75", "8", "1", "46.00", "30.37", None],
    ["4", "12", "S", "10", "125", "65", "——", "——", "2.00", "9", "1", "18.00", "17.10", None],
    ["6", "8", "E", "115", "——", "——", "——", "——", "1.15", "94", "1", "108.10", "45.69", None],
    ["7", "16", "L", "15", "140", "——", "——", "——", "1.55", "2", "2", "6.20", "10.47", None],
    ["11", "12", "X", "15", "70", "335", "140", "10", "5.70", "9", "1", "51.30", "48.74", None],
]


@pytest.fixture(scope="module")
def leido():
    tabla = _Tabla(1, [ARRIBA, ABAJO, *FILAS])
    return _interpretar([tabla], total_paginas=1, warnings=[], source="pdf")


def test_el_encabezado_de_dos_pisos_se_lee_entero(leido) -> None:
    """
    El defecto de fondo: ninguna de las dos filas sola es un encabezado válido.

    «POS.» sin letras de medida no alcanza, y las letras sin «POS.» tampoco. El
    documento se caía con NO_HEADER antes de mirar una sola fila.
    """
    columnas = leido.columns

    assert set(columnas.dimension_columns) == {"a", "b", "c", "d", "e"}
    assert columnas.mapped["code"] == 0
    assert columnas.mapped["diameter"] == 1
    assert columnas.mapped["quantity"] == 9
    assert columnas.mapped["elements"] == 10
    assert columnas.mapped["unit_length"] == 8
    assert columnas.mapped["total_length"] == 11

    assert len(leido.rows) == len(FILAS)
    assert [f.code for f in leido.rows] == ["1", "2", "4", "6", "7", "11"]


def test_las_dos_unidades_del_papel_se_leen_las_dos(leido) -> None:
    """
    Centímetros para medir y metros para pedir, en la misma tabla, y las dos
    rotuladas por el propio encabezado.

    Con una sola unidad para todo el documento esto daba 585 contra 5,85 en
    CADA fila: una planilla perfectamente leída llegaba con catorce
    desacuerdos inventados, y el problema no era la lectura sino que el modelo
    no podía decir lo que el papel decía.
    """
    assert leido.unit == "cm"
    assert leido.claim_unit == "m"
    assert "cm" in leido.unit_evidence and "m" in leido.unit_evidence


def test_las_cuentas_del_papel_cierran_despues_de_leerlo(leido) -> None:
    """
    El oráculo: la planilla verificando su propia lectura.

    La longitud es la suma de las medidas, y el total es la longitud por la
    cantidad por las veces. Si una celda se hubiera leído de la columna vecina,
    o una raya se hubiera colado como número, estas cuentas no dan.
    """
    for fila in leido.rows:
        suma_cm = sum(m.value for m in fila.dimensions)
        assert fila.claimed.unit_length is not None
        assert suma_cm == pytest.approx(fila.claimed.unit_length * 100, abs=0.5), fila.code

        piezas = (fila.quantity or 0) * (fila.elements or 1)
        assert fila.claimed.total_length == pytest.approx(
            fila.claimed.unit_length * piezas, abs=0.01
        ), fila.code


def test_las_veces_no_se_confunden_con_la_cantidad(leido) -> None:
    """
    «CANT.» y «VECES» son dos cosas distintas y el papel las separa.

    En la posición 7 son 2 piezas en cada uno de 2 elementos: cuatro piezas en
    total, y 1,55 × 4 = 6,20, que es lo que el papel afirma. Sumarlas en una
    sola cantidad perdería la distinción; ignorar las veces pediría la mitad
    del fierro.
    """
    p7 = next(f for f in leido.rows if f.code == "7")

    assert p7.quantity == 2
    assert p7.elements == 2
    assert p7.claimed.total_length == 6.20

    # Y una fila sin repetición trae el 1 explícito, no un `None`.
    p1 = next(f for f in leido.rows if f.code == "1")
    assert (p1.quantity, p1.elements) == (9, 1)


def test_las_rayas_son_celdas_vacias_y_no_problemas(leido) -> None:
    """
    Sin esto, la posición 6 —una barra recta, con cuatro rayas— llegaba con
    cuatro problemas de lectura, y el documento entero con medio centenar.

    El ruido no es gratis: entierra los problemas de verdad, que son los que
    hay que mirar.
    """
    assert all(f.issues == [] for f in leido.rows), [
        (f.code, f.issues) for f in leido.rows if f.issues
    ]

    p6 = next(f for f in leido.rows if f.code == "6")
    assert [m.name for m in p6.dimensions] == ["a"]
    assert p6.dimensions[0].value == 115


def test_lo_que_no_se_reconocio_queda_a_la_vista(leido) -> None:
    """
    «PESO +7%» no es el peso total: es el peso con el factor de pérdida ya
    aplicado, que es otra cantidad. Mapearlo como si fuera el peso haría que la
    comparación contra el motor difiriera un 7 % en todas las filas sin que se
    sepa por qué.

    Así que no se adivina — viaja crudo y la pantalla lo muestra, que es donde
    una persona lo resuelve.
    """
    sueltos = [leido.columns.headers_raw[i] for i in leido.columns.unmapped]

    assert "PESO +7% (Kg.)" in sueltos
    assert "total_weight" not in leido.columns.mapped

    p1 = next(f for f in leido.rows if f.code == "1")
    assert p1.unmapped.get("PESO +7% (Kg.)") == "50.03"


def test_las_medidas_salen_en_el_orden_DE_COLUMNA_y_no_del_rotulo() -> None:
    """
    El orden de las medidas lo manda el papel, no el rótulo que leímos.

    ── El defecto ─────────────────────────────────────────────────────────

    Las medidas se armaban recorriendo `dimension_columns` ordenado por LETRA.
    Con los rótulos bien leídos da lo mismo, porque las letras están en orden
    en el papel. Con uno mal leído, no.

    Medido sobre esta misma planilla fotografiada a baja resolución: el «b» se
    lee «d», así que la columna de «b» queda rotulada «d» y la «d» de verdad se
    descarta por repetida. Ordenando por letra, la fila 13 —que en el papel dice
    65, 155, 10— salía **65, 10, 155**: el tramo de 155 y el de 10 cambiados de
    lugar.

    Y eso es lo peor que puede hacer este lector, porque los tres números suman
    lo mismo: la longitud cierra, el total cierra, el peso cierra, y no hay
    ninguna cuenta que lo contradiga. La única señal aparece en la obra, con la
    barra doblada en otro orden.

    Por índice de columna sale lo que dice el papel, con un cartel equivocado
    —un problema de nombre— y la columna repetida perdida, que sí la atrapa el
    recálculo porque a las medidas les falta un tramo.
    """
    arriba = ["POS.", "Ø", "DIMENSIONES (cm.)", None, None, "LONG. (m.)", "CANT."]
    # El encabezado como sale de la foto: «b» leída «d».
    abajo = [None, None, "a", "d", "c", None, None]
    fila = ["13", "12", "65", "155", "10", "2.30", "8"]

    leido = _interpretar(
        [_Tabla(1, [arriba, abajo, fila])], total_paginas=1, warnings=[], source="image"
    )

    p13 = next(f for f in leido.rows if f.code == "13")
    assert [m.value for m in p13.dimensions] == [65, 155, 10]

    # Y el rótulo mal leído se avisa, porque el nombre que muestra la pantalla
    # no es el del papel.
    assert any("rótulos" in a for a in leido.warnings), leido.warnings

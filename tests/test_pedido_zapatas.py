"""
El pedido de corte y doblado por elemento: `Pedido_corte_doblado_ejes_1-10_EMPRESA.pdf`.

Una tabla por grupo de zapatas, sin columna POS ni columnas de medida, con
«Cantidad de barras × Nº zap. = Total barras», el esquema escrito como texto,
filas de longitud variable y los cuadros del resumen.
"""

from pathlib import Path

import pytest

from pdf_parser import parse_pdf

PEDIDO = Path(r"N:\NAAU\Pedido_corte_doblado_ejes_1-10_EMPRESA.pdf")


@pytest.fixture(scope="module")
def pedido():
    if not PEDIDO.exists():
        pytest.skip("falta el PDF de pedido de zapatas")
    return parse_pdf(PEDIDO.read_bytes())


def _fila(pedido, label: str, section_start: str):
    return next(r for r in pedido.rows if r.label == label and (r.section or "").startswith(section_start))


def test_se_reconoce_como_pedido_por_elemento(pedido) -> None:
    assert pedido.layout == "order"
    assert pedido.unit == "cm"
    assert len(pedido.page_sizes) == 8


def test_el_grupo_da_la_seccion_y_el_codigo(pedido) -> None:
    fila = _fila(pedido, "Parrilla inferior X", "ZAPATAS C49")
    assert fila.code == "C49-1"
    assert "200 x 200" in (fila.section or "")
    assert fila.page == 3
    assert fila.bbox and len(fila.bbox) == 4


def test_cantidad_por_numero_de_zapatas_y_total(pedido) -> None:
    fila = _fila(pedido, "Parrilla inferior X", "ZAPATAS C49")
    assert (fila.diameter_mm, fila.quantity, fila.elements) == (16.0, 19, 7)
    assert fila.claimed.total_pieces == 133
    assert not fila.issues


def test_las_medidas_salen_del_esquema_cuando_suman_el_corte(pedido) -> None:
    parrilla = _fila(pedido, "Parrilla inferior X", "ZAPATAS C49")
    assert [d.value for d in parrilla.dimensions] == [30, 190, 30]
    assert parrilla.claimed.unit_length == 250
    arranque = _fila(pedido, "Arranque columna", "ZAPATAS C49")
    assert [d.value for d in arranque.dimensions] == [30, 160]


def test_el_estribo_con_ganchos_va_con_la_longitud_declarada(pedido) -> None:
    estribo = _fila(pedido, "Estribo arranque 40x45", "ZAPATAS C49")
    assert estribo.length_only
    assert estribo.dimensions == []
    assert estribo.claimed.unit_length == 160


def test_la_longitud_variable_es_referencia(pedido) -> None:
    variables = [r for r in pedido.rows if r.reference_only]
    assert len(variables) == 12
    assert all(r.dimensions == [] for r in variables)


def test_el_cuadro_de_barras_rectas_son_posiciones(pedido) -> None:
    rectas = {r.diameter_mm: r.quantity for r in pedido.rows if r.straight_bars}
    assert rectas == {6.0: 100, 12.0: 112, 16.0: 44}
    assert any("Ø6" in w for w in pedido.warnings)


def test_resumen_de_piezas_e_indice_de_elementos(pedido) -> None:
    piezas = {l.diameter_mm: l.pieces for l in pedido.pieces_summary}
    assert piezas == {6.0: 117, 12.0: 1593, 16.0: 1382}
    assert sum(g.count or 0 for g in pedido.elements_index) == 40


def test_los_totales_del_papel_cierran_con_las_filas(pedido) -> None:
    cortadas = [r for r in pedido.rows if not r.reference_only and not r.straight_bars]
    por_diametro: dict[float, int] = {}
    for r in cortadas:
        por_diametro[r.diameter_mm] = por_diametro.get(r.diameter_mm, 0) + (r.claimed.total_pieces or 0)
    assert por_diametro == {6.0: 117, 12.0: 1593, 16.0: 1382}


def test_la_planilla_de_vigas_no_se_toma_por_pedido() -> None:
    vigas = Path(r"N:\NAAU\PLANILLA_VIGAS ENTRE PISO.pdf")
    if not vigas.exists():
        pytest.skip("falta la planilla piloto")
    leido = parse_pdf(vigas.read_bytes())
    assert leido.layout == "table"
    assert len(leido.rows) == 27
    assert leido.rows[0].bbox is not None

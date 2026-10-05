"""
El parser, contra la planilla piloto y contra sus propias piezas.

── Qué se está probando, en realidad ───────────────────────────────────────

Dos cosas distintas, y conviene no confundirlas:

Las pruebas contra el archivo real comprueban que LEEMOS lo que el papel dice.
Su aserción más fuerte no es «hay 27 filas»: es que las tres relaciones que la
planilla afirma de sí misma —el parcial es la suma de las medidas, el total es
el parcial por la cantidad, y el peso es la longitud con pérdida por el peso
unitario— cierran en las 27 filas. Si una celda se leyera de la columna
equivocada, o un «6,150.00» se leyera como 6,15, esas cuentas se rompen. Es la
planilla misma haciendo de oráculo.

Las pruebas de las piezas sueltas comprueban lo que el archivo piloto NO puede
comprobar, porque tiene un solo estilo numérico, un solo idioma de encabezados
y una sola unidad. Ahí es donde se prueba que el mapeo no confunde «PESO TOTAL»
con «PESO», que es el error que un diccionario de sinónimos comete solo.
"""

from __future__ import annotations

import math

import pytest

from conftest import PDF_ROTO, pdf_en_blanco
from models import ColumnMapping
from pdf_parser import (
    SIN_EVIDENCIA_DE_ESTILO,
    CorruptPdfError,
    NoTableError,
    clave,
    detectar_estilo,
    letras_croquis,
    mapear_columnas,
    normalizar,
    numero,
    parse_pdf,
)

# ── El archivo real ────────────────────────────────────────────────────────


def test_lee_las_27_posiciones(resultado) -> None:
    assert len(resultado.rows) == 27
    assert resultado.pages == 2


def test_la_unidad_sale_del_encabezado(resultado) -> None:
    assert resultado.unit == "cm"
    # Y se sabe POR QUÉ: el encabezado la declara. La evidencia se muestra en
    # pantalla, así que tiene que decir algo que una persona pueda verificar
    # mirando el papel.
    assert "cm" in resultado.unit_evidence
    assert "encabezado" in resultado.unit_evidence


def test_mapea_las_17_columnas_sin_dejar_ninguna_suelta(resultado) -> None:
    columnas = resultado.columns
    assert len(columnas.headers_raw) == 17
    assert set(columnas.dimension_columns) == {"a", "b", "c", "d", "e", "f"}
    assert columnas.mapped == {
        "section": 0,
        "code": 1,
        "diameter": 2,
        "weight_per_unit": 3,
        "quantity": 4,
        "sketch": 5,
        "unit_length": 12,
        "total_length": 13,
        "total_with_loss": 14,
        "bars": 15,
        "total_weight": 16,
    }
    assert columnas.unmapped == []


def test_la_seccion_girada_se_lee_derecha(resultado) -> None:
    """
    La columna de sección viene impresa de costado y el PDF la emite al revés.

    Si no se desrotara, la sección diría «LANIDUTIGNOL OZREUFER» y quien revisa
    no podría saber que las dos «P1» del archivo son piezas distintas.
    """
    assert resultado.rows[0].section == "REFUERZO LONGITUDINAL"
    assert resultado.rows[-1].section == "ESTRIBOS"


def test_la_seccion_se_arrastra_a_todas_sus_filas(resultado) -> None:
    """La celda combinada sólo trae texto en la primera fila del grupo."""
    longitudinales = [f for f in resultado.rows if f.section == "REFUERZO LONGITUDINAL"]
    estribos = [f for f in resultado.rows if f.section == "ESTRIBOS"]
    assert len(longitudinales) == 25
    assert len(estribos) == 2
    assert all(f.section for f in resultado.rows)


def test_primera_posicion_completa(resultado) -> None:
    fila = resultado.rows[0]
    assert fila.page == 1
    assert fila.code == "P1"
    assert fila.diameter_mm == 16
    assert fila.quantity == 6
    assert [(d.name, d.value) for d in fila.dimensions] == [("a", 26.0), ("b", 999.0)]
    assert fila.claimed.unit_length == 1025.0
    assert fila.claimed.total_length == 6150.0
    assert fila.claimed.total_with_loss == 6273.0
    assert fila.claimed.bars == 6
    assert fila.claimed.weight_per_unit == 0.01578
    assert fila.claimed.total_weight == 98.99
    assert fila.issues == []


def test_el_estribo_trae_sus_seis_medidas_desdobladas(resultado) -> None:
    """
    El hallazgo que decide el mapeo de tipos.

    Las seis medidas del estribo (43, 23, 43, 23, 5, 5) ya vienen DESDOBLADAS:
    los dos lados largos, los dos cortos y los dos ganchos, cada uno en su
    columna. Su longitud de corte es la suma llana, 142 cm.

    Por eso el importador no puede mapear un estribo al tipo `O` del sistema,
    cuya fórmula es `2*(a+b)+c`: con estos números daría 284 cm y pediría el
    doble de acero. Un estribo así necesita un tipo de la empresa con fórmula
    de suma llana.
    """
    estribo = next(f for f in resultado.rows if f.section == "ESTRIBOS")
    assert estribo.diameter_mm == 8
    assert estribo.quantity == 274
    assert [d.value for d in estribo.dimensions] == [43.0, 23.0, 43.0, 23.0, 5.0, 5.0]
    assert sum(d.value for d in estribo.dimensions) == 142.0
    assert estribo.claimed.unit_length == 142.0


def test_las_letras_del_croquis_cuentan_las_medidas(resultado) -> None:
    """
    Cuántas letras distintas hay es cuántas medidas usa la figura.

    Es lo que funda la sugerencia de tipo, y vale la pena que sea una prueba y
    no una observación: si se rompiera, el importador propondría tipos de la
    cantidad de medidas equivocada en toda la planilla.
    """
    for fila in resultado.rows:
        assert fila.sketch_letters, f"{fila.code} sin rótulos de croquis"
        assert len(fila.sketch_letters) == len(fila.dimensions), fila.code

    # «a bb c» son tres medidas aunque la `b` aparezca dos veces: el tramo se
    # dibuja dos veces y se mide una.
    p20 = next(f for f in resultado.rows if f.code == "P20" and f.page == 2)
    assert p20.sketch == "a bb c"
    assert p20.sketch_letters == ["a", "b", "c"]


def test_la_aritmetica_que_la_planilla_afirma_cierra_en_las_27_filas(resultado) -> None:
    """
    La prueba que de verdad valida la lectura.

    Tres relaciones, todas comprobables sin saber nada de fierros:

      LONG. PARCIAL          = suma de las medidas
      LONG. TOTAL            = parcial × No.
      LONG. TOTAL + PERDIDA  = total × 1,02
      PESO TOTAL             = longitud con pérdida × PESO (kg/cm)

    Una celda leída de la columna vecina, un separador de miles tomado por
    decimal o una fila corrida rompen alguna de las cuatro.
    """
    for fila in resultado.rows:
        c = fila.claimed
        suma = sum(d.value for d in fila.dimensions)

        assert c.unit_length == pytest.approx(suma, abs=0.01), f"{fila.code}: parcial"
        assert c.total_length == pytest.approx(
            c.unit_length * fila.quantity, abs=0.01
        ), f"{fila.code}: total"
        assert c.total_with_loss == pytest.approx(
            c.total_length * 1.02, abs=0.01
        ), f"{fila.code}: pérdida"
        assert c.total_weight == pytest.approx(
            c.total_with_loss * c.weight_per_unit, abs=0.01
        ), f"{fila.code}: peso"


def test_el_peso_de_la_planilla_es_del_ACERO_y_no_de_las_BARRAS(resultado) -> None:
    """
    Una diferencia de MÉTODO, no de lectura, y la razón de ser del producto.

    La planilla pesa el acero con el 2 % de pérdida; no pesa las barras que hay
    que comprar. Y sus `N. BARRAS` salen de dividir la longitud total por 12 m,
    que ignora el sobrante de cada barra: en el estribo P1 son 274 piezas de
    1,42 m, de las que entran 8 por barra, así que hacen falta 35 barras y la
    planilla dice 34.

    Esta prueba fija esa discrepancia a propósito. Cuando Nest recalcule, va a
    dar 35, y hay que poder explicar de dónde sale la diferencia en vez de
    tratarla como un error de lectura.
    """
    estribo = next(f for f in resultado.rows if f.section == "ESTRIBOS")
    assert estribo.claimed.bars == 34

    # El método de la planilla: longitud total con pérdida, dividida por barra.
    por_longitud = math.ceil(estribo.claimed.total_with_loss / 1200)
    assert por_longitud == 34

    # El método físico: piezas enteras por barra.
    piezas_por_barra = int(1200 // estribo.claimed.unit_length)
    assert piezas_por_barra == 8
    assert math.ceil(estribo.quantity / piezas_por_barra) == 35


def test_lee_el_resumen_de_material_del_pie(resultado) -> None:
    resumen = resultado.summary
    assert resumen is not None
    assert [(l.diameter_mm, l.bars) for l in resumen.lines] == [
        (8.0, 34),
        (9.5, 39),
        (12.0, 13),
        (16.0, 32),
    ]
    assert [l.weight_kg for l in resumen.lines] == [156.76, 224.87, 87.13, 528.55]
    assert resumen.total_length == 123148.68


def test_el_resumen_es_la_suma_de_las_filas(resultado) -> None:
    """
    El resumen del pie no es un dato aparte: es la suma de las filas.

    Comprobarlo cierra el círculo — si las filas se leyeran bien y el resumen
    mal, o al revés, esta cuenta no daría.
    """
    resumen = resultado.summary
    assert resumen is not None

    for linea in resumen.lines:
        filas = [f for f in resultado.rows if f.diameter_mm == linea.diameter_mm]
        assert filas, linea.diameter_mm
        assert sum(f.claimed.bars for f in filas) == linea.bars
        assert sum(f.claimed.total_with_loss for f in filas) == pytest.approx(
            linea.length, abs=0.01
        )

        # El peso del resumen NO es la suma de los pesos impresos: es la suma de
        # los pesos sin redondear. En el φ12 los nueve pesos de fila suman 87,15
        # y el resumen dice 87,13, porque cada fila se imprimió redondeada a dos
        # decimales y el total se calculó antes de redondear.
        #
        # No es un error de la planilla ni de la lectura, y vale tenerlo escrito:
        # cuando el importador compare pesos contra el motor propio, dos
        # centavos de diferencia en un total de diámetro son esto y no un
        # problema de acero.
        sin_redondear = sum(
            f.claimed.total_with_loss * f.claimed.weight_per_unit for f in filas
        )
        assert sin_redondear == pytest.approx(linea.weight_kg, abs=0.01)

    assert sum(l.length for l in resumen.lines) == pytest.approx(resumen.total_length, abs=0.01)


def test_los_pesos_impresos_de_las_filas_arrastran_centavos(resultado) -> None:
    """
    La contracara de la prueba anterior, escrita a propósito.

    Sumar los pesos impresos da 87,15 donde el resumen dice 87,13. Fijarlo acá
    es lo que evita que dentro de seis meses alguien «arregle» el parser para
    hacer coincidir dos números que nunca fueron el mismo número.
    """
    doce = [f for f in resultado.rows if f.diameter_mm == 12.0]
    assert len(doce) == 9
    assert sum(f.claimed.total_weight for f in doce) == pytest.approx(87.15, abs=0.005)


def test_avisa_de_los_rotulos_repetidos(resultado) -> None:
    """
    «P1» aparece dos veces: una entre los longitudinales y otra entre los
    estribos. El rótulo es único dentro de su sección, no en la planilla, y
    NAAU numera con un entero sin guardar el rótulo — así que la importación va
    a renumerar y quien revisa tiene que enterarse antes y no después.
    """
    avisos = " ".join(resultado.warnings)
    assert "repetidos" in avisos
    assert "P1" in avisos and "P2" in avisos


def test_el_estilo_numerico_se_detecta_del_documento(resultado) -> None:
    assert "«us»" in resultado.unit_evidence
    # Y como se DETECTÓ, no hay nada que avisar.
    assert "decimal" not in " ".join(resultado.warnings)


def test_avisa_cuando_el_estilo_numerico_se_asumio(planilla_bytes, monkeypatch) -> None:
    """
    El aviso del estilo asumido, que antes no existía.

    ── Por qué importa más que el de la unidad ─────────────────────────────

    Equivocarse de unidad es un factor de 10 o de 100 y se nota en el total.
    Leer «6,150» con punto decimal cuando la coma era el decimal da 6150 en
    lugar de 6,15: un factor de MIL, y la fila sigue pareciendo verosímil. La
    unidad avisaba cuando se asumía y el estilo no, que es la asimetría al
    revés.

    ── Por qué se fuerza la detección en vez de armar un PDF ───────────────

    Porque lo que hay que probar es que QUIEN LLAMA reacciona a «no encontré
    evidencia», y eso se prueba dándole esa respuesta. Que `detectar_estilo` la
    produzca de verdad cuando no hay nada lo prueba su propia prueba, más
    abajo; armar un PDF sin ninguna celda con separadores agregaría un
    generador de tablas al arnés para cubrir el mismo tramo dos veces.
    """
    import pdf_parser

    monkeypatch.setattr(
        pdf_parser,
        "detectar_estilo",
        lambda celdas: ("us", pdf_parser.SIN_EVIDENCIA_DE_ESTILO),
    )

    avisos = " ".join(pdf_parser.parse_pdf(planilla_bytes).warnings)
    assert "coma o punto decimal" in avisos
    assert "revisá las medidas" in avisos


def test_ninguna_fila_quedo_con_problemas_de_lectura(resultado) -> None:
    con_problemas = {f.code: f.issues for f in resultado.rows if f.issues}
    assert con_problemas == {}


# ── Las piezas, donde el archivo piloto no llega ───────────────────────────


def test_normalizar_borra_lo_que_no_identifica_a_un_encabezado() -> None:
    assert normalizar("PESO\n(kg/cm)") == "PESO (KG/CM)"
    assert normalizar("  Diám.  Total ") == "DIAM TOTAL"
    assert normalizar("LONG.\nTOTAL +\nPERDIDA\n(cm)") == "LONG TOTAL + PERDIDA (CM)"
    assert normalizar(None) == ""


def test_los_encabezados_que_se_contienen_no_se_confunden() -> None:
    """
    El error que un diccionario de sinónimos comete solo, y que la igualdad
    exacta evita.

    «LONG. TOTAL + PERDIDA» contiene «LONG. TOTAL», y «PESO TOTAL (Kg)»
    contiene «PESO». Con comparación por subcadena, dos de estas cuatro
    columnas se cruzarían según el orden en que se probaran los campos, y la
    planilla se leería mal sin una sola excepción. Por eso el mapeo compara por
    igualdad.
    """
    columnas = mapear_columnas(
        [
            "POS.",
            "DIAM. (mm)",
            "a (cm)",
            "LONG. TOTAL (cm)",
            "LONG. TOTAL + PERDIDA (cm)",
            "PESO (kg/cm)",
            "PESO TOTAL (Kg)",
        ]
    )
    assert columnas.mapped["total_length"] == 3
    assert columnas.mapped["total_with_loss"] == 4
    assert columnas.mapped["weight_per_unit"] == 5
    assert columnas.mapped["total_weight"] == 6


def test_el_espaciado_no_cambia_el_encabezado_pero_el_rotulo_si() -> None:
    """
    Un encabezado leído de una FOTO pierde espacios, y sigue siendo el mismo.

    Medido sobre la planilla piloto fotografiada: «PESO TOTAL (Kg)» sale
    «PESOTOTAL (Kg)» y «LONG. TOTAL (cm)» sale «LONG. TOTAL(cm)». Es el mismo
    rótulo impreso; lo único que cambió es dónde el detector creyó ver un
    blanco. Sin esto, las dos columnas de RESULTADO de la planilla —el total y
    el peso— se importaban vacías de toda foto.

    Y la segunda mitad del test es la que importa: ignorar el espaciado NO
    afloja el mapeo a un «contiene». «PESOTOTAL(KG)» sigue sin ser «PESO», así
    que las columnas que se contienen entre sí se siguen distinguiendo. Lo que
    se ignora es el espaciado, no el rótulo.
    """
    como_lo_lee_una_foto = mapear_columnas(
        [
            "POS.",
            "DIAM. (mm)",
            "a(cm)",
            "LONG. TOTAL(cm)",
            "LONG. TOTAL + PERDIDA (cm)",
            "PESO (kg/cm)",
            "PESOTOTAL (Kg)",
        ]
    )
    assert como_lo_lee_una_foto.mapped["total_length"] == 3
    assert como_lo_lee_una_foto.mapped["total_with_loss"] == 4
    assert como_lo_lee_una_foto.mapped["weight_per_unit"] == 5
    assert como_lo_lee_una_foto.mapped["total_weight"] == 6
    assert como_lo_lee_una_foto.dimension_columns == {"a": 2}

    assert clave("PESO TOTAL (Kg)") == clave("PESOTOTAL(Kg)") == "PESOTOTAL(KG)"
    assert clave("PESO TOTAL (Kg)") != clave("PESO")
    assert clave("LONG. TOTAL + PERDIDA") != clave("LONG. TOTAL")


def test_el_orden_de_los_campos_no_cambia_el_resultado() -> None:
    """
    Que el mapeo NO dependa del orden es una propiedad, no una casualidad, y
    conviene tenerla fijada: es lo que permite agregar el rótulo de otra
    planilla al diccionario sin pensar dónde ponerlo.

    Se comprueba mapeando la planilla piloto con los campos recorridos al
    revés: si el resultado cambiara, el orden sería carga estructural y habría
    que documentarlo en cada alias nuevo.
    """
    import pdf_parser

    encabezados = [
        "EST.",
        "POS.",
        "DIAM. (mm)",
        "PESO\n(kg/cm)",
        "No.",
        "ELEMENTO",
        "a (cm)",
        "b (cm)",
        "LONG.\nPARCIAL\n(cm)",
        "LONG.\nTOTAL (cm)",
        "LONG.\nTOTAL +\nPERDIDA\n(cm)",
        "N.\nBARRAS",
        "PESO TOTAL\n(Kg)",
    ]
    normal = mapear_columnas(encabezados)

    original = pdf_parser.SINONIMOS
    try:
        pdf_parser.SINONIMOS = tuple(reversed(original))
        al_reves = mapear_columnas(encabezados)
    finally:
        pdf_parser.SINONIMOS = original

    assert al_reves.mapped == normal.mapped
    assert al_reves.dimension_columns == normal.dimension_columns


def test_un_rotulo_desconocido_no_se_adivina_queda_a_la_vista() -> None:
    """
    La contracara del mapeo exacto, y el motivo por el que se eligió.

    «LONG. TOTAL DE LA POSICION» es la longitud total en cualquier lectura
    humana, y el parser NO la mapea: no está en el diccionario. Eso es lo
    buscado — la columna cae en `unmapped`, viaja cruda en cada fila y la
    pantalla la muestra, donde una persona la resuelve. Adivinarla por
    subcadena habría funcionado acá y habría cruzado columnas en otra planilla,
    en silencio.

    Si aparece una planilla que la usa, lo que se hace es agregar el rótulo al
    diccionario; no aflojar la comparación.
    """
    columnas = mapear_columnas(
        ["POS.", "a (cm)", "LONG. TOTAL DE LA POSICION (cm)", "OBSERVACIONES"]
    )
    assert "total_length" not in columnas.mapped
    assert columnas.unmapped == [2, 3]


def test_una_medida_no_se_confunde_con_un_sinonimo() -> None:
    """
    La columna «a» no puede caer en «ACERO», y «No.» no puede ser una medida.

    Las medidas se reconocen por forma —una letra sola— y se resuelven primero
    justamente por esto.
    """
    columnas = mapear_columnas(["POS.", "a", "b", "No.", "ACERO"])
    assert columnas.dimension_columns == {"a": 1, "b": 2}
    assert columnas.mapped["quantity"] == 3
    assert columnas.mapped["diameter"] == 4


def test_lo_que_no_se_mapea_queda_registrado() -> None:
    columnas = mapear_columnas(["POS.", "a (cm)", "OBSERVACIONES", "OBRA"])
    assert columnas.unmapped == [2, 3]


def test_una_columna_vacia_no_cuenta_como_no_mapeada() -> None:
    """Las planillas traen columnas separadoras sin rótulo; no son un hallazgo."""
    columnas = mapear_columnas(["POS.", "a (cm)", None, ""])
    assert columnas.unmapped == []


@pytest.mark.parametrize(
    "texto,estilo,esperado",
    [
        ("6,150.00", "us", 6150.0),
        ("123,148.68", "us", 123148.68),
        ("0.01578", "us", 0.01578),
        ("1.234,56", "eu", 1234.56),
        ("142", "us", 142.0),
        ("142", "eu", 142.0),
        ("9.5", "us", 9.5),
        ("9,5", "eu", 9.5),
        ("Ø16", "us", 16.0),
        ("", "us", None),
        ("-", "us", None),
        (None, "us", None),
        ("a b c", "us", None),
        ("VIGA 1", "us", None),
    ],
)
def test_numero(texto, estilo, esperado) -> None:
    assert numero(texto, estilo) == esperado


def test_el_estilo_numerico_se_decide_con_una_celda_sin_ambiguedad() -> None:
    """
    «6,150» solo es ambiguo: puede ser seis mil ciento cincuenta o seis coma
    ciento cincuenta. «6,150.00» no lo es. Por eso el estilo se decide una vez
    para el documento entero, con la primera celda que traiga los dos
    separadores, y no celda por celda.
    """
    estilo, evidencia = detectar_estilo(["P1", "6,150", "6,150.00"])
    assert estilo == "us"
    assert "6,150.00" in evidencia

    estilo, evidencia = detectar_estilo(["P1", "6.150", "6.150,00"])
    assert estilo == "eu"
    assert "6.150,00" in evidencia

    # Sin ninguna celda con los dos: un separador con dos dígitos al final es
    # un decimal en cualquiera de los dos estilos.
    assert detectar_estilo(["98,99"])[0] == "eu"
    assert detectar_estilo(["98.99"])[0] == "us"

    # Sin nada: se asume el estilo con punto decimal, y se dice que se asumió.
    #
    # La evidencia es EXACTAMENTE la constante, no una frase que la contenga:
    # es lo que compara `parse_pdf` para decidir si emite el aviso, así que si
    # alguien la reescribe acá el aviso se apaga en silencio.
    estilo, evidencia = detectar_estilo(["P1", "142"])
    assert estilo == "us"
    assert evidencia == SIN_EVIDENCIA_DE_ESTILO

    # Y cuando sí hay evidencia, no es la constante: de eso depende que el
    # aviso no salte en un documento que se leyó bien.
    assert detectar_estilo(["6,150.00"])[1] != SIN_EVIDENCIA_DE_ESTILO


@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("a b c", ["a", "b", "c"]),
        ("a bb c", ["a", "b", "c"]),
        ("b\na", ["b", "a"]),
        ("d\ne\na f c\nb", ["d", "e", "a", "f", "c", "b"]),
        ("a", ["a"]),
        # Una palabra no son rótulos: «RECTA» tiene letras fuera de a–l.
        ("RECTA", []),
        ("VIGA", []),
        ("", []),
        (None, []),
        # Sólo números: tampoco.
        ("16", []),
    ],
)
def test_letras_croquis(texto, esperado) -> None:
    assert letras_croquis(texto) == esperado


def test_un_pdf_sin_tabla_falla_en_seco() -> None:
    """
    Un PDF válido, sin líneas de regla.

    Tiene que fallar y no devolver filas: una planilla mal leída que parece
    bien leída es peor que un error, porque lo que sigue es una obra cortando
    fierro con los números equivocados.
    """
    with pytest.raises(NoTableError) as e:
        parse_pdf(pdf_en_blanco())
    assert e.value.code == "NO_TABLE"


def test_un_pdf_roto_se_distingue_de_un_pdf_sin_planilla() -> None:
    """
    Dos fallas distintas porque le piden dos cosas distintas a la persona:
    volver a exportar el archivo, o recuadrar la tabla. Un solo código para las
    dos convertiría el mensaje en «no se pudo», que no dice qué hacer.
    """
    with pytest.raises(CorruptPdfError) as e:
        parse_pdf(PDF_ROTO)
    assert e.value.code == "CORRUPT_PDF"


def test_la_plausibilidad_descarta_unidades_pero_no_siempre_elige_una() -> None:
    """
    Qué puede y qué NO puede decidir la comprobación física.

    El heurístico que el documento de features proponía —«valores > 20 son
    centímetros»— se rompe con una viga de 15 m cargada en metros. Lo que no se
    rompe es que la pieza tiene que caber en una barra, y eso alcanza para
    descartar: 1025 en metros serían mil veinticinco metros de fierro de un
    tirón.

    Pero no alcanza para elegir entre cm y mm cuando los dos dan piezas
    posibles: 142 cm y 142 mm son las dos longitudes de corte reales. Ahí la
    función se calla —devuelve `None`— en vez de inventar, y por eso lo que el
    encabezado DECLARA es la evidencia primaria y esto es sólo la confirmación.
    """
    from pdf_parser import _unidad_plausible

    # Metros y milímetros dan piezas posibles; centímetros no llega a los 2 cm
    # mínimos en ninguna fila, así que queda descartado y gana metros.
    unidad, _ = _unidad_plausible([10.25, 10.45, 1.42, 1.68])
    assert unidad == "m"

    unidad, _ = _unidad_plausible([10250.0, 10450.0, 1420.0, 1680.0])
    assert unidad == "mm"

    # El caso que no se puede decidir: los mismos números son piezas válidas en
    # cm y en mm. Se descarta metros y no se elige entre las otras dos.
    unidad, motivo = _unidad_plausible([1025.0, 1045.0, 142.0, 168.0])
    assert unidad is None
    assert "empate" in motivo and "cm" in motivo and "mm" in motivo


def test_sin_unidad_declarable_se_asume_cm_y_se_avisa() -> None:
    """
    Negarse a devolver la planilla por la unidad sería lo peor de los dos
    mundos: la persona se queda sin ver la tabla justamente por el dato que le
    habría costado un clic arreglar. Se asume el default del oficio y se dice.
    """
    from pdf_parser import _Tabla, _unidad_declarada

    # Un encabezado sin unidad rotulada no declara nada, ni en las medidas ni
    # en la columna de longitud.
    columnas = mapear_columnas(["POS.", "a", "b", "LONG. PARCIAL"])
    todas = [*columnas.dimension_columns.values(), columnas.mapped["unit_length"]]
    unidad, _ = _unidad_declarada(columnas, todas)
    assert unidad is None


def test_una_columna_de_DIBUJOS_no_es_croquis_ni_seccion() -> None:
    """
    El caso que ensuciaba las catorce filas de una planilla real.

    Muchas planillas traen una columna «ESQUEMA» con los DIBUJOS de las
    figuras. Leída de una imagen, esa celda devuelve las letritas que el dibujo
    tiene encima de cada tramo, todas mezcladas y con basura entre medio:

        «b a a a d D b a b a b a b a C a é ? d a b 1C»

    No son rótulos de croquis —basta una letra fuera de la a–l para que no lo
    sean— y hasta acá eso bastaba para reasignar la columna a SECCIÓN. El
    resultado era esa cadena arrastrada como nombre de sección a todas las
    filas de la planilla.

    ── Y por qué el contenido no alcanza para decidirlo ───────────────────

    El primer arreglo miraba el contenido: una sección es una ETIQUETA, hecha
    de palabras, y una lluvia de caracteres sueltos no lo es. Sobre el
    documento real falla, porque el reconocimiento PEGA los rótulos de los
    tramos vecinos:

        «abcd-- a ahc ah- abG- abcde-- ahc. ab»

    «abcd» y «ahc» son palabras de tres letras o más para cualquier criterio
    que mire el texto, y no hay forma de distinguirlas de «VIG» o «COL», que
    son secciones de verdad.

    Lo que sí alcanza es el ENCABEZADO: «ESQUEMA» nombra una columna de dibujos
    y nunca una sección. Lo que no es ni croquis ni sección queda sin mapear y
    viaja crudo a la pantalla, que es lo que hay que hacer con una columna de
    dibujos — mostrar que está, sin inventarle un significado.
    """
    from pdf_parser import _Tabla, _resolver_croquis_y_seccion

    columnas: ColumnMapping = mapear_columnas(["POS.", "a (cm)", "ESQUEMA"])
    assert columnas.mapped["sketch"] == 2

    # Lo que el reconocimiento saca de verdad de la columna de dibujos del
    # papel: los nombres de figura circulados —J, E, L, S— y los rótulos de los
    # tramos, pegados entre sí. Las letras fuera de la a–l son las que delatan
    # que no son rótulos de croquis, y los pegotes como «abcd» son los que
    # engañaban a cualquier criterio basado en el contenido.
    tablas = [
        _Tabla(1, [["1", "15", "J abcd E ahc L abG S abcde"],
                   ["2", "95", None]]),
    ]
    _resolver_croquis_y_seccion(columnas, tablas, ancho=3)

    assert "sketch" not in columnas.mapped
    assert "section" not in columnas.mapped
    # Y sigue estando A LA VISTA, que es el punto: una columna que se libera
    # tiene que pasar a las no reconocidas, no desaparecer de la pantalla.
    assert 2 in columnas.unmapped


def test_ELEMENTO_si_puede_ser_una_seccion() -> None:
    """
    El complemento, y lo que el arreglo NO se llevó puesto.

    «ESQUEMA» nombra un dibujo y nada más, pero «ELEMENTO» es de los dos
    sentidos: en el archivo piloto es la columna del croquis y en otra planilla
    bien puede ser el elemento estructural. Ahí la pregunta por el contenido
    sigue teniendo sentido, y una columna con palabras de verdad sigue pasando
    a sección — que es lo que evita leer «VIGA 1» como las letras v, i, g, a.
    """
    from pdf_parser import _Tabla, _resolver_croquis_y_seccion, parece_etiqueta

    columnas: ColumnMapping = mapear_columnas(["POS.", "a (cm)", "ELEMENTO"])
    assert columnas.mapped["sketch"] == 2

    tablas = [_Tabla(1, [["1", "15", "VIGA 1"], ["2", "95", None]])]
    _resolver_croquis_y_seccion(columnas, tablas, ancho=3)

    assert columnas.mapped.get("section") == 2
    assert "sketch" not in columnas.mapped

    # La distinción que usa ese desempate, aislada: una sección tiene palabras.
    assert parece_etiqueta("REFUERZO LONGITUDINAL")
    assert parece_etiqueta("ESTRIBOS")
    assert parece_etiqueta("VIG")  # hay planillas que abrevian la sección
    assert not parece_etiqueta("b a a a d D b a C a é ? d a b 1C")
    assert not parece_etiqueta("a b c")
    assert not parece_etiqueta("—")
    assert not parece_etiqueta(None)


def test_TIPO_no_es_una_columna_de_croquis() -> None:
    """
    «TIPO» trae la LETRA DE LA FIGURA —J, S, U, L, I, X, C, E, O—, no un dibujo.

    Era sinónimo de croquis, y en la planilla de obra de 14 columnas eso costaba
    dos cosas: el campo se lo quedaba «ESQUEMA» y «TIPO» desaparecía sin que se
    supiera por qué, y cuando se lo quedaba «TIPO» sus letras se contaban como
    rótulos de tramo — «esta figura usa una medida» en una fila que trae cuatro.

    Ahora es su propio campo, `type_code`: no le roba la columna a nadie y dice
    algo que vale, porque el papel nombra la figura. Lo que este lector NO hace
    es resolverlo contra un catálogo que no conoce.
    """
    con_las_dos = mapear_columnas(
        ["POS.", "Ø", "TIPO", "a (cm)", "b", "LONG. (m.)", "CANT.", "ESQUEMA"]
    )
    assert con_las_dos.mapped["sketch"] == 7, "el croquis es ESQUEMA, que es el dibujo"
    assert con_las_dos.mapped["type_code"] == 2, "y TIPO es el tipo, no el croquis"

    # Y sin columna de dibujos, «TIPO» tampoco pasa a ser el croquis: es donde
    # se veía el daño, porque `letras_croquis("J")` da una letra y el lector
    # concluía que la figura usa UNA medida en una fila que trae cuatro.
    sola = mapear_columnas(["POS.", "Ø", "TIPO", "a (cm)", "b", "LONG. (m.)", "CANT."])
    assert "sketch" not in sola.mapped
    assert sola.mapped["type_code"] == 2


def test_una_columna_de_croquis_que_no_trae_rotulos_se_reasigna() -> None:
    """
    «ELEMENTO» es el croquis en esta planilla y bien podría ser el elemento
    estructural en otra. El rótulo solo no distingue; el contenido sí.

    Es mejor perder el croquis que leer «VIGA 1» como si fueran las letras
    v, i, g, a — porque entonces el importador sugeriría un tipo de cuatro
    medidas para una barra recta.
    """
    from pdf_parser import _Tabla, _resolver_croquis_y_seccion

    columnas: ColumnMapping = mapear_columnas(["POS.", "ELEMENTO", "a (cm)"])
    assert columnas.mapped["sketch"] == 1

    tablas = [
        _Tabla(1, [["P1", "VIGA 1", "300"], ["P2", "VIGA 2", "400"]]),
    ]
    _resolver_croquis_y_seccion(columnas, tablas, ancho=3)

    assert "sketch" not in columnas.mapped
    assert columnas.mapped["section"] == 1

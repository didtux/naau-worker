"""
La planilla cargada en Excel, ida y vuelta.

── Qué se prueba acá, y qué no ─────────────────────────────────────────────

Se prueba que la PLANTILLA y el LECTOR no se puedan desincronizar, y que un
libro llenado a mano entre por el mismo camino que un PDF: los mismos campos,
las mismas unidades, la misma deducción y la misma verificación.

No se prueba de nuevo el mapeo de columnas ni la aritmética de las filas — eso
ya tiene sus pruebas y no cambia según de qué formato salió la tabla. Esa es
justamente la razón de que el XLSX se lea con `_interpretar`: si hubiera un
lector de Excel propio, habría que volver a probar todo dos veces, y el día que
las dos versiones difirieran la planilla se leería distinto según en qué archivo
llegó el mismo dato.

Sin OCR: estas pruebas escriben celdas y las vuelven a leer, así que corren en
milisegundos.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from pdf_parser import CorruptExcelError, ParseError, mapear_columnas, parse_xlsx
from xlsx_table import (
    COLUMNAS_DE_RESULTADOS,
    HOJA_DE_AYUDA,
    HOJA_DE_DATOS,
    HOJA_DE_RESULTADOS,
    encabezados_de_exportacion,
    encabezados_de_plantilla,
    generar_export,
    generar_plantilla,
    tablas_de_excel,
)

openpyxl = pytest.importorskip("openpyxl")


def _llenar(filas: list[tuple], unidad: str = "cm") -> bytes:
    """La plantilla oficial, con estas filas escritas debajo del encabezado."""
    libro = openpyxl.load_workbook(io.BytesIO(generar_plantilla(unidad)))
    hoja = libro[HOJA_DE_DATOS]
    for i, fila in enumerate(filas, start=2):
        for j, valor in enumerate(fila, start=1):
            hoja.cell(row=i, column=j, value=valor)
    buffer = io.BytesIO()
    libro.save(buffer)
    return buffer.getvalue()


# ── La plantilla y el lector, atados ───────────────────────────────────────


def test_todos_los_encabezados_de_la_plantilla_se_reconocen() -> None:
    """
    Ni una columna de la plantilla propia puede quedar sin reconocer.

    Es LA prueba de este archivo. La plantilla la genera el mismo módulo que la
    lee justamente para que no se separen, y esto lo comprueba: si alguien
    cambia «CANT.» por «CANTIDAD DE PIEZAS» en `COLUMNAS_FIJAS`, el archivo se
    seguiría descargando igual de lindo y volvería con la columna sin
    reconocer — y la culpa parecería de quien lo llenó.
    """
    columnas = mapear_columnas(encabezados_de_plantilla("cm"))

    assert columnas.mapped["code"] == 0, "POS."
    assert columnas.mapped["type_code"] == 1, "TIPO"
    assert columnas.mapped["diameter"] == 2, "Ø (mm)"
    assert columnas.mapped["quantity"] == 3, "CANT."
    assert columnas.mapped["elements"] == 4, "VECES"
    assert columnas.dimension_columns == {"a": 5, "b": 6, "c": 7, "d": 8, "e": 9, "f": 10}
    assert columnas.unmapped == [], "ninguna columna de la plantilla propia sin reconocer"


def test_la_unidad_de_la_plantilla_la_declara_su_encabezado() -> None:
    """
    La plantilla dice en qué unidad están sus medidas, y el lector lo lee.

    Es el mismo mecanismo que usan las planillas de obra de verdad —«DIMENSIONES
    (cm.)»— y por eso alcanza con rotularlo: no hace falta un campo aparte ni
    que el cliente mande la unidad por separado. Quien prefiera la otra puede
    cambiar el rótulo a mano y el archivo se sigue leyendo bien.
    """
    en_cm = parse_xlsx(_llenar([(1, "I", 10, 4, 1, 250)], "cm"))
    assert en_cm.unit == "cm"
    assert "declara cm" in en_cm.unit_evidence

    en_m = parse_xlsx(_llenar([(1, "I", 10, 4, 1, 2.5)], "m"))
    assert en_m.unit == "m"
    assert "declara m" in en_m.unit_evidence


# ── Una planilla llenada a mano ────────────────────────────────────────────


def test_un_libro_llenado_a_mano_se_lee_entero() -> None:
    """Las cinco columnas fijas y las medidas, fila por fila."""
    leido = parse_xlsx(
        _llenar(
            [
                (1, "L", 12, 8, 2, 250, 45),
                (2, "I", 10, 12, 1, 600),
                (3, "C", 12.7, 4, 1, 120, 80, 120),
            ]
        )
    )

    assert leido.source == "xlsx", "la pantalla tiene que poder decir de dónde salió"
    assert [f.code for f in leido.rows] == ["1", "2", "3"]
    assert [f.type_code for f in leido.rows] == ["L", "I", "C"]
    assert [f.diameter_mm for f in leido.rows] == [12.0, 10.0, 12.7]
    assert [f.quantity for f in leido.rows] == [8, 12, 4]
    assert [f.elements for f in leido.rows] == [2, 1, 1]
    assert [[(d.name, d.value) for d in f.dimensions] for f in leido.rows] == [
        [("a", 250.0), ("b", 45.0)],
        [("a", 600.0)],
        [("a", 120.0), ("b", 80.0), ("c", 120.0)],
    ]
    assert all(not f.issues for f in leido.rows), leido.rows


def test_el_tipo_que_el_papel_declara_viaja_crudo() -> None:
    """
    El código del tipo se entrega tal como está escrito, sin resolverlo.

    Este lector no conoce el catálogo de la empresa, así que no puede decir si
    «EST-1» existe ni si su fórmula reproduce la longitud de la fila. Las dos
    cosas las decide Nest, que tiene el catálogo — y las decide en ese orden:
    primero la aritmética, después el rótulo.
    """
    leido = parse_xlsx(_llenar([(1, "EST-1", 8, 30, 1, 20, 40, 20)]))
    assert leido.rows[0].type_code == "EST-1"


def test_el_orden_de_las_columnas_no_importa() -> None:
    """
    Una planilla con las columnas en otro orden se lee igual.

    Es la misma propiedad que el lector ya tiene para los PDFs, y en un Excel
    cargado a mano hace falta más: la gente mueve columnas.
    """
    libro = openpyxl.Workbook()
    hoja = libro.active
    # Las medidas antes que todo, el diámetro al final, y el tipo en el medio.
    hoja.append(["a (cm)", "b (cm)", "POS.", "TIPO", "CANT.", "VECES", "Ø (mm)"])
    hoja.append([250, 45, 1, "L", 8, 2, 12])
    buffer = io.BytesIO()
    libro.save(buffer)

    fila = parse_xlsx(buffer.getvalue()).rows[0]
    assert fila.code == "1"
    assert fila.type_code == "L"
    assert fila.diameter_mm == 12.0
    assert fila.quantity == 8
    assert fila.elements == 2
    assert [(d.name, d.value) for d in fila.dimensions] == [("a", 250.0), ("b", 45.0)]


def test_las_filas_en_blanco_de_la_plantilla_no_son_filas() -> None:
    """
    La plantilla trae sesenta filas con formato puesto y ninguna es una posición.

    Sin esto, descargar la plantilla, escribir dos filas y subirla devolvería
    dos posiciones y cincuenta y ocho vacías.
    """
    leido = parse_xlsx(_llenar([(1, "I", 10, 4, 1, 250), (2, "I", 10, 4, 1, 300)]))
    assert len(leido.rows) == 2


# ── La hoja de instrucciones ───────────────────────────────────────────────


def test_la_hoja_de_instrucciones_no_aporta_filas() -> None:
    """
    La hoja de ayuda viaja en el archivo y no se lee como datos.

    Se entrega al lector igual que la de datos —no se filtra por nombre— porque
    quién es la tabla de posiciones lo decide el lector mirando los rótulos, y
    eso es lo que hace que una hoja de notas de quien cargó la planilla tampoco
    moleste. Lo que la descarta es que tiene otro ancho.
    """
    matrices, _ = tablas_de_excel(_llenar([(1, "I", 10, 4, 1, 250)]))
    assert len(matrices) == 2, "las dos hojas se entregan"

    leido = parse_xlsx(_llenar([(1, "I", 10, 4, 1, 250)]))
    assert len(leido.rows) == 1
    assert leido.rows[0].code == "1"


def test_la_prosa_de_la_ayuda_no_decide_el_separador_decimal() -> None:
    """
    Una medida de «120.5» tiene que leerse 120,5 y no 1205.

    Es la prueba de un error que estuvo ahí desde el principio y que la hoja de
    instrucciones puso a la vista. El estilo decimal del documento se decidía
    con la primera celda que tuviera un punto y una coma, MIRANDO CUALQUIER
    CELDA: la primera oración de la ayuda —«…es la que se lee. Esta hoja se
    ignora al subir el archivo,»— tiene un punto y después una coma, así que el
    libro entero pasaba a estilo europeo y «120.5» salía 1205.

    Diez veces más grande, verosímil, y sin una sola señal en la pantalla.

    En un PDF hace falta la misma casualidad y basta una vez: el título del
    documento cae en la primera celda de la tabla. Ahora el estilo se decide
    sólo con celdas NUMÉRICAS, que es de donde tiene que salir.
    """
    ayuda = [linea for linea in _ayuda_del_libro(_llenar([(1, "I", 10, 4, 1, 120.5)]))]
    assert any("." in l and "," in l and l.index(".") < l.rindex(",") for l in ayuda), (
        "la prueba no vale si la ayuda dejó de tener la oración que disparaba el error"
    )

    leido = parse_xlsx(_llenar([(1, "I", 10, 4, 1, 120.5)]))
    assert [(d.name, d.value) for d in leido.rows[0].dimensions] == [("a", 120.5)]


def _ayuda_del_libro(data: bytes) -> list[str]:
    libro = openpyxl.load_workbook(io.BytesIO(data))
    return [
        str(fila[0]) for fila in libro[HOJA_DE_AYUDA].iter_rows(values_only=True) if fila[0]
    ]


# ── Lo único ambiguo que puede traer un Excel ──────────────────────────────


def test_un_numero_escrito_como_texto_con_coma_se_avisa() -> None:
    """
    «6,15» en una celda de TEXTO se lee 615, y se dice.

    Es el único caso en que un libro puede ser ambiguo: Excel guarda el valor de
    una celda numérica, no cómo se ve, así que 120.5 es 120,5 en cualquier
    idioma. Una celda de texto no trae valor, trae esas cuatro letras.

    No se corrige, y es a propósito: corregirla sería decidir por cuenta propia
    que la coma es un decimal y no un separador de miles, sobre la única celda
    del libro que no trae su valor. Se nombra, y quien la escribió la vuelve a
    escribir.
    """
    leido = parse_xlsx(_llenar([(1, "I", 10, 4, 1, "6,15")]))

    avisos = " ".join(leido.warnings)
    assert "coma decimal" in avisos
    assert "«6,15»" in avisos
    assert [(d.name, d.value) for d in leido.rows[0].dimensions] == [("a", 615.0)]


def test_una_planilla_de_enteros_no_recibe_avisos_de_decimales() -> None:
    """
    Sin un solo separador en el documento, el estilo no puede cambiar nada.

    El aviso decía «no se pudo determinar si los números usan coma o punto» en
    toda planilla sin una celda con las dos formas, incluidas las que están
    escritas enteramente con números enteros — que son la mitad de las cargadas
    a mano. Ahí no hay ninguna medida que revisar, y un aviso que aparece
    siempre es un aviso que nadie lee.
    """
    leido = parse_xlsx(_llenar([(1, "I", 10, 4, 1, 250), (2, "L", 12, 6, 1, 300, 40)]))
    assert not any("coma o punto decimal" in a for a in leido.warnings), leido.warnings


# ── Archivos que no son un libro ───────────────────────────────────────────


def test_un_zip_que_no_es_un_libro_se_rechaza_con_su_propio_codigo() -> None:
    """
    Un `.xlsx` es un ZIP, así que la firma de bytes deja entrar cualquier ZIP.

    La firma alcanza para que al decodificador no le llegue un video; distinguir
    un libro de cálculo de una carta es trabajo de abrirlo. Y tiene su propio
    código porque lo que hay que hacer es distinto: volver a guardar el archivo
    como .xlsx, no cambiar de archivo.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("hola.txt", "esto no es un libro")

    with pytest.raises(CorruptExcelError) as e:
        parse_xlsx(buffer.getvalue())
    assert "xlsx" in str(e.value)


def test_un_libro_vacio_no_es_una_planilla() -> None:
    """Un libro sin una sola celda no trae tabla, y se dice sin hablar de OCR."""
    libro = openpyxl.Workbook()
    buffer = io.BytesIO()
    libro.save(buffer)

    with pytest.raises(ParseError):
        parse_xlsx(buffer.getvalue())


# ── La exportación ────────────────────────────────────────
#
# Lo que se prueba acá es que el archivo que NAAU baja se pueda volver a subir.
# No es una comodidad: es lo que hace que «exportar a Excel» sirva para
# corregir cuarenta filas con el teclado y devolverlas, en lugar de ser una
# copia de lectura.


LIBRO = {
    "unit": "cm",
    "measures": ["a", "b"],
    "rows": [
        {
            "pos": 1,
            "type": "L",
            "diameter": 10.0,
            "quantity": 24,
            "elements": 1,
            "measures": {"a": 200.0, "b": 40.0},
            "unit_length": None,
        },
        {
            "pos": 2,
            "type": "O",
            "diameter": 8.0,
            "quantity": 291,
            "elements": 1,
            "measures": {"a": 20.0, "b": 30.5},
            "unit_length": None,
        },
        {
            "pos": 3,
            "type": "I",
            "diameter": 12.7,
            "quantity": 6,
            "elements": 2,
            "measures": {},
            "unit_length": 11.4,
        },
    ],
    "results": [
        {
            "pos": 1,
            "type": "L",
            "diameter": 10.0,
            "cut_length": 2.4,
            "pieces": 24,
            "pieces_per_bar": 5,
            "waste_per_bar": 0.0,
            "bars": 5,
            "required_weight_kg": 35.5,
            "bars_weight_kg": 37.0,
        }
    ],
}


def test_la_planilla_exportada_se_vuelve_a_leer_entera() -> None:
    """
    Ida y vuelta: lo que se baja se sube y dice lo mismo.

    Es la prueba central de la exportación. Las cinco columnas fijas, las
    medidas con su nombre y su unidad, y la longitud declarada de la fila que no
    tiene medidas.
    """
    leido = parse_xlsx(generar_export(LIBRO))

    assert leido.source == "xlsx"
    assert [f.code for f in leido.rows] == ["1", "2", "3"]
    assert [f.type_code for f in leido.rows] == ["L", "O", "I"]
    assert [f.diameter_mm for f in leido.rows] == [10.0, 8.0, 12.7]
    assert [f.quantity for f in leido.rows] == [24, 291, 6]
    assert [f.elements for f in leido.rows] == [1, 1, 2]
    assert [[(d.name, d.value) for d in f.dimensions] for f in leido.rows] == [
        [("a", 200.0), ("b", 40.0)],
        [("a", 20.0), ("b", 30.5)],
        [],
    ]
    assert all(not f.issues for f in leido.rows), leido.rows


def test_la_unidad_de_la_planilla_exportada_la_declara_su_encabezado() -> None:
    """Igual que la plantilla: la unidad va en el rótulo de las medidas."""
    assert parse_xlsx(generar_export(LIBRO)).unit == "cm"

    en_metros = dict(LIBRO, unit="m")
    assert parse_xlsx(generar_export(en_metros)).unit == "m"


def test_la_longitud_declarada_vuelve_como_longitud_de_una_pieza() -> None:
    """
    La columna «LONG. (m)» es el DESARROLLO de una pieza, no el total de la fila.

    Es la razón por la que el rótulo es exactamente ése: normalizado es
    `LONG (M)`, que es un alias de `unit_length`. Con «LONG. TOTAL» sería
    `total_length` —la longitud de todas las piezas juntas— y una fila de 291
    estribos de 11,40 m volvería con estribos de 3,9 cm.
    """
    leido = parse_xlsx(generar_export(LIBRO))
    assert leido.rows[2].claimed.unit_length == 11.4
    assert leido.rows[2].claimed.total_length is None


def test_la_hoja_de_resultados_no_puede_ganarle_la_tabla_a_la_planilla() -> None:
    """
    Ninguna columna de «Resultados» es una letra sola, y eso la descarta.

    `_buscar_encabezado` acepta una tabla si reconoce el rótulo de posición Y al
    menos una columna de medida. «Resultados» tiene POS., así que lo único que la
    deja afuera es no tener ninguna letra.

    Vale probarlo explícito porque el día que alguien le agregue una columna
    «a», el archivo re-importado traería los RESULTADOS en lugar de la planilla
    —sin ningún error, y con las columnas cruzadas—.
    """
    columnas = mapear_columnas(list(COLUMNAS_DE_RESULTADOS))
    assert not columnas.dimension_columns, COLUMNAS_DE_RESULTADOS

    # Y en el archivo de verdad: las tres hojas se entregan y se lee la primera.
    matrices, _ = tablas_de_excel(generar_export(LIBRO))
    assert len(matrices) == 3, "planilla, resultados e instrucciones"

    leido = parse_xlsx(generar_export(LIBRO))
    assert [f.code for f in leido.rows] == ["1", "2", "3"]


def test_las_tres_hojas_van_en_orden_con_la_planilla_primero() -> None:
    """El orden del libro es lo que el lector recibe. Ver `generar_export`."""
    libro = openpyxl.load_workbook(io.BytesIO(generar_export(LIBRO)))
    assert libro.sheetnames == [HOJA_DE_DATOS, HOJA_DE_RESULTADOS, HOJA_DE_AYUDA]


def test_sin_calculo_no_se_escribe_la_hoja_de_resultados() -> None:
    """Una hoja con su encabezado y ninguna fila no dice nada."""
    sin_resultados = dict(LIBRO, results=[])
    libro = openpyxl.load_workbook(io.BytesIO(generar_export(sin_resultados)))
    assert libro.sheetnames == [HOJA_DE_DATOS, HOJA_DE_AYUDA]


def test_una_planilla_sin_medidas_sigue_siendo_reconocible() -> None:
    """
    Todas las filas declaran su desarrollo, y el encabezado igual se reconoce.

    Es el caso de la planilla de obra que motivó el modo de longitud declarada:
    `POS. | Ø | TIPO | LOG. TOTAL (m.) | CANT. | VECES` y ninguna medida. Sin
    columnas de medida el lector rechaza la tabla entera, así que se emiten las
    letras de la plantilla vacías — sin eso, el archivo que NAAU baja no se
    podría volver a subir.
    """
    solo_declaradas = dict(
        LIBRO, measures=[], rows=[LIBRO["rows"][2]], results=[]
    )

    encabezados = encabezados_de_exportacion([], "cm")
    assert mapear_columnas(encabezados).dimension_columns, encabezados

    leido = parse_xlsx(generar_export(solo_declaradas))
    assert [f.code for f in leido.rows] == ["3"]
    assert leido.rows[0].claimed.unit_length == 11.4
    assert [d.name for d in leido.rows[0].dimensions] == []


def test_una_medida_con_nombre_propio_no_se_reconoce_y_viaja_cruda() -> None:
    """
    Una medida que se llama «alto» vuelve sin reconocer, y eso está dicho.

    El lector reconoce una columna de medida por su LETRA —`RE_DIMENSION`— así
    que «alto (cm)» no es una medida para él. No se disimula: cae en `unmapped`,
    viaja cruda en cada fila y la pantalla la muestra, que es lo que hace con
    cualquier columna que no reconoce.

    Lo que SÍ tiene que seguir funcionando es el resto de la fila, y por eso la
    planilla lleva igual las letras de la plantilla: sin ninguna columna de
    medida reconocida, el lector rechazaría la tabla completa.
    """
    con_nombres = {
        "unit": "cm",
        "measures": ["alto", "gancho"],
        "rows": [
            {
                "pos": 7,
                "type": "ZAP-2P",
                "diameter": 16.0,
                "quantity": 4,
                "elements": 1,
                "measures": {"alto": 120.0, "gancho": 15.0},
                "unit_length": None,
            }
        ],
        "results": [],
    }

    encabezados = encabezados_de_exportacion(["alto", "gancho"], "cm")
    columnas = mapear_columnas(encabezados)
    assert not columnas.dimension_columns
    # Las dos columnas sin reconocer, por índice: POS., TIPO, Ø, CANT., VECES, y
    # después «alto» y «gancho».
    assert columnas.unmapped == [5, 6]

    # Y por eso este libro NO se puede re-importar.
    #
    # Tampoco lo salva `column_roles`, que deduce el papel de una columna por
    # las CUENTAS del papel cuando el rótulo no se reconoce: para cerrar «suma
    # de las medidas = longitud» hace falta una columna de longitud CON valores,
    # y la planilla exportada no escribe ninguna cifra derivada —la longitud de
    # corte la calcula NAAU y va en la hoja de resultados—.
    #
    # Es el límite conocido de la exportación, y está escrito en la hoja de
    # instrucciones del propio archivo.
    with pytest.raises(ParseError):
        parse_xlsx(generar_export(con_nombres))


"""
Una planilla en Excel: la misma tabla, sin reconocer nada.

── Por qué el XLSX entra por el mismo lector que el PDF ────────────────────

Porque un XLSX no es un documento distinto: es la MISMA matriz de celdas que
`pdfplumber` entrega de un PDF y que el reconocimiento de caracteres entrega de
una foto, sólo que llega sin pérdida. De ahí en adelante todo lo que hace falta
ya está escrito y probado: el mapeo de columnas por sinónimos, las dos unidades,
el estilo numérico, la deducción de la celda que falta a partir de las cuentas
del propio papel, y la verificación fila por fila.

Escribir un lector de Excel propio habría sido un SEGUNDO camino de entrada con
su propia idea de qué es una fila válida — y la clase de desfase que termina en
una planilla que se lee distinto según en qué formato llegó el mismo dato.

Lo único que este módulo hace es convertir celdas de Excel en texto. Nada más:
no mapea columnas, no decide unidades y no interpreta nada.

── Por qué la plantilla se GENERA acá ──────────────────────────────────────

Porque la plantilla y el lector tienen que decir lo mismo, y la única forma de
garantizarlo es que salgan del mismo archivo. Los encabezados que se escriben en
`generar_plantilla` son los que `mapear_columnas` reconoce, y el rótulo de unidad
que llevan las columnas de medida —«a (cm)»— es el que `_unidad_declarada` lee.

Si la plantilla viviera del lado del cliente, un rótulo cambiado ahí dejaría de
mapearse acá sin que nada lo avise: la planilla se subiría y volvería con las
columnas sin reconocer.

── Los topes, y contra qué defienden ───────────────────────────────────────

Un XLSX es un ZIP con XML adentro, así que el tope de bytes del endpoint no dice
nada del trabajo que hay dentro: un archivo de 100 kB puede declarar un millón
de filas. Por eso se lee en modo streaming y con tope de hojas, de filas, de
columnas y de celdas totales — el que se alcanza primero corta.
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime

# ── Topes ──────────────────────────────────────────────────────────────────
#
# Una planilla de un elemento no pasa de unas decenas de filas; `MAX_FILAS` deja
# margen de sobra y corta antes de que una hoja con un millón de filas
# declaradas importe. Las columnas topean en 40 porque la planilla de obra más
# ancha que se midió tiene 14.

MAX_HOJAS = 12
MAX_FILAS = 600
MAX_COLUMNAS = 40
MAX_CELDAS = 20_000

#: Un número escrito con coma decimal, en una celda de TEXTO.
#:
#: Es el único caso en que un libro de Excel puede ser ambiguo, y por eso se
#: busca. Ver el aviso en `tablas_de_excel` y `parse_xlsx`.
RE_COMA_DECIMAL = re.compile(r"^\d{1,6},\d{1,3}$")

#: El caso SIN ambigüedad: uno o dos decimales después de la coma.
#:
#: Un separador de miles siempre va seguido de tres cifras, así que «2,45» y
#: «6,5» sólo pueden ser decimales. «6,150» sí es ambiguo —6,15 o 6150— y ése
#: se sigue avisando sin tocar.
RE_COMA_DECIMAL_CLARA = re.compile(r"^(\d{1,6}),(\d{1,2})$")


class ExcelIlegible(Exception):
    """No se pudo sacar una tabla de este archivo. `code` es lo que Nest mapea."""

    def __init__(self, code: str, mensaje: str) -> None:
        super().__init__(mensaje)
        self.code = code


# ── La plantilla ───────────────────────────────────────────────────────────
#
# Los encabezados son exactamente los que `SINONIMOS` reconoce. No es una
# coincidencia y no se pueden cambiar sueltos: ver el encabezado del módulo.

COLUMNAS_FIJAS: tuple[str, ...] = ("POS.", "ESQUEMA DE DOBLADO", "Ø (mm)", "CANT.", "VECES")

MEDIDAS_DE_LA_PLANTILLA: tuple[str, ...] = ("a", "b", "c", "d", "e", "f")

# Cuántas filas en blanco lleva la plantilla. No es un límite de nada: es
# cuántas filas vienen con formato de número puesto, para que quien cargue no
# tenga que pelear con el formato de celda. Se pueden agregar más.
FILAS_EN_BLANCO = 60

# La hoja de datos va PRIMERA, y es una condición de funcionamiento, no estética:
# el lector busca la fila de encabezado en las tres primeras filas de cada tabla,
# y las hojas se le entregan en orden.
HOJA_DE_DATOS = "Planilla"
HOJA_DE_AYUDA = "Instrucciones"

UNIDADES_DE_PLANTILLA = ("cm", "m")


def encabezados_de_plantilla(unidad: str = "cm") -> list[str]:
    """
    Los encabezados de la plantilla, con la unidad rotulada en las medidas.

    La unidad va EN EL RÓTULO y no en una celda aparte porque es así como la lee
    el propio lector: `_unidad_declarada` busca «(cm)» o «(m)» en el encabezado
    de las columnas de medida. Quien prefiera la otra unidad puede cambiar el
    rótulo a mano y el archivo sigue leyéndose bien — es el mismo mecanismo que
    usan las planillas de obra de verdad.
    """
    if unidad not in UNIDADES_DE_PLANTILLA:
        raise ValueError(f"unidad no admitida: {unidad!r}")
    return [*COLUMNAS_FIJAS, *(f"{letra} ({unidad})" for letra in MEDIDAS_DE_LA_PLANTILLA)]


# ── La exportación ───────────────────────────────────────
#
# La planilla cargada, bajada a Excel. Es la plantilla con las filas puestas, y
# eso es la mitad del punto: el archivo que sale de acá se puede volver a subir
# por el importador que ya existe.
#
# Por eso vive en este módulo y no en Nest, que es donde están los datos: los
# ENCABEZADOS son de acá, porque son los que el lector de este mismo archivo
# reconoce. Nest manda los datos con su papel —`pos`, `type`, `measures`— y
# acá se decide cómo se rotula cada columna. Escritos del otro lado, un rótulo
# cambiado de un solo lado rompería el viaje de vuelta sin que nada lo avise.

#: El rótulo de la longitud de corte declarada.
#:
#: Normalizado es `LONG (M)`, que es un alias EXACTO de `unit_length` en
#: `SINONIMOS`. No es casualidad y no se cambia solo: es la columna que hace que
#: una fila que declara su desarrollo —sin medidas— vuelva a leerse como la
#: longitud de UNA pieza y no como el total de la posición.
COLUMNA_LONGITUD = "LONG. (m)"

HOJA_DE_RESULTADOS = "Resultados"

#: Los encabezados de la hoja de resultados.
#:
#: Ninguno es una letra sola, y eso es una CONDICIÓN y no una casualidad:
#: `_buscar_encabezado` exige el rótulo de posición y al menos una columna de
#: medida —una letra de la «a» a la «l»— para aceptar una tabla. Sin ninguna
#: letra, esta hoja no puede ganarle la tabla de posiciones a la primera cuando
#: el archivo se vuelve a subir.
#:
#: Si algún día se le agrega una columna de una letra, el viaje de vuelta
#: traería los resultados en lugar de la planilla, y sin ningún error. Lo cubre
#: una prueba en `tests/test_xlsx.py`.
COLUMNAS_DE_RESULTADOS = (
    "POS.",
    "ESQUEMA DE DOBLADO",
    "Ø (mm)",
    "LONG. DE CORTE (m)",
    "PIEZAS",
    "PIEZAS POR BARRA",
    "MERMA POR BARRA (m)",
    "BARRAS",
    "PESO EMPLEADO (kg)",
    "PESO COMPRADO (kg)",
)

AYUDA_DE_EXPORTACION = (
    "",
    "Sobre este archivo",
    "",
    "Salió de NAAU con las filas que la planilla tenía GUARDADAS. La hoja",
    "«Planilla» tiene el mismo formato que la plantilla vacía, así que se puede",
    "editar acá y volver a subir por «Importar».",
    "",
    "La hoja «Resultados» es de lectura: la calcula NAAU y se ignora al subir el",
    "archivo. Si cambiás algo en «Planilla», esos números dejan de corresponder",
    "hasta que la planilla se vuelva a calcular.",
    "",
    "Dos cosas que conviene saber del viaje de vuelta:",
    "",
    "  · Las columnas de medida se reconocen por su LETRA. Si tu tipo de barra",
    "    tiene medidas con nombre propio —«alto», «gancho»— esas columnas",
    "    llegan sin reconocer, y la pantalla te las muestra para que las asignes.",
    "",
    "  · El diámetro va siempre en milímetros, sea cual sea la designación que",
    "    use la planilla en pantalla. Es el valor con el que se calcula.",
)


def encabezados_de_exportacion(medidas, unidad="cm"):
    """
    Los encabezados de la planilla exportada: los fijos, las medidas, la longitud.

    Las medidas son las que la planilla usa DE VERDAD, con el nombre que le puso
    quien definió el tipo. No son las seis letras de la plantilla: una planilla
    de tres medidas no tiene por qué llevar tres columnas vacías, y una de ocho
    no entraría.

    Si la planilla no tiene ninguna medida —todas sus filas declaran el
    desarrollo total— se emiten las letras de la plantilla, vacías. Es lo que
    mantiene el encabezado RECONOCIBLE: el lector exige al menos una columna de
    medida para aceptar una tabla, así que sin ellas el archivo bajado no se
    podría volver a subir.
    """
    if unidad not in UNIDADES_DE_PLANTILLA:
        raise ValueError("unidad no admitida: %r" % (unidad,))

    nombres = list(medidas) if medidas else list(MEDIDAS_DE_LA_PLANTILLA)
    return [
        *COLUMNAS_FIJAS,
        *("%s (%s)" % (nombre, unidad) for nombre in nombres),
        COLUMNA_LONGITUD,
    ]


def _encabezar(hoja, encabezados, anchos):
    """La fila de encabezado con su formato, los anchos y el panel fijo."""
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    hoja.append(encabezados)

    negrita = Font(bold=True)
    fondo = PatternFill("solid", fgColor="E8EAED")
    centro = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for columna in range(1, len(encabezados) + 1):
        celda = hoja.cell(row=1, column=columna)
        celda.font = negrita
        celda.fill = fondo
        celda.alignment = centro

    for columna, ancho in enumerate(anchos, start=1):
        hoja.column_dimensions[get_column_letter(columna)].width = ancho

    # El encabezado queda a la vista al bajar: una planilla de cuarenta
    # posiciones se lee desplazándose, y sin esto hay que contar columnas.
    hoja.freeze_panes = "A2"


def generar_export(datos):
    """
    El libro de una planilla cargada: «Planilla», «Resultados» e instrucciones.

    ── El orden de las hojas es una condición ───────────────────────

    «Planilla» va PRIMERA. El lector recibe las hojas en el orden del libro y se
    queda con la primera tabla que reconoce, así que ese orden es parte de lo que
    decide qué se lee al volver a subir el archivo.

    Que las otras dos no puedan ganarle no depende del orden sino de sus
    rótulos —ninguna tiene una columna de una letra sola—, pero las dos cosas
    juntas son lo que hace el viaje de vuelta previsible.

    ── Qué se escribe y qué no ───────────────────────────────────

    Los valores van como NÚMEROS y no como texto, porque es un archivo para
    editar en Excel: una celda de texto con un número adentro es justo lo que
    después no se distingue de un dato, y el propio lector avisa de eso.

    Una medida ausente se deja VACÍA y no en cero. Un cero en una columna de
    medida es una medida de cero, y una figura con un tramo de cero no es la
    misma figura.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font

    unidad = datos.get("unit", "cm")
    medidas = list(datos.get("measures") or [])
    filas = list(datos.get("rows") or [])
    resultados = list(datos.get("results") or [])

    encabezados = encabezados_de_exportacion(medidas, unidad)

    libro = Workbook()
    hoja = libro.active
    hoja.title = HOJA_DE_DATOS

    _encabezar(
        hoja,
        encabezados,
        [8, 12, 10, 8, 8] + [11] * (len(encabezados) - len(COLUMNAS_FIJAS)),
    )

    for fila in filas:
        valores = fila.get("measures") or {}
        hoja.append(
            [
                fila.get("pos"),
                fila.get("type"),
                fila.get("diameter"),
                fila.get("quantity"),
                fila.get("elements"),
                *(valores.get(nombre) for nombre in medidas),
                fila.get("unit_length"),
            ]
        )

    if resultados:
        resumen = libro.create_sheet(HOJA_DE_RESULTADOS)
        _encabezar(
            resumen,
            list(COLUMNAS_DE_RESULTADOS),
            [8, 12, 10, 14, 10, 12, 14, 10, 14, 14],
        )
        for r in resultados:
            resumen.append(
                [
                    r.get("pos"),
                    r.get("type"),
                    r.get("diameter"),
                    r.get("cut_length"),
                    r.get("pieces"),
                    r.get("pieces_per_bar"),
                    r.get("waste_per_bar"),
                    r.get("bars"),
                    r.get("required_weight_kg"),
                    r.get("bars_weight_kg"),
                ]
            )

    ayuda = libro.create_sheet(HOJA_DE_AYUDA)
    for linea in (*AYUDA, *AYUDA_DE_EXPORTACION):
        ayuda.append([linea])
    ayuda.column_dimensions["A"].width = 95
    ayuda.cell(row=1, column=1).font = Font(bold=True, size=13)

    buffer = io.BytesIO()
    libro.save(buffer)
    return buffer.getvalue()


AYUDA = (
    "Cómo llenar esta plantilla",
    "",
    "La hoja «Planilla» es la que se lee. Esta hoja se ignora al subir el archivo,",
    "así que puedes dejarla como está.",
    "",
    "Una fila por posición. Los encabezados NO se cambian de nombre ni de lugar:",
    "el orden de las columnas da igual, pero el rótulo es lo que se reconoce.",
    "",
    "POS.     El número de la posición. Es lo único obligatorio de la fila: una fila",
    "         sin número no se lee. Pueden ir en cualquier orden — la tabla los acomoda.",
    "",
    "TIPO     El código del tipo de barra: I, L, C, P, O del sistema, o el código de",
    "         un tipo que definió tu empresa. Si el tipo no existe en el catálogo, la",
    "         fila llega igual y la pantalla te dice cuál falta.",
    "",
    "Ø (mm)   El diámetro en milímetros: 6, 8, 10, 12, 16, 20, 25, 32 para las",
    "         métricas; 6.4, 9.5, 12.7, 15.9, 19, 22.2, 25.4 para las de pulgada.",
    "",
    "CANT.    Cuántas piezas iguales lleva el elemento.",
    "",
    "VECES    Cuántas veces se repite el elemento. Si no lo separas, pon 1: las",
    "         piezas totales son CANT. × VECES.",
    "",
    "a b c …  Las medidas de la figura, en la unidad que dice el encabezado.",
    "         Llená sólo las que la figura usa: una barra recta usa «a» sola.",
    "",
    "Lo que NO se pone: la longitud de corte, la cantidad de barras y el peso. Esos",
    "los calcula NAAU, y es justamente para lo que sirve — si los escribes a mano,",
    "se usan sólo para comparar contra el cálculo y avisarte si no coinciden.",
    "",
    "Se pueden agregar todas las filas que hagan falta debajo de las que ya están.",
)


def generar_plantilla(unidad: str = "cm") -> bytes:
    """La plantilla vacía, lista para llenar y volver a subir."""
    # Diferido: `openpyxl` son unos megas y un worker que sólo recibe PDFs no
    # los tiene que pagar. Igual que OpenCV en `image_table`.
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    encabezados = encabezados_de_plantilla(unidad)

    libro = Workbook()
    hoja = libro.active
    hoja.title = HOJA_DE_DATOS

    hoja.append(encabezados)

    negrita = Font(bold=True)
    fondo = PatternFill("solid", fgColor="E8EAED")
    centro = Alignment(horizontal="center", vertical="center")

    for columna in range(1, len(encabezados) + 1):
        celda = hoja.cell(row=1, column=columna)
        celda.font = negrita
        celda.fill = fondo
        celda.alignment = centro

    # Anchos: los rótulos fijos son más largos que las letras de medida.
    anchos = [8, 10, 10, 8, 8] + [11] * len(MEDIDAS_DE_LA_PLANTILLA)
    for columna, ancho in enumerate(anchos, start=1):
        hoja.column_dimensions[get_column_letter(columna)].width = ancho

    # El encabezado queda a la vista al bajar: una planilla de cuarenta
    # posiciones se carga desplazándose, y sin esto hay que contar columnas.
    hoja.freeze_panes = "A2"

    # Formato de número en las filas en blanco. Sin esto, Excel puede guardar un
    # «8» tipeado en una celda con formato de texto, y una celda de texto con un
    # número adentro es justo lo que después no se distingue de un dato.
    for fila in range(2, 2 + FILAS_EN_BLANCO):
        for columna in range(1, len(encabezados) + 1):
            hoja.cell(row=fila, column=columna).number_format = "General"

    ayuda = libro.create_sheet(HOJA_DE_AYUDA)
    for linea in AYUDA:
        ayuda.append([linea])
    ayuda.column_dimensions["A"].width = 95
    ayuda.cell(row=1, column=1).font = Font(bold=True, size=13)

    buffer = io.BytesIO()
    libro.save(buffer)
    return buffer.getvalue()


# ── La lectura ─────────────────────────────────────────────────────────────


def tablas_de_excel(data: bytes) -> tuple[list[list[list[str | None]]], list[str]]:
    """
    Las hojas del libro, cada una como una matriz de celdas de texto.

    Devuelve UNA matriz por hoja y en el orden del libro, sin elegir ninguna: cuál
    es la tabla de posiciones lo decide `_buscar_encabezado`, que ya sabe hacerlo
    —lo hace con las páginas de un PDF— y lo hace mirando los rótulos en lugar de
    confiar en cómo se llama la hoja. Una hoja de instrucciones, o una de notas,
    queda descartada por el propio lector: tiene otro ancho y otros rótulos.

    Los valores vuelven como TEXTO, tal como se ven en la celda. No es una
    pérdida: todo el lector trabaja sobre el texto crudo —`detectar_estilo`,
    `_decimales_de`, `_leer_multiplicador`— porque de un PDF es lo único que hay,
    y darle floats desde acá sería un segundo camino con otras reglas de
    redondeo. Un «2x19» escrito en una celda de Excel se lee igual que en el
    papel.
    """
    try:
        from openpyxl import load_workbook
    except ImportError as e:  # pragma: no cover - depende de la instalación
        raise ExcelIlegible(
            "EXCEL_UNAVAILABLE",
            "la lectura de archivos de Excel no está instalada en el servidor",
        ) from e

    try:
        # `read_only`: se recorre en streaming, sin construir el libro entero en
        # memoria. `data_only`: de una celda con fórmula se toma el VALOR que
        # Excel dejó calculado, no la fórmula — nadie quiere cargar «=B2*2».
        libro = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:
        raise ExcelIlegible(
            "CORRUPT_EXCEL",
            "el archivo dice ser una planilla de Excel pero no se pudo abrir; puede "
            "estar truncado o ser de otro formato. Volvé a guardarlo como .xlsx e "
            "intentá de nuevo",
        ) from e

    avisos: list[str] = []
    matrices: list[list[list[str | None]]] = []
    celdas_leidas = 0
    con_coma_decimal: list[str] = []
    convertidas: list[str] = []

    try:
        hojas = libro.worksheets[:MAX_HOJAS]
        if len(libro.worksheets) > MAX_HOJAS:
            avisos.append(
                f"el libro tiene {len(libro.worksheets)} hojas y se leyeron las "
                f"primeras {MAX_HOJAS}"
            )

        for hoja in hojas:
            filas: list[list[str | None]] = []
            recortada = False

            for cruda in hoja.iter_rows(values_only=True):
                if len(filas) >= MAX_FILAS:
                    recortada = True
                    break

                celda_ancha = len(cruda) > MAX_COLUMNAS
                valores = [_texto(v) for v in cruda[:MAX_COLUMNAS]]
                if celda_ancha:
                    recortada = True

                # Un número escrito con coma decimal en una celda de TEXTO.
                #
                # Pasa cuando alguien escribe «6,15» en una celda con formato de
                # texto, o lo pega desde otro lugar: Excel no lo guarda como
                # número, lo guarda como esas cuatro letras. El lector lee con
                # punto decimal —es lo correcto para todas las demás celdas, que
                # traen el valor— así que ésta saldría 615.
                #
                # No se corrige: corregirla sería decidir por cuenta propia que
                # la coma es un decimal y no un separador de miles, sobre la
                # única celda del libro que no trae su valor. Se nombra, y quien
                # la escribió la vuelve a escribir.
                #
                # Con una o dos cifras después de la coma no hay nada que
                # decidir: un separador de miles lleva tres. Ésas se leen como
                # decimales y se avisa qué se convirtió. Las de tres cifras
                # —«6,150»— sí son ambiguas y siguen sin tocarse.
                for i, (original, texto) in enumerate(zip(cruda[:MAX_COLUMNAS], valores)):
                    if not isinstance(original, str) or not texto:
                        continue
                    clara = RE_COMA_DECIMAL_CLARA.match(texto.strip())
                    if clara:
                        valores[i] = f"{clara.group(1)}.{clara.group(2)}"
                        convertidas.append(texto.strip())
                    elif RE_COMA_DECIMAL.match(texto):
                        con_coma_decimal.append(texto)

                celdas_leidas += len(valores)
                if celdas_leidas > MAX_CELDAS:
                    raise ExcelIlegible(
                        "EXCEL_TOO_LARGE",
                        f"el archivo tiene más de {MAX_CELDAS} celdas con contenido y no "
                        f"se puede procesar. Dejá en el libro sólo la hoja de la planilla",
                    )

                filas.append(valores)

            if recortada:
                avisos.append(
                    f"la hoja «{hoja.title}» se leyó hasta {MAX_FILAS} filas y "
                    f"{MAX_COLUMNAS} columnas: lo que pase de ahí no entró"
                )

            # Las filas de una hoja no vienen todas del mismo largo, y el lector
            # compara ANCHOS de tabla para saber si una hoja continúa a la otra.
            # Se rellenan al ancho de la hoja para que ese ancho sea uno solo.
            ancho = max((len(f) for f in filas), default=0)
            if ancho == 0:
                continue
            matrices.append([f + [None] * (ancho - len(f)) for f in filas])
    finally:
        # En modo `read_only` el libro deja el archivo abierto: sin esto, el
        # proceso acumula descriptores una petición por vez.
        libro.close()

    if not matrices:
        raise ExcelIlegible(
            "NO_TABLE",
            "el archivo de Excel no tiene ninguna celda con contenido",
        )

    if convertidas:
        muestra = ", ".join(
            f"«{c}» → {c.replace(',', '.')}" for c in sorted(set(convertidas))[:4]
        )
        avisos.append(
            f"{len(convertidas)} celda{'' if len(convertidas) == 1 else 's'} con un número "
            f"escrito como TEXTO con coma decimal se leyeron como decimales ({muestra}). "
            f"Revisá que sean esas medidas"
        )

    if con_coma_decimal:
        muestra = ", ".join(f"«{c}»" for c in sorted(set(con_coma_decimal))[:4])
        avisos.append(
            f"hay {len(con_coma_decimal)} celda{'' if len(con_coma_decimal) == 1 else 's'} "
            f"con un número escrito como TEXTO y con coma decimal ({muestra}): Excel no las "
            f"guardó como número, así que se leen con punto decimal y «6,15» sale 615. "
            f"Volvé a escribirlas con formato de número, o cambiá la coma por un punto"
        )

    return matrices, avisos


def _texto(valor: object) -> str | None:
    """
    Una celda de Excel, como el texto que el lector espera.

    ── Por qué los números no se entregan como números ─────────────────────

    Porque todo el lector lee TEXTO: `detectar_estilo` decide el separador
    decimal del documento mirando cómo están escritas las celdas,
    `_decimales_de` cuenta los decimales del papel para saber cuánta holgura
    dar a una comparación, y `_leer_multiplicador` reconoce «2x19». Un float
    entregado desde acá se saltearía las tres cosas.

    El formato es el que hace que un número vuelva a verse como estaba:
    `2000.0` se escribe «2000» —no «2000.0», que agregaría un decimal que la
    celda no tiene y con él una holgura de comparación más chica de la debida—
    y `10.5` se escribe «10.5». Nunca notación científica, que `numero()` no
    sabría leer.
    """
    if valor is None:
        return None

    if isinstance(valor, bool):
        # Una celda booleana en una planilla de fierros no es un dato: lo más
        # probable es una casilla de verificación. Se entrega el texto y el
        # lector lo descarta como cualquier celda no numérica.
        return "VERDADERO" if valor else "FALSO"

    if isinstance(valor, (datetime, date)):
        # Pasa de verdad: una celda con formato de fecha en la que alguien
        # escribió «10-12» y Excel decidió que era el 10 de diciembre. Se
        # entrega tal como se ve para que quede a la vista de quien revisa, en
        # lugar de convertirse en un número cualquiera.
        return valor.isoformat()

    if isinstance(valor, float):
        if valor != valor or valor in (float("inf"), float("-inf")):
            return None
        if valor.is_integer():
            return str(int(valor))
        # `.10g` corta el ruido de la representación binaria —0.1 + 0.2 guardado
        # por una fórmula— sin recortar ninguna cifra que la celda tenga de
        # verdad, y no usa notación científica en el rango de una planilla.
        return f"{valor:.10g}"

    if isinstance(valor, int):
        return str(valor)

    texto = str(valor).strip()
    return texto or None

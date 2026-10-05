"""
El contrato de salida del worker.

── Qué devuelve, y qué NO devuelve ─────────────────────────────────────────

Devuelve lo que el papel DICE. No devuelve lo que es correcto.

Esa distinción es todo el diseño: el worker es un lector de PDF, no un
participante del dominio. No sabe qué tipos de barra tiene la empresa, no sabe
cuántas barras hacen falta, y no opina sobre si la planilla está bien hecha. Lo
que lee de las columnas de resultado —longitud, barras, peso— viaja en
`ClaimedRow`, con ese nombre, porque Nest lo va a usar para UNA cosa: comparar
contra lo que calcula el motor propio y mostrar las dos cifras cuando no
coinciden.

Si el worker devolviera un `confidence: 0.72` estaría inventando: no tiene con
qué compararse. El motor de NAAU sí.

── Por qué los valores viajan en la unidad leída ───────────────────────────

Porque la conversión de unidad es una decisión revisable por una persona, y
convertir acá la escondería. El worker informa la unidad que detectó y la
evidencia con la que la detectó (`unit`, `unit_evidence`); la pantalla la
muestra y la deja cambiar; Nest convierte a metros al armar el payload, en el
mismo borde donde ya convierte todo lo demás.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Unit = Literal["mm", "cm", "m"]

Source = Literal["pdf", "image", "xlsx"]
"""
De dónde salió la tabla.

No es telemetría: es lo que decide cuánta confianza merece cada número. De un
PDF digital el texto se LEE —las celdas son trazos vectoriales y los caracteres
son caracteres—; de una imagen se RECONOCE, y un dígito mal reconocido no se
distingue a simple vista de uno bien reconocido. La pantalla tiene que poder
decir cuál de las dos cosas pasó.

`xlsx` es el tercer caso y es el más firme de los tres: la celda trae el número
que alguien escribió, sin lectura de ningún tipo en el medio. Lo que puede estar
mal ahí no es la lectura sino el dato —un diámetro que no existe, una medida en
la columna equivocada—, y eso lo atrapan el recálculo y el catálogo, igual que
en una planilla cargada a mano.
"""

# Los campos que el mapeo de columnas sabe reconocer. Son también las claves de
# `ColumnMapping.mapped` y de `ParsedRow.cells`.
FIELDS = (
    "section",
    "code",
    "type_code",
    "diameter",
    "weight_per_unit",
    "quantity",
    "elements",
    "sketch",
    "unit_length",
    "total_length",
    "total_with_loss",
    "bars",
    "total_weight",
)


# ── El contrato de ENTRADA de la exportación ───────────────────────
#
# Todo lo demás de este módulo es lo que el worker DEVUELVE. Esto es lo único
# que RECIBE, y es al revés por un motivo: escribir el Excel de una planilla
# cargada necesita los datos, que son de Nest, y los rótulos de las columnas,
# que son de acá —son los que el lector de este mismo servicio reconoce—.
#
# Así que el cuerpo es SEMÁNTICO y no una tabla ya armada: dice qué papel cumple
# cada número y no en qué columna va. Nest no sabe cómo se rotula «LONG. (m)» y
# no tiene por qué saberlo; acá no se sabe qué es una empresa y tampoco hace
# falta.
#
# Los valores vienen en la unidad que el rótulo va a declarar —`unit`— salvo la
# longitud de corte, que va en METROS siempre. Es la distinción que hace el
# propio papel: «DIMENSIONES (cm.)» sobre las medidas y «LONG. (m.)» sobre el
# desarrollo, porque una medida de figura se toma con cinta y una longitud de
# corte se le pide al corralón.


class ExportRow(BaseModel):
    """Una fila de la planilla, como se carga."""

    pos: int
    type: str = Field(description="El código del tipo: la columna TIPO")
    diameter: float = Field(description="En MILÍMETROS, sea cual sea la designación")
    quantity: int
    elements: int
    measures: dict[str, float] = Field(
        default_factory=dict,
        description="Las medidas por nombre, en `ExportBook.unit`. Vacío si declara su longitud",
    )
    unit_length: float | None = Field(
        default=None,
        description="La longitud de corte DECLARADA, en metros. Nula en el caso normal",
    )


class ExportResult(BaseModel):
    """Lo que el motor calculó para una fila. De lectura: no se vuelve a subir."""

    pos: int
    type: str
    diameter: float
    cut_length: float
    pieces: int
    pieces_per_bar: int
    waste_per_bar: float
    bars: int
    required_weight_kg: float
    bars_weight_kg: float


class ExportBook(BaseModel):
    """El libro a escribir. Ver `xlsx_table.generar_export`."""

    unit: Literal["cm", "m"] = "cm"
    measures: list[str] = Field(
        default_factory=list,
        description="Las medidas que la planilla usa, en el orden de sus columnas",
    )
    rows: list[ExportRow] = Field(default_factory=list)
    results: list[ExportResult] = Field(
        default_factory=list,
        description="Vacío mientras la planilla no se calculó: ahí no se escribe la hoja",
    )


class Dimension(BaseModel):
    """Una medida del croquis, con el nombre que le pone la planilla."""

    name: str = Field(description="La letra de la columna: «a», «b», …")
    value: float = Field(description="El número tal como está escrito, en `ParseResult.unit`")


class ClaimedRow(BaseModel):
    """
    Las columnas de RESULTADO de la fila: lo que la planilla afirma.

    Todo opcional porque una planilla puede venir a medio llenar, y porque una
    columna que no supimos mapear no es un error de la planilla.
    """

    unit_length: float | None = Field(default=None, description="LONG. PARCIAL")
    total_length: float | None = Field(default=None, description="LONG. TOTAL")
    total_with_loss: float | None = Field(default=None, description="LONG. TOTAL + PERDIDA")
    bars: int | None = Field(default=None, description="N. BARRAS")
    weight_per_unit: float | None = Field(
        default=None, description="PESO por unidad de longitud, tal como lo rotula la planilla"
    )
    total_weight: float | None = Field(default=None, description="PESO TOTAL, en kg")


class ParsedRow(BaseModel):
    """Una fila de posición."""

    page: int = Field(description="Página del PDF, 1-based")
    index: int = Field(description="Orden en el documento, 0-based")

    section: str | None = Field(
        default=None,
        description=(
            "La sección estructural, arrastrada desde la celda combinada que la "
            "abre: «REFUERZO LONGITUDINAL», «ESTRIBOS»."
        ),
    )
    code: str = Field(description="El rótulo de la posición tal como está impreso: «P1»")

    type_code: str | None = Field(
        default=None,
        description=(
            "El tipo de figura que el documento DECLARA en su columna «TIPO», tal como "
            "está escrito: «L», «O», «EST-1». Es lo que dice el papel, no un tipo "
            "resuelto: si existe en el catálogo de la empresa y si su fórmula reproduce "
            "la longitud impresa lo decide Nest, que es quien tiene el catálogo."
        ),
    )

    diameter_mm: float | None = None
    quantity: int | None = None

    elements: int | None = Field(
        default=None,
        description=(
            "Cuántas VECES se repite el elemento, cuando la planilla lo separa de "
            "la cantidad. Las piezas totales son `quantity × elements`. Muchas "
            "planillas traen una sola columna —ahí el total ya está en `quantity` y "
            "esto viene en `None`—, pero otras cuentan «2 piezas por grada × 2 "
            "gradas», y sumarlo a la cantidad perdería la distinción que el propio "
            "papel hace."
        ),
    )

    sketch: str | None = Field(
        default=None, description="El texto crudo de la columna del croquis: «a bb c»"
    )
    sketch_letters: list[str] = Field(
        default_factory=list,
        description=(
            "Las letras DISTINTAS del croquis, en orden de aparición. Cuántas hay "
            "es cuántas medidas usa la figura, y es lo que funda la sugerencia de "
            "tipo — ver `sketch_letters` en pdf_parser.py."
        ),
    )

    dimensions: list[Dimension] = Field(default_factory=list)
    claimed: ClaimedRow = Field(default_factory=ClaimedRow)

    cells: dict[str, str] = Field(
        default_factory=dict,
        description="El texto crudo de cada celda mapeada, por nombre de campo",
    )
    unmapped: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Las celdas de columnas que no supimos mapear, por su encabezado crudo. "
            "Van SIEMPRE: un parser calibrado contra un solo archivo sobreajusta, y "
            "lo que no entendió tiene que quedar a la vista de quien revisa."
        ),
    )
    issues: list[str] = Field(
        default_factory=list,
        description="Problemas de LECTURA de esta fila, no de aritmética",
    )


class SummaryLine(BaseModel):
    """Una línea del resumen de material del pie."""

    diameter_mm: float | None = None
    bars: int | None = None
    weight_kg: float | None = None
    length: float | None = None


class ClaimedSummary(BaseModel):
    """
    El resumen de material del pie de la planilla.

    Es la comprobación más valiosa del archivo: son totales que quien hizo la
    planilla sumó a mano y contra los que se puede medir el motor entero, no
    fila por fila.
    """

    lines: list[SummaryLine] = Field(default_factory=list)
    total_length: float | None = None


class ColumnMapping(BaseModel):
    """Cómo se resolvieron las columnas, para que la pantalla lo pueda mostrar."""

    headers_raw: list[str] = Field(default_factory=list)
    mapped: dict[str, int] = Field(
        default_factory=dict, description="Campo → índice de columna"
    )
    dimension_columns: dict[str, int] = Field(
        default_factory=dict, description="Letra de medida → índice de columna"
    )
    unmapped: list[int] = Field(default_factory=list)


class ParseResult(BaseModel):
    pages: int
    source: Source = Field(
        default="pdf", description="De dónde salió la tabla: texto del PDF u OCR de una imagen"
    )
    unit: Unit
    claim_unit: Unit = Field(
        default="cm",
        description=(
            "La unidad de las columnas de LONGITUD declarada por la planilla. Casi "
            "siempre es la misma que `unit`, y no siempre: hay planillas que miden "
            "en centímetros y piden en metros, con las dos unidades rotuladas en el "
            "mismo encabezado. Ver `_resolver_unidades` en pdf_parser.py."
        ),
    )
    unit_evidence: str = Field(
        description="Con qué se decidieron las unidades, en texto legible por una persona"
    )
    columns: ColumnMapping
    rows: list[ParsedRow] = Field(default_factory=list)
    summary: ClaimedSummary | None = None
    warnings: list[str] = Field(
        default_factory=list,
        description="Problemas del DOCUMENTO: sin encabezado, códigos repetidos, páginas descartadas",
    )

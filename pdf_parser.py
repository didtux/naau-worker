"""
Lectura de una planilla de fierros en PDF.

── Por qué esto es Python y no TypeScript ──────────────────────────────────

Porque la tabla de una planilla no es texto: son celdas delimitadas por trazos
vectoriales, y reconstruirlas es agrupar caracteres por sus coordenadas contra
las líneas de regla. `pdfplumber` ya hace exactamente eso, y rehacerlo en Node
sería reimplementar una biblioteca madura. Es la única razón por la que este
proceso existe; todo lo demás del sistema vive en Nest.

── Las tres decisiones que NO se toman con heurísticos ─────────────────────

1. **Las columnas** se mapean por diccionario de sinónimos contra el
   encabezado impreso, nunca por posición fija. Un parser atado al orden de
   columnas de un archivo funciona con ese archivo y con ninguno más. Y lo que
   no se pudo mapear viaja igual, crudo, en `unmapped`.

2. **El estilo numérico** (`1,234.56` contra `1.234,56`) se decide UNA vez para
   todo el documento, buscando una celda que traiga los dos separadores —ahí no
   hay ambigüedad posible— y no celda por celda. Un «1,425» aislado es
   genuinamente ambiguo; el documento completo casi nunca lo es.

3. **La unidad** se toma de lo que el encabezado DECLARA —«a (cm)»— y se
   confirma contra la física: una pieza de fierro tiene que caber en una barra,
   y eso descarta casi siempre dos de las tres lecturas posibles de un número.
   El «valores > 20 son centímetros» que sugería el documento de features no se
   usa, porque una viga de 15 m cargada en metros lo rompe. Cuando ni el
   encabezado lo declara ni la física alcanza para elegir —142 cm y 142 mm son
   las dos piezas reales—, se asume centímetros y se AVISA, porque negarse a
   devolver la planilla por el único dato que se corrige en un clic es peor.

── Lo que este módulo se niega a hacer ─────────────────────────────────────

No interpreta el croquis. Los dibujos son trazos y deducir de ellos la
geometría de doblado sería inventar una instrucción para el armador. Lo que sí
lee son las LETRAS del croquis, que son texto de verdad, y con eso alcanza:
cuántas letras distintas hay es cuántas medidas usa la figura.
"""

from __future__ import annotations

import io
import re
import unicodedata

import pdfplumber

from models import (
    ClaimedRow,
    ClaimedSummary,
    ColumnMapping,
    Dimension,
    ParsedRow,
    ParseResult,
    Source,
    SummaryLine,
    Unit,
)

# ── Topes ──────────────────────────────────────────────────────────────────
#
# Una planilla de un elemento no pasa de un par de páginas. Los topes existen
# para que un archivo hostil no convierta este proceso en un consumidor de
# memoria: `pdfplumber` carga la página entera en objetos.

MAX_PAGES = 20
MAX_ROWS = 500

# Estrategia «lines»: las celdas se delimitan con los trazos de la tabla. Es la
# correcta para una planilla con regla impresa, y falla en seco (0 tablas) si el
# archivo no la tiene — que es mejor que devolver filas inventadas.
TABLE_SETTINGS = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
    "snap_tolerance": 3,
}

# El largo máximo de una barra comercial, y el mínimo con el que una pieza de
# fierro tiene sentido. Son los límites contra los que se confirma la unidad.
MIN_PIEZA_M = 0.02
MAX_PIEZA_M = 24.0

FACTOR_A_METRO: dict[Unit, float] = {"mm": 0.001, "cm": 0.01, "m": 1.0}


# ── Normalización de encabezados ───────────────────────────────────────────


def normalizar(texto: str | None) -> str:
    """
    Un encabezado, reducido a lo que lo identifica.

    Mayúsculas sin tildes, saltos de línea y espacios colapsados, y sin puntos:
    «PESO\\n(kg/cm)» y «Peso (Kg/cm.)» tienen que ser la misma clave, porque en
    dos planillas del mismo estudio están escritos así.
    """
    if texto is None:
        return ""
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn"
    )
    limpio = re.sub(r"[\s.]+", " ", sin_tildes.upper()).strip()
    return limpio


def clave(texto: str | None) -> str:
    """
    Un encabezado, reducido a su clave de diccionario: sin espacios.

    ── Por qué se ignora el espaciado, y por qué eso NO afloja el mapeo ────

    Porque el reconocimiento de caracteres pierde espacios. Medido sobre la
    planilla piloto fotografiada: «PESO TOTAL (Kg)» sale «PESOTOTAL (Kg)» y
    «LONG. TOTAL (cm)» sale «LONG. TOTAL(cm)». Son el mismo encabezado escrito
    igual; lo único que cambió es dónde el detector creyó ver un blanco.

    La comparación sigue siendo por IGUALDAD, que es la decisión de fondo del
    mapeo — ver `SINONIMOS`. Quitar los espacios no habilita ningún «contiene»:
    «PESOTOTAL(KG)» sigue siendo distinto de «PESO», y «LONGTOTAL+PERDIDA» de
    «LONGTOTAL». Dos alias sólo pueden colisionar acá si difieren únicamente en
    el espaciado, y en ese caso son el mismo rótulo.
    """
    return normalizar(texto).replace(" ", "")


# ── Diccionario de sinónimos ───────────────────────────────────────────────
#
# Se compara por IGUALDAD EXACTA del encabezado normalizado, no por «contiene».
#
# Es la decisión de fondo de este mapeo, y es conservadora a propósito. Con
# «contiene», «PESO TOTAL (Kg)» también satisface el alias «PESO», y «LONG.
# TOTAL + PERDIDA» satisface «LONG. TOTAL»: bastaría un orden de campos
# desafortunado para que la planilla se leyera con dos columnas cruzadas y sin
# una sola señal de que pasó. En un documento que termina en fierro cortado, un
# error invisible es mucho peor que una columna sin reconocer.
#
# El precio es que un encabezado con una variante que no está en esta lista NO
# se mapea. Eso no se pierde: cae en `unmapped`, viaja crudo en cada fila y la
# pantalla lo muestra, que es donde una persona lo resuelve en un clic. Y
# soportar una planilla de otro origen es agregar su rótulo acá.
#
# El orden de los campos, entonces, no cambia el resultado: ningún alias figura
# en dos campos, así que no hay dos campos que puedan reclamar la misma columna.

SINONIMOS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "total_with_loss",
        (
            "LONG TOTAL + PERDIDA (CM)",
            "LONG TOTAL + PERDIDA",
            "LONGITUD TOTAL + PERDIDA",
            "TOTAL + PERDIDA",
            "TOTAL CON PERDIDA",
            "CON PERDIDA",
            "LONG TOTAL MAS PERDIDA",
        ),
    ),
    (
        "unit_length",
        (
            "LONG PARCIAL (CM)",
            "LONG PARCIAL",
            "LONGITUD PARCIAL",
            "LONG UNITARIA",
            "LONGITUD UNITARIA",
            "LONG UNIT",
            "L PARCIAL",
            "DESARROLLO",
            "LONG DE CORTE",
            "LONGITUD DE CORTE",
            # «LONG.» a secas, al lado de una «LONG. TOT.», es la longitud de
            # UNA pieza. Se puede agregar porque la comparación es por
            # igualdad: no le roba la columna a «LONG TOTAL», que es otro
            # rótulo. Con un «contiene» sería exactamente al revés.
            "LONG (M)",
            "LONG (CM)",
            "LONG",
        ),
    ),
    (
        "total_length",
        (
            "LONG TOTAL (CM)",
            "LONG TOTAL (M)",
            "LONG TOTAL",
            "LONGITUD TOTAL",
            "LONG TOT (CM)",
            "LONG TOT (M)",
            "LONG TOT",
            "LONGITUD TOT",
            "L TOTAL",
        ),
    ),
    (
        "total_weight",
        (
            "PESO TOTAL (KG)",
            "PESO TOTAL",
            "PESO (KG)",
            "KG TOTAL",
            "PESO KG",
        ),
    ),
    (
        "weight_per_unit",
        (
            "PESO (KG/CM)",
            "PESO (KG/M)",
            "PESO UNITARIO",
            "PESO UNIT",
            "KG/M",
            "KG/CM",
            "PESO",
        ),
    ),
    (
        "bars",
        (
            "N BARRAS",
            "N DE BARRAS",
            "NO BARRAS",
            "NUMERO DE BARRAS",
            "CANT BARRAS",
            "BARRAS",
        ),
    ),
    (
        "diameter",
        (
            "DIAM (MM)",
            "DIAMETRO (MM)",
            "DIAMETRO",
            "DIAM",
            "FIERRO",
            "ACERO",
            "O (MM)",
            # El símbolo de diámetro, solo o con su unidad.
            #
            # Es el rótulo más corto que existe y aun así no es ambiguo:
            # `RE_DIMENSION` sólo reconoce letras de la A a la L, así que
            # ninguno de éstos le puede robar la columna a una medida, y en una
            # planilla de fierros una columna rotulada con un círculo cruzado no
            # es otra cosa que el diámetro.
            #
            # «O» y «0» están porque es lo que el reconocimiento de caracteres
            # DEVUELVE cuando lee «Ø»: medido sobre esta planilla, el glifo sale
            # como «O» con 0,40 de puntaje. Rechazarlo por no ser el carácter
            # exacto dejaría la columna de diámetro sin rótulo y sus valores
            # descartados en silencio — y el diámetro es el dato sin el cual la
            # fila no se puede cargar.
            "Ø",
            "Ø (MM)",
            "O",
            "0",
            "Φ",
            "PHI",
        ),
    ),
    (
        "quantity",
        (
            "NO",
            "N",
            "CANT",
            "CANTIDAD",
            "N PIEZAS",
            "PIEZAS",
            "NO DE PIEZAS",
            "REPETICIONES",
        ),
    ),
    (
        "elements",
        (
            # Cuántas veces se repite el elemento. Va ANTES de `code` en esta
            # tupla sólo por orden de lectura: como ningún alias figura en dos
            # campos, el orden no cambia el resultado — ver `SINONIMOS`.
            "VECES",
            "N VECES",
            "ELEMENTOS",
            "N ELEMENTOS",
            "NO ELEMENTOS",
            "ELEM",
        ),
    ),
    (
        "code",
        (
            "POS",
            "POSICION",
            "ITEM",
            "MARCA",
            "N POS",
        ),
    ),
    (
        # El tipo de figura que el documento DECLARA.
        #
        # Estuvo sin mapear a propósito durante un tiempo, y la razón era buena
        # pero el remedio estaba errado: «TIPO» figuraba entre los sinónimos del
        # CROQUIS, así que en la planilla de obra que trae las dos columnas
        # —«TIPO» con la letra de la figura y «ESQUEMA» con los dibujos— una se
        # quedaba el campo y la otra caía sin reconocer, en silencio. Y peor:
        # `letras_croquis` contaba la «J» como si fuera un tramo, o sea «esta
        # figura usa una medida», cuando la fila trae cuatro.
        #
        # Como campo PROPIO no le roba la columna a nadie y dice algo que vale:
        # el papel nombra la figura. Lo que este lector NO hace es resolverlo
        # —no conoce el catálogo de la empresa— así que viaja crudo y es Nest
        # quien decide. Y decide con el orden correcto: primero cuál de los
        # tipos reproduce la longitud impresa, que es aritmética, y sólo después
        # lo que la columna declara, que es un rótulo. Un papel que dice «O»
        # sobre una fila cuya longitud sale de otra fórmula está declarando algo
        # que sus propios números desmienten.
        "type_code",
        (
            "TIPO",
            "TIPO DE BARRA",
            "TIPO DE FIERRO",
            "COD TIPO",
            "CODIGO DE TIPO",
        ),
    ),
    (
        "sketch",
        (
            "ELEMENTO",
            "CROQUIS",
            "ESQUEMA",
            "FIGURA",
            "DETALLE",
            "FORMA",
            # «TIPO» NO está acá: es su propio campo, `type_code`. En la
            # planilla de obra de 14 columnas están las dos cosas a la vez —una
            # columna «TIPO» con la letra de la figura y una «ESQUEMA» con los
            # dibujos— y con «TIPO» en esta lista el campo se lo quedaba una de
            # las dos y la otra caía sin reconocer, en silencio.
        ),
    ),
    (
        "section",
        (
            "EST",
            "ESTRUCTURA",
            "SECCION",
            "GRUPO",
            "UBICACION",
        ),
    ),
)

# Una columna de medida: la letra sola, con o sin la unidad entre paréntesis.
# `a`, `a (cm)`, `A (CM)`. Se corta en la `l` porque un tipo admite 12 medidas.
RE_DIMENSION = re.compile(r"^([A-L])(?:\s*\([^)]*\))?$")

# `(cm)`, `(m)`, `(mm)` dentro de un encabezado.
#
# Con espacios adentro a propósito. `normalizar` colapsa los puntos a espacios,
# así que un encabezado escrito «DIMENSIONES (cm.)» —con el punto de abreviatura
# adentro del paréntesis, que es como lo escribe medio Bolivia— llega acá como
# «DIMENSIONES (CM )». Sin el `\s*`, ese documento perdía la unidad declarada y
# caía en la plausibilidad física, que acierta casi siempre pero no es lo mismo
# que leer lo que el papel dice.
RE_UNIDAD = re.compile(r"\(\s*(MM|CM|M)\s*\)")


def _problema_de_las_letras(dimension_columns: dict[str, int]) -> str | None:
    """
    Si los rótulos de las medidas no pueden ser los que dice el encabezado.

    Las letras de una planilla rotulan los tramos de una figura, así que son
    SIEMPRE una tira seguida desde «a» y crecen de izquierda a derecha:

        │   DIMENSIONES (cm.)   │        nunca «a c d e», nunca «a d c e»
        │  a │ b │ c │ d │ e    │

    ── Para qué sirve saberlo, y para qué no ──────────────────────────────

    Medido sobre el documento de obra a baja resolución: el «b» del encabezado
    se lee «d», la columna de «b» queda rotulada «d» y la «d» de verdad se
    descarta por repetida.

    Eso NO mueve ninguna medida de lugar, porque las medidas se entregan en
    orden de COLUMNA y no de letra — ver el bucle que las arma. Lo que hace es
    dos cosas: pone un nombre equivocado en un cartel, y pierde la columna cuyo
    rótulo quedó repetido. La segunda la atrapa el recálculo de la fila, porque
    a las medidas les falta un tramo y ya no suman la longitud declarada.

    Así que esto es un aviso y no un rechazo. Rechazar fue lo primero que se
    probó: tiraba planillas enteras —dos fotos degradadas de la piloto que se
    leían bien, y el documento del usuario— por un glifo mal leído.

    Sirve igual a cualquier resolución y también en un PDF, donde una columna
    rotulada de una forma que no entendimos deja el mismo hueco.

    Devuelve la descripción del problema, o `None` si las letras son coherentes.
    """
    if not dimension_columns:
        return None

    # Por índice de columna, que es el orden en que están en el papel.
    letras = [l for l, _ in sorted(dimension_columns.items(), key=lambda kv: kv[1])]
    tira = "".join(letras)
    esperada = "".join(chr(ord("a") + i) for i in range(len(letras)))

    if tira != esperada:
        return f"«{' '.join(letras)}» en lugar de «{' '.join(esperada)}»"
    return None


def mapear_columnas(encabezados: list[str | None]) -> ColumnMapping:
    """
    Resuelve qué columna es qué, a partir de la fila de encabezado.

    Cada columna se queda con el primer campo que la nombra exactamente, y cada
    campo con una sola columna: dos columnas rotuladas «PESO» no pueden ser las
    dos el peso unitario, y la segunda queda en `unmapped` para que se vea.
    """
    crudos = ["" if h is None else str(h) for h in encabezados]
    normalizados = [normalizar(h) for h in crudos]
    # La búsqueda en el diccionario va por `clave` y no por `normalizar`: un
    # encabezado leído de una foto pierde espacios y sigue siendo el mismo.
    claves = [c.replace(" ", "") for c in normalizados]

    mapped: dict[str, int] = {}
    dimension_columns: dict[str, int] = {}
    tomadas: set[int] = set()

    # Las medidas primero: son las únicas que se reconocen por forma y no por
    # sinónimo, y una columna «a» no debe caer en el sinónimo «ACERO».
    for i, norma in enumerate(normalizados):
        m = RE_DIMENSION.match(norma)
        if m:
            letra = m.group(1).lower()
            if letra not in dimension_columns:
                dimension_columns[letra] = i
                tomadas.add(i)

    for campo, alias in SINONIMOS:
        if campo in mapped:
            continue
        for a in alias:
            clave_alias = a.replace(" ", "")
            for i, norma in enumerate(claves):
                if i in tomadas or not norma:
                    continue
                if norma == clave_alias:
                    mapped[campo] = i
                    tomadas.add(i)
                    break
            if campo in mapped:
                break

    unmapped = [i for i in range(len(crudos)) if i not in tomadas and normalizados[i]]

    return ColumnMapping(
        headers_raw=crudos,
        mapped=mapped,
        dimension_columns=dimension_columns,
        unmapped=unmapped,
    )


# ── Números ────────────────────────────────────────────────────────────────

RE_NUMERO = re.compile(r"^[+-]?[\d.,]+$")

#: La evidencia que devuelve `detectar_estilo` cuando NO encontró ninguna.
#:
#: Es una constante y no una frase suelta porque quien llama tiene que poder
#: distinguir «lo detecté» de «lo asumí» sin comparar prosa: de esa distinción
#: depende que se emita el aviso, y un aviso que se apaga porque alguien
#: reescribió una frase es un aviso que no existe.
SIN_EVIDENCIA_DE_ESTILO = "sin evidencia en el documento; se asume «.» decimal"


def detectar_estilo(celdas: list[str]) -> tuple[str, str]:
    """
    El estilo numérico del documento: «us» (1,234.56) o «eu» (1.234,56).

    Se decide con la primera celda NUMÉRICA que traiga los DOS separadores,
    donde el que está más a la derecha es necesariamente el decimal. Una celda
    así resuelve el documento entero, y es lo que evita tener que adivinar en
    cada «6,150».

    ── Por qué sólo mira celdas numéricas ─────────────────────────────────

    Porque el separador decimal es evidencia de los NÚMEROS del documento, y
    antes se miraba cualquier celda. Una celda de prosa con un punto y después
    una coma —«Obra: Torre Sur. Fecha, marzo»— decidía que el documento entero
    estaba en estilo europeo, y a partir de ahí «10.5» se leía 105: la medida
    queda diez veces más grande, verosímil y sin una sola señal.

    Se descubrió con la hoja de instrucciones de la plantilla de Excel, cuya
    primera oración tiene exactamente esa forma. En un PDF hace falta la misma
    casualidad y basta una vez: el título del documento cae en la primera celda
    de la tabla.

    El filtro es el mismo que usa `numero()` para decidir si una celda tiene un
    número, así que lo que funda el estilo es exactamente lo que después se va a
    leer con ese estilo.
    """
    numericas = [t for t in celdas if _parece_numero(t)]

    for texto in numericas:
        s = texto.replace(" ", "")
        coma, punto = s.rfind(","), s.rfind(".")
        if coma >= 0 and punto >= 0:
            if coma > punto:
                return "eu", f"celda «{texto.strip()}» con separador de miles «.» y decimal «,»"
            return "us", f"celda «{texto.strip()}» con separador de miles «,» y decimal «.»"

    # Sin evidencia dura: un separador seguido de exactamente dos dígitos al
    # final es un decimal en cualquiera de los dos estilos.
    for texto in numericas:
        s = texto.replace(" ", "")
        m = re.search(r"([.,])(\d{2})$", s)
        if m and s.count(m.group(1)) == 1:
            return ("eu", f"celda «{texto.strip()}»: decimal «,»") if m.group(1) == "," else (
                "us",
                f"celda «{texto.strip()}»: decimal «.»",
            )

    return "us", SIN_EVIDENCIA_DE_ESTILO


def _parece_numero(texto: str) -> bool:
    """
    ¿Esta celda tiene un número adentro, sea cual sea el estilo?

    Normaliza igual que `numero()` —le saca los espacios y el símbolo de
    diámetro— para que las dos funciones estén de acuerdo sobre qué celda es
    numérica. Si no lo estuvieran, el estilo podría salir de una celda que
    después no se lee como número.
    """
    s = re.sub(r"[\s ]+", "", str(texto)).replace("Ø", "").replace("φ", "")
    return bool(s) and bool(RE_NUMERO.match(s))


def numero(texto: str | None, estilo: str = "us") -> float | None:
    """Un número escrito en una celda, o `None` si la celda no tiene uno."""
    if texto is None:
        return None
    s = re.sub(r"[\s ]+", "", str(texto)).replace("Ø", "").replace("φ", "")
    if not s or s in {"-", "—", "–", "N/A"}:
        return None
    if not RE_NUMERO.match(s):
        return None

    if estilo == "eu":
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")

    try:
        return float(s)
    except ValueError:
        return None


def entero(texto: str | None, estilo: str = "us") -> int | None:
    v = numero(texto, estilo)
    if v is None:
        return None
    return int(round(v))


# ── El croquis ─────────────────────────────────────────────────────────────


def letras_croquis(texto: str | None) -> list[str]:
    """
    Las letras distintas del croquis, en orden de aparición.

    En el archivo piloto la columna del croquis trae «a b c», «a bb c» o
    «d\\ne\\na f c\\nb»: son los rótulos que el dibujo pone sobre cada tramo, y
    son texto de verdad. Contar las DISTINTAS da cuántas medidas usa la figura
    —«a bb c» son tres, aunque la `b` esté dos veces porque el tramo aparece
    dos veces en el dibujo— y ese número es lo que funda la sugerencia de tipo.

    Se descarta todo si aparece una letra fuera de `a`–`l`: entonces la celda no
    trae rótulos sino una palabra («RECTA»), y contar sus letras no significa
    nada.
    """
    if not texto:
        return []
    halladas = re.findall(r"[A-Za-z]", texto)
    if not halladas:
        return []
    if any(c.lower() < "a" or c.lower() > "l" for c in halladas):
        return []

    orden: list[str] = []
    for c in halladas:
        b = c.lower()
        if b not in orden:
            orden.append(b)
    return orden if len(orden) <= 12 else []


RE_PALABRA = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{3,}")

#: Los rótulos de croquis que TAMBIÉN podrían nombrar una sección.
#:
#: «ESQUEMA», «CROQUIS», «FIGURA», «DETALLE» y «FORMA» nombran un dibujo y nada
#: más. «ELEMENTO» es el único de los dos sentidos: en el archivo piloto es la
#: columna del croquis, y en otra planilla puede ser el elemento estructural.
#: Ver `_resolver_croquis_y_seccion`, que es quien desempata y sólo tiene por qué
#: desempatar esto.
ROTULOS_DE_CROQUIS_AMBIGUOS = frozenset({"ELEMENTO"})


def parece_etiqueta(texto: str | None) -> bool:
    """
    ¿Esto es el nombre de algo, o son caracteres sueltos?

    Basta UNA palabra de tres letras o más. «REFUERZO LONGITUDINAL» y
    «ESTRIBOS» la tienen; «b a a a d D b a C a é ? d a b 1C» —lo que devuelve
    el reconocimiento de caracteres cuando se lo pasa por una columna de
    dibujos— no.

    Tres y no cuatro porque hay planillas que rotulan la sección «VIG», «COL» o
    «LOS», y dos dejaría entrar cualquier par de letras pegadas del dibujo.
    """
    return bool(texto) and bool(RE_PALABRA.search(texto or ""))


# ── Texto rotado ───────────────────────────────────────────────────────────


def _celda_rotada(chars: list[dict], bbox: tuple[float, float, float, float] | None) -> bool:
    """
    ¿Los caracteres de esta celda están escritos de costado?

    La columna de sección de una planilla suele llevar el nombre girado 90°
    para que quepa, y `pdfplumber` entrega esos caracteres en el orden en que
    los emite el PDF, que leído como texto horizontal sale al revés
    («LANIDUTIGNOL OZREUFER»). Esto no se detecta comparando el texto contra un
    diccionario: se detecta preguntándole al PDF, que marca cada carácter con
    su `upright`.
    """
    if bbox is None:
        return False
    x0, top, x1, bottom = bbox
    dentro = [
        c
        for c in chars
        if c["x0"] >= x0 - 1 and c["x1"] <= x1 + 1 and c["top"] >= top - 1 and c["bottom"] <= bottom + 1
    ]
    if not dentro:
        return False
    girados = sum(1 for c in dentro if not c.get("upright", True))
    return girados * 2 > len(dentro)


def _desrotar(texto: str) -> str:
    """
    El texto de una celda girada, puesto derecho.

    Girado 90°, el orden de emisión es exactamente el inverso del orden de
    lectura, salto de línea incluido: «LANIDUTIGNOL\\nOZREUFER» invertido es
    «REFUERZO\\nLONGITUDINAL».
    """
    return texto[::-1]


# ── La unidad ──────────────────────────────────────────────────────────────


def _unidad_declarada(columnas: ColumnMapping, indices: list[int]) -> tuple[Unit | None, str]:
    """
    La unidad que el propio encabezado rotula en estas columnas, si lo hace.

    Recibe qué columnas mirar en vez de decidirlo, porque una misma planilla
    puede rotular dos unidades distintas y las dos son ciertas — ver
    `_resolver_unidades`.
    """
    candidatas: list[tuple[str, str]] = []

    for i in indices:
        if i >= len(columnas.headers_raw):
            continue
        crudo = columnas.headers_raw[i]
        m = RE_UNIDAD.search(normalizar(crudo))
        if m:
            candidatas.append((m.group(1).lower(), crudo))

    if not candidatas:
        return None, ""

    unidades = {u for u, _ in candidatas}
    if len(unidades) > 1:
        return None, f"encabezados con unidades distintas: {sorted(unidades)}"

    unidad = candidatas[0][0]
    rotulo = re.sub(r"\s+", " ", candidatas[0][1]).strip()
    return unidad, f"el encabezado «{rotulo}» declara {unidad}"  # type: ignore[return-value]


def _unidad_plausible(largos: list[float]) -> tuple[Unit | None, str]:
    """
    La unidad con la que los largos leídos son piezas de fierro posibles.

    No es el heurístico «> 20 es centímetros»: es la comprobación de que la
    pieza cabe en una barra. De las tres lecturas de «1025», sólo la de
    centímetros (10,25 m) es una pieza real; en metros serían 1.025 m de fierro
    en un tramo y en milímetros 1,025 m con una planilla entera de piezas de un
    metro. Se elige la unidad que hace plausibles más filas.
    """
    if not largos:
        return None, ""

    conteo: dict[Unit, int] = {}
    for unidad, factor in FACTOR_A_METRO.items():
        conteo[unidad] = sum(1 for v in largos if MIN_PIEZA_M <= v * factor <= MAX_PIEZA_M)

    mejor = max(conteo, key=lambda u: conteo[u])
    if conteo[mejor] == 0:
        return None, "ninguna unidad hace que los largos quepan en una barra"

    empates = [u for u, n in conteo.items() if n == conteo[mejor]]
    if len(empates) > 1:
        return None, f"empate de plausibilidad entre {sorted(empates)}"

    return mejor, f"{conteo[mejor]} de {len(largos)} filas dan piezas de largo posible en {mejor}"


# ── El armado ──────────────────────────────────────────────────────────────


def _es_codigo(texto: str | None) -> bool:
    """
    ¿Esta celda es el rótulo de una posición?

    Corta con lo que se cuela por arriba y por abajo de la tabla: el título del
    documento cae en esta misma columna («PLANILLA DE CORTE Y ARMADO…», 70
    caracteres) y la propia fila de encabezado dice «POS.». Un rótulo real es
    corto y lleva un número.
    """
    if not texto:
        return False
    s = re.sub(r"\s+", "", texto)
    return 0 < len(s) <= 12 and bool(re.search(r"\d", s))


class _Tabla:
    """Una tabla del PDF, ya desrotada, con su página."""

    def __init__(self, pagina: int, filas: list[list[str | None]]) -> None:
        self.pagina = pagina
        self.filas = filas

    @property
    def ancho(self) -> int:
        return max((len(f) for f in self.filas), default=0)


def _leer_tablas(paginas: list) -> list[_Tabla]:
    salida: list[_Tabla] = []

    for numero_pagina, pagina in paginas:
        chars = pagina.chars
        for tabla in pagina.find_tables(TABLE_SETTINGS):
            texto = tabla.extract()
            filas: list[list[str | None]] = []

            for r, fila in enumerate(texto):
                nueva: list[str | None] = []
                celdas_bbox = tabla.rows[r].cells if r < len(tabla.rows) else []
                for c, celda in enumerate(fila):
                    bbox = celdas_bbox[c] if c < len(celdas_bbox) else None
                    if celda and _celda_rotada(chars, bbox):
                        nueva.append(_desrotar(celda))
                    else:
                        nueva.append(celda)
                filas.append(nueva)

            salida.append(_Tabla(numero_pagina, filas))

    return salida


# Lo que una planilla escribe en una celda para decir «acá no hay nada»: una
# raya, cuatro rayas, un guión largo, «N/A».
RE_CELDA_VACIA = re.compile(r"^[-–—_.·\s]+$|^N/?A$", re.IGNORECASE)


def _celda(fila: list[str | None], indice: int | None) -> str | None:
    """
    El texto de una celda, o `None` si la celda no dice nada.

    ── Por qué una raya cuenta como nada ──────────────────────────────────

    Porque muchas planillas rellenan las medidas que la figura no usa con
    «——» en vez de dejarlas en blanco, y para quien la hizo las dos cosas
    significan lo mismo.

    Sin esto, cada una de esas rayas salía como un problema de lectura —«la
    medida "c" dice "——" y no es un número»— y una planilla de catorce filas
    con cinco columnas de medida llegaba a la pantalla con treinta problemas
    inventados. El ruido no es gratis: entierra los pocos problemas de verdad.
    """
    if indice is None or indice >= len(fila):
        return None
    valor = fila[indice]
    if valor is None:
        return None
    limpio = re.sub(r"\s+", " ", valor).strip()
    if not limpio or RE_CELDA_VACIA.match(limpio):
        return None
    return limpio


def _buscar_encabezado(tablas: list[_Tabla]) -> tuple[ColumnMapping, int, int] | None:
    """
    La fila de encabezado de la tabla de posiciones.

    Se busca en las primeras filas de cada tabla y no sólo en la primera,
    porque arriba de la tabla suele haber un título a todo el ancho que
    `extract` entrega como una fila más.

    Un encabezado válido tiene que reconocer el rótulo de posición y al menos
    una medida: con eso ya se puede leer una fila, y sin eso lo que se encontró
    no es la tabla de posiciones.

    ── Los encabezados de DOS PISOS ───────────────────────────────────────

    Hay un formato muy común que no entra en una sola fila:

        POS. │ Ø │ TIPO │   DIMENSIONES (cm.)   │ LONG. │ CANT. │ …
             │   │      │  a │ b │ c │ d │ e    │       │       │

    «POS.» está arriba y las letras abajo, así que **ninguna de las dos filas
    tiene las dos cosas** y la búsqueda de una sola pasada las rechaza a las
    dos. El documento entero se caía con NO_HEADER.

    Por eso hay una segunda pasada que prueba pares de filas consecutivas. Va
    SEGUNDA y no mezclada con la primera a propósito: mientras exista una fila
    que sola alcanza, esa gana, y un documento de un solo piso se lee
    exactamente como antes.

    ── Un candidato con las letras incoherentes no gana ───────────────────

    Un encabezado cuyas medidas salen «a d c e» está mal leído, y se devuelve
    sólo si no hay ningún otro: si existe una fila que dice «a b c d e», esa es
    la buena. El incoherente igual se guarda en vez de descartarse, porque
    `_interpretar` lo necesita para poder decir QUÉ leyó en lugar de un
    «no se reconoció el encabezado» que no ayuda a nadie.
    """
    dudoso: tuple[ColumnMapping, int, int] | None = None

    def considerar(columnas: ColumnMapping, t: int, r: int) -> tuple[ColumnMapping, int, int] | None:
        nonlocal dudoso
        if "code" not in columnas.mapped or not columnas.dimension_columns:
            return None
        if _problema_de_las_letras(columnas.dimension_columns) is None:
            return columnas, t, r
        if dudoso is None:
            dudoso = (columnas, t, r)
        return None

    for t, tabla in enumerate(tablas):
        for r, fila in enumerate(tabla.filas[:3]):
            hallazgo = considerar(mapear_columnas(fila), t, r)
            if hallazgo:
                return hallazgo

    for t, tabla in enumerate(tablas):
        for r in range(min(3, len(tabla.filas) - 1)):
            columnas = mapear_columnas(_fusionar(tabla.filas[r], tabla.filas[r + 1]))
            # El índice que se devuelve es el del piso de ABAJO: las filas de
            # datos empiezan después de él, no después del de arriba.
            hallazgo = considerar(columnas, t, r + 1)
            if hallazgo:
                return hallazgo

    return dudoso


def _fusionar(superior: list[str | None], inferior: list[str | None]) -> list[str | None]:
    """
    Dos pisos de encabezado, en uno solo.

    Manda el de abajo, que es el que rotula la columna; el de arriba sólo
    rellena donde abajo no dice nada, que es lo que pasa en «POS.», «CANT.» y
    todas las que ocupan los dos pisos con una celda combinada.

    ── Lo único que se transplanta del piso de arriba ─────────────────────

    La unidad. En «DIMENSIONES (cm.)» sobre «a b c d e», la unidad de las
    medidas está declarada UNA vez, arriba del grupo, y si se descartara el
    piso superior se perdería — la planilla se leería con la unidad adivinada
    por plausibilidad en lugar de con la que el papel declara.

    Se transplanta sólo el paréntesis y no el rótulo entero porque
    «DIMENSIONES (CM) A» no es el nombre de ninguna columna: lo que hace que
    esto funcione es que la fusión produce exactamente lo que la misma planilla
    habría escrito en una sola fila, «a (cm)».

    Una celda combinada deja su texto en la primera columna del grupo y `None`
    en las demás, así que la unidad sólo cae sobre la primera medida. Alcanza:
    la unidad se decide con las que la declaran, no con todas.
    """
    ancho = max(len(superior), len(inferior))
    salida: list[str | None] = []

    for i in range(ancho):
        arriba = superior[i] if i < len(superior) else None
        abajo = inferior[i] if i < len(inferior) else None

        if not (abajo or "").strip():
            salida.append(arriba)
            continue

        unidad = RE_UNIDAD.search(normalizar(arriba)) if arriba else None
        if unidad and not RE_UNIDAD.search(normalizar(abajo)):
            salida.append(f"{abajo} ({unidad.group(1).lower()})")
        else:
            salida.append(abajo)

    return salida


def _resolver_croquis_y_seccion(
    columnas: ColumnMapping, tablas: list[_Tabla], ancho: int
) -> None:
    """
    Desempata «ELEMENTO» por CONTENIDO cuando el encabezado no alcanza.

    En el archivo piloto «ELEMENTO» es la columna del croquis y «EST.» la de la
    sección, pero en otra planilla «ELEMENTO» bien puede ser el elemento
    estructural. El rótulo solo no distingue; el contenido sí: una columna de
    croquis trae rótulos de tramo («a b c»), una de sección trae palabras.

    Si la columna mapeada como croquis no trae rótulos y no hay ninguna de
    sección, se reasigna: es mejor perder el croquis que leer «VIGA 1» como si
    fueran las letras v, i, g, a.

    ── Y si tampoco parece una sección, no es ninguna de las dos ──────────

    Una columna «ESQUEMA» con los DIBUJOS de las figuras es el caso que obligó
    a agregar esa tercera salida. Leída de una imagen, esa celda devuelve las
    letritas que el dibujo tiene encima de cada tramo, mezcladas: «b a a a d D
    b a b a b a C a é ? d a b 1C». No son rótulos de croquis —basta una letra
    fuera de la a–l para que no lo sean— y tampoco son una sección, y sin la
    comprobación se tomaba como sección y se arrastraba ESA cadena a las
    catorce filas de la planilla.

    Una sección se reconoce por lo que es: una ETIQUETA, hecha de palabras
    —«REFUERZO LONGITUDINAL», «ESTRIBOS»—. Una lluvia de caracteres sueltos no
    lo es. Lo que no es ni croquis ni sección queda sin mapear, y así viaja
    crudo a la pantalla entre las columnas no reconocidas, que es exactamente
    lo que hay que hacer con una columna de dibujos: mostrar que está y no
    inventarle un significado.
    """
    indice = columnas.mapped.get("sketch")
    if indice is None:
        return

    con_letras = 0
    total = 0
    celdas: list[str] = []
    for tabla in tablas:
        if tabla.ancho != ancho:
            continue
        for fila in tabla.filas:
            celda = _celda(fila, indice)
            if not celda:
                continue
            total += 1
            celdas.append(celda)
            if letras_croquis(celda):
                con_letras += 1

    if not total or con_letras * 2 > total:
        return

    del columnas.mapped["sketch"]

    # Que pase a SECCIÓN lo decide el ENCABEZADO, no el contenido.
    #
    # Antes lo decidía el contenido: si la celda «parecía una etiqueta» —una
    # palabra de tres letras o más— la columna se reasignaba a sección. Sobre el
    # documento de obra eso falla, y falla de la peor manera. La columna
    # «ESQUEMA» leída de una imagen devuelve los rótulos que cada dibujo tiene
    # encima de sus tramos, y el reconocedor los pega:
    #
    #     «abcd-- a ahc ah- abG- abcde-- ahc. ab»
    #
    # «abcd» y «ahc» son palabras de tres letras o más para cualquier criterio
    # que mire el texto. No hay forma de distinguirlas de «VIG» o «COL», que son
    # secciones de verdad: el contenido no alcanza, y esa cadena terminaba
    # arrastrada como nombre de sección a las catorce filas de la planilla.
    #
    # El encabezado sí alcanza. «ESQUEMA», «CROQUIS», «FIGURA», «DETALLE» y
    # «FORMA» nombran una columna de dibujos y nunca una sección. El único
    # rótulo genuinamente ambiguo es «ELEMENTO» —en el archivo piloto es el
    # croquis, y en otra planilla bien puede ser el elemento estructural—, y es
    # el único para el que esta pregunta tiene sentido.
    if clave(columnas.headers_raw[indice]) in ROTULOS_DE_CROQUIS_AMBIGUOS and any(
        parece_etiqueta(c) for c in celdas
    ):
        columnas.mapped.setdefault("section", indice)
        return

    # Ni croquis ni sección: entonces es una columna que no se supo mapear, y
    # tiene que figurar como tal. Sin esto la columna desaparecía de la
    # pantalla —no mapeada y tampoco listada—, que es la única salida que este
    # parser no se permite: lo que no se entendió queda a la vista.
    if indice not in columnas.unmapped:
        columnas.unmapped.append(indice)
        columnas.unmapped.sort()


#: Los papeles que la aritmética del documento puede confirmar por sí sola.
#:
#: Son los que participan de alguna ecuación de la planilla. El tipo de figura,
#: la sección y el croquis no están: no hay nada contra qué verificarlos, y
#: deducirlos sería adivinar. Ver `column_roles`.
CAMPOS_DEDUCIBLES = ("unit_length", "total_length", "quantity", "elements", "diameter", "code")


def _letras_para(medidas: list[int]) -> dict[str, int]:
    """Las columnas de medida, rotuladas «a», «b», «c»… en orden de columna."""
    return {chr(ord("a") + i): c for i, c in enumerate(medidas)}


def _filas_de_datos(
    tablas: list[_Tabla], ancho: int, tabla_encabezado: int, fila_encabezado: int
) -> list[list[str | None]]:
    """Las filas de la tabla de posiciones, sin los pisos del encabezado."""
    datos: list[list[str | None]] = []
    for t, tabla in enumerate(tablas):
        if tabla.ancho != ancho:
            continue
        for r, fila in enumerate(tabla.filas):
            if t == tabla_encabezado and r <= fila_encabezado:
                continue
            datos.append(fila)
    return datos


def _mapeo_desde_el_contenido(
    tablas: list[_Tabla], estilo: str
) -> tuple[ColumnMapping, int, int, object] | None:
    """
    El mapeo de columnas cuando NO hay encabezado que se pueda leer.

    Es el caso que el diccionario de sinónimos no puede cubrir por definición: un
    rótulo que no está en la lista, o uno que el reconocimiento de caracteres
    rompió lo suficiente para no ser ninguno de los que conoce. Hasta acá el
    documento entero se caía con NO_HEADER.

    Lo que lo resuelve no es una lista más larga sino las cuentas del papel. Ver
    `column_roles`: si existe un reparto de columnas que hace cerrar la suma de
    las medidas contra la longitud y la longitud por la cantidad contra el total,
    en la mayoría de las filas, ése es el reparto — y lo afirma el documento.

    Las medidas se rotulan «a», «b», «c»… en orden de columna, que es lo que
    habría hecho la planilla: son los tramos de la figura, uno detrás del otro.
    """
    from column_roles import inferir

    # La más ancha primero, y a igual ancho la que más filas trae: la tabla de
    # posiciones es la grande, y el resumen del pie es angosto.
    orden = sorted(range(len(tablas)), key=lambda t: (tablas[t].ancho, len(tablas[t].filas)), reverse=True)

    for t in orden:
        tabla = tablas[t]
        if tabla.ancho < 3:
            continue
        inferencia = inferir(tabla.filas, estilo)
        if inferencia is None or not inferencia.creible:
            continue
        if "code" not in inferencia.roles or not inferencia.medidas:
            continue

        mapped = {c: i for c, i in inferencia.roles.items() if c != "code"}
        mapped["code"] = inferencia.roles["code"]
        letras = _letras_para(inferencia.medidas)
        tomadas = {*mapped.values(), *letras.values()}

        columnas = ColumnMapping(
            headers_raw=[""] * tabla.ancho,
            mapped=mapped,
            dimension_columns=letras,
            unmapped=[i for i in range(tabla.ancho) if i not in tomadas],
        )

        # Dónde terminan los rótulos y empiezan los datos.
        #
        # No se puede dejar que lo decida `_es_codigo`, que es lo único que filtra
        # las filas cuando hay encabezado: un rótulo destrozado como «P0S» pasa
        # por código de posición y entra como una fila más. Lo que sí distingue
        # una fila de datos es que traiga un NÚMERO donde va la longitud —un
        # rótulo, por mal leído que esté, no lo trae—, así que el encabezado
        # termina justo antes de la primera que lo tiene.
        i_largo = inferencia.roles["unit_length"]
        primera = next(
            (r for r, fila in enumerate(tabla.filas) if numero(_celda(fila, i_largo), estilo) is not None),
            0,
        )
        return columnas, t, primera - 1, inferencia

    return None


def _completar_con_inferencia(
    columnas: ColumnMapping,
    tablas: list[_Tabla],
    ancho: int,
    tabla_encabezado: int,
    fila_encabezado: int,
    estilo: str,
    warnings: list[str],
) -> None:
    """
    Los papeles que el encabezado no dio, deducidos de las cuentas del papel.

    ── Por qué el encabezado sigue mandando ───────────────────────────────

    Porque es lo que una persona ESCRIBIÓ. La deducción se usa para rellenar lo
    que falta, no para corregir lo que el papel dice: si el rótulo dice «LONG.
    TOT.» y la aritmética señala otra columna, el que puede estar mal leído es
    cualquiera de los dos, y cambiar en silencio lo que el documento declara es
    exactamente la clase de decisión invisible que este lector no toma. Se avisa
    y se deja el rótulo.

    Lo que sí hace, y es lo que pedía el documento de obra de 14 columnas leído
    de una foto: si «PESO +7%» no está en el diccionario y «CANT.» se leyó
    «CAMT», las columnas que sostienen el recálculo aparecen igual.
    """
    from column_roles import inferir

    faltan = [c for c in CAMPOS_DEDUCIBLES if c not in columnas.mapped]

    # Los rótulos corridos son señal de que puede FALTAR una columna de medida,
    # no sólo de que un nombre esté mal: «d» leída «e» descarta la «e» de verdad
    # por repetida. Así que también ahí vale preguntarle a la aritmética, aunque
    # el encabezado haya nombrado todos los demás campos.
    letras_dudosas = _problema_de_las_letras(columnas.dimension_columns) is not None

    if not faltan and columnas.dimension_columns and not letras_dudosas:
        return

    tomadas = {*columnas.mapped.values(), *columnas.dimension_columns.values()}

    # ── Si el encabezado nombró TODAS las columnas, no hay nada que deducir ──
    #
    # Y no es sólo ahorrarse la búsqueda: sin ninguna columna libre, lo único
    # que la inferencia puede producir es un desacuerdo, y un desacuerdo sobre
    # columnas que todas tienen rótulo impreso es la evidencia más débil que
    # existe.
    #
    # Medido en la plantilla de Excel, que no lleva columna de longitud: con
    # cuatro filas de números de una y dos cifras, la búsqueda «encontró» que la
    # suma de POS + Ø + c da la columna VECES por diez en 3 de 4 filas. Es una
    # casualidad aritmética, no una lectura, y llegaba a la pantalla como
    # «el encabezado y las cuentas no coinciden en code» sobre una planilla
    # perfectamente leída.
    if all(i in tomadas for i in range(ancho)):
        return

    datos = _filas_de_datos(tablas, ancho, tabla_encabezado, fila_encabezado)
    inferencia = inferir(datos, estilo)
    if inferencia is None or not inferencia.creible:
        return

    # ── El oráculo de la inferencia no puede contradecir un rótulo ──────────
    #
    # Toda la inferencia se apoya en UNA ecuación: la suma de las medidas da la
    # longitud. Si la columna que hace de longitud es una que el encabezado
    # nombró como otra cosa —VECES, CANT., POS.—, entonces la ecuación que
    # sostiene todo lo demás está apoyada en una columna que el papel dice que
    # es otra. Ahí no hay dos lecturas para comparar: hay una casualidad.
    #
    # Cuando el encabezado nombró bien la longitud, la aritmética la señala a
    # ELLA —es el caso medido en el documento de obra de 14 columnas— así que
    # esta guarda no cuesta nada donde la inferencia sirve.
    nombrado = {indice: campo for campo, indice in columnas.mapped.items()}
    i_oraculo = inferencia.roles.get("unit_length")
    if i_oraculo is not None and nombrado.get(i_oraculo, "unit_length") != "unit_length":
        return

    puestos: list[str] = []
    medidas_recuperadas = ""

    for campo in faltan:
        indice = inferencia.roles.get(campo)
        if indice is None or indice in tomadas:
            continue
        columnas.mapped[campo] = indice
        tomadas.add(indice)
        puestos.append(campo)

    if inferencia.medidas:
        del_rotulo = set(columnas.dimension_columns.values())
        # Las que la aritmética encontró y el rótulo no nombró, si no se las
        # quedó otro campo.
        agregadas = [c for c in inferencia.medidas if c not in del_rotulo and c not in tomadas]

        if not del_rotulo:
            columnas.dimension_columns.update(_letras_para(agregadas))
            tomadas.update(agregadas)
            medidas_recuperadas = f" Y las {len(agregadas)} columnas de medida."

        elif agregadas:
            # ── Cuando la aritmética encuentra MÁS medidas que el rótulo ────
            #
            # Es el caso del documento de obra leído a 38 px por columna: el
            # rótulo «d» se leyó «e», así que la columna de «d» quedó etiquetada
            # «e», la «e» de verdad se descartó por repetida, y el encabezado
            # entregó cuatro columnas de medida donde el papel tiene cinco. Las
            # filas de 4 y 5 medidas llegaban a la pantalla con 3 y 4.
            #
            # La aritmética, en cambio, encontró las cinco: su suma da la
            # longitud declarada en 12 de 13 filas. Entre un rótulo que se sabe
            # mal leído —lo dice `_problema_de_las_letras`— y una ecuación que
            # cierra en doce filas, la ecuación es mejor evidencia.
            #
            # Se re-rotulan TODAS de nuevo, a, b, c… en orden de columna, y no
            # sólo las que faltaban: con un rótulo corrido, conservar los
            # nombres viejos dejaría dos columnas llamándose «e». El orden de
            # columna es el del papel, que es lo que la figura necesita.
            completas = sorted(del_rotulo | set(agregadas))
            columnas.dimension_columns.clear()
            columnas.dimension_columns.update(_letras_para(completas))
            tomadas.update(completas)
            medidas_recuperadas = (
                f" Y {len(agregadas)} columna{'' if len(agregadas) == 1 else 's'} de medida "
                f"que el encabezado no nombró: van {len(completas)} en total, rotuladas de "
                f"nuevo «a», «b», «c»… en orden de columna."
            )

    # Lo que se reclamó deja de estar «sin reconocer»: si siguiera en las dos
    # listas, la pantalla mostraría la misma columna como reconocida y como no.
    if puestos or medidas_recuperadas:
        columnas.unmapped[:] = [i for i in columnas.unmapped if i not in tomadas]
        campos = f" ({', '.join(puestos)})" if puestos else ""
        warnings.append(
            "hubo columnas que el encabezado no nombró y se deducieron de las cuentas del "
            f"propio documento{campos}."
            + medidas_recuperadas
            + " La evidencia: "
            + "; ".join(inferencia.evidencia)
            + ". Revisalas: lo dedujo la aritmética de la planilla, no un rótulo"
        )

    discrepancias = [
        campo
        for campo in CAMPOS_DEDUCIBLES
        if campo in columnas.mapped
        and campo in inferencia.roles
        and columnas.mapped[campo] != inferencia.roles[campo]
        and campo not in puestos
    ]
    if discrepancias:
        warnings.append(
            "el encabezado y las cuentas del documento no coinciden en "
            f"{', '.join(discrepancias)}: se respeta el rótulo impreso, pero uno de los "
            "dos se leyó mal y conviene revisar esas columnas"
        )


#: Las razones de unidad posibles entre dos columnas de la misma planilla.
#:
#: Existen porque hay planillas que usan dos unidades a la vez y tienen razón:
#: «DIMENSIONES (cm.)» sobre las medidas y «LONG. (m.)» al lado, con 15 + 90 +
#: 435 + 45 = 585 cm = 5,85 m.
RAZONES_DE_UNIDAD = (1.0, 100.0, 10.0, 1000.0, 0.01, 0.1, 0.001)


def _decimales_de(texto: str | None) -> int:
    """
    Con cuántos decimales está escrito un número en el papel.

    Es lo que dice cuánto puede estar redondeado, y de ahí sale la holgura de
    cualquier comprobación que lo use. Se lee del texto y no del `float`, porque
    «5.80» y «5.8» son el mismo número y no el mismo redondeo.
    """
    if not texto:
        return 0
    limpio = str(texto).strip()
    for separador in (".", ","):
        if separador in limpio:
            cola = limpio.rsplit(separador, 1)[1]
            if cola.isdigit():
                return len(cola)
    return 0


def _largo_corroborado(dimensiones: list[Dimension], largo: float) -> bool:
    """
    Si la suma de las medidas confirma la longitud declarada de la pieza.

    Es la condición que hace que deducir sea legítimo y no una cadena de
    suposiciones: la longitud se usa para despejar la cantidad, así que tiene que
    estar respaldada por una ecuación DISTINTA de la que se va a despejar.

    No hace falta saber la unidad: si la suma de las medidas dividida por la
    longitud es una de las razones que separan milímetros, centímetros y metros,
    las dos columnas dicen lo mismo en dos unidades. Cuál es cuál lo resuelve
    después `_resolver_unidades`, con el documento completo a la vista.
    """
    if largo <= 0 or not dimensiones:
        return False
    suma = sum(d.value for d in dimensiones)
    if suma <= 0:
        return False
    razon = suma / largo
    return any(abs(razon - r) <= max(0.02 * r, 0.02) for r in RAZONES_DE_UNIDAD)


def _deducir_piezas(
    dimensiones: list[Dimension],
    texto_largo: str | None,
    largo: float | None,
    texto_total: str | None,
    total: float | None,
    cantidad: int | None,
    elementos: int | None,
    *,
    hay_columna_de_cantidad: bool,
    hay_columna_de_veces: bool,
) -> tuple[int | None, int | None, str | None]:
    """
    La cantidad de piezas que el documento DETERMINA, cuando no se pudo leer.

    ── El problema que resuelve ───────────────────────────────────────────

    En la planilla de obra leída de una foto, seis de las catorce filas llegaban
    a la pantalla con «la fila no trae cantidad de piezas» y quedaban
    bloqueadas. La columna «CANT.» es angosta y sus números tienen una o dos
    cifras, así que es la primera que se pierde a baja resolución.

    Pero la cantidad no hay que adivinarla. El papel afirma

        longitud total = longitud × cantidad × veces

    y de esos cuatro números leyó tres. El que falta está despejado:
    46,00 ÷ 5,75 = 8. No es una heurística, es la ecuación del documento.

    ── Las dos condiciones, y por qué las dos ─────────────────────────────

    1. La longitud tiene que estar CORROBORADA por la suma de las medidas. Si se
       despejara la cantidad de una longitud que a su vez puede estar mal leída,
       el resultado sería una cadena de suposiciones con aspecto de dato. Con la
       suma de las medidas cerrando, la longitud tiene un respaldo independiente.

    2. El resultado tiene que ser un ENTERO, y la cuenta tiene que cerrar hacia
       atrás con la holgura del redondeo del papel. Las piezas se cuentan de una
       en una: un cociente de 7,4 no es una cantidad que no se leyó, es una
       señal de que alguno de los otros números está mal.

    Lo que esto SÍ cuesta, y hay que decirlo: la fila pierde su verificación
    cruzada. Con la cantidad deducida de esa ecuación, la ecuación ya no
    comprueba nada — queda el entero y el redondeo, que es menos. Por eso vuelve
    con una explicación que va a la pantalla, para que se confirme contra el
    papel. Sigue siendo mucho mejor que bloquear una fila cuyo dato el propio
    documento determina.
    """
    if largo is None or total is None or largo <= 0:
        return cantidad, elementos, None

    # ── Sólo se despeja un campo cuya COLUMNA existe ───────────────────────
    #
    # Una planilla sin columna de «VECES» no tiene las veces sin leer: no las
    # tiene, y el modelo ya las cuenta como una. Despejarlas igual y anunciarlo
    # le ponía un aviso a cada una de las 27 filas de la planilla piloto — una
    # deducción correcta, inútil y ruidosa, que es su propia clase de error: el
    # ruido entierra los avisos que sí importan.
    falta_cantidad = cantidad is None and hay_columna_de_cantidad
    falta_veces = elementos is None and hay_columna_de_veces and cantidad is not None
    if not (falta_cantidad or falta_veces):
        return cantidad, elementos, None

    if not _largo_corroborado(dimensiones, largo):
        return cantidad, elementos, None

    # Con las dos sin leer, las veces se toman como una —que es el valor por
    # omisión del modelo— y lo que se despeja es la cantidad.
    conocido = elementos if falta_cantidad else cantidad
    veces = conocido if conocido is not None else 1
    if veces <= 0:
        return cantidad, elementos, None

    candidato = total / (largo * veces)
    n = round(candidato)
    if n < 1:
        return cantidad, elementos, None

    # La holgura, del redondeo del papel: el total está escrito con sus
    # decimales y arrastra además el de la longitud multiplicado por las piezas.
    holgura = 0.5 * 10.0 ** -_decimales_de(texto_total) + (
        0.5 * 10.0 ** -_decimales_de(texto_largo)
    ) * n * veces
    if abs(total - largo * n * veces) > holgura + 1e-9:
        return cantidad, elementos, None

    if falta_cantidad:
        # Cuando faltaban las DOS, lo que queda determinado es el producto y no
        # el reparto: 4 piezas × 1 vez y 2 × 2 dan las mismas cuatro piezas y el
        # mismo acero. Se dice, porque en la pantalla se va a ver un reparto que
        # el papel puede escribir de otra forma.
        if elementos is None and hay_columna_de_veces:
            cuenta = f"{total:g} ÷ {largo:g} = {n}"
            supuesto = (
                " —las veces tampoco se leyeron y se tomaron como 1: lo que la planilla "
                "determina es el total de piezas, no cómo se reparte—"
            )
        else:
            divisor = f" ÷ {veces:g}" if veces != 1 else ""
            cuenta = f"{total:g} ÷ {largo:g}{divisor} = {n}"
            supuesto = ""
        return n, elementos, (
            f"la cantidad de piezas no se pudo leer y se dedujo de la propia planilla "
            f"({cuenta}){supuesto}: confirmala contra el papel"
        )
    return cantidad, n, (
        f"las veces que se repite la pieza no se pudieron leer y se dedujeron de la "
        f"propia planilla ({total:g} ÷ {largo:g} ÷ {cantidad:g} = {n}): confirmalas "
        f"contra el papel"
    )


#: «2x24»: dos tramos de veinticuatro.
#:
#: La `x` se acepta en sus variantes porque el reconocimiento la devuelve como
#: equis mayúscula, por el signo de multiplicar o por un asterisco. Los dos
#: puntos NO están, y es a propósito: en la planilla del usuario «2x24» salió
#: «2:04», o sea que el reconocedor se equivocó en la equis Y en un dígito.
#: Aceptar los dos puntos daría «2 tramos de 4» con aire de dato leído.
RE_MULTIPLICADOR = re.compile(r"^(\d{1,2})\s*[xX×*]\s*([\d.,]+)$")


def _leer_multiplicador(crudo: str, estilo: str) -> tuple[int, float] | None:
    """Cuántos tramos y de qué medida, si la celda viene en notación «2x24»."""
    m = RE_MULTIPLICADOR.match(crudo.strip())
    if m is None:
        return None
    veces = int(m.group(1))
    medida = numero(m.group(2), estilo)
    if medida is None or veces < 1:
        return None
    return veces, medida


#: Los caracteres que el reconocedor confunde con dígitos, y con qué.
#:
#: No es una lista de buenas intenciones: cada entrada es una confusión que se
#: ve de verdad en una planilla fotografiada, porque los glifos se parecen a esa
#: resolución. La `x` del multiplicador entra acá por el mismo motivo — en la
#: foto del documento de obra «2x24» salió «2:24».
CONFUSIONES = {
    "O": "0", "o": "0", "Q": "0", "D": "0",
    "l": "1", "I": "1", "|": "1", "i": "1",
    "Z": "2", "z": "2",
    "S": "5", "s": "5",
    "G": "6",
    "T": "7",
    "B": "8",
    "g": "9", "q": "9",
    ":": "x", ";": "x",
}

TABLA_DE_CONFUSIONES = str.maketrans(CONFUSIONES)


def _relecturas_de(crudo: str) -> list[str]:
    """
    Las relecturas plausibles de una celda que no se pudo leer como número.

    Una sola: la que sale de cambiar TODOS los caracteres confundibles por su
    dígito. Probar las combinaciones de a una multiplicaría los candidatos y con
    ellos la chance de que alguno cierre por casualidad, y lo que hace que esto
    sea seguro es justamente que hay muy pocos candidatos y un juez estricto.
    """
    reparado = crudo.translate(TABLA_DE_CONFUSIONES)
    return [reparado] if reparado != crudo else []


def _reparar_medida(
    crudo: str,
    estilo: str,
    suma_del_resto: float,
    largo: float,
) -> tuple[str, float, tuple[int, float] | None] | None:
    """
    Una celda de medida mal leída, reparada — si la suma del papel lo confirma.

    ── Por qué esto no es adivinar un dígito ──────────────────────────────

    Porque no lo decide el parecido de los glifos: lo decide la aritmética de la
    fila. Se propone la relectura y se acepta SÓLO si con ella la suma de las
    medidas da la longitud que la planilla declara.

    El caso que lo motivó, con los dos resultados. En la foto del documento de
    obra la celda «2x24» salió una vez «2:04» y otra «2:24»:

        «2:24» → 2×24 = 48,  48 + 210 + 2 = 260 = los 2,60 m declarados → SE ACEPTA
        «2:04» → 2×4  =  8,   8 + 210 + 2 = 220 ≠ 260                   → SE RECHAZA

    Es exactamente la distinción que hacía falta: en el primero el reconocedor se
    equivocó sólo en la equis, y en el segundo también en un dígito. Sin el juez,
    aceptar los dos puntos habría metido «2 tramos de 4» con aire de dato leído;
    con el juez, la planilla desmiente el que está mal.

    Devuelve `(texto_reparado, contribución, multiplicador)` o `None`.
    """
    for candidato in _relecturas_de(crudo):
        multiplicador = _leer_multiplicador(candidato, estilo)
        if multiplicador is not None:
            veces, medida = multiplicador
            contribucion = veces * medida
        else:
            valor = numero(candidato, estilo)
            if valor is None or valor <= 0:
                continue
            contribucion = valor

        total = suma_del_resto + contribucion
        razon = total / largo if largo > 0 else 0.0
        if any(abs(razon - r) <= max(0.012 * r, 0.012) for r in RAZONES_DE_UNIDAD):
            return candidato, contribucion, multiplicador

    return None


def _aviso_de_multiplicadores(
    con_multiplicador: list[tuple[str, int, float]],
    dimensiones: list[Dimension],
    largo: float | None,
    celdas_sin_leer: int = 0,
) -> str:
    """
    Qué decir de una fila cuyas medidas vienen «2x24», y qué medida se guarda.

    ── El multiplicador es de la FÓRMULA, no del dato ─────────────────────

    Esto se leyó mal dos veces antes de mirar los números. «2x24» parecía «dos
    tramos de 24» y la pregunta parecía ser en qué ORDEN van esos tramos —24,
    105, 24, 105 o 24, 24, 105, 105— que la suma no puede responder.

    La pregunta no existe. En un estribo cerrado el lado ES 24, y el «×2» lo
    pone la figura: el tipo «O» del sistema tiene fórmula `2*(a + b) + c`. El
    papel escribe en la celda lo que la fórmula va a multiplicar, y lo que hay
    que guardar es 24 — no 48. El orden lo sabe la fórmula, que es su trabajo.

    Las dos filas del documento de obra lo confirman contra su longitud
    declarada:

        fila  3:  2×24 + 2×105 + 2×1 = 260 cm = los 2,60 m declarados
        fila 10:  2×19 + 2×19  + 2×2 =  80 cm = los 0,80 m declarados

    ── Lo que sí queda pendiente, y es del catálogo ───────────────────────

    Que la fórmula del papel no es la del tipo «O» de NAAU. Las dos cuentas de
    arriba multiplican el gancho por dos —el estribo del papel cierra con dos
    ganchos— y `2*(a + b) + c` lo suma una vez: da 259 y 78 contra 260 y 80. La
    diferencia es de UN centímetro y de un gancho, y es real.

    Así que las medidas se entregan, el motor busca un tipo cuya fórmula dé la
    longitud declarada, y si no lo hay la fila queda bloqueada pidiendo lo que
    de verdad falta: un tipo de la empresa con fórmula `2*(a + b + c)`. Uno
    solo, y las dos filas entran.
    """
    detalle = ", ".join(f"«{letra}» = {n}×{medida:g}" for letra, n, medida in con_multiplicador)
    # La suma que verifica es la del PAPEL —N×M— aunque la medida que se guarde
    # sea M: es la única forma de confirmar que la celda se leyó bien.
    suma = sum(n * medida for _, n, medida in con_multiplicador) + sum(
        d.value for d in dimensiones if d.name not in {l for l, _, _ in con_multiplicador}
    )

    cuenta = f"Sumadas dan {suma:g}"
    if largo is not None and largo > 0:
        razon = suma / largo
        if any(abs(razon - r) <= max(0.02 * r, 0.02) for r in RAZONES_DE_UNIDAD):
            cuenta += f" y la planilla declara {largo:g}, así que la fila es coherente"
        elif celdas_sin_leer:
            # Decir «no coincide» acá sería culpar a la notación de un problema
            # que es de resolución: a esta fila le falta una celda, así que la
            # suma NO PUEDE dar. Es la diferencia entre «la planilla está mal» y
            # «la imagen no alcanzó», y el usuario necesita saber cuál de las dos.
            cuenta += (
                f" y la planilla declara {largo:g}, pero a esta fila le "
                f"{'falta' if celdas_sin_leer == 1 else 'faltan'} "
                f"{celdas_sin_leer} celda{'' if celdas_sin_leer == 1 else 's'} sin leer, "
                f"así que la suma no puede dar"
            )
        else:
            cuenta += f" y la planilla declara {largo:g}, que no coincide"

    # El ejemplo, con los números DE ESTA FILA cuando alcanzan: explicar la
    # ambigüedad con «2x24 2x105» en una fila que dice 2x19 obliga a quien lee a
    # traducir el ejemplo a su propio caso.
    return (
        f"las medidas vienen con multiplicador ({detalle}): el «×N» es de la FÓRMULA de "
        f"la figura y no del dato, así que se guardó la medida sola —en un estribo el lado "
        f"es {con_multiplicador[0][2]:g} y el «×{con_multiplicador[0][1]}» lo pone el tipo—. "
        f"{cuenta}. Si ningún tipo del catálogo da esa longitud, el que falta es un estribo "
        f"con fórmula «2*(a + b + c)»: el del sistema suma el gancho una vez y este papel "
        f"lo cuenta dos"
    )


def _leer_resumen(tablas: list[_Tabla], estilo: str, ancho_posiciones: int) -> ClaimedSummary | None:
    """
    El resumen de material del pie.

    Es una tabla aparte, angosta, con una línea por diámetro. Vale más que
    cualquier verificación fila por fila: son los totales que quien hizo la
    planilla sumó, y contra ellos se puede medir el motor completo.
    """
    for tabla in tablas:
        if tabla.ancho == ancho_posiciones or tabla.ancho > 6:
            continue

        for r, fila in enumerate(tabla.filas[:3]):
            columnas = mapear_columnas(fila)
            if "diameter" not in columnas.mapped or "bars" not in columnas.mapped:
                continue

            i_diam = columnas.mapped["diameter"]
            i_barras = columnas.mapped["bars"]
            # El resumen tiene su propio vocabulario, más corto que el de las
            # filas: sus columnas dicen «PESO» y «LONG.» a secas porque el
            # contexto ya las desambigua. En la tabla de posiciones, en cambio,
            # «PESO» a secas es el peso unitario y hay otra columna «PESO
            # TOTAL» — así que el diccionario de las filas no puede aflojar y
            # el desempate se hace acá.
            i_peso = columnas.mapped.get("total_weight", columnas.mapped.get("weight_per_unit"))
            i_largo = columnas.mapped.get(
                "total_length",
                columnas.mapped.get("total_with_loss", columnas.mapped.get("unit_length")),
            )
            if i_largo is None:
                i_largo = next(
                    (i for i in columnas.unmapped if normalizar(columnas.headers_raw[i]).startswith("LONG")),
                    None,
                )

            lineas: list[SummaryLine] = []
            total_largo: float | None = None

            for datos in tabla.filas[r + 1 :]:
                diam = numero(_celda(datos, i_diam), estilo)
                if diam is not None:
                    lineas.append(
                        SummaryLine(
                            diameter_mm=diam,
                            bars=entero(_celda(datos, i_barras), estilo),
                            weight_kg=numero(_celda(datos, i_peso), estilo),
                            length=numero(_celda(datos, i_largo), estilo),
                        )
                    )
                    continue

                # La fila del gran total: sin diámetro, con la palabra TOTAL en
                # alguna celda y un número en otra.
                textos = [_celda(datos, i) or "" for i in range(len(datos))]
                if any("TOTAL" in normalizar(t) for t in textos):
                    numeros = [numero(t, estilo) for t in textos]
                    presentes = [n for n in numeros if n is not None]
                    if presentes:
                        total_largo = presentes[-1]

            if lineas:
                return ClaimedSummary(lines=lineas, total_length=total_largo)

    return None


def parse_pdf(data: bytes, max_pages: int = MAX_PAGES) -> ParseResult:
    """Lee una planilla y devuelve lo que dice, sin corregirla."""
    warnings: list[str] = []

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            total_paginas = len(pdf.pages)
            if total_paginas > max_pages:
                warnings.append(
                    f"el archivo tiene {total_paginas} páginas y se leyeron las primeras {max_pages}"
                )
            paginas = [(n, p) for n, p in enumerate(pdf.pages[:max_pages], start=1)]
            tablas = _leer_tablas(paginas)
    except ParseError:
        raise
    except Exception as e:
        # `pdfminer` levanta su propia jerarquía de errores y no vale exponerla:
        # lo que Nest necesita saber es «este archivo no se puede abrir», con un
        # código estable. Si el detalle se filtrara al cliente, el mensaje
        # cambiaría con cada versión de la biblioteca.
        raise CorruptPdfError(
            "el archivo dice ser un PDF pero no se pudo abrir; puede estar truncado o "
            "mal generado. Volvé a exportarlo e intentá de nuevo."
        ) from e

    if not tablas:
        raise NoTableError(
            "no se encontró ninguna tabla con líneas de regla; el archivo puede ser un "
            "escaneo o tener la planilla sin recuadrar. Si lo que tienes es un escaneo o "
            "una foto, sube la imagen: ésa sí se lee con reconocimiento de caracteres."
        )

    return _interpretar(tablas, total_paginas, warnings, source="pdf")


def parse_imagen(data: bytes) -> ParseResult:
    """
    Lee una planilla FOTOGRAFIADA o ESCANEADA.

    La imagen se convierte en las mismas matrices de celdas que entrega
    `pdfplumber` y de ahí en adelante corre exactamente el mismo código. Es la
    razón por la que esta función es tan corta: el mapeo de columnas, la unidad,
    el estilo numérico y la lectura de filas no saben de dónde salió la tabla, y
    no tienen por qué.

    Lo único que cambia es que la lectura ahora es FALIBLE por naturaleza, y eso
    viaja: `source="image"` en el resultado y un aviso en primer lugar.
    """
    # Diferido a propósito: cargar OpenCV y el motor ONNX son unos segundos y
    # unos cien megas, y un worker que sólo recibe PDFs no los tiene que pagar.
    from image_table import ImagenIlegible, tablas_de_imagen

    from image_table import PX_POR_COLUMNA_MINIMO

    try:
        matrices, avisos, px_columna = tablas_de_imagen(data)
    except ImagenIlegible as e:
        raise _ERRORES_DE_IMAGEN.get(e.code, ParseError)(str(e)) from e

    # Una imagen es una página: no hay dónde ponerle más.
    tablas = [_Tabla(1, filas) for filas in matrices]
    if not tablas:
        raise NoTableError(
            "no se encontró ninguna tabla con líneas de regla en la imagen; la foto "
            "tiene que mostrar la planilla recuadrada, de frente y completa"
        )

    leido = _interpretar(tablas, 1, list(avisos), source="image")

    # ── El piso de resolución ──────────────────────────────────────────────
    #
    # Se aplica DESPUÉS de interpretar, porque la pregunta no es cuántos píxeles
    # tiene la imagen sino si la planilla se puede verificar a sí misma.
    #
    # Una planilla que trae su longitud y su total declarados es su propio
    # oráculo: el importador recalcula la longitud desde las medidas y el total
    # desde la longitud por la cantidad por las veces, y la fila que no cierra
    # queda bloqueada a la vista. Medido a baja resolución, de los valores que
    # el OCR cambia no sobrevive ninguno a esas dos cuentas — con la permutación
    # de letras ya cortada antes, que era la excepción.
    #
    # Sin esas columnas no hay contra qué recalcular, y entonces sí: a esta
    # resolución se devolverían números que nadie puede desmentir.
    if px_columna < PX_POR_COLUMNA_MINIMO:
        sin_oraculo = [
            nombre
            for campo, nombre in (("unit_length", "longitud"), ("total_length", "longitud total"))
            if campo not in leido.columns.mapped
        ]
        if sin_oraculo:
            raise ImageTooSmallError(
                f"la tabla tiene {len(leido.columns.headers_raw)} columnas en muy pocos "
                f"píxeles —unos {int(px_columna)} por columna, y hacen falta al menos "
                f"{PX_POR_COLUMNA_MINIMO}— y a esa resolución los dígitos no se pierden: "
                f"se leen mal, que es peor. En una planilla que declara su longitud y su "
                f"total, el recálculo de cada fila delata el valor mal leído; en ésta no "
                f"se reconoció la columna de {' ni la de '.join(sin_oraculo)}, así que no "
                f"hay con qué comprobarlos. Si tienes el PDF, sube el PDF; si no, toma la "
                f"foto más de cerca o escanea a más resolución."
            )
        leido.warnings.append(
            f"la imagen está por DEBAJO del mínimo de resolución (unos "
            f"{int(px_columna)} píxeles por columna, y el mínimo son "
            f"{PX_POR_COLUMNA_MINIMO}): se leyó igual porque esta planilla declara su "
            f"longitud y su total, y el recálculo de cada fila delata el valor mal "
            f"leído. Esperá celdas en blanco y filas bloqueadas: no es que la planilla "
            f"esté mal, es que la imagen no da. Con el PDF se leen todas"
        )

    return leido


def parse_xlsx(data: bytes) -> ParseResult:
    """
    Lee una planilla CARGADA EN EXCEL.

    Es la más corta de las tres y no es casualidad: un XLSX ya es una matriz de
    celdas, así que no hay nada que extraer. Lo que sigue —qué columna es cada
    cosa, en qué unidad están las medidas, qué celda falta y se puede despejar de
    las cuentas del propio documento, y qué fila no cierra— es el mismo código
    que lee el PDF y la foto.

    ── Una hoja es una «página» ────────────────────────────────────────────

    Cada hoja del libro entra como una tabla, en el orden del libro, y el lector
    elige cuál es la de posiciones igual que elige entre las páginas de un PDF:
    mirando los rótulos. Por eso una hoja de instrucciones, de notas o de
    resumen no molesta —tiene otro ancho y otros rótulos— y no hace falta
    pedirle a nadie que la borre ni exigir que la hoja se llame de una manera.

    ── Qué NO cambia respecto de los otros dos ─────────────────────────────

    La verificación. Un número escrito a mano en una celda de Excel no es más
    cierto que uno impreso: el diámetro puede no existir, la medida puede estar
    en la columna de al lado y la unidad puede ser la otra. Todo eso lo atrapan
    el recálculo de Nest y el catálogo de la empresa, exactamente igual.

    Lo que sí cambia es que no hay lectura falible en el medio, y eso viaja en
    `source="xlsx"` para que la pantalla no pida revisar medida por medida algo
    que nadie reconoció.
    """
    # Diferido, como OpenCV: `openpyxl` son unos megas y un worker que sólo
    # recibe PDFs no los tiene que pagar.
    from xlsx_table import ExcelIlegible, tablas_de_excel

    try:
        matrices, avisos = tablas_de_excel(data)
    except ExcelIlegible as e:
        raise _ERRORES_DE_EXCEL.get(e.code, ParseError)(str(e)) from e

    tablas = [_Tabla(n, filas) for n, filas in enumerate(matrices, start=1)]

    # ── El separador decimal no se adivina, acá ─────────────────────────────
    #
    # Excel guarda el VALOR de una celda numérica, no cómo se ve: la misma celda
    # se muestra «120,5» en una computadora en español y «120.5» en una en
    # inglés, y el archivo dice 120.5 en las dos. `_texto` las escribe con punto,
    # así que el estilo es un hecho y no una inferencia.
    #
    # Detectarlo sería peor que no hacerlo: bastaría una celda de TEXTO con
    # «6,15» escrita a mano para que todo el libro pasara a estilo europeo, y
    # entonces los cientos de celdas numéricas bien guardadas —«120.5»— se
    # leerían 1205. Esa celda escrita como texto se avisa en `tablas_de_excel`,
    # que es quien puede verla.
    return _interpretar(
        tablas,
        len(tablas),
        list(avisos),
        source="xlsx",
        estilo_conocido=(
            "us",
            "las celdas numéricas del libro traen el valor y no su escritura: "
            "el separador decimal no hace falta adivinarlo",
        ),
    )


def _resolver_unidades(
    columnas: ColumnMapping,
    largos_de_medidas: list[float],
    largos_declarados: list[float],
    warnings: list[str],
) -> tuple[Unit, Unit, str]:
    """
    Las DOS unidades de la planilla: la de las medidas y la de las longitudes.

    ── Por qué son dos y no una ───────────────────────────────────────────

    Porque hay planillas que usan las dos a la vez, y tienen razón. Este
    encabezado es de un documento de obra real:

        │   DIMENSIONES (cm.)   │ LONG. (m.) │ CANT. │ LONG. TOT.(m.) │
        │  a │ b  │  c  │  d    │            │       │                │
        │ 15 │ 90 │ 435 │ 45    │    5.85    │   9   │     52.65      │

    Las medidas están en centímetros y las longitudes en metros, y 15 + 90 +
    435 + 45 = 585 cm = 5,85 m. Es coherente: uno mide con cinta y el otro se
    pide al corralón.

    Con una sola unidad para todo el documento, la comparación contra el motor
    daba 585 contra 5,85 en CADA fila, y una planilla perfectamente leída
    llegaba a la pantalla con catorce desacuerdos inventados. El problema no
    era la lectura: era que el modelo no podía decir lo que el papel decía.

    ── Cómo se decide cada una ────────────────────────────────────────────

    Primero lo que el encabezado DECLARA, en sus propias columnas. Si un grupo
    no declara nada, se toma lo que declara el otro —son longitudes de lo
    mismo, y una planilla que rotula un solo grupo lo rotula para todos—. Y
    recién si nadie declara nada, la física: una pieza de fierro tiene que
    caber en una barra.
    """
    indices_medidas = list(columnas.dimension_columns.values())
    indices_largos = [
        columnas.mapped[campo]
        for campo in ("unit_length", "total_length", "total_with_loss")
        if campo in columnas.mapped
    ]

    declarada_medidas, evidencia_medidas = _unidad_declarada(columnas, indices_medidas)
    declarada_largos, evidencia_largos = _unidad_declarada(columnas, indices_largos)

    fisica_medidas, evidencia_fisica = _unidad_plausible(largos_de_medidas)

    unidad = declarada_medidas or declarada_largos
    evidencia = evidencia_medidas if declarada_medidas else evidencia_largos

    if unidad is None:
        unidad, evidencia = fisica_medidas, evidencia_fisica
    elif fisica_medidas is not None and fisica_medidas != unidad:
        warnings.append(
            f"el encabezado declara {unidad} pero los largos son plausibles en "
            f"{fisica_medidas} ({evidencia_fisica}); se usó lo declarado"
        )
    elif fisica_medidas is not None:
        evidencia = f"{evidencia}, y {evidencia_fisica}"

    if unidad is None:
        # No se pudo concluir, y aun así se devuelve la planilla.
        #
        # Negarse acá sería lo peor de los dos mundos: la persona se queda sin
        # ver la tabla justamente por el único dato que le habría costado un
        # clic corregir. Así que se elige el default del oficio —la planilla de
        # obra boliviana está en centímetros— y se dice, en la evidencia y en
        # los avisos, que se eligió sin fundamento.
        unidad = "cm"
        evidencia = f"no se pudo determinar ({evidencia_fisica or 'sin largos legibles'}); se asumió cm"
        warnings.append(
            "la unidad no se pudo determinar y se asumió centímetros: revisala antes de guardar"
        )

    # La de las columnas de longitud. Cuando no la declaran —que es el caso
    # normal— es la misma que la de las medidas, y todo sigue como antes.
    unidad_claim: Unit = declarada_largos or unidad
    if declarada_largos and declarada_largos != unidad:
        fisica_largos, _ = _unidad_plausible(largos_declarados)
        if fisica_largos is not None and fisica_largos != declarada_largos:
            warnings.append(
                f"las columnas de longitud declaran {declarada_largos} pero sus valores son "
                f"plausibles en {fisica_largos}; se usó lo declarado"
            )
        evidencia = (
            f"{evidencia}. Las longitudes van en otra unidad: {evidencia_largos}"
        )

    return unidad, unidad_claim, evidencia


def _interpretar(
    tablas: list[_Tabla],
    total_paginas: int,
    warnings: list[str],
    source: Source,
    estilo_conocido: tuple[str, str] | None = None,
) -> ParseResult:
    """
    De una matriz de celdas a una planilla leída.

    Todo lo que decide qué DICE el documento vive acá, y de un solo lado: si el
    PDF y la imagen tuvieran cada uno su interpretación, la planilla se leería
    distinto según cómo llegó el mismo papel.

    `estilo_conocido` es para la única fuente que NO tiene que adivinar el
    separador decimal: un libro de Excel guarda el valor de la celda, no cómo
    está escrito. Ver `parse_xlsx`.
    """
    # El estilo numérico, del documento entero.
    todas_las_celdas = [c for t in tablas for f in t.filas for c in f if c]
    estilo, evidencia_estilo = estilo_conocido or detectar_estilo(todas_las_celdas)

    hallazgo = _buscar_encabezado(tablas)
    deducido = False

    if hallazgo is None:
        # Sin encabezado legible queda la otra vía: deducir qué es cada columna
        # de las CUENTAS del documento. Ver `_mapeo_desde_el_contenido`.
        desde_el_contenido = _mapeo_desde_el_contenido(tablas, estilo)
        if desde_el_contenido is not None:
            columnas, tabla_encabezado, fila_encabezado, inferencia = desde_el_contenido
            deducido = True
            warnings.append(
                "no se pudo leer la fila de encabezado, así que se dedujo qué es cada "
                "columna de las cuentas del propio documento: "
                + "; ".join(inferencia.evidencia)  # type: ignore[attr-defined]
                + ". Las medidas se rotularon «a», «b», «c»… en el orden en que están "
                "en el papel. REVISÁ las columnas antes de guardar"
            )
            hallazgo = (columnas, tabla_encabezado, fila_encabezado)

    if hallazgo is None:
        raise NoHeaderError(
            "no se reconoció la fila de encabezado, y las cuentas del documento tampoco "
            "alcanzaron para deducir qué es cada columna: hace falta una columna de "
            "posición y al menos una de medida"
            + (
                # Una imagen es UNA página, y la fila de encabezado está sólo en
                # la primera: la foto de una página de continuación no se puede
                # leer sola, por más nítida que esté. Decirlo acá le ahorra a la
                # persona probar con más luz un problema que no es de luz.
                ". Si la planilla tiene varias páginas, subí la foto de la primera: "
                "es la única que trae los rótulos de las columnas."
                if source == "image"
                else ""
            )
        )
    columnas, tabla_encabezado, fila_encabezado = hallazgo

    # Las letras de las medidas, que tienen que ser una tira seguida desde «a».
    #
    # Es un AVISO y no un rechazo, y la razón es que las medidas se entregan en
    # orden de columna: con el rótulo mal leído la figura sale igual, con un
    # cartel equivocado. Lo que sí se pierde es la columna cuyo rótulo quedó
    # repetido —«b» leída «d» descarta la «d» de verdad— y eso lo atrapa el
    # recálculo de la fila, porque las medidas ya no suman la longitud declarada.
    #
    # Rechazar el documento acá fue lo primero que se probó, y era demasiado:
    # tiraba planillas legibles por un glifo. Ver `_problema_de_las_letras`.
    problema = _problema_de_las_letras(columnas.dimension_columns)
    if problema is not None:
        warnings.append(
            f"los rótulos de las medidas se leyeron {problema}, y las letras de una "
            "planilla van siempre seguidas desde «a»: alguno se leyó mal. Las medidas "
            "se entregan igual en el orden del papel, pero revisá los nombres, y puede "
            "faltar la columna cuyo rótulo quedó repetido"
        )

    ancho = len(columnas.headers_raw)

    _resolver_croquis_y_seccion(columnas, tablas, ancho)

    if not deducido:
        _completar_con_inferencia(
            columnas, tablas, ancho, tabla_encabezado, fila_encabezado, estilo, warnings
        )

    i_code = columnas.mapped["code"]
    filas: list[ParsedRow] = []
    seccion_actual: str | None = None
    # Dos listas y no una: los largos que se SUMAN de las medidas y los que la
    # planilla DECLARA en su columna de longitud pueden estar en unidades
    # distintas, y cada uno confirma la suya.
    largos_de_medidas: list[float] = []
    largos_declarados: list[float] = []
    vistos: dict[str, int] = {}

    for t, tabla in enumerate(tablas):
        if tabla.ancho != ancho:
            continue

        # Una página de continuación puede repetir el encabezado; no hace falta
        # detectarlo, porque `_es_codigo` ya descarta la fila que dice «POS.».
        for r, fila in enumerate(tabla.filas):
            if t == tabla_encabezado and r <= fila_encabezado:
                continue

            # La sección viene en una celda combinada: sólo la trae la primera
            # fila del grupo y hay que arrastrarla hacia abajo.
            celda_seccion = _celda(fila, columnas.mapped.get("section"))
            if celda_seccion and celda_seccion not in {"-"}:
                seccion_actual = celda_seccion

            codigo = _celda(fila, i_code)
            if not _es_codigo(codigo):
                continue
            assert codigo is not None

            if len(filas) >= MAX_ROWS:
                warnings.append(f"se leyeron las primeras {MAX_ROWS} filas y el resto se descartó")
                break

            problemas: list[str] = []
            celdas: dict[str, str] = {}
            for campo, indice in columnas.mapped.items():
                valor = _celda(fila, indice)
                if valor:
                    celdas[campo] = valor

            dimensiones: list[Dimension] = []
            # Por ÍNDICE DE COLUMNA, no por letra.
            #
            # Las medidas se consumen en orden —son los tramos de la figura, uno
            # detrás del otro— y el orden que vale es el del papel, de izquierda
            # a derecha. Ordenar por la letra hace que el orden dependa de haber
            # leído bien el rótulo, y eso produce el peor error posible.
            #
            # Medido: a baja resolución el «b» de este encabezado se lee «d», y
            # ordenando por letra la fila 13 salía «65, 10, 155» donde el papel
            # dice «65, 155, 10». Suman lo mismo, así que la longitud cierra, el
            # total cierra y el peso cierra: la única señal de que algo está mal
            # es la barra doblada en otro orden en la obra. Por índice de columna
            # sale «65, 155, 10» —lo que dice el papel— con el rótulo mal puesto,
            # que es un problema de cartel y no de geometría.
            # Las celdas de medida en ORDEN DE COLUMNA, clasificadas antes de
            # armar nada: así la reparación de una celda mal leída puede entrar
            # en su lugar y no al final. El orden es el de la figura.
            ranuras: list[tuple[str, str]] = []
            for letra, indice in sorted(columnas.dimension_columns.items(), key=lambda kv: kv[1]):
                crudo = _celda(fila, indice)
                if crudo:
                    ranuras.append((letra, crudo))

            # ('num', valor) | ('mult', veces, medida) | ('falla',)
            leidas: list[tuple] = []
            for _letra, crudo in ranuras:
                # «2x24»: dos tramos de 24. Ver `_aviso_de_multiplicadores`.
                multiplicador = _leer_multiplicador(crudo, estilo)
                if multiplicador is not None:
                    leidas.append(("mult", *multiplicador))
                    continue
                valor = numero(crudo, estilo)
                leidas.append(("num", valor) if valor is not None else ("falla",))

            # ── La reparación de UNA celda, juzgada por la suma del papel ────
            #
            # Una sola, y sólo con la longitud declarada a mano: la suma es el
            # juez, y con dos incógnitas deja de juzgar. Ver `_reparar_medida`.
            largo_para_reparar = numero(celdas.get("unit_length"), estilo)
            fallas = [i for i, r in enumerate(leidas) if r[0] == "falla"]
            reparadas: list[str] = []
            if len(fallas) == 1 and largo_para_reparar:
                i = fallas[0]
                resto = sum(r[1] if r[0] == "num" else r[1] * r[2] for r in leidas if r[0] != "falla")
                arreglo = _reparar_medida(ranuras[i][1], estilo, resto, largo_para_reparar)
                if arreglo is not None:
                    texto, _contribucion, multiplicador = arreglo
                    leidas[i] = ("mult", *multiplicador) if multiplicador else ("num", numero(texto, estilo))
                    reparadas.append(
                        f"la medida «{ranuras[i][0]}» decía «{ranuras[i][1]}» y se leyó "
                        f"«{texto}» porque así la suma de las medidas da la longitud que "
                        f"la planilla declara; si el papel dice otra cosa, corregila"
                    )

            con_multiplicador: list[tuple[str, int, float]] = []
            medidas_sin_leer = 0
            for (letra, crudo), r in zip(ranuras, leidas):
                if r[0] == "num":
                    dimensiones.append(Dimension(name=letra, value=r[1]))
                elif r[0] == "mult":
                    con_multiplicador.append((letra, r[1], r[2]))
                    # La medida es M, no N×M: el multiplicador es de la FÓRMULA
                    # de la figura, no del dato. Ver `_aviso_de_multiplicadores`.
                    dimensiones.append(Dimension(name=letra, value=r[2]))
                else:
                    medidas_sin_leer += 1
                    problemas.append(f"la medida «{letra}» dice «{crudo}» y no es un número")

            problemas.extend(reparadas)


            largo_unitario = numero(celdas.get("unit_length"), estilo)
            if largo_unitario is not None:
                largos_declarados.append(largo_unitario)
            if dimensiones:
                largos_de_medidas.append(sum(d.value for d in dimensiones))

            diametro = numero(celdas.get("diameter"), estilo)
            if celdas.get("diameter") and diametro is None:
                problemas.append(f"el diámetro dice «{celdas['diameter']}» y no es un número")

            cantidad = entero(celdas.get("quantity"), estilo)
            if celdas.get("quantity") and cantidad is None:
                problemas.append(f"la cantidad dice «{celdas['quantity']}» y no es un número")

            elementos = entero(celdas.get("elements"), estilo)
            if celdas.get("elements") and elementos is None:
                problemas.append(
                    f"las veces que se repite dicen «{celdas['elements']}» y no es un número"
                )

            if con_multiplicador:
                problemas.append(
                    _aviso_de_multiplicadores(
                        con_multiplicador, dimensiones, largo_unitario, medidas_sin_leer
                    )
                )

            # La cantidad y las veces que el documento determina, cuando no se
            # pudieron leer. Ver `_deducir_piezas`: sólo despeja lo que falta si
            # la longitud está corroborada por la suma de las medidas, y sólo
            # acepta un entero que cierre hacia atrás.
            total_declarado = numero(celdas.get("total_length"), estilo)
            cantidad, elementos, deduccion = _deducir_piezas(
                dimensiones,
                celdas.get("unit_length"),
                largo_unitario,
                celdas.get("total_length"),
                total_declarado,
                cantidad,
                elementos,
                hay_columna_de_cantidad="quantity" in columnas.mapped,
                hay_columna_de_veces="elements" in columnas.mapped,
            )
            if deduccion:
                problemas.append(deduccion)

            croquis = celdas.get("sketch")
            letras = letras_croquis(croquis)
            if croquis and not letras:
                problemas.append(f"el croquis dice «{croquis}» y no son rótulos de medida")

            unmapped: dict[str, str] = {}
            for indice in columnas.unmapped:
                valor = _celda(fila, indice)
                if valor:
                    unmapped[columnas.headers_raw[indice]] = valor

            vistos[codigo] = vistos.get(codigo, 0) + 1

            filas.append(
                ParsedRow(
                    page=tabla.pagina,
                    index=len(filas),
                    section=seccion_actual,
                    code=codigo,
                    type_code=celdas.get("type_code"),
                    diameter_mm=diametro,
                    quantity=cantidad,
                    elements=elementos,
                    sketch=croquis,
                    sketch_letters=letras,
                    dimensions=dimensiones,
                    claimed=ClaimedRow(
                        unit_length=largo_unitario,
                        total_length=total_declarado,
                        total_with_loss=numero(celdas.get("total_with_loss"), estilo),
                        bars=entero(celdas.get("bars"), estilo),
                        weight_per_unit=numero(celdas.get("weight_per_unit"), estilo),
                        total_weight=numero(celdas.get("total_weight"), estilo),
                    ),
                    cells=celdas,
                    unmapped=unmapped,
                    issues=problemas,
                )
            )

    if not filas:
        raise NoRowsError("se reconoció el encabezado pero ninguna fila de posición")

    repetidos = sorted(c for c, n in vistos.items() if n > 1)
    if repetidos:
        # No es un error de lectura: en una planilla por secciones el rótulo es
        # único DENTRO de su sección, y «P1» existe entre los longitudinales y
        # otra vez entre los estribos. NAAU numera las posiciones con un entero
        # y no guarda el rótulo, así que la importación va a renumerar y quien
        # revisa tiene que saberlo.
        warnings.append(
            "rótulos repetidos en distintas secciones: "
            + ", ".join(repetidos)
            + "; las posiciones se van a numerar por orden de aparición"
        )

    unidad, unidad_claim, evidencia = _resolver_unidades(
        columnas, largos_de_medidas, largos_declarados, warnings
    )

    # ── Y lo mismo para el estilo numérico ─────────────────────────────────
    #
    # Avisaba la unidad cuando se asumía y el estilo no, y esa asimetría estaba
    # al revés: el estilo es el MÁS caro de los dos. Equivocarse de unidad es un
    # factor de 10 o de 100 y se ve a simple vista en el total; leer «6,150» con
    # punto decimal cuando la coma era decimal da 6150 en lugar de 6,15, un
    # factor de MIL, y la fila sigue pareciendo verosímil.
    #
    # Sin esto, lo único que lo delataba era la cola de la frase que empieza con
    # «Unidad detectada», que es el último lugar donde alguien busca un problema
    # de decimales.
    #
    # Y sólo si hay algo ambiguo: «sin evidencia» no significa que no se sepa,
    # significa que ninguna celda trajo las dos formas a la vez. En una planilla
    # de números enteros —pasa, y es la mitad de las plantillas cargadas a
    # mano— NINGUNA lectura cambia con el estilo, y el aviso mandaba a revisar
    # medidas que no tienen un decimal que revisar. Se avisa cuando existe de
    # verdad una celda con separador que el estilo puede leer de dos maneras.
    if evidencia_estilo == SIN_EVIDENCIA_DE_ESTILO and any(
        _parece_numero(c) and ("," in c or "." in c) for c in todas_las_celdas
    ):
        warnings.append(
            "no se pudo determinar si los números usan coma o punto decimal y se asumió "
            "punto: revisá las medidas antes de guardar, porque leer «6,150» como 6150 "
            "en lugar de 6,15 cambia la medida por mil"
        )

    resumen = _leer_resumen(tablas, estilo, ancho)

    return ParseResult(
        pages=total_paginas,
        source=source,
        unit=unidad,
        claim_unit=unidad_claim,
        unit_evidence=f"{evidencia}; números leídos con estilo «{estilo}» ({evidencia_estilo})",
        columns=columnas,
        rows=filas,
        summary=resumen,
        warnings=warnings,
    )


# ── Errores de lectura ─────────────────────────────────────────────────────
#
# Cada uno describe un archivo que no se puede leer, y son distintos entre sí
# porque lo que tiene que hacer la persona es distinto en cada caso: recuadrar
# la tabla, rotular las columnas, o revisar las unidades.


class ParseError(Exception):
    """Base: el archivo no se pudo leer como planilla."""

    code = "PARSE_FAILED"


class NoTableError(ParseError):
    code = "NO_TABLE"


class NoHeaderError(ParseError):
    code = "NO_HEADER"


class NoRowsError(ParseError):
    code = "NO_ROWS"


class CorruptPdfError(ParseError):
    """
    El archivo empieza con `%PDF-` pero no se puede abrir.

    Pasa de verdad: un PDF truncado por una descarga cortada, o generado por
    una herramienta que dejó la tabla de referencias mal. No es un archivo del
    formato equivocado —eso lo corta la firma, antes— así que merece su propio
    código y su propio mensaje: acá lo que hay que hacer es volver a exportar
    el archivo, no cambiarlo.
    """

    code = "CORRUPT_PDF"


class CorruptImageError(ParseError):
    """La imagen no se pudo decodificar: archivo truncado o formato mentido."""

    code = "CORRUPT_IMAGE"


class ImageTooLargeError(ParseError):
    """
    La imagen tiene demasiados píxeles.

    Es distinto de «el archivo pesa demasiado», y por eso tiene su propio
    código: un JPEG de 2 MB puede traer 60 megapíxeles, así que pasa el tope de
    bytes y aun así son 180 MB al decodificarlo. Lo que hay que hacer también es
    distinto — no comprimir más, sino sacar la foto con menos resolución.
    """

    code = "IMAGE_TOO_LARGE"


class ImageTooSmallError(ParseError):
    """
    La tabla tiene demasiadas columnas para los píxeles que hay.

    Es lo contrario de `ImageTooLargeError` y es más importante, porque no falla
    ruidosamente: a poca resolución el reconocimiento deja de perder celdas y
    empieza a CAMBIARLAS, y una medida cambiada produce una fila verosímil.
    Medido sobre dos planillas reales reducidas a varios anchos, el corte está
    en unos 60 píxeles por columna.

    Antes que devolver esos números, se niega y dice qué hace falta.
    """

    code = "IMAGE_TOO_SMALL"


class NoTextError(ParseError):
    """
    Se encontró la tabla y no se pudo leer una sola palabra.

    Separado de `NO_TABLE` porque le pide otra cosa a la persona: la tabla está
    ahí, lo que falla es la nitidez. Acercarse, más luz, sin sombras.
    """

    code = "NO_TEXT"


class CorruptExcelError(ParseError):
    """
    El archivo es un ZIP pero no es un libro de Excel que se pueda abrir.

    Un `.xlsx` es un ZIP, así que la firma de bytes que lo deja entrar —`PK`—
    también la tienen un `.docx`, un `.jar` y un ZIP cualquiera. La firma alcanza
    para no llevar basura al decodificador; distinguir un libro de una carta la
    hace este error, y le pide otra cosa a la persona: volver a guardar el
    archivo como .xlsx, no cambiar de archivo.
    """

    code = "CORRUPT_EXCEL"


class ExcelTooLargeError(ParseError):
    """
    El libro tiene demasiadas celdas con contenido.

    Es el mismo caso que `ImageTooLargeError` y por la misma razón tiene su
    propio código: un XLSX es un ZIP con XML adentro, así que 100 kB de archivo
    pueden ser un millón de filas al abrirlo. El tope de bytes del endpoint no
    dice nada del trabajo que hay dentro.
    """

    code = "EXCEL_TOO_LARGE"


class ExcelUnavailableError(ParseError):
    """
    Falta la biblioteca que lee Excel.

    No es un problema del archivo: es del servidor, y el mensaje lo dice así
    para que nadie salga a buscar el error en su planilla. Existe porque
    `openpyxl` se importa diferido, y un despliegue con las dependencias a medio
    instalar arranca igual y falla recién cuando alguien sube un .xlsx.
    """

    code = "EXCEL_UNAVAILABLE"


# Los códigos que levanta el lector de Excel, a errores de este módulo. Existe
# por lo mismo que `_ERRORES_DE_IMAGEN`: `xlsx_table` no conoce esta jerarquía.
_ERRORES_DE_EXCEL: dict[str, type[ParseError]] = {
    "NO_TABLE": NoTableError,
    "CORRUPT_EXCEL": CorruptExcelError,
    "EXCEL_TOO_LARGE": ExcelTooLargeError,
    "EXCEL_UNAVAILABLE": ExcelUnavailableError,
}


# Los códigos que levanta el lector de imágenes, a errores de este módulo.
#
# El mapa existe para que `image_table` no tenga que importar esta jerarquía:
# es un lector de píxeles y no sabe nada de planillas, y esa separación es lo
# que deja probarlo con una imagen cualquiera.
_ERRORES_DE_IMAGEN: dict[str, type[ParseError]] = {
    "NO_TABLE": NoTableError,
    "IMAGE_TOO_SMALL": ImageTooSmallError,
    "NO_TEXT": NoTextError,
    "CORRUPT_IMAGE": CorruptImageError,
    "IMAGE_TOO_LARGE": ImageTooLargeError,
}

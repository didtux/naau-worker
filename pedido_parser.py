"""
El pedido de corte y doblado agrupado por ELEMENTO.

Es el otro formato que llega a obra, y no se parece a la planilla de
posiciones que lee `pdf_parser._interpretar`:

    Función             │ Ø   │ Cantidad de Barras │ Nº zap. │ Total barras │ Esquema (cm) │ Long. corte (cm)
    ZAPATAS C49 A C52 · 200 x 200 cm, h = 90 cm · Eje A …                    (fila de grupo)
    Parrilla inferior X │ Ø16 │ 19                 │ 7       │ 133          │ 30 190 30    │ 250
    Estribo arranque    │ Ø6  │ 3                  │ 7       │ 21           │ 34 · 39 …    │ 160

No tiene columna POS ni columnas de medida: las medidas vienen ESCRITAS en
el esquema («30 190 30», «30 (pata) 160»). Cada tabla repite su encabezado y
abre con una fila de grupo a todo el ancho que dice a qué elementos se aplica.

Tres cosas más que la planilla de posiciones no tiene:

- Filas «LONGITUD VARIABLE – SE FABRICARÁ EN OBRA»: zapatas no rectangulares
  que no se cortan en taller. Se devuelven como referencia (`reference_only`)
  y no como posición.
- El cuadro «Barras rectas de 12 m (sin cortar)»: lo que se compra entero para
  esas zapatas. Se devuelve como posiciones de barra recta (`straight_bars`).
- El cuadro «Piezas a cortar y doblar» por diámetro y el índice de elementos
  de la primera hoja, que sirven para verificar.

Igual que el resto del worker: se devuelve lo que el papel DICE.
"""

from __future__ import annotations

import re

from models import (
    ClaimedRow,
    ColumnMapping,
    Dimension,
    ElementGroup,
    ParsedRow,
    ParseResult,
    PiecesSummaryLine,
)
from pdf_parser import MAX_ROWS, _celda, _Tabla, entero, normalizar, numero

#: Lo que tiene que decir el encabezado para que la tabla sea un pedido.
_CAMPOS = {
    "label": ("FUNCION", "DESCRIPCION", "ELEMENTO"),
    "diameter": ("Ø", "ØMM", "DIAM", "DIAMETRO"),
    "quantity": ("CANTIDADDEBARRAS", "CANTIDAD", "CANTBARRAS", "NBARRAS"),
    "elements": ("NºZAP", "NZAP", "NOZAP", "NºZAPATAS", "NZAPATAS", "NºELEM", "NELEM", "NOELEM", "NºVIGAS", "NVIGAS"),
    "total_pieces": ("TOTALBARRAS", "TOTALPIEZAS", "TOTAL"),
    "sketch": ("ESQUEMADEDOBLADO", "ESQUEMA", "CROQUIS"),
    "unit_length": ("LONGCORTE", "LONGDECORTE", "LONGITUDDECORTE", "LONG"),
}

_UNIDAD = re.compile(r"\((MM|CM|M)\)")
_VARIABLE = re.compile(r"LONGITUD\s+VARIABLE|EN\s+OBRA", re.IGNORECASE)
_GRUPO = re.compile(r"^(ZAPATAS?|VIGAS?|COLUMNAS?|LOSAS?|MUROS?|CIMIENTOS?|PILOTES?|ELEMENTOS?)\b", re.IGNORECASE)
_CODIGO_ELEMENTO = re.compile(r"\b([A-Z]{1,3}\d{1,3}(?:-[A-Z]{1,3}\d{1,3})?)\b")
_NUMEROS = re.compile(r"\d+(?:[.,]\d+)?")


def _clave(texto: str | None) -> str:
    """El rótulo sin espacios, puntos, saltos ni la unidad entre paréntesis."""
    t = _UNIDAD.sub("", normalizar(texto))
    return re.sub(r"[\s.:]+", "", t)


def _mapear(encabezado: list[str | None]) -> dict[str, int] | None:
    mapeo: dict[str, int] = {}
    for i, crudo in enumerate(encabezado):
        k = _clave(crudo)
        if not k:
            continue
        for campo, sinonimos in _CAMPOS.items():
            if campo in mapeo:
                continue
            if k in sinonimos:
                mapeo[campo] = i
                break
    necesarios = {"label", "diameter", "quantity", "unit_length"}
    if not necesarios <= mapeo.keys():
        return None
    # Sin multiplicador ni total no es un pedido por elemento: es una planilla
    # común sin POS, y ésa no es de este lector.
    if "elements" not in mapeo and "total_pieces" not in mapeo:
        return None
    return mapeo


def _unidad_de(encabezado: list[str | None], indice: int) -> str:
    m = _UNIDAD.search(normalizar(encabezado[indice] if indice < len(encabezado) else ""))
    return m.group(1).lower() if m else "cm"


def _diametro(texto: str | None) -> float | None:
    if not texto:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)", texto)
    return float(m.group(1).replace(",", ".")) if m else None


def _es_grupo(fila: list[str | None]) -> str | None:
    """La fila a todo el ancho que abre un grupo: sólo la primera celda dice algo."""
    primera = _celda(fila, 0)
    if not primera:
        return None
    resto = [c for c in fila[1:] if c and c.strip()]
    if resto:
        return None
    return re.sub(r"\s+", " ", primera).strip()


def _codigo_de_grupo(texto: str, n: int) -> str:
    """«ZAPATAS C49 A C52 Y C56 …» → «C49»; sin código, «G<n>»."""
    sin_tipo = _GRUPO.sub("", texto).strip()
    sin_tipo = re.sub(r"^COMBINADAS?\b", "", sin_tipo, flags=re.IGNORECASE).strip()
    m = _CODIGO_ELEMENTO.search(sin_tipo.upper())
    return m.group(1) if m else f"G{n}"


def _medidas_del_esquema(esquema: str | None, largo: float | None) -> list[float] | None:
    """
    Los tramos escritos en el esquema, si suman la longitud de corte.

    «30 190 30» con 250 → [30, 190, 30]. «30 (pata) 160» con 190 → [30, 160].
    Si no suman —un estribo con ganchos de 135° suma sus lados y el papel le
    agrega los ganchos— no se inventa una fórmula: la posición va con la
    longitud declarada.
    """
    if not esquema or largo is None:
        return None
    valores = [float(v.replace(",", ".")) for v in _NUMEROS.findall(esquema)]
    if not valores or len(valores) > 8:
        return None
    if abs(sum(valores) - largo) < 0.01:
        return valores
    return None


def _leer_indice(tablas: list[_Tabla]) -> list[ElementGroup]:
    for tabla in tablas:
        if not tabla.filas:
            continue
        cab = [_clave(c) for c in tabla.filas[0]]
        if not cab or not (cab[0].startswith("ZAPATA") or cab[0].startswith("ELEMENTO") or cab[0].startswith("VIGA")):
            continue
        i_n = next((i for i, c in enumerate(cab) if c in {"Nº", "N", "NO", "CANT", "CANTIDAD"}), None)
        i_dim = next((i for i, c in enumerate(cab) if c.startswith("DIMENS")), None)
        i_ubi = next((i for i, c in enumerate(cab) if c.startswith("UBICAC")), None)
        i_h = next((i for i, c in enumerate(cab) if c == "H"), None)
        grupos: list[ElementGroup] = []
        for fila in tabla.filas[1:]:
            nombre = _celda(fila, 0)
            if not nombre or normalizar(nombre).startswith("TOTAL"):
                continue
            grupos.append(
                ElementGroup(
                    name=nombre,
                    location=_celda(fila, i_ubi) if i_ubi is not None else None,
                    dimensions=_celda(fila, i_dim) if i_dim is not None else None,
                    height=_celda(fila, i_h) if i_h is not None else None,
                    count=entero(_celda(fila, i_n)) if i_n is not None else None,
                )
            )
        if grupos:
            return grupos
    return []


def _leer_cuadros(tablas: list[_Tabla]) -> tuple[list[PiecesSummaryLine], list[tuple[float, int, int, list[float] | None, str]]]:
    """El cuadro de piezas por diámetro y el de barras rectas sin cortar."""
    piezas: list[PiecesSummaryLine] = []
    rectas: list[tuple[float, int, int, list[float] | None, str]] = []
    for tabla in tablas:
        if len(tabla.filas) < 2:
            continue
        cab = [_clave(c) for c in tabla.filas[0]]
        if not cab or not cab[0].startswith("DIAMETRO"):
            continue
        texto_cab = " ".join(normalizar(c) for c in tabla.filas[0] if c)
        es_rectas = "RECTAS" in texto_cab or "SIN CORTAR" in texto_cab
        for r, fila in enumerate(tabla.filas[1:], start=1):
            d = _celda(fila, 0)
            if not d or normalizar(d).startswith("TOTAL"):
                continue
            diametro = _diametro(d)
            if diametro is None:
                continue
            if es_rectas:
                n = entero(_celda(fila, 1))
                if n:
                    largo = re.search(r"(\d+(?:[.,]\d+)?)\s*M\b", texto_cab)
                    largo_m = float(largo.group(1).replace(",", ".")) if largo else 12.0
                    rectas.append((diametro, n, tabla.pagina, tabla.caja(r), f"{largo_m:g}"))
            else:
                piezas.append(
                    PiecesSummaryLine(
                        diameter_mm=diametro,
                        pieces=entero(_celda(fila, 1)),
                        length_m=numero(_celda(fila, 2)),
                    )
                )
    return piezas, rectas


def interpretar_pedido(tablas: list[_Tabla], total_paginas: int, warnings: list[str]) -> ParseResult | None:
    """El pedido leído, o `None` si las tablas no son un pedido por elemento."""
    encabezado: list[str | None] | None = None
    mapeo: dict[str, int] | None = None
    for tabla in tablas:
        if tabla.filas:
            mapeo = _mapear(tabla.filas[0])
            if mapeo:
                encabezado = tabla.filas[0]
                break
    if not mapeo or encabezado is None:
        return None

    unidad = _unidad_de(encabezado, mapeo["unit_length"])
    ancho = len(encabezado)

    filas: list[ParsedRow] = []
    grupo: str | None = None
    codigo_grupo = "G1"
    n_grupos = 0
    n_en_grupo = 0
    variables = 0

    for tabla in tablas:
        if not tabla.filas or tabla.ancho != ancho or _mapear(tabla.filas[0]) is None:
            continue
        for r, fila in enumerate(tabla.filas[1:], start=1):
            titulo = _es_grupo(fila)
            if titulo:
                n_grupos += 1
                grupo = titulo
                codigo_grupo = _codigo_de_grupo(titulo, n_grupos)
                n_en_grupo = 0
                continue

            etiqueta = _celda(fila, mapeo["label"])
            if not etiqueta:
                continue
            if len(filas) >= MAX_ROWS:
                warnings.append(f"se leyeron las primeras {MAX_ROWS} filas y el resto se descartó")
                break

            n_en_grupo += 1
            celdas = {campo: v for campo, i in mapeo.items() if (v := _celda(fila, i))}
            esquema = celdas.get("sketch")
            largo_txt = celdas.get("unit_length")
            largo = numero(largo_txt)
            cantidad = entero(celdas.get("quantity"))
            elementos = entero(celdas.get("elements")) if "elements" in mapeo else None
            total = entero(celdas.get("total_pieces")) if "total_pieces" in mapeo else None
            problemas: list[str] = []

            variable = bool(_VARIABLE.search(esquema or "") or _VARIABLE.search(largo_txt or ""))
            medidas = None if variable else _medidas_del_esquema(esquema, largo)
            solo_largo = not variable and medidas is None and largo is not None

            if variable:
                variables += 1
                problemas.append(
                    "longitud variable: se fabrica en obra y no se corta en taller; se compra "
                    "como barra recta (cuadro de barras rectas)"
                )
            elif solo_largo:
                problemas.append(
                    f"los tramos del esquema no suman la longitud de corte ({largo:g} {unidad}); "
                    "se carga con la longitud declarada"
                )
            elif largo is None:
                problemas.append("la fila no trae longitud de corte")

            if cantidad is not None and elementos is not None and total is not None and cantidad * elementos != total:
                problemas.append(
                    f"el total de barras ({total}) no es cantidad × Nº de elementos "
                    f"({cantidad} × {elementos} = {cantidad * elementos})"
                )

            filas.append(
                ParsedRow(
                    page=tabla.pagina,
                    index=len(filas),
                    bbox=tabla.caja(r),
                    section=grupo,
                    code=f"{codigo_grupo}-{n_en_grupo}",
                    label=etiqueta,
                    diameter_mm=_diametro(celdas.get("diameter")),
                    quantity=cantidad,
                    elements=elementos,
                    sketch=esquema,
                    dimensions=[
                        Dimension(name="abcdefgh"[i], value=v) for i, v in enumerate(medidas or [])
                    ],
                    claimed=ClaimedRow(unit_length=largo, total_pieces=total),
                    cells=celdas,
                    reference_only=variable,
                    length_only=solo_largo,
                    issues=problemas,
                )
            )

    if not filas:
        return None

    piezas, rectas = _leer_cuadros(tablas)
    for diametro, n, pagina, caja, largo_m in rectas:
        filas.append(
            ParsedRow(
                page=pagina,
                index=len(filas),
                bbox=caja,
                section=f"BARRAS RECTAS DE {largo_m} m (SIN CORTAR)",
                code=f"R{diametro:g}",
                label=f"Barras rectas de {largo_m} m sin cortar",
                diameter_mm=diametro,
                quantity=n,
                elements=1,
                claimed=ClaimedRow(
                    unit_length=float(largo_m) * (100 if unidad == "cm" else 1000 if unidad == "mm" else 1),
                    total_pieces=n,
                ),
                straight_bars=True,
            )
        )

    if variables and not rectas:
        warnings.append(
            f"{variables} filas de longitud variable y el documento no trae el cuadro de barras "
            "rectas: cárgalas a mano"
        )

    diametros_variables = {f.diameter_mm for f in filas if f.reference_only}
    for diametro, n, *_ in rectas:
        if diametro not in diametros_variables:
            warnings.append(
                f"el cuadro de barras rectas pide {n} barras Ø{diametro:g} pero ninguna fila de "
                f"longitud variable es Ø{diametro:g}: revisa ese número con quien hizo el pedido"
            )

    return ParseResult(
        pages=total_paginas,
        source="pdf",
        unit=unidad,  # type: ignore[arg-type]
        claim_unit=unidad,  # type: ignore[arg-type]
        unit_evidence=f"pedido por elemento; la unidad la declara el encabezado «{encabezado[mapeo['unit_length']]}»",
        columns=ColumnMapping(
            headers_raw=[c or "" for c in encabezado],
            mapped={k: v for k, v in mapeo.items() if k in {"diameter", "quantity", "elements", "sketch", "unit_length"}},
        ),
        rows=filas,
        layout="order",
        pieces_summary=piezas,
        elements_index=_leer_indice(tablas),
        warnings=warnings,
    )

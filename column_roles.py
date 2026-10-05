"""
Qué es cada columna, deducido del CONTENIDO y no del rótulo.

── El problema ─────────────────────────────────────────────────────────────

`pdf_parser.mapear_columnas` resuelve las columnas por diccionario de sinónimos
contra el encabezado impreso. Nunca por posición, así que el orden de las
columnas da igual — pero el rótulo tiene que estar en el diccionario. Un
encabezado que no está («PESO +7%»), o uno que el reconocimiento de caracteres
rompió («LONG. TOT.(m.)» → «LONGTOT(rn)»), deja la columna sin reconocer, y con
ella se va la mitad de la planilla.

Ampliar el diccionario no resuelve eso: es una lista que persigue documentos que
todavía no vimos, y con el reconocimiento de caracteres los rótulos no son ni
estables.

── Por qué esto NO es adivinar ─────────────────────────────────────────────

Porque una planilla de fierros es un SISTEMA DE ECUACIONES, y se verifica a sí
misma. En cada fila:

    suma de las medidas   = longitud de la pieza      (× el factor de unidad)
    longitud × cantidad × veces = longitud total
    longitud total × peso por metro del diámetro = peso total

Así que no hace falta preguntarse qué significa una columna: hace falta BUSCAR
el reparto de papeles que hace que las cuentas del documento cierren. Si existe
uno y sólo uno que cierra en la mayoría de las filas, ése es el reparto — y lo
afirma el documento, no una heurística sobre nombres.

Y si ninguno cierra, esto devuelve `None`. Eso es lo que hace que sea seguro:
la salida de este módulo siempre viene con la cuenta de filas que la confirman,
y quien la usa puede exigir evidencia antes de creerle. Un reparto inventado que
nadie confirma no se distingue de uno correcto, y ése es el único error que un
documento que termina en fierro cortado no se puede permitir.

── Lo que este módulo NO puede deducir ─────────────────────────────────────

Las columnas que no participan de ninguna ecuación: el tipo de figura, la
sección, el croquis. No tienen nada contra qué verificarse, y adivinarlas sería
exactamente lo que este módulo existe para no hacer. Siguen saliendo por
rótulo, y lo que no se reconoce viaja crudo a la pantalla.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from pdf_parser import numero

# ── Topes y tolerancias ────────────────────────────────────────────────────

MINIMO_DE_FILAS = 3
"""
Menos que esto no es evidencia de nada.

Con dos filas, cualquier reparto de columnas «cierra» por casualidad más veces
de las que uno esperaría: hay demasiados subconjuntos y muy pocas ecuaciones.
"""

FRACCION_QUE_DEBE_CERRAR = 0.6
"""
Qué proporción de las filas evaluables tiene que confirmar el reparto.

No el 100 %, porque a baja resolución se pierden celdas y una fila a la que le
falta una medida no cierra por más que el reparto sea el correcto. No la mitad,
porque ahí ya no distingue.
"""

MAX_MEDIDAS = 12
"""Las letras de croquis van de la «a» a la «l»."""

MAX_COLUMNAS_CANDIDATAS = 14
"""
Tope del subconjunto que se enumera.

La búsqueda de las medidas prueba los subconjuntos de una fila sola —2^14 es un
instante— y después verifica los pocos que dieron contra todas las filas. Sin
tope, una tabla ancha de basura podría hacer explotar el tiempo de una petición.
"""

PRESUPUESTO_DE_BUSQUEDA = 400_000
"""
Cuántos subconjuntos se prueban antes de rendirse.

El tope de arriba acota la enumeración por cada columna de longitud, pero no
cuántas columnas de longitud hay: una tabla de veinte columnas de números sin
ninguna relación entre sí multiplica lo uno por lo otro. Medido, una de 17 × 40
tarda 0,8 s en decir que no encontró nada; con veinte se iría a diez segundos,
y esto corre dentro de una petición.

Rendirse es una respuesta correcta y no un atajo: lo que devuelve entonces es
`None`, que es exactamente lo que corresponde cuando no se encontró evidencia.
"""

#: Las razones de unidad posibles entre dos columnas de la misma planilla.
#:
#: Existen porque hay planillas que usan dos unidades a la vez y tienen razón:
#: «DIMENSIONES (cm.)» sobre las medidas y «LONG. (m.)» al lado, con 15 + 90 +
#: 435 + 45 = 585 cm = 5,85 m. El factor que hace cerrar la suma ES la razón
#: entre las dos unidades, así que se descubre en lugar de asumirse.
FACTORES = (1.0, 100.0, 10.0, 1000.0, 0.01, 0.1, 0.001)

#: Los diámetros que una planilla de fierros puede traer, en mm.
#:
#: Métricos y las fracciones de pulgada expresadas en mm, que es como se
#: escriben en la columna. Es una lista cerrada porque el diámetro no se elige:
#: se compra el que existe.
DIAMETROS = frozenset(
    {4, 4.2, 4.7, 5, 5.5, 6, 6.4, 7, 7.9, 8, 9.5, 10, 11, 12, 12.7, 14, 16, 19, 20,
     22, 22.2, 25, 25.4, 28, 28.7, 32, 32.3, 35.8, 36, 40, 50}
)


def _cierra(valor: float, esperado: float, holgura: float) -> bool:
    """
    Si dos números de una planilla son el mismo número.

    ── Por qué la holgura la calcula quien llama ───────────────────────────

    Porque no es una constante: es el REDONDEO del documento. Una longitud
    escrita «5.85» en metros está redondeada al centímetro, así que comparada
    contra una suma en centímetros puede diferir medio centímetro y ser el mismo
    número. Una escrita «585» no puede diferir en nada.

    Empezó siendo una constante —medio punto— y eso rompía el módulo entero: con
    medio punto de margen absoluto, una columna de unos «cierra» contra una
    columna de diámetros dividida por diez, y el buscador se quedaba con ese
    reparto porque cerraba en diez de doce filas. Dos columnas de números chicos
    son indistinguibles si la tolerancia es del tamaño de los números.

    Así que la holgura sale de los decimales con que el papel escribió la columna
    contra la que se compara, escalados por el factor de unidad. Ver
    `_holgura_de`.
    """
    return abs(valor - esperado) <= holgura + abs(esperado) * 1e-9


def _decimales(filas: list[list[str | None]], columna: int) -> int:
    """
    Con cuántos decimales escribió el papel esta columna.

    Es lo que dice cuánto puede estar redondeado cada valor, y de ahí sale la
    holgura de las comparaciones. Se lee del texto crudo y no del `float`,
    porque «5.80» y «5.8» son el mismo número y no el mismo redondeo.
    """
    mayor = 0
    for fila in filas:
        if columna >= len(fila) or fila[columna] is None:
            continue
        texto = str(fila[columna]).strip()
        for separador in (".", ","):
            if separador in texto:
                cola = texto.rsplit(separador, 1)[1]
                if cola.isdigit():
                    mayor = max(mayor, len(cola))
    return mayor


def _holgura_de(decimales: int, factor: float = 1.0) -> float:
    """Medio paso de redondeo de una columna, en las unidades del otro lado."""
    return 0.5 * (10.0**-decimales) * factor


# ── El resultado ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Inferencia:
    """
    El reparto de papeles que el documento confirma, con su evidencia.

    `filas_que_cierran` y `filas_evaluadas` no son adornos: son la razón por la
    que quien llama puede confiar en esto. Un reparto es creíble en la medida en
    que las cuentas del papel lo respaldan, y ese respaldo viaja con él.
    """

    roles: dict[str, int] = field(default_factory=dict)
    medidas: list[int] = field(default_factory=list)
    factor_de_medidas: float = 1.0
    filas_que_cierran: int = 0
    filas_evaluadas: int = 0
    evidencia: list[str] = field(default_factory=list)

    @property
    def creible(self) -> bool:
        return (
            self.filas_evaluadas >= MINIMO_DE_FILAS
            and self.filas_que_cierran >= self.filas_evaluadas * FRACCION_QUE_DEBE_CERRAR
        )


# ── Las columnas, en números ───────────────────────────────────────────────


def _valores(filas: list[list[str | None]], estilo: str) -> dict[int, dict[int, float]]:
    """Por columna, el valor numérico de cada fila que tiene uno."""
    ancho = max((len(f) for f in filas), default=0)
    columnas: dict[int, dict[int, float]] = {}
    for c in range(ancho):
        valores: dict[int, float] = {}
        for r, fila in enumerate(filas):
            if c >= len(fila):
                continue
            v = numero(fila[c], estilo)
            if v is not None:
                valores[r] = v
        if valores:
            columnas[c] = valores
    return columnas


def _densas(columnas: dict[int, dict[int, float]], filas: int) -> list[int]:
    """
    Las columnas que traen números en al menos la mitad de las filas.

    Una columna con dos números sueltos entre catorce filas no es una columna
    numérica: es una columna de texto donde el reconocimiento leyó dos cosas
    como números. Entrar a la búsqueda con ella multiplica los subconjuntos y no
    aporta una sola ecuación.
    """
    return sorted(c for c, v in columnas.items() if len(v) * 2 >= filas)


# ── La ecuación de las medidas ─────────────────────────────────────────────


def _buscar_medidas(
    filas: list[list[str | None]],
    columnas: dict[int, dict[int, float]],
    densas: list[int],
    total_filas: int,
) -> tuple[list[int], int, float, int, int] | None:
    """
    El subconjunto de columnas que SUMA otra columna: las medidas y la longitud.

    Es la ecuación más fuerte de la planilla y por eso va primera. Encontrarla
    resuelve tres cosas de un saque: cuáles son las medidas, cuál es la longitud
    de la pieza, y el factor de unidad entre las dos.

    ── Cómo se busca sin enumerar el universo ─────────────────────────────

    Enumerar todos los subconjuntos de todas las columnas contra todas las filas
    es carísimo. Pero el subconjunto correcto es el MISMO en todas las filas, así
    que basta resolverlo en UNA fila —la más completa— y después verificar los
    pocos candidatos que dieron contra el resto. Una fila con catorce columnas
    son 2^14 sumas, que es un instante; verificar tres candidatos contra catorce
    filas, nada.
    """
    if len(densas) < 2:
        return None

    # La columna de LONGITUD está siempre llena: es el dato que la planilla
    # calcula para cada pieza. Las de MEDIDA no — la «e» de este documento tiene
    # dos valores en catorce filas, porque sólo dos figuras usan cinco tramos, y
    # exigirle densidad sería descartar la mitad de las medidas de la planilla.
    todas = sorted(columnas)
    mejor: tuple[list[int], int, float, int, int] | None = None
    presupuesto = PRESUPUESTO_DE_BUSQUEDA

    for largo in densas:
        decimales = _decimales(filas, largo)
        for factor in FACTORES:
            holgura = _holgura_de(decimales, factor)
            # Un tramo no puede ser más largo que la pieza. Descartar por eso
            # deja la enumeración en un puñado de columnas.
            posibles = [
                c
                for c in todas
                if c != largo
                and sum(
                    1
                    for r, v in columnas[c].items()
                    if r in columnas[largo] and v <= columnas[largo][r] * factor + holgura
                )
                * 2
                >= len(columnas[c])
            ][:MAX_COLUMNAS_CANDIDATAS]
            if not posibles:
                continue

            # La fila más completa: la que tiene número en más de las posibles.
            filas_utiles = [r for r in columnas[largo] if columnas[largo][r] > 0]
            if not filas_utiles:
                continue
            piloto = max(
                filas_utiles,
                key=lambda r: sum(1 for c in posibles if r in columnas[c]),
            )
            presentes = [c for c in posibles if piloto in columnas[c]]
            objetivo = columnas[largo][piloto] * factor

            for n in range(1, min(len(presentes), MAX_MEDIDAS) + 1):
                for combo in itertools.combinations(presentes, n):
                    presupuesto -= 1
                    if presupuesto <= 0:
                        return mejor
                    if not _cierra(sum(columnas[c][piloto] for c in combo), objetivo, holgura):
                        continue

                    cierran = evaluadas = 0
                    for r, largo_r in columnas[largo].items():
                        tramos = [columnas[c][r] for c in combo if r in columnas[c]]
                        if not tramos:
                            continue
                        evaluadas += 1
                        if _cierra(sum(tramos), largo_r * factor, holgura):
                            cierran += 1

                    if evaluadas < MINIMO_DE_FILAS:
                        continue
                    if cierran < evaluadas * FRACCION_QUE_DEBE_CERRAR:
                        continue

                    # Entre dos repartos que cierran, gana el que explica MÁS
                    # columnas: un subconjunto de las medidas también suma la
                    # longitud en las filas donde los tramos que le faltan están
                    # vacíos, y quedarse con él perdería medidas de verdad.
                    puntaje = (cierran, len(combo))
                    if mejor is None or puntaje > (mejor[3], len(mejor[0])):
                        mejor = (sorted(combo), largo, factor, cierran, evaluadas)

    return mejor


# ── La ecuación del total ──────────────────────────────────────────────────


def _buscar_total(
    filas: list[list[str | None]],
    columnas: dict[int, dict[int, float]],
    candidatas: list[int],
    i_largo: int,
    tomadas: set[int],
) -> tuple[int, int | None, int | None, int, int] | None:
    """
    `longitud × cantidad × veces = total`, y de ahí salen las tres columnas.

    La cantidad y las veces se distinguen de cualquier otra columna de enteros
    chicos justamente por esto: son las que hacen cerrar el total. Sin la
    ecuación, «CANT.» y «VECES» son dos columnas de números de una cifra y no hay
    forma de decir cuál es cuál — con la ecuación, el producto las ordena.

    Las veces pueden no existir: hay planillas que no tienen esa columna, y ahí
    el total es longitud × cantidad. Se prueban las dos formas y gana la que
    cierra en más filas.
    """
    enteras = [
        c
        for c in candidatas
        if c not in tomadas
        and c != i_largo
        and all(abs(v - round(v)) < 0.001 and 0 < v <= 10_000 for v in columnas[c].values())
    ]
    mejor: tuple[int, int | None, int | None, int, int] | None = None

    decimales_largo = _decimales(filas, i_largo)

    for total in candidatas:
        if total in tomadas or total == i_largo:
            continue
        decimales_total = _decimales(filas, total)
        for factor in (1.0, 100.0, 0.01):
            for cantidad in enteras:
                if cantidad == total:
                    continue
                # Sin veces, y con cada candidata a veces.
                for veces in [None, *[v for v in enteras if v not in (total, cantidad)]]:
                    cierran = evaluadas = 0
                    for r, largo_r in columnas[i_largo].items():
                        if r not in columnas[total] or r not in columnas[cantidad]:
                            continue
                        n_veces = 1.0 if veces is None else columnas[veces].get(r)
                        if n_veces is None:
                            continue
                        evaluadas += 1
                        piezas = columnas[cantidad][r] * n_veces
                        # El total está redondeado a SUS decimales, y arrastra
                        # además el redondeo de la longitud multiplicado por las
                        # piezas: 94 piezas de una longitud al centímetro son
                        # casi medio metro de incertidumbre legítima.
                        holgura = _holgura_de(decimales_total, factor) + _holgura_de(
                            decimales_largo
                        ) * piezas
                        if _cierra(columnas[total][r] * factor, largo_r * piezas, holgura):
                            cierran += 1

                    if evaluadas < MINIMO_DE_FILAS:
                        continue
                    if cierran < evaluadas * FRACCION_QUE_DEBE_CERRAR:
                        continue

                    # Gana el que cierra en más filas; a igualdad, el que NO
                    # inventa una columna de veces. Una columna de unos multiplica
                    # por uno y cierra igual, y tomarla por «veces» sería ponerle
                    # un nombre a algo que no se verificó.
                    puntaje = (cierran, veces is None)
                    actual = (mejor[3], mejor[2] is None) if mejor else None
                    if actual is None or puntaje > actual:
                        mejor = (total, cantidad, veces, cierran, evaluadas)

    return mejor


# ── El diámetro ────────────────────────────────────────────────────────────


def _buscar_diametro(
    columnas: dict[int, dict[int, float]],
    candidatas: list[int],
    tomadas: set[int],
) -> int | None:
    """
    La columna cuyos valores son todos diámetros que existen.

    No hay ecuación que lo confirme salvo el peso, que muchas planillas no traen
    o traen con el factor de pérdida ya aplicado. Lo que sí hay es una lista
    cerrada: el diámetro no se elige, se compra el que el mercado tiene. Una
    columna de catorce valores todos dentro de esa lista, y ninguna otra
    columna libre que cumpla lo mismo, es el diámetro.

    Si hay dos que cumplen, no se elige: devuelve `None` y la columna queda sin
    reconocer, a la vista. Entre una columna sin rótulo y un diámetro equivocado
    —que cambia el peso de toda la planilla— no hay comparación.
    """
    cumplen = [
        c
        for c in candidatas
        if c not in tomadas
        and len(columnas[c]) >= MINIMO_DE_FILAS
        and all(v in DIAMETROS for v in columnas[c].values())
    ]
    return cumplen[0] if len(cumplen) == 1 else None


# ── El código de posición ──────────────────────────────────────────────────


def _buscar_codigo(
    filas: list[list[str | None]],
    columnas: dict[int, dict[int, float]],
    tomadas: set[int],
) -> int | None:
    """
    La columna de POS.: enteros chicos, distintos en cada fila, y a la izquierda.

    Es la única que se decide sin ecuación, y se puede porque lo que la define es
    estructural: numera las filas. Se pide que sea casi toda distinta —una
    columna de diámetros repite, una de posiciones no— y se toma la de más a la
    izquierda, que es donde está en toda planilla impresa.
    """
    mejor: int | None = None
    for c in sorted(columnas):
        if c in tomadas:
            continue
        valores = columnas[c]
        if len(valores) < MINIMO_DE_FILAS or len(valores) * 2 < len(filas):
            continue
        if not all(abs(v - round(v)) < 0.001 and 0 < v <= 2000 for v in valores.values()):
            continue
        distintos = len({round(v) for v in valores.values()})
        if distintos * 1.0 < len(valores) * 0.9:
            continue
        mejor = c
        break
    return mejor


# ── La puerta de entrada ───────────────────────────────────────────────────


def inferir(filas: list[list[str | None]], estilo: str) -> Inferencia | None:
    """
    El reparto de papeles que las cuentas del documento confirman.

    `filas` son las filas de DATOS, sin el encabezado. Devuelve `None` cuando no
    encontró un reparto respaldado por la aritmética del propio documento, que es
    la respuesta correcta cuando no hay evidencia: dejar que las columnas queden
    sin reconocer y a la vista.
    """
    if len(filas) < MINIMO_DE_FILAS:
        return None

    columnas = _valores(filas, estilo)
    candidatas = _densas(columnas, len(filas))
    if len(candidatas) < 2:
        return None

    hallazgo = _buscar_medidas(filas, columnas, candidatas, len(filas))
    if hallazgo is None:
        return None
    medidas, i_largo, factor, cierran, evaluadas = hallazgo

    roles: dict[str, int] = {"unit_length": i_largo}
    tomadas = {i_largo, *medidas}
    evidencia = [
        f"la suma de {len(medidas)} columnas da la columna {i_largo} en {cierran} de "
        f"{evaluadas} filas" + (f" (× {factor:g})" if factor != 1 else "")
    ]

    total = _buscar_total(filas, columnas, candidatas, i_largo, tomadas)
    if total is not None:
        i_total, i_cantidad, i_veces, cierran_t, evaluadas_t = total
        roles["total_length"] = i_total
        tomadas.add(i_total)
        if i_cantidad is not None:
            roles["quantity"] = i_cantidad
            tomadas.add(i_cantidad)
        if i_veces is not None:
            roles["elements"] = i_veces
            tomadas.add(i_veces)
        evidencia.append(
            f"longitud × cantidad{' × veces' if i_veces is not None else ''} da la "
            f"columna {i_total} en {cierran_t} de {evaluadas_t} filas"
        )
        cierran += cierran_t
        evaluadas += evaluadas_t

    i_diametro = _buscar_diametro(columnas, candidatas, tomadas)
    if i_diametro is not None:
        roles["diameter"] = i_diametro
        tomadas.add(i_diametro)
        evidencia.append(f"la columna {i_diametro} trae sólo diámetros que existen")

    i_codigo = _buscar_codigo(filas, columnas, tomadas)
    if i_codigo is not None:
        roles["code"] = i_codigo
        tomadas.add(i_codigo)

    return Inferencia(
        roles=roles,
        medidas=medidas,
        factor_de_medidas=factor,
        filas_que_cierran=cierran,
        filas_evaluadas=evaluadas,
        evidencia=evidencia,
    )

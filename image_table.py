"""
Lectura de una planilla FOTOGRAFIADA o ESCANEADA.

── Por qué existe este módulo aparte ───────────────────────────────────────

`pdf_parser` lee un PDF digital: el texto ya es texto y las celdas ya son
trazos vectoriales con coordenadas exactas. No hay nada que adivinar.

Una imagen no tiene ninguna de las dos cosas. Son píxeles, y para sacar de ahí
una tabla hay que reconstruir a mano lo que `pdfplumber` entrega gratis: dónde
están las líneas de regla, qué celda delimita cada par de líneas, y qué dice
cada celda. Eso es lo único que hace este módulo.

Lo que NO hace es interpretar: devuelve exactamente la misma estructura que
`pdfplumber.Table.extract()` —una matriz de filas por columnas, con `None` en
las celdas vacías— y de ahí en adelante corre el MISMO código que el PDF. El
mapeo de columnas, la detección de unidad, el estilo numérico y la lectura de
filas no saben ni tienen por qué saber de dónde salió la matriz.

── La regla impresa es un requisito, no una preferencia ────────────────────

Igual que en el PDF, las celdas se delimitan con los trazos de la tabla. Podría
intentarse agrupar el texto por columnas a partir de sus coordenadas, y sería
peor de la peor manera: con una foto torcida, dos columnas angostas —«a (cm)» y
«b (cm)»— se mezclan, y el resultado es una planilla que parece bien leída con
las medidas cambiadas de lugar. Sin regla, este módulo falla en seco.

── Qué hace que una línea sea REGLA y no subrayado ─────────────────────────

Que empiece y termine en un borde de columna. No su largo.

El largo parecía el criterio obvio y estaba mal en las dos direcciones: dejaba
entrar el subrayado de un total y descartaba reglas de verdad. La que lo rompió
separa los dos pisos de este encabezado:

    POS. │ Ø │ TIPO │   DIMENSIONES (cm.)   │ LONG. │ CANT. │ …
         │   │      │  a │ b │ c │ d │ e    │ (m.)  │       │

Existe SÓLO debajo de esas cinco columnas —cruza el 15 % del ancho— así que un
piso mínimo del 30 % la descartaba, los dos pisos quedaban en una sola celda con
«DIMENSIONES (cm.)» y las letras juntas, y el documento entero se caía con
NO_HEADER sin una sola fila leída.

Por eso los bordes de columna se detectan PRIMERO, con sus propias líneas
largas, y las horizontales se juzgan contra ellos.

── Por qué algunas celdas se leen dos veces ────────────────────────────────

El detector de texto propone regiones sobre la página completa, y ahí un
CARÁCTER SUELTO en una celda grande no acumula la evidencia que acumula una
palabra. Medido: de los rótulos «a b c d e» encontraba tres y perdía dos, y el
«Ø» de la columna de diámetro no lo veía nunca.

Un encabezado sin leer no descarta una celda: descarta una COLUMNA entera, en
silencio. Así que las celdas vacías de las primeras filas se recortan y se leen
aparte, ampliadas, con un umbral mucho más bajo — porque ahí la pregunta ya no
es «¿dónde hay texto?» sino «¿qué dice esta celda?».

Ese umbral bajo tiene un precio y se midió: si se baja demasiado, el
reconocedor —cuyo modelo es chino-inglés— devuelve ideogramas leídos de celdas
vacías. En una planilla boliviana eso no es una lectura difícil, es una
invención; de ahí `_es_texto_plausible`, que descarta lo que no entra en el
alfabeto de una planilla.

── Por qué el clasificador de ángulo va APAGADO ────────────────────────────

RapidOCR trae un clasificador que decide si un recorte está dado vuelta 180°.
En una tabla de números eso es una fuente de errores y no una ayuda: se midió
contra la planilla piloto y leyó «999» como «666» y «6» como «9» —que es
exactamente lo que pasa al rotar esos dígitos— en un documento perfectamente
derecho. Una celda de una planilla nunca está boca abajo; si la foto entera lo
está, lo que hay que rotar es la foto.

── Lo que sí se lee girado ─────────────────────────────────────────────────

La columna de sección («REFUERZO LONGITUDINAL») viene escrita en vertical en
una celda combinada, alta y angosta. Esas celdas se recortan y se leen aparte,
probando los dos sentidos de giro y quedándose con el que el OCR puntúa mejor.
"""

from __future__ import annotations

import io
import math
import re

import cv2
import numpy as np
from PIL import Image, ImageOps

# ── Topes ──────────────────────────────────────────────────────────────────
#
# Una foto de celular moderna tiene 50 megapíxeles. Decodificarla son 150 MB en
# RAM antes de tocarla, y el OCR no lee mejor por eso: el detector trabaja sobre
# una versión reducida igual. Se acota la entrada y se normaliza el trabajo.

MAX_PIXELES_ENTRADA = 40_000_000
"""Más que esto no se decodifica. Es el tope de memoria, no de calidad."""

LADO_LARGO_OBJETIVO = 2400
"""
A cuánto se lleva el lado largo antes de leer.

Medido sobre la planilla piloto: por debajo de ~1600 px de ancho los dígitos de
las columnas angostas empiezan a confundirse, y por encima de ~2600 el OCR
tarda el doble sin leer nada nuevo.
"""

MAX_AMPLIACION = 1.45
"""
Cuánto se puede AMPLIAR una imagen chica antes de leerla.

Reducir no tiene tope —hay detalle de sobra y `LADO_LARGO_OBJETIVO` es el punto
donde el reconocedor deja de leer más por mirar más grande—, pero ampliar sí,
porque la interpolación no agrega detalle: agranda lo que hay, y pasado cierto
punto lo único que agrega es borrosidad en los trazos finos.

Medido sobre la réplica del documento de obra a 1000 px, variando el tamaño al
que se le presenta la tabla al reconocedor:

    objetivo   filas   cantidades
      1000      9/14      8/14      (sin ampliar)
      1200      7/14      6/14
      1400     13/14     12/14      ← el mejor
      1600     12/14     12/14
      1800     11/14     11/14
      2400     11/14     10/14

Ninguno cambió ni desordenó una medida; lo que cambia es cuántas se recuperan.
De ahí el tope: 1,45 deja una entrada de 1000 px en unos 1450, arriba del óptimo
medido, y no toca a las imágenes grandes, que siguen reduciéndose a 2400.

El número sale de UN documento a UNA resolución, que es toda la evidencia que
hay. Lo que sí es general es la razón: la escala a la que el reconocedor lee
mejor es una propiedad del modelo, no del archivo, y ampliar sin tope la deja
atrás.
"""

LADO_LARGO_MINIMO = 300
"""
Por debajo de esto no se agranda: no hay tabla que valga la pena buscar.

── Por qué era 1000, y por qué eso costaba celdas ─────────────────────────

Decía «por debajo de mil píxeles no se agranda: no hay información que
recuperar». Lo primero es cierto —ampliar no crea información— y lo segundo no
viene al caso: el reconocedor tiene una altura de texto preferida, y presentarle
la MISMA información a esa altura no inventa nada.

El resultado era que el guardarraíl se activaba justo en los documentos que más
ayuda necesitan. El de obra que trajo el usuario mide exactamente 1000 px, así
que caía del lado de «no agrandar» y se le leía a 1000. Medido sobre la réplica
a esa misma resolución:

    entrada   sin agrandar              agrandada a 2400
    1000 px   9 filas, 8 cantidades     11 filas, 10 cantidades
     900 px   6 filas, 6 cantidades      8 filas,  7 cantidades

Y en las dos, ninguna medida cambiada ni desordenada — sólo celdas recuperadas,
que es el único intercambio que este módulo acepta.

No contradice lo de Lanczos, que sigue descartado: ahí la pregunta era con qué
interpolación ampliar, y la respuesta medida fue la cúbica. Acá la pregunta es
si ampliar, y para eso 300 px es el piso de «esto ni es una planilla».
"""

MAX_TABLAS = 8
"""Una planilla fotografiada trae la tabla de posiciones y a lo sumo el resumen."""


# ── El piso de resolución ──────────────────────────────────────────────────
#
# La medida que importa no es el tamaño de la imagen sino cuántos píxeles tiene
# cada COLUMNA en el archivo original: una tabla de dos columnas en 1000 px se
# lee perfecto y una de diecisiete no. Y se mide sobre el original porque lo que
# se perdió al reducir es nitidez, y ampliar después no la devuelve.
#
# Los dos umbrales salen de medir dos planillas —la piloto de 17 columnas y una
# de 14— reducidas a varios anchos, contando cuántos valores se PIERDEN y
# cuántos se CAMBIAN:
#
#     px/columna    perdidos        CAMBIADOS
#     ~105             0               0
#     ~68              0 a 14          0
#     ~48             21 a 32          0 a 1
#     ~39             40               3
#
# Perder celdas es tolerable: una medida en blanco se ve. Cambiarlas no.
#
# ── Por qué esto NO es un rechazo por sí solo ──────────────────────────────
#
# Porque midiendo QUÉ se cambia apareció algo que el umbral no describe bien. A
# ~52 px por columna el rótulo «b» del encabezado se lee «d»: la columna de «b»
# queda etiquetada «d», la «d» de verdad se descarta por repetida, y las medidas
# de las catorce filas se reparten entre letras equivocadas. Medido:
#
#     px/columna    letras leídas            filas con una medida en otra letra
#     ~102          a b c d e                 0
#     ~68           a b c d e                 0
#     ~52           a d c e  (falta «b»)      8 de 14
#
# Eso es peor que un dígito cambiado y ningún umbral de resolución lo atrapa:
# la suma de las medidas no cambia, así que la longitud cierra, el total cierra,
# el peso cierra, y la barra sale doblada en otro orden. Un recálculo no lo ve.
#
# Lo que sí lo ve es el propio encabezado: las letras de una planilla son
# SIEMPRE una tira seguida desde «a», creciendo de izquierda a derecha. «a d c e»
# viola las dos cosas y se puede rechazar sin saber nada de píxeles — eso está
# en `pdf_parser._problema_de_las_letras`, y vale a cualquier resolución.
#
# Con ese modo detectado de frente, quedó medido que de los valores que sí se
# cambian no sobrevive ninguno al recálculo por filas que el importador ya hace.
# Así que por debajo de este piso la imagen no se rechaza de entrada: se rechaza
# si la planilla NO se puede verificar a sí misma (ver `parse_imagen`).

PX_POR_COLUMNA_MINIMO = 60
"""
Por debajo de esto la planilla tiene que poder verificarse a sí misma.

No es un rechazo directo: es la condición que lo dispara. Ver el bloque de
arriba y `pdf_parser.parse_imagen`.
"""

PX_POR_COLUMNA_COMODO = 90
"""Por debajo de esto se lee, y se avisa que van a faltar celdas."""


# ── Calidad de la lectura ──────────────────────────────────────────────────

PUNTAJE_DUDOSO = 0.80
"""
Por debajo de este puntaje, la celda se reporta como dudosa.

No se DESCARTA. Descartar una celda que el OCR leyó al 60 % la haría
desaparecer de la pantalla, y una medida ausente se nota mucho menos que una
medida rara: lo que hay que hacer es mostrarla y decir que se dude de ella.
"""

MAX_CELDAS_DUDOSAS_LISTADAS = 12
"""Un aviso que enumera doscientas celdas no lo lee nadie."""

MAX_RELECTURAS = 60
"""
Cuántas celdas se releen como máximo, por tabla.

Cada relectura es una pasada de OCR sobre un recorte: unas decenas de
milisegundos. Sólo se releen celdas de encabezado, así que en una planilla
normal son un puñado; el tope está para que una tabla de cien columnas no
convierta la lectura en un trabajo de minutos.
"""

FILAS_DE_ENCABEZADO = 3
"""
Cuántas filas de arriba se tratan como posible encabezado.

Es el mismo número que usa `_buscar_encabezado` para mirar el principio de la
tabla, y por la misma razón: arriba de los rótulos puede haber un título a todo
el ancho, y los rótulos mismos pueden ocupar dos pisos.
"""


class ImagenIlegible(Exception):
    """La imagen llegó bien y no se pudo sacar una tabla de ella."""

    def __init__(self, code: str, mensaje: str) -> None:
        super().__init__(mensaje)
        self.code = code


# ── El motor de OCR ────────────────────────────────────────────────────────
#
# Se construye una sola vez y a demanda. Cargar los modelos ONNX son unos
# segundos y unos 100 MB; hacerlo al importar el módulo le pagaría ese costo a
# un worker que quizá sólo reciba PDFs en toda su vida.

_motor = None


def motor_ocr():
    global _motor
    if _motor is None:
        from rapidocr_onnxruntime import RapidOCR

        _motor = RapidOCR()
    return _motor


# ── Entrada ────────────────────────────────────────────────────────────────


def abrir_imagen_con_escala(data: bytes) -> tuple[np.ndarray, float]:
    """
    Los bytes, como imagen en escala de grises y con el tamaño de trabajo, y
    cuánto se la escaló para llegar ahí.

    La escala hace falta para juzgar la resolución del ORIGINAL: todo lo que
    viene después trabaja sobre la imagen normalizada, donde una planilla de
    1000 px y una de 2400 se ven del mismo tamaño y no leen igual.

    Se usa Pillow y no `cv2.imdecode` por una sola razón: la orientación EXIF.
    Una foto sacada con el teléfono de costado viene con los píxeles en el
    orden original y una etiqueta que dice cómo hay que girarla; OpenCV ignora
    esa etiqueta y devolvería la planilla acostada.
    """
    try:
        imagen = Image.open(io.BytesIO(data))
        imagen.load()
    except Exception as e:
        raise ImagenIlegible(
            "CORRUPT_IMAGE",
            "el archivo dice ser una imagen pero no se pudo abrir; puede estar "
            "truncado. Volvé a sacar la foto o a exportarla e intentá de nuevo.",
        ) from e

    if imagen.width * imagen.height > MAX_PIXELES_ENTRADA:
        raise ImagenIlegible(
            "IMAGE_TOO_LARGE",
            f"la imagen tiene {imagen.width}×{imagen.height} píxeles y es demasiado "
            "grande para procesarla. Sacá la foto con menos resolución o reducila "
            "antes de subirla.",
        )

    imagen = ImageOps.exif_transpose(imagen)
    gris = np.array(imagen.convert("L"))
    de_trabajo = _a_tamano_de_trabajo(gris)
    escala = max(de_trabajo.shape) / max(gris.shape)
    return de_trabajo, escala


def abrir_imagen(data: bytes) -> np.ndarray:
    """La imagen sola, para quien no necesita saber cuánto se escaló."""
    return abrir_imagen_con_escala(data)[0]


def _a_tamano_de_trabajo(gris: np.ndarray) -> np.ndarray:
    alto, ancho = gris.shape
    lado = max(alto, ancho)
    if lado <= LADO_LARGO_MINIMO or lado == LADO_LARGO_OBJETIVO:
        return gris

    escala = LADO_LARGO_OBJETIVO / lado
    # Ampliar tiene un tope que reducir no tiene. Ver `MAX_AMPLIACION`.
    escala = min(escala, MAX_AMPLIACION) if escala > 1 else escala
    if abs(escala - 1.0) < 0.02:
        return gris
    # `INTER_AREA` al reducir y `INTER_CUBIC` al ampliar: reducir con
    # interpolación cúbica deja los trazos finos de la regla con aliasing, y
    # esos trazos son justamente lo que define las celdas.
    #
    # Se probó Lanczos al ampliar, porque recuperaba dos o tres celdas más en
    # una planilla de baja resolución. Se descartó: en la foto degradada de la
    # planilla piloto hacía que un «6» se leyera «8». Ganar celdas a cambio de
    # cambiar un valor es exactamente el intercambio que este módulo no acepta,
    # y no hay tercera opción — se midieron las dos.
    interp = cv2.INTER_AREA if escala < 1 else cv2.INTER_CUBIC
    return cv2.resize(gris, (round(ancho * escala), round(alto * escala)), interpolation=interp)


# ── Enderezado ─────────────────────────────────────────────────────────────


def enderezar(gris: np.ndarray) -> np.ndarray:
    """
    La imagen, con la regla de la tabla horizontal y vertical.

    Una foto sacada a mano nunca está derecha, y dos grados de inclinación
    alcanzan para que la línea que separa «a (cm)» de «b (cm)» cruce de una
    columna a la otra a lo largo de la página.

    Se corrige el GIRO y no la perspectiva. Enderezar la perspectiva pide
    encontrar los cuatro vértices de la tabla, y cuando esa búsqueda se
    equivoca —un marco de la hoja, la sombra del borde de la mesa— el resultado
    es una tabla deformada que parece correcta: peor que no corregir nada. El
    giro, en cambio, se mide sobre las líneas de regla que ya se detectaron, así
    que si no hay regla no hay corrección y no hay invención.
    """
    angulo = _angulo_dominante(gris)
    if angulo is None or abs(angulo) < 0.15:
        return gris

    alto, ancho = gris.shape
    centro = (ancho / 2, alto / 2)
    m = cv2.getRotationMatrix2D(centro, angulo, 1.0)
    return cv2.warpAffine(
        gris,
        m,
        (ancho, alto),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )


def _angulo_dominante(gris: np.ndarray) -> float | None:
    """
    Cuánto está girada la imagen, en grados, según sus líneas más largas.

    Se mira sólo el entorno de la horizontal (±10°). Una planilla girada 30°
    no es una planilla girada: es una foto mal sacada, y corregir 30° a partir
    de líneas sueltas produce basura con mucha confianza.
    """
    bordes = cv2.Canny(gris, 50, 150, apertureSize=3)
    minimo = max(60, min(gris.shape) // 4)
    segmentos = cv2.HoughLinesP(
        bordes, 1, math.pi / 720, threshold=120, minLineLength=minimo, maxLineGap=10
    )
    if segmentos is None:
        return None

    angulos: list[float] = []
    pesos: list[float] = []
    # `HoughLinesP` devuelve (N, 1, 4) en OpenCV 4 y (N, 4) en OpenCV 5.
    for x1, y1, x2, y2 in np.asarray(segmentos).reshape(-1, 4):
        largo = math.hypot(x2 - x1, y2 - y1)
        grados = math.degrees(math.atan2(y2 - y1, x2 - x1))
        # Las verticales aportan el mismo giro, medido a 90°.
        if abs(abs(grados) - 90) <= 10:
            grados = grados - 90 if grados > 0 else grados + 90
        if abs(grados) > 10:
            continue
        angulos.append(grados)
        pesos.append(largo)

    if not angulos:
        return None
    # Mediana ponderada por largo: una línea de regla de media página pesa más
    # que el borde de un croquis, y la mediana no la mueve un puñado de
    # segmentos espurios.
    orden = np.argsort(angulos)
    a = np.array(angulos)[orden]
    p = np.cumsum(np.array(pesos)[orden])
    return float(a[int(np.searchsorted(p, p[-1] / 2))])


# ── La regla ───────────────────────────────────────────────────────────────


def _binarizar(gris: np.ndarray) -> np.ndarray:
    """
    Blanco sobre negro, con umbral local.

    Local y no global: la foto de una planilla tiene una esquina más iluminada
    que la otra, y un umbral único deja media tabla en negro.
    """
    return cv2.adaptiveThreshold(
        gris, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 10
    )


def _mascaras_de_regla(binaria: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Los trazos horizontales y los verticales, por separado."""
    alto, ancho = binaria.shape
    horizontal = cv2.morphologyEx(
        binaria,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, ancho // 60), 1)),
    )
    vertical = cv2.morphologyEx(
        binaria,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, alto // 60))),
    )
    return horizontal, vertical


FRACCION_PARA_RECORTAR = 0.8
"""
Cuánto del tamaño de trabajo tiene que ocupar la tabla para no recortar.

Por debajo de esto conviene recortar y volver a escalar: la tabla se está
llevando sólo una parte de los píxeles que el reconocedor va a mirar.
"""

MARGEN_DE_RECORTE = 12
"""
Píxeles que se dejan alrededor de la tabla al recortar.

La regla exterior ES parte de la tabla: sin margen, el recorte puede cortarla
justo encima y entonces la detección de celdas pierde la columna del borde.
"""


def _recortar_a_la_tabla(
    gris: np.ndarray,
    bloques: list[tuple[int, int, int, int]],
    ampliacion_ya_usada: float,
) -> tuple[np.ndarray, float] | None:
    """
    La imagen recortada a la tabla y vuelta a escalar, si eso gana píxeles.

    ── Por qué esto lo hace el lector y no la persona ─────────────────────

    Porque la pregunta «¿tengo que recortar la foto a la tabla?» tiene una
    respuesta que el lector ya sabe: encontró la tabla él mismo, con las líneas
    de la regla. Pedirle a alguien que recorte a mano lo que el programa ya
    localizó es trabajo manual para arreglar una decisión del programa.

    Y la decisión estaba ahí: la imagen entera se lleva a `LADO_LARGO_OBJETIVO`
    ANTES de buscar la tabla, así que una tabla que ocupa un tercio del cuadro
    se queda con un tercio de esos píxeles. Medido sobre la réplica del
    documento de obra, la misma tabla de 1000 px dentro de un cuadro 2,5 veces
    más grande —una foto sacada de lejos— dejaba de leerse entera.

    Lo que hace: recorta al rectángulo que contiene las tablas, con margen para
    no comerse la regla exterior, y lo vuelve a escalar al tamaño de trabajo.
    Devuelve `None` cuando la tabla ya ocupa el cuadro, que es el caso de una
    planilla escaneada y también el de la foto que el usuario subió — ahí no hay
    nada que ganar y volver a escalar sería trabajo perdido.
    """
    if not bloques:
        return None

    alto, ancho = gris.shape
    x0 = max(0, min(b[0] for b in bloques) - MARGEN_DE_RECORTE)
    y0 = max(0, min(b[1] for b in bloques) - MARGEN_DE_RECORTE)
    x1 = min(ancho, max(b[0] + b[2] for b in bloques) + MARGEN_DE_RECORTE)
    y1 = min(alto, max(b[1] + b[3] for b in bloques) + MARGEN_DE_RECORTE)

    if x1 - x0 < 50 or y1 - y0 < 50:
        return None

    # ¿La tabla ya se lleva los píxeles? Entonces no hay nada que ganar.
    if max(x1 - x0, y1 - y0) >= max(alto, ancho) * FRACCION_PARA_RECORTAR:
        return None

    recorte = gris[y0:y1, x0:x1]

    # ── El tope de ampliación es ACUMULADO, no por paso ────────────────────
    #
    # Esta imagen ya viene ampliada de `abrir_imagen_con_escala`, y volver a
    # ampliarla acá la haría pasar dos veces por la interpolación. Medido: una
    # tabla de 1000 px dentro de un cuadro de 1600 se leía en 14 filas con una
    # sola ampliación y en 12 con dos, para el mismo tamaño final. Interpolar lo
    # interpolado no agrega detalle y sí agrega borrosidad.
    #
    # Así que lo que se permite acá es lo que FALTA para llegar al tope contado
    # desde el archivo original. Si ya se gastó, el recorte se hace igual —
    # acercarse a la tabla sirve por sí solo— pero sin volver a escalar.
    lado = max(recorte.shape)
    permitido = MAX_AMPLIACION / max(ampliacion_ya_usada, 1.0)
    factor = min(LADO_LARGO_OBJETIVO / lado, permitido)
    if factor <= 1.02:
        return recorte, 1.0

    reescalado = cv2.resize(
        recorte, (round(recorte.shape[1] * factor), round(recorte.shape[0] * factor)),
        interpolation=cv2.INTER_CUBIC,
    )
    # La escala que se devuelve es la del RECORTE contra lo que se recortó, para
    # que quien la acumule siga midiendo píxeles del archivo original.
    return reescalado, max(reescalado.shape) / lado


def _px_por_columna(xs: list[float], escala: float) -> float:
    """
    El ancho de una columna en píxeles del archivo ORIGINAL.

    La mediana y no el promedio: en una planilla hay una columna de croquis que
    mide cuatro veces lo que las demás, y el promedio la seguiría a ella.
    """
    anchos = [b - a for a, b in zip(xs, xs[1:])]
    return float(np.median(anchos)) / max(escala, 1e-9)


def _bloques_de_tabla(
    horizontal: np.ndarray, vertical: np.ndarray, forma: tuple[int, int]
) -> list[tuple[int, int, int, int]]:
    """
    Las regiones de la imagen que son una tabla, en orden de lectura.

    Una planilla trae la tabla de posiciones y, abajo o al lado, el resumen de
    material. Son tablas distintas con anchos distintos, y el lector de más
    arriba ya las distingue por eso — pero tienen que llegarle separadas.
    """
    alto, ancho = forma
    unidas = cv2.dilate(cv2.bitwise_or(horizontal, vertical), np.ones((3, 3), np.uint8), iterations=2)
    _, _, stats, _ = cv2.connectedComponentsWithStats(unidas, 8)

    bloques = [
        (int(s[0]), int(s[1]), int(s[2]), int(s[3]))
        for s in stats[1:]
        # Una tabla ocupa una parte sustancial de la hoja. El piso descarta el
        # recuadro de una firma o el marco de un logo sin descartar el resumen
        # de material, que es angosto pero alto.
        if s[2] * s[3] > ancho * alto * 0.01 and s[2] > ancho * 0.1 and s[3] > alto * 0.04
    ]
    bloques.sort(key=lambda b: (b[1], b[0]))
    return bloques[:MAX_TABLAS]


TOLERANCIA_CORTE = 8
"""
Cuántos píxeles de separación siguen siendo la misma línea.

Una línea impresa tiene dos o tres píxeles de grosor, y la foto o el escaneo la
pueden partir en dos componentes desalineados.
"""


def _agrupar(valores: list[float], tolerancia: int = TOLERANCIA_CORTE) -> list[float]:
    if not valores:
        return []
    valores = sorted(valores)
    grupos: list[float] = []
    actual = [valores[0]]
    for v in valores[1:]:
        if v - actual[-1] <= tolerancia:
            actual.append(v)
        else:
            grupos.append(sum(actual) / len(actual))
            actual = [v]
    grupos.append(sum(actual) / len(actual))
    return grupos


def _cortes_verticales(vertical: np.ndarray, bloque: tuple[int, int, int, int]) -> list[float]:
    """
    Los bordes de columna del bloque.

    Se piden largas —un tercio de la altura— porque las columnas de una planilla
    se separan de arriba abajo. Las verticales cortas que aparecen adentro de un
    croquis no son bordes de columna y no tienen que inventar una.
    """
    x, y, ancho, alto = bloque
    sub = vertical[y : y + alto, x : x + ancho]
    _, _, stats, _ = cv2.connectedComponentsWithStats(sub, 8)

    crudos = [
        x + int(s[0]) + int(s[2]) / 2
        for s in stats[1:]
        if int(s[3]) >= alto * 0.3
    ]
    return _agrupar(crudos)


def _cortes_horizontales(
    horizontal: np.ndarray, bloque: tuple[int, int, int, int], xs: list[float]
) -> list[float]:
    """
    Los bordes de fila del bloque, incluidos los que cruzan UNA sola columna.

    ── Por qué no se filtran por largo ────────────────────────────────────

    Porque el largo no distingue una regla de un subrayado, y sí descarta
    reglas de verdad. El caso que lo rompió es un encabezado de dos pisos:

        POS. │ Ø │ TIPO │   DIMENSIONES (cm.)   │ LONG. │ …
             │   │      │  a │ b │ c │ d │ e    │       │

    La línea que separa «DIMENSIONES» de las letras existe SÓLO debajo de esas
    cinco columnas: cruza el 15 % del ancho de la tabla. Con un piso del 30 %
    se descartaba, los dos pisos quedaban en una sola celda —«DIMENSIONES
    (cm.)» y las letras juntas— y el documento entero se caía con NO_HEADER.

    ── Qué se usa en su lugar ─────────────────────────────────────────────

    Que la línea EMPIECE Y TERMINE en un borde de columna, que es lo que hace
    que una línea sea regla y no subrayado. El subrayado de un número arranca y
    muere en mitad de la celda; la regla va de un borde al otro.

    Es un criterio más fuerte que el largo y además más barato de justificar:
    los bordes de columna ya están detectados, con sus propias líneas largas.

    Lo que sobre de acá no hace daño: `_armar_spans` comprueba celda por celda
    si el trazo existe de verdad en ese tramo, así que una fila que sólo cruza
    cinco columnas deja las otras nueve combinadas con la de abajo — que es
    exactamente lo que el papel dibuja.
    """
    x, y, ancho, alto = bloque
    sub = horizontal[y : y + alto, x : x + ancho]
    _, _, stats, _ = cv2.connectedComponentsWithStats(sub, 8)

    if len(xs) < 2:
        return []

    # La columna más angosta marca el mínimo: una regla cruza al menos una.
    columna_minima = min(b - a for a, b in zip(xs, xs[1:]))
    borde = max(TOLERANCIA_CORTE, int(ancho * 0.01))

    def en_un_borde(v: float) -> bool:
        return any(abs(v - corte) <= borde for corte in xs)

    crudos: list[float] = []
    for s in stats[1:]:
        sx, sy, sw, sh = int(s[0]), int(s[1]), int(s[2]), int(s[3])
        izquierda, derecha = x + sx, x + sx + sw
        if sw < columna_minima * 0.85:
            continue
        if not (en_un_borde(izquierda) and en_un_borde(derecha)):
            continue
        crudos.append(y + sy + sh / 2)

    return _agrupar(crudos)


def _hay_trazo(mask: np.ndarray, eje: str, fijo: float, desde: float, hasta: float) -> bool:
    """
    ¿Existe de verdad la línea que separa estas dos celdas?

    Es lo que distingue una celda COMBINADA de dos celdas vacías. La columna de
    sección de una planilla es una sola celda que abarca dieciséis filas; sin
    esta comprobación, la grilla rectangular la partiría en dieciséis, el texto
    caería en la del medio y las quince restantes quedarían sin sección.
    """
    a, b = int(round(min(desde, hasta))), int(round(max(desde, hasta)))
    if b - a < 4:
        return True

    # Una banda de ±2 px: la línea puede no caer exactamente en el centro
    # calculado, sobre todo si la imagen se enderezó.
    c = int(round(fijo))
    if eje == "h":
        banda = mask[max(0, c - 2) : c + 3, a:b]
    else:
        banda = mask[a:b, max(0, c - 2) : c + 3]
    if banda.size == 0:
        return False

    presencia = (banda.max(axis=0 if eje == "h" else 1) > 0).mean()
    # 0,6 y no 0,9: una línea impresa se corta donde la cruza un número, y en
    # una foto se corta donde hay un reflejo.
    return bool(presencia >= 0.6)


# ── La grilla ──────────────────────────────────────────────────────────────


class _Span:
    """Una celda, con las filas y columnas que abarca."""

    __slots__ = ("fila", "columna", "filas", "columnas", "x0", "y0", "x1", "y1")

    def __init__(self, fila: int, columna: int, filas: int, columnas: int,
                 x0: float, y0: float, x1: float, y1: float) -> None:
        self.fila, self.columna = fila, columna
        self.filas, self.columnas = filas, columnas
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1


def _armar_spans(
    xs: list[float], ys: list[float], horizontal: np.ndarray, vertical: np.ndarray
) -> list[_Span]:
    """
    La grilla, con sus celdas combinadas resueltas.

    Se recorre de arriba a abajo y de izquierda a derecha; cada celda que
    todavía no fue absorbida por otra crece hacia la derecha mientras no
    encuentre trazo vertical, y hacia abajo mientras no encuentre trazo
    horizontal en todo su ancho.
    """
    n_filas, n_columnas = len(ys) - 1, len(xs) - 1
    tomada = [[False] * n_columnas for _ in range(n_filas)]
    spans: list[_Span] = []

    for r in range(n_filas):
        for c in range(n_columnas):
            if tomada[r][c]:
                continue

            ancho = 1
            while c + ancho < n_columnas and not _hay_trazo(
                vertical, "v", xs[c + ancho], ys[r], ys[r + 1]
            ):
                ancho += 1

            alto = 1
            while r + alto < n_filas and not any(
                _hay_trazo(horizontal, "h", ys[r + alto], xs[c + k], xs[c + k + 1])
                for k in range(ancho)
            ):
                alto += 1

            for dr in range(alto):
                for dc in range(ancho):
                    tomada[r + dr][c + dc] = True

            spans.append(
                _Span(r, c, alto, ancho, xs[c], ys[r], xs[c + ancho], ys[r + alto])
            )

    return spans


# ── Texto ──────────────────────────────────────────────────────────────────


class _Caja:
    """Un trozo de texto leído, con su recuadro y su puntaje."""

    __slots__ = ("x0", "y0", "x1", "y1", "texto", "puntaje")

    def __init__(self, x0: float, y0: float, x1: float, y1: float, texto: str, puntaje: float) -> None:
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1
        self.texto, self.puntaje = texto, puntaje


UMBRAL_DE_CAJA = 0.35
"""
Cuánto tiene que «verse» una región para que el detector la proponga como texto.

El default de RapidOCR es 0,5 y está calibrado para prosa. Una planilla tiene
celdas con un solo dígito —la columna «No.» dice «2»— y un dígito suelto en una
celda grande no acumula la evidencia que acumula una palabra: medido sobre la
planilla piloto, con 0,5 el detector pierde la cantidad de una fila, y con 0,35
la encuentra sin agregar una sola caja falsa. Por debajo de 0,3 empiezan a
aparecer los trazos del croquis como si fueran texto.
"""


UMBRAL_DE_CAJA_EN_CELDA = 0.30
"""
El mismo umbral, para una celda leída sola.

Mucho más bajo, y no es una contradicción: en la página completa un umbral bajo
deja entrar los trazos de un croquis como si fueran texto, porque no hay nada
que diga dónde puede haber texto. Acá sí lo hay — es una celda, ya delimitada
por la regla, elegida porque la pasada de la página completa no encontró nada en
ella. La pregunta deja de ser «¿dónde hay texto?» y pasa a ser «¿qué dice ESTA
celda?», que admite mucha más tolerancia.

Medido sobre esta planilla: el rótulo «Ø» de la columna de diámetro no aparece
con el umbral de la página, y con éste aparece con 0,40 de puntaje — es decir,
marcado como dudoso, que es exactamente lo que corresponde. Sin esto la columna
quedaba sin rótulo y sus valores se descartaban en silencio.

Bajarlo MÁS no es gratis y se midió: a 0,15 el reconocedor empezó a devolver
caracteres chinos leídos de celdas vacías. De ahí `_es_texto_plausible`.
"""

PUNTAJE_MINIMO_EN_CELDA = 0.30
"""
Y el puntaje mínimo de reconocimiento, para la misma pasada.

RapidOCR descarta por su cuenta lo que lee por debajo de 0,5, y el «Ø» sale con
0,40: sin bajar esto, la caja se detectaba y el texto se tiraba igual.
"""

# Los caracteres que una planilla de fierros puede tener escritos.
#
# ── Por qué existe esta lista ──────────────────────────────────────────────
#
# Porque el modelo de reconocimiento es chino-inglés, y cuando se lo fuerza a
# leer una celda casi vacía devuelve el carácter que más se parece a la mancha
# que encontró — que puede ser «一», que es un trazo horizontal. En una planilla
# boliviana un ideograma no es una lectura difícil: es una invención, y este
# módulo no inventa.
#
# La lista es generosa a propósito: letras con tilde, el símbolo de diámetro, el
# de grado, la «×» de «2x24», y toda la puntuación que aparece en un rótulo.
RE_TEXTO_PLAUSIBLE = re.compile(
    r"^[A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9\s"
    r"ØøΦφ°ºª"
    r"+\-*/=<>%$#&@_·.,:;!¡?¿'\"`^~|()\[\]{}"
    r"×–—]+$"
)


def _es_texto_plausible(texto: str) -> bool:
    """¿Esto es algo que la planilla puede tener escrito, o es una invención?"""
    return bool(RE_TEXTO_PLAUSIBLE.match(texto))


def _leer_texto(
    gris: np.ndarray,
    umbral: float = UMBRAL_DE_CAJA,
    puntaje_minimo: float | None = None,
) -> list[_Caja]:
    motor = motor_ocr()
    resultado, _ = (
        motor(gris, use_cls=False, box_thresh=umbral)
        if puntaje_minimo is None
        else motor(gris, use_cls=False, box_thresh=umbral, text_score=puntaje_minimo)
    )
    cajas: list[_Caja] = []
    for recuadro, texto, puntaje in resultado or []:
        limpio = (texto or "").strip()
        if not limpio or not _es_texto_plausible(limpio):
            continue
        equis = [p[0] for p in recuadro]
        yes = [p[1] for p in recuadro]
        cajas.append(_Caja(min(equis), min(yes), max(equis), max(yes), limpio, float(puntaje)))
    return cajas


def _texto_de_span(span: _Span, cajas: list[_Caja]) -> tuple[str | None, float]:
    """
    Lo que dice una celda, y con cuánta confianza.

    Una caja pertenece a la celda que contiene su CENTRO. El centro y no una
    esquina: el recuadro que devuelve el detector es un poco más grande que las
    letras y suele morder la línea de al lado, así que por esquinas casi
    cualquier celda reclamaría a la caja de su vecina.
    """
    dentro = [
        b
        for b in cajas
        if span.x0 <= (b.x0 + b.x1) / 2 < span.x1 and span.y0 <= (b.y0 + b.y1) / 2 < span.y1
    ]
    if not dentro:
        return None, 1.0

    # En orden de lectura. Un encabezado de tres renglones —«LONG. / TOTAL + /
    # PERDIDA / (cm)»— tiene que llegar junto y en orden, porque el mapeo de
    # columnas lo compara normalizado contra su diccionario.
    dentro.sort(key=lambda b: (round(b.y0 / 12), b.x0))
    return "\n".join(b.texto for b in dentro), min(b.puntaje for b in dentro)


ALTO_MINIMO_DE_CELDA = 56
"""
A cuánto se lleva la altura de una celda recortada antes de leerla sola.

El detector de texto tiene un tamaño por debajo del cual no propone regiones, y
las celdas de una planilla son chicas: la de la columna «TIPO» mide 52 px de
alto a la resolución de trabajo. Ampliarla no agrega información —no la puede
agregar— pero la pone en el rango de tamaños con el que el detector fue
entrenado.
"""


def _recortar(gris: np.ndarray, span: _Span) -> np.ndarray | None:
    """La celda sola, sin sus bordes de regla y con tamaño de lectura."""
    recorte = gris[
        max(0, int(span.y0) + 2) : int(span.y1) - 2,
        max(0, int(span.x0) + 2) : int(span.x1) - 2,
    ]
    if recorte.size == 0 or min(recorte.shape) < 8:
        return None

    if recorte.shape[0] < ALTO_MINIMO_DE_CELDA:
        escala = ALTO_MINIMO_DE_CELDA / recorte.shape[0]
        recorte = cv2.resize(recorte, None, fx=escala, fy=escala, interpolation=cv2.INTER_CUBIC)

    # Un margen de papel alrededor: el detector propone cajas con holgura y una
    # letra pegada al borde le queda partida. El valor del margen sale de la
    # propia celda —su percentil 90 es su papel— para no inventar un blanco que
    # en una foto con sombra sería un salto de contraste.
    return cv2.copyMakeBorder(
        recorte, 16, 16, 16, 16, cv2.BORDER_CONSTANT, value=int(np.percentile(recorte, 90))
    )


AREA_MINIMA_DE_TINTA = 6
"""
Cuántos píxeles seguidos de tinta hacen que valga la pena releer una celda.

Es una PUERTA, no un recorte: sin ella, la relectura —que usa un umbral de
detección mucho más bajo que la de la página— se le pediría también a celdas
vacías, y ahí el reconocedor devuelve el carácter que más se parece a la mancha
que encuentre. Un «1» inventado en una celda en blanco es exactamente la clase
de error que no se ve.

Con la puerta, la relectura sólo se le pide a celdas que tienen algo escrito, y
la pregunta pasa a ser «¿qué dice esto?» en lugar de «¿hay algo acá?».
"""


def _tiene_tinta(gris: np.ndarray, span: _Span) -> bool:
    """
    ¿Esta celda tiene algo escrito?

    Se descartan los componentes que TOCAN el borde del recorte, porque eso es
    lo que queda de la regla cuando la línea es gruesa o la imagen se enderezó:
    sin descartarlos, todas las celdas tendrían «tinta» y la puerta no filtraría
    nada.
    """
    recorte = gris[
        max(0, int(span.y0) + 3) : int(span.y1) - 3,
        max(0, int(span.x0) + 3) : int(span.x1) - 3,
    ]
    if recorte.size == 0 or min(recorte.shape) < 6:
        return False

    alto, ancho = recorte.shape
    binaria = cv2.adaptiveThreshold(
        recorte, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 15, 8
    )
    cantidad, _, stats, _ = cv2.connectedComponentsWithStats(binaria, 8)
    for i in range(1, cantidad):
        x, y, w, h, area = (int(stats[i][k]) for k in range(5))
        if area < AREA_MINIMA_DE_TINTA:
            continue
        if x <= 1 or y <= 1 or x + w >= ancho - 1 or y + h >= alto - 1:
            continue
        return True
    return False


def _leer_span_solo(gris: np.ndarray, span: _Span, girar: bool) -> tuple[str | None, float]:
    """
    Una celda leída aparte, recortada y ampliada.

    ── Para qué hace falta, si ya se leyó la imagen entera ────────────────

    Porque el detector de texto propone regiones sobre la página completa, y
    ahí un CARÁCTER SUELTO en una celda grande no acumula la evidencia que
    acumula una palabra. Medido sobre esta planilla: de los rótulos «a b c d
    e» del encabezado encontraba tres y perdía dos, y el símbolo «Ø» de la
    columna de diámetro no lo veía nunca. Las medidas de esas columnas
    quedaban sin rótulo y se descartaban en silencio, que es el peor final
    posible.

    Recortada y ampliada, la misma celda es una imagen chica con un carácter
    grande, que es el caso para el que el detector está entrenado.

    Es caro —una pasada de OCR por celda— así que se usa SÓLO donde vale: en
    las celdas vacías de las primeras filas, que son las que pueden ser
    encabezado, y en las celdas altas y angostas, que llevan el texto girado.

    ── El giro ────────────────────────────────────────────────────────────

    La columna de sección («REFUERZO LONGITUDINAL») viene escrita en vertical
    en una celda combinada. Se prueban los dos sentidos y gana el que el OCR
    puntúa mejor, porque cuál usa la planilla depende de quién la hizo.
    """
    recorte = _recortar(gris, span)
    if recorte is None:
        return None, 1.0

    candidatos: list[np.ndarray] = [recorte]
    if girar:
        candidatos = [
            cv2.rotate(recorte, cv2.ROTATE_90_CLOCKWISE),
            cv2.rotate(recorte, cv2.ROTATE_90_COUNTERCLOCKWISE),
        ]

    mejor: tuple[str | None, float] = (None, 1.0)
    for imagen in candidatos:
        cajas = _leer_texto(
            imagen, umbral=UMBRAL_DE_CAJA_EN_CELDA, puntaje_minimo=PUNTAJE_MINIMO_EN_CELDA
        )
        if not cajas:
            continue
        cajas.sort(key=lambda b: (round(b.y0 / 12), b.x0))
        texto = " ".join(b.texto for b in cajas)
        puntaje = min(b.puntaje for b in cajas)
        if mejor[0] is None or puntaje > mejor[1]:
            mejor = (texto, puntaje)
    return mejor


# ── La puerta de entrada ───────────────────────────────────────────────────


def tablas_de_imagen(
    data: bytes,
) -> tuple[list[list[list[str | None]]], list[str], float]:
    """
    Las tablas de una imagen, con la forma que devuelve `pdfplumber`.

    Devuelve una matriz por tabla —filas × columnas, `None` donde no hay texto—,
    los avisos de calidad de la lectura y los píxeles por columna de la tabla
    más apretada.

    Los avisos son parte del resultado y no un detalle: una planilla leída de una
    foto tiene que llegar a la pantalla diciendo que se leyó de una foto. Y los
    píxeles por columna se devuelven en vez de decidirse acá porque el criterio
    para rechazar una imagen justa depende de algo que este módulo no ve — ver
    `PX_POR_COLUMNA_MINIMO`.
    """
    gris, escala = abrir_imagen_con_escala(data)
    gris = enderezar(gris)
    binaria = _binarizar(gris)
    horizontal, vertical = _mascaras_de_regla(binaria)
    bloques = _bloques_de_tabla(horizontal, vertical, gris.shape)

    if not bloques:
        raise ImagenIlegible(
            "NO_TABLE",
            "no se encontró ninguna tabla con líneas de regla en la imagen. La foto "
            "tiene que mostrar la planilla completa, de frente y con la tabla "
            "recuadrada; una planilla sin recuadro no se puede leer de una imagen.",
        )

    # Segunda pasada: si la tabla es una PARTE del cuadro, se recorta a ella y se
    # vuelve a mirar. La primera pasada sirvió para encontrarla; el
    # reconocimiento se hace sobre la tabla ocupando los píxeles que hay.
    # Ver `_recortar_a_la_tabla`.
    recortado = _recortar_a_la_tabla(gris, bloques, ampliacion_ya_usada=escala)
    if recortado is not None:
        gris, escala_del_recorte = recortado
        # La escala se ACUMULA: `_px_por_columna` mide contra el archivo
        # original, y entre el original y esta imagen hay dos escalados.
        escala *= escala_del_recorte
        binaria = _binarizar(gris)
        horizontal, vertical = _mascaras_de_regla(binaria)
        bloques = _bloques_de_tabla(horizontal, vertical, gris.shape)
        if not bloques:
            raise ImagenIlegible(
                "NO_TABLE",
                "se encontró la tabla en la imagen pero al acercarse a ella dejó de "
                "reconocerse. Sacá la foto de frente, con la planilla completa y sin "
                "sombras sobre la hoja.",
            )

    cajas = _leer_texto(gris)
    if not cajas:
        raise ImagenIlegible(
            "NO_TEXT",
            "se encontró la tabla pero no se pudo leer ni un solo texto. Sacá la foto "
            "de más cerca, con buena luz y sin sombras sobre la hoja.",
        )

    matrices: list[list[list[str | None]]] = []
    dudosas: list[str] = []
    resoluciones: list[float] = []
    total_celdas = 0

    for bloque in bloques:
        # Las verticales PRIMERO: los bordes de columna son lo que después
        # permite decidir si una horizontal es una regla o un subrayado.
        xs = _cortes_verticales(vertical, bloque)
        ys = _cortes_horizontales(horizontal, bloque, xs)
        if len(xs) < 2 or len(ys) < 2:
            continue

        # La resolución se MIDE acá y se decide más arriba.
        #
        # Este módulo no sabe si la planilla se puede verificar a sí misma —si
        # trae las columnas de longitud y total contra las que recalcular— y esa
        # es la pregunta que decide si una imagen justa se lee o se rechaza.
        # Quien la sabe es `pdf_parser`, que ya interpretó el encabezado.
        px_columna = _px_por_columna(xs, escala)
        resoluciones.append(px_columna)

        n_filas, n_columnas = len(ys) - 1, len(xs) - 1
        filas: list[list[str | None]] = [[None] * n_columnas for _ in range(n_filas)]
        puntajes: dict[tuple[int, int], float] = {}

        # ── Primera pasada: lo que el detector encontró en la página ────────
        spans = _armar_spans(xs, ys, horizontal, vertical)
        for span in spans:
            texto, puntaje = _texto_de_span(span, cajas)
            if texto is None:
                continue
            filas[span.fila][span.columna] = texto
            puntajes[(span.fila, span.columna)] = puntaje

        # ── Segunda pasada: las celdas vacías del ENCABEZADO ────────────────
        #
        # Sólo el encabezado, y la restricción es deliberada.
        #
        # Un rótulo sin leer no pierde una celda: pierde una COLUMNA entera, en
        # silencio, y con ella todas sus medidas. Y equivocarlo no produce un
        # dato falso —un rótulo mal leído no coincide con ningún alias del
        # diccionario y la columna cae en «no reconocidas», a la vista—, así que
        # acá se puede ser generoso con el umbral. Medido: así aparecen los
        # rótulos «d» y «e» y el «Ø» del diámetro, que en la pasada de la página
        # no aparecen nunca.
        #
        # Se probó extenderla a las celdas de DATOS de las columnas casi
        # completas, pensando en las cantidades que se pierden en una planilla
        # de baja resolución. Se descartó por dos razones medidas: no recuperó
        # una sola celda útil en ninguno de los documentos de prueba, y el
        # riesgo es asimétrico —un número inventado en una fila verosímil no se
        # ve, y es el único error que este módulo no se puede permitir—. Una
        # planilla a la que le faltan cantidades no necesita un OCR más audaz:
        # necesita más resolución, y para eso está `PX_POR_COLUMNA_MINIMO`.
        relecturas = 0

        for span in spans:
            if filas[span.fila][span.columna] is not None:
                continue
            if relecturas >= MAX_RELECTURAS:
                break

            alta_y_angosta = (span.y1 - span.y0) > (span.x1 - span.x0) * 1.5
            if not (alta_y_angosta or span.fila < FILAS_DE_ENCABEZADO):
                continue
            if not _tiene_tinta(gris, span):
                continue

            relecturas += 1
            texto, puntaje = _leer_span_solo(gris, span, girar=alta_y_angosta)
            if texto is None:
                continue

            filas[span.fila][span.columna] = texto
            puntajes[(span.fila, span.columna)] = puntaje

        for (f, c), puntaje in puntajes.items():
            total_celdas += 1
            if puntaje < PUNTAJE_DUDOSO:
                dudosas.append(f"«{filas[f][c].replace(chr(10), ' ')}»")

        matrices.append(filas)

    avisos = [
        "la planilla se leyó de una imagen con reconocimiento de caracteres: "
        "REVISÁ CADA MEDIDA contra el papel antes de guardar, porque un dígito mal "
        "leído no se distingue de un dígito bien leído"
    ]

    px_columna = min(resoluciones) if resoluciones else float("inf")
    # Sólo la banda de arriba: por debajo del mínimo el aviso lo pone
    # `pdf_parser.parse_imagen`, que es quien sabe si la planilla se puede
    # verificar a sí misma, y dos avisos de lo mismo con números distintos
    # confunden más de lo que informan.
    if PX_POR_COLUMNA_MINIMO <= px_columna < PX_POR_COLUMNA_COMODO:
        avisos.append(
            f"la imagen está al límite de resolución (unos {px_columna:.0f} píxeles por "
            f"columna, y lo cómodo son {PX_POR_COLUMNA_COMODO}): es probable que falten "
            "celdas. Las que falten van a quedar en blanco y se ven; revisá igual"
        )

    if dudosas:
        muestra = ", ".join(dudosas[:MAX_CELDAS_DUDOSAS_LISTADAS])
        resto = len(dudosas) - MAX_CELDAS_DUDOSAS_LISTADAS
        avisos.append(
            f"{len(dudosas)} de {total_celdas} celdas se leyeron con poca confianza"
            + (f" (las primeras: {muestra} y {resto} más)" if resto > 0 else f": {muestra}")
        )

    return matrices, avisos, px_columna

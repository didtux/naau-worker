"""
naau-worker — cuatro rutas, cero infraestructura.

Lee una planilla de fierros —en PDF, fotografiada o en Excel— y devuelve lo que
dice. Qué se recibió se decide por los primeros bytes del archivo y no por el
nombre ni por el `content-type`, que los pone quien sube. Nada más: no habla
con la base de datos, no escribe a disco, no guarda el archivo y no conoce las
empresas ni los usuarios del sistema. Todo eso es de Nest, que es quien
autentica a la persona, quien sabe de qué empresa es y quien decide qué se
guarda.

── Y también ESCRIBE Excel, que parece de otro proceso ────────────────

Dos rutas devuelven un `.xlsx`: la plantilla vacía y la planilla cargada. No es
una función de más metida acá —es la única forma de que el archivo que NAAU
baja se pueda volver a subir—. Los encabezados que se escriben son los que este
mismo módulo reconoce al leer, y escribirlos del otro lado habría durado hasta
el primer rótulo cambiado de un lado y no del otro.

Nest manda los datos; acá se decide cómo se rotula cada columna. Sigue sin
saberse qué es una empresa.

── Por dónde entra, y por qué así ──────────────────────────────────────────

Escucha SÓLO en 127.0.0.1 y exige un token compartido. Las dos cosas, no una:

  - Sólo en loopback, porque este proceso no tiene noción de identidad. Si
    estuviera expuesto, cualquiera podría hacerle parsear archivos, y aunque no
    devuelva datos de nadie sería un consumidor de CPU gratis en el servidor de
    producción.

  - Con token igual, porque «loopback» no es «privado»: en la misma máquina hay
    otros procesos, y en un servidor compartido, otros usuarios del sistema. Sin
    token, cualquiera de ellos llega. El proceso se NIEGA a arrancar si el token
    no está puesto — un default vacío es peor que ningún token, porque parece
    protección.

── Los límites, y dónde vive cada uno ──────────────────────────────────────

El tamaño y las páginas se topan acá, porque son lo que acota el trabajo:
`pdfplumber` carga la página entera en objetos y una página gigante es memoria.
Las imágenes tienen además su propio tope de PÍXELES, en `image_table`: un JPEG
de dos megas puede traer sesenta megapíxeles, así que pasa el tope de bytes y
son ciento ochenta megas al decodificarlo.

El TIMEOUT no está acá y es a propósito. Un timeout dentro del proceso que hace
el trabajo no lo puede cancelar —matar un hilo de Python a mitad de camino deja
el intérprete en un estado que no se puede razonar—, así que sería un timeout de
mentira. El que importa es el del cliente HTTP en Nest, que es quien no puede
quedarse esperando; acá lo que garantiza que el trabajo termine es que el
tamaño y las páginas están acotados.
"""

from __future__ import annotations

import os
import secrets

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, Response

from models import ExportBook, ParseResult
from pdf_parser import MAX_PAGES, ParseError, parse_imagen, parse_pdf, parse_xlsx

# 8 MB. Una planilla de vigas con la tabla y los croquis vectoriales pesa unos
# 300 kB; el tope deja margen para un documento largo con fuentes incrustadas y
# corta antes de que un archivo hostil importe.
MAX_BYTES = 8 * 1024 * 1024

CHUNK = 64 * 1024

# Los primeros bytes de un PDF. Se comprueba el contenido y no el nombre ni el
# `content-type`, que los pone quien sube el archivo.
FIRMA_PDF = b"%PDF-"

# ── Imágenes ───────────────────────────────────────────────────────────────
#
# Se aceptan las tres que produce cualquier teléfono o escáner. GIF y BMP no:
# nadie fotografía una planilla en GIF, y cada formato de más es una superficie
# de decodificación más en un proceso que recibe archivos de terceros.
#
# La firma se comprueba por CONTENIDO, igual que la del PDF. Un `.jpg` que
# adentro es otra cosa no llega al decodificador.
FIRMAS_DE_IMAGEN: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"\xff\xd8\xff", "JPEG"),
    # WEBP: «RIFF» + 4 bytes de tamaño + «WEBP». El tamaño se saltea.
    (b"RIFF", "WEBP"),
)

# ── Excel ──────────────────────────────────────────────────────────────────
#
# Un `.xlsx` es un ZIP, así que su firma es la del ZIP y la comparten un `.docx`,
# un `.jar` y cualquier carpeta comprimida. Eso es TODO lo que esta firma
# pretende: que al decodificador no le llegue un video ni un ejecutable.
#
# Distinguir un libro de cálculo de una carta no es trabajo de una firma de
# cuatro bytes, es trabajo de abrirlo — y `parse_xlsx` lo abre y devuelve
# `CORRUPT_EXCEL` con un mensaje que dice qué hacer. Pretenderlo acá —mirar el
# índice del ZIP para ver si trae `xl/workbook.xml`— sería empezar a parsear el
# archivo dentro del control de acceso, que es el peor lugar posible.
FIRMA_ZIP = b"PK"

# El tipo de contenido de un .xlsx, para la plantilla que este servicio genera.
TIPO_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def formato_de(contenido: bytes) -> str | None:
    """Qué es este archivo, según sus primeros bytes. `None` si no se reconoce."""
    if contenido[: len(FIRMA_PDF)] == FIRMA_PDF:
        return "PDF"
    if contenido.startswith(FIRMA_ZIP):
        return "XLSX"
    for firma, nombre in FIRMAS_DE_IMAGEN:
        if not contenido.startswith(firma):
            continue
        if nombre == "WEBP" and contenido[8:12] != b"WEBP":
            continue
        return nombre
    return None

TOKEN_ENV = "NAAU_WORKER_TOKEN"


def token_configurado() -> str:
    token = os.environ.get(TOKEN_ENV, "")
    if len(token) < 32:
        raise RuntimeError(
            f"falta {TOKEN_ENV} o es demasiado corto (mínimo 32 caracteres). "
            "El worker no arranca sin token: un token vacío parece protección y no la es."
        )
    return token


def autorizar(x_worker_token: str = Header(default="")) -> None:
    """
    El token compartido.

    `compare_digest` y no `==` porque una comparación que corta en el primer
    byte distinto filtra el token en el tiempo de respuesta, y este endpoint es
    justamente el que un proceso local puede llamar mil veces sin que nadie lo
    note.
    """
    esperado = token_configurado()
    if not secrets.compare_digest(x_worker_token, esperado):
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED"})


app = FastAPI(
    title="naau-worker",
    summary="Lectura de planillas de fierros en PDF",
    version="1.0.0",
    # Sin documentación interactiva: este servicio no tiene consumidores
    # humanos, y `/docs` sería una superficie más en un proceso que sólo debe
    # atender a Nest.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/health")
def health() -> dict[str, str]:
    """Para el arranque y el monitoreo. Sin token: no dice nada de nadie."""
    return {"status": "ok"}


@app.get(
    "/template/schedule-xlsx",
    dependencies=[Depends(autorizar)],
)
def plantilla_xlsx(unit: str = Query(default="cm", pattern="^(cm|m)$")) -> Response:
    """
    La plantilla vacía para cargar una planilla en Excel.

    ── Por qué la genera el LECTOR ─────────────────────────────────────────

    Porque la plantilla y el lector tienen que decir lo mismo. Los encabezados
    que este archivo escribe son exactamente los que `mapear_columnas` reconoce,
    y el rótulo de unidad de las columnas de medida —«a (cm)»— es el que
    `_unidad_declarada` lee. Generada en cualquier otro lado, un rótulo cambiado
    dejaría de mapearse sin que nada lo avise: la planilla volvería con las
    columnas sin reconocer y la culpa parecería del archivo de quien la llenó.

    Es el único endpoint de este servicio que no lee nada: devuelve bytes que no
    dependen de ninguna entrada salvo la unidad. Va con token igual que el
    resto, porque el token es de este proceso y no de cada ruta.
    """
    try:
        from xlsx_table import generar_plantilla

        contenido = generar_plantilla(unit)
    except ImportError as e:
        # Falta `openpyxl`. Es del SERVIDOR y no del pedido, así que 503 y con
        # el mismo código que usa la lectura: quien opera el servidor ve una
        # sola causa en el log en lugar de dos síntomas distintos. Sin esto, un
        # despliegue con las dependencias a medio instalar responde un 500 pelado
        # y manda a buscar el problema al generador de la plantilla.
        raise HTTPException(
            status_code=503,
            detail={
                "code": "EXCEL_UNAVAILABLE",
                "message": "la biblioteca que escribe archivos de Excel no está instalada",
            },
        ) from e

    return Response(
        content=contenido,
        media_type=TIPO_XLSX,
        headers={"Cache-Control": "no-store"},
    )


@app.post(
    "/export/schedule-xlsx",
    dependencies=[Depends(autorizar)],
)
def exportar_xlsx(libro: ExportBook) -> Response:
    """
    Una planilla cargada, bajada a Excel.

    ── Por qué la escribe el LECTOR ──────────────────────────────

    Por lo mismo que la plantilla vacía, y acá pesa más: este archivo tiene que
    poder VOLVER A SUBIRSE. Los encabezados que escribe son los que
    `mapear_columnas` reconoce, y la columna de longitud declarada usa el alias
    exacto de `unit_length` —ver `COLUMNA_LONGITUD`—. Escrito en otro proceso,
    un rótulo cambiado de un solo lado convertiría el viaje de vuelta en una
    planilla con la mitad de las columnas sin reconocer, y la culpa parecería
    del archivo.

    Es un POST y no un GET porque el cuerpo son los datos de la planilla: no
    cabe en una URL, y tampoco corresponde que quede en el log de accesos de
    nadie. No guarda nada y no lee nada: entra un cuerpo, salen bytes.
    """
    try:
        from xlsx_table import generar_export

        contenido = generar_export(libro.model_dump())
    except ImportError as e:
        # Falta `openpyxl`. Mismo código y mismo 503 que la plantilla y que la
        # lectura: quien opera el servidor ve UNA causa en el log en lugar de
        # tres síntomas distintos.
        raise HTTPException(
            status_code=503,
            detail={
                "code": "EXCEL_UNAVAILABLE",
                "message": "la biblioteca que escribe archivos de Excel no está instalada",
            },
        ) from e

    return Response(
        content=contenido,
        media_type=TIPO_XLSX,
        headers={"Cache-Control": "no-store"},
    )


@app.post(
    "/parse/schedule-pdf",
    response_model=ParseResult,
    response_model_exclude_none=False,
    dependencies=[Depends(autorizar)],
)
async def parse_schedule_pdf(file: UploadFile) -> ParseResult:
    contenido = bytearray()
    while True:
        trozo = await file.read(CHUNK)
        if not trozo:
            break
        contenido.extend(trozo)
        if len(contenido) > MAX_BYTES:
            # Se corta durante la lectura y no después: leer entero para
            # después rechazarlo es cargar en memoria justo lo que se quería
            # evitar.
            raise HTTPException(
                status_code=413,
                detail={"code": "FILE_TOO_LARGE", "maxBytes": MAX_BYTES},
            )

    if not contenido:
        raise HTTPException(status_code=400, detail={"code": "EMPTY_FILE"})

    datos = bytes(contenido)
    formato = formato_de(datos)
    if formato is None:
        raise HTTPException(status_code=415, detail={"code": "NOT_A_PDF"})

    try:
        if formato == "PDF":
            return parse_pdf(datos, max_pages=MAX_PAGES)
        if formato == "XLSX":
            return parse_xlsx(datos)
        return parse_imagen(datos)
    except ParseError as e:
        # 422: el archivo llegó bien y es un PDF, pero no es una planilla que se
        # pueda leer. El `code` es lo que Nest traduce a un mensaje accionable.
        raise HTTPException(status_code=422, detail={"code": e.code, "message": str(e)}) from e


@app.exception_handler(HTTPException)
async def http_exception_handler(_request, exc: HTTPException) -> JSONResponse:
    """Un solo formato de error, para que Nest tenga un solo caso que mapear."""
    detalle = exc.detail if isinstance(exc.detail, dict) else {"code": "ERROR", "message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content={"error": detalle})


def main() -> None:
    token_configurado()  # falla acá y no en la primera petición
    uvicorn.run(
        app,
        # Loopback por defecto. En Railway corre como servicio aparte, sin
        # dominio público, y escucha en "::" para la red privada del proyecto:
        # lo único que llega es el server, y el token sigue siendo obligatorio.
        host=os.environ.get("NAAU_WORKER_HOST", "127.0.0.1"),
        port=int(os.environ.get("NAAU_WORKER_PORT") or os.environ.get("PORT") or "8099"),
        log_level=os.environ.get("NAAU_WORKER_LOG", "info"),
        # Sin `access_log` de cuerpos y sin `proxy_headers`: no hay proxy
        # adelante porque no hay red adelante.
        access_log=True,
    )


if __name__ == "__main__":
    main()

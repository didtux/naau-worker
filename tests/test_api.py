"""
El endpoint.

Lo que se prueba acá no es el parseo —eso está en `test_pdf_parser.py`— sino
las guardas: que sin token no se entra, que un archivo que no es un PDF se
rechaza antes de intentar leerlo, y que un archivo grande se corta.

Son las tres cosas que hacen que este proceso pueda estar escuchando en un
servidor de producción.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from conftest import PDF_ROTO, TOKEN_DE_PRUEBA, pdf_en_blanco
from main import MAX_BYTES, app, formato_de

RUTA = "/parse/schedule-pdf"


@pytest.fixture(scope="module")
def cliente() -> TestClient:
    return TestClient(app)


def test_health_no_pide_token(cliente: TestClient) -> None:
    r = cliente.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_sin_token_no_se_entra(cliente: TestClient) -> None:
    r = cliente.post(RUTA, files={"file": ("x.pdf", b"%PDF-1.7\n", "application/pdf")})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"


def test_con_token_equivocado_tampoco(cliente: TestClient) -> None:
    r = cliente.post(
        RUTA,
        headers={"X-Worker-Token": "y" * 40},
        files={"file": ("x.pdf", b"%PDF-1.7\n", "application/pdf")},
    )
    assert r.status_code == 401


def test_un_archivo_que_no_es_pdf_se_rechaza_por_su_CONTENIDO(cliente: TestClient) -> None:
    """
    Se mira la firma del archivo, no el nombre ni el `content-type`: los dos los
    pone quien sube el archivo, y un `.pdf` que en realidad es otra cosa no
    tiene por qué llegar a `pdfplumber`.

    El ejemplo era un ZIP, y dejó de servir cuando entró el XLSX: un `.xlsx` ES
    un ZIP, así que esos cuatro bytes ahora son un formato que este proceso
    acepta y el rechazo pasó a ser el del lector de Excel. Ver
    `test_un_zip_que_no_es_un_libro_responde_CORRUPT_EXCEL`, que prueba lo mismo
    del otro lado.
    """
    r = cliente.post(
        RUTA,
        headers={"X-Worker-Token": TOKEN_DE_PRUEBA},
        files={"file": ("planilla.pdf", b"\x00\x01\x02 no soy un pdf", "application/pdf")},
    )
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "NOT_A_PDF"


def test_un_archivo_vacio_se_rechaza(cliente: TestClient) -> None:
    r = cliente.post(
        RUTA,
        headers={"X-Worker-Token": TOKEN_DE_PRUEBA},
        files={"file": ("planilla.pdf", b"", "application/pdf")},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "EMPTY_FILE"


def test_un_archivo_grande_se_corta(cliente: TestClient) -> None:
    grande = b"%PDF-1.7\n" + b"\x00" * (MAX_BYTES + 1)
    r = cliente.post(
        RUTA,
        headers={"X-Worker-Token": TOKEN_DE_PRUEBA},
        files={"file": ("planilla.pdf", grande, "application/pdf")},
    )
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "FILE_TOO_LARGE"


@pytest.mark.parametrize(
    ("contenido", "esperado"),
    [
        (b"%PDF-1.7", "PDF"),
        (b"\x89PNG\r\n\x1a\n...", "PNG"),
        (b"\xff\xd8\xff\xe0 JFIF", "JPEG"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "WEBP"),
        # RIFF que NO es WEBP: un AVI empieza igual. Sin mirar los cuatro bytes
        # de la posición 8, un video entraría al decodificador de imágenes.
        (b"RIFF\x00\x00\x00\x00AVI LIST", None),
        (b"GIF89a", None),
        # Un ZIP entra como XLSX, y es deliberado: un `.xlsx` ES un ZIP, así que
        # esta firma ya no puede afirmar que sea un libro de cálculo. Lo que
        # sigue garantizando es lo que importa —que no llegue un video ni un
        # ejecutable— y distinguir un libro de una carta es trabajo de abrirlo:
        # `parse_xlsx` lo abre y responde CORRUPT_EXCEL.
        (b"PK\x03\x04", "XLSX"),
        (b"", None),
    ],
)
def test_el_formato_se_decide_por_los_primeros_bytes(contenido: bytes, esperado: str | None) -> None:
    """
    Ni el nombre ni el `content-type`: los dos los pone quien sube el archivo.

    Ahora que entran imágenes hay cuatro formas válidas en vez de una, y cada
    una se reconoce por su firma. Un `.jpg` que adentro es otra cosa no llega al
    decodificador, que es el único componente de este proceso que interpreta
    datos hostiles.
    """
    assert formato_de(contenido) == esperado


def test_un_gif_no_entra_aunque_sea_una_imagen(cliente: TestClient) -> None:
    """
    Se aceptan PNG, JPEG y WEBP, y nada más.

    No es purismo: cada formato de más es un decodificador más expuesto a
    archivos de terceros, y nadie fotografía una planilla en GIF.
    """
    r = cliente.post(
        RUTA,
        headers={"X-Worker-Token": TOKEN_DE_PRUEBA},
        files={"file": ("planilla.gif", b"GIF89a" + b"\x00" * 32, "image/gif")},
    )
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "NOT_A_PDF"


def test_una_imagen_entra_por_la_misma_ruta_y_se_marca_como_imagen(
    cliente: TestClient, planilla_bytes: bytes
) -> None:
    """
    La misma ruta, el mismo contrato, un `source` distinto.

    Una ruta aparte para imágenes habría duplicado el token, el tope de tamaño
    y el mapeo de errores para cambiar una línea. Lo que sí tiene que cambiar
    es lo que la respuesta DICE de sí misma: `source: "image"` es lo que deja
    que la pantalla avise que cada número de esa tabla se reconoció y no se
    leyó.
    """
    pytest.importorskip("cv2", reason="el lector de imágenes necesita las dependencias de OCR")
    pytest.importorskip("rapidocr_onnxruntime", reason="falta el motor de OCR")

    import io

    import pypdfium2 as pdfium
    from PIL import Image

    pagina = pdfium.PdfDocument(io.BytesIO(planilla_bytes))[0].render(scale=200 / 72).to_pil()
    buffer = io.BytesIO()
    pagina.convert("RGB").save(buffer, "PNG")

    r = cliente.post(
        RUTA,
        headers={"X-Worker-Token": TOKEN_DE_PRUEBA},
        # Nombre y tipo MENTIDOS a propósito: lo que decide es el contenido.
        files={"file": ("planilla.pdf", buffer.getvalue(), "application/pdf")},
    )

    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["source"] == "image"
    assert cuerpo["pages"] == 1
    assert len(cuerpo["rows"]) == 16
    assert any("reconocimiento de caracteres" in a for a in cuerpo["warnings"])


def test_la_planilla_piloto_entra_y_sale_completa(cliente: TestClient, planilla_bytes: bytes) -> None:
    r = cliente.post(
        RUTA,
        headers={"X-Worker-Token": TOKEN_DE_PRUEBA},
        files={"file": ("PLANILLA_VIGAS ENTRE PISO.pdf", planilla_bytes, "application/pdf")},
    )
    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["unit"] == "cm"
    assert len(cuerpo["rows"]) == 27
    assert cuerpo["summary"]["total_length"] == 123148.68
    # El contrato que Nest consume: las claves de la fila, tal cual.
    assert set(cuerpo["rows"][0]) == {
        "page",
        "index",
        "section",
        "code",
        "diameter_mm",
        "quantity",
        "elements",
        "sketch",
        "sketch_letters",
        "dimensions",
        "claimed",
        "cells",
        "unmapped",
        "issues",
    }


# ── La plantilla de Excel ──────────────────────────────────────────────────


def test_la_plantilla_pide_token(cliente: TestClient) -> None:
    """
    El token es de este PROCESO, no de cada ruta.

    La plantilla no lee ningún archivo y no dice nada de nadie, así que la
    tentación de dejarla abierta existe. Y sería un error: el token está para
    que nadie más que Nest le hable a este proceso, y una ruta abierta es una
    ruta que se puede llamar mil veces gratis en el servidor de producción.
    """
    assert cliente.get("/template/schedule-xlsx").status_code == 401


def test_la_plantilla_sale_como_un_xlsx_de_verdad(cliente: TestClient) -> None:
    """Un libro que se puede abrir, no un archivo con el nombre correcto."""
    pytest.importorskip("openpyxl")

    r = cliente.get(
        "/template/schedule-xlsx", headers={"X-Worker-Token": TOKEN_DE_PRUEBA}
    )

    assert r.status_code == 200
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    # Los primeros bytes de un ZIP: es lo que hace que el archivo pase la firma
    # del propio endpoint de subida cuando la persona lo devuelva lleno.
    assert r.content.startswith(b"PK\x03\x04")
    assert formato_de(r.content) == "XLSX"


def test_la_plantilla_solo_admite_las_dos_unidades(cliente: TestClient) -> None:
    """
    `cm` y `m`, y nada más.

    Una unidad de más en este parámetro sería una columna rotulada «a (mm)» en
    un archivo que después se lee con esa unidad declarada: la planilla entera
    mil veces más corta, sin una sola señal.
    """
    pytest.importorskip("openpyxl")
    cabeceras = {"X-Worker-Token": TOKEN_DE_PRUEBA}

    for unidad in ("cm", "m"):
        r = cliente.get(f"/template/schedule-xlsx?unit={unidad}", headers=cabeceras)
        assert r.status_code == 200, unidad

    assert cliente.get("/template/schedule-xlsx?unit=mm", headers=cabeceras).status_code == 422
    assert cliente.get("/template/schedule-xlsx?unit=pies", headers=cabeceras).status_code == 422


def test_la_plantilla_llena_vuelve_por_la_misma_ruta(cliente: TestClient) -> None:
    """
    Se descarga, se llena y se sube por el MISMO endpoint que el PDF.

    Es la prueba de que el XLSX es otra fuente de la misma tabla y no un camino
    aparte: lo único distinto es el decodificador, y eso viaja en `source`.
    """
    openpyxl = pytest.importorskip("openpyxl")
    cabeceras = {"X-Worker-Token": TOKEN_DE_PRUEBA}

    descargada = cliente.get("/template/schedule-xlsx", headers=cabeceras).content

    libro = openpyxl.load_workbook(io.BytesIO(descargada))
    hoja = libro["Planilla"]
    for j, valor in enumerate((1, "L", 12, 8, 2, 250, 45), start=1):
        hoja.cell(row=2, column=j, value=valor)
    lleno = io.BytesIO()
    libro.save(lleno)

    r = cliente.post(
        RUTA,
        headers=cabeceras,
        files={"file": ("planilla.xlsx", lleno.getvalue(), "application/octet-stream")},
    )

    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["source"] == "xlsx"
    assert cuerpo["unit"] == "cm"
    assert len(cuerpo["rows"]) == 1
    assert cuerpo["rows"][0]["code"] == "1"
    assert cuerpo["rows"][0]["type_code"] == "L"


def test_un_zip_que_no_es_un_libro_responde_CORRUPT_EXCEL(cliente: TestClient) -> None:
    """
    La firma lo deja entrar y el lector lo rechaza con su propio código.

    Son dos cosas distintas y las dos hacen falta: la firma protege al
    decodificador, y este código le dice a la persona qué hacer —volver a
    guardar el archivo como .xlsx, no cambiar de archivo—.
    """
    pytest.importorskip("openpyxl")

    zip_cualquiera = io.BytesIO()
    with zipfile.ZipFile(zip_cualquiera, "w") as z:
        z.writestr("hola.txt", "esto no es un libro")

    r = cliente.post(
        RUTA,
        headers={"X-Worker-Token": TOKEN_DE_PRUEBA},
        files={"file": ("carta.zip", zip_cualquiera.getvalue(), "application/zip")},
    )

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "CORRUPT_EXCEL"


def test_el_worker_se_niega_a_arrancar_sin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Un token vacío por default parece protección y no la es. El proceso tiene
    que caerse al arrancar, no en la primera petición de producción.
    """
    import main

    monkeypatch.delenv("NAAU_WORKER_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="NAAU_WORKER_TOKEN"):
        main.token_configurado()

    monkeypatch.setenv("NAAU_WORKER_TOKEN", "corto")
    with pytest.raises(RuntimeError, match="NAAU_WORKER_TOKEN"):
        main.token_configurado()

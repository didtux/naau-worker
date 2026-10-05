"""
Fixtures compartidas.

El token se pone ANTES de que se importe `main`, porque el módulo se niega a
arrancar sin token y eso es parte de lo que se quiere probar: la única forma de
tener un worker sin token es no tenerlo.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

TOKEN_DE_PRUEBA = "x" * 40
os.environ.setdefault("NAAU_WORKER_TOKEN", TOKEN_DE_PRUEBA)

# La planilla piloto: un documento de obra real, no una maqueta. Vive en la raíz
# del repositorio porque también es la fuente del golden de paridad del motor.
PLANILLA = RAIZ.parent / "PLANILLA_VIGAS ENTRE PISO.pdf"


def pdf_en_blanco() -> bytes:
    """
    Un PDF válido de una página vacía, armado a mano con su tabla de
    referencias correcta.

    Hace falta que sea VÁLIDO y no sólo que empiece con `%PDF-`: es la única
    forma de probar el camino «el archivo se abre bien pero no tiene una
    planilla adentro», que es distinto de «el archivo está roto» y le pide otra
    cosa a la persona.
    """
    objetos = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>",
    ]
    salida = bytearray(b"%PDF-1.4\n")
    posiciones = []
    for i, objeto in enumerate(objetos, start=1):
        posiciones.append(len(salida))
        salida += b"%d 0 obj\n" % i + objeto + b"\nendobj\n"

    inicio_xref = len(salida)
    salida += b"xref\n0 %d\n" % (len(objetos) + 1)
    salida += b"0000000000 65535 f \n"
    for p in posiciones:
        salida += b"%010d 00000 n \n" % p
    salida += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objetos) + 1,
        inicio_xref,
    )
    return bytes(salida)


# Un PDF que dice ser un PDF y no se puede abrir: empieza con la firma y no
# tiene tabla de referencias. Es lo que llega cuando una descarga se corta.
PDF_ROTO = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>"


@pytest.fixture(scope="session")
def planilla_bytes() -> bytes:
    if not PLANILLA.exists():
        pytest.skip(f"falta la planilla piloto en {PLANILLA}")
    return PLANILLA.read_bytes()


@pytest.fixture(scope="session")
def resultado(planilla_bytes: bytes):
    from pdf_parser import parse_pdf

    return parse_pdf(planilla_bytes)

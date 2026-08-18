"""Data URIs (RFC 2397), el formato con el que osTicket recibe contenido.

osTicket acepta el cuerpo del mensaje como data URI para poder indicar su
tipo, y así distinguir HTML de texto plano:

    data:text/html;charset=utf-8,<p>Hola</p>

Su parser JSON lo interpreta en ApiJsonDataParser::fixup()
(upload/include/class.api.php:457), que se apoya en Format::parseRfc2397()
(upload/include/class.format.php:1010).

Este módulo replica ese comportamiento para el **fallback SQL**, que escribe
en la base sin pasar por osTicket. Sin esto, un mensaje enviado como data URI
se guarda con el prefijo a la vista:

    data:text/html;charset=utf-8,<p>se aumenta limite...</p>

que es literalmente lo que ve el agente en el ticket.
"""

import base64
import binascii
from typing import NamedTuple


class Contenido(NamedTuple):
    texto: str
    # 'html' o 'text', tal como los guarda ost_thread_entry.format.
    formato: str


def parsear_mensaje(valor: str) -> Contenido:
    """Separa un data URI en su contenido y su formato.

    Lo que no sea un data URI se devuelve tal cual como texto plano, que es
    lo mismo que hace osTicket: `parseRfc2397` devuelve el valor íntegro con
    type 'text/plain' cuando no empieza por "data:".
    """
    if not valor.startswith("data:"):
        return Contenido(valor, "text")

    resto = valor[5:]
    if "," not in resto:
        # Un "data:" sin coma no es un data URI válido. Se trata como texto
        # antes que perder el contenido.
        return Contenido(valor, "text")

    meta, contenido = resto.split(",", 1)
    partes = meta.split(";")
    tipo = partes[0] or "text/plain"
    parametros = [p.lower() for p in partes[1:]]

    if "base64" in parametros:
        try:
            contenido = base64.b64decode(contenido).decode("utf-8", errors="replace")
        except (binascii.Error, ValueError):
            # Mejor guardar el texto crudo que perder el mensaje del usuario.
            return Contenido(valor, "text")

    # El charset se ignora a propósito: el cuerpo llegó como JSON, que ya es
    # utf-8 por definición, y la columna de osTicket también. Reconvertirlo
    # solo introduciría mojibake.
    return Contenido(contenido, "html" if tipo == "text/html" else "text")

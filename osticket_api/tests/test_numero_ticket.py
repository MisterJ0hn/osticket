"""El formateo del número replica Sequence::format() de osTicket
(upload/include/class.sequence.php)."""

from app.repositories.ticket_write_repo import _formatear_numero


def test_formato_por_defecto_rellena_a_seis_digitos():
    # Es el formato de esta instalación: ticket_number_format='######'.
    assert _formatear_numero("######", 4831) == "004831"


def test_numero_mas_largo_que_el_formato_no_se_recorta():
    # El último grupo recibe los dígitos que sobren.
    assert _formatear_numero("###", 12345) == "12345"


def test_formato_con_prefijo_y_sufijo():
    assert _formatear_numero("TX-######-CL", 42) == "TX-000042-CL"


def test_varios_grupos_reparten_los_digitos():
    assert _formatear_numero("##-####", 123456) == "12-3456"


def test_almohadilla_escapada_es_literal():
    assert _formatear_numero(r"\#-####", 12) == "#-0012"


def test_relleno_configurable():
    assert _formatear_numero("######", 7, relleno="0") == "000007"


def test_formato_sin_almohadillas_devuelve_el_numero():
    assert _formatear_numero("ABC", 99) == "99"

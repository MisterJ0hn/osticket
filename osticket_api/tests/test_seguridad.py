from tests.conftest import CABECERAS


def test_sin_clave_devuelve_403(cliente_sin_auth):
    respuesta = cliente_sin_auth.get("/api/v1/tickets?email=alguien@ejemplo.cl")
    assert respuesta.status_code == 403
    assert respuesta.json() == {"exito": False, "mensaje": "Acceso denegado"}


def test_clave_incorrecta_devuelve_403(cliente_sin_auth):
    respuesta = cliente_sin_auth.get(
        "/api/v1/tickets?email=alguien@ejemplo.cl",
        headers={"X-API-Key": "no-es-la-clave"},
    )
    assert respuesta.status_code == 403


def test_salud_no_exige_clave(cliente_sin_auth):
    # La consulta a la base falla (no hay MySQL en los tests) y aun así el
    # endpoint debe responder, informando el problema en vez de reventar.
    respuesta = cliente_sin_auth.get("/salud")
    assert respuesta.status_code == 200
    assert "base_datos" in respuesta.json()


def test_ip_no_autorizada_devuelve_403(cliente, monkeypatch):
    import ipaddress

    from app.core import security

    monkeypatch.setattr(
        security, "_REDES_PERMITIDAS", [ipaddress.ip_network("10.9.9.0/24")]
    )
    respuesta = cliente.get("/api/v1/tickets?email=alguien@ejemplo.cl",
                            headers=CABECERAS)
    assert respuesta.status_code == 403


def test_forwarded_for_se_ignora_si_el_proxy_no_es_de_confianza(cliente, monkeypatch):
    """Sin TRUSTED_PROXIES, la cabecera no debe servir para colarse."""
    import ipaddress

    from app.core import security

    monkeypatch.setattr(
        security, "_REDES_PERMITIDAS", [ipaddress.ip_network("10.9.9.0/24")]
    )
    monkeypatch.setattr(security, "_REDES_PROXIES", [])
    respuesta = cliente.get(
        "/api/v1/tickets?email=alguien@ejemplo.cl",
        headers={**CABECERAS, "X-Forwarded-For": "10.9.9.1"},
    )
    assert respuesta.status_code == 403

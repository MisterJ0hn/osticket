# Despliegue en el servidor Linux

Levanta los dos servicios con un solo `docker compose`:

| Servicio | Contenedor | Puerto host | Qué es |
|---|---|---|---|
| `osticket` | `osticket_app` | **7090** → 80 | osTicket 1.18 (PHP 8.2 + Apache) |
| `api` | `osticket_api` | **7091** → 8000 | Fachada REST (FastAPI) sobre osTicket |

La **MySQL no va en un contenedor**: se usa la que ya corre en la misma máquina,
base **`codi_soporte`** (ya migrada, con datos). Ambos contenedores llegan a ella
por `host.docker.internal`, que resuelve gracias al `extra_hosts: host-gateway`.

Red del compose: `172.28.0.0/24` — gateway `172.28.0.1` (el host),
osTicket `172.28.0.10`, API `172.28.0.20`. La subred es fija a propósito:
el `GRANT` de MySQL y la IP autorizada de la API Key dependen de ella.

---

## 1. Prerrequisitos en la MySQL del host

**Hay que hacer esto antes del primer arranque.** Si no, los contenedores
levantan pero no conectan.

### 1.1 Que MySQL escuche fuera de loopback

Por defecto viene con `bind-address = 127.0.0.1`, que no acepta conexiones
desde los contenedores. En `/etc/mysql/mysql.conf.d/mysqld.cnf`
(o `/etc/my.cnf` según la distro):

```ini
bind-address = 0.0.0.0
```

Si se prefiere no exponerlo a toda interfaz, basta con la del gateway de Docker:

```ini
bind-address = 172.28.0.1
```

> Ojo: esa interfaz solo existe una vez creada la red del compose
> (`docker compose up` la crea). Con `0.0.0.0` no hay ese problema de orden.

Luego: `sudo systemctl restart mysql`

### 1.2 Usuario con permiso desde la subred de Docker

Un usuario `'x'@'localhost'` **no** sirve: la conexión llega desde `172.28.0.x`.

```sql
CREATE USER IF NOT EXISTS 'usuario_soporte'@'172.28.0.%' IDENTIFIED BY 'la-clave';
GRANT ALL PRIVILEGES ON codi_soporte.* TO 'usuario_soporte'@'172.28.0.%';
FLUSH PRIVILEGES;
```

Se necesita escritura también para la API, por el `OSTICKET_FALLBACK_SQL=true`.

### 1.3 Firewall

Si hay `ufw` activo:

```bash
sudo ufw allow from 172.28.0.0/24 to any port 3306
sudo ufw allow 7090/tcp
sudo ufw allow 7091/tcp
```

### 1.4 Comprobar que la base está donde se cree

```sql
SELECT COUNT(*) FROM codi_soporte.ost_ticket;
```

Debe devolver los tickets migrados. Si da 0 o "table doesn't exist", falta
restaurar el dump antes de seguir:

```bash
mysql -u root -p codi_soporte < osTicket/01_dump.sql
```

---

## 2. Arranque

```bash
cd /ruta/OsTicket
cp .env.example .env
nano .env          # completar DB_USER, DB_PASSWORD, OSTICKET_URL, API_KEYS

docker compose config      # valida sintaxis e interpolación del .env
docker compose build
docker compose up -d
docker compose ps          # ambos deben quedar (healthy)
```

Lo mínimo a completar en el `.env`:

- `DB_USER` / `DB_PASSWORD` — el usuario del punto 1.2
- `OSTICKET_URL` — URL pública, ej. `http://192.168.1.50:7090` (sin barra final)
- `API_KEYS` — al menos una clave; con esto vacío la API rechaza todo
- `OSTICKET_API_KEY` — se obtiene en el paso 4 de la verificación

**`OSTICKET_SECRET_SALT` no se toca.** Es el salt con el que se cifró la base ya
migrada; con otro valor osTicket no descifra las contraseñas de las cuentas de
correo ni los tokens, y las sesiones dejan de validar.

---

## 3. Verificación

### 3.1 osTicket conecta y ve los datos migrados

```bash
curl -I http://SERVIDOR:7090
docker compose logs osticket | grep -i "unable to connect"   # no debe salir nada
```

En el navegador, `http://SERVIDOR:7090` debe mostrar el helpdesk **con los
tickets ya migrados**. Si aparece vacío o pide instalar, está pegando a otra
base.

### 3.2 El login de staff funciona

Entrar a `http://SERVIDOR:7090/scp/`. Si el login falla o las cuentas de correo
aparecen con clave inválida, el `SECRET_SALT` no coincide con el de la base.

### 3.3 La API ve la MISMA base

```bash
curl http://SERVIDOR:7091/salud
```

Debe responder:

```json
{
  "exito": true,
  "base_datos": "ok",
  "esquema": "ok",
  "conexion": "usuario_soporte@host.docker.internal:3306/codi_soporte"
}
```

Este endpoint no pide autenticación y distingue los dos fallos distintos:
`base_datos` malo = no conecta; `esquema` malo = conecta a la base equivocada
(que es el fallo silencioso: todo "funciona" pero los datos no son los del
helpdesk).

### 3.4 Autenticación de la API

`/api/v1/catalogos/estados` es la mejor prueba: no pide parámetros y solo
responde bien si además está leyendo la base.

```bash
curl -H "X-API-Key: tu-clave" http://SERVIDOR:7091/api/v1/catalogos/estados   # 200 + lista
curl http://SERVIDOR:7091/api/v1/catalogos/estados                            # 403
```

Listado de tickets (necesita el email del dueño):

```bash
curl -H "X-API-Key: tu-clave" "http://SERVIDOR:7091/api/v1/tickets?email=alguien@dominio.cl"
```

Documentación interactiva en `http://SERVIDOR:7091/docs`.

### 3.5 Creación de ticket — el paso que suele fallar

osTicket valida la API Key contra la **IP de origen exacta** (`ost_api_key.ipaddr`).
Como la API llama por la URL pública, el tráfico sale del contenedor, entra por
el puerto publicado del host y llega a osTicket enmascarado: la IP que verá es
la del gateway de la red del compose, **`172.28.0.1`**, no `172.28.0.20`.

1. Crear la API Key en Panel Admin → Manage → API Keys, con IP `172.28.0.1`.
2. Ponerla en `OSTICKET_API_KEY` del `.env` y `docker compose up -d api`.
3. Crear un ticket de prueba por la API.
4. Confirmar en `docker compose logs api` que **no** aparece el aviso de
   fallback SQL. Si aparece, la IP no coincide: ver la real en el access log de
   osTicket y corregirla.

```bash
# IP real que ve osTicket en el POST a /api/http.php
docker compose logs osticket | grep "api/http.php" | tail -5

# Corregirla sin pasar por el panel
mysql -u root -p -e "UPDATE codi_soporte.ost_api_key SET ipaddr='172.28.0.1' WHERE apikey='LA-KEY';"
```

### 3.6 Persistencia de adjuntos

```bash
docker compose restart osticket
```

Un adjunto subido antes debe seguir descargándose: viven en el volumen
`osticket_attachments`, fuera del contenedor.

---

## 4. Cron de osTicket

osTicket necesita un cron para leer los buzones de correo, marcar tickets
vencidos y aplicar auto-cierres. No se agregó un contenedor para eso; va en el
crontab del host:

```cron
*/5 * * * * cd /ruta/OsTicket && /usr/bin/docker compose exec -T osticket php /var/www/html/api/cron.php >/dev/null 2>&1
```

---

## 5. Operación

```bash
docker compose ps                      # estado y healthchecks
docker compose logs -f osticket        # logs de osTicket
docker compose logs -f api             # logs de la API
docker compose restart api             # reiniciar solo la API
docker compose up -d --build           # reconstruir tras cambios de código
docker compose down                    # bajar (el volumen de adjuntos NO se borra)
```

`docker compose down -v` **sí borra los adjuntos**. No usarlo salvo que sea a
propósito.

Cambios en el `.env` requieren `docker compose up -d` (recrea el contenedor);
`restart` a secas no recarga variables de entorno.

---

## 6. Problemas frecuentes

**"Unable to connect to the database" en osTicket**
Es siempre uno de los tres del punto 1: `bind-address` en loopback, el usuario
sin `@'172.28.0.%'`, o el firewall. Probar desde dentro del contenedor:

```bash
docker compose exec osticket php -r "var_dump(mysqli_connect('host.docker.internal','usuario','clave','codi_soporte'));"
```

**`host.docker.internal` no resuelve**
Requiere Docker 20.10+ para que `host-gateway` funcione en Linux. Alternativa:
poner `DB_HOST=172.28.0.1` en el `.env` (el gateway de la red es el host).

**La API responde `"esquema"` distinto de `ok`**
Conecta, pero a una base sin las tablas `ost_*` o con otro prefijo. Revisar
`DB_NAME` y `DB_TABLE_PREFIX`.

**La API devuelve 403 con la clave correcta**
`IPS_PERMITIDAS` está filtrando. Vaciarla o agregar la IP del cliente. Si hay un
proxy delante, además hay que llenar `TRUSTED_PROXIES` con la IP del proxy, o la
allowlist ve la del proxy y no la del cliente.

**Conflicto de subred `172.28.0.0/24`**
Si ya está en uso en el servidor, cambiarla en `docker-compose.yml` (bloque
`networks.soporte.ipam`) y actualizar en consecuencia el `GRANT` de MySQL y la
IP de la API Key.

---

## Archivos de este despliegue

| Archivo | Rol |
|---|---|
| `docker-compose.yml` | El de este despliegue: ambos servicios contra la MySQL del host |
| `.env.example` | Plantilla de configuración; se copia a `.env` (que no se versiona) |
| `osTicket/docker/ost-config.docker.php` | Config de osTicket que lee las credenciales del entorno |
| `osTicket/docker/apache-env.conf` | `PassEnv` para que esas variables lleguen a PHP |
| `osTicket/docker-compose.yml` | Anterior, solo osTicket. Se deja intacto |
| `osTicket/docker-compose.local.yml` | Pruebas locales contra XAMPP. Se deja intacto |

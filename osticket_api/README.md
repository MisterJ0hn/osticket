# osTicket API

Fachada REST en Python (FastAPI) sobre el osTicket de Alfaro Madariaga
(`D:\sitios\Alfaro\osTicket`, versión **v1.18.3**).

## Por qué existe

osTicket v1.18 **no tiene API de lectura**. Su dispatcher
(`upload/api/http.php:19-24`) publica exactamente dos rutas:

```
POST /api/tickets.{xml|json|email}   → crear ticket
POST /api/tasks/cron                 → ejecutar el cron
```

No hay endpoint para listar, ver detalle ni consultar estado. Este servicio
resuelve eso combinando los dos caminos:

| Operación | Cómo se resuelve |
|---|---|
| Crear ticket | API nativa de osTicket (`X-API-Key`), con **fallback a INSERT en MySQL** si el helpdesk no responde |
| Obtener tickets del usuario | SQL de solo lectura |
| Detalle del ticket | SQL de solo lectura |
| Estado del ticket | SQL de solo lectura |

## Endpoints

Todos bajo `/api/v1` y protegidos por `X-API-Key` + allowlist de IP.

Cada consumidor tiene su propia clave, y **solo ve los tickets de su
organización de osTicket**: ver *Clientes de la API*.

| Método | Ruta | Qué hace |
|---|---|---|
| `POST` | `/api/v1/tickets` | Crear ticket |
| `GET` | `/api/v1/tickets?email=...` | Tickets creados por ese usuario (por defecto, solo los abiertos) |
| `GET` | `/api/v1/tickets/{numero}` | Detalle: cabecera + hilo de mensajes + adjuntos (con `incluir_contenido=true`, los archivos en base64) |
| `GET` | `/api/v1/tickets/{numero}/estado` | Estado (consulta liviana, para polling) |
| `GET` | `/api/v1/catalogos/{temas\|estados\|prioridades}` | Ids configurables del helpdesk |
| `GET` | `/salud` | Ping del servicio (sin autenticación) |

Documentación interactiva en `/docs`.

## Instalación

```bash
cd D:\sitios\temposoft\osticket_api
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env    # y editar
uvicorn app.main:app --reload --port 8000
```

Con Docker:

```bash
docker build -t osticket-api .
docker run --rm -p 8000:8000 --env-file .env osticket-api
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest tests -q
```

No necesitan MySQL ni osTicket levantado: parchean el repositorio y el cliente
HTTP para verificar el ruteo, la autenticación, el mapeo de respuestas, la
decisión de fallback y el formateo del número de ticket.

## Levantar osTicket para probar

El `docker-compose.yml` original de `D:\sitios\Alfaro\osTicket` despliega
osTicket contra una base que ya existe en el servidor físico. Para pruebas
locales hay un compose aparte:

```bash
cd D:\sitios\Alfaro\osTicket
docker compose -f docker-compose.local.yml up -d
```

- osTicket queda en `http://localhost:8080`
- la base **no** va en un contenedor: se usa la MySQL de XAMPP del host
  (`temposoft_soporte`), a la que el contenedor llega por
  `host.docker.internal` gracias al `extra_hosts` del compose
- la conexión del contenedor está en `docker/ost-config.local.php`, montado
  encima del `ost-config.php` original, que queda intacto

**Importante**: esta API y el contenedor de osTicket deben apuntar a la misma
base. Si el contenedor escribe en una y la API lee de otra, los tickets recién
creados no aparecerán en los listados. `GET /salud` muestra a qué conexión está
apuntando la API y si la base tiene realmente el esquema de osTicket.

## Configuración obligatoria en osTicket

La creación por API nativa **no funciona sin dar de alta una API Key**
(la tabla `ost_api_key` viene vacía):

1. Entrar al panel de agentes → **Admin Panel → Manage → API Keys**.
2. **Add New API Key**.
3. En *IP Address* poner la IP **desde la que corre este servicio Python**
   (en desarrollo local con el Docker de osTicket suele ser la IP del host,
   no `127.0.0.1`: el contenedor ve la IP del gateway de Docker).
4. Dejar marcado *Can Create Tickets*.
5. Copiar la clave generada a `OSTICKET_API_KEY` en el `.env`.

Si la clave falta o la IP no coincide, osTicket responde `401` y el servicio
cae al fallback SQL (o devuelve `502` si el fallback está desactivado).

## Variables de entorno

Ver `.env.example`. Las que más importan:

| Variable | Para qué |
|---|---|
| `MYSQL_*` | Conexión a la base de osTicket. En producción es la de Hostinger; en desarrollo, la del Docker local |
| `OSTICKET_TABLE_PREFIX` | El `TABLE_PREFIX` de `upload/include/ost-config.php` (acá: `ost_`) |
| `OSTICKET_URL` | Base de la instalación, sin barra final (ej. `http://localhost:8080`) |
| `OSTICKET_API_KEY` | La clave del paso anterior |
| `OSTICKET_FALLBACK_SQL` | `false` para fallar en vez de insertar directo en MySQL |
| `OSTICKET_TIMEOUT_ADJUNTOS` | Timeout para peticiones con adjuntos (120s). Debe ser >= al `max_execution_time` de PHP |
| `ADJUNTOS_MAX_MB` | Tope del conjunto de adjuntos al **crear** un ticket (25 MB) |
| `ADJUNTOS_DESCARGA_MAX_MB` | Tope del contenido devuelto en un detalle con `incluir_contenido=true` (15 MB) |
| `API_KEYS` | **Heredado.** Claves anónimas que ven TODO el helpdesk. Lo nuevo va en la tabla `api_cliente` (ver *Clientes de la API*) |
| `CACHE_CLIENTES_TTL` | Cada cuántos segundos se relee `api_cliente`. Marca el retardo con el que surte efecto una baja (60 por defecto) |
| `IPS_PERMITIDAS` | IPs/CIDR autorizados. **Vacío = sin filtro por IP** |
| `TRUSTED_PROXIES` | Solo desde estas IPs se hace caso a `X-Forwarded-For` |

## Clientes de la API

Cada consumidor se da de alta en la tabla `api_cliente` con su propia clave y
su organización. La organización es lo que delimita sus datos: un cliente solo
ve los tickets de usuarios que pertenecen a ella (`ost_user.org_id`).

### Puesta en marcha

```bash
# 1. Crear la tabla, una sola vez
mysql -u USUARIO -p BASE < sql/001_api_cliente.sql

# 2. Dar de alta cada cliente. La clave se muestra UNA vez.
python -m scripts.gestionar_clientes alta --nombre intranet --org-id 2

# En el despliegue con Docker:
docker compose exec api python -m scripts.gestionar_clientes listar
```

| Comando | Qué hace |
|---|---|
| `alta --nombre X --org-id N` | Crea el cliente y genera su clave |
| `listar` | Todos los clientes, con su organización y último uso |
| `rotar --nombre X` | Clave nueva; la anterior deja de servir |
| `baja --nombre X` | Revoca el acceso (`activo = 0`), sin borrar la fila |

Los cambios surten efecto en ≤ `CACHE_CLIENTES_TTL` segundos, **sin reiniciar
el servicio**: la caché de claves se refresca sola.

### Organización: el detalle que hay que tener presente

`org_id = 0` significa **cliente interno**: ve todos los tickets, sin
aislamiento. Es la única forma de saltárselo, y el script pide confirmarlo.

Al crear un ticket para un email que osTicket no conoce, el usuario nuevo
**queda asignado a la organización del cliente que lo pidió**. Sin ese paso el
usuario quedaría en `org_id = 0` (que es lo que hace osTicket por su cuenta) y
el cliente no podría leer después el ticket que acaba de crear.

Si el email ya pertenece a otra organización, la creación se rechaza con 400:
dejarla pasar crearía un ticket que el cliente que lo pidió no puede leer y que
sí ve otro cliente.

### Permisos

| Permiso | Habilita |
|---|---|
| `crear` | `POST /tickets` |
| `leer` | Los `GET` de tickets y catálogos |
| `notas` | Que `incluir_notas=true` devuelva las notas internas de los agentes |

`notas` va **solo a consumidores internos**. Son comentarios que los agentes
escriben dando por hecho que el cliente no los lee, y el filtro por
organización no los cubre: son datos de la propia organización. Sin el
permiso, el parámetro se ignora en silencio.

### Migrar desde `API_KEYS`

Las claves que estén en `API_KEYS` siguen funcionando como clientes internos
(ven todo, como antes) y el arranque avisa de cada una. Una vez dadas de alta
en `api_cliente`, se vacía la variable y se recrea el contenedor.

## Recuperar los archivos de un ticket

Por defecto el detalle trae solo los **metadatos** de cada adjunto (nombre,
tipo, tamaño): incluir el contenido en cada consulta arrastraría megabytes.
Con `incluir_contenido=true` viene además el archivo en base64:

```bash
curl -H "X-API-Key: $CLAVE"   "http://SERVIDOR:7091/api/v1/tickets/483920?incluir_contenido=true"
```

```json
{
  "mensajes": [{
    "tipo": "mensaje",
    "cuerpo": "<p>Adjunto la pantalla</p>",
    "adjuntos": [{
      "id": 12, "nombre": "foto.jpg", "tipo_mime": "image/jpeg",
      "tamano": 184320, "inline": false, "cid": null,
      "contenido_base64": "/9j/4AAQSkZJRg...", "error": null
    }]
  }]
}
```

Aplica igual a los mensajes y a las **respuestas de los agentes**: todo el
hilo se resuelve en una sola consulta a la base.

### Imágenes incrustadas en el cuerpo

Los tickets creados desde el portal web llevan la imagen **dentro** del HTML:

```html
<figure><img src="cid:8u5pbmprkplgc3rwlkl4zqatcxxan2d4" ...></figure>
```

Esos adjuntos vienen con `inline: true` y su `cid`, que es lo que el `src`
referencia. Para mostrar el mensaje con la imagen, se reemplaza `cid:LA-CLAVE`
por el data URI armado con `tipo_mime` y `contenido_base64`.

### Cuando un adjunto no se puede entregar

Se devuelve igual, con `contenido_base64` en `null` y el motivo en `error`
(omitirlo haría creer que el mensaje no traía imagen). Dos casos:

- **Otro almacenamiento**: osTicket puede guardar los archivos fuera de la
  base (`ost_file.bk` distinto de `D`). Solo el contenido guardado en la base
  es alcanzable desde acá.
- **Tope de tamaño**: si el ticket supera `ADJUNTOS_DESCARGA_MAX_MB`, los
  adjuntos que no caben se marcan con el error en vez de reventar la
  respuesta.

## Sobre el fallback SQL

Cuando osTicket no contesta (conexión caída, timeout, 5xx, o clave/IP
rechazada) y `OSTICKET_FALLBACK_SQL=true`, el ticket se inserta directamente
en MySQL para no perderlo. Ese camino **se salta la lógica de osTicket**:

- no evalúa los filtros de tickets (auto-asignación, rechazo, canned response),
- no manda auto-respuesta al usuario ni alerta a los agentes,
- no calcula el vencimiento del SLA,
- **no guarda los adjuntos**.

Por eso esos tickets quedan marcados con `source_extra = 'api-fallback'` y la
respuesta los identifica con `"origen": "fallback_sql"` más un `mensaje` de
aviso. Para encontrarlos después:

```sql
SELECT number, created FROM ost_ticket WHERE source_extra = 'api-fallback';
```

Un `4xx` de validación de osTicket (datos malos, tema inexistente, email
bloqueado) **no** dispara el fallback: se devuelve `400` al consumidor, porque
insertar por SQL solo saltaría esa validación.

## Ejemplos

Crear:

```bash
curl -X POST http://localhost:8000/api/v1/tickets \
  -H "X-API-Key: $CLAVE" -H "Content-Type: application/json" \
  -d '{
        "email": "cliente@empresa.cl",
        "nombre": "Cliente de Prueba",
        "asunto": "No puedo entrar al sistema",
        "mensaje": "Me dice usuario o contraseña incorrectos.",
        "topic_id": 1
      }'
```

```json
{"exito": true, "numero": "483920", "ticket_id": 17, "origen": "nativa", "mensaje": null}
```

Listar los abiertos de un usuario:

```bash
curl -H "X-API-Key: $CLAVE" \
  "http://localhost:8000/api/v1/tickets?email=cliente@empresa.cl"
```

Todos, con paginación y rango de fechas:

```bash
curl -H "X-API-Key: $CLAVE" \
  "http://localhost:8000/api/v1/tickets?email=cliente@empresa.cl&estado=todos&desde=2026-01-01&pagina=1&tamano=50"
```

Detalle y estado:

```bash
curl -H "X-API-Key: $CLAVE" http://localhost:8000/api/v1/tickets/483920
curl -H "X-API-Key: $CLAVE" http://localhost:8000/api/v1/tickets/483920/estado
```

## Comportamientos de osTicket que conviene conocer

Todo esto se verificó contra la instalación real, no está en su documentación:

- **osTicket valida el dominio del email por DNS.** Con `verify_email_addrs=1`
  (que es como está configurado este helpdesk), `Validator::is_email` hace una
  consulta MX/A del dominio y rechaza la creación si no resuelve. Un email con
  dominio inventado devuelve `400` con *"Incomplete client information"*.
- **osTicket responde HTTP 500 a los errores de validación**, no 400
  (`api.tickets.php:172`). El cliente los distingue por el prefijo
  *"Unable to create new ticket"* del cuerpo, para no confundirlos con una
  caída y disparar el fallback.
- **`ost_ticket.user_email_id` queda en 0** en los tickets que crea osTicket,
  tanto por la API nativa como por el portal web. El email del dueño hay que
  leerlo de `ost_user.default_email_id`.
- **La API Key se valida contra la IP de origen con comparación exacta**
  (`class.api.php:201`), sin comodines. Si la API Python corre en el host y
  osTicket en Docker, la IP a registrar es la del gateway de la red bridge
  (`172.18.0.1` en este entorno), no `127.0.0.1`.
- **En JSON, los adjuntos van en el formato antiguo**
  `[{"archivo.pdf": "data:mime;base64,..."}]`, no en el `{name, type, data,
  encoding}` que documenta `getRequestStructure()` (eso solo aplica a
  XML/email). Ver `ApiJsonDataParser::fixup` en `class.api.php:466`.

## Notas de implementación

- **`abierto` no se calcula por id de estado** sino por
  `ost_ticket_status.state = 'open'`. Los estados son configurables: si el
  helpdesk agrega "En espera de cliente" con `state='open'`, entra solo.
- Los tickets con `state='deleted'` nunca se devuelven: no son visibles ni en
  las colas de agentes ni en el portal del cliente.
- El `{numero}` de las rutas es `ost_ticket.number` (el que ve el cliente),
  no el `ticket_id` interno.
- El asunto y la prioridad se leen de `ost_ticket__cdata`, no de `ost_ticket`:
  en osTicket son campos del formulario dinámico.
- Las notas internas (`type='N'`) quedan fuera del detalle salvo que se pida
  `?incluir_notas=true`.
- No se mapean modelos ORM sobre el esquema de osTicket: es un esquema ajeno
  que cambia con cada upgrade del producto y que acá solo se consulta.

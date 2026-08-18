-- Clientes de la API: una clave por consumidor, con su organización.
--
--   mysql -u root -p codi_soporte < sql/001_api_cliente.sql
--
-- La tabla va SIN el prefijo ost_ a propósito: es nuestra, no de osTicket.
-- Así una actualización del producto no la toca, y un dump del helpdesk no
-- la pisa.
--
-- El alta de clientes NO se hace a mano acá: usar scripts/gestionar_clientes.py,
-- que genera la clave con entropía suficiente y guarda solo su hash.

CREATE TABLE IF NOT EXISTS `api_cliente` (
  `id`             INT AUTO_INCREMENT PRIMARY KEY,
  -- Identifica al consumidor en los logs: 'intranet', 'portal-rrhh'.
  `nombre`         VARCHAR(64)  NOT NULL,
  -- sha256 en hex de la clave. La clave en claro no se guarda en ninguna
  -- parte: si se pierde, se rota.
  `clave_hash`     CHAR(64)     NOT NULL,
  -- ost_organization.id. Delimita qué tickets ve este cliente.
  -- 0 = interno, SIN filtro de organización: ve todo el helpdesk.
  `org_id`         INT UNSIGNED NOT NULL DEFAULT 0,
  -- IPs y/o rangos CIDR separados por coma, solo para este cliente.
  -- Vacío = no se filtra por IP más allá de la allowlist global del .env.
  `ips_permitidas` VARCHAR(255) NOT NULL DEFAULT '',
  -- Separados por coma: crear, leer, notas.
  -- 'notas' habilita ver las notas internas de los agentes: solo consumidores
  -- internos, nunca un cliente externo.
  `permisos`       VARCHAR(64)  NOT NULL DEFAULT 'crear,leer',
  -- Baja lógica. Poner en 0 revoca el acceso sin borrar la fila ni perder
  -- el rastro de quién era.
  `activo`         TINYINT(1)   NOT NULL DEFAULT 1,
  `creado`         DATETIME     NOT NULL,
  -- Se actualiza como mucho una vez cada pocos minutos: sirve para detectar
  -- claves muertas, no para auditar cada llamada.
  `ultimo_uso`     DATETIME     NULL,
  UNIQUE KEY `uq_nombre` (`nombre`),
  -- Único además de índice: dos clientes con la misma clave harían ambiguo
  -- el lookup de autenticación.
  UNIQUE KEY `uq_clave_hash` (`clave_hash`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

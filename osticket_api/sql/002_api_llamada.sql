-- Log de llamados a esta API: quién llamó, con qué y qué se le contestó.
--
--   mysql -u root -p codi_soporte < sql/002_api_llamada.sql
--
-- La tabla va SIN el prefijo ost_ a propósito, igual que api_cliente: es
-- nuestra, no de osTicket.
--
-- El contenido en base64 de los adjuntos (campo contenido_base64, ver
-- app/schemas/ticket.py) se redacta antes de guardarse: solo se deja un
-- placeholder con el tamaño, para no llenar la tabla con binarios.

CREATE TABLE IF NOT EXISTS `api_llamada` (
  `id`             BIGINT AUTO_INCREMENT PRIMARY KEY,
  `fecha`          DATETIME     NOT NULL,
  -- IP de origen del que llama (ver ip_del_cliente() en core/security.py).
  `ip`             VARCHAR(45)  NOT NULL,
  -- Verbo + ruta, ej. "POST /api/v1/tickets".
  `metodo`         VARCHAR(255) NOT NULL,
  -- Body (o query string en los GET) ya redactado y truncado.
  `request`        TEXT         NULL,
  `response_code`  SMALLINT UNSIGNED NOT NULL,
  `response`       TEXT         NULL,
  KEY `ix_fecha` (`fecha`),
  KEY `ix_ip` (`ip`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

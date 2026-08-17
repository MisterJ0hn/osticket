<?php
/*********************************************************************
    ost-config.docker.php

    Configuración de osTicket para el despliegue con el docker-compose.yml
    de la raíz (servidor Linux, MySQL en la misma máquina).

    A diferencia de upload/include/ost-config.php y de ost-config.local.php,
    este archivo NO lleva credenciales escritas: las lee del entorno del
    contenedor, que el compose alimenta desde el .env de la raíz. Así la
    misma imagen sirve para cualquier entorno y el archivo se puede versionar.

    El SECRET_SALT debe ser el mismo con el que se cifró la base. Con otro,
    osTicket no puede descifrar lo ya guardado (contraseñas de las cuentas de
    correo, tokens) y las sesiones no validan. Por eso el valor por defecto es
    el del ost-config.php original.
**********************************************************************/

#Disable direct access.
if(!strcasecmp(basename($_SERVER['SCRIPT_NAME']),basename(__FILE__)) || !defined('INCLUDE_DIR'))
    die('kwaheri rafiki!');

/**
 * Lee una variable del entorno del contenedor.
 *
 * Se consulta getenv() y además $_SERVER: con mod_php la primera basta, pero
 * si en algún momento se pasa a php-fpm/CGI el valor llega por la segunda.
 * Un valor vacío cuenta como ausente para que un .env a medio llenar caiga
 * al valor por defecto en vez de intentar conectar con usuario "".
 */
function ost_env($nombre, $defecto = '') {
    $valor = getenv($nombre);
    if ($valor === false || $valor === '')
        $valor = isset($_SERVER[$nombre]) ? $_SERVER[$nombre] : '';
    return ($valor === '') ? $defecto : $valor;
}

#Install flag
define('OSTINSTALLED',TRUE);
if(OSTINSTALLED!=TRUE){
    if(!file_exists(ROOT_DIR.'setup/install.php')) die('Error: Contact system admin.');
    header('Location: '.ROOT_PATH.'setup/install.php');
    exit;
}

# Encrypt/Decrypt secret key - debe coincidir con el de la base ya existente.
define('SECRET_SALT', ost_env('OSTICKET_SECRET_SALT', 'i5XcPUvx6wbZIfcOSoN4V6t8nYkLmf31'));

#Default admin email. Used only on db connection issues and related alerts.
define('ADMIN_EMAIL', ost_env('OSTICKET_ADMIN_EMAIL', 'desarrollo.ti@alfaromadariaga.cl'));

# Database Options
# ====================================================
define('DBTYPE','mysql');
# La MySQL corre en el host Linux, no en un contenedor. host.docker.internal
# lo resuelve el extra_hosts "host-gateway" del compose. Si el puerto no es el
# 3306, va como "host:puerto" en OSTICKET_DBHOST.
define('DBHOST', ost_env('OSTICKET_DBHOST', 'host.docker.internal'));
define('DBNAME', ost_env('OSTICKET_DBNAME', 'codi_soporte'));
define('DBUSER', ost_env('OSTICKET_DBUSER', 'osticket'));
define('DBPASS', ost_env('OSTICKET_DBPASS', ''));

# Database TCP/IP Connect Timeout (default: 3 seconds)
# define('DBCONNECT_TIMEOUT', 3);

# Table prefix
define('TABLE_PREFIX', ost_env('OSTICKET_TABLE_PREFIX', 'ost_'));

#
# HTTP Server Options
# ===================================================
# Si osTicket queda detrás de un Nginx/Apache del host, poner acá la IP de ese
# proxy (o el rango de la red del compose) para que osTicket tome la IP real
# del cliente desde X-Forwarded-For en vez de la del proxy.
define('TRUSTED_PROXIES', ost_env('OSTICKET_TRUSTED_PROXIES', ''));
define('LOCAL_NETWORKS', ost_env('OSTICKET_LOCAL_NETWORKS', '127.0.0.0/8'));

#
# Session Options
# ===================================================
define('SESSION_SESSID', 'OSTSESSID');
?>

<?php
/*********************************************************************
    ost-config.local.php

    Copia de upload/include/ost-config.php para el entorno de PRUEBAS con
    docker-compose.local.yml. El original se deja intacto, apuntando a
    'localhost' porque está configurado para el servidor físico.

    Diferencia: la base. Acá osTicket usa la MySQL de XAMPP que corre en el
    host (temposoft_soporte), alcanzable desde el contenedor por
    host.docker.internal. Es para lo que existe el extra_hosts del compose.

    El SECRET_SALT debe ser el mismo del original: con otro, osTicket no
    puede descifrar lo que ya está guardado en la base (contraseñas de las
    cuentas de correo, tokens) y las sesiones no validan.
**********************************************************************/

#Disable direct access.
if(!strcasecmp(basename($_SERVER['SCRIPT_NAME']),basename(__FILE__)) || !defined('INCLUDE_DIR'))
    die('kwaheri rafiki!');

#Install flag
define('OSTINSTALLED',TRUE);
if(OSTINSTALLED!=TRUE){
    if(!file_exists(ROOT_DIR.'setup/install.php')) die('Error: Contact system admin.');
    header('Location: '.ROOT_PATH.'setup/install.php');
    exit;
}

# Encrypt/Decrypt secret key - debe coincidir con el del original.
define('SECRET_SALT','i5XcPUvx6wbZIfcOSoN4V6t8nYkLmf31');

#Default admin email. Used only on db connection issues and related alerts.
define('ADMIN_EMAIL','desarrollo.ti@alfaromadariaga.cl');

# Database Options
# ====================================================
define('DBTYPE','mysql');
# MySQL de XAMPP en el host. host.docker.internal es el nombre que Docker
# Desktop resuelve al host desde dentro del contenedor.
define('DBHOST','host.docker.internal');
define('DBNAME','temposoft_soporte');
define('DBUSER','root');
define('DBPASS','toor');

# Table prefix
define('TABLE_PREFIX','ost_');

#
# HTTP Server Options
# ===================================================
define('TRUSTED_PROXIES', '');
define('LOCAL_NETWORKS', '127.0.0.0/24');

#
# Session Options
# ===================================================
define('SESSION_SESSID', 'OSTSESSID');
?>

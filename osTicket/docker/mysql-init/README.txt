INSTRUCCIONES PARA MIGRACIÓN DE BASE DE DATOS
=============================================

Al desplegar en un nuevo servidor, coloca en esta carpeta el archivo SQL
con el dump de la base de datos antes de ejecutar docker-compose up.

El archivo debe llamarse: 01_dump.sql

Ejemplo de cómo exportar la BD desde el servidor original:
  mysqldump -h 190.4.214.234 -u root -p temposoft_soporte > 01_dump.sql

El contenedor MySQL ejecutará automáticamente todos los archivos .sql
de esta carpeta al inicializarse por primera vez.

IMPORTANTE: Si el volumen osticket_db_data ya existe, estos scripts NO
se ejecutan de nuevo. Para forzar la reimportación:
  docker-compose down -v    (borra todos los volúmenes)
  docker-compose up -d

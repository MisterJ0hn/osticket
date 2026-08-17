FROM php:8.2-apache-bullseye

# Habilitar módulos Apache necesarios
RUN a2enmod rewrite headers

# Instalar dependencias del sistema
RUN apt-get update && apt-get install -y \
    libgd-dev \
    libc-client-dev \
    libkrb5-dev \
    libicu-dev \
    libxml2-dev \
    libfreetype6-dev \
    libjpeg62-turbo-dev \
    libpng-dev \
    libzip-dev \
    libonig-dev \
    && rm -rf /var/lib/apt/lists/*

# Crear un mailer nulo para que PHP no falle al intentar enviar correos.
# El correo real se configura desde el panel de osTicket (SMTP).
RUN printf '#!/bin/sh\nexit 0\n' > /usr/local/bin/nullmailer \
    && chmod +x /usr/local/bin/nullmailer

# Configurar e instalar extensiones PHP requeridas por osTicket
RUN docker-php-ext-configure imap --with-kerberos --with-imap-ssl \
    && docker-php-ext-configure gd --with-freetype --with-jpeg \
    && docker-php-ext-install \
        mysqli \
        gd \
        imap \
        mbstring \
        xml \
        intl \
        gettext \
        fileinfo \
        zip

# Copiar configuración Apache
COPY docker/apache.conf /etc/apache2/sites-available/000-default.conf

# Copiar configuración PHP
COPY docker/php.ini /usr/local/etc/php/conf.d/osticket.ini

# Copiar la aplicación
COPY upload/ /var/www/html/

# Crear directorio de adjuntos si no existe y asignar permisos
RUN mkdir -p /var/www/html/attachments \
    && chown -R www-data:www-data /var/www/html \
    && chmod -R 755 /var/www/html \
    && chmod -R 777 /var/www/html/attachments

EXPOSE 80

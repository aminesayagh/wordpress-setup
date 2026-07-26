FROM wordpress:cli AS wpcli

FROM wordpress:php8.4-fpm

COPY --from=wpcli /usr/local/bin/wp /usr/local/bin/wp

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        default-mysql-client \
        less \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# MySQL 8.4 serves an auto-generated self-signed cert, and the MariaDB 11.8
# client verifies by default — so `wp db check` / `wp db export` die with
# "self-signed certificate in certificate chain". WP-CLI invokes the client with
# --no-defaults, so a my.cnf cannot fix it; shadow the binaries and append the
# flag instead. Connection stays TLS-encrypted, it just skips cert verification,
# which is what we want for a container-to-container hop on an internal network.
RUN for b in mariadb mariadb-check mariadb-dump mysql mysqlcheck mysqldump; do \
        printf '#!/bin/sh\nexec /usr/bin/%s "$@" --ssl-verify-server-cert=0\n' "$b" \
            > "/usr/local/bin/$b" \
        && chmod +x "/usr/local/bin/$b"; \
    done

# Align www-data with the host user so files PHP-FPM writes into the bind-mounted
# ./site stay editable from WSL. Avoids the setfacl dance on every new site.
ARG HOST_UID=1000
ARG HOST_GID=1000
RUN groupmod -g "$HOST_GID" www-data \
    && usermod -u "$HOST_UID" -g "$HOST_GID" www-data \
    && chown -R www-data:www-data /var/www

WORKDIR /var/www/html

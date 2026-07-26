# WordPress Project Instructions

## Runtime

The active WordPress installation is located at `./site`, bind-mounted into the
PHP-FPM container at `/var/www/html`.

## WP-CLI

Always run WordPress commands through `./bin/wp`:

- `./bin/wp core version`
- `./bin/wp plugin list`
- `./bin/wp theme list`
- `./bin/wp option get siteurl`
- `./bin/wp rewrite flush`
- `./bin/wp cache flush`

Do not install another WP-CLI copy on the host.

## Custom code

- Plugins: `./site/wp-content/plugins/`
- Themes: `./site/wp-content/themes/`
- Must-use plugins: `./site/wp-content/mu-plugins/`

Each plugin or theme may own its own Git repository. This blueprint does not
manage them.

## Runtime changes

Edits to plugin and theme source files are bind-mounted and take effect without
restarting Docker.

Restart a service only after changing its configuration:

- `docker compose restart nginx`
- `docker compose restart wordpress`

Rebuild only after changing a Dockerfile or system dependency:

- `docker compose up -d --build wordpress`

## Core files

WordPress core is visible for debugging and code tracing. Do not modify core
unless explicitly requested — prefer hooks, plugins, themes, or mu-plugins.

## Secrets

Do not print or modify files under `secrets/` unless explicitly requested.

## Database

Use WP-CLI for normal database operations (`./bin/wp db check`,
`./bin/wp search-replace`, `./bin/wp option get|update`).

Do not expose MySQL port 3306 or PHP-FPM port 9000.

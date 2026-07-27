# WordPress sites on `*.masayagh.com`

Create a new WordPress site with one command. Nothing to configure in Cloudflare.

```bash
python3 new-site.py
```

It asks for the site name and admin credentials, then:

1. copies `blueprint/` to `sites/<slug>/` and picks a free local port
2. pulls the latest WordPress image and boots the stack
3. installs WordPress
4. adds an ingress rule + DNS record to the shared `wordpress` tunnel

The site is live at `https://<slug>.masayagh.com` a minute later (DNS propagation).

## Layout

```
blueprint/    the template — nginx + PHP-FPM + MySQL + WP-CLI
sites/        generated sites, one directory each (gitignored)
tunnel/       the shared cloudflared that publishes all of them
docs/         design notes
```

## Xdebug

Xdebug is built into the image but inert by default. Enable it per site in
`sites/<slug>/.env`:

```dotenv
XDEBUG_MODE=debug        # off | debug | develop | profile | coverage
```

```bash
cd sites/<slug> && docker compose up -d wordpress
```

No rebuild — Xdebug reads `XDEBUG_MODE` from the environment, so a container
restart is enough. `off` has no measurable cost, which is why it ships enabled-but-off
rather than as a separate image.

Point your editor at **port 9003** and let it listen; the container dials out to
`host.docker.internal`, mapped to `host-gateway` in `compose.yaml` because that
name does not exist on Linux/WSL by default. VS Code `launch.json`:

```json
{ "name": "WordPress", "type": "php", "request": "launch", "port": 9003,
  "pathMappings": { "/var/www/html": "${workspaceFolder}/site" } }
```

The path mapping is what makes breakpoints line up — the container sees
`/var/www/html`, you edit `site/`.

Every request starts a session (`xdebug.start_with_request = yes`), including
`./bin/wp` calls, so each one pauses briefly when no editor is listening. For
on-demand sessions set `xdebug.start_with_request = trigger` in
`docker/php/xdebug.ini` and pass `?XDEBUG_TRIGGER=1`.

Note `ini_get('xdebug.mode')` reports the ini default, not the effective mode.
To check what is really active use `xdebug_info('mode')`.

## Database UI

`(a)` in `sites.py` toggles [AdminNeo](https://www.adminneo.org/) for a site, on
`ADMINNEO_PORT` from its `.env`. Equivalent by hand:

```bash
cd sites/<slug>
docker compose --profile tools up -d adminneo     # start
docker compose --profile tools stop adminneo      # stop
```

**The link opens already logged in** — no form, no credentials to copy.
`docker/adminneo/adminneo-config.php` declares exactly one server, which makes
`ExternalLoginPlugin` (enabled in `adminneo-plugins.php`) authenticate the session
automatically. The password is read from the mounted `mysql_password` secret at
request time, so it is never written into a config file and survives a rotation.

That means anyone who can reach the port has full database access with no prompt.
That is deliberate, and it is why the port is bound to `127.0.0.1` only and the
container is off unless you ask for it.

It sits behind a compose **profile**, so it never starts with `bin/start` and adds
nothing to a normal run. The menu entry is a toggle rather than a plain start so
the UI does not sit exposed after you are done. Port 3306 is still never published
— AdminNeo reaches MySQL over the internal `backend` network, and is itself bound
to `127.0.0.1` only.

For quick lookups without a browser, WP-CLI is already there:

```bash
./bin/wp db query "SELECT option_name FROM wp_options LIMIT 20"
./bin/wp db cli
```

## Backups

`(b)` in `sites.py` writes one dated zip per run to `sites/<slug>/backups/`:

```
menu-test-2026-07-26-2011.zip
├── database.sql          full dump via wp db export
└── wp-content/           themes, plugins, uploads, mu-plugins
```

The site must be started — the dump comes from the live database. `node_modules`,
`cache` and `upgrade` are skipped as regenerable; `.git` is kept, because a plugin
or theme may be its own repository.

Not yet built: restore, pruning of old zips, scheduling, and off-machine copies.
Backups currently sit on the same WSL disk as the site they protect, so they cover
a bad update or a dropped table — not disk loss.

## Applying config changes

`(r)` in `sites.py` applies whatever you edited:

```bash
docker compose up -d --build --force-recreate wordpress nginx
```

| You changed | Refresh needed? |
|---|---|
| Plugin/theme PHP, CSS, JS | no — bind-mounted, live immediately |
| `.env` (`XDEBUG_MODE`, `WP_DEBUG`, domain, port) | yes |
| `docker/php/*.ini` | yes |
| `docker/nginx/default.conf.template` | yes |
| `Dockerfile` | yes — `--build` handles it |

`--force-recreate` is not optional. A plain `up -d` compares the *compose* config,
sees nothing changed, and leaves the container running with a stale `php.ini` —
verified: after editing `memory_limit`, `up -d` still reported the old value and
only a forced recreate picked up the new one.

`db` is deliberately excluded; no PHP or nginx setting warrants bouncing the
database. If you change `MYSQL_*` in `.env`, run `docker compose up -d` yourself.

## The menu

```
python3 sites.py
```

Arrow keys and Enter, via [questionary](https://questionary.readthedocs.io/).
Each action then shows its own filtered list, so you never type a name or pick
an option that cannot work:

| Action | Lists |
|---|---|
| start a site | closed sites only |
| close a site | started sites only |
| refresh config | all sites — refreshing a closed one brings it back up |
| backup | started sites only — the dump reads the live database |
| database UI | started sites only — toggles AdminNeo on/off |
| open folder | all sites — opens `sites/<slug>/` in the file manager |
| new site | runs `new-site.py` |
| delete a site | all sites — hands off to `remove-site.py`, which asks you to type the slug |

`← back`, Esc, or Ctrl-C returns to the menu without doing anything; from the
top menu they quit. When an action has nothing to act on it says so instead of
showing an empty list.

Under WSL, **open folder** hands the directory to Windows Explorer via
`explorer.exe`; elsewhere it uses `xdg-open`. Explorer only speaks Windows
paths, so the site directory is passed as the working directory rather than as
an argument.

`questionary` is the repo's only dependency, and Debian's Python refuses to
install into itself (PEP 668). So on first run `sites.py` creates `.venv/`,
installs it there and re-execs — `python3 sites.py` still works with nothing to
set up. `new-site.py` and `remove-site.py` stay stdlib-only and run anywhere.

## Per-site commands

Run from inside `sites/<slug>/`:

| Command | Does |
|---|---|
| `./bin/start` | bring the stack up |
| `./bin/stop` | stop it (keeps database and files) |
| `./bin/status` | container status |
| `./bin/logs [service]` | follow logs |
| `./bin/wp <args>` | WP-CLI against the live site |
| `./bin/shell` | shell in the WordPress container as www-data |

Plugin and theme edits under `sites/<slug>/site/wp-content/` are live immediately —
no rebuild, no restart. Restart only after changing Docker, nginx, or PHP config.

## The shared tunnel

One locally-managed tunnel named `wordpress` serves every site. `tunnel/config.yml`
holds a rule per site; credentials live in `~/.cloudflared/`, outside this repo.

```bash
docker compose -f tunnel/compose.yaml ps
docker compose -f tunnel/compose.yaml logs -f cloudflared
docker compose -f tunnel/compose.yaml up -d --force-recreate   # apply config.yml edits
```

It uses `network_mode: host` so `http://localhost:<port>` reaches each site's
published port. cloudflared does not reload on SIGHUP — that kills it — so applying
a config change means recreating the container.

Other cloudflared instances on this machine (`my-dev-tunnel` via systemd) already
claim some localhost ports. `new-site.py` reads their configs and skips those ports,
so a new site never steals one.

## If a new site's hostname won't resolve

Don't query the hostname before `new-site.py` finishes. The zone's SOA sets a
**1800 s (30 min) negative-cache TTL**, so one early lookup pins "does not exist"
into your resolver for half an hour — flushing locally won't help, because the
stale answer sits upstream.

Check whether the site is actually live, bypassing the local resolver:

```bash
curl --doh-url https://1.1.1.1/dns-query https://<slug>.masayagh.com
```

If that returns 200 the site is fine and only your resolver is behind.

## Removing a site

```bash
python3 remove-site.py [slug]
```

Reverses everything `new-site.py` did: containers and database volume, site files,
the tunnel ingress rule, and the DNS record. It makes you retype the slug first.

**`backups/` is the only thing kept.** Everything else under the site directory is
deleted, and the directory is renamed:

```
sites/del-test/  ->  sites/del-test_deleted_20260726205201/
                     └── backups/        <- all that remains
```

**No backup is taken at delete time** — deliberately. Whatever is already in
`backups/` is what survives, so take one with `(b)` in `sites.py` first if you
want a fresh copy. The confirmation prompt shows how many backups exist and warns
when there are none.

Renamed directories lose their `.env`, so they stop appearing in `sites.py` and are
never offered for deletion again. Delete one for good with `rm -rf` once you no
longer want its backups.

Deleting the DNS record needs `CLOUDFLARE_API_TOKEN` in `.env.provision`
(cloudflared can create routes but not delete them). Create one at **My Profile >
API Tokens** with **Zone > DNS > Edit** on your zone. Without it, every other step
still runs and the script tells you which record to remove by hand.

## Giving one site its own tunnel

Set `CLOUDFLARE_TUNNEL_TOKEN` in `.env.provision` before running `new-site.py`. That
site then runs its own cloudflared inside its stack, and you add its route in the
dashboard pointing at `http://nginx:80`. Leave the token empty for the normal flow.

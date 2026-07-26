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
the tunnel ingress rule, and the DNS record. It makes you retype the slug first —
the database is deleted and not recoverable.

Deleting the DNS record needs `CLOUDFLARE_API_TOKEN` in `.env.provision`
(cloudflared can create routes but not delete them). Create one at **My Profile >
API Tokens** with **Zone > DNS > Edit** on your zone. Without it, every other step
still runs and the script tells you which record to remove by hand.

## Giving one site its own tunnel

Set `CLOUDFLARE_TUNNEL_TOKEN` in `.env.provision` before running `new-site.py`. That
site then runs its own cloudflared inside its stack, and you add its route in the
dashboard pointing at `http://nginx:80`. Leave the token empty for the normal flow.

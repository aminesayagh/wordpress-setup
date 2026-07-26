#!/usr/bin/env python3
"""Create a new WordPress site from ./blueprint.

Asks for the site name and WordPress admin credentials, derives everything else,
then boots the stack and installs WordPress. Stdlib only.

    python3 new-site.py
"""

import getpass
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BLUEPRINT = ROOT / "blueprint"
SITES = ROOT / "sites"
TUNNEL = ROOT / "tunnel"

# Other cloudflared instances on this machine already point hostnames at
# localhost ports. Those ports are spoken for even when nothing is listening,
# so scan these before handing a port to a new site.
OTHER_TUNNEL_CONFIGS = [
    Path.home() / ".cloudflared" / "config.yml",
    Path("/etc/cloudflared/config.yml"),
]

# Per-site state — never copied from the blueprint. site/ in particular must stay
# empty: the wordpress image populates it on first boot with the WordPress version
# baked into the image, which is how a new site gets the latest release instead of
# a snapshot of whatever the blueprint last ran.
NOT_TEMPLATE = {".env", "site", "secrets", "backups"}

FIRST_PORT = 8080


def fail(msg):
    sys.exit(f"error: {msg}")


def read_provision():
    """Parse .env.provision — plain KEY=VALUE, no dependency needed."""
    defaults = {
        "CLOUDFLARE_TUNNEL_TOKEN": "",
        "TUNNEL_NAME": "wordpress",
        "WP_ADMIN_EMAIL": "",
        "DOMAIN_SUFFIX": "masayagh.com",
    }
    path = ROOT / ".env.provision"
    if not path.exists():
        print(f"note: no {path.name} (copy .env.provision.example to create one)\n")
        return defaults
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        defaults[key.strip()] = value.strip().strip("'\"")
    return defaults


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def ask(prompt, default=""):
    suffix = f" [{default}]" if default else ""
    while True:
        answer = input(f"{prompt}{suffix}: ").strip() or default
        if answer:
            return answer
        print("  required.")


def tunnel_ports(path):
    """Ports a cloudflared config already routes a hostname to."""
    if not path.exists():
        return set()
    try:
        text = path.read_text()
    except OSError:
        return set()
    return {int(p) for p in
            re.findall(r"service:\s*https?://(?:localhost|127\.0\.0\.1):(\d+)", text)}


def pick_port():
    """Lowest free port from FIRST_PORT up.

    Checks ports claimed in sites/*/.env, ports other cloudflared configs already
    route to, and live binds — so neither a stopped site nor an idle tunnel route
    gets its port handed to a new site.
    """
    claimed = set()
    for env in SITES.glob("*/.env"):
        found = re.search(r"^LOCAL_HTTP_PORT=(\d+)", env.read_text(), re.M)
        if found:
            claimed.add(int(found.group(1)))
    for cfg in [TUNNEL / "config.yml", *OTHER_TUNNEL_CONFIGS]:
        claimed |= tunnel_ports(cfg)

    for port in range(FIRST_PORT, FIRST_PORT + 200):
        if port in claimed:
            continue
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    fail("no free port found")


def write_env(site_dir, values):
    """Substitute values into the copied .env.example, keeping its comments."""
    text = (site_dir / ".env.example").read_text()
    for key, value in values.items():
        text, count = re.subn(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M)
        if not count:
            text += f"\n{key}={value}\n"
    (site_dir / ".env").write_text(text)


def write_secrets(site_dir, tunnel_token):
    sec = site_dir / "secrets"
    sec.mkdir(exist_ok=True)
    (sec / "mysql_root_password.txt").write_text(secrets.token_urlsafe(36))
    (sec / "mysql_wordpress_password.txt").write_text(secrets.token_urlsafe(36))
    (sec / "cloudflare_tunnel_token.txt").write_text(tunnel_token)

    # 700 on the directory is what keeps other host users out. The WordPress DB
    # password must stay world-readable: wp-config.php re-reads it as www-data on
    # every request (see the comment in compose.yaml).
    sec.chmod(0o700)
    for f in sec.iterdir():
        f.chmod(0o600)
    (sec / "mysql_wordpress_password.txt").chmod(0o644)


def run(cmd, cwd, capture=False):
    result = subprocess.run(cmd, cwd=cwd, text=True,
                            capture_output=capture)
    if result.returncode != 0 and not capture:
        fail(f"{' '.join(cmd)} failed (site left at {cwd} for inspection)")
    return result


def publish(domain, port, tunnel_name):
    """Route domain -> this site through the shared tunnel.

    Adds the ingress rule, creates the DNS record, reloads cloudflared. Together
    these are everything Cloudflare needs, so creating a site touches no dashboard.
    """
    config = TUNNEL / "config.yml"
    if not config.exists():
        fail(f"{config} not found — the shared tunnel is not set up")
    if not shutil.which("cloudflared"):
        fail("cloudflared not installed on the host — needed to create the DNS record")

    text = config.read_text()
    if f"hostname: {domain}" in text:
        print(f"  route for {domain} already present, leaving it alone")
    else:
        # cloudflared matches top to bottom, so the catch-all must stay last.
        catch_all = "  - service: http_status:404"
        if catch_all not in text:
            fail(f"no http_status:404 rule in {config} — refusing to guess where to insert")
        text = text.replace(
            catch_all,
            f"  - hostname: {domain}\n    service: http://localhost:{port}\n\n{catch_all}",
            1)
        config.write_text(text)
        print(f"  added ingress rule -> http://localhost:{port}")

    # --config is not optional here. Without it cloudflared falls back to
    # ~/.cloudflared/config.yml, and a `tunnel:` key there silently wins over the
    # tunnel named on the command line — the CNAME then points at the wrong
    # tunnel and every request lands on that tunnel's catch-all 404.
    dns = subprocess.run(["cloudflared", "--config", str(config),
                          "tunnel", "route", "dns", "--overwrite-dns",
                          tunnel_name, domain],
                         text=True, capture_output=True)
    if dns.returncode != 0:
        fail(f"DNS route failed: {dns.stderr.strip() or dns.stdout.strip()}")
    print(f"  DNS record for {domain} points at the {tunnel_name} tunnel")

    run(["docker", "compose", "up", "-d", "--force-recreate"], TUNNEL)
    print("  cloudflared reloaded")


def wait_for_db(site_dir, timeout=120):
    """Poll `wp db check` — proves core is unpacked, wp-config valid, DB reachable."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if run(["./bin/wp", "db", "check"], site_dir, capture=True).returncode == 0:
            return
        time.sleep(3)
    fail(f"database not ready after {timeout}s — check: cd {site_dir} && ./bin/logs")


def main():
    if not BLUEPRINT.is_dir():
        fail(f"blueprint not found at {BLUEPRINT}")

    provision = read_provision()

    name = ask("Website name (e.g. Portfolio)")
    slug = slugify(name)
    if not slug:
        fail("website name has no usable characters for a slug")

    site_dir = SITES / slug
    if site_dir.exists():
        fail(f"{site_dir} already exists")

    domain = f"{slug}.{provision['DOMAIN_SUFFIX']}"
    port = pick_port()

    admin_user = ask("WordPress admin username", "admin")
    admin_pass = getpass.getpass("WordPress admin password [generate]: ").strip()
    generated = not admin_pass
    if generated:
        admin_pass = secrets.token_urlsafe(18)
    admin_email = ask("WordPress admin email", provision["WP_ADMIN_EMAIL"])

    token = provision["CLOUDFLARE_TUNNEL_TOKEN"]
    tunnel_name = provision["TUNNEL_NAME"]
    print(f"\n  {name} -> https://{domain}  (local: http://127.0.0.1:{port})")
    print("  publishing via: " +
          ("this site's own tunnel (token from .env.provision)" if token
           else f"the shared '{tunnel_name}' tunnel\n"))

    SITES.mkdir(exist_ok=True)
    shutil.copytree(BLUEPRINT, site_dir,
                    ignore=shutil.ignore_patterns(*NOT_TEMPLATE))
    for sub in ("site", "secrets", "backups/database", "backups/files"):
        (site_dir / sub).mkdir(parents=True, exist_ok=True)

    write_env(site_dir, {
        "COMPOSE_PROJECT_NAME": f"wp-{slug}",
        "SITE_NAME": name,
        "WP_DOMAIN": domain,
        "LOCAL_HTTP_PORT": port,
        "HOST_UID": os.getuid(),
        "HOST_GID": os.getgid(),
    })
    write_secrets(site_dir, token)

    print("Pulling the latest WordPress image...")
    run(["docker", "compose", "build", "--pull", "wordpress"], site_dir)

    print("Starting the stack...")
    run(["./bin/start"], site_dir)

    print("Waiting for the database...")
    wait_for_db(site_dir)

    print("Installing WordPress...")
    run(["./bin/wp", "core", "install",
         f"--url=https://{domain}", f"--title={name}",
         f"--admin_user={admin_user}", f"--admin_password={admin_pass}",
         f"--admin_email={admin_email}", "--skip-email"], site_dir)

    if not token:
        print(f"Publishing via the '{tunnel_name}' tunnel...")
        publish(domain, port, tunnel_name)

    version = run(["./bin/wp", "core", "version"], site_dir,
                  capture=True).stdout.strip()

    print(f"""
{'=' * 62}
  {name} — WordPress {version}

  Directory   {site_dir}
  Public      https://{domain}
  Local       http://127.0.0.1:{port}
  Admin       https://{domain}/wp-admin

  Username    {admin_user}
  Password    {admin_pass}{'   (generated — save it now)' if generated else ''}
{'=' * 62}
""")
    if token:
        print(f"""This site runs its own cloudflared. Add its route in the dashboard:

  {domain}  ->  http://nginx:80
""")
    else:
        print(f"""Live now — nothing left to do in Cloudflare.

Route and DNS were added to the '{tunnel_name}' tunnel automatically.
Give the DNS record a moment, then: curl -sI https://{domain}
""")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        sys.exit("\naborted")

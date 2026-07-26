#!/usr/bin/env python3
"""Interactive menu for the WordPress sites.

Lists every site with its status, and starts, closes, or creates one.

    python3 sites.py
"""

import datetime
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SITES = ROOT / "sites"
TUNNEL = ROOT / "tunnel"

# Regenerable, and big enough to double a backup's size. `.git` is deliberately
# NOT here: a plugin or theme may be its own repository, and its history is not
# recoverable from anywhere else on this machine.
SKIP_IN_BACKUP = {"node_modules", "cache", "upgrade"}

COLOR = sys.stdout.isatty()
GREEN, RED, DIM, OFF = ("\033[32m", "\033[31m", "\033[2m", "\033[0m") if COLOR \
    else ("", "", "", "")


def env_value(text, key, default=""):
    found = re.search(rf"^{key}=(.*)$", text, re.M)
    return found.group(1).strip() if found else default


def running_projects():
    """Compose projects with at least one container up — one docker call, not one per site."""
    out = subprocess.run(
        ["docker", "ps", "--format", '{{.Label "com.docker.compose.project"}}'],
        capture_output=True, text=True)
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


def load_sites():
    if not SITES.is_dir():
        return []
    up = running_projects()
    sites = []
    for path in sorted(p for p in SITES.iterdir() if p.is_dir()):
        env = path / ".env"
        if not env.is_file():
            continue
        text = env.read_text()
        sites.append({
            "slug": path.name,
            "dir": path,
            "name": env_value(text, "SITE_NAME", path.name),
            "domain": env_value(text, "WP_DOMAIN"),
            "port": env_value(text, "LOCAL_HTTP_PORT"),
            "running": env_value(text, "COMPOSE_PROJECT_NAME",
                                 f"wp-{path.name}") in up,
        })
    return sites, up


def show(sites, up):
    print(f"\n  {'#':<3} {'SITE':<20} {'STATUS':<9} {'PORT':<6} URL")
    print(f"  {DIM}{'-' * 74}{OFF}")
    if not sites:
        print(f"  {DIM}no sites yet — press (n) to create one{OFF}")
    for i, s in enumerate(sites, 1):
        # Pad the plain label before wrapping it in colour, or the invisible ANSI
        # bytes count toward the field width and the columns drift.
        label = "● started" if s["running"] else "○ closed"
        color = GREEN if s["running"] else RED
        print(f"  {i:<3} {s['name'][:20]:<20} {color}{label:<9}{OFF} "
              f"{s['port']:<6} https://{s['domain']}")

    # Sites are only reachable publicly while the shared tunnel is up, so a site
    # showing "started" with the tunnel down would otherwise be confusing.
    if (TUNNEL / "compose.yaml").is_file():
        state = "up" if "wp-tunnel" in up else "down"
        color = GREEN if state == "up" else RED
        print(f"\n  {DIM}shared tunnel:{OFF} {color}{state}{OFF}")


def pick(sites, prompt):
    choice = input(prompt).strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(sites):
        print("  not a listed number")
        return None
    return sites[int(choice) - 1]


def backup(site):
    """Dump the database and wp-content into one dated zip under the site's backups/.

    Needs the site started — the dump comes from the live MySQL container.
    """
    content = site["dir"] / "site" / "wp-content"
    if not content.is_dir():
        print(f"  {RED}no wp-content at {content}{OFF}")
        return

    print(f"\nBacking up {site['name']}...")
    # -T matters: with a TTY, Docker rewrites newlines and corrupts the SQL.
    # The dump is held in memory, which is fine at these sizes; if a site's
    # database ever outgrows RAM, stream it to a temp file first.
    dump = subprocess.run(
        ["docker", "compose", "exec", "-T", "--user", "www-data",
         "-e", "HOME=/tmp", "wordpress",
         "wp", "--path=/var/www/html", "db", "export", "-"],
        cwd=site["dir"], capture_output=True)
    if dump.returncode != 0 or not dump.stdout:
        detail = dump.stderr.decode(errors="replace").strip().splitlines()
        print(f"  {RED}database export failed:{OFF} "
              f"{detail[-1] if detail else 'empty dump'}")
        return
    print(f"  database dumped ({len(dump.stdout) / 1e6:.1f} MB)")

    dest_dir = site["dir"] / "backups"
    dest_dir.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M")
    dest = dest_dir / f"{site['slug']}-{stamp}.zip"

    files = 0
    # Write to a partial name first so an interrupted run cannot leave behind
    # something that looks like a usable backup.
    partial = dest.with_suffix(".zip.partial")
    try:
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("database.sql", dump.stdout)
            for path in sorted(content.rglob("*")):
                relative = path.relative_to(content)
                if SKIP_IN_BACKUP.intersection(relative.parts):
                    continue
                if path.is_file() and not path.is_symlink():
                    archive.write(path, Path("wp-content") / relative)
                    files += 1
        partial.rename(dest)
    except OSError as e:
        partial.unlink(missing_ok=True)
        print(f"  {RED}backup failed:{OFF} {e}")
        return

    print(f"  {files} files from wp-content")
    print(f"  wrote backups/{dest.name} ({dest.stat().st_size / 1e6:.1f} MB)")


def run(cmd, cwd):
    result = subprocess.run(cmd, cwd=cwd)
    if result.returncode != 0:
        print(f"  {RED}failed:{OFF} {' '.join(str(c) for c in cmd)}")


def main():
    while True:
        sites, up = load_sites()
        show(sites, up)

        print(f"\n  (s) start a site   (c) close a site   "
              f"(b) backup a site   (n) new site   (q) quit")
        action = input("  > ").strip().lower()

        if action in ("q", "quit", "exit"):
            return

        elif action == "s":
            if not sites:
                print("  nothing to start")
                continue
            site = pick(sites, "  start which? ")
            if not site:
                continue
            if site["running"]:
                print(f"  {site['name']} is already started")
                continue
            print(f"\nStarting {site['name']}...")
            run(["./bin/start"], site["dir"])

        elif action == "c":
            if not sites:
                print("  nothing to close")
                continue
            site = pick(sites, "  close which? ")
            if not site:
                continue
            if not site["running"]:
                print(f"  {site['name']} is already closed")
                continue
            # bin/stop is `compose down` — containers go, database and files stay.
            print(f"\nClosing {site['name']}...")
            run(["./bin/stop"], site["dir"])

        elif action == "b":
            if not sites:
                print("  nothing to back up")
                continue
            site = pick(sites, "  back up which? ")
            if not site:
                continue
            if not site["running"]:
                print(f"  {site['name']} is closed — start it first, "
                      f"the dump comes from the live database")
                continue
            backup(site)

        elif action == "n":
            print()
            run([sys.executable, "new-site.py"], ROOT)

        else:
            print("  pick s, c, b, n, or q")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print()

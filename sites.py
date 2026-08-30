#!/usr/bin/env python3
"""Interactive menu for the WordPress sites.

Lists every site with its status, and starts, closes, or creates one.

    python3 sites.py
"""

import datetime
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SITES = ROOT / "sites"
TUNNEL = ROOT / "tunnel"
VENV = ROOT / ".venv"

# questionary is the only dependency in the repo. Debian's Python refuses
# `pip install` outside a virtualenv (PEP 668), so rather than making you set
# one up, the script owns one and re-execs itself into it on first run.
# `python3 sites.py` keeps working exactly as before.
try:
    import questionary
except ImportError:                                 # pragma: no cover
    if os.environ.get("SITES_BOOTSTRAPPED"):
        sys.exit("questionary is still missing after installing into .venv")
    python = VENV / "bin" / "python"
    if not python.exists():
        print("First run: creating .venv...")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    print("Installing questionary...")
    subprocess.run([str(python), "-m", "pip", "install", "-q", "questionary"],
                   check=True)
    os.environ["SITES_BOOTSTRAPPED"] = "1"
    os.execv(str(python), [str(python), *sys.argv])

# Regenerable, and big enough to double a backup's size. `.git` is deliberately
# NOT here: a plugin or theme may be its own repository, and its history is not
# recoverable from anywhere else on this machine.
SKIP_IN_BACKUP = {"node_modules", "cache", "upgrade"}

COLOR = sys.stdout.isatty()
GREEN, RED, DIM, OFF = ("\033[32m", "\033[31m", "\033[2m", "\033[0m") if COLOR \
    else ("", "", "", "")

STYLE = questionary.Style([
    ("qmark", "fg:cyan bold"),
    ("question", "bold"),
    ("pointer", "fg:cyan bold"),
    ("highlighted", "fg:cyan bold"),
    ("answer", "fg:cyan"),
])


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
            "adminneo_port": env_value(text, "ADMINNEO_PORT"),
            "running": env_value(text, "COMPOSE_PROJECT_NAME",
                                 f"wp-{path.name}") in up,
        })
    return sites, up


def show(sites, up):
    print(f"\n  {'#':<3} {'SITE':<20} {'STATUS':<9} {'PORT':<6} URL")
    print(f"  {DIM}{'-' * 74}{OFF}")
    if not sites:
        print(f"  {DIM}no sites yet — pick 'new site' below{OFF}")
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


def choose(candidates, verb, nothing_to_do):
    """Arrow-key list of only the sites this action can act on, plus a way out.

    Each action filters first, so `close` never offers a closed site and you are
    never told a choice was invalid after making it. Esc and Ctrl-C both come
    back as None, same as picking "back".
    """
    if not candidates:
        print(f"  {nothing_to_do}")
        return None

    choices = [
        questionary.Choice(
            f"{'●' if s['running'] else '○'} {s['name'][:24]:<24} port {s['port']}",
            value=s)
        for s in candidates
    ]
    choices.append(questionary.Choice("← back", value=None))
    return questionary.select(f"{verb} which?", choices=choices, style=STYLE,
                              qmark="›", instruction=" ").ask()


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


def adminneo_up(site):
    out = subprocess.run(["docker", "compose", "ps", "--services",
                          "--filter", "status=running"],
                         cwd=site["dir"], capture_output=True, text=True)
    return "adminneo" in out.stdout.split()


def database_ui(site):
    """Toggle the AdminNeo container for this site.

    A toggle rather than a plain start, so the UI does not sit exposed
    indefinitely after you are done with it — the whole point of the profile.
    """
    if not site["adminneo_port"]:
        print(f"  {RED}no ADMINNEO_PORT in {site['slug']}/.env{OFF} — "
              f"this site predates the database UI")
        return

    if adminneo_up(site):
        print(f"\nStopping the database UI for {site['name']}...")
        # rm, not stop: a stopped-but-present container can outlive the network
        # it was attached to (e.g. if the site is later closed and reopened),
        # leaving a stale network reference that fails on the next start.
        # Stateless besides read-only config mounts, so removal costs nothing.
        run(["docker", "compose", "--profile", "tools", "rm", "-s", "-f", "adminneo"],
            site["dir"])
        return

    print(f"\nStarting the database UI for {site['name']}...")
    run(["docker", "compose", "--profile", "tools", "up", "-d", "adminneo"],
        site["dir"])

    print(f"""
  http://127.0.0.1:{site['adminneo_port']}      (opens logged in)

  Pick "database UI" again to shut it down.""")


def open_folder(site):
    """Show the site directory in the desktop file manager.

    Under WSL that means Windows Explorer, reached through `explorer.exe` on the
    interop PATH. It only understands Windows paths, so the directory is passed
    as the cwd and `.` as the argument — and it exits 1 even when it worked,
    which is why this does not go through run().
    """
    if shutil.which("explorer.exe"):
        subprocess.run(["explorer.exe", "."], cwd=site["dir"])
    elif shutil.which("xdg-open"):
        subprocess.run(["xdg-open", str(site["dir"])])
    else:
        print(f"  {RED}no file manager found{OFF} — the site is at {site['dir']}")
        return
    print(f"\n  opened {site['dir']}")
    print(f"  {DIM}WordPress files are under site/, backups under backups/{OFF}")


def refresh(site):
    """Apply configuration changes to a site.

    --build catches Dockerfile edits; --force-recreate is what makes mounted
    config (php ini, nginx template) and .env values actually take effect — a
    plain `up -d` sees no change and leaves the old container running. db is left
    alone; no PHP or nginx setting justifies bouncing the database.
    """
    print(f"\nApplying config for {site['name']}...")
    run(["docker", "compose", "up", "-d", "--build", "--force-recreate",
         "wordpress", "nginx"], site["dir"])


def run(cmd, cwd):
    result = subprocess.run(cmd, cwd=cwd)
    if result.returncode != 0:
        print(f"  {RED}failed:{OFF} {' '.join(str(c) for c in cmd)}")


MENU = [
    ("start",       "▶  start a site"),
    ("close",       "■  close a site"),
    ("refresh",     "↻  refresh config"),
    ("backup",      "⤓  backup"),
    ("database",    "▤  database UI"),
    ("folder",      "⌸  open folder"),
    (None,          questionary.Separator()),
    ("new",         "+  new site"),
    ("delete",      "✕  delete a site"),
    ("quit",        "⏻  quit"),
]


def main():
    while True:
        sites, up = load_sites()
        show(sites, up)

        choices = [item if key is None else questionary.Choice(item, value=key)
                   for key, item in MENU]
        # Ctrl-C and Esc give None, which reads the same as picking quit.
        action = questionary.select("", choices=choices, style=STYLE,
                                    qmark="›", instruction=" ").ask()

        if action in (None, "quit"):
            return

        elif action == "start":
            site = choose([s for s in sites if not s["running"]],
                          "start", "every site is already started")
            if site:
                print(f"\nStarting {site['name']}...")
                run(["./bin/start"], site["dir"])

        elif action == "close":
            site = choose([s for s in sites if s["running"]],
                          "close", "no site is started")
            if site:
                # bin/stop is `compose down` — containers go, data stays.
                print(f"\nClosing {site['name']}...")
                run(["./bin/stop"], site["dir"])

        elif action == "refresh":
            # Closed sites are offered too: refreshing one brings it back up
            # with the new config, which is usually what you want.
            site = choose(sites, "refresh", "no sites yet — pick 'new site'")
            if site:
                refresh(site)

        elif action == "backup":
            site = choose([s for s in sites if s["running"]], "back up",
                          "no site is started — a backup reads the live database")
            if site:
                backup(site)

        elif action == "database":
            site = choose([s for s in sites if s["running"]], "open the DB of",
                          "no site is started — AdminNeo talks to the live database")
            if site:
                database_ui(site)

        elif action == "folder":
            # Closed sites are listed too — the files are on disk either way.
            site = choose(sites, "open the folder of", "no sites yet")
            if site:
                open_folder(site)

        elif action == "new":
            print()
            run([sys.executable, "new-site.py"], ROOT)

        elif action == "delete":
            # Picking the site is all this does. remove-site.py owns the typed
            # confirmation and the deletion itself, so the destructive path
            # stays in exactly one place.
            site = choose(sites, "DELETE", "no sites yet")
            if site:
                print()
                run([sys.executable, "remove-site.py", site["slug"]], ROOT)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print()

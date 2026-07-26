#!/usr/bin/env python3
"""Remove a WordPress site created by new-site.py.

Reverses everything that script did: containers and database volume, site files,
the tunnel ingress rule, and the DNS record.

    python3 remove-site.py [slug]

DNS deletion needs CLOUDFLARE_API_TOKEN in .env.provision (Zone > DNS > Edit on
masayagh.com). Without it every other step still runs and the record is left for
you to delete by hand.
"""

import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SITES = ROOT / "sites"
TUNNEL = ROOT / "tunnel"
API = "https://api.cloudflare.com/client/v4"


def fail(msg):
    sys.exit(f"error: {msg}")


def read_provision():
    values = {"CLOUDFLARE_API_TOKEN": "", "DOMAIN_SUFFIX": "masayagh.com"}
    path = ROOT / ".env.provision"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip("'\"")
    return values


def api(path, token, method="GET"):
    req = urllib.request.Request(f"{API}{path}", method=method,
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b'{"success": false, "errors": []}')
    except OSError as e:
        return {"success": False, "errors": [{"message": str(e)}]}


def delete_dns(domain, suffix, token):
    """Delete the CNAME new-site.py created. Returns a status string."""
    zones = api(f"/zones?name={suffix}", token)
    if not zones.get("success") or not zones.get("result"):
        detail = "; ".join(e.get("message", "?") for e in zones.get("errors", []))
        return f"could not look up zone {suffix}: {detail or 'no result'}"
    zone_id = zones["result"][0]["id"]

    records = api(f"/zones/{zone_id}/dns_records?name={domain}", token)
    if not records.get("success"):
        detail = "; ".join(e.get("message", "?") for e in records.get("errors", []))
        return f"could not list records: {detail}"
    if not records["result"]:
        return f"no DNS record for {domain} (already gone)"

    for record in records["result"]:
        result = api(f"/zones/{zone_id}/dns_records/{record['id']}", token, "DELETE")
        if not result.get("success"):
            detail = "; ".join(e.get("message", "?") for e in result.get("errors", []))
            return f"delete failed: {detail}"
    return f"deleted DNS record for {domain}"


def drop_ingress(domain):
    """Remove this site's rule from the shared tunnel config."""
    config = TUNNEL / "config.yml"
    if not config.exists():
        return "no tunnel config"
    text = config.read_text()
    # Matches the block new-site.py writes, including its trailing blank line.
    pattern = rf"  - hostname: {re.escape(domain)}\n    service: [^\n]*\n\n?"
    new_text, count = re.subn(pattern, "", text)
    if not count:
        return f"no ingress rule for {domain}"
    config.write_text(new_text)
    subprocess.run(["docker", "compose", "up", "-d", "--force-recreate"],
                   cwd=TUNNEL, capture_output=True)
    return "removed ingress rule and reloaded cloudflared"


def main():
    provision = read_provision()
    suffix = provision["DOMAIN_SUFFIX"]

    available = sorted(p.name for p in SITES.iterdir() if p.is_dir()) \
        if SITES.is_dir() else []
    if not available:
        fail("no sites to remove")

    slug = sys.argv[1] if len(sys.argv) > 1 else ""
    if not slug:
        print("Sites:", ", ".join(available))
        slug = input("Remove which? ").strip()
    if slug not in available:
        fail(f"no site '{slug}' (have: {', '.join(available)})")

    site_dir = SITES / slug
    domain = f"{slug}.{suffix}"

    print(f"\nThis permanently deletes {site_dir}, its database, and {domain}.")
    if input(f"Type '{slug}' to confirm: ").strip() != slug:
        sys.exit("aborted")

    print("\nStopping containers and deleting the database volume...")
    subprocess.run(["docker", "compose", "down", "-v"],
                   cwd=site_dir, capture_output=True)

    print(" ", drop_ingress(domain))

    token = provision["CLOUDFLARE_API_TOKEN"]
    if token:
        print(" ", delete_dns(domain, suffix, token))
    else:
        print(f"  no CLOUDFLARE_API_TOKEN — delete the {domain} record manually")

    shutil.rmtree(site_dir)
    print(f"  removed {site_dir}")
    print(f"\n{slug} is gone.")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        sys.exit("\naborted")

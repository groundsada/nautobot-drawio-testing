#!/usr/bin/env python3
"""
fetch_logos.py -- refresh the vendored brand logos in logos/.

    python3 tools/fetch_logos.py            # the default vendor list
    python3 tools/fetch_logos.py arista ciena

Logos come from simple-icons (CC0) on jsDelivr, filled with each brand's own
colour. They are committed so /draw never needs the network to put a logo on
the drawing. Brands simple-icons does not carry are left to llm_edit.py, which
draws a text badge instead; any .svg/.png dropped into logos/ by hand wins.
"""
import json
import pathlib
import re
import sys
import urllib.request

VERSION = "13.21.0"
BASE = "https://cdn.jsdelivr.net/npm/simple-icons@%s" % VERSION
OUT = pathlib.Path(__file__).resolve().parent.parent / "logos"
DEFAULT = ["cisco", "nokia", "junipernetworks", "nvidia", "dell", "intel", "amd", "hp",
           "lenovo", "supermicro", "fortinet", "paloaltonetworks", "f5", "broadcom",
           "huawei", "redhat", "ubuntu", "linux", "kubernetes", "docker", "cloudflare",
           "mikrotik", "ubiquiti", "vmware", "oracle", "googlecloud", "mellanox"]


def slug(title):
    # simple-icons' own slug rule, enough of it for vendor names
    t = title.lower().replace("+", "plus").replace(".", "dot").replace("&", "and")
    return re.sub(r"[^a-z0-9]", "", t)


def get(url):
    with urllib.request.urlopen(url, timeout=20) as r:
        return r.read()


def main(argv=None):
    want = (argv or sys.argv[1:]) or DEFAULT
    data = json.loads(get(BASE + "/_data/simple-icons.json"))
    data = data.get("icons", data)
    colour = {i.get("slug") or slug(i["title"]): i["hex"] for i in data}
    OUT.mkdir(exist_ok=True)
    for s in want:
        if s not in colour:
            print("  - %s: not in simple-icons %s, skipped" % (s, VERSION))
            continue
        svg = get("%s/icons/%s.svg" % (BASE, s)).decode()
        svg = svg.replace("<svg ", '<svg fill="#%s" ' % colour[s], 1)
        (OUT / ("%s.svg" % s)).write_text(svg)
        print("  + %s (#%s)" % (s, colour[s]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

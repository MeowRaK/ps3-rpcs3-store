#!/usr/bin/env python3
"""Generate FPKGi+/FPKGi-compatible PS3 PKG download catalogs from NPS TSV.

Uses NO third-party dependencies. A record needs a real PKG link, a PS3 title ID,
name, and an exact file size to appear in the PS4 FPKGi+ download list.

NOTE: This exports URLs, NOT license keys; possession of a downloaded PKG does
not establish entitlement or RPCS3 compatibility. Use only with content you own
or have permission to obtain.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_SOURCES = (
    "https://nopaystation.com/tsv/PS3_GAMES.tsv",
    "https://nopaystation.org/tsv/PS3_GAMES.tsv",
    "https://github.com/jy95/pkgi-ps3-nopaystation/releases/download/latest/pkgi_games.csv",
)


def get_text(source: str) -> str:
    if source.lower().startswith(("http://", "https://")):
        req = urllib.request.Request(source, headers={"User-Agent": "PS3-Catalog-Builder/1.0", "Accept": "text/tab-separated-values,text/plain,*/*"})
        with urllib.request.urlopen(req, timeout=90) as resp:
            body = resp.read(32 * 1024 * 1024 + 1)
            if len(body) > 32 * 1024 * 1024:
                raise ValueError("TSV too large (>32 MiB)")
        return body.decode("utf-8-sig")
    return Path(source).read_text(encoding="utf-8-sig")


def clean(s: object) -> str:
    return str(s or "").strip().replace("\x00", "")


def pick(row: dict[str, str], *names: str) -> str:
    # Accept upstream header variants and BOM.
    index = {re.sub(r"[^a-z0-9]", "", clean(k).lower()): v for k, v in row.items() if k}
    for n in names:
        result = clean(index.get(re.sub(r"[^a-z0-9]", "", n.lower())))
        if result:
            return result
    return ""


def file_size(raw: str) -> int | None:
    raw = clean(raw).replace(",", "").replace(" ", "")
    if not raw or raw in ("-", "N/A", "Unknown"):
        return None
    if raw.isdigit():
        num = int(raw)
        return num if num > 0 else None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGT]?i?B|[KMGT]B|[KMGT])", raw, re.I)
    if not match:
        return None
    value, unit = match.groups()
    prefix = unit[0].upper()
    power = {"B":0, "K":1, "M":2, "G":3, "T":4}.get(prefix,0)
    base = 1024 if "i" in unit else 1000
    num = round(float(value) * (base ** power))
    return num if num > 0 else None


def region_of(raw: str) -> str | None:
    value = clean(raw).upper()
    if value in ("US", "USA", "NA", "NORTH AMERICA"):
        return "USA"
    if value in ("EU", "EUR", "EUROPE"):
        return "EUR"
    if value in ("JP", "JPN", "JAP", "JAPAN"):
        return "JAP"
    if value in ("AS", "ASIA", "HK", "HKG"):
        return "ASIA"
    return None


def valid_link(u: str) -> bool:
    try:
        p = urlsplit(u)
        # FPKGi+ supports HTTPS downloads; disallow dangerous local / non-HTTP schemes.
        return p.scheme == "https" and bool(p.netloc) and not p.username and not p.password
    except ValueError:
        return False


def load_rows(tsv: str) -> list[dict[str, str]]:
    text = tsv.replace("\x00", "")
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    if not reader.fieldnames:
        raise ValueError("TSV has no header")
    heads = {re.sub(r"[^a-z0-9]", "", h.lower()) for h in reader.fieldnames if h}
    if "name" in heads and ({"pkgdirectlink", "pkgurl", "url"} & heads):
        return list(reader)
    # Fallback source: jy95's already-converted pkgi_games.csv release asset.
    # The semicolon fields are: content ID, type, name, description, RAP, URL,
    # size bytes, checksum, date, region. RAP is deliberately ignored.
    if ";" in text[:1000] and "\t" not in text[:1000]:
        converted = []
        for fields in csv.reader(io.StringIO(text), delimiter=";"):
            if len(fields) < 7 or not fields[0] or fields[0].lower() == "contentid":
                continue
            content_id = clean(fields[0])
            converted.append({
                "Content ID": content_id,
                "Title ID": content_id[7:16] if len(content_id) >= 16 else "",
                "Name": clean(fields[2]),
                "PKG direct link": clean(fields[5]),
                "File Size": clean(fields[6]),
                "Region": clean(fields[9]) if len(fields) > 9 else "",
            })
        if converted:
            return converted
    raise ValueError(f"Not NPS TSV or jy95 pkgi_games.csv: headers={reader.fieldnames}")


def generate(rows: list[dict[str, str]], regions: set[str] | None = None, limit: int = 0) -> tuple[dict, list, dict]:
    catalog = {}
    library = []
    skips = Counter()
    seen_ids = set()
    for row in rows:
        title = pick(row, "Name", "Game Name")
        title_id = pick(row, "Title ID", "TitleID", "Game ID").upper()
        content_id = pick(row, "Content ID", "ContentID")
        if not title_id and len(content_id) >= 16:
            title_id = content_id[7:16].upper()
        reg = region_of(pick(row, "Region"))
        pkg_url = pick(row, "PKG direct link", "PKG URL", "URL")
        size = file_size(pick(row, "File Size", "Size", "Size (bytes)"))
        if regions and reg not in regions:
            skips["filtered_region"] += 1
            continue
        if not title:
            skips["missing_name"] += 1
            continue
        item = {
            "title_id": title_id or None, "name": title, "region": reg,
            "size_bytes": size, "content_id": content_id or None,
            "has_pkg_link": valid_link(pkg_url),
            # URLs kept in download catalog only; do not propagate license/RAP details.
        }
        library.append(item)
        if not re.fullmatch(r"[A-Z0-9]{9}", title_id):
            skips["missing_or_invalid_title_id"] += 1
            continue
        if not valid_link(pkg_url):
            skips["missing_or_invalid_link"] += 1
            continue
        if size is None:
            skips["missing_or_unknown_size"] += 1
            continue
        if pkg_url in catalog:
            skips["duplicate_download_url"] += 1
            continue
        entry = {
            "title_id": title_id,
            "name": title,
            "size": size,
            "region": reg,
            "version": "01.00",
            "release": None,
            "min_fw": None,
            "cover_url": None,
        }
        catalog[pkg_url] = entry
        seen_ids.add(title_id)
        if limit > 0 and len(catalog) >= limit:
            break
    report = {"source_rows_processed": len(rows), "library_entries": len(library),
              "downloadable_catalog_entries": len(catalog), "unique_title_ids_in_downloads": len(seen_ids),
              "skipped_reasons": dict(skips),
              "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "notes": ["No licenses/RAP keys are distributed.",
                        "FPKGi+ downloads PS3 files but does not install them into RPCS3.",
                        "Only download games you own or are authorized to download."]}
    return {"DATA": catalog}, library, report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", metavar="NPS.tsv", help="Use an already downloaded TSV instead of fetching the latest")
    ap.add_argument("--source-url", metavar="URL", help="Override NPS URL")
    ap.add_argument("--output", default=".", help="Output directory")
    ap.add_argument("--regions", default="", help="Only include these regions, e.g. USA,EUR,JAP")
    ap.add_argument("--limit", type=int, default=0, help="For small test catalogs, limit number of download entries")
    ap.add_argument("--allow-empty", action="store_true", help="Do not fail when zero usable download records")
    args = ap.parse_args()
    if args.limit < 0:
        ap.error("--limit must be >= 0")
    regions = {region_of(v) for v in args.regions.split(",") if v.strip()}
    if None in regions:
        ap.error("--regions may contain only USA,EUR,JAP,ASIA")
    source_list = [args.input] if args.input else [args.source_url] if args.source_url else DEFAULT_SOURCES
    errors = []
    for src in source_list:
        try:
            rows = load_rows(get_text(src))
            print(f"Loaded {len(rows)} source rows from {src}")
            break
        except Exception as exc:
            errors.append(f"{src}: {exc}")
    else:
        print("Cannot load any source:\n" + "\n".join(errors), file=sys.stderr)
        return 2
    catalog, library, report = generate(rows, regions or None, args.limit)
    if not catalog["DATA"] and not args.allow_empty:
        print("No usable downloadable entries. Refusing to overwrite an existing catalog. "
              "Check report fields/TSV schema and link/size columns.", file=sys.stderr)
        return 3
    dst = Path(args.output)
    dst.mkdir(parents=True, exist_ok=True)
    for fname, payload in (("GAMES.json",catalog),("DEMOS.json",catalog),
                           ("PS3_LIBRARY.json",library),("catalog_report.json",report)):
        temp = dst / (fname + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(dst / fname)
    print(json.dumps(report, indent=2))
    print("Use DEMOS.json in FPKGi+ Demos source to keep existing PS4 Games intact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

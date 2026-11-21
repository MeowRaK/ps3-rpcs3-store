#!/usr/bin/env python3
"""Add disc-only PS3 games to a NoPayStation library and filter non-English editions.

Runs AFTER the original build_catalog.py. FPKGi+ lists only PKGs with real URLs;
metadata-only disc titles are written to PS3_LIBRARY.json, never given dummy URLs.
No licenses, keys, bypasses, or fabricated download locations are included.
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

GAMEDB_URL = "https://github.com/niemasd/GameDB-PS3/releases/latest/download/PS3.data.json"
# Title ID-specific rules ONLY where the particular PSN edition is known.
# The regional labels USA/EUR/JAP/ASIA are NOT by themselves language evidence.
PSN_LANGUAGE_OVERRIDES = {
    "NPEB01205": ["German"],            # COD Black Ops II (German)
    "NPEB01206": ["Italian", "Spanish"], # COD Black Ops II (Italy/Spain)
    "NPEB01204": ["English", "French"], # COD Black Ops II (England/France)
}
# Avoid loose matching (e.g. 'German Shepherd', 'French Revolution').
# Only patterns that explicitly signal a release language qualify.
NON_ENGLISH = (
    "German", "Deutsch", "French", "Français", "Spanish", "Español",
    "Italian", "Italiano", "Russian", "Русский", "Polish", "Polski",
    "Portuguese", "Português", "Dutch", "Nederlands", "Turkish", "Türkçe",
    "Japanese", "日本語", "Korean", "한국어", "Chinese", "中文", "Czech", "Čeština",
    "Hungarian", "Magyar", "Arabic", "العربية", "Greek", "Ελληνικά",
)
LANGUAGE_WORDS = "|".join(re.escape(s) for s in NON_ENGLISH)
# e.g. 'Call of Duty Classic French Version', '... (German)', '(Russian, Polish)'
LANGUAGE_END = re.compile(
    rf"(?:\s*\((?P<paren>(?:{LANGUAGE_WORDS})(?:(?:[,/&+\s-]+)(?:{LANGUAGE_WORDS}))*)"
    rf"(?:\s+(?:version|ver\.?|only))?\)"
    rf"|\s*[-:]\s*(?P<suffix>{LANGUAGE_WORDS})(?:\s+(?:version|ver\.?|edition))?"
    rf"|\s+(?P<version>{LANGUAGE_WORDS})\s+(?:version|ver\.?))\s*$",
    re.I,
)
ENGLISH_WORD = re.compile(r"(?i)\b(English|UK|British|England)\b")


def clean_id(v: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(v or "").upper())


def language_list(v: object) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return []


def is_english(languages: list[str]) -> bool:
    return any(x.casefold() in ("english", "en", "en-us", "en-gb") for x in languages)


def explicitly_non_english(title: str) -> bool:
    # Optional terminal platform labels (PS2) or (PS3) do not change the language.
    title = re.sub(r"\s*\(PS[123]\)\s*$", "", title or "", flags=re.I)
    # If the same suffix also says English, it is NOT non-English-only.
    m = LANGUAGE_END.search(title)
    return bool(m) and not ENGLISH_WORD.search(m.group(0))


def classify(title: str, title_id: str, known_languages: list[str] | None = None) -> tuple[str, list[str]]:
    """Return (English / Non-English / Unknown, evidence languages)."""
    langs = PSN_LANGUAGE_OVERRIDES.get(clean_id(title_id), known_languages or [])
    if langs:
        return ("English", langs) if is_english(langs) else ("Non-English", langs)
    if explicitly_non_english(title):
        return "Non-English", []
    # A generic English title or an EU/US region is NOT proof of English audio/UI.
    return "Unknown", []


def read_gamedb(path: str | None = None) -> dict:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    request = urllib.request.Request(GAMEDB_URL, headers={"User-Agent": "PS3-Catalog-Builder/1.1"})
    with urllib.request.urlopen(request, timeout=90) as response:
        raw = response.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("GameDB JSON was unexpectedly large")
    return json.loads(raw.decode("utf-8-sig"))


def gamedb_records(data: dict):
    """GameDB-PS3 format: {'BLUS-30481': {'title':'Nier','language':['English',...]}}"""
    if not isinstance(data, dict):
        raise ValueError("Expected GameDB top-level JSON object indexed by disc serial")
    for serial, record in data.items():
        if not isinstance(record, dict):
            continue
        title = str(record.get("title") or "").strip()
        title_id = clean_id(record.get("serial") or serial)
        if title and re.fullmatch(r"[A-Z0-9]{9}", title_id):
            yield title_id, title, language_list(record.get("language")), str(record.get("region") or "")


def region_label(text: str) -> str | None:
    t = text.casefold()
    if "ntsc-u" in t or "usa" in t or "america" in t:
        return "USA"
    if "pal" in t or "europe" in t:
        return "EUR"
    if "ntsc-j" in t or "japan" in t:
        return "JAP"
    return None


def enhance(download_catalog: dict, metadata: list[dict], disc_data: dict):
    if not isinstance(download_catalog, dict) or not isinstance(download_catalog.get("DATA"), dict):
        raise ValueError("GAMES.json has no DATA object")
    if not isinstance(metadata, list):
        raise ValueError("PS3_LIBRARY.json must be a list")
    disc = list(gamedb_records(disc_data))
    if len(disc) == 0:
        raise ValueError("GameDB did not contain any valid disc records")
    # Reject a synthetic fixture only in tests; production source is expected to contain many rows.
    disc_by_id = {title_id: (title, langs, reg) for title_id, title, langs, reg in disc}
    existing_by_id = {}
    retained_meta = []
    excluded = []
    stats = Counter()
    for raw in metadata:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        title = str(item.get("name") or "")
        title_id = clean_id(item.get("title_id"))
        disc_evidence = disc_by_id.get(title_id)
        language_state, langs = classify(title, title_id, disc_evidence[1] if disc_evidence else None)
        if language_state == "Non-English":
            stats["filtered_existing_library"] += 1
            excluded.append({"title_id": title_id, "name": title,
                             "reason": "Explicit non-English edition", "languages": langs})
            continue
        if disc_evidence and langs:
            item["languages"] = langs
        item["language_status"] = language_state
        item.setdefault("source", "NoPayStation")
        retained_meta.append(item)
        existing_by_id.setdefault(title_id, []).append(item)
    filtered = {}
    for url, record in download_catalog["DATA"].items():
        if not isinstance(record, dict):
            continue
        state, langs = classify(str(record.get("name") or ""), str(record.get("title_id") or ""),
                               disc_by_id.get(clean_id(record.get("title_id")), (None, [], None))[1])
        if state == "Non-English":
            stats["filtered_downloads"] += 1
            excluded.append({"title_id": clean_id(record.get("title_id")),
                             "name": record.get("name"), "reason": "Explicit non-English edition",
                             "languages": langs})
            continue
        # Keep the FPKGi+ JSON schema unchanged. Additional metadata stays in the library.
        filtered[url] = record
    downloadable_ids = {clean_id(v.get("title_id")) for v in filtered.values()}
    for title_id, title, languages, region in disc:
        # GameDB disc metadata itself supplies real language evidence.
        state, langs = classify(title, title_id, languages)
        if state == "Non-English":
            stats["filtered_disc_entries"] += 1
            continue
        if title_id in existing_by_id:
            stats["disc_ids_already_in_library"] += 1
            continue
        retained_meta.append({
            "title_id": title_id, "name": title, "region": region_label(region),
            "size_bytes": None, "content_id": None, "has_pkg_link": False,
            "source": "GameDB-PS3 (disc metadata)", "medium": "Disc",
            "languages": langs, "language_status": state,
        })
        existing_by_id[title_id] = [retained_meta[-1]]
        stats["disc_entries_added"] += 1
    retained_meta.sort(key=lambda v: (str(v.get("name", "")).casefold(), str(v.get("title_id", ""))))
    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "disc_source": GAMEDB_URL,
        "disc_records_read": len(disc),
        "library_entries_after_filter_and_merge": len(retained_meta),
        "downloadable_entries_before": len(download_catalog["DATA"]),
        "downloadable_entries_after": len(filtered),
        "downloadable_title_ids_after": len(downloadable_ids),
        **dict(stats),
        "excluded_examples": excluded[:30],
        "caveat": "Unknown-language NPS titles are preserved; region alone does not prove language. Disc-only entries have no downloads and do not appear in FPKGi+.",
    }
    return {"DATA": filtered}, retained_meta, excluded, report


def write_json(path: Path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=".", help="Folder with existing GAMES.json and PS3_LIBRARY.json")
    parser.add_argument("--disc-data", help="Local GameDB data.json (for offline tests)")
    args = parser.parse_args()
    directory = Path(args.output)
    original = json.loads((directory / "GAMES.json").read_text(encoding="utf-8"))
    library = json.loads((directory / "PS3_LIBRARY.json").read_text(encoding="utf-8"))
    data = read_gamedb(args.disc_data)
    catalog, merged, exclusions, report = enhance(original, library, data)
    if not catalog["DATA"]:
        raise RuntimeError("Refusing to replace the current catalog with zero downloads")
    if len(list(gamedb_records(data))) < 100 and not args.disc_data:
        raise RuntimeError("The live disc database is suspiciously small")
    write_json(directory / "GAMES.json", catalog)
    write_json(directory / "DEMOS.json", catalog)
    write_json(directory / "PS3_LIBRARY.json", merged)
    write_json(directory / "PS3_LANGUAGE_EXCLUDED.json", exclusions)
    write_json(directory / "language_filter_report.json", report)
    path = directory / "catalog_report.json"
    if path.exists():
        old_report = json.loads(path.read_text(encoding="utf-8"))
        old_report["enhancement"] = report
        write_json(path, old_report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

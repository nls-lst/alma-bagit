"""Harvest Alma sets into a BagIt package: one folder per record holding the
MARCXML and the file linked from its electronic portfolio.

    uv run alma_bag.py                     # harvest the sets, then bag them

Or a step at a time, when a harvest needs more than one attempt:

    uv run alma_bag.py harvest             # re-run to retry what failed
    uv run alma_bag.py harvest --limit 50  # test run over the first few records
    uv run alma_bag.py bag                 # bag and zip what's downloaded
    uv run alma_bag.py sample              # small bag of a few records, to send for checking

report.csv lists every record with its status, title and catalogue details.

Sets and defaults live in config.toml; the API key in ALMA_API_KEY or .env.
"""

import argparse
import csv
import functools
import itertools
import json
import os
import shutil
import time
import tomllib
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import bagit
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HERE = Path(__file__).parent
MARC_NS = "http://www.loc.gov/MARC21/slim"
FIELDS = [
    "mms_id",
    "title",
    "status",
    "portfolio_id",
    "portfolios",
    "file",
    "url",
    "856_url",
    "created",
    "created_by",
    "modified",
    "modified_by",
    "originating_system",
    "suppressed",
]
PAYLOAD = (".pdf", ".epub")
DEFAULTS = {
    "base": "https://api-eu.hosted.exlibrisgroup.com/almaws/v1",
    "out": "output",
    "work": "harvest",
    "name": "BAG_{date}",
    "workers": 8,
    "sets": [],
}
CONFIG = {}


# config.toml over the defaults, with any [sections] flattened.
def load_config(path):
    if not path.exists():
        raise SystemExit(f"No {path} - copy config.toml.example to config.toml and fill it in.")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    sections = {key: value for section in raw.values() if isinstance(section, dict) for key, value in section.items()}
    return DEFAULTS | sections | {key: value for key, value in raw.items() if not isinstance(value, dict)}


def load_dotenv():
    path = HERE / ".env"
    for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
        name, sep, value = line.strip().partition("=")
        if sep and not name.startswith("#"):
            os.environ.setdefault(name.strip(), value.strip().strip("\"'"))


# Retries for roughly 7 minutes before giving up, for slow or stalling file hosts.
def session(**headers):
    made = requests.Session()
    made.headers.update(headers)
    retry = Retry(total=8, backoff_factor=3, backoff_max=120, status_forcelist=(429, 500, 502, 503, 504))
    made.mount("https://", HTTPAdapter(pool_maxsize=32, max_retries=retry))
    return made


files = session(**{"User-Agent": "Mozilla/5.0 (compatible; alma-bagit/1.0)"})


# Built on first use, so the offline steps need no key.
@functools.lru_cache(maxsize=1)
def alma():
    load_dotenv()
    key = (os.environ.get("ALMA_API_KEY") or os.environ.get("ALMA_API_PROD") or "").strip()
    if not key:
        raise SystemExit("No Alma API key - set ALMA_API_KEY, or put ALMA_API_KEY=<key> in a .env file.")
    return session(Authorization=f"apikey {key}")


def get(path, **params):
    response = alma().get(f"{CONFIG['base']}/{path}", params=params, timeout=120)
    if response.status_code in (401, 403):
        raise SystemExit(f"Alma rejected the API key (HTTP {response.status_code}) on {path}.")
    response.raise_for_status()
    return ET.fromstring(response.content)


# Every $<code> of every <tag> field in a MARCXML record.
def subfields(record, tag, code):
    return [
        sub.text.strip()
        for field in record.findall("datafield")
        if field.get("tag") == tag
        for sub in field.findall("subfield")
        if sub.get("code") == code and sub.text
    ]


def row_for(mms_id, **values):
    return dict.fromkeys(FIELDS, "") | {"mms_id": mms_id, "portfolios": 0} | values


# --- harvest ----------------------------------------------------------------


def set_members(set_id):
    members, offset = [], 0
    while True:
        root = get(f"conf/sets/{set_id}/members", limit=100, offset=offset)
        page = [el.text.strip() for el in root.findall("member/id") if el.text]
        members += page
        offset += 100
        if not page or offset >= int(root.get("total_record_count", 0)):
            return members


def save_marcxml(record, path):
    # Drop the AVA/AVD/AVE availability fields that expand=e_avail adds.
    for field in [f for f in record.findall("datafield") if f.get("tag", "").startswith("AV")]:
        record.remove(field)
    for el in record.iter():
        el.tag = f"{{{MARC_NS}}}{el.tag}"
    collection = ET.Element(f"{{{MARC_NS}}}collection")
    collection.append(record)
    ET.register_namespace("", MARC_NS)
    ET.indent(collection, "  ")
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(collection).write(path, encoding="utf-8", xml_declaration=True)


# Fetches bibs 100 at a time, saving the MARCXML. Returns a row per record.
def fetch_bibs(mms_ids, root):
    rows = {}
    for start in range(0, len(mms_ids), 100):
        batch = mms_ids[start : start + 100]
        for bib in get("bibs", mms_id=",".join(batch), view="full", expand="e_avail").findall("bib"):
            field = {el.tag: (el.text or "").strip() for el in bib}
            mms_id = field.get("mms_id", "")
            record = bib.find("record")
            # AVE $8 is the portfolio ID.
            portfolios = subfields(record, "AVE", "8") if record is not None else []
            links = subfields(record, "856", "u") if record is not None else []
            row = rows[mms_id] = row_for(
                mms_id,
                portfolios=len(portfolios),
                title=field.get("title", ""),
                created=field.get("created_date", "")[:10],
                created_by=field.get("created_by", ""),
                modified=field.get("last_modified_date", "")[:10],
                modified_by=field.get("last_modified_by", ""),
                originating_system=field.get("originating_system", ""),
                suppressed=field.get("suppress_from_publishing", ""),
                **{"856_url": links[0] if links else ""},
            )
            if record is None:
                row["status"] = "no marcxml returned"
            elif not portfolios:
                row["status"] = "no portfolio attached"
            elif len(portfolios) > 1:
                row["status"] = f"{len(portfolios)} portfolios: {' '.join(portfolios)}"
            else:
                row["portfolio_id"] = portfolios[0]
                save_marcxml(record, root / mms_id / f"{mms_id}.xml")
        for missing in set(batch) - set(rows):
            rows[missing] = row_for(missing, status="bib not returned")
        print(f"  {min(start + 100, len(mms_ids))}/{len(mms_ids)} bibs", flush=True)
    return list(rows.values())


# Alma prefixes static links with `jkey=`.
def portfolio_url(mms_id, portfolio_id):
    details = get(f"bibs/{mms_id}/portfolios/{portfolio_id}").find("linking_details")
    for tag in ("static_url_override", "url", "static_url", "dynamic_url"):
        url = (details.findtext(tag) or "").strip() if details is not None else ""
        if url:
            return url.removeprefix("jkey=").strip()
    return ""


# The downloaded file for a record, ignoring zero-byte files.
def payload_file(row, root):
    folder = root / row["mms_id"]
    hits = (p for p in folder.glob(f"{row['mms_id']}.*") if p.suffix in PAYLOAD and p.stat().st_size)
    return next(hits, None)


def marcxml(row, root):
    return root / row["mms_id"] / f"{row['mms_id']}.xml"


def record_complete(row, root):
    return bool(row["portfolio_id"] and marcxml(row, root).exists() and payload_file(row, root))


def fetch_file(row, root):
    done = payload_file(row, root)
    if done:
        row.update(file=done.name, status="ok")
        return
    try:
        row["url"] = row.get("url") or portfolio_url(row["mms_id"], row["portfolio_id"])
        if not row["url"]:
            row["status"] = "portfolio has no link"
            return
        response = files.get(row["url"], timeout=300)
        response.raise_for_status()
        if not response.content:
            raise ValueError("empty response body")
        suffix = Path(urlparse(row["url"]).path).suffix.lower()
        if suffix not in PAYLOAD:
            suffix = ".epub" if "epub" in response.headers.get("Content-Type", "") else ".pdf"
        path = root / row["mms_id"] / (row["mms_id"] + suffix)
        path.write_bytes(response.content)
        row.update(file=path.name, status="ok")
    except Exception as exc:  # noqa: BLE001
        row["status"] = f"failed: {type(exc).__name__}: {exc}"


def load_rows(args, root, cache):
    if cache.exists() and not args.refresh:
        rows = json.loads(cache.read_text(encoding="utf-8"))[: args.limit]
        print(f"Reusing metadata for {len(rows)} records ({cache}; --refresh to rebuild)")
        # The MARCXML isn't cached, so a fresh harvest folder needs it again.
        missing = [r["mms_id"] for r in rows if r["portfolio_id"] and not marcxml(r, root).exists()]
        if missing:
            print(f"Fetching MARCXML for {len(missing)} records...")
            fresh = {r["mms_id"]: r for r in fetch_bibs(missing, root)}
            for row in rows:
                new = fresh.get(row["mms_id"])
                if new:
                    row.update(new, url=row["url"] if new["portfolio_id"] == row["portfolio_id"] else "")
        return rows
    if not args.sets:
        raise SystemExit("No sets to harvest - add them to config.toml or pass --sets.")
    print("Fetching set members...")
    mms_ids = list(dict.fromkeys(mms for s in args.sets for mms in set_members(s)))[: args.limit]
    print(f"  {len(mms_ids)} unique records\nFetching bibs...")
    rows = fetch_bibs(mms_ids, root)
    if not args.limit:
        cache.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return rows


def download_all(rows, root, workers):
    todo = [r for r in rows if r["portfolio_id"]]
    counter = itertools.count(1)

    def download(row):
        fetch_file(row, root)
        done = next(counter)
        if done % 100 == 0 or done == len(todo):
            failed = sum(1 for r in todo if r["status"] not in ("", "ok"))
            print(f"  {done}/{len(todo)} ({failed} problems)", flush=True)

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(download, todo))


# Refreshes each row's status from what's on disk, and writes the CSV.
def write_report(rows, root, report, announce=True):
    done = [r for r in rows if record_complete(r, root)]
    for row in done:
        row["status"] = "ok"
    with report.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    if announce:
        print(f"{len(done)}/{len(rows)} records complete ({report})")
    return done


def harvest(args):
    root, cache = args.out / args.work, args.out / "records.json"
    if (root / "bagit.txt").exists():
        raise SystemExit(f"{root} is already a bag - move it aside, or use `bag` to re-zip it.")
    built = existing_bags(args.out, CONFIG["name"])
    if built and not root.is_dir():
        print(f"Note: {built[0].name} is already bagged; harvesting afresh into {root}.")
    root.mkdir(parents=True, exist_ok=True)
    rows = load_rows(args, root, cache)
    print("Downloading files...")
    try:
        download_all(rows, root, args.workers)
    finally:
        if not args.limit:
            cache.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    done = write_report(rows, root, args.out / "report.csv")
    orphans = [r for r in rows if r["status"] == "no portfolio attached"]
    if orphans:
        with_link = sum(1 for r in orphans if r.get("856_url"))
        print(f"  {len(orphans)} have no portfolio ({with_link} with an 856 link)")
    if args.limit:
        print("Test run (--limit): not cached and not bagged. `sample` will bag what's here.")
    elif len(done) < len(rows):
        print("Re-run to retry the rest before bagging.")
    return len(done) == len(rows)


# Only bags once every record is on disk.
def harvest_and_bag(args):
    if harvest(args):
        bag(args)


# --- bag --------------------------------------------------------------------


# {date} is when the bag is built, as YYYYMMDDHHMM.
def bag_name(template):
    return template.format(date=time.strftime("%Y%m%d%H%M"))


# Bags already built under this naming scheme, newest first.
def existing_bags(out, template):
    return sorted((p for p in out.glob(template.format(date="*")) if (p / "bagit.txt").exists()), reverse=True)


# Retries after undoing the half-finished move a Windows file lock leaves behind.
def build_bag(root, processes):
    for attempt in range(3):
        try:
            return bagit.make_bag(str(root), checksums=["md5"], processes=processes)
        except PermissionError as exc:
            for tmp in root.glob("tmp*"):
                for moved in tmp.iterdir():
                    moved.rename(root / moved.name)
                tmp.rmdir()
            print(f"  attempt {attempt + 1} failed ({exc}); retrying", flush=True)
            time.sleep(15)
    raise SystemExit("Bagging kept hitting locked files - is something scanning the folder?")


# Zips under a temporary name, renamed only once the archive checks out.
def zip_bag(root, out, name):
    final, partial = Path(f"{root}.zip"), Path(f"{root}.partial.zip")
    print(f"Zipping {final} (several minutes for a payload this size)...", flush=True)
    shutil.make_archive(str(partial.with_suffix("")), "zip", out, name)
    print("  checking the zip...", flush=True)
    with zipfile.ZipFile(partial) as archive:
        bad = archive.testzip()
    if bad:
        raise SystemExit(f"{partial} is corrupt (first bad file: {bad}) - delete it and re-run.")
    partial.replace(final)
    print(f"  wrote {final} ({final.stat().st_size / 1e6:,.0f} MB)")


def bag(args):
    work, root, cache = args.out / args.work, args.out / args.name, args.out / "records.json"
    if (root / "bagit.txt").exists():
        print(f"{root} is already a bag; checking it's complete...")
        bagit.Bag(str(root)).validate(fast=True)
        print("  complete")
    elif root.exists():
        raise SystemExit(f"{root} exists but isn't a bag - move it aside.")
    elif not work.is_dir():
        built = existing_bags(args.out, CONFIG["name"])
        older = f" ({built[0].name} is already bagged - `--name {built[0].name}` to re-zip it)" if built else ""
        raise SystemExit(f"Nothing harvested at {work} - run `harvest` first.{older}")
    elif not cache.exists():
        raise SystemExit(f"No harvested metadata at {cache} - run `harvest` first.")
    else:
        rows = json.loads(cache.read_text(encoding="utf-8"))
        done = write_report(rows, work, args.out / "report.csv", announce=False)
        if not done:
            raise SystemExit("Nothing to bag.")
        for row in rows:
            if row["portfolio_id"] and not record_complete(row, work):
                shutil.rmtree(work / row["mms_id"], ignore_errors=True)
        work.rename(root)
        print(f"Bagging {len(done)} of {len(rows)} records into {root}...")
        print("  valid" if build_bag(root, args.workers).is_valid() else "  INVALID BAG")
    if not args.no_zip:
        zip_bag(root, args.out, root.name)


# --- sample -----------------------------------------------------------------


# `count` of `records` (folder, size), evenly spaced by size, within `budget` bytes.
def spread(records, count, budget):
    records = sorted(records, key=lambda pair: pair[1])

    def pick(limit):
        places = {round(i * (limit - 1) / max(count - 1, 1)) for i in range(count)}
        return [records[pos] for pos in sorted(places)]

    low, high = 1, len(records)
    while low < high:
        middle = (low + high + 1) // 2
        low, high = (middle, high) if sum(size for _, size in pick(middle)) <= budget else (low, middle - 1)
    return pick(low)


def sample(args):
    work = args.out / args.work
    built = existing_bags(args.out, CONFIG["name"])
    if work.is_dir():
        data, name = work, args.name
    elif built:
        data, name = built[0] / "data", built[0].name
    else:
        raise SystemExit(f"Nothing to sample from at {work} - run `harvest` first.")
    complete = [
        (folder, sum(f.stat().st_size for f in folder.iterdir()))
        for folder in sorted(data.iterdir())
        if folder.is_dir() and any(f.suffix.lower() in PAYLOAD and f.stat().st_size for f in folder.iterdir())
    ]
    records = spread(complete, args.records, args.max_mb * 1_000_000)
    if not records:
        raise SystemExit(f"No complete records under {args.max_mb} MB in {data}.")

    root = args.out / "sample" / name
    if root.exists():
        raise SystemExit(f"{root} already exists - delete it and run again.")
    root.parent.mkdir(parents=True, exist_ok=True)
    for folder, _ in records:
        shutil.copytree(folder, root / folder.name)
    print(f"Copied {len(records)} records ({sum(s for _, s in records) / 1e6:.1f} MB) to {root}")
    for folder, size in records:
        print(f"    {folder.name}  {size / 1e6:>6.2f} MB")
    print("  valid" if build_bag(root, args.workers).is_valid() else "  INVALID BAG")
    if not args.no_zip:
        zip_bag(root, root.parent, name)


# --- cli --------------------------------------------------------------------


def main():
    global CONFIG
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=HERE / "config.toml", help="config file")
    parser.add_argument("--out", type=Path, help="working directory")
    parser.add_argument("--name", help="bag folder/zip name")
    parser.add_argument("--workers", type=int, help="concurrent downloads / hashing processes")
    parser.set_defaults(run=harvest_and_bag, sets=None, refresh=False, limit=None, no_zip=False)
    commands = parser.add_subparsers(dest="command")

    harvest_cmd = commands.add_parser("harvest", help="fetch metadata and files")
    harvest_cmd.add_argument("--sets", nargs="+", help="Alma set IDs (default: config.toml)")
    harvest_cmd.add_argument("--refresh", action="store_true", help="re-fetch set members and bibs")
    harvest_cmd.add_argument("--limit", type=int, help="test run: only the first N records, not cached")
    harvest_cmd.set_defaults(run=harvest)

    bag_cmd = commands.add_parser("bag", help="bag and zip what's downloaded")
    bag_cmd.add_argument("--no-zip", action="store_true", help="bag but don't zip")
    bag_cmd.set_defaults(run=bag)

    sample_cmd = commands.add_parser("sample", help="build a small bag of a few records, to send for checking")
    sample_cmd.add_argument("--records", type=int, default=25, help="how many records (default 25)")
    sample_cmd.add_argument("--max-mb", type=int, default=20, help="total payload budget (default 20 MB)")
    sample_cmd.add_argument("--no-zip", action="store_true", help="bag but don't zip")
    sample_cmd.set_defaults(run=sample)

    args = parser.parse_args()
    CONFIG = load_config(args.config)
    args.out = args.out or Path(CONFIG["out"])
    args.name = args.name or bag_name(CONFIG["name"])
    args.work = CONFIG["work"]
    args.workers = args.workers or CONFIG["workers"]
    if getattr(args, "sets", None) is None:
        args.sets = CONFIG["sets"]
    args.run(args)


if __name__ == "__main__":
    main()

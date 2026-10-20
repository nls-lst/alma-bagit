# alma-bagit

Builds a zipped [BagIt](https://datatracker.ietf.org/doc/html/rfc8493) package
(md5 manifests) from Alma sets, one folder per record containing the MARCXML
and the file linked from the record's electronic portfolio.

Written for delivering e-deposit records for central ingest.
It should work for any library on Alma: point it at your own sets and name the bag
after your institution. Cover images are not included.

## Setup

You need [uv](https://docs.astral.sh/uv/), (installs
dependencies on first run), and an Alma API key with read access to
Configuration (sets) and Bibliographic Records & Inventory.

1. Copy `config.toml.example` to `config.toml` and fill in your set IDs, bag
   name and API region.
2. Put the API key in the `ALMA_API_KEY` environment variable, or in a `.env`
   file beside the script containing `ALMA_API_KEY=<key>`. It can't go in
   `config.toml`, which is the file most likely to be shared. Both `config.toml`
   and `.env` are gitignored.

Only harvesting needs the key: `bag`, `sample` and re-zipping run without one.

## Usage

```sh
uv run alma_bag.py                     # harvest the sets in config.toml, then bag them
```

It fetches every record in the configured sets, downloads
the files, and builds the zipped bag. If the file host drops some records it says
so and stops short of bagging; run the same command again and it picks up where
it left off, skipping everything already on disk.

The steps are also available separately:

```sh
uv run alma_bag.py harvest             # fetch only; re-run to retry what failed
uv run alma_bag.py harvest --limit 50  # test run over the first 50 records (not cached)
uv run alma_bag.py bag                 # bag and zip what's on disk
uv run alma_bag.py sample              # small bag of a few records, to send for checking
```

Records are harvested into `output/harvest/`. `bag` renames that directory to
the bag name (e.g. `NLS_BAG_<YYYYMMDDHHMM>`) and bags it, so the date on a bag is when it was built,
not the day harvesting started (a harvest spread over several days still comes
out with one delivery date).

A bag is only built once every record is on disk, in case of a flaky file host.
Metadata is cached in `records.json` and files already on disk are skipped.
`report.csv` lists every record with its status (what's missing and why), title,
`856` link and catalogue dates; filter on `status` for the problems.

Global options: `--config`, `--out`, `--name`, `--workers`.
`harvest` adds `--sets`, `--refresh` and `--limit`; `bag` and `sample` add
`--no-zip`; `sample` adds `--records` and `--max-mb`.

## Output

```
output/
  harvest/                        records land here first
  NLS_BAG_202609011430.zip
  report.csv
  records.json
  NLS_BAG_202609011430/
    bagit.txt, bag-info.txt, manifest-md5.txt, tagmanifest-md5.txt
    data/
      99123456780004341/
        99123456780004341.xml     MARCXML for the bib
        99123456780004341.pdf     file from the portfolio link
  sample/
    NLS_BAG_202609011430.zip      `sample` output: same shape, fewer records
    NLS_BAG_202609011430/
```

## Notes

- Portfolio IDs come from the `AVE $8` field returned by `expand=e_avail` on the
  bulk bib call, so each batch of 100 records costs one API call rather than 100.
  Those synthetic `AV*` availability fields are stripped from the saved MARCXML.
- Every record is expected to have exactly one portfolio. Records with none, with
  more than one, with no link, or whose download fails are recorded in
  `report.csv` with a reason and left out of the bag, so the bag only ever
  contains complete record folders.
- The retry settings assume a file host that is unreliable under sustained load.
  Retries are patient (~7 min per file) and short stalls don't turn
  into failures. If yours throttles instead, drop `--workers`.
- On Windows, bagit's "move every folder into a temp dir" step can hit a file
  lock from a virus scanner, leaving the tree half-moved. `build_bag` undoes
  that and retries. If it happens repeatedly, exclude `output/` from real-time
  scanning while bagging.
- Each zip is written as `<name>.partial.zip` and checked before it gets its
  final name, so an interrupted or corrupt zip never looks finished.
- `sample` builds an ordinary bag under `output/sample/`, named and structured
  exactly like a full one -- nothing in `bag-info.txt` marks it as a sample, so
  what a reviewer checks is what they would later receive. It only copies, so the
  full bag is untouched. Records are chosen for a spread of file sizes within a
  total budget (`--max-mb`, 20 by default) so the zip stays emailable; raise the
  budget if you want bigger files than the few MB that fits at the default.

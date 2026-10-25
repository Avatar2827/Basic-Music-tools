"""
fix_albums.py - adds ALBUM NAME, TRACK NUMBER (e.g. 3/11) and ALBUM COVER to mp3 files.

Iterates through all artist folders (and all subfolders) in the target folder, one folder at a time.
Data is fetched from Apple's public iTunes Search API, no account or key needed.

Rules:
  * Only writes what is MISSING. Artist and title are never changed. --force overwrites.
  * If the file already has an album name, only an album matching that name is used.
  * Original albums are prioritized over compilation albums ("Greatest Hits", "Live", "Best of" ...).
  * Songs by the same artist are preferably grouped on the same album.
  * Also fills in disc number (for double albums) and album artist. Turn off with --skip-albumartist.

Instructions, what you need and how to use:
# Download mutagen first: pip install mutagen

# Dry run, shows what would be changed
# py -3.12 fix_albums.py

# Write changes to the files
# py -3.12 fix_albums.py --apply

# Smaller covers (smaller files)
# py -3.12 fix_albums.py --apply --cover-size 300

# Overwrite existing tags
# py -3.12 fix_albums.py --apply --force

# Specify custom folder
# py -3.12 fix_albums.py --folder "C:\Users\Username\Music\name of artist" --apply
"""
import argparse
import difflib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TPE2, TPOS, TRCK

DEFAULT_FOLDER = Path(r"C:\Users\Username\Music")
HERE = Path(__file__).resolve().parent
CACHE_FILE = HERE / "albumcache.json"
REPORT_FILE = HERE / "albumreport.txt"
SKIP_DIRS = {"albums", "wav"}
KEEP = ("trackName", "artistName", "collectionArtistName", "collectionName", "collectionId",
        "trackNumber", "trackCount", "discNumber", "discCount", "releaseDate", "artworkUrl100")
COMPILATION = re.compile(
    r"greatest hits|best of|the very best|essential|anthology|collection|compilation|karaoke|"
    r"tribute|\blive\b|unplugged|rarities|b-sides|sessions?|instrumentals?|remix(es)?",
    re.I)


def norm(s):
    return re.sub(r"[\W_]+", "", (s or "").lower())


def clean_title(t):
    t = re.sub(r"\s*[\(\[]\s*(feat|ft|with)\.?[^\)\]]*[\)\]]", "", t, flags=re.I)
    t = re.sub(r"\s*[\(\[][^\)\]]*remaster[^\)\]]*[\)\]]", "", t, flags=re.I)
    t = re.sub(r"\s*-\s*(\d{4}\s*)?(remaster(ed)?|remastered version|mono|stereo)\b.*$", "", t,
               flags=re.I)
    return t.strip()


def album_norm(s):
    s = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", s or "")
    s = re.sub(r"\s+-\s+(single|ep)$", "", s, flags=re.I)
    return norm(s)


def album_ratio(a, b):
    na, nb = album_norm(a), album_norm(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def split_artists(s):
    return [a.strip() for a in re.split(r"[;/,&]|\sfeat\.?\s|\sft\.?\s", s, flags=re.I) if a.strip()]


def match_score(r, artist, title):
    t_r, t_q = norm(r.get("trackName", "")), norm(clean_title(title))
    tr = difflib.SequenceMatcher(None, t_r, t_q).ratio() if t_r and t_q else 0.0
    a_r, a_full = norm(r.get("artistName", "")), norm(artist)
    if a_r and a_r in a_full:
        ar = 1.0
    else:
        ar = max([difflib.SequenceMatcher(None, a_r, norm(a)).ratio()
                  for a in split_artists(artist)] or [0.0])
    return tr, ar


def choose(cands, artist, title, existing_album, used):
    scored = []
    for r in cands:
        tr, ar = match_score(r, artist, title)
        if tr >= 0.75 and ar >= 0.6:
            scored.append((0.6 * tr + 0.4 * ar, r))
    if not scored:
        return None, "no match"
    if existing_album:
        same = [(s, r) for s, r in scored
                if album_ratio(r.get("collectionName"), existing_album) >= 0.7]
        if not same:
            return None, f"no album matching the tag '{existing_album}'"
        scored = same
    top = max(s for s, _ in scored)
    close = [r for s, r in scored if top - s <= 0.03]

    def rank(r):
        return (bool(COMPILATION.search(r.get("collectionName") or "")),
                r.get("collectionId") not in used,
                r.get("releaseDate") or "9999")
    return min(close, key=rank), ""


def http_get(url, tries=3):
    for attempt in range(tries):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and attempt < tries - 1:
                print(f"   Apple is limiting requests ({e.code}), waiting 65 s ...")
                time.sleep(65)
                continue
            raise
        except urllib.error.URLError:
            if attempt < tries - 1:
                time.sleep(5)
                continue
            raise


def itunes_search(term, country):
    q = urllib.parse.urlencode({"term": term, "entity": "song", "limit": 15, "country": country})
    data = json.loads(http_get("https://itunes.apple.com/search?" + q))
    return [{k: r.get(k) for k in KEEP} for r in data.get("results", [])]


def load_cache():
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_cache(cache):
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def file_info(path, tags, base):
    artist = str(tags["TPE1"]) if "TPE1" in tags else ""
    title = str(tags["TIT2"]) if "TIT2" in tags else ""
    stem = path.stem
    if (not artist or not title) and " - " in stem:
        a, t = stem.split(" - ", 1)
        artist, title = artist or a, title or t
    if not title:
        title = stem
    if not artist and path.parent != base:
        artist = path.parent.name
    return artist, title


def wanted(tags, args):
    if args.force:
        return ["cover", "album", "track"] + ([] if args.skip_albumartist else ["albumartist"])
    miss = []
    if not tags.getall("APIC"):
        miss.append("cover")
    if "TALB" not in tags:
        miss.append("album")
    if "TRCK" not in tags:
        miss.append("track")
    if "TPE2" not in tags and not args.skip_albumartist:
        miss.append("albumartist")
    return miss


def apply_changes(tags, r, miss, cover):
    done = []
    if "cover" in miss and cover:
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
        done.append("cover")
    if "album" in miss and r.get("collectionName"):
        tags.setall("TALB", [TALB(encoding=3, text=r["collectionName"])])
        done.append("album")
    if "track" in miss and r.get("trackNumber"):
        n, c = r["trackNumber"], r.get("trackCount")
        tags.setall("TRCK", [TRCK(encoding=3, text=f"{n}/{c}" if c else str(n))])
        if (r.get("discCount") or 1) > 1 and r.get("discNumber"):
            tags.setall("TPOS", [TPOS(encoding=3, text=f"{r['discNumber']}/{r['discCount']}")])
        done.append("track")
    if "albumartist" in miss:
        aa = r.get("collectionArtistName") or r.get("artistName") or ""
        if aa and "various" not in aa.lower():
            tags.setall("TPE2", [TPE2(encoding=3, text=aa)])
            done.append("albumartist")
    return done


def collect_files(folder):
    out = []
    for p in sorted(folder.rglob("*.mp3")):
        if any(part.lower() in SKIP_DIRS for part in p.relative_to(folder).parts[:-1]):
            continue
        out.append(p)
    return out


def main():
    ap = argparse.ArgumentParser(description="Add album, track number and cover")
    ap.add_argument("--apply", action="store_true", help="write changes (otherwise dry run)")
    ap.add_argument("--force", action="store_true", help="overwrite existing tags and covers")
    ap.add_argument("--skip-albumartist", action="store_true", help="do not write albumartist")
    ap.add_argument("--folder", default=str(DEFAULT_FOLDER), help="folder with artist folders")
    ap.add_argument("--country", default="US", help="Apple store, e.g. US or SE")
    ap.add_argument("--cover-size", type=int, default=600, help="cover size in pixels")
    ap.add_argument("--delay", type=float, default=3.0, help="seconds between searches")
    args = ap.parse_args()

    base = Path(args.folder)
    if not base.is_dir():
        sys.exit(f"Folder does not exist: {base}")
    files = collect_files(base)
    if not files:
        print("Found no mp3 files in", base)
        return

    cache = load_cache()
    covers = {}
    used = defaultdict(set)
    stats = {"changed": 0, "already complete": 0, "no match": 0, "errors": 0}
    problems = []
    last_dir = None
    print(f"{len(files)} files in {base}  ({'WRITING' if args.apply else 'DRY RUN'})")

    try:
        for i, path in enumerate(files, 1):
            if path.parent != last_dir:
                last_dir = path.parent
                rel = last_dir.relative_to(base) if last_dir != base else Path(".")
                print(f"\n== {rel} ==")
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            miss = wanted(tags, args)
            if not miss:
                stats["already complete"] += 1
                continue

            artist, title = file_info(path, tags, base)
            existing_album = None if args.force else (str(tags["TALB"]) if "TALB" in tags else None)
            akey = norm(split_artists(artist)[0] if artist else "")
            label = f"  [{i}/{len(files)}] {title}"

            ckey = f"{akey}|{norm(clean_title(title))}"
            if ckey not in cache:
                try:
                    term = f"{split_artists(artist)[0] if artist else ''} {clean_title(title)}".strip()
                    cache[ckey] = itunes_search(term, args.country)
                except Exception as e:
                    print(f"{label}\n     ERROR during search: {e}")
                    stats["errors"] += 1
                    continue
                save_cache(cache)
                time.sleep(args.delay)

            r, why = choose(cache[ckey], artist, title, existing_album, used[akey])
            if not r:
                print(f"{label}\n     {why}")
                stats["no match"] += 1
                problems.append(f"{artist} - {title}   [{why}]   ({path})")
                continue
            used[akey].add(r.get("collectionId"))

            cover = None
            if "cover" in miss and r.get("artworkUrl100"):
                ck = (r.get("collectionId"), args.cover_size)
                if ck not in covers:
                    size = args.cover_size
                    url = r["artworkUrl100"].replace("100x100bb", f"{size}x{size}bb")
                    try:
                        covers[ck] = http_get(url)
                    except Exception as e:
                        print(f"     could not fetch cover: {e}")
                        covers[ck] = None
                cover = covers[ck]

            done = apply_changes(tags, r, miss, cover)
            if done and args.apply:
                tags.save(path, v2_version=3)
            num = f"  #{r['trackNumber']}/{r.get('trackCount') or '?'}" if r.get("trackNumber") else ""
            print(f"{label}\n     -> {r['collectionName']}{num}   "
                  f"{'(+' + ', +'.join(done) + ')' if done else '(nothing new to add)'}")
            if done:
                stats["changed"] += 1
    except KeyboardInterrupt:
        print("\nAborted. Search results are saved, next run will continue quickly.")

    save_cache(cache)
    REPORT_FILE.write_text(
        "\n".join([f"Songs that could not get an album ({len(problems)}):", *problems]),
        encoding="utf-8")
    print("\n" + "=" * 60)
    for k, v in stats.items():
        print(f"{k}: {v}")
    print(f"Report: {REPORT_FILE}")
    if not args.apply:
        print("\nDry run, nothing was changed. Add --apply to write the tags.")


if __name__ == "__main__":
    sys.exit(main())

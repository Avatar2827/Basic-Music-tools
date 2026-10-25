"""
tag_fixer.py - adds metadata and album covers to mp3 files.

Expected folder structure:
  C:\\Users\\Username\\Music\\<Artist>\\<song>.mp3

Fetches data from Apple's public iTunes Search API (no keys, no account).
Fills in what is MISSING: title, artist, album, albumartist, genre, year, track number,
disc number, and cover. Files that already have a cover, album, year, and track number 
are skipped completely (only --force changes them).

Instructions, what you need and how to use:
# Download mutagen first: pip install mutagen

# Dry run, changes nothing
# py -3.12 tag_fixer.py

# Write changes
# py -3.12 tag_fixer.py --apply

# Overwrite existing tags
# py -3.12 tag_fixer.py --apply --force

# Do not touch genre
# py -3.12 tag_fixer.py --apply --skip-genre

# Custom cover size
# py -3.12 tag_fixer.py --apply --cover-size 1000

# Specify country code for search
# py -3.12 tag_fixer.py --apply --country SE
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
from pathlib import Path

from mutagen.id3 import (APIC, ID3, ID3NoHeaderError, TALB, TCON, TDRC, TIT2,
                         TPE1, TPE2, TPOS, TRCK)

MUSIC_DIR = Path(r"C:\Users\Username\Music")
HERE = Path(__file__).resolve().parent
CACHE_FILE = HERE / "tagcache_fixer.json"
REPORT_FILE = HERE / "tagreport_fixer.txt"
KEEP = ("trackName", "artistName", "collectionName", "collectionId", "collectionArtistName",
        "primaryGenreName", "releaseDate", "trackNumber", "trackCount", "discNumber",
        "discCount", "artworkUrl100")


def norm(s):
    return re.sub(r"[\W_]+", "", (s or "").lower())


def clean_title(t):
    t = re.sub(r"\s*[\(\[]\s*(feat|ft|with)\.?[^\)\]]*[\)\]]", "", t, flags=re.I)
    t = re.sub(r"\s*[\(\[][^\)\]]*remaster[^\)\]]*[\)\]]", "", t, flags=re.I)
    t = re.sub(r"\s*-\s*(\d{4}\s*)?(remaster(ed)?|remastered version|mono|stereo)\b.*$", "", t,
               flags=re.I)
    return t.strip()


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


def best_match(results, artist, title):
    cands = []
    for r in results:
        tr, ar = match_score(r, artist, title)
        if tr >= 0.75 and ar >= 0.6:
            cands.append((0.6 * tr + 0.4 * ar, r))
    if not cands:
        return None
    top = max(s for s, _ in cands)
    close = [r for s, r in cands if top - s <= 0.03]
    return min(close, key=lambda r: r.get("releaseDate") or "9999")


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


def file_info(path, tags, root):
    artist = str(tags["TPE1"]) if "TPE1" in tags else ""
    title = str(tags["TIT2"]) if "TIT2" in tags else ""

    folder_artist = ""
    try:
        parts = path.relative_to(root).parts
        if len(parts) > 1:
            folder_artist = parts[0]
    except ValueError:
        pass

    stem = path.stem
    if not title:
        if " - " in stem:
            a, t = stem.split(" - ", 1)
            title = t.strip()
            artist = artist or a.strip()
        else:
            title = stem
    title = re.sub(r"^\d{1,3}\s*[-._]\s*", "", title).strip() or title
    artist = artist or folder_artist
    return artist, title


def missing_fields(tags, args):
    if args.force:
        return (["cover", "album", "albumartist", "year", "track", "disc"]
                + ([] if args.skip_genre else ["genre"]))
    miss = []
    if not tags.getall("APIC"):
        miss.append("cover")
    if "TALB" not in tags:
        miss.append("album")
    if "TDRC" not in tags and "TYER" not in tags:
        miss.append("year")
    if "TRCK" not in tags:
        miss.append("track")

    if not miss:
        return []

    if "TPE2" not in tags:
        miss.append("albumartist")
    if "TPOS" not in tags:
        miss.append("disc")
    if "TCON" not in tags and not args.skip_genre:
        miss.append("genre")
    return miss


def apply_changes(tags, r, miss, cover, artist, title):
    done = []
    if "TPE1" not in tags and artist:
        tags.setall("TPE1", [TPE1(encoding=3, text=artist)])
        done.append("artist")
    if "TIT2" not in tags and title:
        tags.setall("TIT2", [TIT2(encoding=3, text=title)])
        done.append("title")

    if "cover" in miss and cover:
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
        done.append("cover")
    if "album" in miss and r.get("collectionName"):
        tags.setall("TALB", [TALB(encoding=3, text=r["collectionName"])])
        done.append("album")
    if "albumartist" in miss:
        aa = r.get("collectionArtistName") or r.get("artistName")
        if aa:
            tags.setall("TPE2", [TPE2(encoding=3, text=aa)])
            done.append("albumartist")
    if "genre" in miss and r.get("primaryGenreName"):
        tags.setall("TCON", [TCON(encoding=3, text=r["primaryGenreName"])])
        done.append("genre")
    year = (r.get("releaseDate") or "")[:4]
    if "year" in miss and year.isdigit():
        tags.setall("TDRC", [TDRC(encoding=3, text=year)])
        done.append("year")
    if "track" in miss and r.get("trackNumber"):
        n, c = r["trackNumber"], r.get("trackCount")
        tags.setall("TRCK", [TRCK(encoding=3, text=f"{n}/{c}" if c else str(n))])
        done.append("track")
    if "disc" in miss and r.get("discNumber"):
        n, c = r["discNumber"], r.get("discCount")
        tags.setall("TPOS", [TPOS(encoding=3, text=f"{n}/{c}" if c else str(n))])
        done.append("disc")
    return done


def find_match(artist, title, country):
    first = split_artists(artist)[0] if artist else ""
    term = f"{first} {clean_title(title)}".strip()
    m = best_match(itunes_search(term, country), artist, title)
    if m:
        return m
    return best_match(itunes_search(clean_title(title), country), artist, title)


def collect_files(folder):
    return sorted(folder.rglob("*.mp3"))


def main():
    ap = argparse.ArgumentParser(description="Fill in metadata and covers in mp3 files")
    ap.add_argument("--apply", action="store_true", help="write changes (otherwise dry run)")
    ap.add_argument("--force", action="store_true", help="overwrite existing tags and covers")
    ap.add_argument("--skip-genre", action="store_true", help="do not touch genre tag")
    ap.add_argument("--folder", default=str(MUSIC_DIR), help="root folder with artist folders")
    ap.add_argument("--country", default="US", help="Apple store, e.g. US or SE")
    ap.add_argument("--cover-size", type=int, default=600, help="cover size in pixels")
    ap.add_argument("--delay", type=float, default=3.0, help="seconds between searches")
    args = ap.parse_args()

    root = Path(args.folder)
    files = collect_files(root)
    if not files:
        print("Found no mp3 files in", root)
        return

    cache = load_cache()
    covers = {}
    stats = {"changed": 0, "already complete": 0, "no match": 0, "errors": 0}
    not_found = []
    print(f"{len(files)} files in {root}  ({'WRITING' if args.apply else 'DRY RUN'})\n")

    try:
        for i, path in enumerate(files, 1):
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            except Exception as e:
                print(f"[{i}/{len(files)}] {path.name}\n   ERROR reading file: {e}")
                stats["errors"] += 1
                continue

            miss = missing_fields(tags, args)
            artist, title = file_info(path, tags, root)
            label = f"[{i}/{len(files)}] {artist or '?'} - {title}"

            basic_missing = ("TPE1" not in tags and artist) or ("TIT2" not in tags and title)
            if not miss and not basic_missing:
                stats["already complete"] += 1
                continue

            if not miss:
                done = apply_changes(tags, {}, [], None, artist, title)
                if done and args.apply:
                    tags.save(path, v2_version=3)
                print(f"{label}\n   + {', '.join(done)} (from folder/filename)")
                stats["changed"] += 1
                continue

            key = f"{norm(artist)}|{norm(clean_title(title))}"
            if key not in cache:
                try:
                    cache[key] = find_match(artist, title, args.country)
                except Exception as e:
                    print(f"{label}\n   ERROR during search: {e}")
                    stats["errors"] += 1
                    continue
                save_cache(cache)
                time.sleep(args.delay)
            r = cache[key]

            if not r:
                done = apply_changes(tags, {}, [], None, artist, title)
                if done and args.apply:
                    tags.save(path, v2_version=3)
                print(f"{label}\n   no match" + (f" (wrote anyway: {', '.join(done)})" if done else ""))
                stats["no match"] += 1
                not_found.append(f"{artist} - {title}   ({path})")
                continue

            cover = None
            if "cover" in miss and r.get("artworkUrl100"):
                ck = (r["collectionId"], args.cover_size)
                if ck not in covers:
                    size = args.cover_size
                    url = r["artworkUrl100"].replace("100x100bb", f"{size}x{size}bb")
                    try:
                        covers[ck] = http_get(url)
                    except Exception as e:
                        print(f"   could not fetch cover: {e}")
                        covers[ck] = None
                cover = covers[ck]

            done = apply_changes(tags, r, miss, cover, artist, title)
            if done and args.apply:
                tags.save(path, v2_version=3)
            print(f"{label}\n   {'+ ' if done else ''}{', '.join(done) or 'nothing to add'}"
                  f"   <- {r['artistName']} - {r['collectionName']}")
            if done:
                stats["changed"] += 1
    except KeyboardInterrupt:
        print("\nAborted. Search results are saved, next run will continue quickly.")

    save_cache(cache)
    lines = [f"Songs not found ({len(not_found)}):", *not_found]
    REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")
    print("\n" + "=" * 60)
    for k, v in stats.items():
        print(f"{k}: {v}")
    print(f"Report: {REPORT_FILE}")
    if not args.apply:
        print("\nDry run, nothing was changed. Add --apply to write the tags.")


if __name__ == "__main__":
    sys.exit(main())
import argparse
import json
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path
from mutagen import File as MutagenFile

# Instructions, what you need and how to use:'

# Download mutagen first, you can use this command: pip install mutagen

# Test run, doesn't move any files unless you add --apply from the get go
# py -3.12 sort_artists.py

# Move the files
# py -3.12 sort_artists.py --apply

# Undo the last move
# py -3.12 sort_artists.py --undo

# Specify custom folder and move the files
# py -3.12 sort_artists.py --folder "C:\Users\Username\Music" --apply

# Artists with only 1 song are moved to "Miscellaneous" (or the folder specified by --other)
# py -3.12 sort_artists.py --min 2 --apply

# Show every file in the dry run
# py -3.12 sort_artists.py --verbose

DEFAULT_FOLDER: Path = Path(r"C:\Users\Username\Music")
EXTS: set = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav"}
IGNORE_DIRS: set = {"albums", "wav"}
LOG_FILE: Path = Path(__file__).resolve().with_name("sort_log.json")
RESERVED: set = {"con", "prn", "aux", "nul", *{f"com{i}" for i in range(1, 10)},
                 *{f"lpt{i}" for i in range(1, 10)}}

# Artist names that contain commas and therefore should not be split. Add more if needed.
NO_SPLIT: list = ["tyler, the creator", "earth, wind & fire", "crosby, stills & nash",
                  "emerson, lake & palmer"]


# ---------------------------------------------------------------- helper functions
def key(name: str) -> str:
    k = re.sub(r"[\W_]+", "", name.casefold())
    return k or name.casefold()


def safe_name(name: str) -> str:
    n = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(". ")
    if not n:
        n = "Unknown artist"
    if n.lower() in RESERVED:
        n += "_"
    return n


def primary(s: str) -> str:
    s = s.strip()
    low = s.lower()
    for x in NO_SPLIT:
        if low.startswith(x):
            return s[:len(x)]
    return re.split(r"\s*;\s*|\s+/\s+|,\s+|\x00", s)[0].strip()


def artist_of(path: Path) -> str:
    audio = None
    try:
        audio = MutagenFile(path, easy=True)
    except Exception:
        pass
    if audio is not None and getattr(audio, "tags", None):
        for tag in ("albumartist", "artist"):
            vals = audio.tags.get(tag) or []
            for v in vals:
                name = primary(str(v))
                if name and name.lower() != "various artists":
                    return name
    stem = path.stem
    if " - " in stem:
        return primary(stem.split(" - ", 1)[0])
    return ""


def load_log() -> list:
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def save_log(runs: list) -> None:
    LOG_FILE.write_text(json.dumps(runs, ensure_ascii=False, indent=1), encoding="utf-8")


def undo() -> None:
    runs = load_log()
    if not runs:
        print("There is nothing to undo.")
        return
    run = runs.pop()
    back = 0
    for m in reversed(run["moves"]):
        src, dst = Path(m["to"]), Path(m["from"])
        if src.exists() and not dst.exists():
            shutil.move(str(src), str(dst))
            back += 1
    for d in run.get("created", []):
        try:
            Path(d).rmdir()  # only removed if empty
        except OSError:
            pass
    save_log(runs)
    print(f"Moved back {back} files and removed empty folders that were created.")


# ---------------------------------------------------------------- main program
def main() -> None:
    ap = argparse.ArgumentParser(description="Organize songs into artist folders")
    ap.add_argument("--folder", default=str(DEFAULT_FOLDER), help="the folder with the songs")
    ap.add_argument("--apply", action="store_true", help="move the files (otherwise dry run)")
    ap.add_argument("--undo", action="store_true", help="undo the last move")
    ap.add_argument("--min", type=int, default=1,
                    help="minimum number of songs for a NEW artist folder, others go to --other")
    ap.add_argument("--other", default="Miscellaneous", help="folder for artists with too few songs")
    ap.add_argument("--verbose", action="store_true", help="show every file")
    args = ap.parse_args()

    if args.undo:
        undo()
        return

    folder = Path(args.folder)
    if not folder.is_dir():
        sys.exit(f"Folder does not exist: {folder}")

    # artist folders you already have (and possibly created yourself)
    existing = {}
    for d in folder.iterdir():
        if d.is_dir() and d.name.lower() not in IGNORE_DIRS:
            existing.setdefault(key(d.name), d.name)

    files = [p for p in sorted(folder.iterdir()) if p.is_file() and p.suffix.lower() in EXTS]
    if not files:
        print("No songs directly in", folder, "- nothing to sort.")
        return

    groups, spelling, no_artist = defaultdict(list), defaultdict(Counter), []
    for p in files:
        a = artist_of(p)
        if not a:
            no_artist.append(p)
            continue
        k = key(a)
        groups[k].append(p)
        spelling[k][a] += 1

    plan = []  # (folder name, status, [files])
    other = []
    for k, ps in groups.items():
        if k in existing:
            plan.append((existing[k], "existing folder", ps))
        elif len(ps) >= args.min:
            plan.append((safe_name(spelling[k].most_common(1)[0][0]), "new folder", ps))
        else:
            other.extend(ps)
    if other:
        status = "existing folder" if key(args.other) in existing else "new folder"
        name = existing.get(key(args.other), safe_name(args.other))
        plan.append((name, status, other))
    plan.sort(key=lambda t: t[0].lower())

    print(f"{len(files)} songs directly in {folder}")
    print(f"{len(existing)} existing folders left untouched\n")
    for name, status, ps in plan:
        print(f"{name:<40} {status:<15} {len(ps):>3} files")
        if args.verbose:
            for p in ps:
                print(f"     {p.name}")
    if no_artist:
        print(f"\n{len(no_artist)} files lack an artist in both tags and filename, they remain in place:")
        for p in no_artist:
            print("   ", p.name)

    new_dirs = sum(1 for _, s, _ in plan if s == "new folder")
    print(f"\n{new_dirs} new folders, {len(plan) - new_dirs} existing folders used.")

    if not args.apply:
        print("\nDry run, nothing was moved. Add --apply to move the files.")
        return

    moves, created, skipped = [], [], []
    for name, status, ps in plan:
        target = folder / name
        if not target.exists():
            target.mkdir()
            created.append(str(target))
        for p in ps:
            dest = target / p.name
            if dest.exists():
                skipped.append(p.name)
                continue
            shutil.move(str(p), str(dest))
            moves.append({"from": str(p), "to": str(dest)})

    runs = load_log()
    runs.append({"moves": moves, "created": created})
    save_log(runs)
    print(f"\nDone. Moved {len(moves)} files.")
    if skipped:
        print(f"{len(skipped)} files were skipped because the same filename already existed in the destination folder:")
        for n in skipped:
            print("   ", n)
    print("Undo with: py -3.12 sort_artists.py --undo")


if __name__ == "__main__":
    main()
import argparse
import sys
import wave
from pathlib import Path

import miniaudio

# Instructions, what you need and how to use:

# Download miniaudio first, you can use this command: pip install miniaudio

# Convert all files in the default folder
# py -3.12 mp3_to_wav.py

# Overwrite existing WAV files if they already exist
# py -3.12 mp3_to_wav.py --overwrite

# Convert only specific artist folders
# py -3.12 mp3_to_wav.py --artists "Artist 1" "Artist 2"

# Specify custom input and output folders manually
# py -3.12 mp3_to_wav.py --input "D:\Music" --output "D:\WAV_Export" # You can skip this step if you've fixed default folders as you can see below

# Combine multiple arguments (example: specific artists and force overwrite)
# py -3.12 mp3_to_wav.py --artists "Artist 1" --overwrite

# INPUT_DIR: Path = Path(r"C:\Users\Username\Music")                    # Customize this path to your default folder containing mp3 files
# OUTPUT_DIR: Path = Path(r"C:\Users\Username\Music\wavconverted")      # Customize this path for a default output folder of the converted wav files


def convert(src: Path, dst: Path) -> None:
    info: miniaudio.FileInfo = miniaudio.get_file_info(str(src))
    
    sound: miniaudio.DecodedSoundFile = miniaudio.decode_file(
        str(src),
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=info.nchannels,
        sample_rate=info.sample_rate,
    )
    
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path = dst.with_suffix(".tmp")
    
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(sound.nchannels)
        w.setsampwidth(2)
        w.setframerate(sound.sample_rate)
        w.writeframes(sound.samples.tobytes())
        
    tmp.replace(dst)


def main() -> int:
    ap: argparse.ArgumentParser = argparse.ArgumentParser(description="Convert mp3 to wav")
    ap.add_argument("--input", default=str(INPUT_DIR), help="folder with mp3 files")
    ap.add_argument("--output", default=str(OUTPUT_DIR), help="folder for wav files")
    ap.add_argument("--artists", nargs="*", help="Specific artist folders to convert (separate with spaces)")
    ap.add_argument("--overwrite", action="store_true", help="overwrite existing wav files")
    args: argparse.Namespace = ap.parse_args()

    src_root: Path = Path(args.input)
    dst_root: Path = Path(args.output)
    
    files: list[Path] = []
    
    if args.artists:
        for artist in args.artists:
            artist_path: Path = src_root / artist
            if artist_path.exists():
                files.extend(sorted(artist_path.rglob("*.mp3")))
            else:
                print(f"Warning: Could not find folder {artist_path}")
    else:
        files = sorted(src_root.rglob("*.mp3"))

    if not files:
        print("Found no mp3 files to convert.")
        return 0

    print(f"{len(files)} files. Saving in {dst_root}\n")
    done: int = 0
    skipped: int = 0
    failed: int = 0
    
    try:
        for i, src in enumerate(files, 1):
            dst: Path = dst_root / src.relative_to(src_root).with_suffix(".wav")
            if dst.exists() and not args.overwrite:
                skipped += 1
                continue
            try:
                convert(src, dst)
                done += 1
                print(f"[{i}/{len(files)}] {src.name}")
            except Exception as e:
                failed += 1
                print(f"[{i}/{len(files)}] ERROR {src.name}: {e}")
    except KeyboardInterrupt:
        print("\nAborted. Run again to continue where it left off.")

    print(f"\nCompleted: {done}   Skipped: {skipped}   Errors: {failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

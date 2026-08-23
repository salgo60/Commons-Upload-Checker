#!/usr/bin/env python3
"""
Skapar ett Apple Photos-album med alla bilder från ett datumintervall.
Albumet är sedan redo för manuell granskning innan photos_checker.py körs.
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime


def query_uuids(from_date: str, to_date: str) -> list[str]:
    """Hämtar UUID för alla bilder i datumintervallet via osxphotos."""
    cmd = ["osxphotos", "query", "--json",
           "--from-date", from_date, "--to-date", to_date]
    print(f"Hämtar bilder {from_date} → {to_date} ...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            print(f"osxphotos-fel: {result.stderr[:300]}", file=sys.stderr)
            return []
        assets = json.loads(result.stdout)
        uuids = [a["uuid"] for a in assets if a.get("uuid")]
        print(f"  → {len(uuids)} bilder hittade.")
        return uuids
    except Exception as e:
        print(f"Fel: {e}", file=sys.stderr)
        return []


def create_photos_album(album_name: str, uuids: list[str]) -> None:
    """Skapar ett album i Photos och lägger till bilder via photoscript."""
    try:
        import photoscript
    except ImportError:
        print("photoscript saknas. Installera: pip install photoscript", file=sys.stderr)
        sys.exit(1)

    lib = photoscript.PhotosLibrary()

    # Skapa eller hämta befintligt album (retry vid timeout)
    album = None
    for attempt in range(3):
        try:
            existing = lib.album(album_name)
            if existing:
                print(f"Album '{album_name}' finns redan. Lägger till ...")
                album = existing
            else:
                album = lib.create_album(album_name)
                print(f"Album '{album_name}' skapat.")
            break
        except Exception as e:
            print(f"  Försök {attempt+1}/3 misslyckades: {e}")
            import time; time.sleep(5)

    if album is None:
        print("Kunde inte skapa/hämta album. Försök igen när Photos är redo.", file=sys.stderr)
        sys.exit(1)

    import time
    print(f"Lägger till {len(uuids)} bilder i albumet ...")
    batch_size = 25  # Liten batch för att undvika AppleEvent timeout
    added = 0
    errors = 0
    for i in range(0, len(uuids), batch_size):
        batch = uuids[i : i + batch_size]
        photos = []
        for uuid in batch:
            try:
                photos.append(photoscript.Photo(uuid))
            except Exception:
                errors += 1
        if photos:
            for attempt in range(3):
                try:
                    album.add(photos)
                    added += len(photos)
                    break
                except Exception as e:
                    if attempt < 2:
                        time.sleep(3)
                    else:
                        errors += len(photos)
        print(f"  {added}/{len(uuids)} ...", end="\r", flush=True)
        time.sleep(0.5)  # Andrum för Photos mellan batchar

    print(f"\nKlart! {added} bilder tillagda, {errors} misslyckades.")
    print(f"\nÖppna Photos → Album → '{album_name}'")
    print(f"Gå igenom manuellt, ta bort bilder du inte vill ladda upp.")
    print(f"\nSedan kör du:")
    print(f'  python photos_checker.py --from-date {args.from_date} --to-date {args.to_date} --album "{album_name}"')


def main() -> None:
    global args
    parser = argparse.ArgumentParser(
        description="Skapar ett Photos-album med bilder för manuell granskning."
    )
    parser.add_argument("--from-date", required=True, help="Startdatum YYYY-MM-DD")
    parser.add_argument("--to-date",   required=True, help="Slutdatum YYYY-MM-DD")
    parser.add_argument("--album",
                        help="Albumnamn (standard: 'Commons-Kandidater YYYY')")
    args = parser.parse_args()

    year = args.from_date[:4]
    album_name = args.album or f"Commons-Kandidater {year}"

    # osxphotos --to-date är exklusivt ("before DATE"), lägg till 1 dag
    from datetime import timedelta
    to_date_inclusive = (datetime.strptime(args.to_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    uuids = query_uuids(args.from_date, to_date_inclusive)
    if not uuids:
        print("Inga bilder hittades.")
        sys.exit(0)

    create_photos_album(album_name, uuids)


if __name__ == "__main__":
    main()

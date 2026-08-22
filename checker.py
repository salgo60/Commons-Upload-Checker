#!/usr/bin/env python3
"""
Commons Upload Checker v0.1
Jämför lokala bilder med Wikimedia Commons via EXIF-data (GPS-koordinater + datum).
"""

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS
from tqdm import tqdm
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    _HEIF_SUPPORT = True
except ImportError:
    _HEIF_SUPPORT = False


COMMONS_API = "https://commons.wikimedia.org/w/api.php"
RADIUS_M = 100        # geosearch-radie i meter
DATE_TOLERANCE = 1    # dagars tolerans vid datumsökning
API_DELAY = 10.0      # sekunder mellan API-anrop (snäll mot Wikimedia)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".tif", ".tiff", ".png", ".heic", ".heif"}


def get_exif(path: Path) -> dict:
    """Returnerar råa EXIF-taggar från en bildfil (inkl. HEIC)."""
    try:
        with Image.open(path) as img:
            exif_obj = img.getexif()
            if not exif_obj:
                return {}
            result = {TAGS.get(k, k): v for k, v in exif_obj.items()}
            # Hämta GPS IFD korrekt (fungerar för både JPEG och HEIC)
            gps_ifd = exif_obj.get_ifd(0x8825)
            if gps_ifd:
                result["GPSInfo"] = gps_ifd
            return result
    except Exception:
        return {}


def parse_gps(exif: dict) -> tuple[float, float] | None:
    """Returnerar (lat, lon) eller None om GPS saknas."""
    gps_info = exif.get("GPSInfo")
    if not gps_info or not hasattr(gps_info, "items"):
        return None
    gps = {GPSTAGS.get(k, k): v for k, v in gps_info.items()}

    def to_decimal(vals, ref):
        d, m, s = vals
        dec = float(d) + float(m) / 60 + float(s) / 3600
        if ref in ("S", "W"):
            dec = -dec
        return dec

    try:
        lat = to_decimal(gps["GPSLatitude"], gps.get("GPSLatitudeRef", "N"))
        lon = to_decimal(gps["GPSLongitude"], gps.get("GPSLongitudeRef", "E"))
        return lat, lon
    except (KeyError, TypeError, ZeroDivisionError):
        return None


def parse_date(exif: dict) -> datetime | None:
    """Returnerar datetime från DateTimeOriginal eller DateTime."""
    raw = exif.get("DateTimeOriginal") or exif.get("DateTime")
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def fetch_category_files(category: str, depth: int = 4) -> set[str]:
    """
    Hämtar alla filnamn (File:...) från en Commons-kategori rekursivt.
    Returnerar en set med titlar, t.ex. {'File:Foo.jpg', ...}.
    """
    titles: set[str] = set()
    categories_to_visit = {category}
    visited_cats: set[str] = set()

    print(f"Hämtar filindex för kategorin '{category}' (djup {depth})...")

    for _ in range(depth + 1):
        if not categories_to_visit:
            break
        next_level: set[str] = set()
        for cat in categories_to_visit:
            if cat in visited_cats:
                continue
            visited_cats.add(cat)
            cmcontinue = None
            while True:
                params: dict = {
                    "action": "query",
                    "list": "categorymembers",
                    "cmtitle": f"Category:{cat}",
                    "cmlimit": 500,
                    "cmtype": "file|subcat",
                    "format": "json",
                }
                if cmcontinue:
                    params["cmcontinue"] = cmcontinue
                time.sleep(1)
                try:
                    r = requests.get(COMMONS_API, params=params, timeout=15,
                                     headers={"User-Agent": "CommonsUploadChecker/0.1 (salgo60@msn.com)"})
                    r.raise_for_status()
                    data = r.json()
                except Exception as e:
                    print(f"  [fel vid kategori '{cat}'] {e}", file=sys.stderr)
                    break
                for m in data.get("query", {}).get("categorymembers", []):
                    if m["ns"] == 6:       # File
                        titles.add(m["title"])
                    elif m["ns"] == 14:    # Category
                        subcat = m["title"].removeprefix("Category:")
                        next_level.add(subcat)
                if "continue" in data:
                    cmcontinue = data["continue"].get("cmcontinue")
                else:
                    break
        categories_to_visit = next_level - visited_cats

    print(f"  → {len(titles)} filer i kategorin.\n")
    return titles


def load_or_build_cache(category: str, cache_path: Path, refresh: bool = False) -> set[str]:
    """Läser cache från disk, eller bygger den om den saknas/refresh=True."""
    if not refresh and cache_path.exists():
        print(f"Laddar kategori-cache från {cache_path} ...")
        with cache_path.open(encoding="utf-8") as f:
            data = json.load(f)
        titles = set(data["titles"])
        print(f"  → {len(titles)} filer (cachad {data['created_at']}).\n")
        return titles
    titles = fetch_category_files(category)
    with cache_path.open("w", encoding="utf-8") as f:
        json.dump({"category": category, "created_at": datetime.now().isoformat(),
                   "titles": sorted(titles)}, f, ensure_ascii=False, indent=2)
    print(f"Cache sparad i {cache_path}\n")
    return titles


def geosearch_commons(lat: float, lon: float, radius: int = RADIUS_M) -> list[dict]:
    """Söker Commons-filer nära en koordinat."""
    params = {
        "action": "query",
        "list": "geosearch",
        "gscoord": f"{lat}|{lon}",
        "gsradius": radius,
        "gsnamespace": 6,  # File namespace
        "gslimit": 50,
        "format": "json",
    }
    try:
        time.sleep(API_DELAY)
        r = requests.get(COMMONS_API, params=params, timeout=10,
                         headers={"User-Agent": "CommonsUploadChecker/0.1 (salgo60@msn.com)"})
        r.raise_for_status()
        return r.json().get("query", {}).get("geosearch", [])
    except Exception as e:
        print(f"  [API-fel] {e}", file=sys.stderr)
        return []


def get_file_date(title: str) -> datetime | None:
    """Hämtar DateTimeOriginal från EXIF för en Commons-fil."""
    params = {
        "action": "query",
        "titles": title,
        "prop": "imageinfo",
        "iiprop": "metadata",
        "format": "json",
    }
    try:
        r = requests.get(COMMONS_API, params=params, timeout=10,
                         headers={"User-Agent": "CommonsUploadChecker/0.1 (salgo60@msn.com)"})
        r.raise_for_status()
        pages = r.json().get("query", {}).get("pages", {})
        for page in pages.values():
            metadata = (page.get("imageinfo") or [{}])[0].get("metadata") or []
            for m in metadata:
                if m.get("name") in ("DateTimeOriginal", "DateTime"):
                    try:
                        return datetime.strptime(m["value"], "%Y:%m:%d %H:%M:%S")
                    except (ValueError, KeyError):
                        pass
    except Exception:
        pass
    return None


def check_file(path: Path, category_titles: set[str] | None = None) -> dict:
    """Kör en komplett kontroll för en lokal fil."""
    result = {
        "file": path.name,
        "lat": "",
        "lon": "",
        "local_date": "",
        "status": "okänd",
        "commons_match": "",
        "commons_url": "",
    }

    exif = get_exif(path)
    if not exif:
        result["status"] = "ingen EXIF"
        return result

    coords = parse_gps(exif)
    local_date = parse_date(exif)

    if local_date:
        result["local_date"] = local_date.strftime("%Y-%m-%d %H:%M:%S")
    if coords:
        result["lat"], result["lon"] = f"{coords[0]:.6f}", f"{coords[1]:.6f}"

    if not coords:
        result["status"] = "saknar GPS"
        return result

    candidates = geosearch_commons(coords[0], coords[1])
    # Filtrera kandidater mot kategori-cache om den finns
    if category_titles is not None:
        candidates = [c for c in candidates if c["title"] in category_titles]
    if not candidates:
        result["status"] = "ej funnen"
        return result

    # Jämför datum om vi har det
    for c in candidates:
        title = c["title"]
        if local_date:
            commons_date = get_file_date(title)
            if commons_date:
                diff = abs((commons_date - local_date).days)
                if diff <= DATE_TOLERANCE:
                    result["status"] = "MATCH"
                    result["commons_match"] = title
                    result["commons_url"] = (
                        "https://commons.wikimedia.org/wiki/" + title.replace(" ", "_")
                    )
                    return result
        else:
            # Utan datum – rapportera första träffen som möjlig match
            result["status"] = "möjlig match (inget datum)"
            result["commons_match"] = title
            result["commons_url"] = (
                "https://commons.wikimedia.org/wiki/" + title.replace(" ", "_")
            )
            return result

    result["status"] = "ej funnen (datum skiljer)"
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Kontrollera om lokala bilder finns uppladdade på Wikimedia Commons."
    )
    parser.add_argument("folder", help="Mapp med bilder att kontrollera")
    parser.add_argument("-o", "--output", help="Spara resultat till CSV-fil")
    parser.add_argument("-r", "--radius", type=int, default=RADIUS_M,
                        help=f"Geosearch-radie i meter (standard: {RADIUS_M})")
    parser.add_argument("-d", "--delay", type=float, default=API_DELAY,
                        help=f"Sekunder mellan API-anrop (standard: {API_DELAY})")
    parser.add_argument("-c", "--category", default="Stockholm_Archipelago_Trail",
                        help="Commons-kategori att begränsa sökningen till")
    parser.add_argument("--no-category", action="store_true",
                        help="Sök i hela Commons (ignorera kategori-filter)")
    parser.add_argument("--refresh-cache", action="store_true",
                        help="Uppdatera lokal kategori-cache från Commons")
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"Fel: '{folder}' är inte en giltig mapp.", file=sys.stderr)
        sys.exit(1)

    images = [p for p in folder.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS]
    if not images:
        all_files = list(folder.iterdir())
        if all_files:
            exts = sorted({p.suffix for p in all_files})
            print(f"Inga bilder hittades. Filer i mappen har ändelserna: {exts}")
            print(f"Stödda format: {sorted(IMAGE_EXTENSIONS)}")
        else:
            print("Inga filer hittades i mappen.")
        sys.exit(0)

    if not _HEIF_SUPPORT and any(p.suffix.lower() in {".heic", ".heif"} for p in images):
        print("OBS: pillow-heif saknas – HEIC-filer hoppas över. Installera: pip install pillow-heif", file=sys.stderr)

    eta_min = len(images) * args.delay / 60
    global API_DELAY
    API_DELAY = args.delay

    # Ladda eller bygg kategori-cache
    category_titles: set[str] | None = None
    if not args.no_category:
        cache_file = Path(f"cache_{args.category}.json")
        category_titles = load_or_build_cache(args.category, cache_file, args.refresh_cache)
        print(f"Filtrerar mot {len(category_titles)} filer i '{args.category}'")

    print(f"Kontrollerar {len(images)} bild(er) mot Wikimedia Commons...")
    print(f"Delay: {args.delay}s per bild – beräknad tid: ~{eta_min:.0f} minuter\n")

    results = []
    with tqdm(sorted(images), unit="bild", dynamic_ncols=True) as bar:
        for img in bar:
            bar.set_description(img.name[:30])
            row = check_file(img, category_titles)
            results.append(row)
            bar.set_postfix(status=row["status"])

    # Sammanfattning
    print("\n--- Sammanfattning ---")
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    for status, count in sorted(counts.items()):
        print(f"  {status}: {count}")

    # CSV-export
    if args.output:
        out = Path(args.output)
        with out.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=results[0].keys())
            writer.writeheader()
            writer.writerows(results)
        print(f"\nResultat sparat i {out}")


if __name__ == "__main__":
    main()

# Commons Upload Checker

Ett verktyg för att jämföra lokala filer med [Wikimedia Commons](https://commons.wikimedia.org/).

## Syfte

Commons Upload Checker hjälper dig att:

- Kontrollera vilka lokala filer som redan finns uppladdade på Wikimedia Commons
- Identifiera duplicat-uppladdningar innan de sker
- Hålla koll på uppladdningsstatus för din mediesamling

## Installation

```bash
pip install -r requirements.txt
```

## Användning

```bash
# Kontrollera alla bilder i en mapp
python checker.py /sökväg/till/bilder/

# Spara resultatet till CSV
python checker.py /sökväg/till/bilder/ -o resultat.csv

# Utöka sökadie (meter, standard 100 m)
python checker.py /sökväg/till/bilder/ -r 500
```

### Hur det fungerar

1. Skriptet läser **EXIF-data** (GPS-koordinater + datum) från varje bild i mappen.
2. Det söker på Commons via [geosearch-API](https://www.mediawiki.org/wiki/API:Geosearch) efter filer inom `--radius` meter.
3. Om en Commons-fil har samma datum (±1 dag) som den lokala filen → **MATCH**.
4. Resultatet skrivs till terminalen och valfritt till en CSV-fil.

### Möjliga statusar

| Status | Betydelse |
|---|---|
| MATCH | Fil troligtvis redan uppladdad |
| möjlig match (inget datum) | Nära koordinat men kan inte verifiera datum |
| ej funnen | Ingen fil på Commons inom radien |
| ej funnen (datum skiljer) | Koordinatmatch men datum stämmer inte |
| saknar GPS | EXIF finns men inga koordinater |
| ingen EXIF | Ingen EXIF-data i filen |

## Krav

- Python 3.10+
- Bilder med GPS-koordinater i EXIF (JPG/TIFF)

## Bidra

Bidrag välkomnas! Öppna gärna en [issue](https://github.com/salgo60/Commons-Upload-Checker/issues) eller skicka en pull request.

## Licens

Se [LICENSE](LICENSE) för detaljer.

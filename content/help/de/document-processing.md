# Dokumentverarbeitung

## Übersicht

Das Document Structure Workbench verarbeitet Dokumente in mehreren Schritten, um strukturierte Inhalte wie Tabellen zu extrahieren und zu validieren.

## Verarbeitungsschritte

1. **Dokument empfangen** — Das PDF oder Bild wird hochgeladen und validiert
2. **Seiten vorbereitet** — Jede Seite wird als Bild für die Analyse gerendert
3. **Seiteninhalte erkannt** — Layout-Erkennung findet Text, Tabellen, Abbildungen und andere Bereiche
4. **Tabellen lokalisiert** — Tabellenbereiche werden ausgeschnitten und für die Strukturerkennung vorbereitet
5. **Strukturierte Ergebnisse extrahiert** — Tabellenstruktur (Zeilen, Spalten, Kopfzeilen) wird erkannt
6. **Ergebnisse überprüft** — Extraktionen werden mit Referenzdaten verglichen oder von Menschen überprüft

## Was kann schiefgehen?

- **Erkennungsfehler**: Eine Tabelle könnte übersehen oder mit benachbartem Inhalt verschmolzen werden
- **Ausschnittfehler**: Der ausgeschnittene Bereich könnte zu viel oder zu wenig Kontext enthalten
- **Strukturfehler**: Zeilen, Spalten oder Kopfzeilen könnten falsch identifiziert werden
- **Textfehler**: Zelleninhalte könnten fehlen oder falsch sein

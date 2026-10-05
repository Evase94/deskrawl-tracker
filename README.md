# Deskrawl Tracker

Begleit-Tool für **Deskrawl**: zeigt EXP/h, Gold/h, Runs, Tode, Drops und Stage-Vergleiche live an und
bewertet Items per Tastendruck – „anlegen oder nicht?“ mit Blick auf Schaden, Überleben und Ertrag.

Der Tracker liest nur zwei Dinge:
- die **Log-Datei** des Spiels (`Game.log`) und
- das **Spielbild** (Texterkennung, wie ein Screenshot).

Er liest **keinen Spielspeicher**, verändert keine Spieldateien und schickt nichts ins Internet.

## Was er kann

| Tab | Inhalt |
|---|---|
| Übersicht | EXP/h, Gold/h (inkl. verkaufter Items), Runs/h, DPS pro Run, Zeit bis Level-Up |
| Stages | Vergleich deiner Stages: Zeit, EXP/h, Gold/h, Schadensart der Gegner |
| Items | Item-Vergleich mit F8: Werte wie im Spiel, Unterschiede, Effekte, Urteil |
| Drops | Drops nach Seltenheit, Edelsteine nach Stufe, Runen, Schlüssel |
| Tode | Wer dich womit getötet hat |
| Charakter | Deine Werte (F9) |
| Edelsteine | Welcher Edelstein bringt dir am meisten |
| Bewertung | Eigene Werte für Effekte, die sich nicht berechnen lassen |

## Installation

### Variante A: fertige .exe (empfohlen)
1. Unter [Releases](../../releases) die neueste `DeskrawlTracker.zip` herunterladen.
2. Entpacken, z. B. nach `Dokumente\DeskrawlTracker`.
3. `DeskrawlTracker.exe` starten.

Windows SmartScreen kann beim ersten Start warnen („Unbekannter Herausgeber“), weil die Datei nicht
signiert ist: **Weitere Informationen → Trotzdem ausführen**.

### Variante B: aus dem Quellcode
1. [Python 3.12](https://www.python.org/downloads/) installieren („Add python.exe to PATH“ anhaken).
2. Repo herunterladen (grüner Button **Code → Download ZIP**) und entpacken.
3. `installieren.bat` doppelklicken.
4. Starten mit `Deskrawl Tracker starten.bat`.

### Beim ersten Start
Ein Einrichtungsfenster prüft:
1. **Log-Datei** – wird normalerweise automatisch gefunden
   (`%USERPROFILE%\AppData\LocalLow\First Day Games\Deskrawl\Game.log`), sonst über „Durchsuchen…“ wählen.
2. **Windows-Texterkennung Englisch** – fehlt auf manchen deutschen Windows-Installationen.
   Das Fenster zeigt dann den Befehl zum Nachinstallieren (PowerShell als Administrator):
   ```powershell
   Add-WindowsCapability -Online -Name "Language.OCR~~~en-US~0.0.1.0"
   ```

Später erreichst du das Fenster über **Steuerung → Log-Datei ändern**.

## Bedienung

| Taste | Funktion |
|---|---|
| **F8** | Maus über ein Item halten (Tooltip offen) → Item wird gelesen und bewertet |
| **F9** | Charakterfenster offen → deine Werte werden gelesen |
| **F10** | Deskrawl unsichtbar weiterlaufen lassen bzw. wieder zeigen |

Wichtig: Deskrawl **nicht minimieren** – ein minimiertes Fenster kann nicht gelesen werden.
Dafür gibt es F10: Das Spiel läuft unsichtbar weiter, Klicks gehen durch.

Erste Schritte nach der Einrichtung:
1. Charakterfenster öffnen, **F9** drücken.
2. Maus über deine angelegte Waffe, **F8** drücken (der Tracker merkt sich den Waffenschaden).

## Grenzen
- Angriffsgeschwindigkeit zählt voll in den Schaden, Fähigkeiten mit Abklingzeit profitieren in Wahrheit weniger.
- Manche legendären Effekte lassen sich nicht berechnen – im Tab **Bewertung** eigenen Wert eintragen.
- Leere Sockel werden mit dem besten Edelstein der gewählten Stufe (Tab Edelsteine) eingerechnet.
- Die Texterkennung kann sich verlesen. Unplausible Werte werden gelb mit „?“ markiert.

## Fehler melden
Bitte ein [Issue](../../issues) anlegen und anhängen:
- bei falsch gelesenen Items: die passenden Dateien aus dem Ordner `captures/` (Bild + `.json`),
- bei Abstürzen: `tracker_errors.log`.

Beide liegen im Ordner des Trackers.

---
Inoffizielles Fan-Tool, nicht verbunden mit First Day Games.

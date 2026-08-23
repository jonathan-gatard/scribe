[🇬🇧 English](README.md) · [🇫🇷 Français](README.fr.md) · [🇪🇸 Español](README.es.md) · **🇩🇪 Deutsch**

# Scribe — Hochleistungs-TimescaleDB-Integration für Home Assistant

Scribe ist eine Komponente der neuen Generation, die Zustände und Ereignisse von Home Assistant in eine TimescaleDB-Datenbank schreibt.

**Warum Scribe?**
Scribe ist anders gebaut. Anders als Integrationen, die auf synchrone Treiber oder den Standard-Recorder setzen, verwendet Scribe **`asyncpg`**, einen schnellen asynchronen PostgreSQL-Treiber. Dadurch bewältigt es sehr große Datenmengen, ohne die Event-Loop von Home Assistant zu blockieren. Ausgelegt ist es auf Stabilität, Geschwindigkeit und Effizienz.

**Datenstruktur und Abfragen**

Eine Erklärung der Datenstruktur und wie man sie abfragt, findest du hier: [Datenstruktur](datastructre.md)

## Inhaltsverzeichnis

- [Funktionen](#funktionen)
- [Installation](#installation)
- [Konfiguration](#konfiguration)
- [Speicher-Feinabstimmung](#speicher-feinabstimmung)
- [Aufbewahrung](#aufbewahrung)
- [Migration](#migration)
- [Statistik-Sensoren](#statistik-sensoren)
- [Dienste](#dienste)
- [Dashboard / Ansicht](#dashboard--ansicht)
- [Ökosystem / Verwandte Projekte](#ökosystem--verwandte-projekte)
- [Fehlerbehebung](#fehlerbehebung)
- [Lizenz](#lizenz)

## Funktionen

- 🚀 **Konsequent asynchrone Architektur**: auf `asyncpg` aufgebaut, für nicht blockierende Schreibvorgänge mit hohem Durchsatz.
- 📦 **TimescaleDB von Haus aus**: verwaltet Hypertables und Komprimierungsrichtlinien automatisch.
- 📊 **Detaillierte Statistiken**: optionale Sensoren für Chunk-Anzahl, Komprimierungsraten (bis zu 97 % Ersparnis!) und Schreibleistung.
- 🔒 **Sicher**: vollständige SSL/TLS-Unterstützung.
- 📈 **Zustände und Ereignisse**: schreibt alle Zustandsänderungen und Ereignisse in die Tabellen `states` und `events`.
- 👥 **Benutzerkontext**: synchronisiert die Home-Assistant-Benutzer automatisch in die Datenbank.
- 🧩 **Entitäts-Metadaten**: synchronisiert die Entitäts-Registry (Namen, Plattformen usw.) automatisch in die Tabelle `entities`.
- 🏠 **Bereiche und Geräte**: synchronisiert Bereiche und Geräte automatisch in die Tabellen `areas` und `devices`.
- 🔌 **Integrationsinfos**: synchronisiert die Konfigurationseinträge der Integrationen automatisch in die Tabelle `integrations`.
- 🎯 **Feines Filtern**: Ein- und Ausschluss nach Domain, Entität, Entitätsmuster oder Attribut.
- ✅ **Gegen eine echte Datenbank getestet**: rund 90 % Zeilenabdeckung, dazu eine End-to-End-Suite, die die Integration gegen eine echte TimescaleDB fährt statt gegen Mocks.

## Installation

### 1. Komponente installieren

**HACS (empfohlen)**

[![Öffne deine Home-Assistant-Instanz und ein Repository im Home Assistant Community Store.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=jonathan-gtd&repository=scribe&category=integration)

1. Füge dieses Repository in HACS als benutzerdefiniertes Repository hinzu.
2. Suche nach „Scribe“ und installiere es.
3. Starte Home Assistant neu.

**Manuell**
1. Kopiere den Ordner `custom_components/scribe` in das Verzeichnis `custom_components` deiner Home-Assistant-Installation.
2. Starte Home Assistant neu.

### 2. Datenbank einrichten

Du brauchst eine laufende TimescaleDB-Instanz. Ich empfehle PostgreSQL 17 oder 18.

> [!IMPORTANT]
> **Die TimescaleDB-Erweiterung ist erforderlich.** Chunking, Komprimierung,
> Aufbewahrung und die Größen-Sensoren sind der eigentliche Zweck von Scribe —
> auf reinem PostgreSQL gibt es davon nichts. Eine neue Installation wird
> abgelehnt, wenn die Erweiterung fehlt; Scribe aktiviert sie allerdings selbst,
> sofern der Server sie bereitstellt und dein Datenbankbenutzer `CREATE` auf der
> Datenbank besitzt — was die untenstehende Einrichtung gewährt. Bereits
> laufende Installationen ohne die Erweiterung zeichnen weiter auf und erfahren
> über einen Eintrag unter Reparaturen, was ihnen fehlt.

#### Variante A: Home Assistant OS (Add-on)

Unter Home Assistant OS empfehle ich das [TimescaleDB-Add-on](https://github.com/expaso/hassos-addon-timescaledb).

[![Öffne deine Home-Assistant-Instanz und zeige den Dialog zum Hinzufügen eines Add-on-Repositorys mit vorausgefüllter URL.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fexpaso%2Fhassos-addon-timescaledb)

#### Variante B: Docker (manuell)

```bash
# Hochverfügbarkeit (empfohlen)
docker run -d --name timescaledb -p 5432:5432 -e POSTGRES_PASSWORD=password timescale/timescaledb-ha:pg18

# Standard
docker run -d --name timescaledb -p 5432:5432 -e POSTGRES_PASSWORD=password timescale/timescaledb:pg18
```

Datenbank und Benutzer anlegen:

```sql
CREATE DATABASE scribe;
CREATE USER scribe WITH PASSWORD 'password';
GRANT ALL PRIVILEGES ON DATABASE scribe TO scribe;

\c scribe
CREATE EXTENSION IF NOT EXISTS timescaledb;
GRANT ALL ON SCHEMA public TO scribe;
```

## Konfiguration

### Minimale Konfiguration

```yaml
scribe:
  db_url: postgresql://scribe:password@192.168.1.10:5432/scribe
```

### Vollständige Konfiguration (Standardwerte)

<details>
<summary><b>Vollständige YAML-Konfiguration anzeigen</b></summary>

```yaml
scribe:
  db_url: postgresql://scribe:password@192.168.1.10:5432/scribe
  db_ssl: false
  chunk_time_interval: "7 days"
  compress_after: "7 days"
  retention_states: ""   # leer = unbegrenzt aufbewahren
  retention_events: ""   # leer = unbegrenzt aufbewahren
  record_states: true
  record_events: false
  batch_size: 500
  flush_interval: 5
  max_queue_size: 10000
  buffer_on_failure: true
  enable_stats_io: false
  enable_stats_chunk: false
  enable_stats_size: false
  stats_chunk_interval: 60
  stats_size_interval: 60
  include_domains: []
  include_entity_globs: []
  exclude_domains: []
  exclude_entities: []
  exclude_entity_globs: []
  exclude_attributes: []
  include_events: []
  exclude_events: []
  # Optional: einzelne Metadaten-Tabellen abschalten (Standard: true)
  enable_table_areas: true
  enable_table_devices: true
  enable_table_integrations: true
  enable_table_users: true
```
</details>

### Konfigurationsparameter

<details>
<summary><b>Parameter-Referenz anzeigen</b></summary>

| Parameter | Beschreibung |
| :--- | :--- |
| `db_url` | **Erforderlich.** Verbindungszeichenfolge zu deiner TimescaleDB-Datenbank. |
| `db_ssl` | SSL/TLS für die Datenbankverbindung aktivieren. |
| `chunk_time_interval` | Welchen Zeitraum ein Chunk der Tabelle abdeckt. Siehe [Speicher-Feinabstimmung](#speicher-feinabstimmung). |
| `compress_after` | Chunks, die älter sind als dieses Intervall, werden komprimiert. Siehe [Speicher-Feinabstimmung](#speicher-feinabstimmung). |
| `retention_states` | **Löscht** Zustandsverlauf, der älter ist als dieses Intervall (z. B. `"365 days"`). Leer (Standard) behält alles. Siehe [Aufbewahrung](#aufbewahrung). |
| `retention_events` | **Löscht** Ereignisverlauf, der älter ist als dieses Intervall. Leer (Standard) behält alles. Siehe [Aufbewahrung](#aufbewahrung). |
| `record_states` | Ob Zustandsänderungen aufgezeichnet werden. |
| `record_events` | Ob Ereignisse aufgezeichnet werden. |
| `batch_size` | Anzahl der Einträge, die gepuffert werden, bevor in die Datenbank geschrieben wird. |
| `flush_interval` | Maximale Wartezeit (in Sekunden), bevor der Puffer geleert wird. |
| `max_queue_size` | Maximale Anzahl an Einträgen im Speicher, bevor neue verworfen werden. |
| `buffer_on_failure` | Wenn wahr, bleiben Daten im Speicher, solange die Datenbank nicht erreichbar ist (bis `max_queue_size`). |
| `enable_stats_io` | Echtzeit-Sensoren zur Schreibleistung aktivieren (ohne Datenbankabfragen). |
| `enable_stats_chunk` | Sensoren für die Chunk-Anzahl aktivieren (fragen die Datenbank ab). |
| `enable_stats_size` | Sensoren für den Speicherverbrauch aktivieren (fragen die Datenbank ab). |
| `stats_chunk_interval` | Aktualisierungsintervall (in Minuten) der Chunk-Statistiken. |
| `stats_size_interval` | Aktualisierungsintervall (in Minuten) der Größenstatistiken. |
| `include_domains` | Liste der einzuschließenden Domains. |
| `include_entities` | Liste einzelner einzuschließender Entitäten. |
| `include_entity_globs` | Liste einzuschließender Entitätsmuster (z. B. `sensor.weather_*`). |
| `exclude_domains` | Liste der auszuschließenden Domains. |
| `exclude_entities` | Liste einzelner auszuschließender Entitäten. |
| `exclude_entity_globs` | Liste auszuschließender Entitätsmuster (z. B. `switch.kitchen_*`). |
| `exclude_attributes` | Liste von Attributen, die aus der Spalte `attributes` ausgeschlossen werden. |
| `include_events` | Liste der aufzuzeichnenden Ereignistypen. Leer lassen, um alle aufzuzeichnen. |
| `exclude_events` | Liste der nie aufzuzeichnenden Ereignistypen (wird nach `include_events` angewendet). |
| `enable_table_areas` | Anlegen und Synchronisieren der Tabelle `areas` aktivieren. |
| `enable_table_devices` | Anlegen und Synchronisieren der Tabelle `devices` aktivieren. |
| `enable_table_integrations` | Anlegen und Synchronisieren der Tabelle `integrations` aktivieren. |
| `enable_table_users` | Anlegen und Synchronisieren der Tabelle `users` aktivieren. |
</details>

## Speicher-Feinabstimmung

Scribe legt den Verlauf in **Hypertables** von TimescaleDB ab: eine Tabelle, die
sich wie jede andere verhält und abfragen lässt, physisch aber in **Chunks**
zerlegt ist, von denen jeder einen Zeitabschnitt abdeckt. Fast alles, was
Plattenplatz und Abfragegeschwindigkeit betrifft, folgt aus dieser Aufteilung:
Eine Abfrage über die letzte Woche liest nur die Chunks, die sie überlappen, die
Komprimierung arbeitet Chunk für Chunk, und die [Aufbewahrung](#aufbewahrung)
löscht ganze Chunks statt einzelner Zeilen.

Gesteuert wird das von zwei Einstellungen, in YAML wie in der Oberfläche unter
**Konfigurieren → Erweitert (TimescaleDB & SSL)**:

### `chunk_time_interval` (Standard `7 days`)

Welchen Zeitraum ein Chunk abdeckt.

- **Kleinere Chunks** (z. B. `1 day`) bedeuten mehr, dafür kleinere Dateien:
  feinere Aufbewahrung, und Abfragen über kurze, aktuelle Zeitfenster berühren
  weniger Daten. Ab einem gewissen Punkt muss eine Abfrage über mehrere Monate
  Hunderte von Chunks öffnen.
- **Größere Chunks** (z. B. `30 days`) bedeuten weniger, dafür größere Dateien:
  besser für lange historische Abfragen, schlechter für den Arbeitsspeicher.
  TimescaleDB selbst empfiehlt, dass die Chunks, in die geschrieben wird,
  zusammen mit ihren Indizes bequem in den Speicher passen — ein übergroßer
  Chunk auf einer kleinen Maschine bremst die Schreibvorgänge.

Der Standardwert passt zu einer typischen Home-Assistant-Instanz. `1 day` ist
eine Überlegung wert, wenn du Tausende von Entitäten aufzeichnest — und nur
dann.

> **Eine Änderung betrifft nur neue Chunks.** Bereits geschriebene Chunks
> behalten den Zeitraum, mit dem sie angelegt wurden; nichts wird neu geschrieben
> oder verschoben. Du hast dann schlicht eine Mischung aus alten und neuen
> Zeiträumen, womit TimescaleDB von Haus aus umgeht.

### `compress_after` (Standard `7 days`)

Wie alt ein Chunk sein muss, bevor TimescaleDB ihn komprimiert. Bei dieser Art
von Daten (viele wiederholte `entity_id`s und sich langsam ändernde Werte) fällt
die Größenersparnis in der Regel deutlich aus — deshalb ist die Komprimierung
standardmäßig aktiv.

Komprimierte Chunks bleiben vollständig abfragbar — der Sicht `states` ist das
gleichgültig. Das Schreiben *in* einen solchen Chunk ist langsamer, weshalb die
Komprimierung erst greift, wenn ein Chunk alt genug ist, um praktisch
abgeschlossen zu sein. Halte `compress_after` deutlich über dem Alter der Daten,
die du noch schreibst: Zustände, die verspätet eintreffen (ein Nachtrag, ein
Migrationsskript), landen in alten Chunks.

> **Eine Änderung wirkt beim nächsten Neustart**, und bereits komprimierte
> Chunks bleiben komprimiert — die Einstellung bestimmt nur, wann die
> *nächsten* komprimiert werden.

### Wie die drei Einstellungen zusammenspielen

| Einstellung | Was sie bewirkt | Umkehrbar |
| :--- | :--- | :--- |
| `chunk_time_interval` | Welchen Zeitraum ein Chunk abdeckt | Ja — nur künftige Chunks |
| `compress_after` | Wann ein Chunk komprimiert wird | Ja |
| `retention_states` / `retention_events` | Wann ein Chunk **gelöscht** wird | **Nein** |

Sie greifen in dieser Reihenfolge im Leben desselben Chunks: geschrieben →
komprimiert → gelöscht. Zwei Folgerungen sind wichtig:

- Ist `compress_after` größer als deine Aufbewahrung, werden Chunks gelöscht,
  bevor sie je komprimiert wurden — die Komprimierung bewirkt dann nichts.
- Die Aufbewahrung löscht ganze Chunks: Dein tatsächliches Zeitfenster ist also
  das eingestellte Intervall **plus** bis zu einem `chunk_time_interval`.
  Kleinere Chunks machen es genauer.

Sind die Größen- und Chunk-Sensoren aktiviert (`enable_stats_size`,
`enable_stats_chunk`), zeigen sie genau das, was diese Einstellungen bewirken:
Chunk-Anzahl, komprimierte und unkomprimierte Größen sowie die
Komprimierungsrate.

## Aufbewahrung

Standardmäßig behält Scribe alles, unbegrenzt. Wenn du nur ein begrenztes
Zeitfenster speichern willst — weil du den Rohverlauf anderswo aggregierst oder
schlicht den Plattenplatz deckeln möchtest —, setze ein Aufbewahrungsintervall,
und TimescaleDB verwirft ältere Chunks:

```yaml
scribe:
  db_url: postgresql://scribe:password@192.168.1.10:5432/scribe
  retention_states: "365 days"
  retention_events: "30 days"
```

Beides gibt es auch in der Oberfläche unter
**Konfigurieren → Erweitert (TimescaleDB & SSL)**.

> [!WARNING]
> Die Aufbewahrung **löscht Daten endgültig**. Es gibt kein Rückgängig und
> keinen Papierkorb: Sobald ein Chunk aus dem Fenster fällt, wird er gelöscht,
> und nur eine Sicherung bringt ihn zurück. Zustände und Ereignisse werden
> getrennt konfiguriert, sodass du lärmende Ereignisse verfallen lassen und den
> Zustandsverlauf behalten kannst.

Wissenswertes:

- **Keine Einstellung heißt immer „unbegrenzt aufbewahren“.** Das Feld in der
  Oberfläche zu leeren und die Zeile aus `configuration.yaml` zu löschen
  entfernen beide die Richtlinie — ein einst aus YAML übernommener Wert darf
  niemals die Zeile überleben, die ihn gesetzt hat.
- **Scribe besitzt die Aufbewahrungsrichtlinie auf seinen eigenen Tabellen.**
  Das Leeren des Feldes entfernt sie — auch eine, die du selbst mit
  `add_retention_policy()` angelegt hast. Nur so kann das Leeren der Einstellung
  in der Oberfläche das Löschen tatsächlich stoppen.
- **Sie beginnt sofort.** TimescaleDB führt die Richtlinie schon Sekunden nach
  ihrer Erstellung aus, nicht erst beim nächsten Tagesintervall: Alles außerhalb
  des Fensters ist beim ersten Lauf weg, direkt nach dem Neustart, der sie
  aktiviert hat.
- **Gelöscht wird chunkweise, nicht zeilenweise.** Ein Chunk wird erst gelöscht,
  wenn *alle* seine Zeilen älter als das Intervall sind — mit dem
  Standardwert `chunk_time_interval` von 7 Tagen behältst du also bis zu eine
  Woche mehr als gewünscht. Genau das macht die Aufbewahrung nahezu kostenlos:
  Sie verwirft Dateien, statt Zeilen zu löschen.
- **Nur der Verlauf wird gelöscht.** Die Tabelle `entities` und die übrigen
  Metadaten-Tabellen bleiben unangetastet: Eine Entität, deren Verlauf
  vollständig abgelaufen ist, lässt sich weiterhin auflösen.
- **TimescaleDB ist erforderlich** — die Erweiterung führt die Richtlinie aus.
  Auf reinem PostgreSQL erzeugt ein gesetztes Aufbewahrungsintervall einen
  Eintrag unter Reparaturen, statt stillschweigend nichts zu tun.
- Zulässig sind einfache Intervalle: `30 days`, `6 months`, `1 year`. Alles
  andere wird mit einem Fehler abgelehnt, statt an die Datenbank geschickt zu
  werden.

## Migration

### Aktualisierung von Scribe 2.x

Scribe 3.0 ersetzte die Tabelle `states` durch `states_raw` plus eine
Kompatibilitäts-Sicht und gab `entities` einen numerischen Primärschlüssel. Die
Umwandlung einer alten Datenbank trugen die 3.x-Versionen; **in 3.9 wurde sie
entfernt**.

Wenn deine Datenbank noch eine `states`-*Tabelle* (statt einer Sicht), eine
Tabelle `states_legacy` oder eine Tabelle `entities` ohne Spalte `id` enthält,
hält Scribe beim Start an, zeichnet nichts auf und meldet einen Eintrag unter
Reparaturen — ohne irgendetwas umzubenennen, anzulegen oder zu löschen.
Installiere **Scribe 3.8**, lass Home Assistant laufen, bis das Protokoll die
abgeschlossene Migration meldet (bei einer großen Datenbank rund fünfzehn
Minuten), und aktualisiere dann erneut.

Neuinstallationen und jede von 3.x angelegte Datenbank sind nicht betroffen.

### Daten aus anderen Quellen übernehmen

Scribe bringt Skripte mit, um Daten aus verschiedenen Quellen zu übernehmen.

### Migration aus InfluxDB

<details>
<summary><b>InfluxDB-Migrationsanleitung anzeigen</b></summary>

1. Wechsle in das Verzeichnis `migration`:
   ```bash
   cd migration
   ```

2. Installiere die Abhängigkeiten:
   ```bash
   pip install influxdb-client psycopg2-binary python-dotenv
   ```

3. Konfiguriere die Migration:
   ```bash
   cp .env.example .env
   nano .env
   # Trage [InfluxDB Configuration], [Scribe Configuration] und [Migration Settings] ein
   ```

4. Starte die Migration:
   ```bash
   python3 influx2scribe.py
   ```
</details>

### Migration aus LTSS

<details>
<summary><b>LTSS-Migrationsanleitung anzeigen</b></summary>

1. Wechsle in das Verzeichnis `migration`:
   ```bash
   cd migration
   ```

2. Installiere die Abhängigkeiten:
   ```bash
   pip install psycopg2-binary python-dotenv
   ```

3. Konfiguriere die Migration:
   ```bash
   cp .env.example .env
   nano .env
   # Trage [LTSS Configuration], [Scribe Configuration] und [Migration Settings] ein
   ```

4. Starte die Migration:
   ```bash
   python3 ltss2scribe.py
   ```
</details>

### Migration aus dem Recorder

<details>
<summary><b>Recorder-Migrationsanleitung anzeigen</b></summary>

1. Wechsle in das Verzeichnis `migration`:
   ```bash
   cd migration
   ```

2. Installiere die Abhängigkeiten:
   ```bash
   pip install psycopg2-binary python-dotenv
   ```

3. Konfiguriere die Migration:
   ```bash
   cp .env.example .env
   nano .env
   # Trage [Recorder Configuration], [Scribe Configuration] und [Migration Settings] ein
   ```

4. Starte die Migration:
   ```bash
   python3 recorder2scribe.py
   ```
</details>

## Statistik-Sensoren

Aktiviere die Sensoren, indem du ihre Optionen in der Konfiguration setzt.

### Schreibstatistiken (`enable_stats_io: true`)

<details>
<summary><b>Schreib-Sensoren anzeigen</b></summary>

Echtzeitwerte aus dem Writer (ohne Datenbankabfragen).

| Sensor | Beschreibung |
| :--- | :--- |
| <img src="https://api.iconify.design/mdi:database-plus.svg?color=%232196F3" width="15" /> `sensor.scribe_states_written` | Gesamtzahl der in die Datenbank geschriebenen Zustandsänderungen. |
| <img src="https://api.iconify.design/mdi:database-plus.svg?color=%232196F3" width="15" /> `sensor.scribe_events_written` | Gesamtzahl der in die Datenbank geschriebenen Ereignisse. |
| <img src="https://api.iconify.design/mdi:buffer.svg?color=%232196F3" width="15" /> `sensor.scribe_buffer_size` | Aktuelle Anzahl der Einträge im Speicherpuffer. |
| <img src="https://api.iconify.design/mdi:timer-sand.svg?color=%232196F3" width="15" /> `sensor.scribe_write_duration` | Dauer (in ms) des letzten Schreibvorgangs. |
| <img src="https://api.iconify.design/mdi:speedometer.svg?color=%232196F3" width="15" /> `sensor.scribe_states_rate` | Rate der geschriebenen Zustände (pro Minute). |
| <img src="https://api.iconify.design/mdi:speedometer.svg?color=%232196F3" width="15" /> `sensor.scribe_events_rate` | Rate der geschriebenen Ereignisse (pro Minute). |
</details>

### Chunk-Statistiken (`enable_stats_chunk: true`)

<details>
<summary><b>Chunk-Sensoren anzeigen</b></summary>

Chunk-Anzahl (alle `stats_chunk_interval` Minuten aktualisiert).

| Sensor | Beschreibung |
| :--- | :--- |
| <img src="https://api.iconify.design/mdi:cube-outline.svg?color=%232196F3" width="15" /> `sensor.scribe_states_total_chunks` | Gesamtzahl der Chunks der Zustandstabelle. |
| <img src="https://api.iconify.design/mdi:package-down.svg?color=%232196F3" width="15" /> `sensor.scribe_states_compressed_chunks` | Anzahl der bereits komprimierten Chunks. |
| <img src="https://api.iconify.design/mdi:package-up.svg?color=%232196F3" width="15" /> `sensor.scribe_states_uncompressed_chunks` | Anzahl der Chunks, die auf Komprimierung warten. |
| <img src="https://api.iconify.design/mdi:cube-outline.svg?color=%232196F3" width="15" /> `sensor.scribe_events_total_chunks` | Gesamtzahl der Chunks der Ereignistabelle. |
| <img src="https://api.iconify.design/mdi:package-down.svg?color=%232196F3" width="15" /> `sensor.scribe_events_compressed_chunks` | Anzahl der komprimierten Ereignis-Chunks. |
| <img src="https://api.iconify.design/mdi:package-up.svg?color=%232196F3" width="15" /> `sensor.scribe_events_uncompressed_chunks` | Anzahl der unkomprimierten Ereignis-Chunks. |
</details>

### Größenstatistiken (`enable_stats_size: true`)

<details>
<summary><b>Größen-Sensoren anzeigen</b></summary>

Belegter Speicher in Bytes (alle `stats_size_interval` Minuten aktualisiert).

| Sensor | Beschreibung |
| :--- | :--- |
| <img src="https://api.iconify.design/mdi:database.svg?color=%232196F3" width="15" /> `sensor.scribe_states_total_size` | Gesamtgröße auf der Platte (komprimierte Daten + aktuelle Chunks + Indizes). |
| <img src="https://api.iconify.design/mdi:database-search.svg?color=%232196F3" width="15" /> `sensor.scribe_states_original_size` | **Theoretische Größe** ohne Komprimierung (z. B. 11 GB). |
| <img src="https://api.iconify.design/mdi:package-variant.svg?color=%232196F3" width="15" /> `sensor.scribe_states_compressed_size` | Physische Größe der komprimierten Daten-Chunks. |
| <img src="https://api.iconify.design/mdi:package-variant-closed.svg?color=%232196F3" width="15" /> `sensor.scribe_states_uncompressed_size` | Größe der noch nicht komprimierten aktuellen Daten (oder ausstehender Indizes). |
| <img src="https://api.iconify.design/mdi:percent.svg?color=%232196F3" width="15" /> `sensor.scribe_states_compression_ratio` | Komprimierungsrate der Zustände (%). |
| <img src="https://api.iconify.design/mdi:database.svg?color=%232196F3" width="15" /> `sensor.scribe_events_total_size` | Gesamtgröße der Ereignistabelle auf der Platte. |
| <img src="https://api.iconify.design/mdi:database-search.svg?color=%232196F3" width="15" /> `sensor.scribe_events_original_size` | Theoretische Größe der Ereignisse vor der Komprimierung. |
| <img src="https://api.iconify.design/mdi:package-variant.svg?color=%232196F3" width="15" /> `sensor.scribe_events_compressed_size` | Größe der komprimierten Ereignisdaten. |
| <img src="https://api.iconify.design/mdi:package-variant-closed.svg?color=%232196F3" width="15" /> `sensor.scribe_events_uncompressed_size` | Größe der unkomprimierten Ereignisdaten. |
| <img src="https://api.iconify.design/mdi:percent.svg?color=%232196F3" width="15" /> `sensor.scribe_events_compression_ratio` | Komprimierungsrate der Ereignisse (%). |
</details>

## Dienste

### `scribe.flush`
Erzwingt das sofortige Schreiben der gepufferten Daten in die Datenbank.

```yaml
service: scribe.flush
```

### `scribe.query`
Führt eine reine Leseabfrage (SQL) gegen die TimescaleDB-Datenbank aus.

**Parameter:**
- `sql` (erforderlich): die auszuführende SQL-Abfrage. Muss eine `SELECT`-Anweisung sein.

**Rückgabe:**
Eine Liste von Zeilen, wobei jede Zeile ein Wörterbuch aus Spaltennamen und Werten ist.

**Beispiel:**
```yaml
service: scribe.query
data:
  sql: "SELECT * FROM states ORDER BY time DESC LIMIT 5"
response_variable: query_result
```

## Fehlerbehebung

### Reparaturen

Scribe meldet Probleme, die es nicht selbst lösen kann, unter
**Einstellungen → System → Reparaturen**, damit du nicht die Protokolle
beobachten musst. Jeder Eintrag verschwindet von selbst, sobald die Ursache
behoben ist.

| Reparatur | Was sie bedeutet |
| --- | --- |
| Datenbank nicht erreichbar | Die Verbindung schlug beim Start fehl: **es wird nichts aufgezeichnet**. Prüfe, ob der Server läuft und ob URL und Zugangsdaten stimmen. |
| Schreiben in die Datenbank nicht möglich | Mehrere Schreibvorgänge in Folge sind fehlgeschlagen. Die Daten bleiben im Speicher und werden nach der Erholung geschrieben — sofern Home Assistant nicht vorher neu startet. |
| Puffer ist voll | Die Schreibfehler dauerten lange genug an, um den Puffer zu füllen; die ältesten Einträge werden nun verworfen. Repariere die Datenbank oder erhöhe `max_queue_size`. |
| Einträge werden verworfen | Ein Schreibvorgang schlug fehl, während die Pufferung deaktiviert ist — die Einträge gingen sofort verloren. Aktiviere die Pufferung, um kurze Ausfälle zu überstehen. |
| Tabellen konnten nicht angelegt werden | Scribe hat die Datenbank erreicht, konnte sein Schema aber nicht aufbauen, meist ein Rechteproblem. Auf einer neuen Datenbank wird überhaupt nichts aufgezeichnet. |
| Sicht `states` konnte nicht angelegt werden | Der Verlauf wird aufgezeichnet, doch die Sicht, über die jede Abfrage läuft, fehlt — der Verlauf wirkt leer, obwohl nichts verloren ist. |
| `states_raw` / `events` ist keine Hypertable | TimescaleDB ist installiert, die Tabelle wurde aber nie umgewandelt (häufig, wenn die Erweiterung *nach* dem Befüllen der Tabellen hinzukam). Chunks, Komprimierung und Aufbewahrung bewirken nichts. |
| `states_raw` / `events` wird nie komprimiert | Die Tabelle ist zwar eine Hypertable, hat aber keine Komprimierungsrichtlinie und behält ihre unkomprimierte Größe. |
| TimescaleDB ist nicht installiert | Der Verlauf wird aufgezeichnet, aber Chunking und Komprimierung stehen nicht zur Verfügung: Die Datenbank wächst deutlich schneller und die Größen-Sensoren bleiben leer. |
| Datenbank älter als Version 3.0 | Die Datenbank nutzt noch das Schema vor 3.0, das diese Version nicht umwandeln kann. Es wird nichts aufgezeichnet und nichts verändert — installiere Scribe 3.8 zur Umwandlung und aktualisiere danach erneut. |
| Aufbewahrungsrichtlinie nicht angewendet | Du hast das Löschen von Daten ab einem Intervall verlangt, die Richtlinie konnte aber nicht angelegt werden. Es wurde nichts gelöscht und es wird nichts gelöscht — die Tabelle wächst weiter. |
| Umbenennung einer Entität nicht angewendet | Eine Umbenennung kollidierte mit einer bereits vorhandenen Zeile. Der Verlauf der Entität verteilt sich auf zwei Kennungen. |

### Hoher Speicherverbrauch
- `max_queue_size` verringern
- `flush_interval` verringern, um häufiger zu schreiben
- `sensor.scribe_buffer_size` im Auge behalten

### Leistungsoptimierung

Ist die Sicht `states` langsam (mehrere Sekunden pro Abfrage), wählt der
PostgreSQL-Planer meist einen **Hash Join** statt eines **Nested Loop**, was
TimescaleDB daran hindert, Chunks wirksam auszuschließen.

Häufigste Ursache ist ein hoher `random_page_cost` (Standard `4.0`, auf
Festplatten ausgelegt). Bei modernem Speicher (SSD, NVMe) oder einer gut
zwischengespeicherten Datenbank solltest du diesen Wert senken:

```sql
-- Aktuellen Wert anzeigen
SHOW random_page_cost;

-- Auf einen niedrigeren Wert setzen (meist 1.1)
ALTER SYSTEM SET random_page_cost = 1.1;
SELECT pg_reload_conf();
```

Ein niedrigerer Wert bewegt den Planer zu indexbasierten Verknüpfungen (Nested
Loops), die für Scribes Leistung bei großen Datenmengen entscheidend sind.

### Immer noch Probleme?
[Öffne bitte ein Issue](https://github.com/jonathan-gtd/scribe/issues) auf GitHub mit deinen Protokollen und deiner Konfiguration. Ich helfe gerne!

## Dashboard / Ansicht

Ein vorbereitetes Lovelace-Layout mit allen nützlichen Scribe-Sensoren
(Datenbankstatistiken, Komprimierungsraten, Schreibleistung) liegt in diesem
Repository, in zwei Varianten:

| Datei | Was es ist | Wohin einfügen |
| --- | --- | --- |
| [`lovelace_scribe_card.yaml`](lovelace_scribe_card.yaml) | Eine **einzelne Karte** (`type: vertical-stack`) | Der YAML-Editor einer Karte („Karte hinzufügen“ → „Manuell“) |
| [`lovelace_scribe_view.yaml`](lovelace_scribe_view.yaml) | Eine **ganze Ansicht** (`title` / `icon` / `cards`) | Der YAML-Editor einer Ansicht |

> ⚠️ Die beiden sind nicht austauschbar. Die *Ansicht*-Datei in einen
> *Karten*-Editor einzufügen scheitert mit **„No card type configured“**, denn
> eine Kartenkonfiguration muss mit einem `type:`-Schlüssel beginnen.

**Variante A — als Karte hinzufügen (am einfachsten, funktioniert in jedem Ansichtstyp):**

1.  Öffne dein Dashboard und klicke auf „Dashboard bearbeiten“ (Stiftsymbol).
2.  Klicke auf **+ Karte hinzufügen** und wähle ganz unten in der Auswahl **Manuell**.
3.  Kopiere den Inhalt von [`lovelace_scribe_card.yaml`](lovelace_scribe_card.yaml), ersetze damit alles im Editor und klicke auf **Speichern**.

**Variante B — als eigene Ansicht hinzufügen:**

1.  Öffne dein Dashboard und klicke auf „Dashboard bearbeiten“ (Stiftsymbol).
2.  Klicke auf die Schaltfläche **+** *in der oberen Reiterleiste* (neben deinen vorhandenen Ansichten), um eine Ansicht hinzuzufügen — nicht auf „Karte hinzufügen“.
3.  Öffne im Ansichtsdialog das Menü ⋮ (oder die Schaltfläche „Code-Editor anzeigen“) und wähle **In YAML bearbeiten**.
4.  Kopiere den Inhalt von [`lovelace_scribe_view.yaml`](lovelace_scribe_view.yaml), ersetze damit alles im Editor und klicke auf **Speichern**.

## Ökosystem / Verwandte Projekte

Diese Projekte harmonieren gut mit Scribe:

- [timescale_database_reader](https://github.com/remmob/timescale_database_reader): eine benutzerdefinierte Komponente, die Daten aus TimescaleDB zurück in Home-Assistant-Sensoren liest.
- [timescale-plotly-card](https://github.com/remmob/timescale-plotly-card): eine hochgradig anpassbare Karte auf Plotly-Basis, die TimescaleDB direkt abfragen kann.

## Lizenz

MIT-Lizenz — Einzelheiten in der Datei LICENSE

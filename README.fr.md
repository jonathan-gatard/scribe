[🇬🇧 English](README.md) · **🇫🇷 Français** · [🇪🇸 Español](README.es.md) · [🇩🇪 Deutsch](README.de.md)

# Scribe — Intégration TimescaleDB haute performance pour Home Assistant

Scribe est un composant de nouvelle génération qui écrit les états et les événements de Home Assistant dans une base de données TimescaleDB.

**Pourquoi Scribe ?**
Scribe est conçu différemment. Contrairement aux intégrations qui reposent sur des pilotes synchrones ou sur le recorder par défaut, Scribe utilise **`asyncpg`**, un pilote PostgreSQL asynchrone très performant. Il peut ainsi absorber d'énormes volumes de données sans bloquer la boucle d'événements de Home Assistant. Il est pensé pour la stabilité, la vitesse et l'efficacité.

**Structure des données et requêtes**

Une explication de la structure des données et de la façon de l'interroger se trouve ici : [Structure des données](datastructre.md)

## Table des matières

- [Fonctionnalités](#fonctionnalités)
- [Installation](#installation)
- [Configuration](#configuration)
- [Réglage du stockage](#réglage-du-stockage)
- [Rétention](#rétention)
- [Schéma de la base de données](#schéma-de-la-base-de-données)
- [Migration](#migration)
- [Capteurs de statistiques](#capteurs-de-statistiques)
- [Services](#services)
- [Tableau de bord / Vue](#tableau-de-bord--vue)
- [Écosystème / Projets liés](#écosystème--projets-liés)
- [Dépannage](#dépannage)
- [Licence](#licence)

## Fonctionnalités

- 🚀 **Architecture asynchrone d'abord** : bâtie sur `asyncpg` pour des écritures non bloquantes et à haut débit.
- 📦 **TimescaleDB natif** : gère automatiquement les hypertables et les politiques de compression.
- 📊 **Statistiques détaillées** : capteurs optionnels pour suivre le nombre de chunks, les taux de compression (jusqu'à 97 % d'économie !) et les performances d'écriture.
- 🔒 **Sécurisé** : prise en charge complète de SSL/TLS.
- 📈 **États et événements** : enregistre tous les changements d'état et les événements dans les tables `states` et `events`.
- 👥 **Contexte utilisateur** : synchronise automatiquement les utilisateurs de Home Assistant dans la base pour un contexte plus riche.
- 🧩 **Métadonnées des entités** : synchronise automatiquement le registre des entités (noms, plateformes, etc.) dans la table `entities`.
- 🏠 **Contexte pièces et appareils** : synchronise automatiquement les pièces et les appareils dans les tables `areas` et `devices`.
- 🔌 **Informations d'intégration** : synchronise automatiquement les entrées de configuration des intégrations dans la table `integrations`.
- 🎯 **Filtrage fin** : inclusion/exclusion par domaine, entité, motif d'entité ou attribut.
- ✅ **Testé contre une vraie base** : ~90 % de couverture de lignes, et une suite de bout en bout qui pilote l'intégration contre une vraie TimescaleDB plutôt que des mocks.

## Installation

### 1. Installer le composant

**HACS (recommandé)**

[![Ouvrir votre instance Home Assistant et ouvrir un dépôt dans le Home Assistant Community Store.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=jonathan-gtd&repository=scribe&category=integration)

1. Ajoutez ce dépôt comme dépôt personnalisé dans HACS.
2. Cherchez « Scribe » et installez-le.
3. Redémarrez Home Assistant.

**Manuel**
1. Copiez le dossier `custom_components/scribe` dans le répertoire `custom_components` de votre Home Assistant.
2. Redémarrez Home Assistant.

### 2. Mise en place de la base de données

Il vous faut une instance TimescaleDB en fonctionnement. Je recommande PostgreSQL 17 ou 18.

> [!IMPORTANT]
> **L'extension TimescaleDB est obligatoire.** Le découpage en chunks, la
> compression, la rétention et les capteurs de taille sont toute la raison
> d'être de Scribe, et aucun n'existe sur PostgreSQL nu. Une nouvelle
> installation est refusée si l'extension manque — Scribe l'active toutefois
> lui-même lorsque le serveur en dispose et que votre utilisateur PostgreSQL a
> le droit `CREATE` sur la base, ce que la procédure ci-dessous accorde. Les
> installations qui tournent déjà sans elle continuent d'enregistrer et sont
> informées de ce qui leur manque par un problème dans Repairs.

#### Option A : Home Assistant OS (module complémentaire)

Sous Home Assistant OS, je recommande le [module complémentaire TimescaleDB](https://github.com/expaso/hassos-addon-timescaledb).

[![Ouvrir votre instance Home Assistant et afficher la boîte de dialogue d'ajout de dépôt de modules complémentaires avec une URL pré-remplie.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fexpaso%2Fhassos-addon-timescaledb)

#### Option B : Docker (manuel)

```bash
# Haute disponibilité (recommandé)
docker run -d --name timescaledb -p 5432:5432 -e POSTGRES_PASSWORD=password timescale/timescaledb-ha:pg18

# Standard
docker run -d --name timescaledb -p 5432:5432 -e POSTGRES_PASSWORD=password timescale/timescaledb:pg18
```

Créez la base et l'utilisateur :

```sql
CREATE DATABASE scribe;
CREATE USER scribe WITH PASSWORD 'password';
GRANT ALL PRIVILEGES ON DATABASE scribe TO scribe;

\c scribe
CREATE EXTENSION IF NOT EXISTS timescaledb;
GRANT ALL ON SCHEMA public TO scribe;
```

## Configuration

### Configuration minimale

```yaml
scribe:
  db_url: postgresql://scribe:password@192.168.1.10:5432/scribe
```

### Configuration complète (valeurs par défaut)

<details>
<summary><b>Afficher la configuration YAML complète</b></summary>

```yaml
scribe:
  db_url: postgresql://scribe:password@192.168.1.10:5432/scribe
  db_ssl: false
  ssl_root_cert: ""      # utilisé uniquement si db_ssl vaut true
  ssl_cert_file: ""
  ssl_key_file: ""
  db_schema: ""      # vide = le schéma de la connexion
  chunk_time_interval: "7 days"
  compress_after: "7 days"
  retention_states: ""   # vide = conservation illimitée
  retention_events: ""   # vide = conservation illimitée
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
  include_entities: []
  include_entity_globs: []
  exclude_domains: []
  exclude_entities: []
  exclude_entity_globs: []
  exclude_attributes: []
  include_events: []
  exclude_events: []
  # Optionnel : désactiver certaines tables de métadonnées (défaut : true)
  enable_table_areas: true
  enable_table_devices: true
  enable_table_integrations: true
  enable_table_users: true
```
</details>

### Paramètres de configuration

<details>
<summary><b>Afficher la référence des paramètres</b></summary>

| Paramètre | Description |
| :--- | :--- |
| `db_url` | **Obligatoire.** Chaîne de connexion vers votre base TimescaleDB. |
| `db_ssl` | Activer SSL/TLS pour la connexion à la base. |
| `ssl_root_cert` | Chemin vers le fichier CA (ex. `/ssl/ca.crt`). Un chemin relatif est résolu depuis le répertoire de configuration de Home Assistant. |
| `ssl_cert_file` | Chemin vers le certificat client, pour le TLS mutuel. |
| `ssl_key_file` | Chemin vers la clé privée client, pour le TLS mutuel. |
| `db_schema` | Schéma PostgreSQL dans lequel enregistrer. Vide (défaut) : celui de la connexion, normalement `public`. Voir [Schéma de la base de données](#schéma-de-la-base-de-données). |
| `chunk_time_interval` | Durée couverte par chaque chunk de la table. Voir [Réglage du stockage](#réglage-du-stockage). |
| `compress_after` | Les chunks plus anciens que cet intervalle sont compressés. Voir [Réglage du stockage](#réglage-du-stockage). |
| `retention_states` | **Supprime** l'historique des états plus ancien que cet intervalle (ex. `"365 days"`). Vide (défaut) : tout est conservé. Voir [Rétention](#rétention). |
| `retention_events` | **Supprime** l'historique des événements plus ancien que cet intervalle. Vide (défaut) : tout est conservé. Voir [Rétention](#rétention). |
| `record_states` | Enregistrer ou non les changements d'état. |
| `record_events` | Enregistrer ou non les événements. |
| `batch_size` | Nombre d'éléments mis en tampon avant écriture en base. |
| `flush_interval` | Délai maximal (en secondes) avant de vider le tampon. |
| `max_queue_size` | Nombre maximal d'éléments gardés en mémoire avant d'écarter les nouveaux. |
| `buffer_on_failure` | Si vrai, conserve les données en mémoire quand la base est injoignable (jusqu'à `max_queue_size`). |
| `enable_stats_io` | Activer les capteurs de performance de l'écrivain en temps réel (aucune requête en base). |
| `enable_stats_chunk` | Activer les capteurs de nombre de chunks (interrogent la base). |
| `enable_stats_size` | Activer les capteurs de taille de stockage (interrogent la base). |
| `stats_chunk_interval` | Intervalle (en minutes) de mise à jour des statistiques de chunks. |
| `stats_size_interval` | Intervalle (en minutes) de mise à jour des statistiques de taille. |
| `include_domains` | Liste des domaines à inclure. |
| `include_entities` | Liste des entités précises à inclure. |
| `include_entity_globs` | Liste de motifs d'entités à inclure (ex. `sensor.weather_*`). |
| `exclude_domains` | Liste des domaines à exclure. |
| `exclude_entities` | Liste des entités précises à exclure. |
| `exclude_entity_globs` | Liste de motifs d'entités à exclure (ex. `switch.kitchen_*`). |
| `exclude_attributes` | Liste d'attributs à exclure de la colonne `attributes`. |
| `include_events` | Liste des types d'événements à enregistrer. Laisser vide pour tous les enregistrer. |
| `exclude_events` | Liste des types d'événements à ne jamais enregistrer (appliquée après `include_events`). |
| `enable_table_areas` | Activer la création et la synchronisation de la table `areas`. |
| `enable_table_devices` | Activer la création et la synchronisation de la table `devices`. |
| `enable_table_integrations` | Activer la création et la synchronisation de la table `integrations`. |
| `enable_table_users` | Activer la création et la synchronisation de la table `users`. |
</details>

## Réglage du stockage

Scribe range l'historique dans des **hypertables** TimescaleDB : une table qui
s'utilise et s'interroge comme n'importe quelle autre, mais qui est
physiquement découpée en **chunks**, chacun couvrant une tranche de temps.
Presque tout ce qui touche à l'espace disque et à la vitesse des requêtes
découle de ce découpage — une requête sur la semaine dernière ne lit que les
chunks qui la recouvrent, la compression travaille chunk par chunk, et la
[rétention](#rétention) supprime des chunks entiers plutôt que des lignes.

Deux réglages le pilotent, en YAML comme dans l'interface sous
**Configurer → Avancé (TimescaleDB & SSL)** :

### `chunk_time_interval` (défaut `7 days`)

La durée couverte par un chunk.

- **Des chunks plus petits** (ex. `1 day`) : plus de fichiers, plus petits — une
  rétention plus fine, et les requêtes sur des fenêtres récentes touchent moins
  de données. Passé un certain point, une requête sur plusieurs mois doit ouvrir
  des centaines de chunks.
- **Des chunks plus gros** (ex. `30 days`) : moins de fichiers, plus gros —
  mieux pour les requêtes historiques longues, moins bien pour la mémoire.
  La recommandation de TimescaleDB est que les chunks dans lesquels vous écrivez
  tiennent confortablement en mémoire avec leurs index : un chunk surdimensionné
  sur une petite machine pénalise les écritures.

La valeur par défaut convient à une instance Home Assistant classique.
Envisagez `1 day` si vous enregistrez des milliers d'entités, et seulement dans
ce cas.

> **Le changement ne concerne que les nouveaux chunks.** Ceux déjà écrits
> conservent la durée avec laquelle ils ont été créés, rien n'est réécrit ni
> déplacé — vous aurez simplement un mélange d'anciennes et de nouvelles durées,
> ce que TimescaleDB gère nativement.

### `compress_after` (défaut `7 days`)

L'âge à partir duquel TimescaleDB compresse un chunk. La compression réduit
fortement la taille pour ce type de données (beaucoup d'`entity_id` répétés et
des valeurs qui changent lentement), d'où son activation par défaut.

Les chunks compressés restent parfaitement interrogeables — la vue `states` n'y
voit aucune différence. En revanche, y **écrire** est plus lent : c'est pourquoi
la compression n'intervient qu'une fois le chunk assez ancien pour être
considéré comme terminé. Gardez `compress_after` confortablement au-dessus de
l'âge des données que vous écrivez encore ; des états arrivant dans le désordre
(un rattrapage, un script de migration) atterrissent dans d'anciens chunks.

> **Le changement prend effet au redémarrage suivant**, et les chunks déjà
> compressés le restent — le réglage ne décide que du moment où les *prochains*
> le seront.

### Comment les trois réglages s'articulent

| Réglage | Ce qu'il fait | Réversible |
| :--- | :--- | :--- |
| `chunk_time_interval` | La durée couverte par un chunk | Oui — futurs chunks seulement |
| `compress_after` | Quand un chunk est compressé | Oui |
| `retention_states` / `retention_events` | Quand un chunk est **supprimé** | **Non** |

Ils s'appliquent dans cet ordre au même chunk tout au long de sa vie : écrit →
compressé → supprimé. Deux conséquences à connaître :

- Si `compress_after` dépasse votre rétention, les chunks sont supprimés avant
  d'avoir jamais été compressés, et la compression ne sert à rien.
- La rétention supprime des chunks entiers : votre fenêtre réelle est donc
  l'intervalle demandé **plus** jusqu'à un `chunk_time_interval`. Des chunks
  plus petits la resserrent.

Si les capteurs de taille et de chunks sont activés (`enable_stats_size`,
`enable_stats_chunk`), ils rapportent exactement ce que ces réglages produisent :
nombre de chunks, tailles compressée et non compressée, taux de compression.

## Rétention

Par défaut, Scribe conserve tout, indéfiniment. Si vous ne voulez garder qu'une
fenêtre bornée — parce que vous agrégez l'historique brut ailleurs, ou
simplement pour plafonner l'espace disque — indiquez un intervalle de rétention
et TimescaleDB supprimera les chunks plus anciens :

```yaml
scribe:
  db_url: postgresql://scribe:password@192.168.1.10:5432/scribe
  retention_states: "365 days"
  retention_events: "30 days"
```

Les deux sont aussi disponibles dans l'interface, sous
**Configurer → Avancé (TimescaleDB & SSL)**.

> [!WARNING]
> La rétention **supprime les données définitivement**. Il n'y a ni annulation
> ni corbeille : dès qu'un chunk sort de la fenêtre, il est supprimé, et seule
> une sauvegarde peut le ramener. États et événements se configurent séparément,
> ce qui permet de faire expirer des événements bavards tout en conservant
> l'historique des états.

Ce qu'il faut savoir :

- **Aucun réglage signifie toujours « conserver indéfiniment ».** Vider le champ
  dans l'interface et supprimer la ligne de `configuration.yaml` retirent tous
  deux la politique — une valeur importée un jour depuis le YAML n'a jamais le
  droit de survivre à la ligne qui l'a définie.
- **Scribe est propriétaire de la politique de rétention sur ses propres
  tables.** Vider le champ la supprime — y compris une politique que vous auriez
  créée à la main avec `add_retention_policy()`, ce qui est la seule façon pour
  l'interface d'arrêter réellement les suppressions.
- **Cela démarre immédiatement.** TimescaleDB exécute la politique quelques
  secondes après sa création, et non au prochain intervalle quotidien : tout ce
  qui est hors de la fenêtre disparaît dès la première exécution, juste après le
  redémarrage qui l'a activée.
- **La suppression se fait par chunk, pas par ligne.** Un chunk n'est supprimé
  que lorsque *toutes* ses lignes sont plus anciennes que l'intervalle : avec le
  `chunk_time_interval` par défaut de 7 jours, vous conservez donc jusqu'à une
  semaine de plus que demandé. C'est ce qui rend la rétention quasi gratuite :
  elle supprime des fichiers plutôt que des lignes.
- **Seul l'historique est supprimé.** La table `entities` et les autres tables
  de métadonnées ne sont pas touchées : une entité dont l'historique a
  entièrement expiré reste résolue.
- **TimescaleDB est indispensable** — c'est l'extension qui exécute la
  politique. Sur PostgreSQL nu, indiquer un intervalle de rétention déclenche un
  problème dans Repairs au lieu de ne rien faire en silence.
- Les valeurs acceptées sont des intervalles simples : `30 days`, `6 months`,
  `1 year`. Toute autre valeur est refusée avec une erreur plutôt qu'envoyée à
  la base.

## Schéma de la base de données

Par défaut, Scribe enregistre dans le schéma vers lequel votre connexion pointe
déjà — normalement `public`. Renseignez `db_schema` et il crée ce schéma et y
place tout : ses tables, ses vues, ses hypertables et ses politiques.

```yaml
scribe:
  db_url: postgresql://scribe:password@192.168.1.10:5432/scribe
  db_schema: scribe
```

L'option est aussi dans l'interface, sous **Configurer → Avancé (TimescaleDB &
SSL)**.

C'est ce qu'il vous faut quand Scribe partage une base avec autre chose : vos
propres copies transformées de l'historique, les tables d'une autre intégration,
ou un second Home Assistant qui enregistre sur le même serveur. Chaque schéma est
indépendant — tables, hypertables, politiques de rétention et de compression
distinctes — et rien de ce que Scribe fait dans l'un n'atteint l'autre.

À savoir :

- **Seules les nouvelles données y vont.** Renseigner `db_schema` ne déplace pas
  l'historique déjà enregistré : Scribe crée un jeu de tables vide dans le
  nouveau schéma et commence à y écrire. Pour conserver l'ancien historique,
  déplacez-le vous-même (`ALTER TABLE public.states_raw SET SCHEMA scribe;`)
  avant le redémarrage, ou interrogez directement l'ancien schéma.
- **Scribe crée le schéma s'il le peut.** L'utilisateur de la base a besoin du
  droit `CREATE` sur celle-ci. Un schéma créé par quelqu'un d'autre convient tout
  aussi bien, tant que l'utilisateur y a `USAGE` et `CREATE` :
  ```sql
  CREATE SCHEMA IF NOT EXISTS scribe;
  GRANT USAGE, CREATE ON SCHEMA scribe TO scribe;
  ```
- **Un schéma inaccessible arrête l'enregistrement.** PostgreSQL n'échoue pas
  sur un schéma absent — il passe silencieusement à l'entrée suivante du search
  path — de sorte qu'une faute de frappe ou un droit manquant remplirait
  `public` pendant que l'interface afficherait autre chose. Scribe vérifie où il
  a réellement atterri et n'enregistre rien plutôt que d'enregistrer au mauvais
  endroit, avec un problème dans Réparations qui explique quels droits accorder.
- **Vos requêtes ne changent pas.** Scribe place le schéma en tête du
  `search_path` de la connexion, donc `SELECT * FROM states` continue de
  fonctionner via le service `scribe.query`. Depuis ailleurs — Grafana, psql, un
  tableau de bord — qualifiez le nom (`scribe.states`) ou définissez votre propre
  `search_path`.
- **`public` reste dans le chemin.** C'est là que l'extension TimescaleDB
  installe `create_hypertable()` et consorts, que Scribe doit appeler.
- Les valeurs acceptées sont des identifiants simples : lettres, chiffres et
  tirets bas, ne commençant pas par un chiffre. Laissez vide pour continuer à
  utiliser le schéma de la connexion — y compris celui que vous avez défini
  vous-même avec `?options=-csearch_path%3Dmonschema` dans l'URL, que Scribe
  suit sans le remplacer.

## Migration

### Mise à jour depuis Scribe 2.x

Scribe 3.0 a remplacé la table `states` par `states_raw` accompagnée d'une vue de
compatibilité, et a donné à `entities` une clé primaire numérique. La conversion
d'une ancienne base était assurée par les versions 3.x et a été **supprimée en
3.9**.

Si votre base contient encore une *table* `states` (et non une vue), une table
`states_legacy`, ou une table `entities` sans colonne `id`, Scribe s'arrête au
démarrage, n'enregistre rien et signale un problème dans Repairs — sans rien
renommer, créer ni supprimer. Installez **Scribe 3.8**, laissez Home Assistant
tourner jusqu'à ce que les logs annoncent la fin de la migration (une quinzaine
de minutes sur une grosse base), puis remettez à jour.

Les installations neuves et toute base créée par une version 3.x ne sont pas
concernées.

### Reprise de données depuis d'autres sources

Scribe fournit des scripts pour reprendre des données depuis diverses sources.

### Migration InfluxDB

<details>
<summary><b>Afficher le guide de migration InfluxDB</b></summary>

1. Placez-vous dans le répertoire `migration` :
   ```bash
   cd migration
   ```

2. Installez les dépendances :
   ```bash
   pip install influxdb-client psycopg2-binary python-dotenv
   ```

3. Configurez la migration :
   ```bash
   cp .env.example .env
   nano .env
   # Renseignez [InfluxDB Configuration], [Scribe Configuration] et [Migration Settings]
   ```

4. Lancez la migration :
   ```bash
   python3 influx2scribe.py
   ```
</details>

### Migration LTSS

<details>
<summary><b>Afficher le guide de migration LTSS</b></summary>

1. Placez-vous dans le répertoire `migration` :
   ```bash
   cd migration
   ```

2. Installez les dépendances :
   ```bash
   pip install psycopg2-binary python-dotenv
   ```

3. Configurez la migration :
   ```bash
   cp .env.example .env
   nano .env
   # Renseignez [LTSS Configuration], [Scribe Configuration] et [Migration Settings]
   ```

4. Lancez la migration :
   ```bash
   python3 ltss2scribe.py
   ```
</details>

### Migration Recorder

<details>
<summary><b>Afficher le guide de migration Recorder</b></summary>

1. Placez-vous dans le répertoire `migration` :
   ```bash
   cd migration
   ```

2. Installez les dépendances :
   ```bash
   pip install psycopg2-binary python-dotenv
   ```

3. Configurez la migration :
   ```bash
   cp .env.example .env
   nano .env
   # Renseignez [Recorder Configuration], [Scribe Configuration] et [Migration Settings]
   ```

4. Lancez la migration :
   ```bash
   python3 recorder2scribe.py
   ```
</details>

## Capteurs de statistiques

Activez les capteurs en positionnant leurs options dans votre configuration.

### Statistiques d'écriture (`enable_stats_io: true`)

<details>
<summary><b>Afficher les capteurs d'écriture</b></summary>

Mesures en temps réel issues de l'écrivain (aucune requête en base).

| Capteur | Description |
| :--- | :--- |
| <img src="https://api.iconify.design/mdi:database-plus.svg?color=%232196F3" width="15" /> `sensor.scribe_states_written` | Nombre total de changements d'état écrits en base. |
| <img src="https://api.iconify.design/mdi:database-plus.svg?color=%232196F3" width="15" /> `sensor.scribe_events_written` | Nombre total d'événements écrits en base. |
| <img src="https://api.iconify.design/mdi:buffer.svg?color=%232196F3" width="15" /> `sensor.scribe_buffer_size` | Nombre d'éléments actuellement en attente dans le tampon mémoire. |
| <img src="https://api.iconify.design/mdi:timer-sand.svg?color=%232196F3" width="15" /> `sensor.scribe_write_duration` | Durée (en ms) de la dernière écriture en base. |
| <img src="https://api.iconify.design/mdi:speedometer.svg?color=%232196F3" width="15" /> `sensor.scribe_states_rate` | Débit d'états écrits en base (par minute). |
| <img src="https://api.iconify.design/mdi:speedometer.svg?color=%232196F3" width="15" /> `sensor.scribe_events_rate` | Débit d'événements écrits en base (par minute). |
</details>

### Statistiques de chunks (`enable_stats_chunk: true`)

<details>
<summary><b>Afficher les capteurs de chunks</b></summary>

Nombre de chunks (mis à jour toutes les `stats_chunk_interval` minutes).

| Capteur | Description |
| :--- | :--- |
| <img src="https://api.iconify.design/mdi:cube-outline.svg?color=%232196F3" width="15" /> `sensor.scribe_states_total_chunks` | Nombre total de chunks de la table des états. |
| <img src="https://api.iconify.design/mdi:package-down.svg?color=%232196F3" width="15" /> `sensor.scribe_states_compressed_chunks` | Nombre de chunks déjà compressés. |
| <img src="https://api.iconify.design/mdi:package-up.svg?color=%232196F3" width="15" /> `sensor.scribe_states_uncompressed_chunks` | Nombre de chunks en attente de compression. |
| <img src="https://api.iconify.design/mdi:cube-outline.svg?color=%232196F3" width="15" /> `sensor.scribe_events_total_chunks` | Nombre total de chunks de la table des événements. |
| <img src="https://api.iconify.design/mdi:package-down.svg?color=%232196F3" width="15" /> `sensor.scribe_events_compressed_chunks` | Nombre de chunks d'événements compressés. |
| <img src="https://api.iconify.design/mdi:package-up.svg?color=%232196F3" width="15" /> `sensor.scribe_events_uncompressed_chunks` | Nombre de chunks d'événements non compressés. |
</details>

### Statistiques de taille (`enable_stats_size: true`)

<details>
<summary><b>Afficher les capteurs de taille</b></summary>

Espace occupé, en octets (mis à jour toutes les `stats_size_interval` minutes).

| Capteur | Description |
| :--- | :--- |
| <img src="https://api.iconify.design/mdi:database.svg?color=%232196F3" width="15" /> `sensor.scribe_states_total_size` | Taille totale sur disque (données compressées + chunks récents + index). |
| <img src="https://api.iconify.design/mdi:database-search.svg?color=%232196F3" width="15" /> `sensor.scribe_states_original_size` | **Taille théorique** si les données n'étaient pas compressées (ex. 11 Go). |
| <img src="https://api.iconify.design/mdi:package-variant.svg?color=%232196F3" width="15" /> `sensor.scribe_states_compressed_size` | Taille physique des chunks de données compressés. |
| <img src="https://api.iconify.design/mdi:package-variant-closed.svg?color=%232196F3" width="15" /> `sensor.scribe_states_uncompressed_size` | Taille des données récentes pas encore compressées (ou index en attente). |
| <img src="https://api.iconify.design/mdi:percent.svg?color=%232196F3" width="15" /> `sensor.scribe_states_compression_ratio` | Taux de compression des états (%). |
| <img src="https://api.iconify.design/mdi:database.svg?color=%232196F3" width="15" /> `sensor.scribe_events_total_size` | Taille totale sur disque de la table des événements. |
| <img src="https://api.iconify.design/mdi:database-search.svg?color=%232196F3" width="15" /> `sensor.scribe_events_original_size` | Taille théorique des événements avant compression. |
| <img src="https://api.iconify.design/mdi:package-variant.svg?color=%232196F3" width="15" /> `sensor.scribe_events_compressed_size` | Taille des données d'événements compressées. |
| <img src="https://api.iconify.design/mdi:package-variant-closed.svg?color=%232196F3" width="15" /> `sensor.scribe_events_uncompressed_size` | Taille des données d'événements non compressées. |
| <img src="https://api.iconify.design/mdi:percent.svg?color=%232196F3" width="15" /> `sensor.scribe_events_compression_ratio` | Taux de compression des événements (%). |
</details>

## Services

### `scribe.flush`
Force l'écriture immédiate en base des données en tampon.

```yaml
service: scribe.flush
```

### `scribe.query`
Exécute une requête SQL en lecture seule sur la base TimescaleDB.

**Paramètres :**
- `sql` (obligatoire) : la requête SQL à exécuter. Ce doit être un `SELECT`.

**Retour :**
Une liste de lignes, chaque ligne étant un dictionnaire nom de colonne → valeur.

**Exemple :**
```yaml
service: scribe.query
data:
  sql: "SELECT * FROM states ORDER BY time DESC LIMIT 5"
response_variable: query_result
```

## Dépannage

### À regarder en premier

Deux endroits répondent à « pourquoi rien n'est enregistré ? » sans lire une seule ligne de log :

- **Paramètres → Appareils et services → Scribe → ⋮ → Télécharger les diagnostics** indique
  ce que fait réellement l'écrivain : connecté ou non, TimescaleDB trouvé ou non, combien
  d'éléments attendent dans le tampon et combien ont été écartés, les échecs d'écriture
  consécutifs, les réglages de stockage et de rétention en vigueur. L'URL de la base n'y
  figure jamais, et les erreurs du pilote sont expurgées de toute chaîne de connexion.
- **Paramètres → Système → Réparations** liste les problèmes ci-dessous, et
  **Paramètres → Système → État du système** indique vers quelle base Scribe pointe et
  s'il y est connecté à l'instant.

### Repairs

Scribe signale les problèmes qu'il ne peut pas résoudre seul dans
**Paramètres → Système → Réparations**, pour vous éviter de surveiller les logs.
Chacun disparaît de lui-même une fois la cause corrigée.

| Problème | Ce que cela signifie |
| --- | --- |
| Base de données injoignable | La connexion a échoué. Scribe continue de mettre en tampon et réessaie en arrière-plan : l'historique enregistré pendant la panne est écrit dès que la base revient. Vérifiez que le serveur tourne et que l'URL et les identifiants sont corrects. |
| Écriture impossible en base | Plusieurs écritures consécutives ont échoué. Les données sont gardées en mémoire et écrites au rétablissement — sauf si Home Assistant redémarre avant. |
| Tampon plein | Les écritures ont échoué assez longtemps pour saturer le tampon ; les enregistrements les plus anciens sont maintenant écartés. Réparez la base, ou augmentez `max_queue_size`. |
| Enregistrements écartés | Une écriture a échoué alors que la mise en tampon est désactivée : les enregistrements ont été perdus immédiatement. Activez la mise en tampon pour survivre aux coupures brèves. |
| Impossible de créer ses tables | Scribe a joint la base mais n'a pas pu construire son schéma, généralement un problème de droits. Sur une base neuve, rien n'est enregistré du tout. |
| Impossible d'atteindre le schéma indiqué | Le schéma de `db_schema` n'existe pas et n'a pas pu être créé, ou l'utilisateur de la base n'y a aucun droit. Rien n'est enregistré — plutôt que de remplir `public` en silence. |
| Impossible de créer la vue `states` | L'historique est enregistré, mais la vue par laquelle passent toutes les requêtes est absente — l'historique paraît vide alors que rien n'est perdu. |
| `states_raw` / `events` n'est pas une hypertable | TimescaleDB est installé mais la table n'a jamais été convertie (cas fréquent quand l'extension est ajoutée *après* le remplissage des tables). Chunks, compression et rétention ne font plus rien. |
| `states_raw` / `events` n'est jamais compressée | La table est bien une hypertable mais n'a aucune politique de compression : elle conserve sa taille non compressée. |
| TLS partiellement appliqué | Scribe se connecte en TLS, mais un certificat configuré n'a pas pu être appliqué — le plus souvent un certificat client : il s'authentifie alors comme un client ordinaire et non comme celui que vous aviez provisionné. |
| TimescaleDB n'est pas installé | L'historique est enregistré, mais le découpage et la compression sont indisponibles : la base grossit bien plus vite et les capteurs de taille restent vides. |
| Base antérieure à la version 3.0 | La base utilise encore le schéma pré-3.0, que cette version ne sait pas convertir. Rien n'est enregistré et rien n'a été modifié — installez Scribe 3.8 pour la convertir, puis remettez à jour. |
| Politique de rétention non appliquée | Vous avez demandé la suppression des données au-delà d'un intervalle et la politique n'a pas pu être créée. Rien n'a été supprimé et rien ne l'est — la table continue de grossir. |
| Renommage d'entité non appliqué | Un renommage est entré en collision avec une ligne existante en base. L'historique de l'entité est réparti sur deux identifiants. |

### Consommation mémoire élevée
- Réduisez `max_queue_size`
- Réduisez `flush_interval` pour écrire plus souvent
- Surveillez `sensor.scribe_buffer_size`

### Réglage des performances

Si la vue `states` est lente (plusieurs secondes par requête), c'est
généralement que le planificateur PostgreSQL choisit un **Hash Join** au lieu
d'un **Nested Loop**, ce qui empêche TimescaleDB d'élaguer efficacement les
chunks.

La cause la plus fréquente est un `random_page_cost` élevé (la valeur par défaut
est `4.0`, optimisée pour les disques durs). Avec du stockage moderne (SSD,
NVMe) ou une base bien mise en cache, abaissez cette valeur :

```sql
-- Voir la valeur actuelle
SHOW random_page_cost;

-- Abaisser la valeur (souvent 1.1)
ALTER SYSTEM SET random_page_cost = 1.1;
SELECT pg_reload_conf();
```

Une valeur plus basse encourage le planificateur à utiliser des jointures par
index (Nested Loops), essentielles aux performances de Scribe sur de gros
volumes.

### Toujours bloqué ?
[Ouvrez un ticket](https://github.com/jonathan-gtd/scribe/issues) sur GitHub avec vos logs et votre configuration. Je serai ravi de vous aider !

## Tableau de bord / Vue

Une mise en page Lovelace prête à l'emploi rassemblant tous les capteurs utiles
de Scribe (statistiques de base, taux de compression, performances d'écriture)
est disponible dans ce dépôt, en deux variantes :

| Fichier | Ce que c'est | Où le coller |
| --- | --- | --- |
| [`lovelace_scribe_card.yaml`](lovelace_scribe_card.yaml) | Une **carte unique** (`type: vertical-stack`) | L'éditeur YAML de carte (« Ajouter une carte » → « Manuel ») |
| [`lovelace_scribe_view.yaml`](lovelace_scribe_view.yaml) | Une **vue entière** (`title` / `icon` / `cards`) | L'éditeur YAML de vue |

> ⚠️ Les deux ne sont pas interchangeables. Coller le fichier de *vue* dans un
> éditeur de *carte* échoue avec **« Aucun type de carte configuré »**, car une
> configuration de carte doit commencer par une clé `type:`.

**Option A — l'ajouter comme carte (le plus simple, fonctionne dans tous les types de vue) :**

1.  Ouvrez votre tableau de bord et cliquez sur « Modifier le tableau de bord » (icône crayon).
2.  Cliquez sur **+ Ajouter une carte** et descendez tout en bas du sélecteur pour choisir **Manuel**.
3.  Copiez le contenu de [`lovelace_scribe_card.yaml`](lovelace_scribe_card.yaml), remplacez tout ce qui se trouve dans l'éditeur, puis cliquez sur **Enregistrer**.

**Option B — l'ajouter comme vue dédiée :**

1.  Ouvrez votre tableau de bord et cliquez sur « Modifier le tableau de bord » (icône crayon).
2.  Cliquez sur le bouton **+** *dans la barre d'onglets du haut* (à côté du nom de vos vues) pour ajouter une vue — et non sur le bouton « Ajouter une carte ».
3.  Dans la boîte de dialogue de la vue, ouvrez le menu ⋮ (ou le bouton « Afficher l'éditeur de code ») et choisissez **Modifier en YAML**.
4.  Copiez le contenu de [`lovelace_scribe_view.yaml`](lovelace_scribe_view.yaml), remplacez tout ce qui se trouve dans l'éditeur, puis cliquez sur **Enregistrer**.

## Écosystème / Projets liés

Ces projets fonctionnent très bien avec Scribe :

- [timescale_database_reader](https://github.com/remmob/timescale_database_reader) : un composant personnalisé pour relire les données de TimescaleDB dans des capteurs Home Assistant.
- [timescale-plotly-card](https://github.com/remmob/timescale-plotly-card) : une carte Plotly très personnalisable, capable d'interroger TimescaleDB directement.

## Licence

Licence MIT — voir le fichier LICENSE pour les détails

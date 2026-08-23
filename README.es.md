[🇬🇧 English](README.md) · [🇫🇷 Français](README.fr.md) · **🇪🇸 Español** · [🇩🇪 Deutsch](README.de.md)

# Scribe — Integración TimescaleDB de alto rendimiento para Home Assistant

Scribe es un componente de nueva generación que escribe los estados y eventos de Home Assistant en una base de datos TimescaleDB.

**¿Por qué Scribe?**
Scribe está construido de otra manera. A diferencia de las integraciones que dependen de controladores síncronos o del recorder por defecto, Scribe usa **`asyncpg`**, un controlador PostgreSQL asíncrono de alto rendimiento. Esto le permite gestionar volúmenes enormes de datos sin bloquear el bucle de eventos de Home Assistant. Está diseñado para la estabilidad, la velocidad y la eficiencia.

**Estructura de datos y consultas**

Aquí encontrarás una explicación de la estructura de datos y de cómo consultarla: [Estructura de datos](datastructre.md)

## Índice

- [Características](#características)
- [Instalación](#instalación)
- [Configuración](#configuración)
- [Ajuste del almacenamiento](#ajuste-del-almacenamiento)
- [Retención](#retención)
- [Migración](#migración)
- [Sensores de estadísticas](#sensores-de-estadísticas)
- [Servicios](#servicios)
- [Panel / Vista](#panel--vista)
- [Ecosistema / Proyectos relacionados](#ecosistema--proyectos-relacionados)
- [Solución de problemas](#solución-de-problemas)
- [Licencia](#licencia)

## Características

- 🚀 **Arquitectura asíncrona ante todo**: construida sobre `asyncpg` para escrituras no bloqueantes y de alto rendimiento.
- 📦 **TimescaleDB nativo**: gestiona automáticamente las hypertables y las políticas de compresión.
- 📊 **Estadísticas detalladas**: sensores opcionales para vigilar el número de chunks, los ratios de compresión (¡hasta un 97 % de ahorro!) y el rendimiento de escritura.
- 🔒 **Seguro**: compatibilidad completa con SSL/TLS.
- 📈 **Estados y eventos**: registra todos los cambios de estado y los eventos en las tablas `states` y `events`.
- 👥 **Contexto de usuario**: sincroniza automáticamente los usuarios de Home Assistant en la base de datos para dar más contexto.
- 🧩 **Metadatos de entidades**: sincroniza automáticamente el registro de entidades (nombres, plataformas, etc.) en la tabla `entities`.
- 🏠 **Contexto de áreas y dispositivos**: sincroniza automáticamente áreas y dispositivos en las tablas `areas` y `devices`.
- 🔌 **Información de integraciones**: sincroniza automáticamente las entradas de configuración en la tabla `integrations`.
- 🎯 **Filtrado preciso**: incluir o excluir por dominio, entidad, patrón de entidad o atributo.
- ✅ **Probado contra una base de datos real**: ~90 % de cobertura de líneas, y una suite de extremo a extremo que ejecuta la integración contra una TimescaleDB real en lugar de mocks.

## Instalación

### 1. Instalar el componente

**HACS (recomendado)**

[![Abre tu instancia de Home Assistant y abre un repositorio en la Home Assistant Community Store.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=jonathan-gtd&repository=scribe&category=integration)

1. Añade este repositorio como repositorio personalizado en HACS.
2. Busca «Scribe» e instálalo.
3. Reinicia Home Assistant.

**Manual**
1. Copia la carpeta `custom_components/scribe` en el directorio `custom_components` de tu Home Assistant.
2. Reinicia Home Assistant.

### 2. Preparar la base de datos

Necesitas una instancia de TimescaleDB en funcionamiento. Recomiendo PostgreSQL 17 o 18.

> [!IMPORTANT]
> **La extensión TimescaleDB es obligatoria.** La división en chunks, la
> compresión, la retención y los sensores de tamaño son la razón de ser de
> Scribe, y ninguno existe en PostgreSQL a secas. Una instalación nueva se
> rechaza si falta la extensión, aunque Scribe la activa por ti cuando el
> servidor la tiene disponible y tu usuario de la base de datos posee `CREATE`
> sobre ella, que es lo que concede la preparación de abajo. Las instalaciones
> que ya funcionan sin ella siguen registrando y se les indica lo que les falta
> mediante un problema en Reparaciones.

#### Opción A: Home Assistant OS (complemento)

Si usas Home Assistant OS, recomiendo el [complemento TimescaleDB](https://github.com/expaso/hassos-addon-timescaledb).

[![Abre tu instancia de Home Assistant y muestra el diálogo de añadir repositorio de complementos con una URL ya rellenada.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fexpaso%2Fhassos-addon-timescaledb)

#### Opción B: Docker (manual)

```bash
# Alta disponibilidad (recomendado)
docker run -d --name timescaledb -p 5432:5432 -e POSTGRES_PASSWORD=password timescale/timescaledb-ha:pg18

# Estándar
docker run -d --name timescaledb -p 5432:5432 -e POSTGRES_PASSWORD=password timescale/timescaledb:pg18
```

Crea la base de datos y el usuario:

```sql
CREATE DATABASE scribe;
CREATE USER scribe WITH PASSWORD 'password';
GRANT ALL PRIVILEGES ON DATABASE scribe TO scribe;

\c scribe
CREATE EXTENSION IF NOT EXISTS timescaledb;
GRANT ALL ON SCHEMA public TO scribe;
```

## Configuración

### Configuración mínima

```yaml
scribe:
  db_url: postgresql://scribe:password@192.168.1.10:5432/scribe
```

### Configuración completa (valores por defecto)

<details>
<summary><b>Mostrar la configuración YAML completa</b></summary>

```yaml
scribe:
  db_url: postgresql://scribe:password@192.168.1.10:5432/scribe
  db_ssl: false
  chunk_time_interval: "7 days"
  compress_after: "7 days"
  retention_states: ""   # vacío = conservar para siempre
  retention_events: ""   # vacío = conservar para siempre
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
  # Opcional: desactivar tablas de metadatos concretas (por defecto: true)
  enable_table_areas: true
  enable_table_devices: true
  enable_table_integrations: true
  enable_table_users: true
```
</details>

### Parámetros de configuración

<details>
<summary><b>Mostrar la referencia de parámetros</b></summary>

| Parámetro | Descripción |
| :--- | :--- |
| `db_url` | **Obligatorio.** Cadena de conexión a tu base de datos TimescaleDB. |
| `db_ssl` | Activar SSL/TLS para la conexión a la base de datos. |
| `chunk_time_interval` | Cuánto tiempo abarca cada chunk de la tabla. Ver [Ajuste del almacenamiento](#ajuste-del-almacenamiento). |
| `compress_after` | Los chunks más antiguos que este intervalo se comprimen. Ver [Ajuste del almacenamiento](#ajuste-del-almacenamiento). |
| `retention_states` | **Elimina** el historial de estados más antiguo que este intervalo (p. ej. `"365 days"`). Vacío (por defecto) conserva todo. Ver [Retención](#retención). |
| `retention_events` | **Elimina** el historial de eventos más antiguo que este intervalo. Vacío (por defecto) conserva todo. Ver [Retención](#retención). |
| `record_states` | Registrar o no los cambios de estado. |
| `record_events` | Registrar o no los eventos. |
| `batch_size` | Número de elementos que se acumulan antes de escribir en la base de datos. |
| `flush_interval` | Tiempo máximo (en segundos) antes de vaciar el búfer. |
| `max_queue_size` | Número máximo de elementos en memoria antes de descartar los nuevos. |
| `buffer_on_failure` | Si es verdadero, mantiene los datos en memoria cuando la base de datos no responde (hasta `max_queue_size`). |
| `enable_stats_io` | Activar los sensores de rendimiento del escritor en tiempo real (sin consultas a la base). |
| `enable_stats_chunk` | Activar los sensores de número de chunks (consultan la base). |
| `enable_stats_size` | Activar los sensores de tamaño de almacenamiento (consultan la base). |
| `stats_chunk_interval` | Intervalo (en minutos) de actualización de las estadísticas de chunks. |
| `stats_size_interval` | Intervalo (en minutos) de actualización de las estadísticas de tamaño. |
| `include_domains` | Lista de dominios a incluir. |
| `include_entities` | Lista de entidades concretas a incluir. |
| `include_entity_globs` | Lista de patrones de entidad a incluir (p. ej. `sensor.weather_*`). |
| `exclude_domains` | Lista de dominios a excluir. |
| `exclude_entities` | Lista de entidades concretas a excluir. |
| `exclude_entity_globs` | Lista de patrones de entidad a excluir (p. ej. `switch.kitchen_*`). |
| `exclude_attributes` | Lista de atributos a excluir de la columna `attributes`. |
| `include_events` | Lista de tipos de evento a registrar. Déjala vacía para registrarlos todos. |
| `exclude_events` | Lista de tipos de evento que nunca se registran (se aplica después de `include_events`). |
| `enable_table_areas` | Activar la creación y sincronización de la tabla `areas`. |
| `enable_table_devices` | Activar la creación y sincronización de la tabla `devices`. |
| `enable_table_integrations` | Activar la creación y sincronización de la tabla `integrations`. |
| `enable_table_users` | Activar la creación y sincronización de la tabla `users`. |
</details>

## Ajuste del almacenamiento

Scribe guarda el historial en **hypertables** de TimescaleDB: una tabla que se
usa y se consulta como cualquier otra, pero que está físicamente dividida en
**chunks**, cada uno cubriendo un tramo de tiempo. Casi todo lo relativo al
espacio en disco y a la velocidad de las consultas viene de esa división: una
consulta sobre la semana pasada solo lee los chunks que la solapan, la
compresión trabaja chunk a chunk, y la [retención](#retención) elimina chunks
enteros en lugar de filas sueltas.

Lo controlan dos ajustes, tanto en YAML como en la interfaz, en
**Configurar → Avanzado (TimescaleDB y SSL)**:

### `chunk_time_interval` (por defecto `7 days`)

Cuánto tiempo abarca un chunk.

- **Chunks más pequeños** (p. ej. `1 day`): más archivos y más pequeños — una
  retención más fina, y las consultas sobre ventanas recientes tocan menos
  datos. Pasado cierto punto, una consulta de varios meses tiene que abrir
  cientos de chunks.
- **Chunks más grandes** (p. ej. `30 days`): menos archivos y más grandes —
  mejor para consultas históricas largas, peor para la memoria. La propia
  recomendación de TimescaleDB es que los chunks en los que escribes quepan
  holgadamente en memoria junto con sus índices, así que un chunk sobredimensionado
  en una máquina pequeña perjudica las escrituras.

El valor por defecto es adecuado para una instancia típica de Home Assistant.
Plantéate `1 day` si registras miles de entidades, y solo entonces.

> **El cambio solo afecta a los chunks nuevos.** Los ya escritos conservan el
> tramo con el que se crearon, y nada se reescribe ni se mueve: simplemente
> tendrás una mezcla de tramos antiguos y nuevos, algo que TimescaleDB gestiona
> de forma nativa.

### `compress_after` (por defecto `7 days`)

Qué antigüedad debe tener un chunk para que TimescaleDB lo comprima. Con este
tipo de datos (muchos `entity_id` repetidos y valores que cambian despacio) la
compresión suele reducir mucho el tamaño, por eso está activada por defecto.

Los chunks comprimidos siguen siendo perfectamente consultables — a la vista
`states` le da igual. Escribir *dentro* de uno sí es más lento, y por eso la
compresión solo entra en juego cuando el chunk ya es lo bastante antiguo como
para darse por terminado. Mantén `compress_after` holgadamente por encima de la
antigüedad de los datos que aún escribes: los estados que llegan desordenados
(un relleno retroactivo, un script de migración) aterrizan en chunks antiguos.

> **El cambio surte efecto en el siguiente reinicio**, y los chunks ya
> comprimidos siguen comprimidos: el ajuste solo decide cuándo se comprimirán
> los *siguientes*.

### Cómo encajan los tres ajustes

| Ajuste | Qué hace | Reversible |
| :--- | :--- | :--- |
| `chunk_time_interval` | Cuánto tiempo abarca un chunk | Sí — solo chunks futuros |
| `compress_after` | Cuándo se comprime un chunk | Sí |
| `retention_states` / `retention_events` | Cuándo se **elimina** un chunk | **No** |

Se aplican en ese orden al mismo chunk a lo largo de su vida: escrito →
comprimido → eliminado. Dos consecuencias que conviene conocer:

- Si `compress_after` es mayor que tu retención, los chunks se eliminan antes de
  haberse comprimido nunca, y la compresión no hace nada.
- La retención elimina chunks enteros, así que tu ventana real es el intervalo
  que fijas **más** hasta un `chunk_time_interval`. Chunks más pequeños la
  ajustan mejor.

Si los sensores de tamaño y de chunks están activados (`enable_stats_size`,
`enable_stats_chunk`), informan exactamente de lo que producen estos ajustes:
número de chunks, tamaños comprimido y sin comprimir, y ratio de compresión.

## Retención

Por defecto, Scribe lo conserva todo, para siempre. Si solo quieres almacenar
una ventana acotada —porque agregas el historial en bruto en otro sitio, o
simplemente para limitar el disco—, fija un intervalo de retención y TimescaleDB
eliminará los chunks más antiguos:

```yaml
scribe:
  db_url: postgresql://scribe:password@192.168.1.10:5432/scribe
  retention_states: "365 days"
  retention_events: "30 days"
```

Ambos están también en la interfaz, en **Configurar → Avanzado (TimescaleDB y SSL)**.

> [!WARNING]
> La retención **elimina datos de forma permanente**. No hay deshacer ni
> papelera: en cuanto un chunk queda fuera de la ventana se elimina, y solo una
> copia de seguridad puede recuperarlo. Estados y eventos se configuran por
> separado, así puedes caducar eventos ruidosos conservando el historial de
> estados.

Conviene saber:

- **Ningún ajuste significa siempre «conservar para siempre».** Vaciar el campo
  en la interfaz y borrar la línea de `configuration.yaml` eliminan ambos la
  política: a un valor importado en su día desde el YAML nunca se le permite
  sobrevivir a la línea que lo definió.
- **Scribe es dueño de la política de retención en sus propias tablas.** Vaciar
  el campo la elimina —incluida una que hayas creado a mano con
  `add_retention_policy()`—, que es la única forma de que vaciar el ajuste en la
  interfaz detenga realmente las eliminaciones.
- **Empieza de inmediato.** TimescaleDB ejecuta la política a los pocos segundos
  de crearla, no en el siguiente intervalo diario: todo lo que queda fuera de la
  ventana desaparece en la primera ejecución, justo tras el reinicio que la
  activó.
- **La eliminación es por chunk, no por fila.** Un chunk solo se elimina cuando
  *todas* sus filas son más antiguas que el intervalo, así que con el
  `chunk_time_interval` por defecto de 7 días conservas hasta una semana más de
  lo que pediste. Eso es lo que hace la retención casi gratuita: elimina
  archivos en lugar de borrar filas.
- **Solo se elimina el historial.** La tabla `entities` y las demás tablas de
  metadatos no se tocan, así que una entidad cuyo historial ha caducado por
  completo sigue resolviéndose.
- **TimescaleDB es imprescindible**: es la extensión que ejecuta la política. En
  PostgreSQL sin ella, fijar un intervalo de retención genera un problema en
  Reparaciones en lugar de no hacer nada en silencio.
- Los valores aceptados son intervalos simples: `30 days`, `6 months`,
  `1 year`. Cualquier otra cosa se rechaza con un error en vez de enviarse a la
  base de datos.

## Migración

### Actualizar desde Scribe 2.x

Scribe 3.0 sustituyó la tabla `states` por `states_raw` más una vista de
compatibilidad, y dio a `entities` una clave primaria numérica. La conversión de
una base antigua la hacían las versiones 3.x y se **eliminó en la 3.9**.

Si tu base de datos todavía tiene una *tabla* `states` (en lugar de una vista),
una tabla `states_legacy`, o una tabla `entities` sin columna `id`, Scribe se
detiene al arrancar, no registra nada y genera un problema en Reparaciones, sin
renombrar, crear ni eliminar nada. Instala **Scribe 3.8**, deja Home Assistant
en marcha hasta que los registros indiquen que la migración ha terminado (unos
quince minutos en una base grande) y actualiza de nuevo.

Las instalaciones nuevas y cualquier base creada por una versión 3.x no se ven
afectadas.

### Importar datos desde otras fuentes

Scribe incluye scripts para importar datos desde varias fuentes.

### Migración desde InfluxDB

<details>
<summary><b>Mostrar la guía de migración de InfluxDB</b></summary>

1. Sitúate en el directorio `migration`:
   ```bash
   cd migration
   ```

2. Instala las dependencias:
   ```bash
   pip install influxdb-client psycopg2-binary python-dotenv
   ```

3. Configura la migración:
   ```bash
   cp .env.example .env
   nano .env
   # Rellena [InfluxDB Configuration], [Scribe Configuration] y [Migration Settings]
   ```

4. Ejecuta la migración:
   ```bash
   python3 influx2scribe.py
   ```
</details>

### Migración desde LTSS

<details>
<summary><b>Mostrar la guía de migración de LTSS</b></summary>

1. Sitúate en el directorio `migration`:
   ```bash
   cd migration
   ```

2. Instala las dependencias:
   ```bash
   pip install psycopg2-binary python-dotenv
   ```

3. Configura la migración:
   ```bash
   cp .env.example .env
   nano .env
   # Rellena [LTSS Configuration], [Scribe Configuration] y [Migration Settings]
   ```

4. Ejecuta la migración:
   ```bash
   python3 ltss2scribe.py
   ```
</details>

### Migración desde Recorder

<details>
<summary><b>Mostrar la guía de migración de Recorder</b></summary>

1. Sitúate en el directorio `migration`:
   ```bash
   cd migration
   ```

2. Instala las dependencias:
   ```bash
   pip install psycopg2-binary python-dotenv
   ```

3. Configura la migración:
   ```bash
   cp .env.example .env
   nano .env
   # Rellena [Recorder Configuration], [Scribe Configuration] y [Migration Settings]
   ```

4. Ejecuta la migración:
   ```bash
   python3 recorder2scribe.py
   ```
</details>

## Sensores de estadísticas

Activa los sensores estableciendo sus opciones en la configuración.

### Estadísticas de escritura (`enable_stats_io: true`)

<details>
<summary><b>Mostrar los sensores de escritura</b></summary>

Métricas en tiempo real del escritor (sin consultas a la base de datos).

| Sensor | Descripción |
| :--- | :--- |
| <img src="https://api.iconify.design/mdi:database-plus.svg?color=%232196F3" width="15" /> `sensor.scribe_states_written` | Número total de cambios de estado escritos en la base. |
| <img src="https://api.iconify.design/mdi:database-plus.svg?color=%232196F3" width="15" /> `sensor.scribe_events_written` | Número total de eventos escritos en la base. |
| <img src="https://api.iconify.design/mdi:buffer.svg?color=%232196F3" width="15" /> `sensor.scribe_buffer_size` | Elementos que esperan actualmente en el búfer de memoria. |
| <img src="https://api.iconify.design/mdi:timer-sand.svg?color=%232196F3" width="15" /> `sensor.scribe_write_duration` | Tiempo (en ms) de la última escritura en la base. |
| <img src="https://api.iconify.design/mdi:speedometer.svg?color=%232196F3" width="15" /> `sensor.scribe_states_rate` | Ritmo de estados escritos (por minuto). |
| <img src="https://api.iconify.design/mdi:speedometer.svg?color=%232196F3" width="15" /> `sensor.scribe_events_rate` | Ritmo de eventos escritos (por minuto). |
</details>

### Estadísticas de chunks (`enable_stats_chunk: true`)

<details>
<summary><b>Mostrar los sensores de chunks</b></summary>

Número de chunks (actualizado cada `stats_chunk_interval` minutos).

| Sensor | Descripción |
| :--- | :--- |
| <img src="https://api.iconify.design/mdi:cube-outline.svg?color=%232196F3" width="15" /> `sensor.scribe_states_total_chunks` | Número total de chunks de la tabla de estados. |
| <img src="https://api.iconify.design/mdi:package-down.svg?color=%232196F3" width="15" /> `sensor.scribe_states_compressed_chunks` | Número de chunks ya comprimidos. |
| <img src="https://api.iconify.design/mdi:package-up.svg?color=%232196F3" width="15" /> `sensor.scribe_states_uncompressed_chunks` | Número de chunks pendientes de comprimir. |
| <img src="https://api.iconify.design/mdi:cube-outline.svg?color=%232196F3" width="15" /> `sensor.scribe_events_total_chunks` | Número total de chunks de la tabla de eventos. |
| <img src="https://api.iconify.design/mdi:package-down.svg?color=%232196F3" width="15" /> `sensor.scribe_events_compressed_chunks` | Número de chunks de eventos comprimidos. |
| <img src="https://api.iconify.design/mdi:package-up.svg?color=%232196F3" width="15" /> `sensor.scribe_events_uncompressed_chunks` | Número de chunks de eventos sin comprimir. |
</details>

### Estadísticas de tamaño (`enable_stats_size: true`)

<details>
<summary><b>Mostrar los sensores de tamaño</b></summary>

Espacio ocupado en bytes (actualizado cada `stats_size_interval` minutos).

| Sensor | Descripción |
| :--- | :--- |
| <img src="https://api.iconify.design/mdi:database.svg?color=%232196F3" width="15" /> `sensor.scribe_states_total_size` | Tamaño total en disco (datos comprimidos + chunks recientes + índices). |
| <img src="https://api.iconify.design/mdi:database-search.svg?color=%232196F3" width="15" /> `sensor.scribe_states_original_size` | **Tamaño teórico** si los datos no estuvieran comprimidos (p. ej. 11 GB). |
| <img src="https://api.iconify.design/mdi:package-variant.svg?color=%232196F3" width="15" /> `sensor.scribe_states_compressed_size` | Tamaño físico de los chunks de datos comprimidos. |
| <img src="https://api.iconify.design/mdi:package-variant-closed.svg?color=%232196F3" width="15" /> `sensor.scribe_states_uncompressed_size` | Tamaño de los datos recientes aún sin comprimir (o índices pendientes). |
| <img src="https://api.iconify.design/mdi:percent.svg?color=%232196F3" width="15" /> `sensor.scribe_states_compression_ratio` | Ratio de compresión de los estados (%). |
| <img src="https://api.iconify.design/mdi:database.svg?color=%232196F3" width="15" /> `sensor.scribe_events_total_size` | Tamaño total en disco de la tabla de eventos. |
| <img src="https://api.iconify.design/mdi:database-search.svg?color=%232196F3" width="15" /> `sensor.scribe_events_original_size` | Tamaño teórico de los eventos antes de comprimir. |
| <img src="https://api.iconify.design/mdi:package-variant.svg?color=%232196F3" width="15" /> `sensor.scribe_events_compressed_size` | Tamaño de los datos de eventos comprimidos. |
| <img src="https://api.iconify.design/mdi:package-variant-closed.svg?color=%232196F3" width="15" /> `sensor.scribe_events_uncompressed_size` | Tamaño de los datos de eventos sin comprimir. |
| <img src="https://api.iconify.design/mdi:percent.svg?color=%232196F3" width="15" /> `sensor.scribe_events_compression_ratio` | Ratio de compresión de los eventos (%). |
</details>

## Servicios

### `scribe.flush`
Fuerza el volcado inmediato a la base de datos de los datos en búfer.

```yaml
service: scribe.flush
```

### `scribe.query`
Ejecuta una consulta SQL de solo lectura contra la base de datos TimescaleDB.

**Parámetros:**
- `sql` (obligatorio): la consulta SQL a ejecutar. Debe ser una sentencia `SELECT`.

**Devuelve:**
Una lista de filas, donde cada fila es un diccionario de nombres de columna y valores.

**Ejemplo:**
```yaml
service: scribe.query
data:
  sql: "SELECT * FROM states ORDER BY time DESC LIMIT 5"
response_variable: query_result
```

## Solución de problemas

### Reparaciones

Scribe informa de los problemas que no puede resolver por sí mismo en
**Ajustes → Sistema → Reparaciones**, para que no tengas que vigilar los
registros. Cada uno desaparece solo en cuanto se corrige la causa.

| Reparación | Qué significa |
| --- | --- |
| No puede acceder a su base de datos | La conexión falló al arrancar: **no se está registrando nada**. Comprueba que el servidor esté activo y que la URL y las credenciales sean correctas. |
| No puede escribir en su base de datos | Varias escrituras consecutivas fallaron. Los datos se mantienen en memoria y se escriben al recuperarse, salvo que Home Assistant se reinicie antes. |
| El búfer está lleno | Las escrituras fallaron el tiempo suficiente para saturar el búfer; los registros más antiguos se están descartando. Arregla la base de datos o sube `max_queue_size`. |
| Descartando registros | Una escritura falló con el búfer desactivado, así que los registros se perdieron de inmediato. Activa el búfer para sobrevivir a cortes breves. |
| No pudo crear sus tablas | Scribe llegó a la base de datos pero no pudo construir su esquema, casi siempre un problema de permisos. En una base nueva no se registra nada en absoluto. |
| No pudo crear la vista `states` | El historial se registra, pero falta la vista por la que pasan todas las consultas: el historial parece vacío aunque no se ha perdido nada. |
| `states_raw` / `events` no es una hypertable | TimescaleDB está instalado pero la tabla nunca se convirtió (algo habitual cuando la extensión se añade *después* de llenar las tablas). Los chunks, la compresión y la retención no hacen nada. |
| `states_raw` / `events` nunca se comprime | La tabla sí es una hypertable pero no tiene política de compresión, así que mantiene su tamaño sin comprimir. |
| TLS no aplicado por completo | Scribe conecta por TLS, pero un certificado configurado no pudo aplicarse — casi siempre un certificado de cliente: se autentica como un cliente cualquiera en lugar del que aprovisionaste. |
| TimescaleDB no está instalado | El historial se registra, pero la división en chunks y la compresión no están disponibles: la base crece mucho más rápido y los sensores de tamaño se quedan vacíos. |
| La base es anterior a la versión 3.0 | La base todavía usa el esquema anterior a 3.0, que esta versión no sabe convertir. No se registra nada y no se ha modificado nada: instala Scribe 3.8 para convertirla y vuelve a actualizar. |
| No se pudo aplicar la política de retención | Pediste eliminar los datos anteriores a un intervalo y la política no pudo crearse. No se ha eliminado nada y nada se está eliminando: la tabla sigue creciendo. |
| El cambio de nombre de entidad no se aplicó | Un cambio de nombre chocó con una fila ya existente en la base. El historial de la entidad queda repartido entre dos identificadores. |

### Consumo de memoria elevado
- Reduce `max_queue_size`
- Reduce `flush_interval` para escribir más a menudo
- Vigila `sensor.scribe_buffer_size`

### Ajuste del rendimiento

Si la vista `states` va lenta (varios segundos por consulta), suele deberse a
que el planificador de PostgreSQL elige un **Hash Join** en lugar de un **Nested
Loop**, lo que impide a TimescaleDB podar chunks de forma eficaz.

La causa más habitual es un `random_page_cost` alto (el valor por defecto es
`4.0`, pensado para discos duros). Con almacenamiento moderno (SSD, NVMe) o una
base bien cacheada, conviene bajarlo:

```sql
-- Ver el valor actual
SHOW random_page_cost;

-- Bajarlo (habitualmente 1.1)
ALTER SYSTEM SET random_page_cost = 1.1;
SELECT pg_reload_conf();
```

Un valor más bajo anima al planificador a usar uniones por índice (Nested
Loops), esenciales para el rendimiento de Scribe con grandes volúmenes.

### ¿Sigues con problemas?
[Abre una incidencia](https://github.com/jonathan-gtd/scribe/issues) en GitHub con tus registros y tu configuración. ¡Estaré encantado de ayudar!

## Panel / Vista

En este repositorio hay una disposición Lovelace lista para usar con todos los
sensores útiles de Scribe (estadísticas de la base, ratios de compresión,
rendimiento de escritura), en dos variantes:

| Archivo | Qué es | Dónde pegarlo |
| --- | --- | --- |
| [`lovelace_scribe_card.yaml`](lovelace_scribe_card.yaml) | Una **sola tarjeta** (`type: vertical-stack`) | El editor YAML de tarjeta («Añadir tarjeta» → «Manual») |
| [`lovelace_scribe_view.yaml`](lovelace_scribe_view.yaml) | Una **vista completa** (`title` / `icon` / `cards`) | El editor YAML de vista |

> ⚠️ No son intercambiables. Pegar el archivo de *vista* en un editor de
> *tarjeta* falla con **«No card type configured»**, porque la configuración de
> una tarjeta debe empezar por una clave `type:`.

**Opción A — añadirla como tarjeta (lo más fácil, funciona en cualquier tipo de vista):**

1.  Abre tu panel y pulsa «Editar panel» (icono del lápiz).
2.  Pulsa **+ Añadir tarjeta** y baja hasta el final del selector para elegir **Manual**.
3.  Copia el contenido de [`lovelace_scribe_card.yaml`](lovelace_scribe_card.yaml), sustituye todo lo que haya en el editor y pulsa **Guardar**.

**Opción B — añadirla como vista dedicada:**

1.  Abre tu panel y pulsa «Editar panel» (icono del lápiz).
2.  Pulsa el botón **+** *en la barra de pestañas superior* (junto a los nombres de tus vistas) para añadir una vista, no el botón «Añadir tarjeta».
3.  En el diálogo de la vista, abre el menú ⋮ (o el botón «Mostrar editor de código») y elige **Editar en YAML**.
4.  Copia el contenido de [`lovelace_scribe_view.yaml`](lovelace_scribe_view.yaml), sustituye todo lo que haya en el editor y pulsa **Guardar**.

## Ecosistema / Proyectos relacionados

Estos proyectos funcionan muy bien con Scribe:

- [timescale_database_reader](https://github.com/remmob/timescale_database_reader): un componente personalizado para volver a leer datos de TimescaleDB en sensores de Home Assistant.
- [timescale-plotly-card](https://github.com/remmob/timescale-plotly-card): una tarjeta basada en Plotly, muy personalizable, capaz de consultar TimescaleDB directamente.

## Licencia

Licencia MIT — consulta el archivo LICENSE para más detalles

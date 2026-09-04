"""The config flow: what a user is told before Scribe is ever set up.

Everything here is what stands between a mistyped URL — or a database that
cannot do the job — and an installation that looks fine while recording
nothing.
"""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.scribe.config_flow import _coerce_options
from custom_components.scribe.const import (
    CONF_DB_URL,
    CONF_EXCLUDE_DOMAINS,
    CONF_RECORD_EVENTS,
    CONF_RECORD_STATES,
    DOMAIN,
)

GOOD_URL = "postgresql://scribe:secret@127.0.0.1:5432/scribe"


def _check_database(result):
    """Answer the flow's database check without touching a database."""
    return patch(
        "custom_components.scribe.config_flow._check_database",
        new_callable=AsyncMock,
        return_value=result,
    )


async def _start_user_flow(hass):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})


@pytest.mark.asyncio
async def test_a_usable_database_creates_the_entry(hass):
    result = await _start_user_flow(hass)
    assert result["type"] is FlowResultType.FORM

    with _check_database(None):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DB_URL: GOOD_URL}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DB_URL] == GOOD_URL


@pytest.mark.asyncio
async def test_an_empty_url_is_reported_on_the_field(hass):
    """The message belongs next to the box the user left blank."""
    result = await _start_user_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DB_URL: "   "}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_DB_URL: "cannot_connect"}


@pytest.mark.asyncio
async def test_an_unreachable_database_is_reported(hass):
    result = await _start_user_flow(hass)

    with _check_database("cannot_connect"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DB_URL: GOOD_URL}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.asyncio
async def test_a_database_without_timescaledb_is_refused(hass):
    """Distinguishable from an unreachable one: they need different fixes."""
    result = await _start_user_flow(hass)

    with _check_database("no_timescaledb"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DB_URL: GOOD_URL}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_timescaledb"}


@pytest.mark.asyncio
async def test_yaml_import_is_refused_without_timescaledb(hass):
    """A YAML setup gets the same gate as the UI, and leaves no entry behind.

    Nothing is stored, so the block is imported again at the next restart — a
    database that was merely missing the extension recovers by fixing it.
    """
    with _check_database("no_timescaledb"):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "import"}, data={CONF_DB_URL: GOOD_URL}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_timescaledb"
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.asyncio
async def test_yaml_import_creates_the_entry_when_the_database_is_usable(hass):
    with _check_database(None):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "import"}, data={CONF_DB_URL: GOOD_URL}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DB_URL] == GOOD_URL


@pytest.mark.asyncio
async def test_a_second_setup_is_refused(hass, mock_config_entry):
    """Scribe is single-instance: the form is never even shown a second time.

    `single_config_entry` in the manifest makes Home Assistant abort the flow
    before the first step, so an existing installation is configured through
    its options rather than set up again.
    """
    mock_config_entry.add_to_hass(hass)

    result = await _start_user_flow(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


@pytest.mark.asyncio
async def test_recording_nothing_at_all_is_refused(hass, mock_config_entry):
    """An install that records neither states nor events would be inert."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_RECORD_STATES: False, CONF_RECORD_EVENTS: False},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "must_record_something"}


def test_a_single_filter_value_is_accepted_as_a_list():
    """The UI hands back a bare string when only one value was entered."""
    coerced = _coerce_options({CONF_EXCLUDE_DOMAINS: "sensor"})
    assert coerced[CONF_EXCLUDE_DOMAINS] == ["sensor"]


def test_an_empty_filter_value_becomes_an_empty_list():
    coerced = _coerce_options({CONF_EXCLUDE_DOMAINS: ""})
    assert coerced[CONF_EXCLUDE_DOMAINS] == []


def _keys(node, prefix=""):
    """Every leaf path in a translation file."""
    found = set()
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        found |= _keys(value, path) if isinstance(value, dict) else {path}
    return found


def test_the_documented_languages_are_fully_translated():
    """English, French, Spanish and German — the four the README exists in.

    A missing key falls back to English silently, which is how the whole
    Repairs panel stayed untranslated for three of them without anyone
    noticing.
    """
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "custom_components" / "scribe"
    reference = _keys(json.loads((root / "strings.json").read_text()))

    for language in ("en", "fr", "es", "de"):
        path = root / "translations" / f"{language}.json"
        missing = reference - _keys(json.loads(path.read_text()))
        assert not missing, f"{language}.json is missing {sorted(missing)}"


def test_no_translation_invents_a_key_of_its_own():
    """A key nothing reads is a typo, and it renders as nothing at all."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "custom_components" / "scribe"
    reference = _keys(json.loads((root / "strings.json").read_text()))

    for path in sorted((root / "translations").glob("*.json")):
        extra = _keys(json.loads(path.read_text())) - reference
        assert not extra, f"{path.name} defines {sorted(extra)}"


def test_every_placeholder_survives_translation():
    """A dropped {placeholder} renders as an empty gap in the Repairs card."""
    import json
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "custom_components" / "scribe"
    reference = json.loads((root / "strings.json").read_text())["issues"]

    for language in ("fr", "es", "de"):
        translated = json.loads(
            (root / "translations" / f"{language}.json").read_text()
        )["issues"]
        for issue, texts in reference.items():
            for field in ("title", "description"):
                expected = set(re.findall(r"\{(\w+)\}", texts[field]))
                actual = set(re.findall(r"\{(\w+)\}", translated[issue][field]))
                assert expected == actual, (
                    f"{language} {issue}.{field}: {expected ^ actual}"
                )


def _readme_config_keys(text):
    """The YAML example's keys and the parameter table's keys, from a README."""
    import re

    # The second ```yaml block is "Full Configuration"; the first is the
    # minimal one, which is meant to be short.
    example = text.split("```yaml\nscribe:\n", 2)[2].split("```")[0]
    return (
        re.findall(r"^  ([a-z_]+):", example, re.M),
        re.findall(r"^\| `([a-z_]+)` \|", text, re.M),
    )


def _yaml_schema_keys():
    """Every key `configuration.yaml` actually accepts, read from CONFIG_SCHEMA."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "custom_components" / "scribe"
    source = (root / "__init__.py").read_text()
    block = source[
        source.index("CONFIG_SCHEMA = vol.Schema(") : source.index(
            "extra=vol.ALLOW_EXTRA,\n        )"
        )
    ]
    names = dict(
        re.findall(
            r'^(CONF_[A-Z_]+) = "([a-z_]+)"', (root / "const.py").read_text(), re.M
        )
    )
    return [
        names[c]
        for c in re.findall(r"vol\.(?:Required|Optional)\((CONF_[A-Z_]+)\)", block)
    ]


@pytest.mark.parametrize(
    "readme", ["README.md", "README.fr.md", "README.es.md", "README.de.md"]
)
def test_every_yaml_option_is_documented_in_every_readme(readme):
    """ "Full Configuration" and "Parameter Reference" have to mean it.

    Both drifted from CONFIG_SCHEMA without anyone noticing: `include_entities`
    was in the table but not the example (reported in #53), and the three TLS
    certificate paths were in neither, in all four languages — so a YAML user
    had no way to learn names the integration accepts and the UI exposes.
    """
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / readme).read_text()
    example, table = _readme_config_keys(text)
    accepted = _yaml_schema_keys()

    assert not [k for k in accepted if k not in example], (
        f"{readme}: the full YAML example is missing "
        f"{[k for k in accepted if k not in example]}"
    )
    assert not [k for k in accepted if k not in table], (
        f"{readme}: the parameter reference is missing "
        f"{[k for k in accepted if k not in table]}"
    )


@pytest.mark.parametrize(
    "readme", ["README.md", "README.fr.md", "README.es.md", "README.de.md"]
)
def test_no_readme_documents_an_option_that_does_not_exist(readme):
    """A documented key nothing reads is worse than an undocumented one."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / readme).read_text()
    example, table = _readme_config_keys(text)
    accepted = set(_yaml_schema_keys())

    assert not [k for k in example if k not in accepted], (
        f"{readme}: the YAML example invents {[k for k in example if k not in accepted]}"
    )
    assert not [k for k in table if k not in accepted], (
        f"{readme}: the parameter reference invents "
        f"{[k for k in table if k not in accepted]}"
    )


# A string that is legitimately spelled the same in a target language. Anything
# here is a deliberate decision, not an untranslated leftover — which is the
# whole point of listing them rather than loosening the check.
ALLOWED_IDENTICAL_TO_ENGLISH = {("fr", "options.step.performance.title")}


@pytest.mark.parametrize("language", ["fr", "es", "de"])
def test_no_documented_language_is_secretly_still_english(language):
    """Key-presence is not translation.

    `test_the_documented_languages_are_fully_translated` only checks that the
    keys exist, so a value copied verbatim from English passes it. Fourteen
    strings sat like that in both es.json and de.json — including every label
    in the metadata step and the three TLS certificate paths — while the suite
    reported both files complete.
    """
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "custom_components" / "scribe"
    english = _keys_and_values(
        json.loads((root / "translations" / "en.json").read_text())
    )
    translated = _keys_and_values(
        json.loads((root / "translations" / f"{language}.json").read_text())
    )

    identical = sorted(
        key
        for key, value in english.items()
        if translated.get(key) == value
        and (language, key) not in ALLOWED_IDENTICAL_TO_ENGLISH
    )
    assert not identical, f"{language}.json is still English for {identical}"


def _keys_and_values(node, prefix=""):
    """Every leaf path in a translation file, mapped to its text."""
    found = {}
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            found.update(_keys_and_values(value, path))
        else:
            found[path] = value
    return found


def test_every_interval_setting_is_validated_before_it_reaches_sql():
    """Four settings take an interval; all four must be checked, not just two.

    `chunk_time_interval` was interpolated straight into `create_hypertable`
    while only the two retention fields were validated, so a value ending the
    quoted literal ran whatever followed it — asyncpg's simple query protocol
    executes every statement in the string.
    """
    from custom_components.scribe.config_flow import ScribeOptionsFlowHandler
    import inspect

    source = inspect.getsource(ScribeOptionsFlowHandler.async_step_advanced)
    for setting in (
        "CONF_RETENTION_STATES",
        "CONF_RETENTION_EVENTS",
        "CONF_CHUNK_TIME_INTERVAL",
        "CONF_COMPRESS_AFTER",
    ):
        assert setting in source, f"{setting} reaches SQL without being validated"


def test_the_hypertable_call_never_interpolates_the_chunk_interval():
    """The value is user input; it has to be a parameter, not text in the SQL."""
    import inspect

    from custom_components.scribe.writer import ScribeWriter

    source = inspect.getsource(ScribeWriter._init_hypertable)
    assert "{self.chunk_interval}" not in source
    assert "create_hypertable($1::regclass" in source

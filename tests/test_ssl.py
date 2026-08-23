"""TLS setup: what gets applied, and what is said when it cannot be.

`_create_ssl_context` runs in an executor because asyncpg would otherwise read
certificate files on the event loop. Everything it can fail to apply leaves the
connection *working but less protected than configured*, which is why each of
those cases is reported rather than logged.
"""

import ssl
import subprocess

import pytest

from custom_components.scribe.writer import (
    ISSUE_SSL_DEGRADED,
    ScribeWriter,
    WriterConfig,
    _create_ssl_context,
)


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    """A self-signed certificate and its key, plus a mismatched key."""
    d = tmp_path_factory.mktemp("certs")
    cert, key, other_key = d / "client.crt", d / "client.key", d / "other.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=scribe-test",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["openssl", "genrsa", "-out", str(other_key), "2048"],
        check=True,
        capture_output=True,
    )
    return {"cert": str(cert), "key": str(key), "other_key": str(other_key), "dir": d}


def _writer(hass, **overrides):
    writer = ScribeWriter(
        hass, WriterConfig(db_url="postgresql://u:p@h/d", **overrides)
    )
    return writer


def test_plain_context_verifies_the_server_by_default():
    """Nothing configured still means a verified, encrypted connection."""
    context, problems = _create_ssl_context()

    assert problems == []
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_client_certificate_is_loaded(certs):
    context, problems = _create_ssl_context(
        ssl_cert_file=certs["cert"], ssl_key_file=certs["key"]
    )

    assert problems == []
    assert context.get_ca_certs() is not None


def test_ca_certificate_is_loaded(certs):
    context, problems = _create_ssl_context(ssl_root_cert=certs["cert"])

    assert problems == []
    # The self-signed certificate is now among the trusted ones.
    assert any(
        "scribe-test" in str(entry.get("subject", ""))
        for entry in context.get_ca_certs()
    )


def test_missing_client_certificate_is_reported(certs):
    """The connection proceeds unauthenticated — that must not pass unnoticed."""
    context, problems = _create_ssl_context(
        ssl_cert_file=str(certs["dir"] / "absent.crt")
    )

    assert context is not None
    assert len(problems) == 1
    assert "client certificate not found" in problems[0]


def test_missing_ca_certificate_is_reported(certs):
    context, problems = _create_ssl_context(
        ssl_root_cert=str(certs["dir"] / "absent-ca.crt")
    )

    assert len(problems) == 1
    assert "CA certificate not found" in problems[0]


def test_unusable_client_certificate_is_reported(certs):
    """A key that does not match its certificate must not fail silently."""
    _, problems = _create_ssl_context(
        ssl_cert_file=certs["cert"], ssl_key_file=certs["other_key"]
    )

    assert len(problems) == 1
    assert "could not be loaded" in problems[0]


def test_every_problem_is_reported_at_once(certs):
    _, problems = _create_ssl_context(
        ssl_cert_file=str(certs["dir"] / "absent.crt"),
        ssl_root_cert=str(certs["dir"] / "absent-ca.crt"),
    )

    assert len(problems) == 2


@pytest.mark.asyncio
async def test_ssl_disabled_builds_no_context(hass, mock_pool):
    writer = _writer(hass, use_ssl=False)
    assert await writer._build_ssl_context() is False


@pytest.mark.asyncio
async def test_relative_paths_resolve_from_the_config_directory(hass, certs, tmp_path):
    """Home Assistant users write `/ssl/ca.crt`-style paths, not absolute ones."""
    from unittest.mock import patch

    writer = _writer(hass, use_ssl=True, ssl_root_cert="certs/ca.crt")
    hass.config.config_dir = str(tmp_path)

    with patch(
        "custom_components.scribe.writer._create_ssl_context",
        return_value=(ssl.create_default_context(), []),
    ) as create:
        await writer._build_ssl_context()

    assert create.call_args.args[0] == f"{tmp_path}/certs/ca.crt"


@pytest.mark.asyncio
async def test_degraded_tls_raises_a_repairs_issue(hass, certs):
    """A client certificate that never loads looks exactly like one that works."""
    from unittest.mock import patch

    writer = _writer(hass, use_ssl=True, ssl_cert_file=certs["cert"])

    with (
        patch(
            "custom_components.scribe.writer._create_ssl_context",
            return_value=(
                ssl.create_default_context(),
                ["client certificate not found: x"],
            ),
        ),
        patch.object(writer, "_report_issue") as report,
    ):
        await writer._build_ssl_context()

    assert report.call_args.args[0] == ISSUE_SSL_DEGRADED
    assert "client certificate not found" in report.call_args.args[2]["problems"]


@pytest.mark.asyncio
async def test_a_healthy_tls_setup_retires_the_issue(hass, certs):
    from unittest.mock import patch

    writer = _writer(hass, use_ssl=True, ssl_cert_file=certs["cert"])

    with (
        patch(
            "custom_components.scribe.writer._create_ssl_context",
            return_value=(ssl.create_default_context(), []),
        ),
        patch.object(writer, "_clear_issue") as clear,
    ):
        await writer._build_ssl_context()

    clear.assert_called_once_with(ISSUE_SSL_DEGRADED)

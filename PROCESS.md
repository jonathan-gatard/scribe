# Scribe Development Process

## 1. Development Guidelines

### Coding Standards
- **Language**: Python 3.11+.
- **Style**: Follow PEP 8.
- **Type Hinting**: Mandatory for all new function signatures matching Home Assistant standards (e.g., `-> None`, `-> bool`).
- **Asyncio**: Scribe is fully async. Do not use blocking I/O calls (like `requests` or `time.sleep`) in the event loop. Use `hass.async_add_executor_job` for unavoidable blocking operations (e.g., specific library calls).

### Sensor & Entity structure
- Use `SensorEntity` or `CoordinatorEntity` appropriately.
- Use `SensorEntityDescription` for defining entity properties when possible.
- Use Home Assistant enums (e.g., `UnitOfInformation`, `SensorDeviceClass`, `SensorStateClass`) instead of string literals.

### Database Interaction
- **Writes**: Always go through `writer.py` queue system.
- **Reads**: Use `writer.query()` or specific `get_db_stats()` methods. Ensure queries are read-only (`SELECT`).
- **Migrations**: Database schema updates should be handled in `writer.init_db()`.

## 2. Workflow Cycle

### A. Development & Testing
1.  **Code**: Implement feature or fix.
2.  **Test Locally**: 
    - Create/Update unit tests in `tests/`.
    - Run linting: `ruff check .` (mandatory).
    - Run tests: `./.venv/bin/pytest`.
    - Code must pass all tests before deployment.
3.  **Commit (Local)**: 
    - Commit changes locally to save progress.
    - Format: `type(scope): description` (e.g., `feat(sensor): add new compression ratio`).

### B. Deployment & Verification
1.  **Deploy**: Run `./scripts/deploy.sh /home/jonathan/docker/homeassistant/custom_components/scribe`.
2.  **Restart HA**: Restart Home Assistant core.
3.  **Verify**: Check logs for errors and UI for expected behavior.

### C. Release (REQUIRES USER "GO")
**⚠️ NEVER PUSH OR TAG WITHOUT EXPLICIT USER APPROVAL.**

1.  **Bump Version**: Update `version` in `manifest.json`.
2.  **Documentation**: Update `README.md` and `TECHNICAL_DOCS.md` if features/sensors changed.
3.  **Changelog**: Update `WALKTHROUGH.md` with new version notes.
4.  **Commit Release**: `git commit -am "chore: release vX.Y.Z"`
4.  **Push**: `git push`
5.  **Tag**: 
    - `git tag vX.Y.Z`
    - `git push origin vX.Y.Z`
6.  **GitHub Release**: Create release on GitHub UI or via CLI (only if explicitly asked).

## 3. Environment
- **Virtual Env**: Use `.venv` for running tests to avoid polluting system Python.
- **Dependencies**: Managed in `manifest.json` for HA, and `requirements_test.txt` for local testing.

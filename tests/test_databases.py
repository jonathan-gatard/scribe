import pytest
import docker
import time
import os
from sqlalchemy import create_engine, text
from custom_components.scribe.writer import ScribeWriter
from unittest.mock import MagicMock
import asyncio

# Versions to test
PG_VERSIONS = ["latest-pg15", "latest-pg16", "latest-pg17", "latest-pg18"]

class TestDatabaseVersions:
    @pytest.fixture(scope="module")
    def docker_client(self):
        return docker.from_env()

    @pytest.fixture(scope="module")
    def db_container(self, docker_client, request):
        """Spin up a TimescaleDB container for the requested version."""
        version = request.param
        image = f"timescale/timescaledb:{version}"
        
        print(f"Starting container with image: {image}")
        container = docker_client.containers.run(
            image,
            ports={'5432/tcp': None}, # Bind to random port
            environment={
                "POSTGRES_PASSWORD": "password",
                "POSTGRES_DB": "scribe"
            },
            detach=True,
            auto_remove=True
        )
        
        # Wait for DB to be ready
        start_time = time.time()
        port = None
        
        try:
            while time.time() - start_time < 30:
                container.reload()
                if container.status == 'running':
                    # Get the mapped port
                    ports = container.attrs['NetworkSettings']['Ports']
                    if '5432/tcp' in ports and ports['5432/tcp']:
                        port = ports['5432/tcp'][0]['HostPort']
                        
                        # Try connecting
                        try:
                            # We use sync engine for readiness check
                            url = f"postgresql://postgres:password@localhost:{port}/scribe"
                            engine = create_engine(url)
                            with engine.connect() as conn:
                                conn.execute(text("SELECT 1"))
                            print(f"Database ready on port {port}")
                            break
                        except Exception:
                            pass
                time.sleep(1)
            else:
                raise RuntimeError("Container failed to start or DB not ready")

            yield f"postgresql://postgres:password@localhost:{port}/scribe"
            
        finally:
            print("Stopping container...")
            container.stop()

    @pytest.mark.parametrize("db_container", PG_VERSIONS, indirect=True)
    @pytest.mark.asyncio
    async def test_database_integration(self, db_container):
        """Test ScribeWriter against the spun-up database."""
        db_url = db_container
        
        # Setup ScribeWriter
        hass = MagicMock()
        hass.loop = asyncio.get_event_loop()
        hass.config.config_dir = "/config"
        
        writer = ScribeWriter(
            hass=hass,
            db_url=db_url,
            chunk_interval="7 days",
            compress_after="60 days",
            record_states=True,
            record_events=True,
            batch_size=1, # Flush immediately
            flush_interval=1,
            max_queue_size=100,
            buffer_on_failure=True,
            table_name_states="states",
            table_name_events="events",
            # Enable all stats to test all queries
            enable_stats_io=True,
            enable_stats_chunk=True,
            enable_stats_size=True,
            stats_chunk_interval=1,
            stats_size_interval=1
        )
        
        # Initialize DB (Tests CREATE TABLE, create_hypertable, add_compression_policy)
        await writer._init_db()
        assert writer._engine is not None
        
        # Verify tables created
        async with writer._engine.connect() as conn:
            # Check for hypertable
            result = await conn.execute(text(
                "SELECT * FROM timescaledb_information.hypertables WHERE hypertable_name = 'states'"
            ))
            assert result.rowcount > 0 or len(result.fetchall()) > 0

            # Check for compression policy
            # Note: The view name might vary slightly by version, but jobs view is standard
            result = await conn.execute(text(
                "SELECT * FROM timescaledb_information.jobs WHERE proc_name = 'policy_compression'"
            ))
            assert result.rowcount > 0 or len(result.fetchall()) > 0
            
        # Test writing data (Tests INSERT)
        writer.enqueue({
            "type": "state",
            "time": time.time(),
            "entity_id": "sensor.test",
            "state": "on",
            "value": 1.0,
            "attributes": "{}"
        })
        
        writer.enqueue({
            "type": "event",
            "time": time.time(),
            "event_type": "test_event",
            "event_data": "{}",
            "origin": "LOCAL",
            "context_id": "1",
            "context_user_id": "user",
            "context_parent_id": "parent"
        })
        
        # Force flush
        await writer._flush()
        
        assert writer._states_written == 1
        assert writer._events_written == 1
        
        # Verify data in DB
        async with writer._engine.connect() as conn:
            result = await conn.execute(text("SELECT * FROM states WHERE entity_id = 'sensor.test'"))
            rows = result.fetchall()
            assert len(rows) == 1
            assert rows[0].state == "on"

        # Test Statistics Queries (get_db_stats)
        stats = await writer.get_db_stats()
        
        # Verify keys exist (means queries ran without error)
        assert "states_total_chunks" in stats
        assert "states_total_size" in stats
        assert "events_total_chunks" in stats
        assert "events_total_size" in stats
        
        # Since we inserted data, we should have at least 1 chunk and some size
        # Note: TimescaleDB might not update stats immediately or create chunks immediately for small data
        # But the query execution itself is what we are testing here.
        print(f"Stats retrieved: {stats}")

        await writer.close()

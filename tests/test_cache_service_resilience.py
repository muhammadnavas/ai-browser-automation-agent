import asyncio
import time
from unittest.mock import AsyncMock, patch
import pytest
from redis.exceptions import TimeoutError

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
for path in [str(BACKEND_DIR), str(ROOT_DIR)]:
    if path not in sys.path:
        sys.path.insert(0, path)

from services.cache_service import CacheService


@pytest.mark.asyncio
async def test_redis_timeout_enters_cooldown_immediately():
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=TimeoutError("Timeout connecting to server"))

    service = CacheService(
        redis_url="redis://localhost:6379/0",
        connect_cooldown_seconds=60.0,
        client=mock_client,
    )
    assert service._is_available is True
    assert service._client is mock_client

    # First get operation should catch TimeoutError and enter cooldown
    result = await service.get("agent_cache:test:1")
    assert result is None
    assert service._is_available is False
    assert service._client is None
    assert service._last_connect_attempt > 0

    # Second get operation within 60s cooldown must NOT attempt to connect
    with patch.object(service, "connect", AsyncMock(return_value=False)) as mock_connect:
        result2 = await service.get("agent_cache:test:2")
        assert result2 is None
        mock_connect.assert_not_called()


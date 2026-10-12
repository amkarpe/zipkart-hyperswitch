import pytest


@pytest.fixture(scope="session")
def app_client():
    """One app lifespan for the whole run: the connection pool lives in the app's event
    loop and can only be opened once per process. Async helpers run in that same loop
    via app_client.portal.call(...). The background sweep is disabled."""
    from fastapi.testclient import TestClient

    from app import sweep
    from app.main import app

    async def no_sweep():
        return None

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sweep, "sweep_forever", no_sweep)
        with TestClient(app) as client:
            yield client

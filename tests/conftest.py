import os

import pytest

from bbtui import redact
from bbtui.api import BearerTokenAuth, BitbucketClient
from bbtui.testing.fake_server import DEMO_TOKEN, DEMO_URL, FakeBitbucket


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # Never touch the user's real config dir or env credentials during tests.
    monkeypatch.setenv("BBTUI_CONFIG_DIR", str(tmp_path / "config"))
    for k in list(os.environ):
        if k.startswith("BITBUCKET_"):
            monkeypatch.delenv(k, raising=False)
    redact.clear_secrets()
    yield
    redact.clear_secrets()


@pytest.fixture
def fake():
    return FakeBitbucket(n_bulk_projects=120, n_bulk_repos=130)


@pytest.fixture
async def client(fake):
    c = BitbucketClient(DEMO_URL, BearerTokenAuth(DEMO_TOKEN), transport=fake.transport(), backoff=0)
    yield c
    await c.aclose()

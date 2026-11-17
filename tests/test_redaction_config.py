import base64
import json
import logging
import os
import stat


from bbtui import config, redact
from bbtui.api import BasicAuth, BearerTokenAuth, BitbucketClient
from bbtui.logsetup import setup_logging
from bbtui.testing.fake_server import DEMO_TOKEN, DEMO_URL, FakeBitbucket


def test_redact_patterns():
    s = "Authorization: Bearer abcdefghijklmnop and Basic dXNlcjpwYXNzd29yZA== https://u:secretpw@host/x GIT_CONFIG_VALUE_0=Authorization:x"
    out = redact.redact(s)
    assert "abcdefghijklmnop" not in out
    assert "dXNlcjpwYXNzd29yZA==" not in out
    assert "secretpw" not in out
    assert "GIT_CONFIG_VALUE_0=" + redact.REDACTED in out


def test_registered_secrets_are_redacted():
    BasicAuth("bob", "hunter2-pat")
    text = "token hunter2-pat and b64 " + base64.b64encode(b"bob:hunter2-pat").decode()
    out = redact.redact(text)
    assert "hunter2-pat" not in out
    assert base64.b64encode(b"bob:hunter2-pat").decode() not in out


def test_redact_headers():
    h = redact.redact_headers({"Authorization": "Bearer x", "Accept": "application/json", "Cookie": "a=b"})
    assert h["Authorization"] == redact.REDACTED and h["Cookie"] == redact.REDACTED
    assert h["Accept"] == "application/json"


async def test_debug_log_never_contains_token(tmp_path):
    log_path = tmp_path / "debug.log"
    setup_logging(str(log_path))
    try:
        fake = FakeBitbucket()
        async with BitbucketClient(DEMO_URL, BearerTokenAuth(DEMO_TOKEN), transport=fake.transport()) as c:
            await c.test_connection()
            await c.list_projects()
        # Also try logging the secret directly: it must be scrubbed.
        logging.getLogger("bbtui.test").debug("oops %s", DEMO_TOKEN)
        for h in logging.getLogger("bbtui").handlers:
            h.flush()
        text = log_path.read_text()
        assert "GET /rest/api/1.0/projects" in text
        assert DEMO_TOKEN not in text
        assert redact.REDACTED in text
        assert stat.S_IMODE(os.stat(log_path).st_mode) == 0o600
    finally:
        setup_logging(None)


def test_parse_dotenv():
    text = """
# comment
export BITBUCKET_URL=https://bb.example.test
BITBUCKET_PAT="abc 123"
BITBUCKET_USER='alice'
EMPTY=
WITH_COMMENT=value # trailing
"""
    env = config.parse_dotenv(text)
    assert env == {
        "BITBUCKET_URL": "https://bb.example.test",
        "BITBUCKET_PAT": "abc 123",
        "BITBUCKET_USER": "alice",
        "EMPTY": "",
        "WITH_COMMENT": "value",
    }


def test_credentials_from_env():
    c = config.credentials_from_env({"BITBUCKET_URL": "https://x", "BITBUCKET_PAT": "tok"})
    assert c.auth_method == "bearer" and c.secret == "tok"
    assert "tok" not in repr(c)
    c = config.credentials_from_env({"BITBUCKET_USER": "u", "BITBUCKET_PASSWORD": "pw"})
    assert c.auth_method == "basic" and c.secret == "pw"
    c = config.credentials_from_env({"BITBUCKET_AUTH": "basic", "BITBUCKET_USER": "u", "BITBUCKET_PAT": "tok"})
    assert c.auth_method == "basic" and c.secret == "tok"


def test_saved_config_never_contains_secrets(tmp_path):
    s = config.Settings(base_url="https://bb", username="alice", remember=True)
    s.secret = "should-not-persist"  # type: ignore[attr-defined]  # even if someone attaches one
    path = config.save(s)
    data = json.loads(open(path).read())
    assert "should-not-persist" not in json.dumps(data)
    assert set(data) <= set(config.Settings.PERSISTED)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    loaded = config.load_saved()
    assert loaded.base_url == "https://bb" and loaded.username == "alice"


def test_tls_options_build(tmp_path):
    import ssl

    import pytest

    from bbtui.api.tls import TLSOptions, build_verify

    assert build_verify(TLSOptions(verify=False)) is False
    ctx = build_verify(TLSOptions())
    assert isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED
    with pytest.raises(FileNotFoundError):
        build_verify(TLSOptions(ca_bundle=str(tmp_path / "missing.pem")))

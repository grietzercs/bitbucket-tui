import json

import keyring
import pytest
from keyring.backend import KeyringBackend

from bbtui import config, keychain
from bbtui.__main__ import build_parser, resolve_settings
from bbtui.config import EnvCredentials
from bbtui.testing.fake_server import DEMO_TOKEN, DEMO_URL, FakeBitbucket


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.store = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        self.store.pop((service, username), None)


@pytest.fixture
def mem_keyring():
    previous = keyring.get_keyring()
    kr = MemoryKeyring()
    keyring.set_keyring(kr)
    yield kr
    keyring.set_keyring(previous)


def test_cli_precedence(tmp_path):
    config.save(config.Settings(base_url="https://saved.example", username="saved", remember=True))
    envfile = tmp_path / ".env"
    envfile.write_text("BITBUCKET_URL=https://envfile.example\nBITBUCKET_PAT=from-file-token\n")
    args = build_parser().parse_args(["--env-file", str(envfile), "--user", "cliuser", "--timeout", "5"])
    settings, creds, env = resolve_settings(args, {"BITBUCKET_USER": "envuser"})
    assert settings.base_url == "https://envfile.example"  # env-file beats saved config
    assert settings.username == "cliuser"  # CLI beats env
    assert creds.secret == "from-file-token" and creds.auth_method == "bearer"
    assert settings.request_timeout == 5


def test_no_token_cli_flag():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--token", "x"])


def test_keychain_roundtrip(mem_keyring):
    assert keychain.set_token("https://bb", "bearer", "alice", "tok-1")
    assert keychain.get_token("https://bb", "bearer", "alice") == "tok-1"
    assert (keychain.SERVICE, "https://bb|bearer|alice") in mem_keyring.store
    keychain.delete_token("https://bb", "bearer", "alice")
    assert keychain.get_token("https://bb", "bearer", "alice") is None


async def test_login_opt_in_keychain_saves_token_not_in_config(mem_keyring):
    from textual.widgets import Checkbox, Input

    from bbtui.tui.app import BitbucketApp
    from bbtui.tui.browse import ProjectsScreen

    from test_tui import SIZE, wait_for

    fake = FakeBitbucket()
    app = BitbucketApp(config.Settings(base_url=DEMO_URL, use_macos_keychain_cas=False), env_creds=EnvCredentials(),
                       env={}, transport_factory=fake.transport, ui_timeout=10)
    async with app.run_test(size=SIZE) as pilot:
        app.screen.query_one("#secret", Input).value = DEMO_TOKEN
        app.screen.query_one("#remember", Checkbox).value = True
        app.screen.query_one("#save_token", Checkbox).value = True
        await pilot.press("ctrl+s")
        await wait_for(pilot, lambda: isinstance(app.screen, ProjectsScreen))
    assert mem_keyring.store[(keychain.SERVICE, f"{DEMO_URL}|bearer|")] == DEMO_TOKEN
    saved = open(config.config_path()).read()
    assert DEMO_TOKEN not in saved
    assert json.loads(saved)["token_in_keychain"] is True

    # Next start: token is pre-filled from the Keychain (masked input).
    app2 = BitbucketApp(config.load_saved(), env_creds=EnvCredentials(), env={}, transport_factory=fake.transport)
    async with app2.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert app2.screen.query_one("#secret", Input).value == DEMO_TOKEN

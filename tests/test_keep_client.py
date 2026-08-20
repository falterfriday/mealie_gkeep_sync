"""Error translation in the Keep client.

gkeepapi reaches Google through ``requests``, so network failures arrive as ``requests``
exceptions rather than gkeepapi ones. Regression cover: these used to escape to the
runner's catch-all and log a full traceback on every retry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import requests
from gkeepapi import exception as gkeep_exc

from mealie_gkeep_sync.errors import AuthError, TransientError
from mealie_gkeep_sync.keep_client import KeepClient


@pytest.fixture
def client(tmp_path: Path) -> KeepClient:
    return KeepClient(
        "user@example.com",
        "aas_et/fake",
        state_path=tmp_path / "keep-state.json",
        list_name="Groceries",
    )


def _raise(exc: BaseException) -> Any:
    def _inner(*_args: object, **_kwargs: object) -> None:
        raise exc

    return _inner


class TestConnectErrorTranslation:
    def test_connection_error_is_transient(self, client: KeepClient) -> None:
        client._keep.authenticate = _raise(  # type: ignore[method-assign]
            requests.exceptions.ConnectionError("name resolution failed")
        )
        with pytest.raises(TransientError, match="Cannot reach Google Keep"):
            client.connect()
        assert client.connected is False

    def test_timeout_is_transient(self, client: KeepClient) -> None:
        client._keep.authenticate = _raise(  # type: ignore[method-assign]
            requests.exceptions.Timeout("timed out")
        )
        with pytest.raises(TransientError):
            client.connect()

    def test_bad_token_is_auth_error(self, client: KeepClient) -> None:
        client._keep.authenticate = _raise(  # type: ignore[method-assign]
            gkeep_exc.LoginException("BadAuthentication")
        )
        with pytest.raises(AuthError, match="get_master_token"):
            client.connect()

    def test_browser_login_required_is_auth_error(self, client: KeepClient) -> None:
        client._keep.authenticate = _raise(  # type: ignore[method-assign]
            gkeep_exc.BrowserLoginRequiredException("NeedsBrowser")
        )
        with pytest.raises(AuthError, match="browser login"):
            client.connect()

    def test_resync_required_triggers_full_resync(self, client: KeepClient) -> None:
        calls: list[bool] = []

        def fake_sync(resync: bool = False) -> None:
            calls.append(resync)

        client._keep.authenticate = _raise(  # type: ignore[method-assign]
            gkeep_exc.ResyncRequiredException("stale")
        )
        client._keep.sync = fake_sync  # type: ignore[method-assign]
        client._keep.dump = lambda: {}  # type: ignore[method-assign]

        client.connect()
        assert calls == [True]
        assert client.connected is True


class TestSyncErrorTranslation:
    def test_refresh_connection_error_is_transient(self, client: KeepClient) -> None:
        client._keep.sync = _raise(  # type: ignore[method-assign]
            requests.exceptions.ConnectionError("boom")
        )
        with pytest.raises(TransientError, match="Cannot reach Google Keep"):
            client.refresh()

    def test_flush_connection_error_is_transient(self, client: KeepClient) -> None:
        client._keep.sync = _raise(  # type: ignore[method-assign]
            requests.exceptions.ConnectionError("boom")
        )
        with pytest.raises(TransientError):
            client.flush()

    def test_api_401_is_auth_error(self, client: KeepClient) -> None:
        exc = gkeep_exc.APIException(401, "unauthorized")
        client._keep.sync = _raise(exc)  # type: ignore[method-assign]
        with pytest.raises(AuthError, match="401"):
            client.refresh()

    def test_api_500_is_transient(self, client: KeepClient) -> None:
        exc = gkeep_exc.APIException(500, "server error")
        client._keep.sync = _raise(exc)  # type: ignore[method-assign]
        with pytest.raises(TransientError):
            client.refresh()

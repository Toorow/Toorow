from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace


def test_cli_persists_capability_and_prints_fragment_only_once(monkeypatch, capsys):
    from core import db, deployment_mode, self_hosted_bootstrap_cli, self_hosted_instance_claim

    class Connection:
        committed = False

        def commit(self):
            self.committed = True

    conn = Connection()

    @contextmanager
    def get_connection():
        yield conn

    captured = {}

    def provision(connection, **kwargs):
        captured["connection"] = connection
        captured.update(kwargs)
        return SimpleNamespace(expires_at=datetime(2026, 7, 26, 14, tzinfo=timezone.utc))

    monkeypatch.setattr(deployment_mode, "deployment_mode", lambda: "self_hosted")
    monkeypatch.setattr(db, "get_connection", get_connection)
    monkeypatch.setattr(self_hosted_instance_claim, "provision_bootstrap_capability", provision)
    monkeypatch.setattr(
        self_hosted_bootstrap_cli.secrets, "token_urlsafe", lambda _: "secret-value"
    )

    assert self_hosted_bootstrap_cli.main(["--base-url", "https://data.example"]) == 0

    output = capsys.readouterr().out
    assert output.count("secret-value") == 1
    assert "https://data.example/setup#bootstrap=secret-value" in output
    assert captured["connection"] is conn
    assert captured["deployment_mode"] == "self_hosted"
    assert captured["bearer"] == "secret-value"
    assert conn.committed is True


def test_cli_refuses_hosted_mode(monkeypatch):
    import pytest
    from core import deployment_mode, self_hosted_bootstrap_cli

    monkeypatch.setattr(deployment_mode, "deployment_mode", lambda: "hosted")
    with pytest.raises(SystemExit, match="self_hosted"):
        self_hosted_bootstrap_cli.main([])

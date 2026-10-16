"""Sealing stored secrets (``graphos.vault``) and the Host allowlist, without a database."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from graphos import vault
from graphos.app import allowed_hosts, create_app
from graphos.config import Settings
from graphos.vault import Vault, VaultError


def _vault(tmp_path: Path, *lines: str) -> Vault:
    path = tmp_path / "master_key"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return Vault.from_file(path)


def test_a_sealed_value_opens_only_for_its_own_row(tmp_path: Path) -> None:
    v = _vault(tmp_path, vault.new_key_line())
    sealed = v.seal("-----BEGIN PRIVATE KEY-----", "signing_key:a")
    assert sealed.startswith("v1:1:") and "PRIVATE" not in sealed
    assert v.open(sealed, "signing_key:a") == "-----BEGIN PRIVATE KEY-----"
    assert v.seal("x", "c") != v.seal("x", "c")          # a fresh salt and nonce each time
    with pytest.raises(VaultError, match="another row"):
        v.open(sealed, "signing_key:b")
    tampered = sealed[:-6] + ("A" if sealed[-6] != "A" else "B") + sealed[-5:]
    with pytest.raises(VaultError):
        v.open(tampered, "signing_key:a")


def test_values_in_clear_pass_through_and_no_key_stores_in_clear(tmp_path: Path) -> None:
    v = _vault(tmp_path, vault.new_key_line())
    assert v.open("plain old value", "ctx") == "plain old value"
    off = Vault({})
    assert not off.enabled
    assert off.seal("secret", "ctx") == "secret"
    with pytest.raises(VaultError, match="version 1"):
        off.open(v.seal("secret", "ctx"), "ctx")


def test_the_newest_key_seals_and_every_listed_key_opens(tmp_path: Path) -> None:
    old = _vault(tmp_path, vault.new_key_line(1))
    sealed_old = old.seal("s", "ctx")
    both = Vault({**old.keys, **_vault(tmp_path, vault.new_key_line(2)).keys})
    assert both.open(sealed_old, "ctx") == "s"
    resealed = both.reseal(sealed_old, "ctx")
    assert resealed.startswith("v1:2:") and both.open(resealed, "ctx") == "s"
    assert both.reseal("in clear", "ctx").startswith("v1:2:")


@pytest.mark.parametrize("content", ["", "# only a comment", "1:short", "one:" + "A" * 44, "1"])
def test_a_master_key_file_that_holds_no_valid_key_is_refused(
    tmp_path: Path, content: str,
) -> None:
    path = tmp_path / "master_key"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(VaultError):
        Vault.from_file(path)
    with pytest.raises(VaultError, match="cannot read"):
        Vault.from_file(tmp_path / "missing")


def test_the_new_key_command_prints_a_line_the_file_reads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    assert vault.main(["new-key", "--version", "3"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("3:")
    assert set(_vault(tmp_path, "# comment", "", line).keys) == {3}


# ── Host allowlist ─────────────────────────────────────────────────────────


def test_the_allowlist_adds_the_loopback_names_and_is_off_when_empty() -> None:
    assert allowed_hosts("") == []
    assert allowed_hosts(" , ") == []
    assert allowed_hosts("*") == []
    assert allowed_hosts("Graph.Example.com, *.internal") == [
        "graph.example.com", "*.internal", "localhost", "127.0.0.1",
    ]


def test_a_request_for_another_host_is_refused() -> None:
    app = create_app(Settings(
        db_password="x", auth_disabled=True, allowed_hosts="graph.example.com,graphos-api",
    ))
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/openapi.json", headers={"host": "graph.example.com"}).status_code == 200  # noqa: PLR2004
    assert client.get("/openapi.json", headers={"host": "graphos-api:8080"}).status_code == 200  # noqa: PLR2004
    assert client.get("/openapi.json", headers={"host": "127.0.0.1:8080"}).status_code == 200  # noqa: PLR2004
    refused = client.get("/openapi.json", headers={"host": "evil.example"})
    assert refused.status_code == 400  # noqa: PLR2004
    open_app = TestClient(create_app(Settings(db_password="x", auth_disabled=True)))
    assert open_app.get("/openapi.json", headers={"host": "evil.example"}).status_code == 200  # noqa: PLR2004

"""Private LAN credential provisioning is explicit and idempotent."""
import hashlib
import json
import stat


def test_provision_writes_private_tokens_and_preserves_them_on_rerun(tmp_path):
    from evolvmem.lan_provision import provision

    config = tmp_path / "private" / "lan-server.json"
    credentials = tmp_path / "private" / "clients"
    result = provision(
        config_path=config,
        credentials_dir=credentials,
        data_dir=tmp_path / "lan-data",
        owner_data_dir=tmp_path / "owner-data",
        host="0.0.0.0",
        port=9478,
        client_host="memory.lan",
    )

    assert result.created is True
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o700
    server = json.loads(config.read_text())
    assert server["host"] == "0.0.0.0" and server["port"] == 9478
    assert set(server["token_hashes"]) == {"jiangli", "kane"}
    before = {}
    for name in ("jiangli", "kane"):
        token_path = credentials / f"{name}-token"
        token = token_path.read_text().strip()
        before[name] = token
        assert len(token) >= 32
        assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
        assert server["token_hashes"][name] == hashlib.sha256(token.encode()).hexdigest()
        instructions = credentials / f"{name}-client-instructions.txt"
        assert stat.S_IMODE(instructions.stat().st_mode) == 0o600
        if name == "kane":
            assert token in instructions.read_text()
    assert "127.0.0.1:9478/owner/mcp" in (credentials / "jiangli-client.json").read_text()
    assert json.loads((credentials / "jiangli-client.json").read_text())["token_file"] == str(credentials / "jiangli-token")
    assert "http://memory.lan:9478/mcp" in (credentials / "kane-client-instructions.txt").read_text()

    rerun = provision(config, credentials, tmp_path / "other-lan", tmp_path / "other-owner", client_host="other.lan")
    assert rerun.created is False
    assert {name: (credentials / f"{name}-token").read_text().strip() for name in before} == before
    assert json.loads(config.read_text()) == server

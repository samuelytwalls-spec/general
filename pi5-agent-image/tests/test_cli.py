"""The piagent CLI and the client's own CLI, against a live API server."""

import json
import stat

import pytest

import piagent_client as pc
from piagent import cli
from test_api_e2e import Api


@pytest.fixture
def api(cfg):
    a = Api(cfg)
    yield a
    a.close()


@pytest.fixture
def base(cfg, api, tmp_path):
    toml = tmp_path / "piagent.toml"
    toml.write_text(
        f'[api]\ntls_cert = "{cfg.api.tls_cert}"\ntoken_file = "{cfg.api.token_file}"\nport = {api.port}\n'
    )
    return ["--config", str(toml)]


def test_ask_rejects_destructive_without_terminal(base, llama, capsys):
    llama.script = [
        {"tool": "shell", "input": "rm important.txt"},
        {"tool": "final_answer", "input": "I did not delete it."},
    ]
    cli.main(base + ["ask", "--no-confirm", "delete important.txt"])
    out = capsys.readouterr().out
    assert "-> shell: rm important.txt" in out
    assert "rejected: rm important.txt" in out
    assert "I did not delete it." in out


def test_devices_add_list_remove(base, tmp_path, capsys):
    out_file = tmp_path / "box.json"
    cli.main(base + ["devices", "add", "box", "-o", str(out_file)])
    conf = json.loads(out_file.read_text())
    assert conf["device"] == "box" and len(conf["secret"]) == 64 and conf["tls_fingerprint"]
    assert stat.S_IMODE(out_file.stat().st_mode) == 0o600
    cli.main(base + ["devices", "list"])
    assert "box" in capsys.readouterr().out
    cli.main(base + ["devices", "remove", "box"])
    with pytest.raises(SystemExit):
        cli.main(base + ["devices", "remove", "box"])


def test_status(base, capsys):
    cli.main(base + ["status"])
    assert json.loads(capsys.readouterr().out)["chat_model_ready"] is True


def test_client_cli_init_check_test(api, tmp_path, capsys):
    status, res = api.call("POST", "/v1/devices", {"name": "cli"})
    enrolled = tmp_path / "cli.json"
    enrolled.write_text(json.dumps(res["client_config"]))
    conf = tmp_path / "conf" / "config.json"

    pc.main(["--config", str(conf), "init", "--from-json", str(enrolled)])
    assert stat.S_IMODE(conf.stat().st_mode) == 0o600
    assert pc.load_config(str(conf))["policy"]["allow"]
    with pytest.raises(SystemExit):  # refuses to overwrite
        pc.main(["--config", str(conf), "init", "--from-json", str(enrolled)])

    with pytest.raises(SystemExit) as e:
        pc.main(["--config", str(conf), "check", "uptime"])
    assert e.value.code == 0
    with pytest.raises(SystemExit) as e:
        pc.main(["--config", str(conf), "check", "rm -rf /tmp/x"])
    assert e.value.code == 1

    data = json.loads(conf.read_text())
    data["server"] = f"https://127.0.0.1:{api.port}"
    conf.write_text(json.dumps(data))
    pc.main(["--config", str(conf), "test"])
    assert "authenticated" in capsys.readouterr().out


def test_state_path_falls_back_when_config_dir_is_read_only(tmp_path, monkeypatch):
    ro = tmp_path / "etc"
    ro.mkdir()
    monkeypatch.setattr(pc.os, "access", lambda p, m: False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert pc.default_state_path(str(ro / "config.json")) == str(tmp_path / "state" / "piagent-client" / "state.json")

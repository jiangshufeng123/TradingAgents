"""Running the service on its own: network allowlist, backups, the Windows logon task and firewall."""

from __future__ import annotations

import sqlite3

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from server import access, db  # noqa: E402
from server.backup import backup  # noqa: E402
from server.services import users  # noqa: E402


@pytest.mark.unit
def test_allowed_networks_default_and_custom():
    nets = access.allowed_networks("")
    for ok in ("127.0.0.1", "192.168.1.20", "10.1.2.3", "172.20.0.5", "100.101.102.103", "::1",
               "::ffff:192.168.1.20", "fd7a:115c:a1e0::1"):
        assert access.is_allowed(ok, nets), ok
    for bad in ("8.8.8.8", "100.128.0.1", "2001:db8::1", None, "garbage"):
        assert not access.is_allowed(bad, nets), bad
    only = access.allowed_networks("100.64.0.0/10")
    assert access.is_allowed("100.64.0.9", only) and not access.is_allowed("192.168.1.2", only)
    with pytest.raises(ValueError, match="not a network"):
        access.allowed_networks("192.168.1.0/99")


@pytest.mark.unit
def test_requests_from_outside_are_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TRADINGAGENTS_DATABASE_URL", f"sqlite:///{(tmp_path / 'a.db').as_posix()}")
    monkeypatch.setenv("TRADINGAGENTS_ALLOWED_NETS", "192.168.0.0/16,100.64.0.0/10")
    from server.app import create_app

    app = create_app({"reports_dir": str(tmp_path / "r"), "results_dir": str(tmp_path / "x")},
                     start_worker=False, start_scheduler=False)
    with TestClient(app, client=("8.8.8.8", 50000)) as outside:
        r = outside.get("/api/health")
        assert r.status_code == 403 and "Tailscale" in r.text
        assert outside.get("/").status_code == 403
    with TestClient(app, client=("100.80.1.2", 50000)) as phone:
        assert phone.get("/api/health").status_code == 200


@pytest.mark.unit
def test_backup_copies_the_live_database_and_keeps_the_newest(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_HOME", str(tmp_path / "home"))
    url = f"sqlite:///{(tmp_path / 'live.db').as_posix()}"
    monkeypatch.setenv("TRADINGAGENTS_DATABASE_URL", url)
    db.init_db(url)
    with db.session(url) as s:
        users.create_user(s, "ian", "admin-pass-1", role="admin")
    folder = tmp_path / "bk"
    for i in range(4):
        (folder).mkdir(exist_ok=True)
        (folder / f"server_2026010{i}_000000.db").write_bytes(b"old")
    path = backup(url, keep=3, folder=folder)
    with sqlite3.connect(path) as con:
        assert con.execute("select username from users").fetchone() == ("ian",)
    assert len(list(folder.glob("server_*.db"))) == 3 and path.exists()
    with pytest.raises(RuntimeError, match="pg_dump"):
        backup("postgresql+psycopg://u:p@h/db")


@pytest.mark.unit
def test_service_install_puts_a_launcher_in_the_startup_folder(monkeypatch, tmp_path):
    import subprocess

    from typer.testing import CliRunner

    from cli import main as cli_main

    started, ran = [], []
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(cli_main, "_on_windows", lambda: True)
    monkeypatch.setattr(subprocess, "Popen", lambda args, **k: started.append(args))
    monkeypatch.setattr(subprocess, "run", lambda args, **k: ran.append(args))
    r = CliRunner().invoke(cli_main.app, ["server", "service", "install", "--port", "8100"])
    assert r.exit_code == 0, r.output
    launcher = tmp_path / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "TradingAgents Server.vbs"
    text = launcher.read_text(encoding="utf-16")
    assert "run_server.ps1" in text and "-BindHost 0.0.0.0 -Port 8100" in text and ", 0, False" in text
    assert started == [["wscript.exe", str(launcher)]] and ran            # old copy stopped, new one started
    assert "server firewall" in r.output

    monkeypatch.setattr(cli_main, "_schtasks", lambda *a: (1, ""))
    r = CliRunner().invoke(cli_main.app, ["server", "service", "remove"])
    assert r.exit_code == 0 and not launcher.exists()


@pytest.mark.unit
def test_firewall_rule_is_limited_to_tailscale_and_the_lan(monkeypatch):
    import subprocess
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from cli import main as cli_main

    ran = []
    monkeypatch.setattr(cli_main, "_on_windows", lambda: True)
    monkeypatch.setattr(subprocess, "run", lambda args, **k: ran.append(args) or SimpleNamespace(returncode=0))
    r = CliRunner().invoke(cli_main.app, ["server", "firewall", "--no-lan"])
    assert r.exit_code == 0, r.output
    rule = ran[-1]
    assert "remoteip=100.64.0.0/10" in rule and "localport=8000" in rule and "action=allow" in rule
    monkeypatch.setattr(subprocess, "run", lambda args, **k: SimpleNamespace(returncode=1))
    r = CliRunner().invoke(cli_main.app, ["server", "firewall"])
    assert r.exit_code == 1 and "administrator" in r.output and "LocalSubnet" in r.output


@pytest.mark.unit
def test_restarting_asks_first_when_an_analysis_is_running(monkeypatch, tmp_path):
    import subprocess

    from typer.testing import CliRunner

    from cli import main as cli_main

    started = []
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(cli_main, "_on_windows", lambda: True)
    monkeypatch.setattr(subprocess, "Popen", lambda args, **k: started.append(args))
    monkeypatch.setattr(subprocess, "run", lambda args, **k: None)
    monkeypatch.setattr(cli_main, "_running_jobs", lambda: [(7, "analysis", {"tickers": ["NVDA"]})])
    r = CliRunner().invoke(cli_main.app, ["server", "service", "install"], input="n\n")
    assert r.exit_code == 1 and "Job 7" in r.output and started == []
    r = CliRunner().invoke(cli_main.app, ["server", "service", "install", "--yes"])
    assert r.exit_code == 0 and len(started) == 1


def test_service_is_started_without_the_dotenv_values(tmp_path, monkeypatch):
    """The service reads .env itself when it starts, so editing .env and restarting takes
    effect: it must not inherit the values the install command loaded."""
    from cli.main import _env_without_dotenv

    (tmp_path / ".env").write_text("TRADINGAGENTS_VOTE_RUNS=2\nOTHER_SETTING=x\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRADINGAGENTS_VOTE_RUNS", "2")        # as load_dotenv left it
    monkeypatch.setenv("OTHER_SETTING", "set-by-the-user")    # differs from .env: the user's own
    env = _env_without_dotenv()
    assert "TRADINGAGENTS_VOTE_RUNS" not in env
    assert env["OTHER_SETTING"] == "set-by-the-user"
    assert "PATH" in env


@pytest.mark.unit
def test_install_stops_when_the_old_service_cannot_be_stopped(monkeypatch, tmp_path):
    """A service started from an administrator window cannot be stopped from an ordinary
    one: say so and how to stop it, rather than start a second copy that cannot get the port."""
    import subprocess
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from cli import main as cli_main

    started = []
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(cli_main, "_on_windows", lambda: True)
    monkeypatch.setattr(cli_main, "_running_jobs", lambda: [])
    monkeypatch.setattr(subprocess, "Popen", lambda args, **k: started.append(args))
    monkeypatch.setattr(subprocess, "run", lambda args, **k: SimpleNamespace(stdout="16948 python.exe\n", returncode=0))
    r = CliRunner().invoke(cli_main.app, ["server", "service", "install"])
    assert r.exit_code == 1 and started == []
    assert "16948 python.exe" in r.output and "administrator" in r.output and "Stop-Process" in r.output


@pytest.mark.unit
def test_selfcheck_reports_a_service_on_old_code_and_a_failed_analysis(monkeypatch, tmp_path):
    import io
    import json
    import subprocess
    import urllib.request
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from cli import main as cli_main
    from server import version

    monkeypatch.setenv("DEEPSEEK_API_KEY", "set")
    monkeypatch.setitem(cli_main.DEFAULT_CONFIG, "llm_provider", "deepseek")
    monkeypatch.setitem(cli_main.DEFAULT_CONFIG, "vote_runs", 1)
    seen = {}

    def good_run(cmd, **kw):
        seen["env"] = kw["env"]
        reports = __import__("pathlib").Path(kw["env"]["TRADINGAGENTS_REPORTS_DIR"])
        (reports / "X" / "d").mkdir(parents=True)
        (reports / "X" / "d" / "complete_report.html").write_text("<h1>x</h1>")
        (reports / "cost_ledger.csv").write_text("run_time\n")
        return SimpleNamespace(returncode=0, stdout="[进度 12/12] 组合经理 完成\nAnalyzed 1/1: X\n", stderr="")

    health = {"ok": True, "started": "2026-10-15T21:11:13", "code": "old",
              "settings": {**version.settings(cli_main.DEFAULT_CONFIG), "vote_runs": 2}}
    monkeypatch.setattr(subprocess, "run", good_run)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: io.BytesIO(json.dumps(health).encode()))
    r = CliRunner().invoke(cli_main.app, ["server", "selfcheck"], env={"COLUMNS": "200"})
    assert r.exit_code == 0, r.output
    assert "不是磁盘上的最新代码" in r.output and "vote_runs 2→1" in r.output and "需要重启" in r.output
    env = seen["env"]                                   # nothing of the user's is touched
    assert env["TRADINGAGENTS_SELFCHECK"] == "1" and env["TRADINGAGENTS_REPORT_REUSE_HOURS"] == "0"
    assert env["TRADINGAGENTS_PAPER_TRADING"] == "false" and "ta-selfcheck-" in env["TRADINGAGENTS_REPORTS_DIR"]

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: SimpleNamespace(
        returncode=0, stdout="600519.SS failed: 'NoneType' object has no attribute 'write'\nAnalyzed 0/1: -\n", stderr=""))
    r = CliRunner().invoke(cli_main.app, ["server", "selfcheck"], env={"COLUMNS": "200"})
    assert r.exit_code == 1 and "没有得出结论" in r.output and "先别重启" in r.output and "NoneType" in r.output


@pytest.mark.unit
def test_the_log_keeps_requests_that_matter_and_two_weeks_of_files(tmp_path):
    import logging
    import os
    import time

    from server.logs import QuietAccess, prune

    def line(method, status):
        return logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                                 ("127.0.0.1:1", method, "/api/jobs", "1.1", status), None)

    quiet = QuietAccess()
    assert not quiet.filter(line("GET", 200)) and not quiet.filter(line("GET", 304))
    assert quiet.filter(line("GET", 404)) and quiet.filter(line("POST", 200)) and quiet.filter(line("GET", 500))

    now = time.time()
    for name, age_days in (("server_2026-09-01.log", 30), ("server_2026-10-10.log", 5), ("other.log", 30)):
        path = tmp_path / name
        path.write_text("x")
        os.utime(path, (now - age_days * 86400, now - age_days * 86400))
    assert [p.name for p in prune(tmp_path, now=now)] == ["server_2026-09-01.log"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["other.log", "server_2026-10-10.log"]

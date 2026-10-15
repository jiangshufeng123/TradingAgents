"""Security names next to codes: which source, caching, failures that never break anything."""

from __future__ import annotations

import pytest

from tradingagents import names


@pytest.fixture
def tables(monkeypatch):
    calls = {"cn": 0, "hk": 0}

    def cn():
        calls["cn"] += 1
        return {"600519": "贵州茅台", "000960": "锡业股份", "603980": "吉华集团", "430047": "诺思兰德"}

    def hk():
        calls["hk"] += 1
        return {"00700": "騰訊控股", "06616": "環球新材國際"}

    monkeypatch.setattr(names, "_LOADERS", {"cn": cn, "hk": hk})
    names.reset()
    return calls


@pytest.mark.unit
def test_where_each_listing_is_looked_up():
    assert names._where("600519.SS") == ("cn", "600519")
    assert names._where("603980.SH") == ("cn", "603980")
    assert names._where("430047.BJ") == ("cn", "430047")
    assert names._where("6616.HK") == ("hk", "06616")
    assert names._where("nvda") == ("us", "NVDA")
    assert names._where("7203.T") is None and names._where("") is None


@pytest.mark.unit
def test_names_from_the_tables_are_cached_on_disk(tables):
    assert names.name_of("600519.SS") == "贵州茅台" and names.name_of("603980.SH") == "吉华集团"
    assert names.name_of("0700.HK") == "騰訊控股" and names.label("6616.HK") == "6616.HK 環球新材國際"
    assert names.name_of("688888.SS") is None and names.label("688888.SS") == "688888.SS"
    assert tables == {"cn": 1, "hk": 1}
    names.reset()                                             # a new process reads the file
    assert names.name_of("000960.SZ") == "锡业股份" and tables == {"cn": 1, "hk": 1}


@pytest.mark.unit
def test_a_failed_source_is_not_asked_again_at_once(monkeypatch):
    asked = []

    def down():
        asked.append(1)
        raise ConnectionError("HKEX unreachable")

    monkeypatch.setattr(names, "_LOADERS", {"cn": lambda: {}, "hk": down})
    names.reset()
    assert names.name_of("0700.HK") is None and names.name_of("0005.HK") is None
    assert len(asked) == 1


@pytest.mark.unit
def test_us_names_come_one_at_a_time(monkeypatch):
    monkeypatch.undo()                                        # the real _us_name, with Yahoo faked
    import sys
    from types import SimpleNamespace

    seen = []
    fake = SimpleNamespace(Ticker=lambda s: SimpleNamespace(get_info=lambda: seen.append(s) or {"shortName": "NVIDIA Corporation"}))
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    monkeypatch.setattr(names, "_LOADERS", {"cn": lambda: {}, "hk": lambda: {}})
    names.reset()
    assert names.name_of("NVDA") == "NVIDIA Corporation" and names.name_of("nvda") == "NVIDIA Corporation"
    assert seen == ["NVDA"]


@pytest.mark.unit
def test_report_title_carries_the_name(tables, tmp_path):
    from tradingagents.reporting import write_report_tree

    state = {"trade_date": "2026-10-15", "final_trade_decision": "Rating: Buy",
             "market_report": "m", "sentiment_report": "s", "news_report": "n", "fundamentals_report": "f"}
    md = write_report_tree(state, "600519.SS", tmp_path / "r")
    assert md.read_text(encoding="utf-8").startswith("# Trading Analysis Report: 600519.SS 贵州茅台")
    html = (tmp_path / "r" / "complete_report.html").read_text(encoding="utf-8")
    assert '<span class="name">贵州茅台</span>' in html and "<title>600519.SS 贵州茅台 2026-10-15" in html


@pytest.mark.unit
def test_names_endpoint(tables, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from server import db
    from server.app import create_app
    from server.services import users

    monkeypatch.setenv("TRADINGAGENTS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TRADINGAGENTS_DATABASE_URL", f"sqlite:///{(tmp_path / 'n.db').as_posix()}")
    db.init_db()
    with db.session() as s:
        users.create_user(s, "ian", "admin-pass-1", role="admin")
    app = create_app({"reports_dir": str(tmp_path / "r"), "results_dir": str(tmp_path / "x"),
                      "data_cache_dir": str(tmp_path / "cache")}, start_worker=False, start_scheduler=False)
    with TestClient(app) as c:
        assert c.get("/api/names?t=600519.SS").status_code == 401
        c.post("/api/auth/login", json={"username": "ian", "password": "admin-pass-1"})
        assert c.get("/api/names?t=600519.SS,0700.HK,XYZ").json() == {"600519.SS": "贵州茅台", "0700.HK": "騰訊控股",
                                                                    "XYZ": None}
        assert c.get("/api/names?t=bad ticker!").status_code == 422


@pytest.mark.unit
def test_a_new_listing_refreshes_a_day_old_table(tables, monkeypatch):
    assert names.name_of("600519.SS") == "贵州茅台" and tables["cn"] == 1
    assert names.name_of("301999.SZ") is None and tables["cn"] == 1        # fresh table: no reload
    real = names.time.time
    monkeypatch.setattr(names.time, "time", lambda: real() + 2 * 86400)
    assert names.name_of("301999.SZ") is None and tables["cn"] == 2        # a day old: one reload
    assert names.name_of("000960.SZ") == "锡业股份" and tables["cn"] == 2

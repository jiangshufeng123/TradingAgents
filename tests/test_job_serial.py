"""Screen jobs get a number people can quote: YYMMDD plus that day's sequence."""

from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine, text  # noqa: E402

from server import db  # noqa: E402
from server.models import Job  # noqa: E402
from server.services import jobs, users  # noqa: E402


@pytest.fixture
def url(tmp_path):
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    db.init_db(url)
    with db.session(url) as s:
        users.create_user(s, "ian", "admin-pass-1", role="admin")
        users.create_user(s, "amy", "other-pass-1")
    return url


@pytest.mark.unit
def test_screens_are_numbered_by_day_across_accounts(url, monkeypatch):
    class Day:                                   # freeze "now" for the job service
        value = datetime(2026, 10, 16, 9, 30)

        @classmethod
        def now(cls):
            return cls.value

    monkeypatch.setattr(jobs, "datetime", Day)
    with db.session(url) as s:
        a = jobs.create(s, 1, "screen", {})
        b = jobs.create(s, 2, "screen", {})
        other = jobs.create(s, 1, "analysis", {"tickers": ["600519.SS"]})
        assert (a.serial, b.serial, other.serial) == ("26101601", "26101602", None)
    Day.value = datetime(2026, 10, 17, 8, 0)
    with db.session(url) as s:
        assert jobs.create(s, 1, "screen", {}).serial == "26101701"
        s.add(Job(user_id=1, kind="screen", params={}, serial="26101799"))
        s.flush()
        assert jobs.create(s, 1, "screen", {}).serial == "261017100"     # past 99: a third digit


@pytest.mark.unit
def test_a_database_from_before_numbers_is_upgraded_and_backfilled(tmp_path):
    url = f"sqlite:///{(tmp_path / 'old.db').as_posix()}"
    db.init_db(url)
    eng = create_engine(url)
    with eng.begin() as conn:                    # make it look like the earlier version
        conn.execute(text("DROP INDEX ix_jobs_serial"))
        conn.execute(text("ALTER TABLE jobs DROP COLUMN serial"))
        conn.execute(text("INSERT INTO users (username, password_hash, role, is_active, display_name, created_at) "
                          "VALUES ('ian', 'x', 'admin', 1, '', '2026-01-01 00:00:00')"))
        for kind, when in [("screen", "2026-10-15 20:00:00"), ("analysis", "2026-10-15 20:30:00"),
                           ("screen", "2026-10-15 09:00:00"), ("screen", "2026-10-16 10:00:00")]:
            conn.execute(text("INSERT INTO jobs (user_id, kind, params, status, created_at) "
                              f"VALUES (1, '{kind}', '{{}}', 'done', '{when}')"))
    eng.dispose()
    db.init_db(url)
    db.init_db(url)                              # repeating it changes nothing
    with db.session(url) as s:
        got = {j.id: j.serial for j in s.query(Job).order_by(Job.id)}
    assert got == {1: "26101502", 2: None, 3: "26101501", 4: "26101601"}

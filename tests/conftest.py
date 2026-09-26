from pathlib import Path

import pytest

from morning_brief import db
from morning_brief.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def settings(tmp_path):
    s = Settings(
        data_dir=tmp_path / "data",
        feed_token="t" * 40,
        public_base_url="https://brief.test",
        feeds_path=FIXTURES / "feeds.yaml",
    )
    s.data_dir.mkdir(parents=True)
    return s


@pytest.fixture
def conn(settings):
    c = db.connect(settings.db_path)
    db.migrate(c)
    yield c
    c.close()

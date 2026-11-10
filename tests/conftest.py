from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fake_testit import FakeTestIt
from tms_agent_kit.config import CONFIG_FILE, Config, load_config
from tms_agent_kit.testit import TestItClient
from tms_agent_kit.workspace import Workspace

CONFIG = """
tms = "testit"
base_url = "https://example.testit.software"
project_id = 42
"""


@pytest.fixture
def fake() -> FakeTestIt:
    return FakeTestIt()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    path = tmp_path / CONFIG_FILE
    path.write_text(CONFIG, encoding="utf-8")
    return load_config(path)


@pytest.fixture
def ws(config: Config) -> Workspace:
    return Workspace(config)


@pytest.fixture
def client(fake: FakeTestIt) -> Iterator[TestItClient]:
    with TestItClient(
        "https://example.testit.software", "token", transport=fake.transport, sleep=lambda _: None
    ) as c:
        yield c

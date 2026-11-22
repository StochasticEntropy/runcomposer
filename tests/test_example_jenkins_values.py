"""The Jenkins example's values from the environment: the addresses and the
user name are plain values (``${env-public:…}``), the API token a secret
(``${env:…}``) — so a health answer names the server it checked, a short
user name is fine, and only the token is blanked out of what is answered.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from runcomposer.sdk import Service, load_config
from runcomposer.testing.jenkins import FakeJenkins

from .conftest import REPO

TOKEN = "shop-api-token-example"
CALLBACK_BASE = "http://runcomposer.shop.example:8100"


@pytest.fixture()
def jenkins_example(tmp_path: Path, monkeypatch):
    shutil.copytree(REPO / "examples" / "jenkins", tmp_path / "jenkins",
                    ignore=shutil.ignore_patterns("state", "__pycache__"))
    monkeypatch.syspath_prepend(str(tmp_path / "jenkins"))  # shop_job.py, as PYTHONPATH=examples/jenkins
    fake = FakeJenkins()
    fake.add_job("runcomposer-consumer", lambda build: None)
    fake.add_job("runcomposer-fanout", lambda build: None)
    monkeypatch.setenv("JENKINS_URL", fake.url)
    monkeypatch.setenv("JENKINS_USER", "ci")  # two characters: refused if it were a secret
    monkeypatch.setenv("JENKINS_API_TOKEN", TOKEN)
    monkeypatch.setenv("RUNCOMPOSER_CALLBACK_BASE", CALLBACK_BASE)
    service = Service(load_config(tmp_path / "jenkins" / "config.yaml"))
    try:
        yield service, fake
    finally:
        service.close()
        fake.close()


def test_every_entry_starts_with_a_short_user_name(jenkins_example):
    service, _fake = jenkins_example
    service.validate()
    for entry in ("jenkins", "jenkins-poll", "jenkins-fanout", "jenkins-fanout-push"):
        assert service.runner(entry).client.user == "ci"


def test_health_names_the_server_it_checked(jenkins_example):
    service, fake = jenkins_example
    health = service.runner_health("jenkins", refresh=True)
    assert health.status == "ok", health.detail
    assert health.data["job_url"] == f"{fake.url}/job/runcomposer-consumer"


def test_only_the_api_token_is_a_secret(jenkins_example):
    service, fake = jenkins_example
    secrets = service.config.secret_values()
    assert TOKEN in secrets
    assert fake.url not in secrets and CALLBACK_BASE not in secrets and "ci" not in secrets
    assert service.runner("jenkins").callback_base == CALLBACK_BASE

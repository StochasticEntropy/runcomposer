"""The Robot listener that pushes live status home, over HTTPS: used by
name, it takes the CA bundle and the verification switch from the
environment — the two variables the `runcomposer-exec` consumer reads too."""

# ruff: noqa: F811 — the imported fixtures are named as the tests name them
from __future__ import annotations

import json

import pytest

from .test_remote_tls import (  # noqa: F401 — fixtures
    IDS,
    RUN_ID,
    TOKEN,
    clean_env,
    tls_files,
    tls_server,
)


class _Started:
    """What Robot hands a listener when a test starts, as far as it is read."""

    longname = IDS[0]


class TestTheRobotListenerOverHttps:
    """`RemoteLiveStatusListener` is used by name, so what it needs comes
    from the environment — the two TLS variables the consumer reads too."""

    def listener(self, tls_server, **given):
        from runcomposer.sdk.robot import RemoteLiveStatusListener

        return RemoteLiveStatusListener(tls_server.callback, TOKEN, "eu", **given)

    def test_without_a_bundle_nothing_arrives_and_nothing_is_raised(self, tls_server, clean_env):
        self.listener(tls_server).start_test(None, _Started())
        assert tls_server.paths() == []

    def test_the_bundle_named_in_the_environment_is_trusted(self, tls_server, clean_env):
        clean_env.setenv("RUNCOMPOSER_CA_BUNDLE", str(tls_server.cert))
        self.listener(tls_server).start_test(None, _Started())
        (live,) = tls_server.received
        assert live.path == f"/api/v1/runs/{RUN_ID}/live"
        update = json.loads(live.body)["updates"][0]
        assert (update["shard"], update["item_id"], update["status"]) == ("eu", IDS[0], "RUNNING")

    @pytest.mark.parametrize("word", ["false", "0", "No", "off"])
    def test_verification_is_skipped_when_the_environment_says_so(self, tls_server, clean_env, word):
        clean_env.setenv("RUNCOMPOSER_VERIFY_TLS", word)
        self.listener(tls_server).start_test(None, _Started())
        assert tls_server.paths() == [f"/api/v1/runs/{RUN_ID}/live"]

    def test_arguments_win_over_the_environment(self, tls_server, clean_env):
        clean_env.setenv("RUNCOMPOSER_VERIFY_TLS", "false")
        self.listener(tls_server, verify_tls="true").start_test(None, _Started())
        assert tls_server.paths() == []
        self.listener(tls_server, ca_bundle=str(tls_server.cert), verify_tls="true").start_test(None, _Started())
        assert tls_server.paths() == [f"/api/v1/runs/{RUN_ID}/live"]

    def test_a_word_that_is_neither_true_nor_false_is_refused(self, tls_server, clean_env):
        clean_env.setenv("RUNCOMPOSER_VERIFY_TLS", "perhaps")
        with pytest.raises(ValueError, match="not true or false: 'perhaps'"):
            self.listener(tls_server)

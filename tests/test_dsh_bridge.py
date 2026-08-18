"""dsh_bridge 测试：DSH 薄壳的 Python 入口（inject / extract）。"""

import json

import pytest

from evolvmem import kimi_hooks as kh
from evolvmem.auto_extractor import CandidateMemory
from evolvmem.dsh_bridge import extract_from_messages, inject


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """所有测试用独立数据目录，绝不触碰生产库。"""
    monkeypatch.setenv("EVOLVMEM_DATA_DIR", str(tmp_path / "data"))
    yield tmp_path / "data"


def _long_messages(n=20):
    return [
        {"role": "user", "content": f"这是第 {i} 条用于测试提取的消息内容。"}
        for i in range(n)
    ]


def _summary_candidate(value="本次会话讨论了记忆系统对比测试，结论是两边各有适用场景。"):
    return CandidateMemory(
        key="SESSION_SUMMARY", value=value, attribute="fact",
        tags=["日志", "分类:test"], confidence=1.0, importance=5.0,
        tier="normal",
    )


def _atomic_candidate():
    return CandidateMemory(
        key="test:memory:conclusion:对比",
        value="OpenViking 用目录树组织上下文，EvolvMem 用扁平条目加本地向量。",
        attribute="fact", tags=["对比"], confidence=0.9, importance=6.0,
        tier="normal",
    )


class TestInject:
    def test_inject_empty_store_returns_string(self):
        block = inject(project="test")
        assert isinstance(block, str)

    def test_inject_respects_env_data_dir(self, _isolated_data_dir):
        from evolvmem.config import Config
        cfg = Config()
        assert str(cfg.data_dir).endswith("data")
        block = inject()
        assert isinstance(block, str)


class TestExtract:
    def test_short_conversation_skipped(self):
        status, details = extract_from_messages(
            [{"role": "user", "content": "你好"}], "s1", "test")
        assert status == "skipped"
        assert "too short" in details["reason"]

    def test_missing_llm_config_retry(self, monkeypatch):
        monkeypatch.setattr(kh, "_load_llm_config", lambda: None)
        status, details = extract_from_messages(
            _long_messages(), "s1", "test")
        assert status == "retry"
        assert "unavailable" in details["reason"]

    def test_happy_path_persists_summary_and_atomic(self, monkeypatch,
                                                   _isolated_data_dir):
        from evolvmem.kimi_hooks import LLMConfig

        monkeypatch.setattr(
            kh, "_load_llm_config",
            lambda: LLMConfig(provider="deepseek", api_key="k",
                              base_url="https://example.invalid",
                              model="deepseek-v4-flash"),
        )
        monkeypatch.setattr(
            kh, "_extract_candidates",
            lambda messages, llm_config: [_summary_candidate(),
                                          _atomic_candidate()],
        )
        status, details = extract_from_messages(
            _long_messages(), "dsh-test-session", "testproj")
        assert status == "completed"
        assert details["persisted"] == 2

        from evolvmem.config import Config
        from evolvmem.memory_store import MemoryStore
        with MemoryStore(Config()) as store:
            keys = {r["key"] for r in store.get_active()}
            assert "test:memory:conclusion:对比" in keys
            assert any(":progress:log:" in k for k in keys)
            by_key = store.get_by_key("test:memory:conclusion:对比")
            assert by_key[0]["source_session"].startswith("dsh:")

    def test_happy_path_compat_mode_writes_projection_and_core(
            self, monkeypatch, _isolated_data_dir):
        from evolvmem.kimi_hooks import LLMConfig

        monkeypatch.setenv("EVOLVMEM_CONTEXT_MODE", "compat")
        # compat 模式以既有 legacy 库为前提（正式切换在迁移后才开启）
        from evolvmem.config import Config
        from evolvmem.memory_store import MemoryStore
        with MemoryStore(Config()):
            pass
        monkeypatch.setattr(
            kh, "_load_llm_config",
            lambda: LLMConfig(provider="deepseek", api_key="k",
                              base_url="https://example.invalid",
                              model="deepseek-v4-flash"),
        )
        monkeypatch.setattr(
            kh, "_extract_candidates",
            lambda messages, llm_config: [_summary_candidate(),
                                          _atomic_candidate()],
        )
        status, details = extract_from_messages(
            _long_messages(), "dsh-compat-session", "testproj")
        assert status == "completed"
        assert details["persisted"] == 2

        from evolvmem.context_models import ContextStatus
        from evolvmem.context_store import ContextStore
        with MemoryStore(Config()) as store:
            records = store.get_active()
        assert len(records) == 2
        with ContextStore(Config()) as context_store:
            for record in records:
                context_id = context_store.resolve_legacy_mapping(record["id"])
                assert context_id is not None
                item = context_store.get_item(context_id)
                assert item.status is ContextStatus.ACTIVE
                assert item.layers is not None  # L0/L1/L2 三层齐备
                assert item.layers.l1 == record["value"]
                if record["key"] == "test:memory:conclusion:对比":
                    assert item.confidence == 0.9
                    assert item.importance == 6.0

    def test_invalid_context_mode_persists_via_legacy_backend(
            self, monkeypatch, _isolated_data_dir):
        """非法 context_mode（如 typo）不得让提炼永远 retry：按 legacy 落库，
        Context 功能 fail-closed，Core 表无写入。"""
        from evolvmem.kimi_hooks import LLMConfig

        monkeypatch.setenv("EVOLVMEM_CONTEXT_MODE", "primray")
        monkeypatch.setattr(
            kh, "_load_llm_config",
            lambda: LLMConfig(provider="deepseek", api_key="k",
                              base_url="https://example.invalid",
                              model="deepseek-v4-flash"),
        )
        monkeypatch.setattr(
            kh, "_extract_candidates",
            lambda messages, llm_config: [_summary_candidate(),
                                          _atomic_candidate()],
        )
        status, details = extract_from_messages(
            _long_messages(), "dsh-invalid-mode-session", "testproj")
        assert status == "completed"
        assert details["persisted"] == 2

        from evolvmem.config import Config
        from evolvmem.context_store import ContextStore
        from evolvmem.memory_store import MemoryStore
        with MemoryStore(Config()) as store:
            keys = {r["key"] for r in store.get_active()}
            assert "test:memory:conclusion:对比" in keys
            assert any(":progress:log:" in k for k in keys)
        with ContextStore(Config()) as context_store:
            assert context_store.count_by_status() == {}

    def test_extract_fails_open_never_raises(self, monkeypatch,
                                             _isolated_data_dir):
        def boom(*a, **kw):
            raise RuntimeError("boom")
        monkeypatch.setattr(kh, "_load_llm_config", lambda: object())
        monkeypatch.setattr(kh, "_extract_candidates", boom)
        status, details = extract_from_messages(
            _long_messages(), "s1", "test")
        assert status == "retry"


class TestCli:
    def test_extract_cli_roundtrip(self, tmp_path, monkeypatch,
                                   _isolated_data_dir):
        from evolvmem.dsh_bridge import main as bridge_main
        msgs = tmp_path / "msgs.json"
        msgs.write_text(json.dumps(_long_messages()), encoding="utf-8")

        class FakeLLM:
            provider = "deepseek"
        monkeypatch.setattr(kh, "_load_llm_config", lambda: FakeLLM())
        monkeypatch.setattr(
            kh, "_extract_candidates",
            lambda messages, llm_config: [_summary_candidate()],
        )
        monkeypatch.setattr(
            "sys.argv",
            ["dsh_bridge", "extract", "--messages-file", str(msgs),
             "--session-id", "cli-sess", "--project", "cliproj"],
        )
        assert bridge_main() == 0

    def test_extract_cli_bad_file_returns_retry_json(self, tmp_path,
                                                     capsys, monkeypatch):
        from evolvmem.dsh_bridge import main as bridge_main
        monkeypatch.setattr(
            "sys.argv",
            ["dsh_bridge", "extract", "--messages-file",
             str(tmp_path / "missing.json")],
        )
        assert bridge_main() == 0
        out = json.loads(capsys.readouterr().out.strip())
        assert out["status"] == "retry"

    def test_inject_cli_prints_block(self, capsys, monkeypatch,
                                     _isolated_data_dir):
        from evolvmem.dsh_bridge import main as bridge_main
        monkeypatch.setattr("sys.argv", ["dsh_bridge", "inject"])
        assert bridge_main() == 0
        assert isinstance(capsys.readouterr().out, str)

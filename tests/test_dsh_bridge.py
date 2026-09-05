"""dsh_bridge 测试：DSH 薄壳的 Python 入口（inject / recall / extract）。"""

import io
import json
from pathlib import Path
import subprocess

import pytest

from evolvmem import kimi_hooks as kh
from evolvmem.auto_extractor import CandidateMemory
from evolvmem.dsh_bridge import extract_from_messages, inject, recall


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


def _empty_recall_bridge(tmp_path):
    bridge = tmp_path / "empty-recall-bridge"
    bridge.write_text(
        "#!/bin/sh\n"
        "case \" $* \" in\n"
        "  *\" recall \"*) printf 'recall\\n' >> \"$CALLS_FILE\" ;;\n"
        "  *) printf 'inject\\n' >> \"$CALLS_FILE\"; "
        "printf 'base block\\n' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    bridge.chmod(0o755)
    return bridge


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


class TestDshProjection:
    def test_bridge_timeout_defaults_to_sixty_seconds_and_accepts_override(self):
        inject_js = Path(__file__).parents[1] / "dsh" / "src" / "inject.js"
        script = (
            "const {recallBridgeTimeoutMs}=await import(process.argv[1]);"
            "console.log(JSON.stringify([recallBridgeTimeoutMs({}),"
            "recallBridgeTimeoutMs({timeoutMs:1234})]));"
        )

        output = subprocess.check_output(
            ["node", "--input-type=module", "--eval", script, inject_js.as_uri()],
            text=True,
        )

        assert json.loads(output) == [60_000, 1_234]

    def test_project_messages_excludes_evolvmem_injection(self):
        common = Path(__file__).parents[1] / "dsh" / "src" / "common.js"
        session = {
            "events": [
                {
                    "type": "user/message",
                    "data": {
                        "content": [{"type": "text", "text": "旧记忆注入"}],
                        "source": {
                            "kind": "plugin",
                            "plugin": "evolvmem-inject",
                        },
                    },
                },
                {
                    "type": "user/message",
                    "data": {"content": [{"type": "text", "text": "真实用户任务"}]},
                },
            ]
        }
        script = (
            "const {projectMessages}=await import(process.argv[1]);"
            "console.log(JSON.stringify(projectMessages(JSON.parse(process.argv[2]))));"
        )

        output = subprocess.check_output(
            ["node", "--input-type=module", "--eval", script, common.as_uri(),
             json.dumps(session, ensure_ascii=False)],
            text=True,
        )

        assert json.loads(output) == [{"role": "user", "content": "真实用户任务"}]

    def test_content_version_changes_only_when_projected_messages_change(self):
        common = Path(__file__).parents[1] / "dsh" / "src" / "common.js"
        messages = [{"role": "user", "content": "任务甲"}]
        appended = [*messages, {"role": "assistant", "content": "结果乙"}]
        script = (
            "const {contentVersion}=await import(process.argv[1]);"
            "const a=JSON.parse(process.argv[2]);const b=JSON.parse(process.argv[3]);"
            "console.log(JSON.stringify([contentVersion(a),contentVersion(a),contentVersion(b)]));"
        )

        output = subprocess.check_output(
            ["node", "--input-type=module", "--eval", script, common.as_uri(),
             json.dumps(messages, ensure_ascii=False),
             json.dumps(appended, ensure_ascii=False)],
            text=True,
        )
        first, repeat, changed = json.loads(output)

        assert first == repeat
        assert changed != first
        assert first.startswith("sha256:")

    def test_latest_task_skips_plugin_messages_and_simple_acknowledgements(self):
        inject_js = Path(__file__).parents[1] / "dsh" / "src" / "inject.js"
        history = {
            "session": {
                "events": [
                    {
                        "type": "user/message",
                        "data": {"content": [{"type": "text", "text": "排查订单列表变慢"}]},
                    },
                    {
                        "type": "user/message",
                        "data": {
                            "content": [{"type": "text", "text": "历史经验注入"}],
                            "source": {"kind": "plugin", "plugin": "evolvmem-inject"},
                        },
                    },
                ]
            }
        }
        decision = {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "继续检查慢查询证据"}]},
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "新经验注入"}],
                    "source": {"kind": "plugin", "plugin": "evolvmem-inject"},
                },
                {
                    "role": "user", "content": "项目执行规则",
                    "source": {"kind": "agent-instructions"},
                },
                {
                    "role": "user", "content": "运行快照和技能目录",
                    "source": {"kind": "plugin", "plugin": "@deepseek-ai/dsh-system-prompt"},
                },
            ]
        }
        script = (
            "const m=await import(process.argv[1]);"
            "const agent=JSON.parse(process.argv[2]);const decision=JSON.parse(process.argv[3]);"
            "const task=m.latestUserTask(agent,decision);"
            "console.log(JSON.stringify([task,m.shouldRecallTask(task),"
            "m.shouldRecallTask('好的。'),m.taskFingerprint(task)]));"
        )

        output = subprocess.check_output(
            ["node", "--input-type=module", "--eval", script, inject_js.as_uri(),
             json.dumps(history, ensure_ascii=False),
             json.dumps(decision, ensure_ascii=False)],
            text=True,
        )
        task, substantive, acknowledgement, fingerprint = json.loads(output)

        assert task == "继续检查慢查询证据"
        assert substantive is True
        assert acknowledgement is False
        assert fingerprint.startswith("sha256:")

    def test_pre_step_recalls_once_per_real_task_and_skips_acknowledgement(
            self, tmp_path):
        inject_js = Path(__file__).parents[1] / "dsh" / "src" / "inject.js"
        bridge = tmp_path / "fake-bridge"
        calls = tmp_path / "calls.log"
        bridge.write_text(
            "#!/bin/sh\n"
            "case \" $* \" in\n"
            "  *\" recall \"*) printf 'recall\\n' >> \"$CALLS_FILE\"; "
            "printf 'experience block\\n' ;;\n"
            "  *) printf 'inject\\n' >> \"$CALLS_FILE\"; "
            "printf 'memory block\\n' ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        bridge.chmod(0o755)
        script = (
            "const m=await import(process.argv[1]);let handler;"
            "m.apply({on:(_name,fn)=>{handler=fn;}},{python:process.argv[2],"
            "env:{CALLS_FILE:process.argv[3]}});"
            "const agent={session:{header:{cwd:process.argv[4]},events:[]}};"
            "let decision={kind:'enter',messages:[{role:'user',content:[{type:'text',"
            "text:'排查订单慢查询'}]}]};"
            "let before=decision;"
            "decision=await handler({agent,signal:{aborted:false}},async()=>before);"
            "before=decision;"
            "decision=await handler({agent,signal:{aborted:false}},async()=>before);"
            "decision={...decision,messages:[...decision.messages,{role:'user',"
            "content:[{type:'text',text:'好的。'}]}]};"
            "before=decision;"
            "decision=await handler({agent,signal:{aborted:false}},async()=>before);"
            "console.log(JSON.stringify(decision.messages.map(x=>x.extra??null)));"
        )

        output = subprocess.check_output(
            ["node", "--input-type=module", "--eval", script,
             inject_js.as_uri(), str(bridge), str(calls), str(tmp_path)],
            text=True,
        )

        extras = json.loads(output)
        assert calls.read_text(encoding="utf-8").splitlines() == [
            "recall", "inject",
        ], extras
        assert len(extras) == 3
        assert extras[1]["taskFingerprint"].startswith("sha256:")

    def test_pre_step_caches_successful_empty_recall_without_dummy_message(
            self, tmp_path):
        inject_js = Path(__file__).parents[1] / "dsh" / "src" / "inject.js"
        bridge = _empty_recall_bridge(tmp_path)
        calls = tmp_path / "calls.log"
        script = (
            "const {apply}=await import(process.argv[1]);let handler;"
            "apply({on:(_name,fn)=>{handler=fn;}},{python:process.argv[2],"
            "env:{CALLS_FILE:process.argv[3]}});"
            "const agent={session:{header:{cwd:process.argv[4]},events:[]}};"
            "let decision={kind:'enter',messages:[{role:'user',content:[{type:'text',"
            "text:'排查订单慢查询'}]}]};"
            "for(let i=0;i<3;i++){const before=decision;decision=await handler("
            "{agent,signal:{aborted:false}},async()=>before);}"
            "console.log(JSON.stringify(decision.messages.map(m=>m.extra??null)));"
        )

        output = subprocess.check_output(
            ["node", "--input-type=module", "--eval", script,
             inject_js.as_uri(), str(bridge), str(calls), str(tmp_path)], text=True)

        extras = json.loads(output)
        assert calls.read_text(encoding="utf-8").splitlines() == [
            "recall", "inject",
        ]
        assert len(extras) == 2
        assert extras[1]["taskFingerprint"].startswith("sha256:")

    def test_pre_step_retries_failed_recall_process(self, tmp_path):
        inject_js = Path(__file__).parents[1] / "dsh" / "src" / "inject.js"
        bridge = tmp_path / "failed-recall"
        calls = tmp_path / "calls.log"
        bridge.write_text(
            "#!/bin/sh\n"
            "case \" $* \" in\n"
            "  *\" recall \"*) printf 'recall\\n' >> \"$CALLS_FILE\"; exit 7 ;;\n"
            "  *) printf 'inject\\n' >> \"$CALLS_FILE\"; printf 'base\\n' ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        bridge.chmod(0o755)
        script = (
            "const {apply}=await import(process.argv[1]);let handler;"
            "apply({on:(_name,fn)=>{handler=fn;}},{python:process.argv[2],"
            "env:{CALLS_FILE:process.argv[3]}});"
            "const prior={type:'user/message',data:{content:[{type:'text',text:'base'}],"
            "source:{kind:'plugin',plugin:'evolvmem-inject'}}};"
            "const agent={session:{header:{cwd:process.argv[4]},events:[prior]}};"
            "let decision={kind:'enter',messages:[{role:'user',content:[{type:'text',"
            "text:'排查订单慢查询'}]}]};"
            "for(let i=0;i<3;i++){const before=decision;decision=await handler("
            "{agent,signal:{aborted:false}},async()=>before);}"
            "console.log(decision.messages.length);"
        )

        output = subprocess.check_output(
            ["node", "--input-type=module", "--eval", script,
             inject_js.as_uri(), str(bridge), str(calls), str(tmp_path)], text=True)

        assert calls.read_text(encoding="utf-8").splitlines() == [
            "recall", "recall", "recall",
        ]
        assert output.strip() == "1"

    def test_empty_recall_cache_invalidates_on_constraints_or_new_user_text(
            self, tmp_path):
        inject_js = Path(__file__).parents[1] / "dsh" / "src" / "inject.js"
        bridge = _empty_recall_bridge(tmp_path)
        calls = tmp_path / "calls.log"
        script = (
            "const {apply}=await import(process.argv[1]);let handler;"
            "apply({on:(_name,fn)=>{handler=fn;}},{python:process.argv[2],"
            "env:{CALLS_FILE:process.argv[3]}});"
            "const prior={type:'user/message',data:{content:[{type:'text',text:'base'}],"
            "source:{kind:'plugin',plugin:'evolvmem-inject'}}};"
            "const agent={session:{header:{cwd:process.argv[4]},events:[prior]}};"
            "let decision={kind:'enter',constraints:{database:'sqlite'},messages:["
            "{role:'user',content:[{type:'text',text:'排查订单慢查询'}]}]};"
            "let before=decision;decision=await handler({agent,signal:{}},async()=>before);"
            "before=decision;decision=await handler({agent,signal:{}},async()=>before);"
            "decision={...decision,constraints:{database:'postgres'}};before=decision;"
            "decision=await handler({agent,signal:{}},async()=>before);"
            "decision={...decision,messages:[...decision.messages,{role:'user',"
            "content:[{type:'text',text:'再检查连接池'}]}]};before=decision;"
            "decision=await handler({agent,signal:{}},async()=>before);"
            "console.log(decision.messages.length);"
        )

        output = subprocess.check_output(
            ["node", "--input-type=module", "--eval", script,
             inject_js.as_uri(), str(bridge), str(calls), str(tmp_path)], text=True)

        assert calls.read_text(encoding="utf-8").splitlines() == [
            "recall", "recall", "recall",
        ]
        assert output.strip() == "2"


class TestRecall:
    def test_recall_uses_dsh_core_service_and_renders_results(
            self, monkeypatch, test_config):
        import evolvmem.context_service as context_service
        import evolvmem.embedding as embedding
        from evolvmem.config import Config
        from evolvmem.context_models import ContextMode

        test_config.context_mode = "primary"
        captured = {"events": []}

        class FakeEngine:
            is_loaded = True

            def __init__(self, config):
                captured["engine_config"] = config

            def initialize(self):
                captured["engine_initialized"] = True

        class FakeExperiences:
            def recall(self, **kwargs):
                captured["events"].append("recall")
                captured["recall"] = kwargs
                return {
                    "results": [{
                        "id": 7,
                        "problem": "列表打开缓慢",
                        "conditions": {"data_size": "large"},
                        "steps": ["先测量查询耗时", "再按页读取"],
                        "exclusions": ["外部接口本身超时"],
                        "validation_level": "single_verified",
                        "sources": [{"id": 3, "source_kind": "tool_result",
                                     "source_ref": "pagination:test"}],
                    }],
                    "used_chars": 180,
                }

        class FakeVectorIndex:
            def initialize(self, *, dim):
                captured["events"].append("vector_index")
                captured["vector_dim"] = dim

        class FakeService:
            def __init__(self, config, embedding_engine=None):
                captured["service_config"] = config
                captured["service_engine"] = embedding_engine
                self.vector_index = FakeVectorIndex()

            def initialize(self, *, mode, adapter):
                captured["events"].append("service")
                captured["mode"] = mode
                captured["adapter"] = adapter

            def _refresh_health(self):
                captured["events"].append("health")

            def experiences(self):
                return FakeExperiences()

            def close(self):
                captured["closed"] = True

        monkeypatch.setattr(Config, "from_file", classmethod(lambda cls, path=None: test_config))
        monkeypatch.setattr(embedding, "EmbeddingEngine", FakeEngine)
        monkeypatch.setattr(context_service, "ContextService", FakeService)

        block = recall(
            project="eva",
            query="订单列表很慢，帮我排查",
            constraints={"data_size": "large"},
            workstream_id="work-1",
        )

        assert captured["mode"] is ContextMode.PRIMARY
        assert captured["adapter"] == "dsh"
        assert captured["engine_initialized"] is True
        assert captured["vector_dim"] == test_config.embedding_dim
        assert captured["events"] == [
            "service", "vector_index", "health", "recall",
        ]
        assert captured["recall"] == {
            "project": "eva",
            "query": "订单列表很慢，帮我排查",
            "constraints": {"data_size": "large"},
            "workstream_id": "work-1",
        }
        assert "列表打开缓慢" in block
        assert "先测量查询耗时" in block
        assert "untrusted" in block
        assert captured["closed"] is True

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
        from evolvmem.context_service import ContextService
        from evolvmem.kimi_hooks import LLMConfig

        original = ContextService.persist_legacy_extraction
        captured = {}

        def spy(service, request, **kwargs):
            captured["llm"] = kwargs.get("llm")
            return original(service, request, **kwargs)

        monkeypatch.setattr(ContextService, "persist_legacy_extraction", spy)
        monkeypatch.setattr(
            kh, "_call_llm_with_retry", lambda prompt, config: "滚动摘要模型结果"
        )
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
        assert callable(captured["llm"])
        assert captured["llm"]("更新项目摘要") == "滚动摘要模型结果"

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
    @staticmethod
    def _run_extract_cli(monkeypatch, messages_path, session_id, content_version):
        from evolvmem.dsh_bridge import main as bridge_main

        monkeypatch.setattr(
            "sys.argv",
            ["dsh_bridge", "extract", "--messages-file", str(messages_path),
             "--session-id", session_id, "--project", "cliproj",
             "--content-version", content_version],
        )
        return bridge_main()

    def test_extract_cli_marks_content_version_and_allows_new_content(
            self, tmp_path, monkeypatch, capsys, _isolated_data_dir):
        import evolvmem.dsh_bridge as bridge

        messages = tmp_path / "msgs.json"
        messages.write_text(json.dumps(_long_messages()), encoding="utf-8")
        calls = []

        def fake_extract(*args, **kwargs):
            calls.append(kwargs["session_id"])
            return "completed", {"persisted": 1}

        monkeypatch.setattr(bridge, "extract_from_messages", fake_extract)

        assert self._run_extract_cli(monkeypatch, messages, "versioned", "sha256:v1") == 0
        assert json.loads(capsys.readouterr().out)["status"] == "completed"
        assert self._run_extract_cli(monkeypatch, messages, "versioned", "sha256:v1") == 0
        assert json.loads(capsys.readouterr().out)["reason"] == "already extracted"
        assert self._run_extract_cli(monkeypatch, messages, "versioned", "sha256:v2") == 0
        assert json.loads(capsys.readouterr().out)["status"] == "completed"
        assert calls == ["versioned", "versioned"]

    def test_extract_cli_upgrades_legacy_session_marker(
            self, tmp_path, monkeypatch, capsys, _isolated_data_dir):
        import evolvmem.dsh_bridge as bridge

        _isolated_data_dir.mkdir(parents=True, exist_ok=True)
        marker = _isolated_data_dir / ".dsh_extracted.json"
        marker.write_text(json.dumps({"legacy": "2026-09-04T10:00:00"}), encoding="utf-8")
        messages = tmp_path / "msgs.json"
        messages.write_text(json.dumps(_long_messages()), encoding="utf-8")
        monkeypatch.setattr(
            bridge, "extract_from_messages",
            lambda *args, **kwargs: ("completed", {"persisted": 1}),
        )

        assert self._run_extract_cli(
            monkeypatch, messages, "legacy", "sha256:current"
        ) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "completed"
        assert json.loads(marker.read_text(encoding="utf-8"))["legacy"][
            "content_version"
        ] == "sha256:current"

    def test_recall_cli_reads_task_payload_and_prints_only_memory_block(
            self, monkeypatch, capsys):
        import evolvmem.dsh_bridge as bridge

        captured = {}

        def fake_recall(**kwargs):
            captured.update(kwargs)
            return "经验召回块"

        monkeypatch.setattr(bridge, "recall", fake_recall)
        monkeypatch.setattr(
            "sys.argv", ["dsh_bridge", "recall", "--project", "eva"])
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(json.dumps({
                "query": "排查订单慢查询",
                "constraints": {"database": "sqlite"},
                "workstream_id": "work-2",
            })),
        )

        assert bridge.main() == 0
        assert capsys.readouterr().out == "经验召回块\n"
        assert captured == {
            "project": "eva",
            "query": "排查订单慢查询",
            "constraints": {"database": "sqlite"},
            "workstream_id": "work-2",
        }

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

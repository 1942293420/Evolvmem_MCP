#!/usr/bin/env python3
"""Evolvmem 离线兜底提炼：兜底 SessionEnd hook 不触发（崩溃/强杀/忘记关闭）的会话。

扫描 ~/.kimi-code/sessions/wd_*/session_*/agents/main/wire.jsonl，对满足以下
条件的会话补跑与 SessionEnd hook 相同的提炼逻辑（evolvmem.kimi_hooks.session_end）：

  1. wire.jsonl 最近 IDLE_MINUTES 分钟内没有改动（大概率已无活会话占用）；
  2. 自上次成功提炼后 wire 又有更新（以 mtime 为准，状态存 STATE_PATH）。

只有状态文件中与当前 wire mtime 精确对应的终态检查点才证明该版本已完成。
历史 memory 行不代表当前 wire 版本；缺少检查点的会话会安全地重跑一次。
每轮最多处理 MAX_PER_RUN 个会话（新的优先），避免积压会话一次性打满 LLM
调用。所有动作写到 stdout，由 cron 重定向到 LOG_PATH，让"没写"可见。

用法（crontab，每小时一次）：
  23 * * * * /usr/bin/flock -n ~/.claude/evolvmem/.extract_stale.lock \
    env PYTHONPATH=/home/jiangli/hermes-memory-plugin \
    /home/jiangli/hermes-memory-plugin/.venv/bin/python \
    /home/jiangli/hermes-memory-plugin/scripts/extract_stale_sessions.py \
    >> ~/.claude/evolvmem/extract_stale.log 2>&1
"""

import glob
import json
import os
import sys
import time
from pathlib import Path

DATA_DIR = Path.home() / ".claude" / "evolvmem"
STATE_PATH = DATA_DIR / ".extracted_sessions.json"
LOG_PATH = DATA_DIR / "extract_stale.log"
SESSIONS_GLOB = str(
    Path.home() / ".kimi-code" / "sessions" / "*" / "session_*"
    / "agents" / "main" / "wire.jsonl")

IDLE_MINUTES = 30       # wire 静默多久才认为会话已结束/僵死
MAX_PER_RUN = 3         # 每轮最多提炼几个会话
MIN_WIRE_BYTES = 4096   # 太短的会话没有提炼价值


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)  # cron 重定向到 extract_stale.log


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def find_candidates(now: float, state: dict) -> list[tuple[float, str]]:
    """Find idle sessions whose latest wire version has not completed."""
    candidates = []
    for wire in glob.glob(SESSIONS_GLOB):
        try:
            st = os.stat(wire)
        except OSError:
            continue
        if st.st_size < MIN_WIRE_BYTES:
            continue
        if now - st.st_mtime < IDLE_MINUTES * 60:
            continue
        session_id = Path(wire).parents[2].name
        if st.st_mtime <= state.get(session_id, {}).get("mtime", 0):
            continue
        candidates.append((st.st_mtime, session_id))
    candidates.sort(reverse=True)
    return candidates


def process_batch(batch: list[tuple[float, str]], state: dict,
                  kimi_hooks) -> bool:
    """Process candidates and advance state only for terminal outcomes.

    Returns True when rate limiting should stop the rest of this run.
    """
    for mtime, session_id in batch:
        try:
            result = kimi_hooks.session_end({"session_id": session_id})
        except Exception as e:
            log(f"extract failed {session_id}: {e}")
            continue

        if result.status in ("completed", "skipped"):
            state[session_id] = {
                "mtime": mtime,
                "via": "offline-fallback",
                "status": result.status,
            }
            if result.status == "completed":
                log(f"completed {session_id}: {result.persisted} memories")
            else:
                log(f"skipped {session_id}: {result.reason}")
            continue

        log(f"deferred {session_id}: {result.reason}")
        if result.rate_limited:
            log("rate limit exhausted; stopping remaining sessions this run")
            return True
    return False


def main() -> None:
    now = time.time()
    state = load_state()
    candidates = find_candidates(now, state)
    batch = candidates[:MAX_PER_RUN]
    log(f"scan: {len(candidates)} stale session(s) pending, "
        f"processing {len(batch)}")

    if not batch:
        save_state(state)
        return

    # provider 预检：凭据未配置时整轮放弃（不消耗会话配额去试）
    from evolvmem import kimi_hooks
    if not kimi_hooks._load_llm_config():
        log("abort: extraction provider credentials unavailable")
        save_state(state)
        return

    process_batch(batch, state, kimi_hooks)

    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"fatal: {e}")
    sys.exit(0)

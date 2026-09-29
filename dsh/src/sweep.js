/**
 * evolvmem-sweep — 定时补扫：对已持久化但未提取的闲置会话跑提取。
 *
 * 与 kimi 侧 extract_stale_sessions.py + cron 同款思路：不依赖会话生命周期
 * 事件（headless 进程退出、web 长闲置会话都可能不触发 disposed），每
 * sweepIntervalMs 扫一次持久化会话列表：
 *   - live 中的会话跳过（仍在被使用）
 *   - 当前投影内容版本已提取的跳过（零 LLM 开销，只读 JSON 标记文件）
 *   - 持久化物件 mtime 距今 < sweepIdleMs 的跳过（会话还在写入）
 *   - 压缩产物 < minArtifactBytes 的跳过（对话太短，必过不了 minChars 门）
 * 每轮最多处理 maxPerSweep 个会话。
 */
import { alreadyExtracted, contentVersion, debugLog, dispatchExtraction, persistedStat, projectMessages } from "./common.js";
import { homedir } from "node:os";
import { join } from "node:path";

export const name = "evolvmem-sweep";
export const inject = ["timer", "sessionQuery"];

export function apply(ctx, config) {
  const sweepIntervalMs = config?.sweepIntervalMs ?? 30 * 60 * 1000;
  const sweepIdleMs = config?.sweepIdleMs ?? 30 * 60 * 1000;
  const maxPerSweep = config?.maxPerSweep ?? 3;
  const minArtifactBytes = config?.minArtifactBytes ?? 1024;
  const configuredDataDir = config?.dataDir || config?.env?.EVOLVMEM_DATA_DIR
    || process.env.EVOLVMEM_DATA_DIR || join(homedir(), ".claude", "evolvmem");
  const dataDir = configuredDataDir === "~" ? homedir()
    : configuredDataDir.startsWith("~/") ? join(homedir(), configuredDataDir.slice(2))
      : configuredDataDir;

  // sessionPersistence 是可选服务（部分 profile 不挂载）：用 ctx.inject 运行时
  // 获取，服务缺席时保持 undefined 并降级为跳过 mtime/size 守卫。
  let sessionPersistence;
  const persistenceFiber = ctx.inject(["sessionPersistence"], (childCtx) => {
    sessionPersistence = childCtx.sessionPersistence;
  });

  let running = false;
  const tick = async () => {
    if (running) return;
    running = true;
    debugLog("tick start");
    try {
      const records = await ctx.sessionQuery?.listSessions();
      debugLog(`tick sessions=${records?.length ?? "nil"}`);
      if (!records || records.length === 0) return;
      let taken = 0;
      let inspected = 0;
      for (const rec of records) {
        if (taken >= maxPerSweep) break;
        if (inspected >= 10) break; // 诊断期限制日志量
        const id = rec?.header?.id ?? rec?.id;
        if (!id) continue;
        inspected += 1;
        if (rec.live) {
          debugLog(`sweep skip id=${id.slice(0, 12)} reason=live`);
          continue;
        }
        const stat = persistedStat(sessionPersistence, id);
        if (stat.size > 0 && stat.size < minArtifactBytes) {
          debugLog(`sweep skip id=${id.slice(0, 12)} reason=small size=${stat.size}`);
          continue;
        }
        if (stat.mtimeMs > 0 && Date.now() - stat.mtimeMs < sweepIdleMs) {
          debugLog(`sweep skip id=${id.slice(0, 12)} reason=fresh mtime=${stat.mtimeMs}`);
          continue;
        }
        debugLog(`sweep candidate id=${id.slice(0, 12)} size=${stat.size}`);
        let session;
        try {
          session = await ctx.sessionQuery.readSession(id);
        } catch (error) {
          debugLog(`sweep readSession failed id=${id}: ${error?.message}`);
          continue;
        }
        const messages = projectMessages(session);
        if (alreadyExtracted(dataDir, id, contentVersion(messages))) {
          debugLog(`sweep skip id=${id.slice(0, 12)} reason=marked`);
          continue;
        }
        const cwd = session?.header?.cwd ?? rec?.header?.cwd ?? process.cwd();
        const project = cwd.split("/").filter(Boolean).pop() ?? "dsh";
        const dispatched = dispatchExtraction(config, messages, id, project);
        if (dispatched) taken += 1;
        debugLog(`sweep id=${id} dispatched=${dispatched} msgs=${messages.length}`);
      }
    } catch (error) {
      debugLog(`sweep error: ${error?.message}`);
    } finally {
      running = false;
    }
  };

  const timer = ctx.setInterval(tick, sweepIntervalMs);
  // 启动后先等一个空闲周期再扫（会话可能正在收尾）
  debugLog(`sweep armed interval=${sweepIntervalMs} idle=${sweepIdleMs}`);
  ctx.on("dispose", () => {
    try { clearInterval(timer); } catch {}
    try { persistenceFiber.dispose(); } catch {}
  });
}

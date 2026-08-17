/**
 * evolvmem-inject — 会话开始一次性注入 EvolvMem L0 记忆块。
 *
 * 学 time-context 的模式：挂在 agent/pre-step 瀑布流上，首个 step 时把
 * Python 一次性命令输出的记忆块包装为 plugin user/message 追加进决策消息。
 * 同一会话（含 fork/resume/replay）只注入一次：注入前先扫会话日志里是否
 * 已存在本插件来源的消息。
 *
 * 全部 fail-open：Python 失败/超时/空块一律静默跳过，绝不阻塞会话。
 */
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { basename } from "node:path";

export const name = "evolvmem-inject";

const PLUGIN_SOURCE = "evolvmem-inject";

function sessionCwd(agent) {
  const header = agent?.session?.header;
  if (header && typeof header.cwd === "string" && header.cwd.length > 0) return header.cwd;
  return process.cwd();
}

function alreadyInjected(agent) {
  const events = agent?.session?.events;
  if (!Array.isArray(events)) return false;
  for (const event of events) {
    if (event?.type === "user/message") {
      const source = event.data?.source;
      if (source?.kind === "plugin" && source?.plugin === PLUGIN_SOURCE) return true;
    }
  }
  return false;
}

function runBridge(config, cwd) {
  const python = config?.python ?? "python3";
  const env = { ...process.env, ...(config?.env ?? {}) };
  const timeoutMs = Number.isFinite(config?.timeoutMs) ? config.timeoutMs : 15000;
  try {
    const result = spawnSync(python, ["-m", "evolvmem.dsh_bridge", "inject"], {
      cwd,
      env,
      encoding: "utf-8",
      timeout: timeoutMs,
      maxBuffer: 1024 * 1024,
    });
    if (result.error || result.status !== 0) return "";
    return (result.stdout ?? "").trim();
  } catch {
    return "";
  }
}

export function apply(ctx, config) {
  ctx.on(
    "agent/pre-step",
    async ({ agent, signal }, next) => {
      const decision = await next();
      if (decision.kind === "reject" || signal?.aborted) return decision;
      if (alreadyInjected(agent)) return decision;
      const cwd = sessionCwd(agent);
      const block = runBridge(config, cwd);
      if (!block) return decision;
      const messages = [...decision.messages, {
        id: randomUUID(),
        role: "user",
        content: [{ type: "text", text: block }],
        source: {
          kind: "plugin",
          plugin: PLUGIN_SOURCE,
          form: "snapshot",
          sections: [{ name: "evolvmem-memory", text: block }],
        },
        extra: { project: basename(cwd) },
      }];
      return { kind: "enter", messages };
    },
    { prepend: true },
  );
}

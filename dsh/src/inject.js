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
import { createHash, randomUUID } from "node:crypto";
import { basename } from "node:path";

export const name = "evolvmem-inject";

const PLUGIN_SOURCE = "evolvmem-inject";
const lastRecalledByAgent = new WeakMap();

function sessionCwd(agent) {
  const header = agent?.session?.header;
  if (header && typeof header.cwd === "string" && header.cwd.length > 0) return header.cwd;
  return process.cwd();
}

function pluginSource(item) {
  const source = item?.data?.source ?? item?.source;
  return source?.kind === "plugin" && source?.plugin === PLUGIN_SOURCE;
}

function itemText(item) {
  const content = item?.data?.content ?? item?.content;
  if (typeof content === "string") return content.trim();
  if (!Array.isArray(content)) return "";
  return content
    .filter((part) => part?.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("\n")
    .trim();
}

function sessionItems(agent, decision) {
  const events = Array.isArray(agent?.session?.events) ? agent.session.events : [];
  const messages = Array.isArray(decision?.messages) ? decision.messages : [];
  return { events, messages };
}

function alreadyInjected(agent, decision) {
  const { events, messages } = sessionItems(agent, decision);
  return [...events, ...messages].some((item) => pluginSource(item));
}

export function latestUserTask(agent, decision) {
  const { events, messages } = sessionItems(agent, decision);
  // DSH also represents plugin snapshots, rules and skill catalogs as user
  // messages. Only native user provenance can supply a task; absent metadata
  // remains compatible with older hosts.
  const fromUser = (item) => {
    const source = item?.data?.source ?? item?.source;
    return source == null || source.kind === "user";
  };
  for (const item of [...messages].reverse()) {
    if (item?.role === "user" && fromUser(item)) {
      const text = itemText(item);
      if (text) return text;
    }
  }
  for (const item of [...events].reverse()) {
    if (item?.type === "user/message" && fromUser(item)) {
      const text = itemText(item);
      if (text) return text;
    }
  }
  return "";
}

export function shouldRecallTask(text) {
  const normalized = String(text ?? "").trim();
  if (!normalized) return false;
  return !/^(?:好(?:的)?|可以|行|收到|明白|谢谢|谢了|嗯+|ok(?:ay)?|yes|没问题)[。.!！?？~～]*$/iu.test(normalized);
}

function stableValue(value) {
  if (Array.isArray(value)) return value.map(stableValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value).sort().map((key) => [key, stableValue(value[key])]),
    );
  }
  return value;
}

function recallConstraints(agent, decision, config) {
  const value = decision?.constraints
    ?? agent?.session?.constraints
    ?? config?.recallConstraints
    ?? {};
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

export function taskFingerprint(text, constraints = {}) {
  const normalized = String(text ?? "").trim().replace(/\s+/gu, " ");
  const input = JSON.stringify([normalized, stableValue(constraints)]);
  return `sha256:${createHash("sha256").update(input).digest("hex")}`;
}

function alreadyRecalled(agent, decision, fingerprint) {
  if (agent && typeof agent === "object"
      && lastRecalledByAgent.get(agent) === fingerprint) return true;
  const { events, messages } = sessionItems(agent, decision);
  return [...events, ...messages].some((item) => {
    if (!pluginSource(item)) return false;
    const extra = item?.data?.extra ?? item?.extra;
    return extra?.taskFingerprint === fingerprint;
  });
}

function markRecalled(agent, fingerprint) {
  if (agent && typeof agent === "object") {
    lastRecalledByAgent.set(agent, fingerprint);
  }
}

function runInjectBridge(config, cwd) {
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
    if (result.status !== 0) return "";
    return (result.stdout ?? "").trim();
  } catch {
    return "";
  }
}

export function recallBridgeTimeoutMs(config) {
  return Number.isFinite(config?.timeoutMs) ? config.timeoutMs : 60000;
}

function runRecallBridge(config, cwd, task, constraints) {
  const python = config?.python ?? "python3";
  const env = { ...process.env, ...(config?.env ?? {}) };
  const timeoutMs = recallBridgeTimeoutMs(config);
  try {
    const result = spawnSync(
      python,
      ["-m", "evolvmem.dsh_bridge", "recall", "--project", basename(cwd)],
      {
        cwd,
        env,
        encoding: "utf-8",
        input: JSON.stringify({ query: task, constraints }),
        timeout: timeoutMs,
        maxBuffer: 1024 * 1024,
      },
    );
    if (result.status !== 0) return { ok: false, block: "" };
    return { ok: true, block: (result.stdout ?? "").trim() };
  } catch {
    return { ok: false, block: "" };
  }
}

export function apply(ctx, config) {
  ctx.on(
    "agent/pre-step",
    async ({ agent, signal }, next) => {
      const decision = await next();
      if (decision.kind === "reject" || signal?.aborted) return decision;
      const cwd = sessionCwd(agent);
      const task = latestUserTask(agent, decision);
      const substantive = shouldRecallTask(task);
      const constraints = recallConstraints(agent, decision, config);
      const fingerprint = substantive ? taskFingerprint(task, constraints) : "";
      let recallKnown = substantive && alreadyRecalled(agent, decision, fingerprint);
      let recallBlock = "";
      if (substantive && !recallKnown) {
        const recalled = runRecallBridge(config, cwd, task, constraints);
        if (recalled.ok) {
          recallKnown = true;
          recallBlock = recalled.block;
          markRecalled(agent, fingerprint);
        }
      }
      const baseBlock = alreadyInjected(agent, decision) ? "" : runInjectBridge(config, cwd);
      const block = [baseBlock, recallBlock].filter(Boolean).join("\n\n");
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
        extra: {
          project: basename(cwd),
          ...(recallKnown ? { taskFingerprint: fingerprint } : {}),
        },
      }];
      return { kind: "enter", messages };
    },
    { prepend: true },
  );
}

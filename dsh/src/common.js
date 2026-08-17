/**
 * evolvmem 共享薄壳工具：会话消息投影 + 提取一次性命令派发。
 * inject.js / extract.js / sweep.js 共用。
 */
import { spawn } from "node:child_process";
import { appendFileSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, basename } from "node:path";

export function debugLog(line) {
  try {
    appendFileSync("/tmp/evolvmem-dsh-extract.log", `${new Date().toISOString()} ${line}\n`);
  } catch {}
}

function eventTextBlocks(event) {
  const content = event?.data?.content;
  if (!Array.isArray(content)) return [];
  return content
    .filter((block) => block?.type === "text" && typeof block.text === "string")
    .map((block) => block.text);
}

/** DSH 会话事件 → [{role, content}]，口径对齐 kimi_hooks（user/assistant 文本）。 */
export function projectMessages(session) {
  const events = session?.events;
  if (!Array.isArray(events)) return [];
  const messages = [];
  for (const event of events) {
    if (event?.type === "user/message") {
      const parts = eventTextBlocks(event);
      if (parts.length > 0) messages.push({ role: "user", content: parts.join("\n") });
    } else if (event?.type === "assistant/message") {
      const parts = eventTextBlocks(event);
      if (parts.length > 0) messages.push({ role: "assistant", content: parts.join("\n") });
    }
  }
  return messages;
}

export function sessionProject(session) {
  const header = session?.header;
  const cwd = typeof header?.cwd === "string" ? header.cwd : process.cwd();
  return {
    cwd,
    project: basename(cwd),
    id: session?.id ?? "",
  };
}

/** 读取 Python 侧提取标记（dataDir/.dsh_extracted.json），判断会话是否已提取。 */
export function alreadyExtracted(dataDir, sessionId) {
  try {
    const markers = JSON.parse(readFileSync(join(dataDir, ".dsh_extracted.json"), "utf-8"));
    return Object.prototype.hasOwnProperty.call(markers, sessionId);
  } catch {
    return false;
  }
}

/** 派发一次提取一次性命令（fire-and-forget，fail-open）。 */
export function dispatchExtraction(config, messages, sessionId, project) {
  const totalChars = messages.reduce((sum, m) => sum + m.content.length, 0);
  const minChars = Number.isFinite(config?.minChars) ? config.minChars : 200;
  if (totalChars < minChars) return false;

  const python = config?.python ?? "python3";
  const env = { ...process.env, ...(config?.env ?? {}) };
  let dir;
  try {
    dir = mkdtempSync(join(tmpdir(), "evolvmem-dsh-"));
  } catch {
    return false;
  }
  const file = join(dir, `session-${sessionId || Date.now()}.json`);
  try {
    writeFileSync(file, JSON.stringify(messages), "utf-8");
  } catch {
    return false;
  }
  const args = [
    "-m", "evolvmem.dsh_bridge", "extract",
    "--messages-file", file,
    "--session-id", sessionId,
    "--project", project,
  ];
  const child = spawn(python, args, { env, stdio: ["ignore", "ignore", "ignore"] });
  child.on("error", () => {});
  child.on("exit", () => {
    try { rmSync(dir, { recursive: true, force: true }); } catch {}
  });
  return true;
}

/** 持久化会话物件的 stat（mtimeMs/size）；不可用时返回 {mtimeMs:0,size:0}。 */
export function persistedStat(sessionPersistence, sessionId) {
  try {
    const loc = sessionPersistence?.locate?.({ id: sessionId });
    const path = typeof loc === "string" ? loc : loc?.path;
    if (path) {
      const st = statSync(path);
      return { mtimeMs: st.mtimeMs, size: st.size };
    }
  } catch {}
  return { mtimeMs: 0, size: 0 };
}

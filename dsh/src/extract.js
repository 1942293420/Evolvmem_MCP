/**
 * evolvmem-extract — 会话结束时把对话投影为消息并交给 Python 提取器。
 *
 * 订阅 session/disposed（root 级监听：teardown 阶段可能晚于本插件子作用域
 * 的销毁顺序）。sweep.js 定时兜底扫漏；两侧都由 Python 侧标记去重。
 */
import { debugLog, dispatchExtraction, projectMessages, sessionProject } from "./common.js";

export const name = "evolvmem-extract";

export function apply(ctx, config) {
  const off = ctx.root.on("session/disposed", (session) => {
    debugLog(`disposed id=${session?.id} events=${session?.events?.length}`);
    const messages = projectMessages(session);
    const { project, id } = sessionProject(session);
    const dispatched = dispatchExtraction(config, messages, id, project);
    debugLog(`dispatched=${dispatched} id=${id} msgs=${messages.length}`);
  });
  ctx.on("dispose", () => off());
}

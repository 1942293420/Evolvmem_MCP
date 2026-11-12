# EvolvMem 个人协作体系开发实施计划

> 日期：2026-10-04。依据：[已认可的融合方案](/home/jiangli/evolvmem-协作体系方案-2026-10-04.md)。本文件是简短执行清单；2026-10-04 用户指定先做 P1，勾选项为已验证完成。
> 执行 Skill：`superpowers-executing-plans`，由主助手逐项实现、验证；沿用本会话 DSH 关闭的选择。

**目标：** 让用户看见并纠正 AI 的理解，让真实对话逐步形成可复用的个人协作方法。
**架构与技术：** 沿用 Python、SQLite、现有 MCP/会话接入和原生 JavaScript 知识库页面；扩展既有学习链路与两个 Skill。

## 共同约束

- 明确资料自动入库、明确规则自动更新；推断、未解冲突和扩大适用范围待确认。人工纠正优先，临时要求不得变成长期习惯。
- 管理融入原知识库；保留七种用途，以五域组织视图。检索不增加在线模型调用，不新增服务或依赖。
- 每阶段按“关键行为测试 → 最小实现 → 页面/真实流程验收 → 相关回归与提交”推进；不为文案和简单样式单独写测试。
- 本轮仅编写计划。后续分析真实资料前核对外部模型处理范围与配额；既有一次 40 条授权不扩展为持续或全库外发。
- 以下路径相对 `/home/jiangli/hermes-memory-plugin`；测试使用项目 `.venv/bin/python -m pytest`。阶段按 P0 → P1 → P2 → P3 → P4 执行。

## P0：先让理解可见、可纠正（N4、N5、N10）

**改动入口：** `evolvmem/memory_learning.py`、`knowledge_api.py`、`web_static/learning.js`、`web_static/knowledge.js`、`web_static/knowledge.css`。

- [ ] 复核现有分类、规则、框架和版本能力；选 Evo 与两个资料明确项目的约 20 段对话，记录预期归属、用途与规则，留出独立验收样本。
- [ ] 在原页面增加“关于我的理解”五域视图，显示来源、明确/推断、适用范围；贯通编辑、改归属、确认/否决与失效规则提示。
- [ ] 用真实资料完成一次“看到理解 → 纠正 → 再次读取”；检查其他项目不受误改、被替代规则不再提供。

**验收：** 用户能回答“AI 学到了什么、依据是什么、怎么改”；既有功能复用，不重复计为新增。
**测试：** 扩展并运行 `tests/test_memory_learning.py`、`tests/test_knowledge_management.py`；浏览器操作验证编辑及移动端布局。

## P1：提炼完整协作过程（N2、N3、N6、N7、N9）

**改动入口：** `evolvmem/auto_extractor.py`、`session_extraction.py`、`kimi_hooks.py`、`memory_learning.py`、`knowledge_rules.py`、`knowledge_api.py`、`web_static/learning.js`。

- [x] 在现有提炼合约中补齐“目标、理解、纠正、决定、验证”和引用；对照同项目相关旧资料，区分新增、补充、替代与跳过。
- [x] 知识管理 Skill 开放提示、分类与分流条件编辑，增加样例预览/前后对照；保留代码中的来源核验、去重与适用范围约束。
- [x] 用样本验证明确规则自动生效；推断/冲突待确认；临时要求、伪造引用和同源重复不得晋升。只修复能复现且影响该链路的旧问题。

**验收：** 同一来源重复运行不产生重复成果；用户纠正能关联旧理解；规则编辑后，预览与正式提炼采用同一版本。
**测试：** 扩展并运行 `tests/test_auto_extractor.py`、`tests/test_memory_learning.py`、`tests/test_knowledge_management.py`、`tests/test_kimi_hooks.py`。

## P2：后台整理并完善协作 Skill（N2、N4、N5、N7、N9）

**改动入口：** `evolvmem/learning_analysis.py`、`memory_learning.py`、`learning_schema.py`、`consolidator.py`、`knowledge_api.py`、`web_static/learning.js`；新增 `evolvmem/learning_consolidation.py` 承担增量整合编排。

- [ ] 增加手动与新增/纠正触发的整理：去重、条件分支、替代关系、独立证据检查及同类项目归纳；分别展示合并、停用、未晋升的原因。
- [ ] 以来源 ID、来源版本和规则版本记录进度；明确变化更新协作 Skill，疑难进入队列；重跑去重、失败续跑、版本恢复均检查来源有效性。
- [ ] 小批回填通过后逐步扩展，显示可处理/完成/跳过/失败及用量；按批准方案，Markdown 文件同步为可选项，启用时必须带记录 ID 和基线版本，避免与数据库分叉。

**验收：** 完成一轮“资料整理 → Skill 更新 → 下一次读取”；重复失败不变成功，新项目保留自身例外，纠正/归档后旧规则停用，模型失败不丢资料。
**测试：** 新增 `tests/test_learning_consolidation.py`；同时运行 `tests/test_memory_learning.py`、`tests/test_consolidator.py`。

## P3：缺口发现与知识补给（N3、N5、N8）

**改动入口：** 新增 `evolvmem/learning_feedback.py`；接入 `learning_schema.py`、`context_service.py`、`knowledge_api.py`、`knowledge_rules.py`、`web_static/learning.js`。

- [ ] 记录应命中未命中、引用被纠正、同话题重复解释；区分采集、提炼、归属、检索、注入、采用及知识错误，不把普通零命中当缺口。
- [ ] 按任务、事件和规则版本记录“提供、采用、有效、不适用、失败、未知”；增加缺口去重、处理、复验关闭及复发重开。
- [ ] 选一个真实缺口检索一手资料，保存来源、版本与适用条件，形成改进建议并验证；更新知识管理 Skill 的补给流程，外部结论先作为参考资料。

**验收：** 一个真实缺口能定位原因、修正并在后续任务复验关闭；一条外部知识有来源和采用依据，未采用不计失败。
**测试：** 新增 `tests/test_learning_feedback.py`；运行 `tests/test_memory_learning.py`、`tests/test_knowledge_management.py`。

## P4：跨客户端使用与最终交付（N1、N3、N6、N10）

**改动入口：** `evolvmem/context_service.py`、`mcp_server.py`、`hooks.py`、`kimi_hooks.py`、`lan_tools.py`、`memory_learning.py`、`web_static/learning.js`；复用现有调度入口。

- [ ] 验证 Codex/Kimi 在开始、继续、切项目和纠正后取得同一有效规则版本；仅注入相关摘要，保留预算、范围与来源限制。
- [ ] 在原页面展示纠正后复发、错误规则应用、采用/验证结果和待确认积压；用留出样本与后续真实任务比较，手动整理稳定后再启用定时运行。
- [ ] 更新架构图与说明，完成相关回归和两客户端真实续接；按已有发布方式交付，登记并回读总控台入口，按公开导出范围同步 GitHub/Gitea。

**验收：** 无需手工粘贴记忆即可跨客户端续接；候选、过期及失效来源不注入；修订/回滚正确，任务断点不丢失，不以条目增长代替效果。
**测试：** 运行 `tests/test_context_service.py`、`tests/test_memory_learning.py`、`tests/test_hooks.py`、`tests/test_kimi_hooks.py`、`tests/test_lan_parity.py`、`tests/test_continuity_service.py`、`tests/test_continuity_mcp.py`，随后执行项目完整回归并区分既有失败与新增回归。
**交付文件：** 同步 `docs/2026-10-04-memory-learning.md`、`docs/evolvmem-atlas.json`，运行 `node scripts/build_architecture_atlas.mjs`，核对生成页面与校验记录；个人记忆和数据库不随源码发布。

## 执行与停点

第一批完成 P0 的“看见并纠正一次理解”，再进入 P1；每阶段验收通过后更新勾选项、保存断点并汇报实际结果。工时在 P0 样本跑通后估算；可选文件同步及未复现旧故障不阻塞主链路。

P1 验证：关键提炼与规则场景 50 项通过；合成数据浏览器完成预览、保存读回及移动端检查。旧知识对照默认未启用，按项目选择；未执行历史回填。

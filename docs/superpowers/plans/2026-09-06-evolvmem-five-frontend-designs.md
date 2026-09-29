# EvolvMem 五版前端设计

用户目标：重构前端审美，首页保留星系图的核心意象，提供5套适合记忆管理、经验复用、项目续接的完整页面方案供比较。
本轮交付是5套可操作设计预览和对比入口；正式选用哪套由用户看过之后决定。
用户本次允许其他设计改变，因此新方案可使用各自色彩、排版、圆角及一种深色探索，不受旧版单一视觉规范限制。
复用现有 API、原生 HTML/CSS/Canvas/JavaScript，不新增服务或运行时依赖；只读预览，不创建虚构记忆、验证结果或成功指标。

## 方案与验收

- [x] 01 Orbit 白昼观测站：通透绿白、横向导航、宽幅立体星系与轻量项目侧栏。
- [x] 02 Atlas 知识图鉴：暖白纸感、图鉴排版、侧边目录与平面轨道图，突出经验阅读。
- [x] 03 Halo 光晕工作台：清透浅色、侧栏工作区、重点星系面板与日常项目管理。
- [x] 04 Signal 星图研究室：灰白钴蓝、瑞士网格、技术星图与清晰的信息索引。
- [x] 05 Nocturne 深空航行：深色宇宙、星光与沉浸式导航，保持管理内容易读。
- [x] 每版均可导航首页、记忆库、经验案例、项目进展；搜索/筛选/详情有实际响应。
- [x] 首页星系由真实项目与记忆数据生成，鼠标/触摸可选项目，提供等效项目列表入口。
- [x] 内容区显示真实验证状态、来源、项目摘要和任务断点；不把高频命中当成功证明。
- [x] 统一对比页可打开5版，附桌面实拍缩略图及各版适用说明。
- [x] 浏览器验收5版桌面与手机、导航、搜索、详情、星图；API失败有明确状态和重试。
- [x] 在现有 Web 服务的 /designs/ 提供预览；原首页只添加设计预览入口。

## 并行分工与公共契约

主代理：common.css、app.js、galaxy.js、对比页、HTTP路由、集成验收及部署。
设计代理分别独占 orbit.html/css + atlas.html/css，halo.html/css + signal.html/css，nocturne.html/css。
每版 HTML 使用 /designs/common.css，再加载专属 CSS；底部依次加载 /designs/galaxy.js 与 /designs/app.js。
不使用框架、远程依赖、远程字体或位图星图，星系使用可交互 Canvas。
公共 body 数据属性 data-design=orbit|atlas|halo|signal|nocturne。
变量：--bg、--panel、--ink、--muted、--line、--accent、--soft、--radius、--display、--body、--mono。
导航按钮或链接 data-page-link=home|memories|experiences|progress；页面容器 data-page-panel 对应同名。
首页自定结构；后3个页面各放 data-slot=memory-browser|experience-browser|progress-browser 的挂载点。
首页挂载点：data-slot=metrics（4统计）、featured-experiences（3条真实案例）、project-list（项目行）、task-list（断点行）、recent-memories（近期记忆）。可按版面选择其中部分。
单个动态数字 data-stat=total_active|verified_experiences|ready_summaries|unfinished_workstreams|project_count。
星图容器 data-galaxy=orbital|atlas|halo|signal|deep；主代理提供渲染及项目点击。容器必须有明确高度且宽度自适应。
可放 data-ui-search 的搜索输入，回车进入记忆搜索；data-refresh 按钮刷新；data-data-status 显示数据状态。
所有设计须含返回 /designs/ 的明确链接，以及“设计预览 · 实时数据”的短标签。
公共样式组件：ui-metric/metric-value/metric-label，ui-card/ui-card-head/ui-card-title/ui-card-copy，ui-badge，ui-row/ui-row-title/ui-row-meta，ui-toolbar/ui-search/ui-select/ui-button，ui-grid/ui-list/ui-empty/ui-pager，ui-dialog/ui-detail-section/ui-detail-meta。
公共控件由主代理实现行为，设计代理可在专属 CSS 覆盖这些组件的视觉与网格布局。
不添加没有行为的假按钮；导航/搜索用上述属性，其他操作只放有真实 href 的链接。
各代理只写自己名下HTML/CSS，不修改公共文件、服务、数据库、旧首页或其他方案。

## 验证结果

- 2026-09-06：Web 路由、原 Web 功能、上下文只读接口测试 83 passed。
- 五版 × 1440/390：星图项目进入、记忆搜索与全文、验证/候选经验、摘要和已完成任务详情通过；没有 JS 错误、横向溢出或 API 写请求。
- 对比页桌面/手机：五张实拍缩略图、并排打开、切换方案、收起再打开通过。
- 已复现并修正：搜索框纵向 flex 撑高、星图数据失败后恢复丢失、项目入口继承旧筛选；最近记忆按创建时间单独读取。
- 独立审查只发现项目入口筛选问题，修复后专项回归通过。
- 浏览器临时验收脚本和截图：/tmp/evolvmem-five-designs/；缩略图随前端保存。

- 部署完成：http://192.168.1.135:9377/designs/；原首页已增加预览入口。代码备份：/home/jiangli/.claude/evolvmem/backups/five-designs-20260906-041851。

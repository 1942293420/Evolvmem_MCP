# 当前前端：Signal · 星图研究室

2026-09-06 用户明确选用 `/designs/signal.html#home`。本节优先于下方旧整理台规范。

- 正式 `/` 使用 Signal；首页保留坐标星图、灰白钴蓝和现有布局。
- 数据页以数据为视觉重点：紧凑表格、清楚正文、对齐字段，避免宣传式大标题、装饰图形和大卡片。
- 记忆库、经验案例、项目进展共用 `web_static/designs/common.css` 的 data-page 样式。
- Signal 页面源码为 `web_static/designs/signal.html`，专属首页样式为 `signal.css`，共用交互为 `app.js`。
- 编辑、归档、项目映射和 AI 整理必须直接集成在 Signal 记忆库，同一导航与数据表格，不得跳转旧管理界面或嵌入旧界面。旧 `/organize` 链接只兼容跳回 `/#memories`。
- `/workflow` 是独立工作原理页，解释真实的召回、经验适配、证据反馈及续接流程；入口置于 Signal 顶栏。
- 其他四版留作备选，默认前端不得再切回旧银河页。

---

# 旧整理台视觉规范（AURORA · DAYLIGHT）

> 以下规范仅供保留的旧整理台参考。来源：定稿银河页 `evolvmem-themes/v4/01-galaxy.html` 与极光页 `evolvmem-themes/v5/01-aurora.html`（风格参考 arstraumur.music 的白昼化转译）。
> **任何新页面开发前必须先读本文件，并严格使用下面的 token；禁止引入本文件之外的色值、圆角、阴影和缓动。**

## 1. 配色

整体是「白昼观测站」：亮底、墨色文字、极光绿点缀。禁止暗色/纯黑背景，禁止纯 `#000` 文字和纯 `#fff` 大平铺以外的死白对比。

| 角色 | 色值 | 用途 |
|---|---|---|
| `--bg0` | `#ffffff` | 页面顶部背景、输入框/卡片底 |
| `--bg1` | `#f5f8fa` | 背景渐变中段 |
| `--bg2` | `#eef3f5` | 背景渐变末端 |
| 页面背景 | `linear-gradient(180deg,--bg0 0%,--bg1 46%,--bg2 100%)` | body 统一三段渐变 |
| `--ink` | `rgba(21,34,43,.90)` | 正文主文字 |
| `--ink-dim` | `rgba(21,34,43,.64)` | 次要文字、lede |
| `--ink-faint` | `rgba(21,34,43,.46)` | 微标签、辅助说明 |
| `--line` | `rgba(21,34,43,.10)` | 发丝分隔线（默认） |
| `--line-strong` | `rgba(21,34,43,.18)` | 列表顶线、强调分隔 |
| `--accent` | `#8fc7a4` | 极光绿：装饰、hover 光晕、tag 底色 |
| `--accent-ink` | `#2f8f6b` | 交互绿：链接、激活态、强调数字 |
| `--accent-soft` | `rgba(143,199,164,.16)` | 柔和填充（徽标、渐变光斑） |
| hover 行底 | `rgba(143,199,164,.06~.07)` | 列表行 hover |

银河页（v4）允许使用青碧主色 `#0f9b8e` 作为星系图主强调，与 `--accent-ink` 同族，二选一，不混用。

## 2. 字体

| 角色 | 字族 | 用途 |
|---|---|---|
| `--font-display` | `Palatino,"Palatino Linotype","Book Antiqua",Georgia,"Songti SC",serif` | 大标题、大数字、wordmark，**字重一律 400** |
| `--font-body` | `"Helvetica Neue",Arial,"Noto Sans SC",system-ui,sans-serif` | 正文，15px / 1.75 |
| `--font-mono` | `ui-monospace,"SF Mono",Menlo,Consolas,monospace` | 微标签、编号、数据，配 `text-transform:uppercase;letter-spacing:.13~.18em` |

字号层级：

- Hero H1：`clamp(2.6rem,5.4vw,4.4rem)` / 行高 1.08 / display 400，强调词用 `<em>`（非斜体，accent-ink 色）
- 区块 H2：`clamp(1.9rem,3.4vw,2.9rem)` / display 400
- 大统计数字：`clamp(2.6rem,4.5vw,4rem)` / display 400 / 行高 1
- kicker / 微标签：`.66rem` mono 大写（`.mono` 工具类）
- 正文/列表：`.92~1.02rem`；辅助说明 `.72~.85rem`

## 3. 间距与留白

- 左右页边距：`--pad-x: clamp(1.25rem,4.5vw,3rem)`
- 内容最大宽：`70rem`；窄栏（hero 文本）：`44rem`
- 区块纵向 padding：`clamp(4rem,11vh,7.5rem)`
- sec-head 与内容间距：`3.2rem`；统计网格 gap：`3rem 2.5rem`
- 列表行 padding：`1.15~1.3rem .4rem`；表单控件 padding：`.8rem .9rem`
- 原则：用留白分区，不用色块和卡片堆叠；一根发丝线胜过一个容器。

## 4. 组件样式

- **圆角**：默认 `0`（直角）；只有圆形元素（行星点、头像）用 `50%`。浮层/弹窗最多 `10px`。
- **边框**：一律 1px `var(--line)`；强调用 `var(--line-strong)`。禁用粗边框。
- **阴影**：常规组件**无阴影**；仅浮层（详情弹窗、tooltip 光晕）允许 `0 0 0 4px rgba(143,199,164,.18)` 这类光晕或轻投影。
- **按钮**：透明底 + 1px `--line` 边框，mono 小字；hover 变 `--accent-ink` 文字/边框。主操作（如「AI 整理」）可用 `--accent-ink` 实心 + 白字。
- **导航**：fixed 顶栏高 `4.25rem`，`rgba(255,255,255,.72)` + `backdrop-filter:blur(14px)`，底部 1px 发丝线；激活项 accent-ink + 下划线。
- **输入/下拉**：白底、1px `--line`、直角；focus 边框变 `--accent-ink`，无 outline。
- **列表行**：顶部 `--line-strong` 起始线 + 行间 `--line`，hover 整行 `rgba(143,199,164,.06)` 底色。
- **标签/徽标**：mono 小字 + 1px 边框或 `--accent-soft` 底，无圆角胶囊。

## 5. 动画（本项目的灵魂）

- 签名缓动只有两个：`--ease-out: cubic-bezier(0.22,0.61,0.36,1)`（进场/揭示）、`--ease-sine: cubic-bezier(0.45,0.05,0.55,0.95)`（微交互）。
- 微交互统一 `.25s`；scroll-reveal `.6s ease-out`，初始态 `opacity:0; translateY(24px)`，进视口加 `.in`。
- 视图切换：旧视图 `.leaving`（fade + 下移 10px）→ 新视图 `.entering`（fade + 上移 14px），`.45s ease-out`。
- 数字一律做 count-up 滚动；条形图 `width` 过渡 `1s ease-out`。
- 氛围动画必须慢：`beam-spin` 光束 42s/64s 反向双层、行星自转/环公转 ≥30s 周期；贝色彩只用 `--accent` 系，透明度 ≤.10。
- 禁止弹跳、快速闪烁、重粒子背景；动画是「呼吸」不是「表演」。

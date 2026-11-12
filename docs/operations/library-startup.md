# 现有 LAN 进程的可选 PG 启动路径

状态：默认关闭 PG，未部署、未切换生产、未执行本差量 SQL。当前 host 只支持四个业务工具（memory_add、memory_replace、memory_remove、context_read）以及可选的库/项目 ACL 管理；完整召回仍未实现。

## 启动与配置

沿用原 --config：lan_server.main → load_server_configuration → build_library_host → 原 make_http_server → 原 Handler → LibraryHost。没有第二个服务。缺省 library_backend 或 enabled:false 保持原 SQLite 行为，不读取新密码、不导入 psycopg、不连接 PG。

启用必须显式配置 legacy_mode:allow|reject 和 semantic_merge_enabled:false。缺失或 true 拒绝启动，因为真实 LegacyWire 尚不支持该语义合并路径。reject 不构造 LanRuntime，从而不顺带启动 SQLite、旧模型或采集 worker；allow 保留原客户端所需的旧 runtime。LanSettings 仍要求原有两份 token_hashes，但它们不是 PG 注册身份，不做隐式映射。

可信服务端配置需实际 control reader binding、content runtime bindings、model_contract、password_ref 到绝对私有文件路径的 secret_files。可选 acl.control / acl.targets 需已核实的 coordinator/projector；runtime bindings 从已有内容绑定精确选取。禁止从请求或设备主机名虚构 principal/device/library/project。Config.from_file 使用 apply_environment=False / ensure_dirs=False。

配置文件最多 1 MiB；启用 PG 要求实际当前属主与 0600。秘密文件为当前属主、0600、普通文件、最大 4096 字节，拒绝 symlink 和内联密码；启动先验证，再创建旧 worker/listener。错误不打印秘密。

文件读取采用 O_NONBLOCK → fstat regular → 限长读取，避免误指向 FIFO 时在 fstat 前阻塞。fdopen 成功后才转移描述符所有权，否则 finally 关闭。现有证据是 Windows 对 flags/stat/fdopen 的模拟检查和静态审阅；未实际创建/打开 Linux FIFO。

## 显式后端与兼容边界

- /library/mcp，或 /mcp 加唯一 X-EvolvMem-Backend: postgres，先于旧 token 匹配选择 PG。未启用、PG 认证失败或不可用时直接拒绝，不重试 SQLite；重复/非法 header 也拒绝。
- allow 模式无新 header 的旧 /mcp 保持旧认证。若旧身份 body 却包含 library_id/project_key/project_id 或 ACL 管理工具，返回 409 explicit_postgres_backend_required，不能静默执行 SQLite。
- PG Expected-User 为实际 principal UUID。旧 token 只有正式登记到控制库后才能用于 PG；同一 token 在两端存在时也应显式选择 PG 路由。
- PG 四工具要求前述注册 project_key。PG-only 不接通旧 owner、embedding、rerank 等接口；只有保留旧 runtime 时这些旧接口才可用。
- device identity、注册 binding、lease、RLS、deadline guard 和 request_scope/ExitStack 清理继续生效。启动失败会关闭已创建的 host/runtime。

## 部署前仍需完成

需要核实实际角色连接/私有文件、凭据与设备注册、真实项目及 ACL 投影、control v5/content v4/provisioning 状态、psycopg cancel-safe 和模型契约。未生成真实生产配置。host 当前请求容量上限仍是 1–4，不代表已达到 10 多人目标。UnsupportedBusiness 仍走通用 HTTP 错误边界，完整 DTO 适配待完成。

候选 19 项新增本地检查曾通过，真实 Handler 使用不绑定 socket 的 fake server，未连接 DB。集成时只做 hash/AST；tests/library_startup/test_startup.py 的 portable loader 支持仓库落点。两份轻量测试均操作 sys.modules，后续需要运行时应使用各自独立 Python 进程，不能把既往通过当作真实 PG 或生产验收。

源代码合入、SQL 应用、真实配置启用、生产切换是四个不同事件，必须分别记回执。当前只准备第一项。完整历史召回、历史 promotion、向量增量/删除、授权跨库召回及容量/回退仍是后续工作。

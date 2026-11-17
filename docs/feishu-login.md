# 飞书登录与只读访问

Web 工作台可选接入飞书 OAuth 登录。启用后，所有访问者先登录：
`owner_open_id` 对应的本人可编辑，其他可使用该飞书应用的账号只能查看同一套数据。
未登录的 API 请求返回 401；只读账号对全部写接口的请求返回 403。
界面隐藏只读账号的编辑、删除、归档、项目改名和 AI 整理入口。

## 配置

1. 在飞书开放平台创建企业自建应用，或选择已有应用。
2. 在应用「安全设置 → 重定向 URL」添加完整回调地址，例如：
   `http://192.168.1.100:9377/auth/feishu/callback`。
3. 应用可用范围包含需要查看工作台的同事；按飞书要求使配置生效。
4. 在 EvolvMem 数据目录创建 `web_auth.json`，权限设为 `0600`：

```json
{
  "enabled": true,
  "app_id": "填入应用 App ID",
  "app_secret": "填入应用 App Secret",
  "redirect_uri": "http://192.168.1.100:9377/auth/feishu/callback",
  "owner_open_id": "填入本人在这个应用中的 open_id",
  "tenant_key": "填入本企业 tenant_key"
}
```

默认数据目录是 `~/.claude/evolvmem`；也可用 `EVOLVMEM_WEB_AUTH_FILE` 指定此配置文件。
明确指定的文件缺失、启用配置不完整或格式错误时，服务启动失败，不会退回无认证模式。
没有配置文件时保持原有本地模式；如需关闭登录，明确设置 `enabled: false` 后重启。
凭据只放在服务器的配置文件里，不提交 Git，也不发送给浏览器。

`open_id` 是同一应用内的账号标识，换应用后需要重新获取；不能使用姓名识别所有者。
使用当前应用的用户凭证调用飞书用户信息接口，即可得到 `open_id` 和 `tenant_key`。
`tenant_key` 可省略，此时所有能够登录该应用的企业账号均可成为只读访客。
管理员必须提前配置，系统不会把首个登录者自动设为管理员。

重启 Web 服务后打开工作台，点击「飞书登录」。你的账号右上角显示「可编辑」，
其他账号显示「只读」。使用其他主机名打开登录入口时，会跳转到配置的固定主机名，
确保登录状态与回调处于同一站点。浏览器必须能访问该地址，服务器必须能出网访问飞书。
HTTP 内网地址可用；使用 HTTPS 回调时，会话 Cookie 自动加 `Secure` 属性。

## 会话与验收

- 会话保存在单个 Web 进程内，8 小时过期；重启 Web 或点击退出登录后需重新登录。
- 退出只结束 EvolvMem 会话，不注销飞书客户端。
- 授权 `state` 与发起登录的浏览器绑定、5 分钟过期、一次性使用；授权码交换带 PKCE。
- 授权码交换使用 v2 JSON 接口，兼容当前飞书授权入口的 S256 PKCE 流程；v3 曾在真实登录中返回 `20049`，参见[兼容问题记录](https://github.com/larksuite/oapi-sdk-go/issues/230)。
- 管理员写请求需要当前会话的 CSRF 令牌；飞书 access token 不返回浏览器、不持久化。
- 登录失败时，服务日志只记录失败步骤、HTTP 状态和数字错误码，不记录密钥、授权码或令牌。
- 未登录访问页面进入登录页；匿名调用数据接口返回 401。
- 用本人账号登录，验证编辑与保存；用同事账号登录，验证搜索、详情与只读提示。
- 同事直接调用删除、归档、项目更新和整理建议等 POST 接口，也必须返回 403。

验证命令：`python -m pytest tests/test_web_auth.py tests/test_web_server.py tests/test_web_designs.py -q`。
自动测试使用临时数据库和模拟的飞书响应；真实账号登录需要完成开发者后台配置后验收。

此设置仅控制 Web 工作台；本地 stdio MCP、hooks 与后台提取继续按原方式运行。

官方依据：[授权码](https://open.feishu.cn/document/authentication-management/access-token/obtain-oauth-code)、
[v2 令牌接口](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/authentication-management/access-token/get-user-access-token)、
[用户信息](https://open.feishu.cn/document/server-docs/authentication-management/login-state-management/get)。

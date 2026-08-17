# 安全修复计划

## 1. 文档目的

本文基于当前项目安全审计结果，记录：

- 已发现并修复的问题；
- 尚存的安全风险；
- 后续修复顺序、实施边界和验收标准；
- 生产部署前必须完成的安全检查。

项目当前是单账号、单实例优先的 OCI Object Storage 管理面板。安全修复以保持现有 FastAPI、Jinja2、OCI SDK 和本地 JSON 持久化架构为前提，不提前引入多租户或分布式基础设施。

## 2. 安全目标

1. 浏览器状态变更必须具备 CSRF 防护。
2. 用户输入的对象名、prefix、跳转地址和响应头值不得改变服务端控制流或协议语义。
3. 登录 Session、分享 token、WebDAV 密码和 OCI 凭据不得泄漏。
4. 上传任务在并发、取消、异常和重启恢复时保持状态一致。
5. 对外错误信息只暴露用户可执行的信息，不暴露 provider、文件系统或内部实现细节。
6. 单实例部署默认安全；多进程、多副本部署必须明确状态协调方案后才能启用。
7. 每项修复都必须有对应的回归测试或真实运行验证。

## 3. 当前审计结论

### 3.1 已修复问题

#### S-001：浏览器状态变更缺少 CSRF 防护

- 严重性：高
- 影响范围：登录、登出、上传、删除、重命名、设置、分享和上传任务控制等状态变更接口。
- 修复位置：`app/main.py`
- 当前方案：校验 `Origin` 或 `Referer` 是否与当前服务同源；校验失败返回 `403`。
- WebDAV 例外：`/webdav` 和 `/webdav/` 使用独立 Basic Auth，不走浏览器 Session CSRF 校验。
- 当前验证：缺少同源请求头时返回 `403`；同源浏览器流程回归通过。

#### S-002：登录 `next_path` 存在开放重定向风险

- 严重性：高
- 影响范围：登录成功后的跳转。
- 修复位置：`app/routes.py`
- 当前方案：只允许站内绝对路径；外部 URL、协议相对 URL 和非绝对路径统一回退到 `/`。
- 附加修复：登录成功前清空旧 Session，降低 Session Fixation 风险。
- 当前验证：外部跳转地址被重置为 `/`。

#### S-003：对象路径接受上级目录和反斜杠路径

- 严重性：高
- 影响范围：对象删除、批量删除、批量下载、上传名、prefix 和相关文件操作。
- 修复位置：`app/routes.py`、`app/utils.py`
- 当前方案：拒绝 `..`、反斜杠和不明确的路径输入；统一使用 POSIX 对象键规则。
- 当前验证：`../secret`、`..\\secret` 等输入返回 `400`。

#### S-004：下载响应的 Content-Disposition 文件名存在响应头注入风险

- 严重性：中
- 影响范围：对象下载、分享下载、WebDAV 下载以及 CSV 等附件响应。
- 修复位置：`app/routes.py`
- 当前方案：移除控制字符、双引号、反斜杠和分号，再生成 ASCII fallback 与 RFC 兼容的 UTF-8 文件名。
- 当前验证：下载响应仍能生成附件，并且不会把危险分隔符带入响应头。

#### S-005：并发 staging chunk 可能造成文件与元数据不一致

- 严重性：高（完整性 / 可用性）
- 影响范围：server-proxy 上传的并发分段暂存和断点恢复。
- 修复位置：`app/temp_uploads.py`、`app/routes.py`
- 当前方案：在同一把进程锁内完成 chunk 校验、文件写入和 session JSON 原子替换。
- 当前验证：新增并发 staging 测试；文件内容和 chunk 元数据保持一致。
- 部署边界：锁仍是进程内锁，不支持跨进程协调。

#### S-006：取消后的上传任务可能被异常处理覆盖为失败

- 严重性：中
- 影响范围：后台上传任务取消、重试等待和异常退出。
- 修复位置：`app/upload_tasks.py`
- 当前方案：worker 异常处理在写入 `failed` 前检查当前状态；已取消任务保持 `canceled`。
- 当前验证：现有上传恢复、取消、重试测试通过。

## 4. 尚存风险与后续计划

### P0：生产部署前必须完成

#### R-001：Session Cookie 未强制 Secure

- 当前状态：已修复。`app/config.py` 支持 `APP_SESSION_HTTPS_ONLY`；生产环境默认开启，本地 HTTP 开发默认关闭且可显式配置。
- 修复位置：`app/config.py`、`app/main.py`、`tests/test_auth_smoke.py`
- 当前方案：`SessionMiddleware` 使用配置驱动的 `https_only`；生产环境若显式关闭会记录明显告警，不通过 User-Agent 或来源字符串绕过。
- 当前验证：`python -m pytest tests/test_auth_smoke.py -q -p no:cacheprovider --basetemp=tmp/pytest-count`，11 passed；Session Cookie 包含 `Secure` 和 `HttpOnly`。
- 部署边界：生产部署必须使用 HTTPS，并保持 `APP_SESSION_HTTPS_ONLY=true`（生产默认值）。
- 验收：已完成。

#### R-002：默认认证和 Session Secret 仍存在弱配置风险

- 当前状态：已修复。生产环境由 `APP_ENV=production` 明确开启启动安全检查。
- 修复位置：`app/config.py`、`app/main.py`、`.env.example`、`README.md`、`tests/test_auth_smoke.py`
- 当前方案：生产启动拒绝默认密码、默认 Session Secret、空密码和少于 32 个字符的 Session Secret；错误提示只包含环境变量名和修复动作，不输出敏感值。开发和测试环境仍可显式使用测试凭据。
- 当前验证：同一认证回归命令 11 passed；覆盖默认凭据拒绝、短 Secret 拒绝和错误信息脱敏。
- 配置说明：`.env.example` 和 README 已补充 `APP_ENV`、`APP_SESSION_HTTPS_ONLY` 及生产凭据要求。
- 验收：已完成。

#### R-003：systemd 部署文件与仓库实际文件结构不一致

- 当前状态：已修复。新增真实的 `deploy/systemd/oci-object-bucket-browser.service`，README 改为使用同一文件，并固定单 Worker、非 root 用户、生产环境文件和本地状态目录。
- 修复位置：`deploy/systemd/oci-object-bucket-browser.service`、`app/routes.py`、`README.md`、`tests/test_auth_smoke.py`
- 当前方案：新增无需登录的 `/healthz` 健康入口；systemd 默认监听 `127.0.0.1:25103`，由 HTTPS 反向代理对外提供访问。
服务 `ProtectHome=read-only`，避免与默认 OCI 配置读取路径冲突；README 明确要求 `oci-browser` 可读 OCI 配置和私钥权限。
- 当前验证：同一认证回归命令 11 passed；脚本确认 service 文件存在、使用 `oci-browser`、单 Worker 和 `/etc/oci-object-bucket-browser.env`。
- 未验证部分：当前环境为 Windows，未执行 Linux systemd 实机启动；部署时仍需按 README 在目标主机执行 `systemctl` 和 `/healthz` 检查。
- 验收：仓库文件结构和文档一致；目标 Linux 主机启动验证待部署阶段完成。

### P1：近期完成

#### R-004：provider 和内部异常信息对外暴露过多

- 当前状态：已修复。路由层不再把 OCI、上传、下载、统计、分享、WebDAV、批量操作和后台任务的原始异常返回给客户端。
- 修复位置：`app/oci_client.py`、`app/routes.py`、`app/upload_tasks.py`、`tests/test_upload_routes.py`
- 当前方案：`public_storage_error` 和路由 `_safe_error` 统一生成稳定中文提示；日志只记录操作上下文、异常类型、分类和状态码，不记录异常原文、凭据、Authorization 或 token；批量结果保留对象名并使用安全摘要。
- 当前验证：`python -m pytest tests/test_upload_routes.py tests/test_stats_routes.py tests/test_share_routes.py tests/test_webdav_routes.py -q -p no:cacheprovider --basetemp=tmp/pytest-r004`，66 passed；新增 provider 错误消息不泄漏测试。
- 输入校验：批量下载请求体错误也改为固定提示，避免回显任意提交内容。
- 验收：已完成。

#### R-005：本地 JSON 状态存储不支持多进程协调

- 当前状态：已明确并保护单实例边界；本地 JSON 和进程内锁不宣称支持多进程或多副本。
- 修复位置：`app/config.py`、`app/main.py`、`README.md`、`deploy/systemd/oci-object-bucket-browser.service`、`tests/test_auth_smoke.py`
- 当前方案：生产启动检查 `APP_WORKERS`、`WEB_CONCURRENCY`、`UVICORN_WORKERS` 和 `GUNICORN_WORKERS`，发现大于 1 时拒绝启动；systemd 与手动启动文档均固定单 Worker。多实例需要独立共享状态架构。
应用进程无法可靠读取 `uvicorn --workers N` 的 CLI 参数；因此生产唯一支持入口是仓库 systemd unit，手动启动命令也固定 `--workers 1`。绕过这两个入口自行启动多 Worker/多副本仍属于不支持配置，不能把本地锁视为分布式协调。
- 当前验证：同一认证回归命令 11 passed；覆盖生产多 Worker 拒绝和健康入口。
- 部署边界：未引入伪分布式锁；跨进程/跨机器部署仍不支持。
- 验收：单实例边界已由启动检查和部署文件保护；共享状态架构不在本阶段范围内。

#### R-006：CSRF 同源校验对无 Origin / Referer 的客户端不兼容

- 当前状态：已明确并保持严格契约，不放宽浏览器 Session API。
- 当前方案：`POST`、`PUT`、`PATCH`、`DELETE` 状态变更必须携带与 `request.base_url` 同源的 `Origin` 或 `Referer`；无来源请求继续返回 `403`。WebDAV 使用独立 Basic Auth 并跳过浏览器 Session CSRF 校验。
- 当前验证：同一认证回归命令 11 passed；覆盖同源登录、无来源拒绝、外部跳转拒绝和健康入口。
- 程序化访问边界：当前没有复用 Web UI Session 的程序化 API；若后续需要，将单独设计 API 认证和 token/CSRF 契约，不按 User-Agent 放行。
- 验收：浏览器端和 WebDAV 边界已明确；无来源状态变更仍拒绝。

#### R-007：登录和公开分享接口缺少明确的速率限制策略

- 当前状态：已修复。新增线程安全的单实例内存失败限流器。
- 修复位置：`app/security.py`、`app/routes.py`、`tests/test_auth_smoke.py`、`tests/test_share_routes.py`、`tests/test_webdav_routes.py`
- 当前方案：登录、分享密码和 WebDAV Basic Auth 分别按来源地址计数；分享密码同时按分享 token 的 SHA-256 摘要隔离。默认 5 次失败 / 300 秒窗口，第 5 次失败后返回 `429` 和 `Retry-After`；成功认证清零对应计数。限流器不记录密码、token 或 Authorization 头。
- 部署边界：计数只存在当前进程，重启后清零；多实例部署必须在反向代理或共享限流存储层限流，不能依赖本实现。
- 当前验证：`python -m pytest tests/test_auth_smoke.py tests/test_share_routes.py tests/test_webdav_routes.py -q -p no:cacheprovider --basetemp=tmp/pytest-r007`，23 passed；覆盖连续失败、429、Retry-After 和成功路径不受错误计数污染。
- 验收：已完成。

### P2：持续安全维护

#### R-008：依赖和供应链检查

- 当前状态：已完成本轮依赖固定和漏洞扫描。
- 依赖变更：`requirements.txt` 固定 FastAPI `0.141.1`、Starlette `1.3.1`、python-multipart `0.0.32`、python-dotenv `1.2.3`、OCI SDK `2.184.1`、cryptography `50.0.0`、pyOpenSSL `26.4.0`、pytest `9.0.3`；其余依赖继续固定原版本。
- 升级原因：修复扫描发现的已知漏洞；OCI SDK 升级用于兼容 cryptography 50，FastAPI/Starlette 同步升级以使用无已知漏洞的 Starlette 版本。
- 当前验证：`python -m pip_audit -r requirements.txt` 输出 `No known vulnerabilities found`；`python -m pip check` 输出 `No broken requirements found`。
- 维护边界：后续依赖升级必须重复执行 `pip-audit` 和 `pip check`，并在本文件记录版本与原因；只从可信 PyPI 源安装。
- 验收：已完成。

#### R-009：安全响应头和内容策略

- 当前状态：基础响应头已完成；破坏性 CSP 暂不启用。
- 修复位置：`app/main.py`、`tests/test_auth_smoke.py`
- 当前方案：所有响应增加 `X-Content-Type-Options: nosniff`、`Referrer-Policy: strict-origin-when-cross-origin` 和限制摄像头/麦克风/地理位置的 `Permissions-Policy`；生产且 `APP_SESSION_HTTPS_ONLY=true` 时增加一年 HSTS。CSP 暂不直接启用，因为现有页面包含内联脚本、data URL 预览和 WebDAV 兼容路径，需单独迁移与真实客户端回归后再收紧。
- 当前验证：`python -m pytest tests/test_auth_smoke.py tests/test_share_routes.py tests/test_webdav_routes.py tests/test_server_proxy_upload.py -q -p no:cacheprovider --basetemp=tmp/pytest-r009`，36 passed；覆盖基础响应头、生产 HSTS、公开分享、WebDAV 和上传链。
- 未验证部分：当前环境未执行真实浏览器 CSP 兼容性审计；因此 CSP 仍保持未启用，HSTS 仅在明确 HTTPS 生产配置时发送。
- 验收：基础安全响应头已完成；CSP 兼容性审计保留为后续持续维护项。

#### R-010：上传资源边界

- 当前状态：已修复。直接 `/upload`、兼容 multipart 链和 server-proxy 链均执行单文件/单 chunk 边界；server-proxy staging 具备总量和未提交会话数量配额。
- 修复位置：`app/config.py`、`app/routes.py`、`app/temp_uploads.py`、`app/upload_tasks.py`、`app/upload_cleanup.py`、`.env.example`、`README.md` 及上传回归测试。
- 当前方案：默认单文件上限 5120 MB、单 chunk 上限 64 MB、staging 预留上限 20480 MB、活跃 staging / 后台任务上限 100，均可通过环境变量调整；staging 与任务创建在进程锁内原子预留，超限返回 `413`，磁盘写入不足返回 `507` 并回滚初始化 session。清理复用 staging 写锁，继续跳过 `queued`、`running`、`finalizing` 任务及其关联 staging/session。
- 当前验证：`python -m pytest tests/test_server_proxy_upload.py tests/test_upload_task_recovery.py tests/test_upload_cleanup.py tests/test_upload_routes.py -q -p no:cacheprovider --basetemp=tmp/pytest-review-final2`，88 passed；覆盖直接上传、空 staging 预留、文件/chunk/staging 配额、任务创建前配额拒绝、初始化磁盘失败回滚、原子预留、恢复和活跃任务清理保护。
- 部署边界：配额只保护单实例本地状态；多实例仍由 R-005 单实例约束阻止。
- 验收：已完成。

## 5. 实施顺序

### 阶段一：生产安全基线

1. 修复 Session Cookie Secure 配置。
2. 拒绝默认认证凭据和默认 Session Secret。
3. 统一生产启动检查和错误提示。
4. 补齐或修正 systemd 部署文件与 README。
5. 验证 HTTPS、反向代理和 Cookie 行为。

### 阶段二：错误边界与认证防护

1. 统一 provider 异常映射。
2. 增加登录、分享密码和 WebDAV 失败限流策略。
3. 补充错误响应安全测试。
4. 审计日志，确认没有凭据、Cookie、Authorization、token 或密码 hash。

### 阶段三：资源与部署边界

1. 增加上传总量、chunk、任务和 staging 配额。
2. 补充磁盘不足、清理失败和恢复异常场景。
3. 明确单实例部署约束。
4. 若需求升级到多实例，再单独设计共享状态和任务协调架构。

### 阶段四：持续安全维护

1. 依赖漏洞扫描。
2. 安全响应头和 CSP 兼容性审计。
3. 浏览器、WebDAV、分享链接和上传链路定期回归。
4. 每次安全相关改动同步更新本计划和验证记录。

## 6. 回归验收清单

### 认证与 Session

- [x] 默认密码部署被拒绝。
- [x] 默认 Session Secret 部署被拒绝。
- [x] HTTPS 下 Cookie 包含 `Secure`。
- [x] 登录成功后旧 Session 被清除。
- [x] 外部 `next_path` 不会跳转。
- [x] 登录、登出和设置操作具备同源校验。

### 对象路径与响应头

- [x] `..` 路径被拒绝。
- [x] 反斜杠路径被拒绝。
- [x] 批量删除和批量下载执行同样的路径校验。
- [x] 上传名执行路径校验。
- [x] Content-Disposition 文件名经过清理。
- [x] 错误响应不泄漏本地路径或 provider 原始异常。

### 上传与临时状态

- [x] 并发 chunk 写入后文件和 session 元数据一致。
- [x] 重复 chunk 可以幂等返回。
- [x] 不一致的重复 chunk 被拒绝。
- [x] 取消任务不会被异常处理覆盖为失败。
- [x] staging 总量和单任务资源限制已验证。
- [x] 多进程部署边界已明确并被启动检查保护。

### 公开分享与 WebDAV

- [x] 分享密码失败具备限流。
- [x] WebDAV Basic Auth 失败具备限流。
- [x] 分享 token、密码 hash 和 Authorization 头不进入日志。
- [x] WebDAV 只读模式继续阻止所有写操作。
- [ ] 安全响应头不会破坏对象预览和 WebDAV 客户端。

## 7. 当前验证基线

本轮修复后已验证：

```text
python -m pytest -q -p no:cacheprovider --basetemp=tmp/pytest-final4
141 passed

python -m compileall -q app tests
通过

node --check app/static/js/settings.js
node --check app/static/js/shares.js
node --check app/static/js/stats.js
node --check app/static/js/uploads.js
全部通过

python -m pip_audit -r requirements.txt
No known vulnerabilities found

python -m pip check
No broken requirements found
```

已有浏览器隔离审计覆盖 1440px、640px 和 360px 文件管理页面，使用长中文、Unicode 和长英文对象名验证布局；未发现横向溢出、图标覆盖、控制台错误或非预期请求。本轮新增安全头和 HSTS 通过 TestClient 路由回归验证；CSP 真实浏览器兼容性仍按 R-009 保持未启用。

## 8. 非本计划范围

以下事项不在本轮安全修复计划内，除非后续需求明确改变架构：

- 多租户权限体系；
- prefix 级细粒度授权；
- 分布式任务队列；
- 跨机器共享上传状态；
- 完整 WebDAV 锁和属性系统；
- 云盘级审计与合规报表；
- 通过重型前端框架重写现有页面。

## 9. 维护约束

后续安全改动必须遵守：

1. 先补充失败场景测试，再修改真实调用链。
2. 不通过 User-Agent、来源字符串或隐藏参数绕过认证和 CSRF。
3. 不在日志、错误响应、截图或测试数据中写入真实凭据。
4. 不把单实例本地锁描述成分布式一致性方案。
5. 每次改变认证、路径、上传、分享或 WebDAV 行为时，同时更新本文件的状态和验收项。

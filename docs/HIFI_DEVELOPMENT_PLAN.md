# 基于高保真的开发文档

来源：`reports/oci-object-bucket-browser-ui-20260726/`

状态：实施路线与验收基线；P1-P5 已完成首版实现，本文作为后续回归与维护基线。

当前实现状态（2026-07-26）：

- P1：共享应用壳、文件筛选 / 分页、上传工作台、详情侧栏与响应式导航。
- P2：独立上传任务页、状态筛选、轮询、重试 / 取消、失败详情与清理策略。
- P3：持久化设置、环境变量优先级、PBKDF2 WebDAV 密码、只读模式、回收站与批量数量确认。
- P4：token hash 分享模型、密码 / 过期 / 撤销 / 下载次数限制、管理 API、公开访问与导出。
- P5：同步存储统计与刷新 API；WebDAV Basic Auth、OPTIONS / PROPFIND / GET / PUT / DELETE / MKCOL / MOVE。
- 自动化证据（2026-07-26）：`pytest -q` 的 119 项测试通过，`compileall` 与前端 JavaScript 语法检查通过；隔离无头 Chrome 覆盖登录、文件、上传任务、分享、统计、设置和公开分享的桌面 / 移动明暗主题（28 张截图，未发现控制台错误、页面异常、失败请求或非预期 HTTP 状态）；rclone `lsf` 已通过临时 WebDAV 服务验证目录列举。

## 1. 目标

把现有 `oci-object-bucket-browser` 从轻量文件面板，升级为更完整的 OCI Object Storage 管理控制台。

目标体验：

- 左侧主导航承载核心模块
- 文件管理作为主工作台
- 上传任务独立可追踪
- 分享链接可创建、撤销和统计
- 存储统计可视化
- 设置页集中管理上传、认证、WebDAV、安全策略

## 2. 范围边界

### 2.1 本阶段纳入

- 新 UI 信息架构
- 文件页交互整理
- 上传任务页
- 分享页
- 存储统计页
- 设置页
- WebDAV 配置入口
- 回收站与危险操作策略
- API 契约草案
- 数据模型草案
- 开发切片顺序

### 2.2 本阶段未实现或暂不纳入

- 生产数据库迁移脚本
- 多租户与完整协同权限
- 分布式任务调度与跨实例抢占
- WebDAV 锁、属性写入等高级协议能力
- 回收站恢复页面与自动清理任务

分享链接当前使用随机 opaque token，并只保存 token hash；它不是 JWT 或可离线验证的签名 token。

## 3. 现有基础

项目当前已有能力：

- FastAPI
- Jinja2 Templates
- OCI Python SDK
- SessionMiddleware 登录保护
- 对象列表、前缀浏览
- 上传、下载、删除
- 批量下载 ZIP
- 文本 / 图片 / PDF 预览
- 图片缩略图
- server-proxy 上传链
- 服务端后台上传任务
- multipart 并行上传
- 上传恢复
- 临时文件清理
- systemd 部署

高保真设计应优先复用这些后端能力，不重复造上传链。

### 3.1 实现模块与所有权

- `app/routes.py`：HTTP 路由与用例编排；负责认证检查、请求校验、调用领域模块并组装响应，不持有长期业务状态。
- `app/settings_store.py`：设置 JSON 的唯一所有者；负责环境变量优先级、持久化和 WebDAV 凭据配置。
- `app/share_store.py`：分享记录、token hash、密码校验、撤销、过期和下载额度的唯一所有者。
- `app/trash_store.py`：回收站记录与 copy-record-delete 顺序的唯一所有者；Web UI 与 WebDAV 删除复用该策略。
- `app/file_browser.py`、`app/upload_dashboard.py`、`app/storage_stats.py`：无外部副作用的筛选、汇总和统计逻辑，可脱离 Web 页面独立测试。
- `app/webdav.py`：Basic Auth、路径映射、Destination 校验和 Multi-Status 生成等协议规则；具体 OCI 读写仍由路由编排并经统一只读策略保护。
- `app/security.py`：密码派生与恒定时间校验，不感知页面、存储或协议。

依赖方向保持为“路由编排 -> 领域规则 / 状态存储 -> OCI 客户端或本地文件副作用”。模板和前端脚本只消费 API / 页面上下文，不直接拥有服务端业务状态。

## 4. 页面结构

### 4.1 通用布局

建议继续使用服务端模板渲染，先不引入重型前端框架。

模板建议：

- `base.html`
  - 页面骨架
  - 左侧导航
  - 顶部用户区
  - 通用样式变量
- `index.html`
  - 文件管理页
- `uploads.html`
  - 上传任务页
- `shares.html`
  - 分享管理页
- `stats.html`
  - 存储统计页
- `settings.html`
  - 设置页

静态资源建议：

- `static/app.css`
- `static/app.js`
- `static/uploads.js`
- `static/shares.js`
- `static/settings.js`

### 4.2 导航路由

建议页面路由：

```text
GET /                  文件管理
GET /uploads           上传任务
GET /shares            分享管理
GET /stats             存储统计
GET /settings          设置
```

## 5. 文件管理页

### 5.1 页面组成

- 页面标题：文件管理
- 主操作：上传文件、新建文件夹、刷新
- 面包屑
- 搜索与筛选条
- 拖拽上传区
- 文件表格
- 批量操作条
- 分页
- 文件详情侧栏

### 5.2 推荐接口

现有接口可继续保留。若要适配高保真，可以整理为：

```text
GET    /api/objects?prefix=&query=&type=&size_min=&size_max=&page=&page_size=
POST   /api/folders
POST   /api/objects/rename
DELETE /api/objects
POST   /api/objects/batch-delete
POST   /api/objects/batch-download
GET    /api/objects/detail?key=
GET    /api/objects/preview?key=
GET    /api/objects/download?key=
```

### 5.3 前端状态

文件页至少维护：

- 当前 prefix
- 当前搜索词
- 类型筛选
- 大小筛选
- 当前页码
- 每页数量
- 已选择对象集合
- 当前详情对象
- 上传面板展开状态

## 6. 上传任务页

### 6.1 页面组成

- 上传中数量
- 今日上传量
- 失败待重试数量
- 上传任务列表
- 失败原因展示
- 任务操作入口

### 6.2 任务状态

建议统一状态枚举：

```text
queued
running
throttled
retrying
finalizing
completed
failed
cancelled
```

UI 展示可映射为：

- 等待中
- 上传中
- 限速中
- 重试中
- 收尾中
- 完成
- 失败
- 已取消

### 6.3 推荐接口

```text
GET  /api/server-uploads/tasks
GET  /api/server-uploads/tasks/{task_id}
POST /api/server-uploads/tasks/{task_id}/retry
POST /api/server-uploads/tasks/{task_id}/cancel
POST /api/server-uploads/cleanup
```

### 6.4 与现有上传链的关系

继续以 server-proxy 为默认上传链：

1. 浏览器上传到本服务 staging
2. 本服务创建后台任务
3. 后台任务上传到 OCI
4. 上传任务页展示最终入桶状态

文件页上传完成后，不应宣称“已入桶”。应提示：

> 已上传到服务器，正在后台入桶。最终状态请查看上传任务。

## 7. 分享管理

### 7.1 页面组成

- 有效分享链接数
- 今日访问次数
- 即将过期数量
- 分享列表
- 创建分享表单

### 7.2 数据模型草案

如果保持轻量实现，可先用 JSON 文件持久化；后续再迁移 SQLite。

```json
{
  "id": "share_xxx",
  "object_key": "research/2026/report.pdf",
  "token_hash": "...",
  "password_hash": null,
  "expires_at": "2026-07-29T12:00:00Z",
  "download_limit": 20,
  "download_count": 3,
  "created_at": "2026-07-26T12:00:00Z",
  "revoked_at": null
}
```

### 7.3 推荐接口

```text
GET    /api/shares
POST   /api/shares
DELETE /api/shares/{share_id}
GET    /s/{token}
POST   /s/{token}/verify-password
GET    /s/{token}/download
```

### 7.4 安全要求

- token 只保存 hash
- 密码只保存 hash
- 链接必须支持过期
- 链接必须支持撤销
- 下载次数到达限制后不可继续下载
- 分享下载不应暴露 OCI 凭据

## 8. 存储统计

### 8.1 统计项

- 对象总大小
- 对象数量
- 当前目录对象数量
- 近 7 日上传量
- 按类型分布

### 8.2 推荐接口

```text
GET  /api/stats/summary?prefix=
POST /api/stats/refresh
```

### 8.3 实现建议

第一版可以同步扫描当前 bucket 的对象列表。

若对象很多，后续再升级为：

- 后台统计任务
- 缓存最近统计结果
- 增量刷新
- 分 prefix 统计

## 9. 设置页

### 9.1 设置分组

- 认证与访问
- 对象存储
- 上传默认值
- 危险操作

### 9.2 设置项

认证与访问：

- Web UI 登录用户
- WebDAV Basic Auth 用户
- WebDAV Basic Auth 密码
- 只读模式

对象存储：

- Namespace
- Bucket
- Region
- Prefix Root

上传默认值：

- Chunk Size
- Parallelism
- Single PUT Threshold

危险操作：

- 启用回收站
- 批量删除二次确认
- 清理过期临时文件

### 9.3 推荐接口

```text
GET  /api/settings
POST /api/settings
POST /api/settings/validate
POST /api/maintenance/cleanup-expired-temp-files
```

### 9.4 配置保存策略

建议分层：

- `.env`：部署级固定配置
- 本地 settings JSON：UI 可编辑配置
- 环境变量优先级高于 UI 设置

敏感值不回显明文。

## 10. WebDAV

### 10.1 目标

让常见客户端可以把 bucket 当远程目录使用。

目标客户端：

- Cyberduck
- rclone
- macOS Finder，可选
- Windows 网络位置，可选

### 10.2 方法范围

第一版建议支持：

```text
OPTIONS
PROPFIND
GET
PUT
DELETE
MKCOL
MOVE
```

### 10.3 只读模式

只读模式开启后：

- 允许 OPTIONS / PROPFIND / GET
- 禁止 PUT / DELETE / MKCOL / MOVE

禁止时返回清晰错误。

### 10.4 路由建议

```text
/webdav/{path:path}
```

WebDAV 和 Web UI 登录态解耦，使用独立 Basic Auth。

## 11. 回收站

### 11.1 删除策略

若未启用回收站：

- 直接删除 OCI 对象

若启用回收站：

1. Copy Object 到 `.trash/{timestamp}/{original_key}`
2. 写入回收站记录
3. 删除原对象

### 11.2 回收站记录

```json
{
  "id": "trash_xxx",
  "original_key": "research/a.pdf",
  "trash_key": ".trash/20260726/research/a.pdf",
  "size": 123456,
  "deleted_at": "2026-07-26T12:00:00Z",
  "deleted_by": "web-ui"
}
```

### 11.3 后续可扩展

- 回收站页面
- 恢复对象
- 永久删除
- 自动清理过期回收站对象

## 12. 开发切片顺序

原计划分为 6 个切片；当前 P0-P5 已完成首版实现，以下条目用于回归核对和后续维护。

### P0：文档与接口冻结

- 功能清单确认
- 页面结构确认
- 接口命名确认
- 现有接口盘点

交付物：

- `docs/HIFI_FEATURE_LIST.md`
- `docs/HIFI_DEVELOPMENT_PLAN.md`

### P1：布局与文件页改造

- 左侧导航
- 顶部信息区
- 文件页表格视觉升级
- 面包屑
- 筛选栏
- 详情侧栏

验证：

- 登录后默认进入文件页
- 现有上传、下载、删除不回退
- 移动端布局不破

### P2：上传任务页

- 上传任务独立页面
- 任务列表
- 状态映射
- 清理完成任务
- 失败原因展示

验证：

- 上传大文件后能看到任务状态
- 服务重启恢复任务后状态仍可读

### P3：设置页

- 设置页面
- 上传默认值展示与保存
- 只读模式
- 危险操作配置

验证：

- 设置保存后重载仍存在
- 只读模式能阻止写操作

### P4：分享管理

- 分享模型
- 创建分享
- 撤销分享
- 访问统计
- 密码与次数限制

验证：

- 过期链接不可访问
- 撤销链接不可访问
- 达到下载次数后不可访问

### P5：存储统计与 WebDAV

- 统计页
- 类型占比
- WebDAV Basic Auth
- WebDAV 基础方法

验证：

- Cyberduck / rclone 可列目录
- 只读模式下 WebDAV 写操作失败

## 13. 验证清单

### 13.1 后端

- `pytest`
- 上传任务恢复测试
- 删除 / 批量删除测试
- 分享过期 / 撤销 / 次数限制测试
- 设置保存测试
- WebDAV Basic Auth 测试

### 13.2 前端 / 页面

- 登录页 smoke
- 文件页 smoke
- 上传任务页 smoke
- 分享页 smoke
- 统计页 smoke
- 设置页 smoke
- 移动端宽度 smoke

### 13.3 真实链路

- 小文件上传
- 大文件上传
- 上传后入桶确认
- 下载
- 批量下载
- 删除
- 回收站删除
- 创建分享并下载
- WebDAV 客户端列目录与上传

## 14. 风险点

- 分享链接需要谨慎处理 token 与密码，不能明文保存。
- WebDAV 当前实现为首版方法子集；锁、属性写入等高级协议能力仍需单独切片。
- 存储统计如果对象数量很大，同步扫描会慢，后续应加缓存。
- 回收站基于 copy + delete，会增加请求量和存储占用。
- 设置页若允许修改 bucket / namespace，必须避免误操作正式环境。
- 只读模式必须覆盖 Web UI API 与 WebDAV 两条入口。

## 15. 暂定不做事项

- 不引入 React / Vue 等重型前端框架。
- 不替换现有 server-proxy 上传主链。
- 不做多租户。
- 不做完整云盘协同权限。
- 不做分布式任务队列。
- 不做复杂目录树索引。

## 16. 实施后的维护入口

P1-P5 已接入真实路由调用链。继续开发相邻功能时，应以本文和 `HIFI_FEATURE_LIST.md` 为回归基线：

- 先补充对应的纯逻辑、存储契约或路由测试，再修改页面；
- 保持环境变量优先级、缓存键、持久化结构和现有 API 行为稳定；
- 涉及 UI 时补充桌面 / 移动与明 / 暗主题的真实浏览器验证；
- 同步更新 README、CHANGELOG 和本清单，避免把历史计划继续当作未实现需求。

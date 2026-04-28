# OCI Object Bucket Browser

一个轻量、可直接落地的 OCI Object Storage Web 前端。

它适合拿来做：
- 文件上传与下载
- 文件管理面板（前缀模拟目录）
- 对象列表浏览
- 文本 / 图片 / PDF 预览
- 单账号登录保护
- 服务端中转上传
- 大文件服务端 multipart 并行上传
- bucket 内对象的日常清理

## 主要功能

- 登录 / 登出
- 文件管理面板（前缀模拟目录）
- 对象列表浏览
- 按前缀过滤
- 新建文件夹（占位对象）
- 文件 / 目录重命名
- 文件 / 目录删除
- 文件上传
- 浏览器 -> 本服务 -> OCI 的服务端中转上传
- 服务端异步上传任务与任务状态查看
- 浏览器直传 OCI 测试入口（实验）
- 文件下载
- 单对象断点续传 / Range 下载
- 批量下载（当前结果多选后打包 ZIP，支持跳过失败项）
- 文本预览
- 图片预览
- PDF 预览
- 图片缩略图
- 文件类型图标
- 单对象删除
- 批量删除
- 批量下载
- 大文件分片上传
- 上传进度、速度、ETA
- 上传会话恢复
- 远端 multipart 对账恢复

## 技术栈

- FastAPI
- Jinja2 Templates
- OCI Python SDK
- Starlette SessionMiddleware
- itsdangerous

## 上传策略

当前项目有两条上传链：

### 默认上传链：`server-proxy`
这是首页现在默认使用的主链。

流程是：
1. 浏览器先把文件按 `APP_UPLOAD_PROXY_CHUNK_SIZE_MB` 分段传到本服务临时暂存目录
2. 浏览器提交 commit 请求
3. 服务端只负责校验 staging 完整性并创建后台上传任务
4. 前端在拿到 `task_id` 后，立刻把“前台上传”视为完成
5. 小文件走 `single-put-server-proxy`
6. 大文件走 `oci-multipart-server-proxy`
7. 服务端在后台并发上传分片到 OCI，并在完成后统一 commit multipart

特点：
- 前端不再直连 OCI multipart
- 首页上传进度只关注“浏览器 -> 本服务”这一段
- 浏览器传完后会立刻提示“已上传到服务器，正在后台入桶”
- 前端不会再等待 OCI 完成；浏览器 → 服务器阶段会优先显示可计算百分比、已上传大小、分段完成进度，若拿不到稳定实时上行速度，也会显示明确的“正在上传到服务器”状态，而不是长期停在“计算中”
- 更适合挂反代、鉴权、后续接个人网盘能力
- 便于后面扩展成真正的后台任务、队列、限速、审计
- 当前前端已经能看到任务状态
- 对象是否最终入桶，以“上传任务”列表状态为准

### 兼容保留链：浏览器 multipart 直传 `oci-multipart-browser-chunked`
旧的大文件 multipart 会话接口仍然保留：
- `/api/uploads/init`
- `/api/uploads/{upload_id}/part/{part_num}`
- `/api/uploads/{upload_id}`
- `/api/uploads/{upload_id}/complete`

这条链暂时没删，主要用于兼容已有实现与测试。

### 当前恢复 / 任务能力

server-proxy 后台任务现在采用“启动认领 / 重新入队”最小恢复语义：
- 服务启动时会扫描 `APP_UPLOAD_TASK_DIR` 里的任务状态文件
- 若任务仍是 `queued` / `running` / `finalizing`，且暂存文件仍在，就把它重新标成 `queued` 并重启后台执行线程
- 为避免同一进程内重复并发执行，同一个 `task_id` 若已存在存活线程，不会再次启动
- 对 multipart 任务，会优先读取本地 `upload_session` 已记录的 uploaded parts
- 若已有 `multipart_upload_id`，恢复时会再向 OCI 查询远端已存在 parts，尽量跳过已成功上传的分片
- 若任务其实只差最后 commit（例如崩在 `finalizing`），恢复后会直接进入 commit，不重复上传 parts
- 若暂存文件已丢失，则该任务会被标记为 `failed`，并写入恢复失败原因

默认 server-proxy 链当前具备：
- 浏览器分段上传到本站
- 服务端后台异步上传 OCI
- 服务端 multipart 并行上传
- 上传任务状态查询
- 最近任务列表查看
- 服务重启后自动扫描并恢复未完成后台任务（queued / running / finalizing）

兼容保留的旧 browser multipart 链仍具备：
- 上传会话恢复
- 已完成 part 跳过
- 与 OCI 远端 uploaded parts 对账

当前还不具备：
- 分布式任务队列
- 秒传 / 去重
- 跨机器共享上传状态

## 上传临时文件清理策略

当前提供一套“最小可用”的本地清理机制，目标是防止 `tmp/upload_staging`、`tmp/upload_tasks`、`tmp/upload_sessions` 越积越多，同时尽量不打断断点续传和重启恢复。

清理规则：

- 已完成任务
  - 成功入桶后，任务 JSON 会在 `APP_UPLOAD_COMPLETED_TASK_VISIBLE_SECONDS` 指定的短暂展示窗口后自动删除，避免 completed 任务长期占据“上传任务”列表
  - 超过 `APP_UPLOAD_CLEANUP_COMPLETED_RETENTION_HOURS` 后，cleanup 仍会继续删除已提交的 staging 元数据（`*.upload.json`）与关联的 multipart upload session JSON
  - 若该任务对应的 staging 临时文件还残留，也一并删除
- 已失败 / 已取消任务
  - 超过 `APP_UPLOAD_CLEANUP_FAILED_RETENTION_HOURS` 后，删除：
    - 任务 JSON
    - 仍残留的 staging 临时文件
    - 若任务关联了 multipart upload session，也一并删除
- 长时间未完成、且未提交的 staging 会话
  - 超过 `APP_UPLOAD_CLEANUP_STALE_STAGING_RETENTION_HOURS` 后，删除：
    - `APP_UPLOAD_TEMP_DIR` 下的暂存文件
    - 对应的 `*.upload.json` staging 元数据
- 活跃任务保护
  - 状态仍为 `queued` / `running` / `finalizing` 的任务不会被清理
  - 当前进程里仍有执行线程的任务不会被清理
  - 这些活跃任务关联的 staging 文件、staging 元数据、upload session 也会被跳过

触发方式：

- 自动（启动时）：如果 `APP_UPLOAD_CLEANUP_ENABLED=true` 且 `APP_UPLOAD_CLEANUP_STARTUP_ENABLED=true`，服务启动后会先做一次轻量清理
- 自动（定时）：如果 `APP_UPLOAD_CLEANUP_ENABLED=true` 且 `APP_UPLOAD_CLEANUP_SCHEDULER_ENABLED=true`，进程内会启动一个后台线程，按 `APP_UPLOAD_CLEANUP_INTERVAL_SECONDS` 周期重复执行同一套 cleanup 逻辑
- 手动：可调用

```http
POST /api/server-uploads/cleanup
```

返回里会列出本次删除或跳过的任务 / 文件，方便排查。

补充说明：

- 定时清理与手动清理共用同一个 `UploadCleanupService.run_once()`
- cleanup 内部带串行锁，同一时刻只会有一个 cleanup 在跑，避免手动触发与后台定时清理撞车
- 活跃任务保护在启动清理前会重新扫描一次任务状态与当前进程内存活线程，因此不会主动删除仍在运行 / 可恢复的 staging、task、upload session
- 服务关闭时会向后台清理线程发 stop signal，并等待一小段时间，尽量平滑退出

## 错误与重试

server-proxy 后台任务现在带一层最小可用重试：

- `single-put-server-proxy`：对可重试错误最多重试 3 次
- `oci-multipart-server-proxy`：每个分片对可重试错误最多重试 3 次
- 默认退避：1s -> 2s -> 最多 8s
- 若 OCI / 上游明确返回 `Retry-After`，优先按该等待时间退避（同样受最大 8s 上限保护）

分片失败时会按错误类型区分：

- `timeout`：可重试
- `connection`：可重试
- `http_5xx`：可重试
- `http_429`：可重试，并优先遵循服务端返回的等待时间
- `http_4xx`：通常不重试
- `unknown`：默认不重试

前端侧现在不再阻塞等待这些后台重试完成。
真正的入桶进度、重试中状态、最终失败原因，都在“上传任务”列表里看。

## 配置

先复制环境变量模板：

```bash
cp .env.example .env
```

然后填写：

```dotenv
OCI_CONFIG_PATH=~/.oci/config
OCI_PROFILE=DEFAULT
OCI_NAMESPACE=your_namespace
OCI_BUCKET_NAME=your_bucket_name
OCI_COMPARTMENT_ID=
OCI_PREVIEW_TEXT_LIMIT=20000
OCI_MAX_LIST_LIMIT=200

APP_AUTH_USERNAME=your_admin_username
APP_AUTH_PASSWORD=your_admin_password_here
APP_SESSION_SECRET=replace_with_a_random_long_session_secret
APP_SESSION_COOKIE_NAME=oci_bucket_browser_session
APP_UPLOAD_CHUNK_SIZE_MB=16
APP_UPLOAD_SINGLE_PUT_THRESHOLD_MB=32
APP_UPLOAD_PARALLELISM=6
APP_UPLOAD_SESSION_DIR=./tmp/upload_sessions
APP_UPLOAD_TASK_DIR=./tmp/upload_tasks
APP_UPLOAD_TEMP_DIR=./tmp/upload_staging
APP_UPLOAD_PROXY_CHUNK_SIZE_MB=8
APP_UPLOAD_CLEANUP_ENABLED=true
APP_UPLOAD_CLEANUP_STARTUP_ENABLED=true
APP_UPLOAD_CLEANUP_SCHEDULER_ENABLED=true
APP_UPLOAD_CLEANUP_INTERVAL_SECONDS=3600
APP_UPLOAD_COMPLETED_TASK_VISIBLE_SECONDS=1.0
APP_UPLOAD_CLEANUP_COMPLETED_RETENTION_HOURS=24
APP_UPLOAD_CLEANUP_FAILED_RETENTION_HOURS=72
APP_UPLOAD_CLEANUP_STALE_STAGING_RETENTION_HOURS=24
```

### 常用配置说明

- `OCI_NAMESPACE`：Object Storage namespace
- `OCI_BUCKET_NAME`：bucket 名称
- `APP_AUTH_USERNAME`：固定登录用户名
- `APP_AUTH_PASSWORD`：固定登录密码
- `APP_SESSION_SECRET`：session 签名密钥
- `APP_UPLOAD_CHUNK_SIZE_MB`：分片大小，默认 16 MB
- `APP_UPLOAD_SINGLE_PUT_THRESHOLD_MB`：单请求上传阈值，默认 32 MB
- `APP_UPLOAD_PARALLELISM`：服务端上传 OCI 的并发分片数，默认 6
- `APP_UPLOAD_SESSION_DIR`：旧 browser multipart 恢复会话目录
- `APP_UPLOAD_TASK_DIR`：服务端上传任务状态目录
- `APP_UPLOAD_TEMP_DIR`：浏览器传到本站后的临时暂存目录
- `APP_UPLOAD_PROXY_CHUNK_SIZE_MB`：浏览器到本站的分段大小，默认 8 MB
- `APP_UPLOAD_CLEANUP_ENABLED`：是否启用上传临时文件清理总开关，默认 `true`
- `APP_UPLOAD_CLEANUP_STARTUP_ENABLED`：服务启动时是否做一次轻量清理，默认 `true`
- `APP_UPLOAD_CLEANUP_SCHEDULER_ENABLED`：是否启用进程内定时清理线程，默认 `true`
- `APP_UPLOAD_CLEANUP_INTERVAL_SECONDS`：后台定时清理间隔，默认 `3600` 秒
- `APP_UPLOAD_COMPLETED_TASK_VISIBLE_SECONDS`：成功入桶后 completed 任务在任务列表里继续短暂展示多久再自动删除，默认 `1.0` 秒
- `APP_UPLOAD_CLEANUP_COMPLETED_RETENTION_HOURS`：已完成任务保留多久后再清理任务元数据 / staging 元数据 / upload session，默认 24 小时
- `APP_UPLOAD_CLEANUP_FAILED_RETENTION_HOURS`：失败或取消任务保留多久后再清理任务元数据和残留 staging 文件，默认 72 小时
- `APP_UPLOAD_CLEANUP_STALE_STAGING_RETENTION_HOURS`：未提交、长期无更新的 staging 会话保留多久后清理，默认 24 小时

## 本地运行

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)
uvicorn app.main:app --host 0.0.0.0 --port 25103
```

访问：

- <http://127.0.0.1:25103>

## 部署建议

如果前面还有反代或 Cloudflare，建议：

- 上传域名尽量 DNS only
- 单个 chunk 不要太大
- 反代开启更长超时
- `proxy_request_buffering off`

推荐起步参数：

- `APP_UPLOAD_PROXY_CHUNK_SIZE_MB=8`
- `APP_UPLOAD_CHUNK_SIZE_MB=16`
- `APP_UPLOAD_PARALLELISM=4` 或 `6`
- `APP_UPLOAD_SINGLE_PUT_THRESHOLD_MB=16` 或 `32`

## 文件管理能力说明

当前文件管理层坚持“最小可用”，不引入数据库，直接复用 Object Storage 现有对象列举与对象操作能力。

### 目录语义

OCI Object Storage 本身没有真实目录，这里采用“前缀 + `/`”模拟：

- `docs/a.txt` 视为 `docs/` 目录下的文件
- `docs/sub/b.txt` 会在 `docs/` 下显示出 `sub/` 子目录
- 新建文件夹时会额外写一个零字节占位对象，例如 `docs/new-folder/`
- 即使没有占位对象，只要某个前缀下存在对象，也会在 UI 里显示成目录

### 上传页当前交互说明

- 点击上传后，页面进度条只代表“浏览器 -> 本服务”暂存进度
- 这一段完成后，页面会立即显示：文件已上传到服务器，正在后台入桶
- 此时上传按钮会恢复可用，用户可以继续别的操作
- 后台任务列表会继续异步刷新，展示 queued / running / retrying / failed；成功完成的任务只会短暂保留，确认对象已成功入桶后会自动从任务 store 删除，所以下一次刷新通常就不会再看到 completed 任务长期挂在列表里
- 文件列表不会在 staging 完成时立刻刷新；只有后台真正入桶后，再手动刷新文件列表或等待后续页面刷新时才能看到对象

### 当前提供的最小文件管理操作

首页文件管理面板支持：

- 浏览当前目录下的“子目录 + 文件”
- 面包屑导航，可逐级点击回到上层目录
- 当前目录提示，明确显示上传 / 新建文件夹 / 重命名默认作用在哪个目录
- 返回上级目录
- 新建文件夹
- 文件重命名
- 目录重命名（通过复制所有对象到新前缀后删除旧对象实现）
- 文件删除
- 目录删除（删除该前缀下全部对象）
- 文件批量下载 / 批量删除

### 同名冲突保护（默认开启）

当前对以下操作增加了“先检测冲突，再确认覆盖”的最小保护：

- 上传到当前目录
- 新建文件夹
- 文件重命名
- 目录重命名

默认规则：

- 文件上传：若目标对象名已存在，返回 `409`，默认不直接覆盖
- 新建文件夹：若同名目录占位对象已存在，或同名前缀下已存在对象，返回 `409`
- 文件重命名：若目标文件已存在，返回 `409`
- 目录重命名：若目标前缀下已存在任何对象，返回 `409`

前端收到 `409 conflict` 后会：

- 在当前页面明确提示冲突目标
- 列出目标路径
- 再次向用户确认
- 用户确认后，带 `overwrite=true` 重试同一个 API

注意：

- 这里优先挡住“明显会误覆盖”的情况，不引入数据库或复杂事务
- 目录重命名的覆盖确认是“整前缀级别”的，一旦确认，现有实现会继续执行复制 + 删除
- 目录删除当前不做额外 confirm token，只保留原有前端确认框

### API

新增了几组简单 API：

- `GET /api/files?prefix=docs/`
  - 返回当前前缀下拆分后的 `folders` 和 `files`
  - 同时返回 `breadcrumbs`、`current_directory_label`、`parent_prefix`，供首页文件管理面板显示当前目录状态
- `POST /api/files/folders`
  - 请求体：`{"prefix":"docs/","folder_name":"new-folder"}`
  - 冲突确认重试：`{"prefix":"docs/","folder_name":"new-folder","overwrite":true}`
  - 通过写入占位对象创建目录
- `POST /api/files/rename`
  - 请求体：`{"source_path":"docs/a.txt","new_name":"b.txt"}`
  - 或：`{"source_path":"docs/","new_name":"archive"}`
  - 冲突确认重试时追加：`"overwrite": true`
- `POST /api/files/delete`
  - 请求体：`{"path":"docs/"}` 或 `{"path":"docs/a.txt"}`

当发生冲突时，上述写接口会返回：

- HTTP `409`
- JSON 里包含 `detail`
- `conflict.action / conflict.kind / conflict.destination_path / conflict.existing_paths`
- `overwrite_allowed=true`

### 与上传链的衔接

- server-proxy 上传仍沿用原有命名规则：`object_name_from_upload(filename)`
- 现在首页上传会自动带上当前目录前缀
  - 例如你在 `docs/2026/` 目录里上传 `a.txt`
  - 最终对象名会落成 `docs/2026/a.txt`
- 文件管理面板顶部会同步显示当前目录提示与面包屑，减少多层前缀操作时的误判
- 上传完成后前端会刷新当前文件管理面板，因此新文件能直接出现

## 下载能力说明

### 单对象下载

单对象下载端点现在支持：

- `Accept-Ranges: bytes`
- `Range: bytes=start-end`
- `Range: bytes=start-`
- `Range: bytes=-suffix`
- 返回 `206 Partial Content`
- 非法或暂不支持的 Range 会返回 `416`

这意味着：
- 浏览器断点续传兼容性更好
- 外部下载器可以利用 Range 做续传
- 支持多线程下载的下载器，通常也能基于这个端点自行做分段拉取

当前边界：
- 目前只支持**单段 Range**
- 不支持一个请求里返回 multipart/byteranges 多段内容

### 批量下载策略

当前采用更稳的浏览器原生下载触发方案：

- 在当前对象列表里直接多选
- 点击“下载所选”
- 前端改为隐藏 form POST 到批量下载端点
- 浏览器原生接管这次下载响应，不再先把整个 ZIP 读进前端 blob 再 `createObjectURL`
- 服务端仍临时把所选对象打成一个 ZIP 并直接返回

现在批量下载新增了容错模式：

- 某个对象读取失败时，不再直接让整次打包失败
- 成功对象会继续写入 ZIP
- 若存在失败项，ZIP 内会附带：
  - `_batch_download_failures.json`
  - `_batch_download_failures.txt`
- 响应头也会带：
  - `X-Batch-Requested-Count`
  - `X-Batch-Archived-Count`
  - `X-Batch-Failed-Count`
  - `X-Batch-Partial`

这样做的好处是：
- 不用引入额外前端打包依赖
- 不需要浏览器一次弹很多下载
- 能保留对象原始路径结构
- 用户能实际拿到“成功部分”，不会因为一个坏对象整包作废
- 前端不再持有整包 ZIP blob，较大文件时内存压力更小
- 下载触发更依赖浏览器原生处理链，兼容性通常更稳
- 失败信息也能被用户现实地看到

当前边界：
- 批量 ZIP 仍是一次动态生成流，不适合做真正意义上的多线程断点下载
- 若浏览器中途断掉，通常还是要重新生成这一份 ZIP
- 超大批量下载暂时还没有做后台任务化

## 关键 API

- `POST /api/server-uploads/cleanup`：手动执行一次上传临时文件清理

### 默认 server-proxy 上传链

- `POST /api/server-uploads/init`
  - 初始化一次浏览器 -> 本服务的暂存上传
  - 现在支持基于 `file_fingerprint` 复用未完成的暂存上传会话
  - 默认也会先检查目标对象是否已存在；若存在则返回 `409 conflict`
  - 用户确认覆盖时可在请求体追加 `overwrite: true`
  - 返回 `temp_upload_id`、`upload_url`、策略信息，以及 `uploaded_chunks` / `missing_chunks`

- `GET /api/server-uploads/staging/{temp_upload_id}`
  - 查询该暂存上传会话当前已收哪些 chunk、还缺哪些 chunk、是否已 commit

- `PUT /api/server-uploads/staging/{temp_upload_id}?chunk_index=N[&chunk_sha256=...]`
  - 上传一段浏览器分片到服务端暂存文件
  - 若该 chunk 已存在且 `sha256 + size` 一致，则返回 `already_uploaded=true`
  - 若该 chunk 已存在但内容不一致，则返回 `409`，避免重复块覆盖破坏暂存状态

- `POST /api/server-uploads/commit?temp_upload_id=...`
  - 通知服务端基于暂存文件创建后台上传任务
  - commit 前会校验缺失 chunk 与暂存文件大小
  - commit 前也会再次检查目标对象冲突，避免初始化后到 commit 之间被别人抢先写入
  - 用户确认覆盖时可在请求体追加 `overwrite: true`

- `GET /api/server-uploads/tasks`
  - 查看最近上传任务
  - 现在会额外返回更适合前端直显的任务状态字段：
    - `current_phase`：原始 phase，便于程序判断
    - `phase_label`：人类可读阶段文案
    - `recovered`：该任务当前是否处于“恢复后重新入队”语义
    - `recovery_attempted`：是否发生过恢复相关处理
    - `recovery_source_status`：若是恢复重排队，记录恢复前状态
    - `recovery_problem`：恢复失败类型，当前可能为 `missing_temp_file`
    - `status_label`：人类可读状态文案
    - `is_retrying`：当前是否处于后台重试中
    - `retry_count`：当前这轮已重试几次
    - `retry_attempt`：当前重试序号
    - `retry_max_attempts`：当前重试上限
    - `retry_kind`：重试类型，可能为 `single_put` 或 `part`
    - `retry_part_num`：若为 multipart 分片重试，给出分片号
    - `retry_label`：适合直接显示的重试说明文案
    - `last_error`：最近一次失败原因

- `GET /api/server-uploads/tasks/{task_id}`
  - 查看单个任务详情和进度
  - 同样包含上述 `phase / recovery / retry` 可读字段

- `DELETE /api/server-uploads/tasks/{task_id}`
  - 请求取消任务

### 兼容保留的旧 multipart API

- `POST /api/uploads/init`
- `PUT /api/uploads/{upload_id}/part/{part_num}`
- `GET /api/uploads/{upload_id}`
- `POST /api/uploads/{upload_id}/complete`
- `DELETE /api/uploads/{upload_id}`

## 浏览器 → 本站断点续传（当前最小实现）

当前默认 `server-proxy` 上传链已经支持最小可用断点续传：

- 前端在初始化暂存上传时会带上 `file_fingerprint`
  - 当前采用：`name + size + type + lastModified`
- 服务端会在 `APP_UPLOAD_TEMP_DIR` 下为每个 `temp_upload_id` 旁边保存一个 `.upload.json` 元数据文件
- 元数据里记录：
  - 文件基础信息
  - chunk 大小
  - 已上传 chunk 索引
  - 每个 chunk 的 `size` 与 `sha256`
  - 是否已经 commit
- 浏览器刷新后：
  - 前端从 `localStorage` 取回最近一次未完成上传的 `temp_upload_id + file metadata`
  - 用户重新选择同一个文件后，前端会请求服务端状态
  - 若 fingerprint 一致，就提示“可恢复上传”
  - 再次点击上传时，只补传 `missing_chunks`

### 当前恢复判定

恢复依赖两个条件：

1. 前端本地还保留了 `localStorage` 里的最近上传状态
2. 用户重新选择的文件，其 `name / size / type / lastModified` 与之前一致

这意味着它是“简单恢复”，不是强指纹秒传，也不是跨浏览器恢复。

### 当前限制

- 只保证同一浏览器、本地 `localStorage` 尚在时的简单恢复
- 若用户换浏览器、清掉 localStorage、或者系统拿不到稳定 `lastModified`，前端不会自动提示恢复
- 现在只做了“重复 chunk 幂等跳过”，没有做整文件级别秒传
- 暂存会话 commit 后不会自动清理 `.upload.json`，而是标记 `committed=true`
- 浏览器侧 chunk sha256 在提交前现算，超大文件时会增加一点点前端 CPU 开销，但实现简单、边界更稳

## 自检

### 后台任务重启恢复覆盖范围

当前最小可用覆盖：
- `queued`：重启后会重新入队并执行
- `running`：会基于本地 session / 远端 multipart parts 尽量续传剩余分片
- `finalizing`：若 parts 已齐，会直接重新 commit multipart

首页“最近上传任务”卡片现在会直接展示：
- 是否为恢复出来的任务（显示“恢复任务”标签）
- 当前恢复后的阶段说明，例如“服务重启后已恢复，原状态：上传中 / 完成提交中”
- 恢复失败是否因为暂存文件缺失（显示“暂存文件丢失”标签）
- 后台是否正在重试（显示“后台重试中”标签）
- 当前已重试几次
- 最近一次失败原因
- 保留原始 `current_phase` 给 API 消费方，同时给前端用 `phase_label / status_label / retry_label` 直显

当前边界：
- 仍是单机进程内线程模型，不做跨实例抢占协调
- 若进程崩溃时某个 part 正在上传，恢复后可能安全重传该 part，一 OCI 端以 part_num + etag 最终 commit 结果为准
- 恢复依赖 `APP_UPLOAD_TASK_DIR`、`APP_UPLOAD_SESSION_DIR`、`APP_UPLOAD_TEMP_DIR` 三处状态仍可读

```bash
python3 -m compileall app tests
. .venv/bin/activate && pytest -q
```

## systemd 示例

```ini
[Unit]
Description=OCI Object Bucket Browser
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/path/to/oci-object-bucket-browser
EnvironmentFile=/path/to/oci-object-bucket-browser/.env
ExecStart=/path/to/oci-object-bucket-browser/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 25103
Restart=always
RestartSec=2
User=root

[Install]
WantedBy=multi-user.target
```

- 不会侵入现有正式上传链

它和现有上传链的关系：
- 正式上传链保持原样：小文件仍走 `/upload`，大文件仍走现有的 multipart 会话 + part 上传 + complete
- 实验入口只是额外加的一张卡片，用来做手工测速和可行性验证
- 删除、批量下载、正式上传恢复链都不受影响

已知限制 / 风险：
- 目前实验入口是单次 PUT，不支持浏览器端 multipart、断点续传、失败后续传
- PAR 在有效期内对目标对象名有写权限，因此有效期不宜设太长
- 若目标对象已存在，这次 PUT 会覆盖同名对象
- 浏览器直传是否能跑通，受 OCI endpoint 可达性和 CORS 配置影响较大

## 后续方向

- 删除体验继续优化
- 上传可靠性继续加固
- 批量下载
- 重命名对象
- 目录树视图
- 更强的预览能力
- 多用户权限体系

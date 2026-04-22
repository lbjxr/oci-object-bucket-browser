# oci-object-bucket-browser

一个轻量、可直接落地的 OCI Object Storage Web 前端。

它提供：
- 文件上传
- 对象列表浏览
- 文件下载
- 文本 / 图片 / PDF 预览
- 固定账号密码登录保护
- 移动端友好的浅蓝风格界面
- 图片对象缩略图、其他文件类型图标
- 对象删除（单对象）
- 大文件分片上传、并发上传、上传会话恢复

## 技术栈

- FastAPI
- Jinja2 Templates
- OCI Python SDK
- Starlette SessionMiddleware
- itsdangerous

## 适用场景

适合需要一个简单 bucket 管理页的场景，比如：
- 个人对象存储文件浏览
- 小团队内部临时文件站
- 自用的 OCI Object Storage 轻量面板
- 给后续更复杂前端做 MVP 验证

## 当前功能

- 登录 / 登出
- 首页对象列表
- 按前缀过滤对象
- 上传对象到 bucket
- 下载对象
- 文本预览
- 图片预览
- PDF 预览
- 图片对象右侧缩略图
- 非图片对象显示类型图标
- 大文件自动切片上传
- 前端显示实时速度与 ETA
- 上传完成后的友好成功提示

## 目录结构

```text
.
├── app/
│   ├── config.py
│   ├── main.py
│   ├── models.py
│   ├── oci_client.py
│   ├── routes.py
│   ├── upload_sessions.py
│   ├── utils.py
│   └── templates/
├── tests/
├── .env.example
├── .gitignore
├── README.md
└── requirements.txt
```

## 环境要求

- Python 3.11+
- 本机已可用的 OCI CLI / SDK 配置（默认 `~/.oci/config`）
- 一个可访问的 OCI Object Storage bucket

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
```

### 配置说明

- `OCI_CONFIG_PATH`：OCI 配置文件路径
- `OCI_PROFILE`：OCI profile 名
- `OCI_NAMESPACE`：Object Storage namespace
- `OCI_BUCKET_NAME`：bucket 名称
- `OCI_COMPARTMENT_ID`：可选，当前版本未强依赖
- `OCI_PREVIEW_TEXT_LIMIT`：文本预览最大截取长度
- `OCI_MAX_LIST_LIMIT`：单次对象列表上限
- `APP_AUTH_USERNAME`：固定登录用户名
- `APP_AUTH_PASSWORD`：固定登录密码
- `APP_SESSION_SECRET`：session 签名密钥，务必改成随机长串
- `APP_SESSION_COOKIE_NAME`：session cookie 名称
- `APP_UPLOAD_CHUNK_SIZE_MB`：大文件分片大小，默认 16 MB
- `APP_UPLOAD_SINGLE_PUT_THRESHOLD_MB`：小于该阈值时仍用单请求直传，默认 32 MB
- `APP_UPLOAD_PARALLELISM`：前端最多同时上传多少个分片，默认 6
- `APP_UPLOAD_SESSION_DIR`：上传会话元数据保存目录，用于恢复未完成上传

## 上传策略说明

当前版本采用两档上传策略：

1. 小文件：`single-put`
   - 小于 `APP_UPLOAD_SINGLE_PUT_THRESHOLD_MB` 时，前端继续使用单请求上传。
   - 优点是简单，代码路径短。
   - 适合图片、文档、小压缩包。

2. 大文件：`oci-multipart-browser-chunked`
   - 浏览器把文件切成固定大小分片。
   - 每个分片单独请求到 FastAPI。
   - FastAPI 直接调用 OCI Python SDK 的 multipart 接口，把每个分片写入 OCI Object Storage。
   - 全部分片上传完成后，再由服务端调用 `commit_multipart_upload` 合并。

### 这版是否真的支持断点续传

是，但属于**轻量可恢复**，不是完整意义上的秒传型断点续传。

当前实现支持：
- 浏览器刷新后重新选择同一个文件，可根据文件名、大小、时间戳和首段哈希恢复同一个上传会话
- 服务端会记录已经成功上传的 part number 和 etag
- 前端重新发起时会跳过已完成分片，只补传剩余分片
- 上传过程中支持取消，取消时会主动 abort OCI multipart upload

当前**不支持**：
- 跨机器 / 跨浏览器共享恢复状态
- 服务端重启后仍 100% 保证恢复 OCI 远端状态并重新对账每个 part
- 基于完整文件哈希的“秒传”
- 更复杂的按错误类型区分重试策略调优（当前已提供基础自动重试）

换句话说，它已经能覆盖“浏览器卡住、页面刷新、代理中断后重新进来继续传”的主要落地场景，但还不是对象存储网盘级别的完整上传引擎。

### 并发 / 分段实现方式

- 分片大小：默认 16 MB，可通过 `APP_UPLOAD_CHUNK_SIZE_MB` 调整
- 并发数：默认 6，可通过 `APP_UPLOAD_PARALLELISM` 调整
- 分片编号：从 1 开始，对应 OCI multipart part number
- 会话持久化：保存在 `APP_UPLOAD_SESSION_DIR`
- 完成条件：所有 part 上传成功后，服务端统一 commit

### 为什么这样设计

这是在现有 `FastAPI + OCI Python SDK` 架构下最现实、最稳、维护成本最低的方案：

- 不需要把 OCI 凭证下放到浏览器
- 不需要引入消息队列、Celery、Redis 才能先跑起来
- 不需要把整个大文件先吃进 FastAPI 再转传
- 单个 HTTP 请求只处理一个 chunk，更容易绕过代理超时和 body 限制
- 兼容现在的登录和页面结构，前端只是把上传入口从“整文件 XHR”升级成“会话化分片上传”

## 安装

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## 本地运行

```bash
. .venv/bin/activate
export $(grep -v '^#' .env | xargs)
uvicorn app.main:app --host 0.0.0.0 --port 25103
```

打开：

- <http://127.0.0.1:25103>

## 登录逻辑

- 未登录访问首页、上传、下载、预览时，会跳转到 `/login`
- 登录成功后使用 session cookie 维持状态
- 当前版本为固定单账号模式，不包含多用户系统

## 预览与缩略图

- 文本文件：直接文本预览
- 图片文件：支持图片预览，并在列表页显示缩略图
- PDF 文件：支持 PDF 预览
- 其他文件：不做内容预览，列表页显示类型图标

## 大文件上传与反向代理 / Cloudflare 建议

如果你是通过域名访问这个页面，真正卡人的通常不是 OCI，而是前面的代理层。

### 推荐原则

优先顺序通常是：

1. **最好**：上传域名直连源站，不走 Cloudflare 代理
2. **其次**：保留 Cloudflare，但把上传域名设为 DNS only（灰云）
3. **再次**：继续走代理，但把单次请求体控制在代理可接受范围内，并适当降低 chunk 大小

### Cloudflare 的现实限制

Cloudflare 代理模式下，请求体大小和超时都可能卡住大文件上传。即便浏览器还在传，边缘代理也可能先断开。

所以推荐：

- 给上传站点单独开一个子域名，例如 `upload.example.com`
- 这个子域名改成 **DNS only**，不要走橙云代理
- Nginx / Caddy / Traefik 直接反代到 FastAPI
- 保证源站允许较长请求时间和足够大的 request body

### 推荐的访问方式

#### 方案 A：上传专用域名不走 Cloudflare 代理

最推荐。

好处：
- 避开 Cloudflare 上传限制
- ETA 更稳定
- 分片上传更不容易被中途切断

#### 方案 B：管理页走 Cloudflare，上传入口走独立 DNS only 子域名

这是很实用的折中：
- `files.example.com`：继续走 Cloudflare，负责浏览、预览、下载
- `upload.example.com`：DNS only，只负责大文件上传

如果后续要继续进化，可以把前端 API base URL 单独抽成配置，让上传请求直接打到 upload 子域名。

### Nginx 示例

```nginx
server {
    listen 443 ssl http2;
    server_name upload.example.com;

    client_max_body_size 128m;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    send_timeout 3600s;

    location / {
        proxy_pass http://127.0.0.1:25103;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_request_buffering off;
    }
}
```

说明：
- `client_max_body_size` 不需要设成超大，因为现在是按 chunk 上传；只要高于单个 chunk 即可
- 若 chunk 是 64 MB，Nginx 可以设成 `128m`，留足余量
- `proxy_request_buffering off` 对流式 / 大请求更友好

### Caddy 示例

```caddy
upload.example.com {
    reverse_proxy 127.0.0.1:25103 {
        flush_interval -1
    }

    request_body {
        max_size 128MB
    }
}
```

### 参数怎么调更稳

如果前面还有代理层，建议从下面这个组合开始：

- `APP_UPLOAD_CHUNK_SIZE_MB=16`
- `APP_UPLOAD_PARALLELISM=4` 或 `6`
- `APP_UPLOAD_SINGLE_PUT_THRESHOLD_MB=16` 或 `32`

如果是直连源站、网络也稳定，可以再提高：

- `APP_UPLOAD_CHUNK_SIZE_MB=32`
- `APP_UPLOAD_PARALLELISM=6` 或 `8`

不是 chunk 越大越好。chunk 太大时，单个分片失败成本也会更高。

## 2026-04-22 上传优化实测

当前默认参数：

- `APP_UPLOAD_CHUNK_SIZE_MB=16`
- `APP_UPLOAD_PARALLELISM=6`
- `APP_UPLOAD_SINGLE_PUT_THRESHOLD_MB=32`

本次优化包含：

- 默认分片从 `64 MB` 下调到 `16 MB`
- 默认并发从 `4` 提高到 `6`
- multipart 分片上传从 `fetch` 改为 `XMLHttpRequest`
- 前端速度显示改为最近 `3 秒` 滑动窗口平均，避免瞬时峰值过度夸大
- 失败分片支持最多 `3` 次自动重试，并带简单退避
- 修复上传会话并发写入导致的分片状态丢失问题

实测样本：

1. `1.3.5.SP1补丁包.rar`
   - 文件大小：`382,938,704 bytes`（约 `365.2 MiB`）
   - 参数：`16 MiB chunk` + `6 并发`
   - 从 `init` 到 `complete`：约 `130 秒`
   - 整段平均速度：约 `2.95 MB/s`（约 `2.81 MiB/s`）
   - 进入稳定上传阶段后：约 `3.5 MB/s` 左右，峰值可到 `4 MB/s`

2. 失败会话恢复验证
   - 同一文件上传失败后，再次选择同一文件，可只补传缺失分片
   - 已验证可只续传缺失分片并成功完成合并

说明：

- 前端展示的“速度”现在是最近几秒的平滑上传速率，更适合看当前体感
- 若要核算真实平均吞吐，应优先按日志里的 `init → complete` 总耗时计算

### 失败分片自动重试

- 仅失败的分片会重试，不会影响其他已成功分片
- 默认最多重试 `3` 次
- 重试前有简单线性退避（约 `0.8s / 1.6s / 2.4s`）
- 页面状态会显示当前失败分片与重试次数
- 若最终仍失败，会保留清晰的失败提示，且已上传分片状态不会丢

## 自检

```bash
python3 -m compileall app tests
pytest -q
```

如果只想做一次最小烟雾测试：

```bash
python3 - <<'PY'
import os
from fastapi.testclient import TestClient
from app.config import get_settings
from app.main import create_app

os.environ['APP_AUTH_USERNAME'] = 'test-admin'
os.environ['APP_AUTH_PASSWORD'] = 'test-password-for-smoke'
os.environ['APP_SESSION_SECRET'] = 'test-session-secret-for-smoke'
get_settings.cache_clear()
client = TestClient(create_app())
assert client.get('/', follow_redirects=False).status_code == 303
assert client.post('/login', data={'username': 'admin', 'password': 'secret123', 'next_path': '/'}, follow_redirects=False).status_code == 401
print('smoke-ok')
PY
```

## systemd 部署示例

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

## Changelog 与项目状态

- 版本变更记录：`CHANGELOG.md`
- 当前状态与后续路线：`docs/PROJECT_STATUS_AND_ROADMAP.md`

## 后续可扩展方向

当前下一步任务优先级里，**删除功能继续优化**排在最前面。

- 删除功能继续优化（更清晰反馈、更顺手的清理流程）
- 把上传状态改成 Redis / DB 存储，支持多实例共享恢复
- 在服务端补 `list_multipart_upload_parts` 对账逻辑，增强重启后的恢复能力
- 为单个 chunk 增加自动 retry / 指数退避
- Public / private 分享链接
- PAR 支持
- 重命名对象
- 目录树视图
- 批量上传 / 批量下载
- 更强的预览能力和文件类型识别
- 多用户权限体系

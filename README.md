# OCI Object Bucket Browser

一个轻量、可直接落地的 OCI Object Storage Web 前端。

它适合拿来做：
- 文件上传与下载
- 对象列表浏览
- 文本 / 图片 / PDF 预览
- 单账号登录保护
- 大文件分片上传与恢复
- bucket 内对象的日常清理

## 主要功能

- 登录 / 登出
- 对象列表浏览
- 按前缀过滤
- 轻量目录导航（面包屑 / 返回上级 / 当前层级子目录）
- 文件上传
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

当前采用两档上传：

### 小文件：`single-put`
- 低于 `APP_UPLOAD_SINGLE_PUT_THRESHOLD_MB` 时直传
- 路径短，适合图片、文档、小压缩包

### 大文件：`oci-multipart-browser-chunked`
- 自动切成固定大小分片
- 前端并发上传分片
- 服务端调用 OCI multipart 接口
- 所有分片完成后统一合并

### 断点恢复能力

这是**轻量可恢复**，不是秒传。

支持：
- 刷新后重新选择同一文件继续上传
- 跳过已完成分片
- 重新进入页面后恢复上传状态
- 服务重启后继续恢复
- 恢复时与 OCI 远端已上传 parts 对账
- 对账失败时保守降级为“先按本地状态继续恢复”，但最终合并前仍会再次校验

不支持：
- 跨机器共享恢复
- 跨浏览器共享恢复
- 完整秒传
- 队列化上传任务编排

## 错误与重试

分片失败时会按错误类型区分：

- `timeout`：可重试
- `connection`：可重试
- `http_5xx`：可重试
- `http_429`：可重试，并优先遵循服务端返回的等待时间
- `http_4xx`：通常不重试
- `unknown`：默认不重试

前端会显示：
- 失败原因
- 是否继续重试
- 是否已停止重试
- 若遇到 `429`，会提示当前处于限流退避，并展示建议等待时间
- 若连续遇到 `429`，multipart 上传并发会保守地下调，并提示当前已临时收敛到多少路并发

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

### 常用配置说明

- `OCI_NAMESPACE`：Object Storage namespace
- `OCI_BUCKET_NAME`：bucket 名称
- `APP_AUTH_USERNAME`：固定登录用户名
- `APP_AUTH_PASSWORD`：固定登录密码
- `APP_SESSION_SECRET`：session 签名密钥
- `APP_UPLOAD_CHUNK_SIZE_MB`：分片大小，默认 16 MB
- `APP_UPLOAD_SINGLE_PUT_THRESHOLD_MB`：单请求上传阈值，默认 32 MB
- `APP_UPLOAD_PARALLELISM`：并发分片数，默认 6
- `APP_UPLOAD_SESSION_DIR`：上传会话目录

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

- `APP_UPLOAD_CHUNK_SIZE_MB=16`
- `APP_UPLOAD_PARALLELISM=4` 或 `6`
- `APP_UPLOAD_SINGLE_PUT_THRESHOLD_MB=16` 或 `32`

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

当前仍采用一个很轻的现实方案：

- 在当前对象列表里直接多选
- 点击“下载所选”
- 服务端临时把所选对象打成一个 ZIP
- 浏览器只接收并下载这一个 ZIP 文件

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
- 失败信息也能被前端或用户现实地看到

当前边界：
- 批量 ZIP 仍是一次动态生成流，不适合做真正意义上的多线程断点下载
- 若浏览器中途断掉，通常还是要重新生成这一份 ZIP
- 超大批量下载暂时还没有做后台任务化

## 自检

```bash
python3 -m compileall app tests
pytest -q
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

## 后续方向

- 删除体验继续优化
- 上传可靠性继续加固
- 批量下载
- 重命名对象
- 更强的目录视图（当前已支持轻量目录导航）
- 更强的预览能力
- 多用户权限体系

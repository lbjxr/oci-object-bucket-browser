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
- 预览文本文件
- 预览图片
- 预览 PDF
- 图片对象右侧缩略图
- 非图片对象显示类型图标

## 目录结构

```text
.
├── app/
│   ├── config.py
│   ├── main.py
│   ├── models.py
│   ├── oci_client.py
│   ├── routes.py
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
assert client.post('/login', data={'username': 'admin', 'password': 'secret123', 'next_path': '/'}, follow_redirects=False).status_code == 303
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

## 后续可扩展方向

- Public / private 分享链接
- PAR 支持
- 删除 / 重命名对象
- 目录树视图
- 批量上传 / 批量下载
- 更强的预览能力和文件类型识别
- 多用户权限体系

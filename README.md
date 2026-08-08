# Parity Browser Sandbox

Parity 是从 `mojing_agent` 完整迁出的独立 E2B 浏览器采集项目。它负责 E2B Desktop microVM、
JD 搜索与详情采集、浏览器 Profile、证据文件、隔离策略和生命周期测试，不导入 Agent、Kafka 或
业务数据库代码。

## 目录

```text
parity/
├── src/parity/              Python 包与 E2B Runner
├── src/parity/collectors/   JD 搜索和详情页采集器
├── extension/               用户点击触发的 Chrome 当前页采集扩展
├── config/                  沙盒策略示例
├── tests/                   单元与隔离测试
├── artifacts/               已迁移的 Profile、截图和采集证据（不提交版本库）
├── ARCHITECTURE.md          隔离设计和上线门槛
└── pyproject.toml           独立安装配置
```

## 安装

在 `D:\Document\省心购\mojing` 下执行：

```powershell
python -m venv parity\.venv
parity\.venv\Scripts\python.exe -m pip install -e ".\parity[test]"
```

将 `parity/.env.example` 复制为 `parity/.env`，填入 E2B API Key：

```text
E2B_API_KEY=e2b_你的密钥
PARITY_PROFILE_KEY=由初始化命令生成
PARITY_BRIDGE_TOKEN=由初始化命令生成
```

`.env`、Profile 和采集证据已由 `.gitignore` 排除。不要把密钥、Cookie 或 Profile 提交到版本库。

首次使用时生成本地 Profile 加密密钥（命令只报告是否创建，不显示密钥）：

```powershell
.\.venv\Scripts\python.exe -m parity.profile_store init-key --env-file .env
```

## 在 E2B 中人工登录

启动带随机密码保护的临时实时桌面：

```powershell
.\.venv\Scripts\python.exe -m parity.e2b_login --login-timeout-seconds 600
```

命令会输出一个短时 `live_view_url`。在浏览器打开该地址并完成京东登录；系统检测到 `pt_key` 和
`pt_pin` 后关闭 Chrome，将 Profile 加密保存到 `artifacts/profiles/jd-default.tar.gz.fernet`，随后
停止实时桌面并销毁 microVM。登录失败或超时不会覆盖已有健康 Profile。

## 运行真实 E2B 采集

进入项目目录：

```powershell
Set-Location .\parity
```

在 E2B Desktop microVM 内启动 headed Chrome 并搜索商品：

```powershell
.\.venv\Scripts\python.exe -m parity.e2b_runner search --query "SK-II 神仙水 230ml"
```

核验明确的京东商品详情页：

```powershell
.\.venv\Scripts\python.exe -m parity.e2b_runner product --url "https://item.jd.com/100179356947.html" --expected-spec "10ml"
```

默认结果保存在 `artifacts/price-crawler/e2b-latest`。Runner 负责创建 E2B Desktop microVM、上传
collector、恢复加密 Profile、在 VM 内启动浏览器、下载 JSON 和截图，并在结束时销毁实例。采集状态
为 `succeeded` 或 `partial` 时才会安全写回 Profile。

## 本地调试

本地运行必须显式使用 `--local-dev`，避免把宿主机浏览器误认为 E2B 沙盒：

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m parity.collectors.jd_search --local-dev --query "SK-II 神仙水 230ml"
```

人工登录并保存开发 Profile：

```powershell
.\.venv\Scripts\python.exe -m parity.collectors.jd_product --local-dev --login-only --profile-dir artifacts/price-crawler/profile-jd
```

本地 Profile 只是开发状态持久化，不提供 microVM 隔离。远程 Profile 加密仓库和人工登录写回仍是
后续能力。

## 使用日常 Chrome 采集当前搜索页

京东对自动化浏览器返回频控页、但同一台电脑上的日常 Chrome 可以正常搜索时，使用用户辅助采集模式。
这个模式不会自动打开京东或代替用户登录：用户在自己的日常 Chrome 中搜索，确认页面正常后，再点击
扩展读取当前页面已经展示的商品卡片。

首次生成本机桥接令牌：

```powershell
.\.venv\Scripts\python.exe -m parity.bridge_server init-token --env-file .env
```

启动本机接收服务，并保持该窗口开启：

```powershell
.\.venv\Scripts\python.exe -m parity.bridge_server serve
```

然后安装扩展：

1. 在 Chrome 地址栏打开 `chrome://extensions`。
2. 打开右上角“开发者模式”，选择“加载已解压的扩展程序”。
3. 选择 `D:\Document\省心购\mojing\parity\extension`。
4. 在另一个 PowerShell 窗口运行下列命令；把显示的令牌复制到扩展弹窗并保存。令牌不要发到聊天或提交到版本库。

```powershell
.\.venv\Scripts\python.exe -m parity.bridge_server show-token
```

日常使用流程：

1. 用平时可以正常访问京东的 Chrome 打开搜索结果页并等待商品显示。
2. 如需更多结果，先在页面中正常滚动，让商品卡片加载出来。
3. 点击“Parity 京东当前页采集”扩展，再点击“采集当前页”。
4. 查看 `artifacts\bridge\inbox\latest.json`；每次采集还会保留一个带时间和随机编号的 JSON 文件。

扩展只在用户点击后获得当前标签页的临时权限，只接受 `https://search.jd.com/` 页面。上传字段限制为搜索词、
页面地址，以及商品的 SKU、标题、价格、店铺和商品链接；不读取 Cookie、密码、localStorage、浏览历史或
完整 HTML。本机服务只绑定 `127.0.0.1:8765`，并校验扩展来源、随机令牌、京东域名和字段大小。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m parity.policy config/sandbox-policy.example.toml
```

详细安全边界、Profile 生命周期和生产上线门槛见 `ARCHITECTURE.md`。

## 与 Agent 的边界

Agent 后续只能通过受认证 API 提交任务参数并读取结构化结果和证据引用。Parity 不向 Agent 返回
Cookie、Profile 压缩包、E2B 凭据、VNC 内部地址、对象存储签名地址或 KMS 数据密钥。

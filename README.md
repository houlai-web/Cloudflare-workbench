# v2rayN VLESS 节点批量生成与 Xray 实测工具

一个面向 Windows 的中文命令行工具：按国家/地区查询 FOFA、从结果文件提取 `IP:端口`、
以一条示例 `vless://` 链接批量生成节点，再调用 Xray 内核测试真实延迟与下载速度，
最后导出 CSV/JSON 或可直接导入 v2rayN 的 `vless://` 链接。

> 仅用于你有权访问、有权测试的数据源和目标主机。使用前请阅读文末「合规提示」。

## 首次使用（三行）

1. 根目录 `config.json` 缺失时，程序会自动把 `app\config.example.json` 复制生成；也可手动复制。**本工具不需要 FOFA 账号/API Key/Cookie**，`fofa` 段只保存查询语句模板。
2. 把 `xray.exe` 放到 `app\xray\` 下（默认路径 `app\xray\xray.exe`，可在 `config.json` 修改）。没有内核也能完成「导入 → 提取 → 生成」并导出未测试链接，只影响真实延迟/测速。
3. 双击 `VLESS-Workbench.exe`，窗口出现后服务自动启动，端口监听成功后浏览器自动打开 `http://127.0.0.1:8765/`，随后自动收进系统托盘。

## 目录结构

```
D:\v2ray_test\
├─ .gitignore
├─ README.md
├─ config.json            # 本地配置（不含任何凭据，已 gitignore，不入仓库；缺失时自动生成）
├─ VLESS-Workbench.exe    # 单文件启动器（双击启动，含窗口+托盘）
├─ downloads\             # 用户文件 + 导出结果（CSV/JSON/vless txt）
└─ app\                   # 代码与运行目录
   ├─ main.py  requirements.txt  config.example.json  # 配置模板（打包后也在包内）
   ├─ launcher.pyw        # 启动器源码（tkinter 窗口 + ctypes 托盘）
   ├─ build_exe.ps1       # PyInstaller 打包脚本
   ├─ vless_tool\         # 核心代码
   ├─ web\                # 前端单文件
   ├─ sample_data\        # 示例数据
   ├─ xray\               # xray.exe（gitignore）
   ├─ tmp\
   │  ├─ work\            # 工具过程文件（上传副本、提取中间结果、测速临时目录等）
   │  └─ selftest\        # 自检产物
   └─ logs\               # 运行日志
```

**路径语义**（所有相对路径以程序根目录为基准：源码运行 = 仓库根；打包运行 = exe 所在目录）：
- `downloads/`（根目录）：用户手动下载/放入的文件 + 导出结果（`中文用途_YYYYMMDD_HHMMSS.csv/json/txt`，如 `测试结果_20261104_014000.csv`、`未测试链接_20261104_014000.txt`）。
- `app/tmp/work/`：工具自动产生的过程文件（上传落盘副本、粘贴/本地导入副本、FOFA 原始页、提取中间结果、测试批次配置与临时日志），按任务分子目录。
- `app/xray/xray.exe`：Xray 内核路径。
- `app/logs/`：运行日志。

配置查找顺序：`--config` 参数 → 环境变量 `VLESS_CONFIG` → 程序根目录（exe 同级）→ `app/` → 当前工作目录。

## 1. 环境与安装

- Windows 10/11，Python 3.10 及以上（已在 Python 3.13.9 上验证）
- 依赖：`requests[socks]`、`flask`、`waitress`（Web 界面使用；CLI 只依赖前两项）
  - Web 默认用 waitress 生产级 WSGI（多线程，SSE 不缓冲）；未安装 waitress 时自动回退 Flask
    内置服务器，并在控制台明确提示回退原因（此时才可能出现 `development server` 警告）。

```powershell
cd D:\v2ray_test\app
python -m pip install -r requirements.txt
```

如果使用虚拟环境：

```powershell
cd D:\v2ray_test\app
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## 2. 启动方式

| 方式 | 说明 |
| --- | --- |
| 双击 `VLESS-Workbench.exe` | **推荐**：打开控制窗口 + 自动启动 Web 服务 + 自动打开浏览器 + 自动收进托盘；单实例、端口占用提示 |
| `python app\main.py --web` | 备用：命令行启动 Web 界面（默认 `http://127.0.0.1:8765/`），需本机 Python 环境 |
| `python app\main.py` | 进入中文主菜单（从仓库根目录运行；也可先 `cd app` 再运行 `main.py`） |
| `python app\main.py --xray-check` | 检查 Xray 路径/版本，最小 socks 入站启动验证后退出 |
| `python app\main.py --selftest` | 自检：示例数据 + 模拟内核，不联网、不启动真实 Xray |
| `python app\main.py --config D:\path\config.json` | 使用指定配置文件 |
| `python app\main.py --help` | 查看参数说明 |

> 复制命令时不要带上 Markdown 反引号（`python app\main.py --web` 外层的那对符号）。PowerShell 会把行尾多出的反引号并进参数，于是 usage 里明明有 `[--web]`，却报 `unrecognized arguments: --web`。

程序启动时自动创建 `downloads\`、`app\tmp\work\`、`app\logs\` 等配置目录；`app\tmp\selftest\` 仅在执行 `--selftest` 时按时间戳创建。
日志写入 `app\logs\run.log`（滚动保留 5 份，每份最大 5 MB）。

所有命令都不涉及账号或凭据，启动即可使用。

## 2.1 VLESS-Workbench.exe 启动器

双击 `VLESS-Workbench.exe` 打开控制窗口：服务**自动启动**（默认端口 8765），端口监听成功后**自动打开浏览器**，并在托盘就绪时**自动收进系统托盘**。

窗口功能：
- 状态区：服务地址（可点复制）、服务引擎（waitress/Flask）、Xray 内核（路径 + 版本）、配置路径
- 端口输入框 + 一个「启动/停止」合并开关：未运行时显示「启动」，运行中显示「停止」，切换过程中禁用防误点
- 「退出」按钮（与托盘菜单「退出」共用同一条清理流程）
- 右侧「打开工作目录」按钮（打开程序根目录）
- 日志框：滚动显示最近启动/错误信息（最多 500 行）
- 底栏提示：关闭 / 最小化都收起到托盘；退出用「退出」按钮或托盘菜单

窗口生命周期：
- 点 X 或最小化：只收起到系统托盘，不结束进程；托盘创建失败时窗口保持显示，不会“消失后找不到”
- 退出只有两条路径：窗口「退出」按钮、托盘菜单「退出」；两条路径都会停服务、等端口释放、清理 Xray 子进程、移除托盘图标后再退出

托盘行为：
- 右键菜单固定 4 项：`显示窗口` / `打开页面` / `打开工作目录` / `退出`
- 左键单击托盘图标恢复窗口；「打开页面」恢复窗口并打开浏览器

其他：
- 单实例：二次启动会先尝试激活已有窗口；失败时提示“已在运行（见系统托盘）”后退出，不重复起服务
- 无控制台窗口（`--noconsole`）
- 外部文件不打包：`config.json`、`downloads/`、`app/xray/`、`app/web/`、`app/tmp/`、`app/logs/` 均从 exe 同级目录读取
- 分发时只发 exe：无 `xray.exe` 也能完成「导入 → 提取 → 生成」并导出未测试链接；真实延迟/测速必须由用户自备 `app\xray\xray.exe`

重新打包（需本机 Python + PyInstaller）：

```powershell
cd D:\v2ray_test\app
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

产物 `app\dist\VLESS-Workbench.exe` 会自动复制到仓库根目录。

## 3. 本工具不需要 FOFA 凭据

- 程序不再收集或保存 FOFA 邮箱、API Key、Cookie、登录态；`config.json` 的 `fofa` 段只保留查询模板
  `query_template`，不会再出现 `email` / `key` / `session` 等字段。
- 查询语句与搜索链接（`https://fofa.info/result?qbase64=...`）完全在本地生成，可复制或直接用默认浏览器打开。
- 浏览器里搜索完成后，用「手动粘贴网页/API 结果」或「导入本地页面/文件」把结果带回工具，后续提取/生成/测速不受影响。
- 如果你在旧版本里填过 Key：它已经不在配置文件里；若担心外泄，建议登录 FOFA 后台重置或吊销旧 Key。

## 4. 五步操作流程（对应主菜单 1–5）

### 第 1 步：生成查询语句（菜单 1）

- `1`：从内置地区清单（日本、新加坡、美国、香港、台湾、韩国、德国、荷兰、英国、加拿大）选择，
  按 `config.json -> fofa.query_template` 生成查询语句，打印语句与 `qbase64` 编码，并可一键在浏览器打开搜索页。
- `2`：输入自定义国家代码（如 `JP`/`US`/`HK`），也可直接输入完整自定义查询语句。
- `3`：手动粘贴网页/API 结果（不需要任何凭据）。
- `4`：导入本地已保存的 FOFA 页面或结果文件（支持 txt/csv/json/html）。

导入内容会复制/保存到 `app/tmp/work/`：

- `fofa_paste_<时间戳>_<地区>.txt`（粘贴）或 `fofa_local_<时间戳>_<地区>.<后缀>`（本地文件）
- 同名 `.meta.json`：来源、地区代码、原始文件名、导入时间等来源记录

### 第 2 步：选择下载文件并提取 IP:端口（菜单 2）

- 列出 `downloads/` 中可解析文件（按修改时间倒序），显示文件大小与来源查询。
- 支持 `1,3`、`1-5` 多选，也支持 `m` 手动输入任意文件路径。
- 自动识别 txt/csv/json/html，支持三种来源（按「明文 → vless → 纯 IP」顺序处理，同一 `host:port` 只保留首次出现）：
  1. **明文 `host:port`**：`1.2.3.4:443`、`cdn.example.com:8443`、`[2606:4700::1111]:443`；
  2. **vless 链接**：`vless://UUID@1.2.3.4:443?...#name`，取 `@` 后地址与端口（缺端口用默认端口），`host` 原样保留；
  3. **纯 IP（无端口）**：`1.2.3.4` 与 `2606:4700:4700::1111`（IPv6 必须含 `::`），补 `extract.default_port`（默认 443）。
- 明确不误抓：`12:34:56` 之类时间戳、`/api/v1/7.8.9.10/status` 路径、`v1.2.3.4`、`999.999.999.999` 等非法/噪声地址。
- 按 `extract.port_allowlist` / `extract.port_blocklist` 过滤端口，排除私有/本地地址，
  `extract.max_items` 截断，跨文件与同批内去重，并打印统计与预览。
- 统计字段：候选、重复、非法、私有/本地、端口过滤、截断、**补默认端口条数**、**vless 链接条数**；
  每个端点记录类型（`明文` / `纯IP补端口` / `vless链接`）与来源文件。
- 中间结果写在工作目录 `app/tmp/work/extracted_<时间戳>[_地区].txt` 与同名 `.json`（JSON 内含每个端点的 `kind`）。

### 第 3 步：批量生成 VLESS 节点（菜单 3）

- 输入一条完整的示例 `vless://` 链接，或回车使用 `generate.sample_vless`；
  输入 `@文件路径` 可从文件读取第一条 `vless://` 链接。
- 程序保留示例中的 `encryption/security/sni/host/path/type/flow/fp/alpn/pbk/sid` 等参数，
  **只替换地址与端口**，并按 `generate.name_template` 重命名。
- 可设置生成数量上限（默认 `generate.max_nodes`，且不超过端点数）。
- 缺少 UUID、`encryption`、`type`、`security`，或 UUID/地址/端口格式错误时，会一次性列出全部问题。

### 第 4 步：Xray 真延迟与下载速度（菜单 4）

- 每 `test.kernel_batch`（默认 50）个节点共用一个 Xray 内核进程，一批只生成一份配置：每个节点一个本地入站端口（`test.test_inbound` 为 `http` 或 `socks`，默认 `http`），出站与节点一一对应，启动开销整批只付一次。
- 延迟阶段并发由 `test.latency_concurrency`（默认 50）控制：只是短连接握手，彼此不占带宽，可以开得较高；测速阶段并发由 `test.speed_concurrency`（默认 5）控制：下载会抢带宽，必须压低，否则多节点同时测速会把结果拉低甚至互相拖垮。两阶段分开，所以能先快速筛掉坏节点，再对少量存活节点测真实速度。
- 延迟按 `test.latency_samples` 多次取样，取最优或平均（`latency_sample_mode`）。
- 测法由 `test.test_mode` 决定：`latency` 只测真连接延迟、跳过测速（速度字段留空）；
  `latency_speed` 在延迟成功后继续下载测速。
- 测速带连接/读取超时，并限制下载字节数与耗时（`speed_max_bytes` / `speed_max_seconds`）。
- 失败原因分类：内核启动失败、TLS 失败、连接/读取超时、目标连接失败、协议错误、
  本地代理失败、代理异常状态、测速失败等。
- 每批测试结束立即结束共用内核、释放该批次占用的端口并删除临时目录。
  按 `Ctrl+C` 或程序异常退出时，会清理所有已注册的内核子进程；Windows 下还使用
  Job Object 作为兜底。

### 第 5 步：结果查看、导出与重测（菜单 5）

- 以中文对齐表格展示序号、地址、地区、延迟、速度、状态、错误分类。
- `1` 保存完整结果为 CSV + JSON（写入 `downloads/`）。
- `2` 导出全部 `vless://` 链接 txt（默认按延迟排序，可直接导入 v2rayN）。
- `3` 按速度排序导出；`4` 按延迟排序导出。
- `5` 重新测试选中项（支持 `1,3` / `2-4`），结果合并回当前列表。
- `6` 查看失败/测速异常节点的具体原因。
- 导出文件名统一为 `中文用途_YYYYMMDD_HHMMSS.ext`，例如 `测试结果_20261104_014000.csv`、
  `未测试链接_20261104_014000.txt`、`按延迟排序_20261104_014000.txt`；
  同一秒内重复导出自动追加 `_2`、`_3`。

## 5. Web 界面（本地浏览器操作）

不想用命令行时，可用同一套业务逻辑的本地网页：

### 5.1 启动与访问

| 方式 | 说明 |
| --- | --- |
| 双击 `VLESS-Workbench.exe` | 打开控制窗口，服务自动启动并自动打开浏览器，随后收进系统托盘 |
| `python app\main.py --web` | 命令行启动服务并自动打开浏览器，默认 `http://127.0.0.1:8765/` |
| `python app\main.py --web --port 8877` | 指定端口；默认 8765，被占用时会明确报错并建议换端口 |
| `python app\main.py --web --host 127.0.0.1` | 监听地址，默认只监听本机回环 |
| `python app\main.py --web --no-browser` | 不自动打开浏览器 |

- EXE 窗口显示服务地址、引擎（waitress/Flask）、内核状态、配置路径；可改端口后用「启动/停止」合并开关控制服务；
  窗口只有「启动/停止」「退出」两枚操作按钮，右侧另有「打开工作目录」。X 与最小化都只收进托盘，
  退出只有窗口「退出」按钮和托盘菜单「退出」两条路径（托盘菜单：显示窗口 / 打开页面 / 打开工作目录 / 退出）。
- 启动后终端会打印完整地址（含端口，如 `http://127.0.0.1:8765/`）、配置文件路径与服务引擎
  （`waitress 3.x（生产级 WSGI，8 线程）` 或回退提示）；按 `Ctrl+C` 停止。
- 请访问打印的 `http://` 地址，**不要直接双击 `web\index.html`**；`file://` 页面无法调用 `/api` 接口。
- 端口被占用时会打印占用提示与换端口命令，不会静默失败；若服务已在运行，可直接访问提示中的地址。
- Web 与 CLI 共用同一份 `config.json`（`--config` 同样适用）：Xray 路径、端口范围、导出目录、
  测试参数全部沿用配置；CLI 原有菜单与参数不受影响。
- 后端复用 CLI 的提取/生成/测试/导出模块，不重复业务逻辑；导出文件写入
  `config.json -> export_dir`（默认 `D:\v2ray_test\downloads`），页面提供下载链接。
- 打包后页面可外部覆盖：优先读 `<exe目录>\app\web\index.html`，外部文件缺失才回落包内副本（日志会写明“外部缺失”）；
  `GET /` 返回 `Cache-Control: no-store`，改完外部页面刷新即可生效，无需重打包。
- 默认只监听 `127.0.0.1`。改成 `0.0.0.0` 会暴露到局域网，且页面没有登录鉴权，
  请勿在不可信网络使用。

### 5.2 页面六个可见步骤（FOFA 搜索 → 导入 → 提取 → 生成 → 测试 → 结果）

1. **FOFA 搜索（第 1 步）**：三列下拉 = **地区/国家 · 网络/线路 · 端口**，选完即由后端
   `vless_tool/fofa.py` 拼装查询语句，显示在命令框，可「复制命令」或「立即搜索」
   （新标签打开 `fofa.info/result?qbase64=...`，使用**标准 Base64 + `encodeURIComponent`** 编码，
   与 FOFA 官方 `btoa` 方式一致，避免客户端解码失败）。本步只做「生成语句 + 复制 + 打开浏览器」，
   **不需要任何账号、API Key 或 Cookie**；Web 界面不再显示完整搜索链接，只在命令框下方给出字段规则说明。
   - **字段规则（country vs region）**：香港 `HK`、台湾 `TW`、澳门 `MO` 是中国的地方行政区，
     用 `region="HK"`；美国 `US`、韩国 `KR`、日本 `JP` 等主权国家用 `country="US"`。
     每项的字段归属在 `FOFA_PLACE_OPTIONS`（`fofa.py`）里**显式标注** `field`，前后端共用，
     不做隐式猜测。
   - **线路 7 项**（严格按《FOFA语法示例.txt》的 ASN 组合）：
     移动线路（45102/37963/45090）、联通线路（132203/396982/31898）、
     电信线路（45102/55990/37963）、一线机房（45102/132203/45090/37963）、
     二线机房（55990/396982/31898）、海外VPS（25820/16276/16509/20473/24940/63949/14061/36352）、
     全部(不限ASN)（不拼 `asn==`，其余条件不变）。
   - 语句结构：`server=="cloudflare" && <region|country> && port="443" && header="Forbidden"`
     `&& (<asn==...>) && (<19 条 asn!=...>)`；默认 中国香港 / 全部(不限ASN) / 443。
   - 地区 24 项、端口 8 项；下拉均为**自定义组件**（见 5.4），选项带国旗/图标。

2. **导入资产**：三个来源任选——上传文件（txt/csv/json/html，最大 32 MB）、
   从 `downloads/` 勾选已有文件、直接粘贴文本；可填地区代码（如 `JP`）。
   「选择文件」按钮与主按钮同一套样式，选中后显示文件名与大小。
3. **提取预览**：显示有效条数与候选/去重/非法/私有/端口过滤/补默认端口/vless 链接计数，
   预览表带「类型（明文 / 纯IP补端口 / vless链接）」与「来源」列；可填「默认端口」（默认 443，
   仅对缺端口的纯 IP 与 vless 链接生效）；支持搜索；结果全部渲染在约 7 行高的滚动容器里，
   按滚动位置显示「第 x / y 屏 · 共 N 条」；提取成功后并排出现「导出IP / 导出IP+端口 / 导出IP+端口+国家」。
4. **生成节点**：1~10 条示例 `vless://` 可增删，每条独立校验（失败行标红并给出原因），
   支持一键清空，草稿存 `localStorage` 刷新不丢。
   「生成数量」与输入框并列，默认跟随提取到的有效端点数；分配模式二选一：
   - **随机分配** `random`：每个 IP 随机取一条模板，洗牌分桶保证每条模板都出现；
   - **顺序循环** `sequential`：按模板输入顺序循环，第 1/2/3 个 IP 用第 1/2/3 条模板，第 4 个回到第 1 条。
   预览表带勾选列（表头可全选/反选），显示「已选 N / 共 M」。
5. **测试**：所有参数都有中文标签、单位与说明（并发数、延迟取样次数、启动/请求超时（秒）、
   下载阈值（字节）、测速时长（秒）、测速 URL）；支持全部节点或仅勾选节点、开始/停止。
   **测法二选一**（对应 `test.test_mode`）：
   - **测试真连接延迟**（`latency`）：只建立真实连接并测握手延迟，不发起下载测速，
     速度列显示「未测」；更快、几乎不耗流量（通常每节点 1~3 秒）。
   - **测试延迟和速度**（`latency_speed`，默认）：在延迟成功后再下载测速，结果更全，
     但每节点会多消耗 `speed_max_bytes` 上限的流量、并多花最多 `speed_max_seconds` 秒。

   范围选「仅勾选节点」时若没有勾选会自动禁用并提示去哪里勾选。
   进度通过 SSE 实时推送，逐节点写回延迟/速度/状态/失败原因，进度序号按 `[已完成/总数]` 递增。
   测试结束后**自动把结果写入 `downloads/`（CSV+JSON）**，只有真正写盘成功才提示，
   提示与日志带实际绝对路径（如 `已写入 D:\v2ray_test\downloads\测试结果_20261104_014000.csv`）；
   0 个成功节点时不写空文件，提示「未产生可导出结果：N 个节点全部失败」。
   断线或 done 事件丢失时，页面会通过 `/api/state` 轮询发现 `running=false` 并自动归位。
   **失败原因已中文化**：表格显示中文（鼠标悬停显示原始 `error_type`），
   CSV 增加「中文原因」列，JSON 同时给 `reason_code` 与 `reason_cn`。
6. **结果与导出**：按延迟/速度排序，关键词、只看可用、延迟上限（ms）、速度下限（Mbps）筛选；
   仅延迟模式（`latency`）下速度列显示「未测」，**速度下限输入框自动禁用**并提示
   「当前为仅延迟模式，速度条件不适用」，排序自动切到按延迟（导出同理，`speed_mbps` 为空/null）；
   导出按钮固定为：`导出分享链接` → `导出API` → `导出选中` → `全部导出 TXT` → `按延迟 TXT`
   → `按速度 TXT` → `全部导出 CSV+JSON`（最后）。`导出API`、`导出选中`、`全部导出 TXT`、
   `全部导出 CSV+JSON` 的顺序跟随结果表当前「排序字段」，分页不影响导出（始终导全量）；
   `按延迟 TXT` 固定按延迟升序、`按速度 TXT` 固定按速度降序（失败/缺值垫底），不随页面排序变化。
   `导出分享链接` 只把优选节点的 `vless://` 链接写入剪贴板，
   不生成文件；`导出API` 生成 `IP:端口#国旗-国家` 形式的 TXT；`导出选中` 生成 `vless://` 分享链接 TXT
   （一行一条，可直接导入 v2rayN），严格按勾选行导出、不过滤失败节点，并在提示里说明其中多少条
   未成功或无延迟数据；未勾选时提示「未勾选任何节点」。
   结果表带勾选列与「已选 N / 共 M」，可「重测勾选项」；导出后列出文件名与下载链接
   （`/api/download/<文件名>`）。导出文件名统一为 `中文用途_YYYYMMDD_HHMMSS.ext`
   （用途：测试结果、未测试链接、按延迟排序、按速度排序、全部节点、优选API、选中节点）；
   同一秒内重复导出自动追加 `_2`、`_3`；下载链接的 `download` 属性带同一中文名。

### 5.3 Trae 宝挂件

页面右下角有一个 Trae 宝形象（图片以 base64 data URI 内嵌，单文件、零外部依赖）：

- 鼠标进入其周围约 190px 时，它会带阻尼缓动地移向鼠标并保持偏移，不挡指针；离开后回到原位。
- 按住可拖到页面任意位置，松手记忆位置（`localStorage` 键 `trae_pet_pos`），刷新后回到上次位置。
- 旁边的对话气泡按当前步骤给提示；点击气泡换下一句，点击挂件收起/展开气泡，14 秒无交互自动收起。
- 小屏（<900px）自动缩小靠边；系统开启「减少动态效果」（`prefers-reduced-motion: reduce`）时
  关闭跟随与浮动动画。

### 5.4 自定义下拉组件

全站 7 个下拉（FOFA 三列、测法、测试范围、排序字段、只看可用）统一用自定义组件，
不再使用原生 `<select>`：

- 触发器：左为当前选项 + 图标/国旗，右侧箭头；hover 描边、focus 发光。
- 面板：圆角浮层 + 阴影，选项行含国旗/图标，hover 高亮，选中行带 ✓，超高时滚动（上限 320px）。
- 交互：点击外部关闭、`Esc` 关闭、`↑↓` 选择、`Enter` 确认、`Home/End` 跳首尾、打开时滚到当前项；
  `role="listbox"` / `aria-expanded` 等无障碍属性齐全；面板 `position: fixed` 不被父容器裁切。
- 原生 `<select>` 仍在 DOM 中（视觉隐藏），所有既有逻辑继续读取 `.value`，零改动兼容。

### 5.5 常见问题

**Q：双击 `web\index.html` 后页面提示连不上服务？**
这是 `file://` 打开方式导致的：浏览器禁止本地文件页面访问 `127.0.0.1` 的 `/api` 接口，
页面能渲染但所有功能都不可用。请改为先启动服务，再访问 `http://127.0.0.1:8765/`：
双击 `VLESS-Workbench.exe`，或运行 `python app\main.py --web`。
页面出现该提示时会直接给出完整 http 地址或启动命令，并带「复制」按钮。

**Q：页面提示「服务未启动或已停止」？**
说明页面是用 http 地址打开的，但后端进程已退出。双击 `VLESS-Workbench.exe` 或运行
`python app\main.py --web`，启动后刷新页面即可。

**Q：端口被占用 / 打不开页面？**
EXE 启动时若端口被占用，窗口内会弹出提示并可改端口；命令行模式下终端会打印
`[端口占用]` 与换端口命令，不会静默退出。换端口：`python app\main.py --web --port 8877`；
确认没有报错、防火墙未拦截本机回环地址。

**Q：页面显示 Xray 不可用？**
页面顶部状态会显示内核路径。按第 7 节放置 `xray.exe`，或修改 `config.json -> xray_path`
后重启服务。若只是先验证页面流程，可把测法切到「测试真连接延迟」并用任意节点试跑。

**Q：真实测试一直失败？**
先确认节点本身可用、本机能访问 `test.latency_url` / `test.speed_url`；
可在页面里临时调大请求超时、降低并发。失败原因会写进结果表与 `logs/run.log`。

**Q：停止测试后还有进程残留？**
正常停止会结束已注册的 Xray 子进程并清理临时目录；若强制关闭终端，
重新启动服务前可用任务管理器确认没有遗留 `xray.exe`。

**Q：导出的文件在哪里？**
`config.json -> export_dir`（默认 `D:\v2ray_test\downloads`），也可直接点页面上的下载链接保存。

**Q：导出顺序是固定的延迟升序吗？**
不是全部固定。`导出API`、`导出选中`、`全部导出 TXT`、`全部导出 CSV+JSON` 跟随结果表当前
「排序字段」（按延迟升序 / 按速度降序，失败垫底），分页不影响导出（导全量）；
`按延迟 TXT` 永远按延迟升序、`按速度 TXT` 永远按速度降序，与页面排序无关。
「测试完自动存盘」那份同样固定为速度降序 / 延迟升序。

**Q：想只测部分节点，勾选在哪里？**
「生成节点」预览表和「结果」表最左侧都有勾选列，表头复选框可全选/反选，下方显示
「已选 N / 共 M」。勾选后把测试区「测试范围」设为「仅勾选节点」；未勾选时该选项自动禁用并说明原因。
勾选状态在同一会话内保留（重测、切换筛选、刷新页面都不丢，存于 `sessionStorage`）。

**Q：结果里的失败原因看不懂？**
页面、CSV、JSON 都已中文化：如 `connection_timeout` → 连接超时、`speed_failed` → 测速失败、
`tls_failed` → TLS 握手或证书校验失败、`proxy_error` → 代理返回异常 HTTP 状态。
界面显示中文，鼠标悬停可看原始 `error_type`；CSV 追加「中文原因」列，JSON 同时给
`reason_code` 与 `reason_cn`。对照表见 `vless_tool/results.py -> ERROR_REASON_CN`。

**Q：为什么选美国用 `country`，选香港却用 `region`？**
FOFA 的 `region` 字段用于国家以下的地方行政区（香港 HK / 台湾 TW / 澳门 MO），
主权国家要用 `country`（如 `country="US"`、`country="JP"`）。页面的地区/国家下拉里
每一项都带显式 `field` 标记，后端按标记拼装，不会混用。

**Q：Trae 宝挂件挡住按钮怎么办？**
挂件的可点击区域只在图形本身，气泡会自动淡出（14 秒无交互或点击挂件收起）；
拖动它到别处即可，位置会被记住。小屏下会自动缩小靠边，开启系统「减少动态效果」时
不再跟随鼠标和浮动。

**Q：测试进度是不是等全部跑完才一次性出现？**
不是。SSE 事件逐节点推送，waitress 下实测首个进度约 0.3s 到达、后续分批到达；
测试期间同时发普通 GET 请求响应时间仍在几十毫秒内，不会被 SSE 阻塞。

## 6. 配置说明（config.json）

所有相对路径均以程序根目录为基准：源码运行时是仓库根，打包后是 exe 所在目录；`config.json` 通常也放在这里。
修改后重启程序生效；缺字段会自动回退到内置默认值。

### 顶层路径与地区

| 字段 | 含义 |
| --- | --- |
| `xray_path` | Xray 内核路径，默认 `./app/xray/xray.exe`；也可填目录（自动查找 `xray.exe`） |
| `download_dir` | 用户手动放入/下载的文件目录（菜单 2 默认从这里选文件）；工具不往这里写过程文件 |
| `work_dir` | 工具过程目录：上传副本、粘贴/本地导入副本、FOFA 原始页、提取中间 txt/json、测试批次配置与日志；默认 `./app/tmp/work`，批次结束即删除 |
| `export_dir` | CSV/JSON 结果与 vless txt 导出目录，默认 `./downloads` |
| `temp_dir` | 工作目录的父目录，默认 `./app/tmp`；自检会在 `tmp/selftest/<时间戳>/` 下隔离运行，不写正式目录 |
| `log_dir` | 日志目录，默认 `./app/logs` |
| `regions` | 内置地区清单，每项 `{ "name": "日本", "code": "JP" }` |

### `fofa`（仅查询语句）

| 字段 | 含义 |
| --- | --- |
| `_readme` | 说明：该段不含账号/API Key/Cookie |
| `query_template` | 查询模板，支持 `{code}`、`{port}`、`{protocol}` |

默认查询语句示例：`country="JP" && protocol="vless"`。语句与 `qbase64` 搜索链接都在本地生成，
不再发起任何 API 请求，也不会写入 `email` / `key` / `session` 字段。

### `extract`（提取）

| 字段 | 含义 |
| --- | --- |
| `port_allowlist` | 端口白名单；非空时只保留其中的端口 |
| `port_blocklist` | 端口黑名单，优先生效 |
| `exclude_private_ip` | 是否排除私有/本地/回环地址与 `.local` 域名 |
| `max_items` | 最大提取条数，`0` 表示不限 |
| `default_port` | 纯 IP 与缺端口的 vless 链接补的端口，默认 `443`（仅对缺端口条目生效） |
| `preview` | 控制台预览条数 |

### `generate`（节点生成）

| 字段 | 含义 |
| --- | --- |
| `sample_vless` | 默认示例链接；菜单中直接回车即使用它 |
| `name_template` | 节点名模板，可用 `{region}`、`{index:03d}`、`{latency}`、`{address}`、`{port}` |
| `max_nodes` | 单次生成数量上限 |
| `latency_placeholder` | 生成阶段名称中的延迟占位符，默认 `待测` |
| `strict_uuid` | 是否严格校验 UUID 的 8-4-4-4-12 格式 |

### `test`（Xray 测试）

| 字段 | 含义 |
| --- | --- |
| `latency_concurrency` | 延迟阶段并发（默认 50）；短连接握手，彼此不占带宽 |
| `speed_concurrency` | 测速阶段并发（默认 5）；下载抢带宽，需压低以保证结果可比 |
| `kernel_batch` | 单个共享内核承载的最大节点数（默认 50，不超过 `latency_concurrency`） |
| `concurrency` | 旧版兼容字段；仅当没有 `latency_concurrency` 时作为其回退值 |
| `startup_timeout` | 等待本地入站端口就绪的秒数（默认 8） |
| `latency_timeout` | 单次延迟请求超时秒数（默认 6），比测速超时短 |
| `latency_url` | 延迟测试目标，默认 `https://www.gstatic.com/generate_204` |
| `latency_samples` | 延迟取样次数 |
| `latency_sample_mode` | `best`（取最小值）或 `average`（取平均） |
| `request_timeout` | 单次 HTTP 请求超时 |
| `speed_url` | 测速下载地址 |
| `speed_max_bytes` | 单节点最多下载字节数，`0` 表示不限 |
| `speed_max_seconds` | 单节点测速最长耗时 |
| `speed_timeout` | 测速读取超时 |
| `retries` | 节点失败后的重试次数 |
| `test_inbound` | 走本地 `http` 或 `socks` 入站 |
| `test_mode` | 测法：`latency`（只测延迟、不测速）或 `latency_speed`（延迟 + 下载测速，默认） |
| `port_start` / `port_end` | 本地端口分配范围（默认 20000~40000）；每个节点占用 1 个本地入站端口 |
| `keep_temp_on_error` | 是否保留失败节点的临时配置，便于排查 |

### `output`（输出）

| 字段 | 含义 |
| --- | --- |
| `csv_encoding` | CSV 编码，默认 `utf-8-sig`（Excel 直接打开不乱码） |

## 7. FOFA 查询与 Xray 内核

### FOFA 查询

- 不需要账号或 API Key：菜单 1 在本地按 `fofa.query_template` 生成语句，打印 `qbase64` 并可直接打开
  `https://fofa.info/result?qbase64=...` 搜索页。
- 浏览器搜索完成后，用菜单 1→3 粘贴结果或 1→4 导入本地页面/文件即可。
- 旧版本填过的 `email` / `key` / `session.cookie` 已从配置中移除；如担心外泄，请到 FOFA 后台重置旧 Key。

### Xray 内核

1. 从 Xray-core 官方 Release 下载 Windows 64 位压缩包（`Xray-windows-64.zip`）。
2. 解压后把 `xray.exe` 放到：

```text
D:\v2ray_test\app\xray\xray.exe
```

这是 `config.json` 默认的 `xray_path`。也可放到任意目录，并把完整路径写入 `xray_path`。
也可直接使用 v2rayN 自带的 `xray.exe`。

3. 如果路径写错、压缩包多了一层目录，程序会在 `D:\v2ray_test\app\xray` 下递归查找
   `xray.exe`，找到后自动回写 `config.json` 的 `xray_path`。
4. 验证内核版本并做最小启动检查：

```powershell
python app\main.py --xray-check
```

该命令会打印真实版本、用本地 `127.0.0.1` socks 入站启动一次内核、确认端口监听后
结束进程并释放端口；不会连接任何外部节点。本次实测版本为
`Xray 26.10.31 (Xray, Penetrates Everything.) a4a3dff (go1.27.3 windows/amd64)`。

若未配置或路径不存在，菜单 4 会给出明确提示，且不影响菜单 5 导出未测试节点。

## 8. 自检

三个入口各自独立，均为无人工介入：

| 命令 | 验证内容 | 产物 |
| --- | --- | --- |
| `python app\main.py --selftest` | 离线全链路 24 项（见下表），不联网、不启动真实内核 | `app\tmp\selftest\<时间戳>\selftest_summary.json` |
| `python app\launcher.pyw --tray-selftest` | 托盘图标能 `NIM_ADD`、图标确实来自 `app.ico`、右键菜单 4 项且顺序正确、左键/右键回调、退出与窗口按钮共用清理路径、`NIM_DELETE` | `app\logs\tray_selftest.log` |
| `python app\launcher.pyw --ui-selftest` | 真实 Tk + waitress 状态机：自动启动并监听端口、自动开浏览器、自动收托盘、开关停止/重启、X 与最小化都只收托盘、打开工作目录、退出释放端口并移除托盘 | `app\logs\ui_selftest.log` |

`--selftest` 使用 `app\sample_data\fofa_sample.json` 与模拟内核，按执行顺序验证 24 项：

| # | 验证项 |
| ---: | --- |
| 1 | 提取：示例 JSON 得到 6 条有效 `host:port`，私有地址/非法端口已过滤 |
| 2 | 生成：6 个节点，仅替换地址/端口，query 参数完整保留 |
| 3 | 容错：缺少 `encryption`/`security` 时给出明确错误 |
| 4 | 测试（模拟内核）：延迟、速度、超时、测速失败分类均生成 |
| 5 | 结果：CSV + JSON 已写入且数量正确 |
| 6 | 导出：全部/按速度/按延迟三种 txt 均生成 |
| 7 | FOFA：HK/TW/MO 用 `region`，US/KR/JP/GB/DE 用 `country` |
| 8 | FOFA：US/443 语句头、固定排除 ASN、`qbase64` 去 `=` 且可解码回原句 |
| 9 | FOFA：7 条线路中 6 条带 ASN 的组合与 `header="Forbidden"` 条件正确 |
| 10 | FOFA：不限地区/全部端口/非标端口拼装正确 |
| 11 | FOFA：非法线路/端口/地区取值均给出明确错误 |
| 12 | 失败原因中文映射全部一致 |
| 13 | 提取-A 纯 IP：补默认端口 443，私网/非法过滤，版本号/URL 路径/时间戳不误抓 |
| 14 | 提取-B vless：6 条链接解析为 `host:port`，缺端口/端口 0 按默认端口补齐，域名与 IPv6 保留 |
| 15 | 提取-C 混合：明文优先于同 `host:port` 的 vless，跨来源去重，噪声不误抓 |
| 16 | 提取-D 默认端口：0 与 70000 越界时回落到 443 |
| 17 | 提取-E 大文件：10 MB 纯文本解析不卡死并报告耗时 |
| 18 | 多示例·顺序循环：100 个节点的模板序号严格 `1,2,3,1,2,3…` |
| 19 | 多示例·随机分配：100 个节点三条模板全覆盖且计数均衡 |
| 20 | 多示例参数隔离：`security/sni/host/path/type/flow/fp/alpn` 与各模板一致、不串参 |
| 21 | 多示例逐条校验：失败信息带「第 N 条」 |
| 22 | 导出：分享链接只返回文本；导出API按页面排序；导出选中为 vless 链接、按页面排序且不过滤失败；按延迟/按速度 TXT 固定排序；空勾选被拦截 |
| 23 | Xray 单节点配置：本地 socks/http 入站 + VLESS/WS/TLS/SNI/ALPN 出站字段正确 |
| 24 | 清理：调用全局子进程清理，无真实内核启动 |

产物写入 `app\tmp\selftest\<时间戳>\`，不会污染源码目录或正式下载/导出目录。自检不会写回 `config.json`，也不访问网络。

## 9. 常见问题

**Q：提取结果为 0 条？**
检查文件格式是否为 txt/csv/json/html；确认端口未被 `extract.port_blocklist` 拦截；
确认 `extract.exclude_private_ip` 是否把内网地址排除了；用菜单 2 的预览确认文件确实含 `IP:端口`。

**Q：还需要配置 FOFA 凭据吗？**
不需要。本工具不收集也不保存账号、API Key、Cookie；查询语句与搜索链接都在本地生成。
若旧版本的 Key 曾泄露，建议到 FOFA 后台重置。

**Q：菜单 4 提示未配置 Xray？**
按第 7 节放置 `xray.exe` 并检查 `xray_path`。路径含空格也可，程序会加引号传递。

**Q：测试全部超时或速度为 0？**
确认本机可直连 `test.latency_url` / `test.speed_url`；目标节点可能已失效；
可减小并发、增大超时、更换测试 URL。测速为 0 不一定是节点不可用，也可能是目标站点限速。

**Q：端口分配失败？**
每个节点需要 1 个本地入站端口，调大 `test.port_start`/`test.port_end` 或降低 `test.kernel_batch`。

**Q：临时目录残留？**
正常结束会自动删除；把 `test.keep_temp_on_error` 设为 `true` 才会保留失败节点配置用于排查。
程序退出时会清理已注册的内核进程；若被强制杀进程，Windows Job Object 会兜底结束子进程。

**Q：CSV 用 Excel 打开乱码？**
保持 `output.csv_encoding` 为 `utf-8-sig`。

**Q：路径含中文可以吗？**
程序使用 `pathlib` 并显式 UTF-8 读写，通常可用；若遇杀毒软件拦截或编码异常，
建议把项目和 Xray 放在纯英文路径下。

## 10. 合规提示

- 仅查询、提取、测试你**拥有授权**的数据源与主机；不要扫描或测试他人系统。
- 使用 FOFA 时遵守其服务条款、账号权限与 API 配额，不要绕过限制或批量滥用。
- 不得将本工具用于侵入、攻击、传播违法内容或规避当地法律法规。
- 导出的节点配置与链接可能包含敏感信息，请妥善保管，不要公开分享。
- 使用者应自行承担因不当使用产生的全部责任。

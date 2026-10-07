# G4W

> 把微信变成你的 AI 工作入口：能长期记住、主动提醒、后台执行，并把结果送回聊天窗口。

G4W（Generic Agent for WeChat）运行在 Windows 电脑上，通过微信提供持续在线的个人 AI 协作能力。它不只是回答问题，还可以维护长期上下文、调用后台 Worker、整理日记与时间线、管理提醒，并围绕本地资料建立知识库和向量索引。

当前发布底座：[GenericAgent Desktop Portable 1.8](https://github.com/lsdefine/GenericAgent/releases#release-desktop-portable-v0.1.8)。

当前版本：**3.1.2**（2026-10）· [📦 下载 Windows 便携包](https://github.com/MoFrom-FG/G4W/releases/latest) · [各版本更新说明](https://github.com/MoFrom-FG/G4W/releases)

3.1.x 主要更新：**人设预设编辑页**、**供应商 / 模型可视化配置**（任意 OpenAI 兼容接口，不强制 DeepSeek）、**时间轴站点主题切换**、首次向导**完成后自动进入看板**且 API Key 步骤**可跳过**，以及从向导到一键更新的整条链路加固。

WeChat功能逻辑参考：[Cyberboss](https://github.com/WenXiaoWendy/cyberboss/tree/main)。

## 为什么需要 G4W

| 常见痛点 | G4W 的解决方式 |
| --- | --- |
| 每次聊天都要重新交代背景 | 保存经过确认的长期事实、偏好和项目上下文 |
| 长任务容易中断，必须一直盯着 | 交给后台 Worker 执行，完成后回到微信通知 |
| 提醒、日记、资料分散在多个应用 | 在微信里统一记录、查询和维护 |
| 聊天记录很多，靠关键词找不到 | 可选安装向量检索，按语义定位相关原文 |
| AI 给了答案，却没有依据和原文 | 检索后继续核验原始记录或知识库来源 |
| 本地部署步骤复杂、环境容易冲突 | 自带 Python 和离线依赖，主环境与向量环境相互隔离 |

## 核心能力

- **微信长期协作**：用自然语言发起任务、补充要求并接收结果，不必反复切换网页。
- **控制中心 Dashboard**：浏览器或 `G4W.exe` 一键直达，服务启停、实时日志、待办、画像、主题集中管理。
- **长期记忆**：保存有来源依据的稳定事实，并由 AI 持续维护**用户画像**（内容变化自动保留旧值与时间），让后续对话保持连续。
- **后台 Worker**：搜索、资料整理、代码分析和文档生成等耗时任务可在后台完成。
- **提醒与主动联系**：支持固定时间提醒，也支持在设定区间内主动 check-in。
- **日记与时间线**：记录生活或项目事件，生成日、周、月视图与统计信息。
- **知识库**：管理 PDF、TXT、Markdown 等资料，并在回答时引用来源。
- **语义检索**：从大量记忆与聊天记录中召回意思相关、但用词不同的内容。
- **文件交付**：将允许目录中的报告、表格、图片等结果直接发送回微信。
- **人设可自定义**：控制中心「人设」页分节编辑（`##` / `###`）、多套预设（内置「默认」「猫娘」）、保存与注入分离，改完下一条消息即生效。
- **模型自由配置**：看板可视化维护**供应商**与**可用模型**（任意 OpenAI 兼容接口），支持从供应商拉取模型列表、逐个测试连通性，并把模型分配给 `conductor` / `worker` / `pro`。
- **一键更新**：看板「环境配置 → G4W 程序更新」检查更新后用增量补丁升级，不必每次下载完整包。
- **过程可控**：可以开启中间进度、停止当前任务、切换模型或重新读取配置。

完整产品介绍见 [G4W使用说明.md](G4W使用说明.md)。

## 能力展示

### 从模糊描述定位原始记录

安装向量扩展后，G4W 会先进行语义召回，再读取原文核验，避免只凭“印象”回答：

| 用户的模糊描述 | 语义召回 | 原文核验 | 微信回复 |
| --- | --- | --- | --- |
| “有一天晚上我不舒服，因为什么？” | 找到意思相关的历史片段 | 定位原始聊天文件和具体位置 | 基于原文给出有依据的回答 |

下面是一次完整的实际演示：用户只提供模糊描述，G4W 先通过向量索引召回相关内容，再读取对应聊天原文核验，最后给出可追溯的回答。

<table>
  <tr>
    <td width="33%" align="center"><a href="images/2026-08-02-19-45-31.png"><img src="images/2026-08-02-19-45-31.png" width="100%" alt="G4W 根据模糊描述定位聊天记录"></a></td>
    <td width="33%" align="center"><a href="images/2026-08-02-19-45-52.png"><img src="images/2026-08-02-19-45-52.png" width="100%" alt="被定位到的原始微信聊天记录"></a></td>
    <td width="33%" align="center"><a href="images/2026-08-02-19-46-04.png"><img src="images/2026-08-02-19-46-04.png" width="100%" alt="G4W 后台检索并读取原始记录"></a></td>
  </tr>
  <tr>
    <td align="center"><sub>模糊提问后定位记录</sub></td>
    <td align="center"><sub>对应的原始聊天内容</sub></td>
    <td align="center"><sub>后台召回并读取原文</sub></td>
  </tr>
</table>

<sub>以上聊天内容是专门挑选的产品演示样例，用于展示语义召回和原文核验能力。</sub>

### 时间线与统计视图

日、周、月视图放在同一行展示，点击图片可以查看原图。

<table>
  <tr>
    <td width="33%" align="center"><a href="images/2026-06-12-11-48-58.png"><img src="images/2026-06-12-11-48-58.png" width="100%" alt="时间线日视图"></a></td>
    <td width="33%" align="center"><a href="images/2026-06-12-11-48-35.png"><img src="images/2026-06-12-11-48-35.png" width="100%" alt="时间线周视图"></a></td>
    <td width="33%" align="center"><a href="images/2026-06-12-11-48-04.png"><img src="images/2026-06-12-11-48-04.png" width="100%" alt="时间线月视图"></a></td>
  </tr>
  <tr>
    <td align="center"><sub>日视图</sub></td>
    <td align="center"><sub>周视图</sub></td>
    <td align="center"><sub>月视图</sub></td>
  </tr>
</table>

<sub>界面截图用于展示布局和能力，实际内容由用户自己的运行数据生成。</sub>

### 实际余额与用量

可以看到G4W在使用deepseek-v4-flash的情况下,一天使用成本不到一块钱。

<table>
  <tr>
    <td width="50%" align="center"><a href="images/2026-06-12-11-43-13.png"><img src="images/2026-06-12-11-43-13.png" width="100%" alt="实际余额和模型用量统计"></a></td>
    <td width="50%" align="center"><a href="images/2026-06-12-11-46-50.png"><img src="images/2026-06-12-11-46-50.png" width="100%" alt="实际模型请求事件明细"></a></td>
  </tr>
  <tr>
    <td align="center"><sub>实际余额、请求次数与 Token 用量</sub></td>
    <td align="center"><sub>实际请求事件与调用明细</sub></td>
  </tr>
</table>

<sub>以上余额和用量数据是专门保留的产品演示样例；实际成本取决于模型、调用频率和供应商计费规则。</sub>

### 控制中心 Dashboard

浏览器打开 `http://127.0.0.1:18180` 即可进入 G4W 控制中心（桌面壳 `G4W.exe` 双击即达）。首次进入会引导设置登录密码，后续重启不再需要重复登录。主要页面：

- **总览**：Worker / 记忆 / 知识库实时状态，一键启动 / 停止 G4W 服务，下次主动联系时间
- **待办**：与微信 `/todo` 同源的待办 / 已办清单，分页管理
- **记忆**：用户画像置顶 + 长期记忆档案检索
- **人设**：多套人设预设的分节编辑、保存 / 注入、Markdown 导入导出
- **时间轴 / 日记**：按天分布与统计、事件分类着色
- **知识库**：文档列表、PDF/TXT/Markdown/DOCX 预览与检索
- **服务与终端**：服务启停（含模型输出监视器）+ 实时日志终端
- **Worker**：后台任务列表 + 模型输出监视器（每条记录可查看输出原文）
- **环境配置**：供应商配置 + 模型配置（拉取 / 测试 / 添加 / 删除模型）、`conductor` / `worker` / `pro` 模型选择、网络偏好与向量检索安装、G4W 程序更新
- **外观**：微信绿 / Neko 粉 / 简洁蓝预设主题 + 自定义主题编辑器

<table>
  <tr>
    <td width="33%" align="center"><a href="images/dashboard-overview.png"><img src="images/dashboard-overview.png" width="100%" alt="G4W 控制中心总览"></a></td>
    <td width="33%" align="center"><a href="images/dashboard-workers.png"><img src="images/dashboard-workers.png" width="100%" alt="G4W 控制中心 Worker"></a></td>
    <td width="33%" align="center"><a href="images/dashboard-memory.png"><img src="images/dashboard-memory.png" width="100%" alt="G4W 控制中心长期记忆"></a></td>
  </tr>
  <tr>
    <td align="center"><sub>总览：实时状态与一键启停</sub></td>
    <td align="center"><sub>Worker：任务列表与模型输出监视器</sub></td>
    <td align="center"><sub>长期记忆：用户画像与记忆档案</sub></td>
  </tr>
  <tr>
    <td width="33%" align="center"><a href="images/dashboard-memory-brief.png"><img src="images/dashboard-memory-brief.png" width="100%" alt="G4W 控制中心记忆简报"></a></td>
    <td width="33%" align="center"><a href="images/dashboard-timeline.png"><img src="images/dashboard-timeline.png" width="100%" alt="G4W 控制中心时间轴"></a></td>
    <td width="33%" align="center"><a href="images/dashboard-services.png"><img src="images/dashboard-services.png" width="100%" alt="G4W 控制中心服务与终端"></a></td>
  </tr>
  <tr>
    <td align="center"><sub>记忆简报：画像时间轴与结构化记忆</sub></td>
    <td align="center"><sub>时间轴：每日活动分布与统计</sub></td>
    <td align="center"><sub>服务与终端：服务启停与实时日志</sub></td>
  </tr>
</table>

<sub>界面截图用于展示布局和能力，实际内容由用户自己的运行数据生成。</sub>

### 自定义主题

控制中心内置 **微信绿 / Neko 粉 / 简洁蓝** 三套预设主题（侧边栏"外观"一键切换，深浅色模式独立记忆）。此外支持**自定义主题**：

- 位置：设置 → **自定义主题**
- 可编辑全部界面变量：主色系、辅助色、分类色、背景/面板/文字/阴影，以及**界面圆角**（--radius）；深色与浅色模式各一套
- 保存后**立即生效**；配置存于 `G4W-data/dashboard-theme.json`，升级 G4W 不影响，手机/电脑访问保持一致
- 未配置时"自定义"与默认微信绿一致；"恢复默认"一键清空

参考配色示例（简洁蓝经典蓝风格）：主色 `#1e6fff`、圆角 `16px`、深色背景 `#141a23` / 浅色背景 `#f2f4f8`。

### 时间轴站点主题

看板「时间轴」页可以一键启动一个**独立的时间轴站点**（默认 `http://127.0.0.1:18181`）。该站点自带主题选择器，位置紧贴页面上的「日期」控件左侧，内置 **default / neko** 两套皮肤：

- 选择结果记在浏览器本地，刷新即换肤；也可以用 `?theme=neko` 直接分享
- 服务端默认主题由 `.env` 的 `G4W_TIMELINE_UI_THEME` 决定
- 两套皮肤在构建站点时一并释放，切换不需要重启任何服务

## 目录结构

便携包解压后根目录保持干净，日常只需双击 `G4W.exe`（控制中心桌面壳）或 `tools\` 里的启动脚本：

```text
G4W\
├── G4W.exe                  <- 控制中心桌面壳（推荐入口：托盘驻留、免登录态）
├── desktop\                 <- 桌面壳源码与构建脚本
├── tools\                   <- 初始化与运维脚本（1_prepare / 2_key / 3_env /
│                              4_login / 5_embedding / start / stop / uninstall 等）
├── runtime\                 <- Python 运行时、G4W 代码、GA 框架、运行数据
├── README.md / G4W使用说明.md
```

所有 `.bat` 脚本通过 `%~dp0..\` 定位便携包根目录，**必须保持 `tools\` 在包内原位**，不要单独复制脚本到别处。

## 从旧版本升级

### 3.0 及以后的日常升级（推荐）

看板「环境配置 → **G4W 程序更新**」→ 点「检查更新」→ 有一键更新时有进度条，实际下载的是**增量补丁**（几百 KB 量级），不必每次下载上百 MB 的完整包：

- 升级过程会自动停服、替换文件、并在结束后把 G4W 重新拉起来；替换前的文件备份在安装目录 `.g4w-update\backup-<版本>\`
- 网络不通时可在同一张卡片里打开「国内镜像加速」
- 手动升级：下载 `G4W-patch-<你的版本>-to-<新版本>.zip` → 托盘退出 G4W → `tools\stop_G4W_ga.bat` → 解压到**独立文件夹** → 双击 `apply-update.cmd`
- `runtime\G4W-data`（账号、记忆、时间线、日记、待办、人设、模型配置）**不会被更新覆盖**

### 从 2.0 升级到 3.x

⚠️ 与 2.0 相比目录结构变化较大（根目录脚本移入 `tools\`、新增桌面壳与首次使用向导），**不要直接在旧目录上覆盖解压**，请按以下步骤操作：

1. **备份**：将旧便携包整体复制一份到安全位置；空间有限时至少完整备份 `runtime\G4W-data`（记忆、账号登录、配置与待办全部在此）
2. **解压新包到全新目录**（不要解压进旧目录）
3. **迁移数据**：把旧包的 `runtime\G4W-data` 整个复制到新包对应位置——微信账号、会话、长期记忆、待办与配置全部保留，无需重新配置
4. **可选：迁移向量环境**：旧包已安装过语义检索时，一并复制 `runtime\G4W-embedding` 与 `runtime\G4W-vector-index`，可省去重新下载数 GB 的 torch 与模型（看板「环境配置」页会自动识别已装状态）
5. **首次启动**：双击 `G4W.exe` → 首次打开看板设置一次登录密码 → 老账号直接登录
6. **核对**：看板「服务」页确认主服务与 Embedding 状态正常；微信发送 `/vector status` 验证索引；「记忆」页确认用户画像与历史对话都在

注意：旧包根目录的 bat 脚本不要复制进新包（新包统一在 `tools\`，推荐以 `G4W.exe` 为日常入口）。

## 第一次使用

将便携包完整解压到普通可写目录（不要直接在 ZIP 预览窗口运行）。

**推荐方式：双击根目录 `G4W.exe`**，会自动弹出**首次使用向导**，按 4 步引导完成全部初始化：

1. **① 准备环境** — 用包内 Python 与离线依赖创建运行环境（通常 1-3 分钟，进度实时显示）
2. **② 模型 API Key（可跳过）** — 填任意 **OpenAI 兼容**服务的 Key（不强制 DeepSeek：通义 / Kimi / 火山 / 中转站都可以），保存在本机 `runtime\app\mykey.py`；也可以点「先跳过」，装好之后在控制中心「环境配置 → 供应商配置 / 模型配置」里再配置
3. **③ 环境配置** — 用户名、称呼、机器人名字、默认模型
4. **④ 扫码登录** — 二维码直接在向导内显示，微信扫码即完成

全部完成后**自动进入控制中心看板**（首次启动后端约 5~20 秒，向导页会显示进度；若没自动切换也可以点卡片上的按钮）。之后每次双击 `G4W.exe` 直接打开看板（托盘驻留、免重复登录）。

**手动方式（替代向导）**：也可以按 `tools\` 目录脚本编号依次运行初始化（`1_prepare_G4W_ga.bat` → `2_key_for_ga.bat` → `3_env_for_G4W.bat` → `4_login_G4W_ga.bat` → `start_G4W_ga.bat`）。

日常使用：双击 `G4W.exe`（自动拉起看板、托盘驻留）；停止服务用 `tools\stop_G4W_ga.bat`。

各脚本的详细说明见 [G4W使用说明.md](G4W使用说明.md)。

## 可选：安装向量语义检索

普通功能不依赖向量扩展。需要语义记忆和知识库召回时，运行 `tools\5_embedding_for_G4W.bat`，或打开看板 →「环境配置」→ EMBEDDING 卡片，选择网络偏好、测速选优后一键安装（进度在「服务与终端」页查看）。需预留约 4–8 GB 空间。

安装完成后，在微信中发送 `/vector on` 开启；`/vector status` 查看状态，`/vector off` 关闭。详细说明见 [G4W使用说明.md](G4W使用说明.md)。

## 可以怎样使用

不必背命令，直接在微信里描述需求即可，例如：

- “明天下午三点提醒我提交周报。”
- “把今天发生的事情整理成一篇日记。”
- “查一下我之前什么时候提到过这个问题，并给出原文依据。”
- “阅读这份 PDF，整理重点并生成 Markdown 报告。”
- “这个任务比较久，放到后台完成，结束后告诉我。”
- “统计本周各类活动花了多少时间。”

## 常用微信命令

| 命令 | 作用 |
| --- | --- |
| `/help` | 显示当前完整命令列表 |
| `/bind` | 将当前微信聊天绑定到工作区 |
| `/status` | 查看工作区、线程、模型和运行状态 |
| `/new` | 创建新线程草稿 |
| `/switch <threadId>` | 切换到指定线程 |
| `/stop` | 停止当前线程正在运行的任务 |
| `/reread` | 重新读取最新人设和操作指令 |
| `/checkin <最小分钟>-<最大分钟>` | 设置主动联系区间 |
| `/todo add\|list\|done\|del\|cancel` | 管理个人待办清单（check-in 时主动提醒） |
| `/turn on\|off\|status` | 控制是否显示中间执行进度 |
| `/worker_turn on\|off\|status` | 控制 Worker 中间进度汇报（默认关闭） |
| `/input on\|off\|status` | 控制完整 LLM Input 快照 |
| `/chunk <数字>` | 调整微信短回复合并字符数 |
| `/name <名称>` | 查看或修改用户名 |
| `/identity <称呼>` | 查看或修改身份、日常称呼 |
| `/gender <female\|male\|neutral>` | 查看或修改性别配置 |
| `/botname <名称>` | 查看或修改机器人名称 |
| `/model` | 查看或切换模型 |
| `/l4compress` | 手动触发长期记忆深度整理 |
| `/kb list` | 查看知识库文档 |
| `/kb remove <编号>` | 删除指定知识库文档 |
| `/kb rebuild` | 重建知识库索引 |
| `/vector status\|on\|off\|meta\|rebuild` | 管理向量扩展和索引 |

## 人设与称呼

用户名、日常称呼、性别和机器人名称可以在 `3_env_for_G4W.bat` 中配置，也可以使用 `/name`、`/identity`、`/gender` 和 `/botname` 动态修改。

**人设（写给 AI 的长期指令）在控制中心「人设」页维护**：

- 分节编辑：大节 `##`、小节 `###`，标题即输入框，正文支持完整 Markdown（表格、引用、列表原样保留）；可插入 / 删除 / 上下移动，大节之间自动生成分隔线
- 多套预设：内置「默认」（中性骨架，「基础定位」留白）与「猫娘」（角色扮演预设，默认不激活），也可以新建 / 复制 / 重命名 / 删除 / 从 Markdown 导入 / 导出
- **保存**只写当前打开的那套预设；**注入**才把它写成运行时人设，并让**当前正在对话的会话**立刻重读（其他会话在各自下一条消息生效）
- 顶部列出 4 个可用变量：`{{USER_NAME}}`、`{{USER_IDENTITY}}`、`{{BOT_NAME}}`、`{{USER_PRONOUN}}`，点击即复制
- 每次保存前自动备份，每个预设保留最近 3 份；文件位于 `runtime\G4W-data\memory\persona\`

改完人设后也可以在微信里发送：

```text
/reread
```

让当前会话重读一次人设与操作指令（缓存重载是立即的，下一条消息即生效）。

## 配置模型

G4W 不绑定任何一家模型服务，只要接口兼容 OpenAI 的 `/chat/completions` 即可。全部在控制中心「环境配置」里完成：

- **供应商配置**：填名称、接口地址（`apibase`）与 API Key → 「添加 / 更新供应商」。每行的「测试」会用该地址与 Key 拉一次模型列表（等于连通性检查）。密钥只存本机，界面只显示掩码
- **模型配置**：选一个供应商 → 「拉取模型列表」→ 对任意模型点「测试」会真发一次最小请求（行内显示耗时与回复片段，失败时原样带出上游错误）→ 选中的点「添加」即写入 `runtime\app\mykey.py` 成为可用模型；「可用模型」列表里也能逐条测试 / 删除（删除只移除该模型配置，原文件自动备份）
- **模型分工**：在「模型与工作区」里为 `conductor`（当前对话）、`worker`、`pro` 分别选择模型，保存后立即生效
- 页面只改动你要改的那几个变量，`mykey.py` 里其它手写配置、注释与空行会**原样保留**；GA 按文件修改时间自动重载，保存后**不需要重启**
- 内置常见服务商预设（DeepSeek / OpenAI / 通义百炼 / 阶跃 / 火山方舟 / 百度千帆 / 小米 MiMo / 腾讯 TokenHub / 自定义中转）；Anthropic 协议（Claude / Kimi / 智谱 / MiniMax / CC Switch 透传）目前需要手写 `native_claude_*` 配置

## 数据与隐私

除 README 中经过挑选的产品演示截图外，干净发布包不包含用户密钥、账号状态、运行时聊天记录、记忆、日记、时间线、知识库、向量模型或索引。这些内容只会在用户自己的电脑上配置或运行后生成。

以下内容不应出现在对外发布包中：

- `runtime\app\mykey.py`
- `runtime\app\.venv`
- `runtime\G4W-main\.env`
- `runtime\G4W-data`
- `runtime\G4W-embedding`
- `runtime\G4W-vector-index`
- 微信账号、用户记忆、模型文件和缓存

Dashboard 控制中心带登录密码保护（首次打开时设置，之后免重复登录）；如需局域网访问，请确认密码强度并自行评估暴露面。

模型请求会发送给用户配置的模型供应商，微信消息传输依赖微信服务，因此 G4W 不是完全离线产品。不要把已经运行过、包含个人数据的目录直接分享给其他人。

## 常见问题

### 提示找不到 Python

先运行 `tools\1_prepare_G4W_ga.bat`，或直接双击 `G4W.exe` 用向导的「① 准备环境」完成创建。必须保留完整产品目录（`tools\` 与 `runtime\` 同级），不要只复制 BAT 文件。

### 双击 G4W.exe 弹出的是向导而不是看板

说明初始化尚未完成（准备环境 / API Key / 环境配置 / 扫码登录四项中有未完成的项）。按向导走完四步后，再次启动即直接进入看板；已初始化想重配某项时，可删除对应配置文件（`runtime\app\mykey.py`、`runtime\G4W-main\.env`、`runtime\G4W-data\accounts`）或手动运行 `tools\` 脚本。

### 向量安装速度慢

重新运行 `tools\5_embedding_for_G4W.bat` 可以继续安装。也可以在看板「环境配置」页选择国内/国外网络偏好并测速选优后一键安装。Torch 默认使用“南京大学镜像 → 阿里云 → 官方源”的回退顺序，也可以通过 `G4W_TORCH_INDEX_URL` 指定其他可用镜像。

### 不想用 DeepSeek 可以吗

可以。G4W 不强制任何一家服务：只要接口兼容 OpenAI 的 `/chat/completions`，在看板「环境配置 → 供应商配置 / 模型配置」里填入地址与 Key，拉取并测试模型后添加，再到「模型与工作区」把 `conductor` / `worker` / `pro` 指过去即可。首次向导里的 API Key 步骤也可以直接跳过，装好之后再配置。

### 一键更新失败怎么办

先看提示信息：若提示需要完整包（例如安装目录没有版本戳），下载完整便携包解压覆盖即可（**不要删 `runtime\G4W-data`**）。更稳的手动方式见「从旧版本升级」章节；替换前的文件备份在安装目录 `.g4w-update\backup-<版本>\`。

### 如何从旧版本（2.0）升级

见上方「从旧版本升级」章节：**不要覆盖解压**。备份旧包 `runtime\G4W-data`（可选再加 `G4W-embedding` / `G4W-vector-index`）→ 解压新包到全新目录 → 复制数据 → 双击 `G4W.exe` 完成收尾。

### 能否移动整个目录

可以整体移动。不要只移动其中一部分（尤其 `tools\` 必须与 `runtime\` 保持同级）。移动后重新运行启动脚本即可使用基于当前目录计算的路径。

## 内置 GenericAgent Desktop

`tools\GenericAgent.exe` 和 `tools\readme_GA_desktop.txt` 属于内置的 GenericAgent Desktop 便携底座（归档备用）。G4W 的标准入口是 `G4W.exe` 或 `tools\` 下的 G4W 脚本，而不是直接启动 `GenericAgent.exe`。

## 使用边界

G4W 可以辅助执行、整理和复盘，但 AI 仍可能理解错误。涉及删除、对外发送、重要决策或关键资料修改时，应由用户确认。它不能替代医疗、法律或财务专业意见。

## 未来 TODO

- 优化 Todo 看板，让任务、提醒、今日事项和长期计划可以在一个统一视图里管理。

  ![](images/2026-06-12-11-49-21.png)
- 后续评估是否加入图片理解和贴纸相关能力。

## 致谢

感谢 [GenericAgent](https://github.com/lsdefine/GenericAgent) 与 [Cyberboss](https://github.com/WenXiaoWendy/cyberboss/tree/main) 的开发者。G4W 基于这两个项目的能力组合和实践思路进行整理与适配。

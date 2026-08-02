# G4W

> 把微信变成你的 AI 工作入口：能长期记住、主动提醒、后台执行，并把结果送回聊天窗口。

G4W（Generic Agent for WeChat）运行在 Windows 电脑上，通过微信提供持续在线的个人 AI 协作能力。它不只是回答问题，还可以维护长期上下文、调用后台 Worker、整理日记与时间线、管理提醒，并围绕本地资料建立知识库和向量索引。

> 本 Git 仓库是源码仓库，不包含便携 Python、离线 wheels 和 `GenericAgent.exe`。普通用户请从 Releases 下载完整 Windows 便携包；开发者可使用 `packaging\build_portable.ps1` 从 GenericAgent Desktop Portable 1.8 构建完整发布包。

当前发布底座：[GenericAgent Desktop Portable 1.8](https://github.com/lsdefine/GenericAgent/releases#release-desktop-portable-v0.1.8)。

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
- **长期记忆**：保存有来源依据的稳定事实，让后续对话保持连续。
- **后台 Worker**：搜索、资料整理、代码分析和文档生成等耗时任务可在后台完成。
- **提醒与主动联系**：支持固定时间提醒，也支持在设定区间内主动 check-in。
- **日记与时间线**：记录生活或项目事件，生成日、周、月视图与统计信息。
- **知识库**：管理 PDF、TXT、Markdown 等资料，并在回答时引用来源。
- **语义检索**：从大量记忆与聊天记录中召回意思相关、但用词不同的内容。
- **文件交付**：将允许目录中的报告、表格、图片等结果直接发送回微信。
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

## 第一次使用

先把压缩包完整解压到普通可写目录。不要直接在 ZIP 预览窗口中运行，也不建议放入需要管理员权限的系统目录。

请先从 Microsoft Store 安装 Windows Terminal。G4W 启动脚本会使用它分别打开服务日志和模型思考窗口。

按顺序双击根目录脚本：

1. `1_prepare_G4W_ga.bat`
   - 使用内置 Python 和离线 wheels 创建 GA 主环境。
   - 不依赖系统 Python，也不需要联网安装基础依赖。
2. `2_key_for_ga.bat`
   - 配置 DeepSeek API Key。
   - 密钥仅保存在用户自己的产品目录中。
3. `3_env_for_G4W.bat`
   - 配置用户名、日常称呼、机器人名称、模型和时间线主题。
4. `4_login_G4W_ga.bat`
   - 扫码登录专门用于 G4W 的微信账号。
5. `start_G4W_ga.bat`
   - 启动微信服务、后台 Worker 和模型输出监控。

以后日常使用通常只需双击：

```text
start_G4W_ga.bat
```

停止服务：

```text
stop_G4W_ga.bat
```

## 可选：安装向量语义检索

普通聊天、提醒、Worker、日记、时间线和关键词知识库不依赖向量扩展。只有需要更强的语义记忆和知识库召回时，才运行：

```text
5_embedding_for_G4W.bat
```

安装器会：

- 创建独立的 `runtime\G4W-embedding\.venv`，不向 GA 主环境安装 NumPy 或 Torch。
- 检测到 NVIDIA GPU 时安装 CUDA 12.8 通道的 PyTorch，否则安装 CPU 版。
- 优先通过南京大学 PyTorch 镜像下载，失败后依次回退阿里云和官方源。
- 自动下载 Qwen3-Embedding-0.6B，模型源失败时自动回退。
- 支持下载中断后重新运行脚本继续安装。

向量环境和模型通常需要额外预留约 4–8 GB 空间。

安装完成后，在微信中发送：

```text
/vector on
```

查看状态或关闭：

```text
/vector status
/vector off
```

如果需要自定义下载源，可在运行脚本前设置：

```text
G4W_HF_ENDPOINT
G4W_TORCH_MODE
G4W_TORCH_INDEX_URL
```

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
| `/turn on\|off\|status` | 控制是否显示中间执行进度 |
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

修改运行中的人设或操作指令后，发送：

```text
/reread
```

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

模型请求会发送给用户配置的模型供应商，微信消息传输依赖微信服务，因此 G4W 不是完全离线产品。不要把已经运行过、包含个人数据的目录直接分享给其他人。

## 常见问题

### 提示找不到 Python

先运行 `1_prepare_G4W_ga.bat`。必须保留完整产品目录，不要只复制 BAT 文件。

### 微信登录失败或二维码异常

- 确认电脑能够正常访问微信服务。
- 保持 `4_login_G4W_ga.bat` 窗口开启，直到登录完成。
- 先停止旧的 G4W 进程，再重新尝试登录。

### 启动后微信没有回复

- 确认 1—4 号步骤均已成功完成。
- 查看 `start_G4W_ga.bat` 打开的服务日志窗口。
- 确认电脑没有休眠，网络连接正常。
- 检查防火墙或安全软件是否拦截包内 Python。

### 向量安装速度慢

重新运行 `5_embedding_for_G4W.bat` 可以继续安装。Torch 默认使用“南京大学镜像 → 阿里云 → 官方源”的回退顺序，也可以通过 `G4W_TORCH_INDEX_URL` 指定其他可用镜像。

### 能否移动整个目录

可以整体移动。不要只移动其中一部分。移动后重新运行启动脚本即可使用基于当前目录计算的路径。

## 内置 GenericAgent Desktop

根目录的 `GenericAgent.exe` 和 `readme_GA_desktop.txt` 属于内置的 GenericAgent Desktop 便携底座。G4W 的标准入口是本 README 列出的 G4W 脚本，而不是直接启动 `GenericAgent.exe`。

## 使用边界

G4W 可以辅助执行、整理和复盘，但 AI 仍可能理解错误。涉及删除、对外发送、重要决策或关键资料修改时，应由用户确认。它不能替代医疗、法律或财务专业意见。

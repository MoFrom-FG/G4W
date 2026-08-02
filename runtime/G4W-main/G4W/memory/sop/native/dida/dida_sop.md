# 滴答 CLI SOP

> 滴答是G4W原生SOP能力，名称统一使用`dida`。命令只从`G4W_DIDA_COMMAND`读取，Token只留ENV引用。

---

## 一、调用方式

### 1.1 轻量封装（现有actions）

通过 `dida.py` 调用 `execute("dida", action, arguments)`：

```python
from G4W.features.native_sop import print_result
print_result("dida", "status|list|complete|focus", {...})
```

| action | 说明 | 参数 |
|--------|------|------|
| `status` | 检查CLI是否可用 | 无 |
| `list` | 读取未完成任务 | 可选：`limit` |
| `complete` | 完成任务 | 传入经list返回并确认的完整`task`对象 |
| `focus` | 记录专注时段 | 传入`task`、`started_at`、`ended_at`、`minutes` |

### 1.2 直接调用（复杂操作）

对于轻量封装未覆盖的操作（create/update/filter/project/habit等），直接用 `code_run` 调滴答CLI：

```python
import subprocess, json
result = subprocess.run(["dida", "task", "create", "--title", "xxx"], capture_output=True, text=True)
```

> ⚠️ **禁止猜任务ID、项目ID或命令路径**；执行前先 `list` 或 `filter` 核实。

---

## 二、通用操作规范（经验沉淀）

以下仅保留经过验证、可跨用户复用的操作规则；用户清单名、项目ID、任务内容、健康信息和习惯数据不得写入共享SOP。

### 2.1 任务创建 → 必须带提醒

```bash
# ✅ 正确：带 --reminders，手机日历才能同步
dida task create --title "示例提醒任务" --due-date "2026-08-01T18:00:00+08:00" --reminders "TRIGGER:PT-30M" --time-zone "Asia/Shanghai"

# ❌ 错误：不带 reminders → 手机日历不显示
dida task create --title "示例提醒任务" --due-date "2026-08-01T18:00:00+08:00"
```

### 2.2 习惯 → 不需要提醒

习惯（habit）靠手动打卡标记完成，**不需要** `--reminders` 参数。用户若表示「习惯不用设提醒 / 不用叫我」，按本域规则执行即可；**不要**把该偏好上升成全局记忆检索题，也不要为此改通用召回策略。

```bash
# ✅ 创建习惯
dida habit create --name "示例每日习惯" --repeat "RRULE:FREQ=DAILY"

# ✅ 每日打卡
dida habit checkin <habitId> --stamp 20260717
```

### 2.3 目标清单选择

- 任何清单ID必须先用`dida project list --json`查询，不得把真实项目ID写入共享SOP。
- 创建任务到指定清单时使用运行时查询结果：`dida task create --title "<title>" --project <projectId> --reminders "TRIGGER:PT-15M" --time-zone "Asia/Shanghai"`。
- 用户特定的默认清单、任务模板或习惯规则应保存在conversation用户记忆或ENV引用中。

### 2.4 习惯数据边界

- 习惯名称、健康内容和打卡记录属于用户数据，不写入共享SOP。
- 执行打卡前先查询当前账号的habit列表并确认目标ID。

### 2.5 安全规则

- 禁止猜任务ID、项目ID
- 创建/更新/完成操作前，先用 `list` 或 `filter` 获取确认
- Token过期时通知用户重新获取，不自行猜测

---

## 三、完整命令参考

所有命令使用 `dida <noun> <verb> [args...]` 模式。加 `--json` 获得原始API JSON。

### 3.1 任务 (task)

| 操作 | 命令 |
|------|------|
| 获取单个任务 | `dida task get <projectId> <taskId>` |
| 创建任务 | `dida task create --title <标题> [--project <projectId>] [--content <内容>] [--desc <描述>]` |
| 更新任务 | `dida task update <taskId> [--title <标题>] [--content <内容>] [--start-date <ISO>] [--due-date <ISO>] [--priority <0-5>]` |
| 完成任务 | `dida task complete <projectId> <taskId>` |
| 删除任务 | `dida task delete <projectId> <taskId>` |
| 已完成列表 | `dida task completed --projects <ids> [--start-date <ISO>] [--end-date <ISO>]` |
| 筛选任务 | `dida task filter --projects <ids> [--status <0/2>] [--start-date <ISO>] [--end-date <ISO>] [--priority <levels>] [--tag <tags>]` |

#### task create 详细参数

```
--title <title>         任务标题（必填）
--project <projectId>   清单ID（不填则进收集箱）
--content <content>     任务内容/备注
--desc <desc>           清单描述
--all-day               全天任务
--start-date <date>     开始时间（yyyy-MM-ddTHH:mm:ssZ）
--due-date <date>       截止时间（yyyy-MM-ddTHH:mm:ssZ）
--time-zone <tz>        时区（如 Asia/Shanghai）
--reminders <triggers>  提醒触发器（逗号分隔）
--repeat <rule>         重复规则（RRULE格式）
--priority <n>          优先级：0=无，1=低，3=中，5=高
--tags <tags>           任务标签（逗号分隔）
--items <json|csv>      子任务
```

### 3.2 清单/项目 (project)

| 操作 | 命令 |
|------|------|
| 列出清单 | `dida project list [--json]` |
| 获取详情 | `dida project get <projectId>` |
| 获取数据（含任务与分组） | `dida project data <projectId>` |
| 创建清单 | `dida project create --name <名称> [--color <hex>] [--view-mode <list/kanban/timeline>]` |
| 更新清单 | `dida project update <projectId> [--name <n>] [--color <hex>]` |
| 删除清单 | `dida project delete <projectId>` |

### 3.3 习惯 (habit)

| 操作 | 命令 |
|------|------|
| 获取单个 | `dida habit get <habitId>` |
| 列表 | `dida habit list` |
| 创建 | `dida habit create --name <名称> [--goal <n>] [--repeat <RRULE>] [--unit <u>] [--type <type>]` |
| 更新 | `dida habit update <habitId> [--name <n>] [--goal <n>]` |
| 打卡 | `dida habit checkin <habitId> [--stamp <YYYYMMDD>]` |
| 打卡记录查询 | `dida habit checkins [--start-date <ISO>] [--end-date <ISO>]` |

### 3.4 专注/番茄钟 (focus)

| 操作 | 命令 |
|------|------|
| 列表 | `dida focus list [--start-date <ISO>] [--end-date <ISO>]`（最大30天） |
| 创建 | `dida focus create --type <0|1> --start-time <ISO> --end-time <ISO> [--task-id <id>] [--note <text>] [--duration <秒>]` |
| 删除 | `dida focus delete <focusId>` |

### 3.5 标签 (tag)

| 操作 | 命令 |
|------|------|
| 列出标签 | `dida tag list [--json]` |
| 创建标签 | `dida tag create [options]` |

### 3.6 倒数日 (countdown)

| 操作 | 命令 |
|------|------|
| 列表 | `dida countdown list` |

---

## 四、常用操作流程例子

### 4.1 快速添加任务到收集箱

```bash
# 最简单的任务
dida task create --title "买牛奶"

# 带截止时间和提醒（⚠️ 必须带 reminders）
dida task create --title "交作业" --due-date "2026-07-15T23:59:59+08:00" --reminders "TRIGGER:PT-30M" --time-zone "Asia/Shanghai"

# 高优先级 + 标签
dida task create --title "复习考试" --priority 5 --tags "考试,重要" --content "第三章到第五章"
```

### 4.2 添加任务到已确认的目标清单

```bash
dida task create --title "<title>" --project <projectId> --reminders "TRIGGER:PT-15M" --time-zone "Asia/Shanghai" --due-date "2026-08-01T18:00:00+08:00"
```

### 4.3 健康习惯打卡

```bash
# 先查习惯ID
dida habit list

# 在用户明确确认目标习惯和日期后打卡
dida habit checkin <habitId> --stamp <YYYYMMDD>
```

### 4.4 查看今日待办

```bash
dida task filter --projects <projectId> --status 0 --json
```

### 4.5 查看已完成任务

```bash
dida task completed --projects <projectId> --start-date "2026-07-01T00:00:00Z" --end-date "2026-07-14T23:59:59Z"
```

### 4.6 提醒触发器格式

```
TRIGGER:PT0S       # 截止时提醒
TRIGGER:PT-15M     # 提前15分钟
TRIGGER:PT-1H      # 提前1小时
TRIGGER:PT-1D      # 提前1天
多个提醒：--reminders "TRIGGER:PT-1D,TRIGGER:PT-1H"
```

---

## 五、常用查询

### JSON输出对接脚本

```bash
dida task filter --projects <inboxProjectId> --status 0 --json | python -c "import sys,json; [print(t.get('title')) for t in json.load(sys.stdin)]"
```

### 认证状态检查

```bash
dida auth status    # 查看登录状态
dida --version      # 确认安装
```

---

## 六、常见坑 & 注意事项

1. **projectId由当前账号查询结果提供**：不同账号和清单的ID不同，不能从示例或历史记录复用
2. **task create的--project是可选的**：不指定则进入收集箱（Inbox）
3. **--json返回原始API结构**：解析时注意数组/对象结构
4. **日期格式**：支持ISO 8601，建议带时区（如 `+08:00` 或 `Z` 结尾的UTC时间）
5. **提醒触发器格式**：`TRIGGER:PT<偏移>`，正偏移=截止时触发，负偏移=提前触发
6. **Token过期需重新认证**：`dida auth token <new-token>` 或重新OAuth登录
7. **filter命令的--status参数**：0=未完成，2=已完成
8. **filter命令可以查多个清单**：`--projects id1,id2`
9. **task update需要先知道taskId**：可通过filter或get获取
10. **focus list最多只能查30天范围**
11. **任务必须带`--reminders`才能使手机日历同步**：这是硬性规则，创建task时务必加提醒参数
12. **习惯（habit）无需reminders**：靠手动打卡完成
13. **禁止猜ID**：任何ID必须先查再操作
14. **TickTick（国际版）与滴答（国内版）账号不互通**：Token和CLI命令互不兼容

---

## 七、验证可用性

```bash
dida --version              # 确认安装
dida auth status            # 确认认证状态
dida project list           # 确认能读取数据
dida project list --json      # 先查询当前账号的收集箱/清单ID
dida task filter --projects <inboxProjectId> --status 0 --json  # 查看收集箱待办
```

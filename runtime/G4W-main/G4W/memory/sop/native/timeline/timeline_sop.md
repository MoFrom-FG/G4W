# G4W 时间线 SOP

时间线是G4W原生确定性能力，不是专用LLM Tool。使用GA `code_run`调用固定入口：

```python
from G4W.features.native_sop import print_result
print_result("timeline", "read|list|taxonomy|write|delete|build|serve|screenshot", {...})
```

### context 路径定位规则

`print_result` 默认读取当前 conversation 的 `G4W-context.json`。

**查找顺序（按优先级）：**
1. `code_run` 当前工作目录下的 `G4W-context.json`
2. 通过 `code_run` 搜索 conversation runtime 目录（路径模式：`runtime/G4W-data/memory/conversations/*/`）
3. 从 `.env` 或启动日志提取 `G4W_CONVERSATION_DIR`
4. 以上都找不到 → **停止执行**，使用 `code_run` 搜索实际路径，不要凭印象猜测

**建议做法：** 调用前先用 `code_run` 确认 context 文件的实际位置，拼接绝对路径传入。

## 二、时间线截图（核心操作）

时间线截图不依赖MCP。统一使用G4W原生确定性入口：构建原版时间线页面 → Headless Edge精确截图 → 写入耐久Outbox → 当前Round结束后发送微信。

### 周视图

```python
from G4W.features.native_sop import print_result
print_result("timeline", "screenshot", {
    "range": "week",
    "date": "2026-07-13",
    "send": True
})
```

`date`为周一日期；不传则使用页面默认最新周。

### 日视图

```python
from G4W.features.native_sop import print_result
print_result("timeline", "screenshot", {
    "range": "day",
    "date": "2026-07-17",
    "send": True
})
```

### 月视图

```python
from G4W.features.native_sop import print_result
print_result("timeline", "screenshot", {
    "range": "month",
    "month": "2026-07",
    "send": True
})
```

**参数说明：**

| 参数 | 作用 | 默认值 |
|:---|:---|:---|
| `range` | day/week/month | week |
| `date` | 日期（日视图）或周一日期（周视图） | 页面默认最新日期/周 |
| `month` | 月份（月视图用） | 页面默认最新月 |
| `send` | 是否发送微信 | false |
| `output_file` | 指定截图输出路径 | 自动生成 |
| `width` / `height` | Headless页面尺寸 | 1680 / 1400 |

### 截图发送机制

`send=True` 时经Outbox held → 当前Round完成释放 → 发送微信。不要使用MCP脚本或私有`python-sops/timeline_screenshot.py`，无MCP环境会失败。

## 三、失败处理流程

截图失败时按以下顺序处理：

```
① 检查 context 文件是否存在 → 按上方定位规则重新搜索
② 确认日/月/周参数存在于`dashboard-data.json`的`ranges.day/week/month`
③ 不指定日期或月份，让页面使用默认最新范围重试
④ 仍失败 → 告知用户截图异常，询问是否降级为文字版时间线
```

## 四、写入规则

- 时间戳使用北京时间并携带`+08:00`；跨日事件拆分。
- 写入前调用`taxonomy`，每个事件填写真实`categoryId/subcategoryId`。
- 睡眠/小睡：`rest.sleep/rest.nap`；编程/重构/测试：`work.coding`；聊天：`social.chat`；吃饭：`life.meal`。
- 页面颜色来自分类，禁止为省事全部写成`life`。
- 修改旧日期前先read；普通补充使用append/upsert语义，删除需用户明确要求。

### 写入示例

```python
from G4W.features.native_sop import print_result

# 先查可用分类（可选，确认分类ID）
print_result("timeline", "taxonomy", {})

# 写入事件
print_result("timeline", "write", {
    "events": [
        {
            "startAt": "2026-07-17T21:21:00+08:00",
            "endAt": "2026-07-17T22:50:00+08:00",
            "categoryId": "work",
            "subcategoryId": "work.coding",
            "title": "技术讨论与配置"
        }
    ]
})
```

**事件字段说明：**

| 字段 | 类型 | 必填 | 说明 |
|:---|:---|:---:|:---|
| `startAt` | string | ✅ | ISO8601格式，含时区 `+08:00` |
| `endAt` | string | ✅ | 同上 |
| `categoryId` | string | ✅ | 顶级分类ID，通过 taxonomy 获取 |
| `subcategoryId` | string | ✅ | 子分类ID，格式 `父类.子类` |
| `title` | string | ✅ | 事件标题，简短概括 |
| `description` | string | ❌ | 事件描述/备注 |

**常用分类速查（taxonomy完整返回）：**

| categoryId | 可用 subcategoryId |
|:---|:---|
| `life` | `life.meal` / `life.hygiene` / `life.chores` / `life.shopping` / `life.errand` / `life.other` |
| `work` | `work.coding` / `work.meeting` / `work.writing` / `work.communication` / `work.other` |
| `study` | `study.reading` / `study.course` / `study.practice` / `study.review` / `study.other` |
| `exercise` | `exercise.walk` / `exercise.workout` / `exercise.stretch` / `exercise.other` |
| `entertainment` | `entertainment.video` / `entertainment.game` / `entertainment.social_media` / `entertainment.music` / `entertainment.other` |
| `health` | `health.rest` / `health.medication` / `health.pain` / `health.hospital` / `health.other` |
| `social` | `social.chat` / `social.call` / `social.family` / `social.other` |
| `care` | `care.pet` / `care.household` / `care.self` / `care.other` |
| `travel` | `travel.commute` / `travel.transit` / `travel.other` |
| `rest` | `rest.sleep` / `rest.nap` / `rest.idle` / `rest.other` |

## 五、速查备忘录

| 需求 | 方式 | 关键参数 |
|:---|:---|:---|
| 读时间线 | print_result | `print_result("timeline", "read", {...})` |
| 写事件 | print_result | `print_result("timeline", "write", {"events":[{"startAt":"...","endAt":"...","categoryId":"...","subcategoryId":"...","title":"..."}]})` |
| 查分类 | print_result | `print_result("timeline", "taxonomy", {})` |
| 周截图 | print_result | `{"range":"week","date":"YYYY-MM-DD","send":true}` |
| 日截图 | print_result | `{"range":"day","date":"YYYY-MM-DD","send":true}` |
| 月截图 | print_result | `{"range":"month","month":"YYYY-MM","send":true}` |
| 快捷截图 | print_result | `print_result("timeline", "screenshot", {"send": True})` |

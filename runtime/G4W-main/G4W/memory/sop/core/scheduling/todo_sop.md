# Todo 任务清单

用户交代的**待办/持续任务**存入确定性任务清单(`todo-state.json`,按微信账号隔离)。
无时间的任务在每次主动 check-in 时注入给你(OPEN_TODOS 菜单);有时间的任务到点触发
`system.scheduled(kind=todo)` 事件;循环任务按周期自动重排。**所有未完成任务都会出现在
check-in 注入里,由你判断优先级并督办。**

## 入口

- 微信命令(用户手动):`/todo add|list|done <id>|del <id>|cancel <id>` — 用户显式操作,直接生效。
- LLM 能力(你):经 GA `code_run` 调用 `print_result("todo", "todo_add|todo_list|todo_done|todo_cancel|todo_del", {...})`。

## 能力

```
todo_add      {text, due_at?, recurrence_seconds?}   添加任务(due_at 为带时区 ISO,无时区按 Asia/Shanghai)
todo_list     {}                                     列出当前账号全部任务
todo_done     {todo_id, confirm_text?}               标记完成(写完成凭证)
todo_cancel   {todo_id}                              用户主动取消
todo_del      {todo_id}                              物理删除(仅用户明确要求时)
```

## 铁律

1. **创建前必须 ask_user 确认**：用户表达任务意图但未明确说"记下来/帮我记/添加"时，
   调用 `ask_user` 询问"要我记下这件事吗?"；用户确认后才 `todo_add`。
   用户明确命令式表述("记一下…""提醒我…""帮我设个…")可直接添加。
2. **只有用户确认完成才 `todo_done`**：不能仅凭"看起来做完了"标记完成；用户说
   "完成了/搞定了/不用了"才调用。完成后写 `confirm_text`(用户原话)留凭证。
3. **取消/删除必须用户主动**：用户说"取消吧/删了"才 `todo_cancel`/`todo_del`。
4. 到点未完成的任务是**督办对象**：继续在 check-in 菜单中出现,你应主动询问状态,
   而不是忽略。拿不准就问(ask_user)。
5. 循环任务(`recurrence_seconds`)到点自动重排,不要重复添加。

## 与提醒的关系

- "每天 9 点提醒喝水" = `todo_add {text:"喝水", due_at:"09:00", recurrence_seconds:86400}`
- "周五交房租" = `todo_add {text:"交房租", due_at:"2026-08-15T12:00:00+08:00"}`
- "每次主动找我时提醒我吃药" = `todo_add {text:"提醒我吃药"}`(无时间,每次 check-in 注入)
- 旧版 `schedule_create`(reminder)仍兼容,新任务一律用 todo。

## 完成演示

```python
print_result("todo", "todo_add", {"text": "喝水", "due_at": "09:00", "recurrence_seconds": 86400})
print_result("todo", "todo_list", {})
print_result("todo", "todo_done", {"todo_id": "t1", "confirm_text": "喝了"})
```

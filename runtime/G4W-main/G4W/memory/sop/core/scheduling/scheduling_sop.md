# 提醒与随机 Check-in

管理提醒、延迟回复、周期任务和随机主动check-in。check-in时间从最后一个用户可见Round结束后重新计算。

使用GA `code_run`调用固定入口：

```python
from G4W.features.native_sop import print_result
print_result("scheduling", "schedule_create|schedule_list|schedule_cancel", {...})
print_result("scheduling", "checkin_configure|checkin_status|checkin_disable", {...})
```

- `due_at`必须是带时区ISO时间；无时区按Asia/Shanghai解释。
- check-in区间以分钟传入；配置同时原子更新ENV和确定性状态。
- 普通微信`/checkin`命令仍优先走命令路由，不需要LLM调用。

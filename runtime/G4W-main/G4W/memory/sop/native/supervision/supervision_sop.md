# G4W 自制力监督 SOP

监督状态由确定性状态机维护，Conductor负责选择动作和微信措辞；长期Supervisor Worker只做分析建议。

使用GA `code_run`调用：

```python
from G4W.features.native_sop import print_result
print_result("supervision", "status|open|select|schedule|start|extend|complete|interrupt|exception", {...})
```

- `open`后如需要持续策略分析，使用G4W Worker工具创建/恢复`worker.supervisor`。
- 倒计时、链条、完成和中断必须写确定性状态，不得凭对话自行计算。
- 滴答同步失败不抹掉本地完成事实；把`syncError`保留并自然告知。
- Worker建议必须由Conductor验收，不直接面向微信。

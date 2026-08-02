# G4W 日记 SOP

日记按conversation写入`summaries/diary/YYYY/MM`，只记录有明确意义的情绪、决定、关系或连续事件。

```python
from G4W.features.native_sop import print_result
print_result("diary", "append|read|list", {...})
```

不要把微信聊天全文、普通寒暄或纯客观时间块抄入日记；客观时间块优先写Timeline。

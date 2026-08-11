# 微信文件发送 SOP

只有用户明确要求发送现有文件或原生SOP生成的图片时使用。通过GA `code_run`调用固定入口：

```python
from G4W.features.native_sop import print_result
print_result("file-send", "send", {"path": r"文件绝对路径"})
```

固定入口校验文件必须位于G4W state或当前workspace，写入耐久Outbox，并根据`G4W-context.json`绑定正确sender、Round和Turn。禁止直接修改Outbox JSON或自行调用微信上传接口。

## 发送状态确认（避坑）
- 查「是否已发送成功」以发送记录中的 `status: sent` 为准，不要只翻 `media/outbox` 文件夹看有没有文件：文件消费后会被移走，文件夹只剩旧文件，会误判「上次没发成功」而重复入队（2026-08-04凌晨教训）。
- 收到用户说「明明收到了为什么还在队列」时，先查发送记录核实 status，再决定是否重发。

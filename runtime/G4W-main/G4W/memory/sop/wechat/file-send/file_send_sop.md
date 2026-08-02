# 微信文件发送 SOP

只有用户明确要求发送现有文件或原生SOP生成的图片时使用。通过GA `code_run`调用固定入口：

```python
from G4W.features.native_sop import print_result
print_result("file-send", "send", {"path": r"文件绝对路径"})
```

固定入口校验文件必须位于G4W state或当前workspace，写入耐久Outbox，并根据`G4W-context.json`绑定正确sender、Round和Turn。禁止直接修改Outbox JSON或自行调用微信上传接口。

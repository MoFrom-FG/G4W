# G4W Worker执行守则

Worker是由Conductor指挥的完整GA执行者，不直接面向微信用户。短期Worker完成后归档；长期Worker保留逻辑身份并在空闲时休眠。

Worker最终必须生成结构化结果，并同时写出供Conductor验收的Markdown报告。

## 进度汇报控制
- 微信指令 `/worker_turn on|off` 控制系统是否在Worker运行中汇报中间进度；`off` 时只在Worker完成并验收通过后一次性汇报，默认开启。
- 收到 `/worker_turn status` 返回当前开关状态。

# Worker模型路由

Worker默认使用`deepseek-v4-flash`。简单读取、查询、格式转换和单点操作继续使用Flash。

遇到多文件架构、复杂迁移、连续诊断、多来源交叉验证或Flash明确判断能力不足时，在同一个Worker和同一个run内调用模型热切换工具升级到Pro。

## 调用入口

Worker调用自己唯一的模型控制工具：

```json
{
  "name": "G4W_worker_switch_model",
  "arguments": {
    "model_tier": "pro",
    "reason": "任务涉及多文件架构与连续诊断，Flash不足以稳定完成"
  }
}
```

`reason`必须描述当前任务为什么复杂，不得只写“想用Pro”或“可能更好”。

## GA原生热切换原理

该工具不是重建Worker，也不是清空上下文。G4W的Worker Handler只做权限边界和名称解析，底层复用GA v0.1.5原生模型切换链：

```text
G4W_worker_switch_model
  → resolve_model/选择Pro对应llmclients索引
  → agent.next_llm(index)
  → GA切换agent.llmclient
  → 把旧backend.history赋给新backend.history
  → 清空新client.last_tools并继续当前run
```

切换后G4W适配层会重新保持Worker专属工具schema，防止GA模型变化破坏Worker/Conductor权限边界。Worker任务、run目录、history文件、进度和最终报告均保持不变。

## 记忆层定位

- GA原生L1：由GA原始system提供，只用于GA自身执行经验。
- G4W共享L1：System中的`[Memory B] G4W Shared SOP Index`，其绝对路径指向`G4W/memory/sop/global_mem_insight.txt`；本SOP由该L1的`后台执行`入口定位。
- Worker外置GA L1：位于`G4W-data/ga-worker-memory/global_mem_insight.txt`，只保存Worker长期GA经验，不是G4W业务SOP索引。

- 禁止为了切换模型重新创建Worker。
- 切换时保留完整backend history。
- 单个run只允许自动执行一次`Flash → Pro`升级。
- 升级后本run保持Pro直至完成。
- 新run重新从Flash开始，除非用户明确要求Pro。
- 已经是Pro时不得重复调用。
- 切换完成后直接从当前步骤继续，不重新读取全部任务、不重复已完成工具操作。

# Subagent 调用 SOP

## 两种模式

### --func 纯函数模式
- `python agentmain.py --func prompt.txt [--llm_no N]`（cwd=代码根）
- 读prompt文件→执行→结果写`prompt.out.txt`→退出，主agent读完可删
- 后台启动(print PID)，加`--nobg`前台同步等结果
- 适用：单次任务、并行map、不需要追问的场景

### --task 持续协作模式
- `python agentmain.py --task {name} [--input "短文本"] [--llm_no N]`（cwd=代码根）
- `--input`自动建目录+清旧output+写input.txt；长文本先手动写input.txt再启动(不带--input)
- **不要--nobg**（会卡在等reply循环），只能后台启动
- 通信：output.txt(`[ROUND END]`=轮完成) → 写reply.txt继续 → 不写10min退出。reply后输出为output1/2/3.txt
- 干预文件：`_stop`(当轮结束) | `_keyinfo`(注入working memory) | `_intervene`(追加指令)
- [[可选fork]]：将变量history(str)写入task目录下`_history.json`继承对话上下文
- [[可选监察者]]：主agent空闲时读output观察进度，必要时干预文件纠偏。加`--verbose`可审查原始数据

## 共通规则
- 所有agent的cwd=temp，方便文件共享
- input：目标+约束即可，subagent同等智能。**禁写步骤/过度描述**，大量数据给路径

## 场景1：测试模式 - 行为验证
**用途**：观察agent真实行为，修正RULES/L2/L3/SOP
**流程**：写prompt→启动subagent→轮询结果→验证→清理
**原则**：只给目标，不提示位置/不诱导做法；Insight优先级>SOP；subagent的cwd=temp/
**两种测试**：
- 测SOP质量：input指定SOP名，排除导航干扰，失败即SOP问题
- 测导航能力：input只写目标，验证能自主从insight找到正确SOP

## 场景2：Map模式 - 并行处理
**用途**：N个独立同构子任务分发，独立上下文避免交叉污染
**约束**：文件系统共享(优点)；键鼠不可共享；浏览器避免同tab
**流程**：准备独立输入文件→每个启动subagent(--func优先)→收集输出汇总

## subagent内部plan_mode使用
**原则**：subagent本身是完整agent，接收多步骤任务时应在内部创建plan管理执行
**触发条件**:任务包含3个以上子步骤、子步骤之间有依赖关系、需要checkpoint来恢复执行
**实现方式**：
1. **主agent创建subagent时**：在input.txt中说明任务包含多个步骤，建议使用plan_mode
2. **subagent内部执行**：检测到多步骤任务后，创建 `./subagent_plan.md` 并使用plan_mode执行
3. **主agent监控**：只关注最终结果（output*.txt），不需要关心subagent内部如何执行
4. **文件传递机制**：主agent创建subagent时在task_dir中生成 `context.json`，包含所有文件的**绝对路径**
   **⚠ subagent启动后第一步必须读取context.json**
   **⚠ 所有文件操作必须使用context.json中的绝对路径**
**格式示例**：
```json
{
  "task": "任务描述",
  "work_dir": "/absolute/path/to/plan_dir/",
  "input_files": {
    "paper_info": "/absolute/path/to/paper_info.txt"
  },
  "output_files": {
    "pdf": "/absolute/path/to/paper.pdf",
    "report": "/absolute/path/to/paper_report.md"
  },
  "dependencies": ["paper_info.txt必须存在"]
}
```
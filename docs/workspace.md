# 工作区：接入 MCP 工具与技能包

> 目标只有一个：**别让课脉变成一座孤岛**。
> 学生手上的 SPICE 仿真、FPGA 烧板、Word 报告工具都已经在跑，
> 课脉要做的是把它们接进来，而不是另造一套。

---

## 一、为什么不自己定格式

课脉直接读这两份**已有的**配置：

| 类型 | 位置 | 格式 |
|---|---|---|
| MCP 服务器 | `~/.workbuddy/mcp.json`（项目内 `.workbuddy/mcp.json` 优先） | Claude Desktop 标准 `mcpServers` |
| 技能包 | `~/.workbuddy/skills/<name>/SKILL.md` | YAML frontmatter + 正文 |

理由：用户已经配好了 5 个 MCP 服务器（ltspice、quartus、blender、multisim、UUMit）
和 21 个技能包。自己定格式意味着这些全部作废，而"迁移成本"是接入功能最常见的死因。

**零迁移**是这套设计唯一重要的指标。

---

## 二、MCP 协议实现

`backend/app/mcp_client.py`，stdio 传输，NDJSON。

```
→ initialize                 （带 protocolVersion，等响应）
→ notifications/initialized  （通知，无 id，不等响应）
→ tools/list                 （拿工具清单）
→ tools/call                 （调用）
```

三个容易写错的地方：

1. **握手顺序**：`initialize` 必须等响应，然后发 `notifications/initialized`（这条**没有 id**，服务器不会回）。
   顺序错了，多数服务器会静默不响应后续请求。
2. **id 配对**：响应可能乱序到达（并发调用时）。所以按 `id` 匹配，不匹配的丢弃，不能"读一行当答案"。
3. **非协议输出**：服务器的 stderr 是日志，stdout 偶尔也会混入启动横幅。解析失败的行跳过，不能崩。

超时策略：
- 单次调用默认 45 秒，工具执行类可传到 300 秒
- 界面探测压缩到 12 秒（一个没响应的服务器不该让整页卡住）
- **失败冷却 60 秒**：连不上的服务器记一次时间戳，之后的刷新直接返回上次的错误，
  不再重复等——否则每次打开工作区都要多等十几秒

---

## 三、技能包：渐进式加载

一个 `SKILL.md` 正文常常几千字。21 个全塞进上下文 = 几万 token，还没开始干活就烧完了。

所以分两级：

```
清单阶段   只读 frontmatter 的 name + description（21 个加起来约 3KB）
命中之后   才读正文（load_skill）
```

`match_skills(query)` 做粗排：名字命中权重 3，描述命中权重 1。真正的语义匹配留给模型。

frontmatter 用手写解析而不是 YAML 库：技能包的 description 经常是几百字长句、
带冒号和引号，完整 YAML 解析器在这些地方反而容易炸，而我们只要 `key: value`。

---

## 四、信任边界：不交给模型自动调用

MCP 工具能执行任意本地命令——`run_code` 能跑 Python，`run_experiments` 能启动 LTspice，
`quartus` 那套能编译和烧板子。

所以第一版坚持：

> **工具只能由人在界面上点，模型看不到这个能力。**

`workspace_api.py` 里没有把工具注册进任何模型上下文，`allow_auto` 这个概念也不存在。
界面上每个工具都显示 `只读` / `会改东西` 徽章（来自 MCP 的 `annotations`），
点开后的提示写明「工具直接跑在这台机器上，结果不会发给模型」。

放开自动调用需要一套可信的确认流程（预演、diff、回滚），那是另一个工程量的事。

---

## 五、加载体验：壳先出来

首次探测实测 19 秒（两个服务器握手 + 拉工具）。如果界面等它，就是个白屏。

改成三段：

```
probe=false   0.3 秒   只读配置，不启动任何子进程 → 壳和技能列表先出来
probe=true   19.0 秒   真去握手、拉工具 → 工具列表填进已有卡片
probe=true    0.1 秒   全部命中缓存 + 失败冷却
```

前端 `load(false).then(() => load(true))`，界面不会跳，只是卡片里的工具从「0 个」变成「8 个」。

---

## 六、实测

```
GET /workspace/overview?probe=false   → 0.3s，伺服器配置 + 21 个技能
GET /workspace/overview?probe=true    → 19.0s，ltspice 8 个工具，UUMit 56 个工具
GET /workspace/overview?probe=true    → 0.1s（缓存命中）

POST /workspace/mcp/call
  {server: "ltspice", tool: "jobs", arguments: {"action": "list"}}
  → is_error: false, text: "Listed 16 of 16 circuit group(s)"
```

单测 10 项（`backend/tests/test_workspace.py`），用一个真的子进程
（`fixtures/fake_mcp.py`）验证握手、id 配对、错误传播、关闭幂等，
以及技能包的 frontmatter 解析、同名校验、搜索排序。

---

## 七、还没做：「整理学习文件」

这是用户提的第一个用例，但**顺序上应该放在接入之后**：

```
扫描目录 → 按课程/类型归类建议 → 人工确认 → 执行移动
```

为什么先不做：
1. 归类规则要靠 MCP 之外的本地逻辑（文件类型 + 课程名匹配），不依赖接入层也能做；
2. 一旦要动用户的文件，就必须先有「预览 → 确认 → 可撤销」的完整流程，
   否则一次误判就是灾难；
3. 它更适合作为一个**内置技能包**（`workspace-organizer`）存在，
   而不是硬编码在代码里——这样规则可改、可分享、可被模型读懂。

所以下一步是：写一个 `SKILL.md` 描述整理规则 + 一个只生成清单不执行的后端接口，
让用户先看清单，确认了再执行。

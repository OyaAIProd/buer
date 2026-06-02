# BUER 设计文档 v2.0

**全称**:BUER（不二法门）：一个 SDT-grounded 的 AI 编码 agent 漂移监测系统
**定位**:用结构数据帮 vibe coder + agent 更安全地写代码，两面：**报警**（事后检测 agent 打转/越界/改坏，先提醒 agent 自纠、再升级用户）+ **辅助**（事中用同一份结构数据帮 agent 和用户框定该做什么：补测试、看改动影响、在对的时机提交）。报警减少漏网，辅助减少出事。
**许可**:BSL 1.1
**版本说明**:v2.0 是 v1.1.r8 的干净重组（B 方案）。前七个版本（r1-r7）框架定位变了三次（代码库即生命体 → DI 借用 Bio 特征 → 纯 DI/SDT），每次在旧版本上打补丁，积累了大量框架化石。v2.0 基于已确立的纯 DI/SDT 定位重新组织，丢弃所有化石，场景驱动。演进历史不进设计正文。

---

# 文档组织

本文档分五部分，前四部分给实际使用者（理解 BUER 干什么、怎么用），第五部分给严格性审计（验证 SDT 依据、防漂移）：

- **第一部分　BUER 是什么**：真实工作流、拦截的问题清单、明确不做什么、两步响应机制、受众定位
- **第二部分　BUER 拦的每类问题**：每类问题统一三段式（真实场景 → BUER 怎么拦 → SDT 依据指针）
- **第三部分　节点横向关系（debug 加速）**：bug 在 A 根因在 B 的场景，BUER 给影响域 + 根因方向（不给解决方案）
- **第四部分　工程实现**：schema、MCP 工具、hook 集成、JUnit XML 接入、savings
- **第五部分　SDT 映射与严格性**：SDT apparatus 速查、每个信号的依据、Trade-off 总账、降级清单、防漂移验证记录

---

# 第一部分　BUER 是什么

## 1.1 一个真实工作流

Sarah 用 Claude Code 开发一个 web 应用。她不是资深工程师，更多靠描述需求让 agent 写代码：典型的 vibe coding。

她遇到的真实困扰：

- agent 修一个 bug，改了半天没修好，她不知道 agent 已经在原地打转，白烧了 token 和时间
- agent 改着改着，把不该动的模块也改了，等她发现已经一团乱
- agent 复制粘贴生成一堆几乎一样的代码，技术债悄悄堆积
- 她装了个新工具，第一天完全不知道这工具看到了什么、能帮她什么

BUER 在这个工作流里的位置：**它在 agent 旁边看着 agent 的每一次编辑，维护一份 agent 自己没有的"历史视角"：agent 改了什么、改了几次、改的地方之间有什么关系。当它发现 agent 可能卡住、打转、越界时，先把这个历史视角还给 agent，让 agent 自己纠正；如果 agent 纠正不过来，再提醒 Sarah 该介入了。**

关键点：BUER 不替 agent 写代码，不替 Sarah 做决定。它只做一件事：在该介入的时候，让该介入的人（先是 agent，后是用户）及时知道。

## 1.2 BUER 拦截的问题清单

BUER 监测的都是 **agent 的行为问题**，不是代码的对错问题。信号按「能否有效处理」分三层（默认 / 增强 / 砍降，理由见第五部分 §5.3）：

**默认信号**（自动派生 + 误报可控 + 独特覆盖，进 vibe coder 默认报警）：

| 问题 | 一句话场景 | 信号名 |
|---|---|---|
| debug 死循环 | agent 反复改同一函数想修 bug，没收敛 | stuck_region（§2.2） |
| 改了又改回去 | agent 改了一版，又绕回到和之前等价的状态 | define_loop（§2.1） |
| 复制粘贴重复代码 | agent 生成了和已有代码几乎一样的新代码 | find_duplicates（§2.5） |
| 改到项目外 | agent 编辑了项目根目录以外的文件 | boundary_breach（§2.4） |
| 调用不存在的函数 | agent 调用幻觉 API / 拼错名字，持续未定义 | 悬空引用（§2.8，限静态类型语言） |

**增强信号**（有前提时启用，前提缺失则退回对应默认信号 / 不报）：

| 问题 | 前提 | 信号名 |
|---|---|---|
| debug 死循环（确认版） | 项目有测试且能拿到结果 | debug_loop（§2.2，stuck_region 的测试增强） |
| 改坏了原来能用的功能 | 项目有测试 | regression（§2.6，测试绿转红 + 代码改导致） |
| 偷偷改测试掩盖问题 | 项目有测试 + agent 改了测试 | test tampering（§2.7，作弊类，早上报用户） |
| 改超出范围 | 用户/agent 声明了任务范围 | task_scope_breach（§2.3，opt-in） |

**砍 / 降级**（不能有效处理）：
- **behavior_loop（砍独立，保留为 define_loop 聚合提示）**：「跨文件兜圈」严格化后退化为「多个 define 各自 §2.4.4 等价回环」= 多个 define_loop 同时触发，无独立价值；宽松定义（仅跨文件操作模式）则误报高（正常分层开发就在多文件间来回）。两头不讨好，砍独立信号；多个 define 同时 loop 时作为 define_loop 的「多处打转」聚合提示展示。
- **thrashing（降为内部判据）**：「净位移小的反复折腾」与 stuck_region 重叠（都抓「同区域反复改」），独立报警会与 stuck_region 双重报警；且其独有的「净位移小」维度恰是误报源：正常的微调迭代（调样式/调参数本就要试很多次）在结构上与 thrashing 不可分。其 Jaccard 端点度量有用，降为 stuck_region / define_loop 的内部判据维度（判断「净进展」），不独立报警。

## 1.3 BUER 明确不做什么

BUER 看的是结构（文件、函数、它们之间的依赖关系）和测试结果，看不到代码的语义和业务意图。这决定了它的能力边界。诚实划清：

- **不给解决方案**。BUER 能告诉 agent「你在这个函数上卡了 8 次」，但不能告诉它「应该这么改」：它不懂 bug 的根因，不懂业务逻辑。给方案就是过度承诺。BUER 给的是「该看看这里了」和「相关的结构线索」，不是「这么修」。
- **不验证功能正确性**。BUER 所有信号全绿，只代表「没检测到 agent 打转 / 越界 / 测试持续失败」，**不代表代码是对的**。一个结构漂亮、agent 没打转的项目，逻辑可能完全错误。BUER 不是测试、不是 code review、不是正确性证明工具。
- **不判断项目方向**。「这个项目从做博客变成做 AI 聊天了，是不是跑偏了」：这是意图层判断，BUER 看不到。测试覆盖反映的是「测了什么」，不是「项目该往哪走」。方向偏移 BUER 架构上做不了，不留「未来会做」的空头承诺。

## 1.4 两步响应：先提醒 agent，再升级用户

这是 BUER 最核心的机制，所有信号都走它。

### 为什么先提醒 agent

agent 打转、debug 死循环的根本原因，是 **agent 意识不到自己在重复**。它每一步都觉得自己在合理推进，看不到「这已经是第 8 次了」：它缺的是跨越多次编辑的历史视角。

BUER 恰好有这个视角。所以第一步不是惊动用户，而是**把 agent 缺的历史事实还给它**。agent 收到「你在 validate_token 上已经改了 8 次，测试一直没过」之后，通常会触发自我评估、换个思路：这是 agent 的行为模式决定的，对多数情况有效。

只有 agent 收到提醒后**还在重复**（自我评估也没跳出来），才升级到第二步，提醒用户。这时用户收到的是真正需要人工介入的高价值信号，而不是被每个小波动打扰。

### 两步的流程

```
信号首次确认（达到首次阈值 θ₁）
   │
   ▼
第一步：通过 hook 把历史事实注入 agent
   │   内容是客观结构事实，不含解决方案
   │
   ├─ agent 自己纠正了（行为停止 / 测试转绿）
   │     → 标记解决，用户全程无感
   │
   └─ 提醒后还在重复（达到第二阈值 θ₂，比 θ₁ 小）
         │
         ▼
      第二步：提醒用户该介入了
         │   告诉用户 agent 卡在哪、已提醒过仍没跳出
         │   仍不给解决方案，只给「该看看了」+ 结构线索
         │
         └─ 仍持续 → 保持已升级状态，不重复轰炸用户
```

θ₂ 比 θ₁ 小，因为已经提醒过一次，容忍度应该降低。例：stuck_region 首次 5 次触发（提醒 agent），提醒后再 3 次未收敛就升级用户。

### 第一步怎么送达 agent

默认用 hook。以 Claude Code 为例：配置一个 PostToolUse hook（在 agent 每次编辑后触发），指向 BUER。BUER 检查后，如果有该提醒 agent 的信号，hook 把内容作为反馈注入 agent 的上下文，agent 下一步推理时就看到了。

没有 hook 的环境，退回到 agent 主动查询（每次编辑后调 BUER 的检查工具）。可靠性低一些，但仍能工作。

详见第四部分 §4.4（hook 集成）。

## 1.5 受众与定位

**目标用户**：vibe coder（靠描述需求让 agent 写代码的人）+ 较低质量的 agent。这个定位决定了几条设计原则：

- **BUER 自己算，不靠声明**。BUER 需要的信息（agent 改了什么、改了几次、文件关系）全部从它能观察到的信号自动推导：编辑事件、文件内容、git、测试结果文件。不要求 agent 或用户主动声明额外信息（范围控制的 scope 是唯一例外，且是 opt-in，见 §2.3）。理由：目标用户和较低质量 agent 不会可靠地主动声明。
- **默认输出对人友好**。给 vibe coder 的默认是「人话」：「agent 在这个函数上卡住了」，不是一堆指标和公式。严格的结构指标在 raw / audit 层备查，给高级用户和审计用。
- **及时，不全面**。BUER 不做项目的全面体检，只做「agent 行为该介入了」的及时信号。覆盖窄但每个信号都可操作。

## 1.6 辅助：用结构数据帮忙框定，不只报警

报警是事后（出事了告诉你）。但 BUER 的结构数据还能事中用：帮 agent 和用户**把该做的事框定好**，减少出事概率。同一份数据，事前防 + 事后报。

BUER 提供的辅助（都守三条边界）：
- **一键补测试**（§4.9）：用覆盖率 + 结构重要性，把模糊的「加测试」变成有清单、有优先级、有进度、有 loop 监测的任务
- **改动前预览影响域**（§3.8）：agent 改一个 define 前，告诉它会波及谁（基于横向关系数据）
- **提交时机辅助**（§4.10）：在结构稳定的好提交点提醒 vibe coder 提交，降低「久不提交、改崩回不去」的风险
- **安全网缺失预警**（§2.9）：项目没测试 / 没 git 时，提示系统性风险

**辅助的三条边界**（防越界）：
1. **建议非决定**：BUER 建议，用户确认，可拒绝。不替用户/agent 做决定。
2. **结构非语义**：辅助给结构事实（哪些 define 没覆盖、改动波及谁），不给语义方案（该怎么写测试、bug 怎么修）。
3. **可拒绝/可关闭**：任何辅助用户都能忽略或永久关闭。

**辅助的「宁缺毋滥」原则**：报警是异常时才出现（低频）；辅助是每个决策点都可能出现（高频），高频辅助会变噪音、被用户全部忽略。所以辅助**只在高价值时机出手**，且有**辅助仲裁**：健康提示（安全网）按自己的低频节奏，高频事中辅助（提交/影响域）同一时刻最多出一个（§4.11）。辅助宁可少出、不打扰，对应报警的「及时不全面」。

**哪些辅助压测后没做**（诚实记录）：「从自然语言任务框定 task_scope」（需理解语义关联 define，BUER 不懂，且错误范围建议会制造反向误报）；「写代码前提示已有相似」（判断相似需要内容，有内容时已非事前，与 find_duplicates 事后报警同一时刻）。这两个压测不过，不做。

---

# 第二部分　BUER 拦的每类问题

每节四段式：**场景**（真实发生什么）→ **BUER 怎么拦**（两步响应）→ **SDT 依据**（指针，细节在第五部分）→ **真实场景反思**（最不利情况下是否真回应问题、暴露什么前提依赖）。

阈值符号：θ₁ = 首次触发（提醒 agent），θ₂ = 升级（提醒用户），θ₂ < θ₁。

> **关于 thrashing**：早期版本有独立的「原地打转」信号（净位移小的反复折腾）。降级理由（订正）：它与 **stuck_region 重叠**（都抓「同区域反复改」，stuck_region 已覆盖「反复改未收敛」这个核心），而它独有的「净位移小」维度恰是**最高误报源**：正常的微调迭代（调样式/参数本就反复试）在结构上与 thrashing 不可分。所以独立报警 = 与 stuck_region 双重报警 + 独有部分高误报。其端点 Jaccard 度量有用，已降为 stuck_region（§2.2）与 define_loop（§2.1）的**内部判据维度**（度量「净进展」），不独立报警。（注意重叠对象是 stuck_region 而非 define_loop：thrashing 的「净位移小」与 define_loop 的「撞回等价点」是不同现象。）

## 2.1 改了又改回去（define_loop）

**场景**：Claude 改 `parse_date` 函数，把它从用正则改成用库；跑下来有问题，又改回正则；再遇到另一个问题，又想改回库……第三版和第一版几乎一模一样。它在两个等价的方案间来回，没意识到自己绕回了改过的状态。

**BUER 怎么拦**：
- 检测：同一函数的版本链上，后面某个版本和前面某个非相邻版本**结构等价**（绕回了改过的状态）。θ₁ 触发后提醒 Claude：「parse_date 第 3 版和第 1 版结构等价，你可能绕回了之前改过的方案。」
- 自纠 / 升级：同两步。

**SDT 依据**：「结构等价」用 [SDT §2.4.4] 的 structural layer 等价：两个版本对应的结构层 $L$ 模式相同、具体对象不同（满足 §2.4.4 三判据）。BUER 用 node_fingerprint 作工程启发式哈希，方向与 §2.4.4 同向、主要粗覆盖判据 (b)（P-配置/属性模式），判据 (a) E-结构同构、(a') determination type、(c) C-过滤等价均未覆盖（第五部分 §5.3 详列降级）。判断「绕回」还是「朝一个方向收敛」时，用端点 Jaccard 距离 $d_J$ 作内部判据（[Math Ext §12.1.2]，原 thrashing 度量降级至此）。

**真实场景反思**：define_loop 抓「绕回旧状态」比 thrashing 严格（不是「接近」，是「结构等价」）。压测出两类需缓解的情况：(1) **有意退回**：agent 试了 B 发现不如 A，主动退回接近 A 的形式，这是合理决策不是 loop。所以「绕回 = 问题」的假设要松动：措辞用疑问「你回到了和第 N 版等价的结构，是有意的吗」，且要求回环跨越足够编辑次数（短距离往返不报），避免把正常的探索退回当 loop。(2) **格式化往返**：改格式又改回，指纹可能判等价，但无害；缓解同上。另一面，最不利情况：**两个方案都不行，agent 来回是因为没有第三条路**：这时 define_loop 受益于横向关系（第三部分）：提示「这两个方案的共同上游 X 可能才是问题」比单纯说「你绕回去了」有用。行为信号 + 结构线索才完整。

## 2.2 debug 死循环（stuck_region 主力，debug_loop 增强）

这是 vibe coding 最高频、最烧钱的问题，重点写。

**场景**：Sarah 让 Claude 修「登录后 token 偶尔失效」。Claude 看 `validate_token`，改过期判断逻辑，没好；改时区处理，没好；加日志、改缓存、调顺序……每次改的都不一样，bug 始终在。它没意识到自己已经在这个函数上耗了很久还没进展。

**BUER 怎么拦**：

两个信号，分主力和增强：

**stuck_region（主力，纯结构，不需要测试）**：
- 检测：同一区域（函数 / 文件）被反复改（达 θ₁=5 次），每次改动幅度都不小（不是 thrashing 的小振荡，是持续实质改动），且没有绕回旧状态（不是 define_loop），改动还在继续。
- 这是「agent 在这块持续折腾但没收敛」的纯结构信号，**不需要任何测试数据**。
- 第一步提醒 Claude：「validate_token 你已经实质改了 5 次还在改，可能卡住了。」（本节末尾会说这条提醒应附带什么结构线索）

**debug_loop（增强，有测试时）**：
- 如果项目有测试且能拿到结果（第四部分 §4.5 JUnit XML），stuck_region 升级为 debug_loop：「validate_token 改了 5 次，而 test_token_expiry 这 5 次一直没过。」
- 测试转绿 = **客观的解决判据**（不是推断），BUER 自动标记解决，用户无感。这是 debug_loop 比其他信号强的地方：有客观闭环。

**SDT 依据**：「同区域反复改」= 同一 define 的版本链长（[GD] chain）；「幅度大」= 相邻版本 $d_J$ 持续大（[Math Ext §12.1]）；「没绕回」= 无 §2.4.4 等价回环。测试关联分两档（§4.5）：有覆盖率数据时用覆盖率精确映射（测试实际覆盖哪些 define），无则退 testcase 路径/名称启发（第五部分 §5.3 标降级）。

**真实场景反思**（这节最关键的压测）：

压测暴露三件事，每件都改进了设计：

1. **真实 bug 的根因常常不在 agent 改的地方**。token 偶尔失效，根因可能在 `create_token`（生成时用错时钟源），而 `validate_token` 完全正确。Claude 死磕 validate_token 因为现象在那里。BUER 提醒「你在 validate_token 卡了 5 次」：Claude 收到后还是盯着 validate_token，因为提醒没告诉它「往上游 create_token 看」。**结论：debug_loop 的提醒必须附带根因方向**（「validate_token 依赖 create_token，后者最近也改过」）。这就是为什么 debug_loop 和第三部分横向关系**绑定**：单独的 debug_loop 只是告诉 agent 它已经知道的事（我卡住了），加上横向关系才告诉它有用的新信息（往哪看）。

2. **真实 vibe coder 常常没有测试**。Sarah 的 bug 是手动点出来的，项目里可能根本没有 test_token_expiry。没测试，debug_loop 退回 stuck_region。**这就是为什么 stuck_region 是主力、debug_loop 是增强**：现实里测试齐全是少数。文档不把 debug_loop 当主角。

3. **第一步提醒是否触发 agent 自纠，依赖 agent 质量**。Claude 这类有自我评估能力的 agent，收到提醒会换思路；但 BUER 的目标受众也包括较低质量 agent，它可能道歉一句然后用同样思路改第 6 次。**对低质量 agent，第一步会快速空转到第二步升级用户**：这不是 bug，正是 θ₂ 机制的价值（自纠失败就及时升级）。诚实说：第一步有效性随 agent 质量变化，BUER 不假设它总有效。

## （已砍独立，保留为聚合提示）跨文件兜圈子（behavior_loop）

> behavior_loop 在 v2.0 砍除**独立信号**。理由（订正）：严格化后，「多个 define 各自出现 §2.4.4 等价回环且交替」= 多个 define_loop 同时触发，无独立价值（define_loop 已覆盖）；宽松化：「仅跨文件操作模式重复」误报高（正常分层开发本就在 service/controller/model 间来回）且根基弱。两头不讨好（它并非无 SDT 根基：可建在多 define 的 §2.4.4 聚合上，但严格化后即退化为 define_loop 聚合）。
>
> **保留为 define_loop 聚合提示**：当多个 define 在相近时间窗各自触发 define_loop，BUER 提示「你在多处（A/B/C）反复打转」：这是 define_loop 的聚合展示，不是独立信号，复用 define_loop 的判据和两步响应。

## 2.3 改超出范围（task_scope_breach）

**场景**：Sarah 说「帮我改一下 user 模块的注册逻辑」。Claude 改完注册，顺手觉得 admin 模块也有类似问题，就把 admin 也改了。Sarah 没让它动 admin，现在多了一堆她没预期的改动。

**BUER 怎么拦**：
- 前提：Sarah 或 Claude 通过 `set_task_scope` 声明了这个任务的范围（allowed: `user/**`）。这是 BUER 唯一的 opt-in 信息（§1.5 例外）。
- 检测：Claude 编辑的文件落在范围外（改了 `admin/`）。范围外是**单次即可判定**的（不像 loop 要累积），所以 θ₁=1：第一次越界就提醒 Claude：「你改了 admin/role.py，超出了本次任务范围 user/**。」
- 自纠：Claude 退回范围内（或不再改范围外）→ 解决。升级：再越界 → 提醒 Sarah，她决定是扩范围还是让 Claude 退回。

**SDT 依据**：范围检查用 [GD] 的 $\rho(D)$（判定产物）+ $E(D)$（元素）+ R-member 概念表达：检查 agent 判定涉及的 R-member 是否落在用户给定的子集内。**注意**：范围约束本身是 entity-theoretic 外加的工程约束，不是 SDT 定理；SDT 提供概念词汇（$\rho$、R-member），「agent 应限定范围」是工程要求（第五部分 §5.3 标这个区分，并说明这与 use-exclusivity 无关）。

**真实场景反思**：范围控制是真实痛点（agent 越界是 vibe coding 常见困扰），但前提是**有人声明了范围**。Sarah 作为 vibe coder，很可能不会主动调 set_task_scope。所以：没声明范围时，BUER 退回到「项目根边界」检查（§2.4），只在 agent 改到项目外才报。诚实说：task_scope 的价值依赖声明，不声明就退化：它是给「愿意花一句话框定范围」的用户的增强，不是默认保护。范围怎么设、并发任务怎么管，见第四部分 §4.3。

## 2.4 改到项目外（boundary_breach）

**场景**：Claude 在改项目时，编辑了项目根目录以外的文件：比如改了系统的全局配置、或另一个项目的文件。Sarah 完全没预期 agent 会动项目以外的东西。

**BUER 怎么拦**：
- 检测：编辑的文件路径不在项目根目录下。单次判定，θ₁=1：立即提醒 Claude：「你编辑了项目目录以外的文件 ~/.config/x，这超出了项目范围。」
- 升级阈值小（θ₂=1，改到项目外比任务越界更严重）：再犯就提醒用户。

**SDT 依据**：同 §2.3，基于 R-member 是否落在项目根定义的集合内。boundary 是项目级的默认边界，不需要用户声明（区别于 task_scope 的 opt-in）。

**真实场景反思**：boundary_breach 是默认开启的兜底保护，不需要任何声明。压测推翻了早先「最干净、几乎无虚处」的断言（那是没压就拍的）：真误报源有 **monorepo / 多项目工作区**（agent 合法跨子项目编辑，若「项目根」设成单个子项目则全报）、**符号链接 / 项目外的合法配置**（改 ~/.config 等）、**构建产物写到项目外**。修正：支持配置**多个项目根**（monorepo 把各子项目根都纳入），符号链接解析到真实路径再判，构建产物目录可加白名单。单项目、清晰根目录时它确实干净、误报低；多根 / 符号链接场景需配置，否则误报。

## 2.5 复制粘贴重复代码（find_duplicates）

**场景**：Claude 要加一个「导出 PDF」功能，它没有复用已有的「导出 Excel」逻辑，而是几乎原样复制了一份再改几行。项目里现在有两块 90% 一样的导出代码。以后改一处忘了改另一处，就是 bug 的温床。

**BUER 怎么拦**：

这个信号和前面不同：它不是「agent 行为打转」，是「结构上出现了重复」，所以响应方式略有调整：
- 检测：新增的 define 和已有 define **结构等价或高度相似**（node_fingerprint 相同或差异极小）。
- 第一步提醒 Claude：「export_pdf 和已有的 export_excel 结构 90% 相同，考虑复用而不是复制。」
- 这里第一步特别有价值：agent 复制粘贴时往往不知道（或忘了）已有相似代码，BUER 正好补上「项目里已经有类似的了」这个它缺的全局视角。
- 升级：如果 agent 确认要保留重复（有时是合理的），用户可标记接受；否则重复持续累积可升级提醒用户。

**SDT 依据**：「结构等价」= [SDT §2.4.4] 等价类，完全重复对应 $\epsilon_R = 0$（零容差等价）；高度相似对应小 $\epsilon_R$。node_fingerprint 是工程启发式哈希，借 §2.4.4 等价类概念命名，主要粗覆盖判据 (b)，判据 (a)/(a')/(c) 未覆盖（第五部分 §5.3）。

**真实场景反思**：find_duplicates 是真实高频痛点（agent 爱复制粘贴），且 BUER 的全局结构视角正好是 agent 缺的。压测暴露一类**真误报**：测试代码（arrange-act-assert 天然相似）、样板代码（DTO/config/route）、生成代码（protobuf/ORM）天然高度重复，全报会被噪音淹没。修正：默认排除测试目录、生成代码目录、已知样板模式，只报「非样板的逻辑重复」。另一个边界：**有些重复是合理的**（有意解耦）。所以 BUER 只说「这里有重复，你可能没注意到」，合不合并是用户/agent 的决定。诚实守住：报事实（有重复），不做判断（该不该合）。

## 2.6 改坏了原来能用的功能（regression）

**场景**：Sarah 让 Claude 加一个新功能。Claude 加完，新功能能用：但它顺手改动的地方把**原来好好的登录功能弄坏了**。Sarah 没注意，等发现时已经过了好几轮。「刚才还能用的，怎么改完别的就坏了」是 vibe coding 最让人崩溃的高频问题之一。

**BUER 怎么拦**：
- 检测：某 testcase 之前 **passed**，agent 改动后变 **failed**（测试绿转红），且转红是因为**被测代码改了**（不是测试本身改了，见 §2.7 区分）。
- 第一步提醒 Claude：「test_login 之前通过，你这次改动后失败了：可能改坏了登录。是预期的吗？」（疑问句，不断言「你改错了」，因为可能是需求变更。）
- 自纠：Claude 发现确实改坏 → 修回。升级：持续红 → 提醒 Sarah。

**SDT 依据**：纯版本链 + test_runs（已接入，§4.5）。测试状态从 passed→failed 是该 testcase 关联 define 的判定历史上的客观状态变化。零边际成本：测试已接入，只是加一个「绿转红」的反向检查（debug_loop 是「持续红」，regression 是「绿转红」，方向相反）。

**真实场景反思**（用正确判据：前提满足时解决什么痛点）：
- **前提**：项目有测试。前提满足时，regression 抓的「改坏原功能」是 vibe coder 最痛的问题之一，且判据客观（绿转红是事实，不是推断）。**前提满足 → 解决高痛点 → 好功能。**
- **前提不满足**（无测试）：不报。这是诚实边界，不是缺陷：不能因为「没测试时不工作」否定「有测试时解决高痛点」。
- **误报源**（需求变更导致的合法绿转红）：用疑问句措辞缓解（「是预期的吗」），误报代价降到「问一句」。
- **可被规避**：agent 把红测试直接改绿掩盖 → 这正是 §2.7 test tampering 检测的。

## 2.7 偷偷改测试掩盖问题（test tampering）

**场景**：Claude 改坏了登录，test_login 变红。低质量 agent 的常见操作：不去修登录，而是**把 test_login 改成通过**（改断言、加 skip、删测试）。测试不红了，看起来「修好了」：但登录其实还是坏的。这是 agent 在**作弊**。

**BUER 怎么拦**：
- 检测：某 testcase 从 failed→passed（转绿），但 BUER 版本链显示**转绿那次是测试 define 自己被改了**，而不是被测代码改了。这是 tampering 嫌疑。
- 对比：test_login 转绿是因为 login 代码改了 → 真修复；test_login 转绿是因为 test_login 自己被改了 → 作弊嫌疑。版本链客观区分这两者。
- **上报方式特殊（你问的「agent 提醒还是上报用户」）**：作弊类信号**直接偏向上报用户**，不走标准「先提醒 agent」。因为提醒一个正在作弊（或低质量地改测试）的 agent「别改测试」基本无效：要让 Sarah 知道。θ₁ 就提示用户：「Claude 让 test_login 通过了，但它改的是测试本身，不是登录代码：登录可能还是坏的。」

**SDT 依据**：纯版本链（determinations 表，已有）。「转绿那次改的是测试 define 还是被测 define」是判定历史上的客观事实。零边际成本。

**真实场景反思**：
- **前提**：有测试 + agent 改了测试让它转绿。前提满足时，这抓的是 agent 作弊/掩盖：比 regression 更隐蔽、更该让用户知道。版本链客观检测，不靠猜。
- **为什么不先提醒 agent**：这是少数该打破两步响应「先 agent」默认的信号。自纠类（打转/越界）提醒 agent 有效；作弊类提醒作弊者无效，早上报用户。
- **误报源**：agent 合法地修正一个本就错的测试（测试之前写错了，改对它导致转绿）。这是合法的。缓解：措辞「改的是测试不是被测代码，确认测试改对了吗」（疑问），且只在「被测代码这次没动、只动了测试」时报（如果代码和测试一起改，不报）。
- **前提不满足**（无测试 / agent 没碰测试）：不报，诚实边界。

## 2.8 调用了不存在的函数（悬空引用）

**场景**：Claude 写代码调用 `pandas.read_excell`（拼错，应是 read_excel）或一个它**幻觉出来的、根本不存在的函数**。vibe coding 高频：agent 经常调用它以为存在但其实没有的东西。

**BUER 怎么拦**：
- 检测：agent 调用的 callee 既不在项目 define 全集、也不在已知库，且**跨多次编辑后仍未定义**（排除「自顶向下、马上要写」的正常中间态）。
- 第一步提醒 Claude：「你调用的 process_excel 一直没定义，也不在已知库里：是漏写了还是拼错了？」（疑问，agent 自查有效。）
- 标准两步（agent 拼错/忘写，提醒它自查通常能纠正）。

**SDT 依据**：调用图 callee 解析（§4.2a 的副产品）+ define 全集（$\mathcal{G}_D$ 节点集）。callee 解析不到任何 R-member（define 或库）且持续 = 引用了不存在的结构。零边际成本：build_call_edges 已经在解析 callee，解析不到的本来就要跳过，这里把「持续解析不到」捞出来作信号。

**真实场景反思**（正确判据）：
- **前提**：静态类型语言 + 持续未定义。前提满足时，抓「调用幻觉 API / 拼错」是真痛点，可靠检测。**前提满足 → 解决痛点 → 好功能。**
- **「持续未定义」是误报控制（留）**：单次解析不到不报：agent 常自顶向下，先写调用再写定义。要求跨多次编辑仍未定义，排除「马上要写」。这是误报控制，不是前提收窄。
- **「静态类型语言」是诚实边界（标）**：动态语言（Python/JS）大量合法动态引用（getattr/反射/注入）静态解析不到，全报会淹没。动态语言不报，是边界，不是缺陷。
- 不能因为「动态语言不可靠」否定「静态类型语言 + 持续未定义时解决痛点」。

**报警增强**：报「Y 没定义」时，附名字相似的已有 define：「Y 一直没定义，项目里有名字相似的 X，你是不是想调它？」（名字相似度是结构，不需语义）。这是悬空引用报警的增强，不是独立的事前辅助（事前辅助需要知道 agent 想调什么 = 语义，压测不过）。

## 2.9 项目没有安全网（测试 / git 缺失预警）

这是**项目健康预警**，不是 agent 行为信号：给用户，不走两步响应。

**场景**：Sarah 让 agent 持续开发了几小时，项目已经有几十个函数。但项目**没有测试**（agent 改坏了什么没人知道）、或**没有 git / 很久没提交**（改崩了回不去任何好状态）。Sarah 不知道自己一直在没有安全网的情况下让 agent 大改。这是 vibe coding 最大的系统性风险：不是某次改动的问题，是整个项目裸奔。

**BUER 怎么提示**：

测试和 git 是 vibe coder 的两张安全网：测试是「改坏了能发现」，git 是「改坏了能退回」。BUER 检测缺失：

```
⚠ 这个项目缺少安全网

· 没有测试：agent 改了 23 个函数，但没有测试在验证它们还能正常工作。
  改坏了原来能用的功能时，没人会发现，直到出问题。
· 很久没提交 git：距上次提交已改了 18 处。如果接下来 agent 改崩了，
  你只能退回到 18 处改动以前，中间的好状态都丢了。

这是系统性风险：不是某次改动的问题，是缺少兜底。
建议：补一些测试（我可以帮你组织，见「一键补测试」）；养成改一段就 git commit 的习惯。
```

**SDT 依据 / 数据来源**：全是 BUER 已有数据：测试有无（§4.5 检测）、git 有无（`.git` 检测）、define 数（$\mathcal{G}_D$ 节点）、距上次提交的改动量（编辑历史 + git log）。零边际成本。

**真实场景反思**（压测后的收窄）：
- **前提满足时解决最高痛点**：有规模的持续项目 + 真没安全网 → 提示系统性风险，是 vibe coder 最该听到的。判据客观（没测试/没 git 是事实，不是判断）。
- **真误报源 1：小原型/一次性脚本**不需要测试 git。收窄：只在「项目有一定规模（define/文件数过阈值）且持续开发（多 session）」才提示，不对小脚本弹。
- **真误报源 2：git 检测假阴性**：用了 hg/svn 或 git 在父目录。收窄：向上查父目录 .git、识别其他 VCS。
- **可永久关闭**：提示一次为主，用户「我知道，别再提」后不反复（小原型/有意不用测试的用户）。
- **元价值**：这个预警诚实暴露了 BUER 自己的盲区来源：BUER 在无测试项目上覆盖薄（debug_loop/regression/tampering 都依赖测试），预警等于主动说「你没测试，所以我只能给基础保护，改坏功能这类我看不到」。比默默退回诚实。

**上报方式**：给用户（不是 agent）。「要不要测试/git」是用户对项目的决策，不是 agent 能自己定的。出现在 onboarding（§4.8）和阶段性健康检查，带「一键补测试」入口（§4.9）。

---
# 第三部分　节点横向关系（debug 加速）

第二部分的压测反复指向一件事：debug_loop / define_loop 单独用，只告诉 agent 它已知道的（我卡住了）。真正帮到用户的是它不知道的：往哪看。这就是节点横向关系。

核心定位（守 §1.3）：**只给影响域 + 根因方向，不给解决方案**。BUER 说「A 和 B 共享历史、B 最近改过，值得看」，不说「把 B 改成 X」。

横向关系全部基于纯结构运算，**不理解代码语义**：这是刻意的。你最初指出的痛点是「agent 有相关性判断但常错漏」。BUER 的价值正在于给**算出来的结构事实**（共享祖先、共享汇合、调用图），而不是再做一次 agent 已经在做的语义猜测。一旦 BUER 去理解语义，就和 agent 做同一件易错的事，丢了「结构事实 vs 语义猜测」的分工。所以横向关系是结构的、确定的、不猜的。

## 3.1 场景：bug 在 A，根因在 B

回到 §2.2 的 token 失效。Claude 死磕 `validate_token`，因为现象在那里。但真实根因在 `create_token`：它生成 token 时用错时钟源，token 一出生就带病。

Claude 自己判断「哪些代码相关」靠读代码的理解，会漏、会错。BUER 有 agent 没有的东西：**判定历史**：这两个函数是不是源于同一次重构、它们的产物是不是都流向同一个下游。这是 agent 看不到的（它只看当前代码，看不到编辑历史的结构）。

## 3.2 横向关系的 SDT 基础：反链上的关系运算

SDT 对「横向」节点间关系有一整套严格运算。先说清「横向」在 SDT 里是什么。

**反链（antichain）= 横向节点**。在判定历史 $\mathcal{G}_D$ 里，互不为祖先（≺-incomparable）的节点构成反链：它们是「并发的」「同时的」。[Math Ext §11.1a] 把极大反链称为 SDT 的「空间切片」。横向关系就是反链上节点之间的关系。

反链上有两个方向的严格关系运算：

**上游方向：前驱重叠 $\omega$**（[Math Ext §11.1b]）：

$$\omega(u,v) = |\downarrow u \cap \downarrow v|$$

两个横向节点共享多少祖先。大 = 历史高度重叠（源于共同的过去判定），拓扑邻近；零 = 历史完全独立。**纯结构，SDT-internal，不依赖任何语义。**

**下游方向：$R$-关联 $\Gamma_R$**（[GD §7.1]）：两节点的产物都被某个共同汇合节点消耗（都是某 $w$ 的前驱），则二者 $\Gamma_R$-关联。且 [GD Prop 7.2] 关联可传递。

**因果独立：连通分量**（[GD §9.2]）：不在同一连通分量的节点结构因果独立（无共享历史、无共享下游）。

**一个关键的非平凡性质**（[Math Ext Thm 11.1f.2]）：$\omega$ 邻近**不可传递**：A 与 B 共享祖先、B 与 C 共享祖先，不推出 A 与 C 共享祖先。而 $\Gamma_R$ 关联**可传递**（[GD Prop 7.2]）。工程含义见 §3.3：基于共享祖先（上游）的影响域不能链式扩散，基于共享汇合（下游）的可以。

**不做的：联合约束能力 $\sigma$**（[Math Ext §11.1d]）。SDT 有此运算（两节点对未来层的联合约束），但它依赖 $G$（配置兼容性 $G_{\text{co}}$）：落到代码就是理解调用的数据流/契约语义。BUER 刻意不理解语义（见本部分开头），故 $\sigma$ 不实现。第五部分 §5.3 标「SDT 有此运算，BUER 因依赖 $G$ 且语义理解性价比不足而不做」。

## 3.3 这套运算作用在哪：判定历史 $\mathcal{G}_D$

$\omega$ / $\Gamma_R$ / 连通分量全部依赖 $\downarrow v$（祖先锥），祖先锥取决于 $\mathcal{G}_D$ 的边。所以横向关系的前提是 BUER 把编辑历史构建成 $\mathcal{G}_D$。

**BUER $\mathcal{G}_D$ 的节点**：每次编辑 define = 一个判定节点（determinations 表，§4.2）。

**粒度锁定在 define（函数/方法/类）级，不是文件级**。这是横向关系有没有用的前提：
- **文件级无效**：「auth.py 依赖 token.py」对 debug 几乎没用：一个文件几十个函数，指到文件等于没指方向，Claude 还是不知道看哪个函数。
- **define 级有效**：「validate_token 依赖 create_token」直接指到该看的函数。§2.2 的 token 例子全靠这个粒度才成立。
- **语句级（函数内 def-use）不做**：「第 12 行用了 .expiry 字段」更精确，且函数内 def-use 是过程内分析、不难实现。但不做的真实理由是：(1) agent 拿到 define 级定位（「看 validate_token」）已能自行读函数内部找到那几行，语句级多给的精度对 agent 帮助有限；(2) 语句级会打破 BUER 的 define 级节点架构（ω/Γ_R/node_fingerprint 全在 define 级），引入语句级要么是 define 内子图（另一层结构）要么污染粒度统一。易实现 ≠ 该做：价值有限 + 架构代价大，不做。

所以 define 级是「够指到 debug 该看哪个函数」且「过程间数据流可达」的粒度。`extract_defines` 抽到函数/方法/类，node_fingerprint、$\mathcal{G}_D$ 节点、ω/Γ_R 全部在 define 级。**import 边（文件/模块级）只作辅助，不作为主要跨 define 边**：主要跨 define 边必须是 define→define（数据流档）或函数级 caller→callee（调用图档），否则粒度塌回文件级、横向关系失效。

**边的两类构建**（[GD G3] $u \to v$ iff $\rho(u) \cap E_v \neq \emptyset$ 在编辑场景的落地）：

1. **版本链边（严格，无损）**：编辑 v 修改 define A，消耗 A 的上一版本（编辑 u 的产物）。同一 define 的连续编辑成链。版本前后关系确定，无损。

2. **跨 define 边（按语言分两档）**：编辑 v 写 define B 时消耗了 define A 的当前版本，$u \to v$。「消耗」的识别分两档（§4.2a）：
   - **数据流档（TS，按需）**：过程间数据流分析，追 A 的产物是否真流进 B 并被使用。**精确命中 GD G3** $\rho(u) \cap E_v \neq \emptyset$（A 的产物进入 B 的元素集）。输入严格。**但此档仅 `analyze_dangling` 按需调用，默认构图不走**；且需 TS 工具链（tsconfig + node_modules/typescript + node）齐全时生效，缺失则 degrade 回调用图。
   - **调用图档（默认全语言）**：默认构图全语言（含 TS）走调用图近似。调用 ≠ 真消耗（有假边），输入有损。

**关键定性（输入层：默认 + 按需）**：

> **运算层严格**：$\omega$ / $\Gamma_R$ / 连通分量是 [Math Ext §11] / [GD §7,§9] 定理，不打折。
> **输入层（默认）**：版本链边无损；跨 define 边**默认全语言走调用图近似**（调用 ≠ 消耗，有假边）。数据流（对位 GD G3）是按需窄场景能力，非默认主体。

「输入有损」是默认全语言现状（调用图近似）。数据流（对位 GD G3）仅 `analyze_dangling` 按需调用 + TS 工具链齐全时生效；缺失则 degrade 回调用图。**故 G3 严格是按需窄场景能力，不是默认主体。**

**最终输出可信度的诚实说明**：运算层严格不自动等于输出可信。最终线索可信度由输入层决定：
- **默认（全语言，调用图档）**：输入有损（调用 ≠ 依赖 + 动态调用漏）→ 输出可信度受输入上限限制，运算严格不突破它。
- **按需（TS + 工具链齐全，数据流档）**：输入严格（GD G3）+ 运算严格 → 输出可信度高，仅剩动态调用（反射/回调）漏边的残余局限。代价：实测 ~4s/调用（较调用图约 450 倍）。

调用图是默认主体（全语言）；数据流是按需窄场景增强（`analyze_dangling`，TS 工具链齐全时）。两种路径下调用图都可单独查（agent 看「谁调用 A」）。


## 3.4 影响域（blast radius）

agent 在 A 卡住或要改 A 时，BUER 给出 A 的结构影响域：

- **共享祖先**（$\omega$ 大的节点）：和 A 源于共同历史的 define。$\omega$ 不可传递，所以只取与 A 直接 $\omega$ 大的，**不链式扩散**。
- **共享下游**（$\Gamma_R$ 关联）：产物和 A 流向同一汇合的 define。$\Gamma_R$ 可传递，可沿关联链扩展（受深度限制）。
- **调用关系**：A 调用谁（callee，行为依赖）、谁调用 A（caller，改动波及）。
- **同类**（等价类，§2.4.4）：和 A 结构等价的 define。

这是结构事实（$\mathcal{G}_D$ 上算出），比 agent 凭理解猜的全。但受 $\mathcal{G}_D$ 边的输入局限（动态调用漏，见 §3.7）。

**$\omega$ 与 $d_J$ 的关系（相关但不同）**：$\omega$（[Math Ext §11.1b]）用**严格前驱锥** $\downarrow$（不含自身）：$\omega(u,v)=|\downarrow u\cap\downarrow v|$。$d_J$（[Math Ext §12.1.2]）用**扩展祖先集** $\text{anc}(v)=\downarrow v\cup\{v\}$（含自身）：$d_J(u,v)=|\text{anc}(u)\triangle\text{anc}(v)|/|\text{anc}(u)\cup\text{anc}(v)|$。$d_J$ 的重叠项 $\omega^*=|\text{anc}(u)\cap\text{anc}(v)|$ 基于 anc，**不等于** §11.1b 的 $\omega$。两者来自同一 $\mathcal{G}_D$，集合基础不同，不直接等价。版本链闭式：$u\prec v$ 时 $d_J(u,v)=1-|\text{anc}(u)|/|\text{anc}(v)|$；纯链示例 $d_J(v_1,v_2)=1/2$，$d_J(v_1,v_3)=2/3$（[Math Ext Ex 12.1.6]）。stuck_region/define_loop 用 $d_J$ 度量同一 define 版本链的**纵向**接近度（相邻版本漂移幅度），横向关系用 $\omega$ 度量不同 define 编辑的**横向**共享历史。

## 3.5 根因方向提示

debug_loop / stuck_region 第一步提醒附带根因方向，由三个结构事实叠加：

```
[BUER] validate_token 已改 5 次未收敛（debug_loop）
       测试 test_token_expiry 持续失败

       结构线索（供 debug 参考，非结论）：
       · 共享祖先：validate_token 与 create_token 源于同一次 token 重构
         （ω 高：两者编辑历史共享 7 个祖先判定）
       · 时序：create_token 在你 debug 前 2 次 sync 刚改过
       · 调用：validate_token 调用 create_token
       → 优先检查 create_token，根因可能不在 validate_token 本身
```

根因方向 = 共享祖先（$\omega$）+ 时序（最近改过）+ 调用（callee）的交集。$\omega$ 共享祖先是严格维度，揭示 agent 完全看不到的编辑历史关联。

措辞守定位：「可能」「优先检查」「非结论」：给方向不给改法。

**能否有效处理**：自动派生（$\omega$/时序/调用都自动算）、误报可控（措辞为方向，错了代价是多看一处）、独特覆盖（$\omega$ 共享祖先是 agent 看不到的）。**留**。诚实标：根因方向是启发组合（共享祖先/最近改过不保证是根因），且 $\mathcal{G}_D$ 边有输入局限。

## 3.6 同类传播 + 接入两步响应

**同类传播**（$\omega$ 之外，用等价类）：A 触发 debug_loop，BUER 提示 A 的等价类成员（validate_session / validate_refresh）：「结构同类，若结构性 bug 可能波及」。措辞「可能」（同类不一定同 bug）。

**接入两步响应**：横向关系不是独立信号，是 debug_loop / stuck_region / define_loop 第一步提醒的**内容增强**（§3.5 的提醒块）。agent 收到的不是「你卡住了」（已知），而是「你卡住了 + 这些结构方向值得看」（未知）。§2.2 压测暴露的「debug_loop 单独无用」由此解决。升级用户时，用户也收到同样的影响域 + 根因方向。

## 3.7 承压反思（第三部分整体）

1. **真正回应了 §2.2 的问题**：加上共享祖先 + 根因方向，debug_loop 从「复述已知」变成「给未知方向」。这是横向关系存在的根本理由。

2. **守住不给方案**：全程「方向」「可能」「优先检查」，无一处「应该这么改」。

3. **SDT 严格性的准确定性**：横向关系运算严格（$\omega$/$\Gamma_R$/连通分量是 SDT 定理）。输入层**默认全语言走调用图近似**（有损）。数据流（对位 GD G3）是按需能力（`analyze_dangling` + TS 工具链），非默认主体。

4. **输入局限（默认 + 按需分层）**：
   - **默认（全语言，调用图档）**：三重局限：动态调用漏、调用 ≠ 数据流（假边）、非调用路径依赖看不到。TS 亦如此——默认 `build_gd_edges` 走调用图（实测 got 项目：gd_edges 168 条全 callgraph，0 条 dataflow）。
   - **按需（TS + analyze_dangling + 工具链齐全，数据流档）**：跨 define 边严格（真实数据依赖），仅剩动态调用漏边残余局限。消除「调用 ≠ 依赖」假边。代价：实测 ~4s/调用（~450 倍）；缺工具链则 degrade 回调用图。
   - **两者共有**：非调用/非数据流路径（全局状态/数据库/消息队列）$\mathcal{G}_D$ 边里都没有。

5. **数据流的成本与收益（诚实核算）**：实测 ~4s/调用（每次重载 TS program）；全量 got 256 define ≈ 17 分钟 vs 调用图 2.2 秒（~450 倍代价）。默认走调用图是合理性能权衡，不是 bug。数据流作为按需工具：用户深挖 bug 根因时，4s 换精度（消除假边 + GD G3 严格）是合理交换。这是基于性价比的设计，不是「成本不存在」。

6. **不做语义（$\sigma$）的代价**：放弃 $\sigma$ 的「联合约束未来」精度。但 $\sigma$ 依赖 $G$（语义理解），与数据流不同（数据流是结构分析，追值流动不懂值含义）。不做 $\sigma$ 是守「不理解语义」定位；做数据流是结构分析，不破这条边界。

7. **最不利情况**：根因在 $\mathcal{G}_D$ 边之外（外部 API、数据库隐式传播）。这时横向关系给不出方向，退回「你卡住了」基本提醒。横向关系提高命中概率，不覆盖结构图外的根因。

8. **最尖锐的承压：define 级覆盖率可能不够**。横向关系的有效性 = define 级依赖分析的覆盖率。而 vibe coder 主力是动态语言（Python/JS），其真实项目常用动态分发、装饰器、回调、依赖注入：这些 define 级静态分析（调用图档）大量漏边。极端情况：一个重度动态的 Python 项目，define 级调用图可能漏掉一大半边，横向关系即使粒度名义是 define 级，**实际覆盖率低到接近无用**。这不是「漏几条边」的小局限，是「对部分目标用户的部分项目，横向关系可能整体失效」的大局限。诚实定位：横向关系对静态类型语言项目（数据流档，高覆盖）价值高，对重度动态的动态语言项目（调用图档，低覆盖）价值可能有限。它是「覆盖率够时的 debug 加速」，覆盖率是前提不是保证。

**承压结论**：横向关系**运算严格；输入默认有损**：默认全语言走调用图近似（有损）。数据流（GD G3 严格）是按需窄场景能力（`analyze_dangling` + TS 工具链），**不是「把 TS 主体从近似扶正到 GD G3 严格的默认路径」**。$\omega$/$\Gamma_R$/连通分量是严格 SDT。数据流是「用户深挖根因时按需启用的精度增强」。对「真实数据依赖 + 最近改过」的根因有效（按需精度高），对隐式传播（全局状态/数据库/外部）的根因无效。价值真实、边界明确，不是万能 debug 定位器。

## 3.8 辅助：改动前预览影响域

横向关系数据除了事后给 debug 根因方向（报警面），还能事前用（辅助面）：agent 要改一个 define 前，BUER 告诉它会波及谁。

**场景**：Claude 要改 `validate_token`。改之前，BUER：「validate_token 被 5 处调用（auth_middleware / login_flow / ...），改它的行为会影响这些。」Claude 改之前就知道波及面，而不是改坏了下游（regression）再被报。这是用影响域数据**预防** regression。

**怎么触发（守宁缺毋滥）**：只在改**影响域大**的 define 时给（caller 多 / hub 节点），改叶子节点（没人依赖）不打扰。受辅助仲裁（§1.6）约束。

**必须标注不完整性**（压测出的关键收窄）：影响域基于调用图，动态语言漏边（§3.7）。如果说「只影响这 5 处」，用户以为安全放心改，结果改坏了漏掉的 3 处：**不完整的影响域制造虚假安全感，比不给更危险**。所以措辞必须是「**至少**影响这 5 处（动态调用可能还有未检测到的）」，不说「只影响」。静态类型语言影响域较全，价值高；动态语言价值打折但仍比不知道强：前提是诚实说「可能更多」。

**守三边界**：建议非决定（只是告知波及面，改不改、怎么改是 agent/用户的事）；结构非语义（给「谁调用它」，不给「该不该改」）；可关闭。

---

# 第四部分　工程实现

本部分把前三部分的信号、横向关系、两步响应落成可实现的工程。每个组件标注它支撑前面哪个场景/信号。

## 4.1 架构总览

BUER 是一个 MCP 服务器，跟在 agent 旁边运行。一次完整的数据流：

```
agent 编辑文件
   │
   ▼
hook 触发（PostToolUse）→ 通知 BUER（§4.4）
   │
   ▼
reconcile：扫描变更，更新结构（§4.6）
   ├─ 抽取 define、算 node_fingerprint（§4.2）
   ├─ 更新版本链、调用图、等价类（§4.2 / §4.2a）
   ├─ 扫 JUnit XML 更新测试结果（§4.5）
   └─ 检测信号（define_loop / stuck_region / debug_loop / scope / boundary / duplicates）
   │
   ▼
两步响应状态机（§4.3）
   ├─ 信号首次确认 → hook 注入提醒 agent（附横向关系线索）
   ├─ agent 自纠 → resolved
   └─ 持续超 θ₂ → 提醒用户
```

## 4.2 数据模型（schema）

支撑：所有信号的底层数据。

```sql
-- 项目
CREATE TABLE projects (
    id              INTEGER PRIMARY KEY,
    root_path       TEXT NOT NULL,           -- 项目根（boundary_breach 用，§2.4）
    lifecycle_phase TEXT DEFAULT 'growth',   -- growth / stable（§4.8）
    test_report_path TEXT,                   -- JUnit XML 位置（§4.5，NULL=用常见位置启发）
    created_at      TIMESTAMP
);

-- 判定记录（agent 每次编辑 = 一个判定），节点版本链的基础
CREATE TABLE determinations (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER REFERENCES projects(id),
    seq             INTEGER NOT NULL,        -- 单调记录号（≺ 偏序的线性扩展；≺ 本身由 gd_edges DAG 承载，§3.2 时序关联）
    file_path       TEXT NOT NULL,
    define_name     TEXT,                    -- 函数/类名（节点标识）
    node_fingerprint TEXT,                   -- 结构指纹（define_loop / find_duplicates 用）
    edit_type       TEXT,                    -- create / modify / delete
    created_at      TIMESTAMP
);
CREATE INDEX idx_det_node ON determinations(project_id, file_path, define_name, seq);

-- 调用/引用图（caller → callee，静态分析。entity-theoretic 工程结构。
-- 双重角色：(1) 喂 𝒢_D 跨 define 边的识别（§4.2a）；(2) 单独可查（agent 看「谁调用 A」）。
-- 不区分「消耗 E 关系」与「引用 C 关系」：代码静态调用在 SDT 无干净对位，见 §3.2）
CREATE TABLE call_edges (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER REFERENCES projects(id),
    caller      TEXT NOT NULL,   -- 调用方
    callee      TEXT NOT NULL,   -- 被调用方
    edge_kind   TEXT,            -- call / import
    UNIQUE(project_id, caller, callee)
);

-- 判定历史 𝒢_D 边（§3.3，横向关系 ω/Γ_R/连通分量的基础）
-- 节点 = determinations.id（每次编辑判定）。边 = u→v 表示编辑 v 消耗编辑 u 的产物。
CREATE TABLE gd_edges (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER REFERENCES projects(id),
    from_det    INTEGER REFERENCES determinations(id),  -- 上游判定 u（产物被消耗）
    to_det      INTEGER REFERENCES determinations(id),  -- 下游判定 v（消耗者）
    edge_class  TEXT NOT NULL,   -- version_chain（严格无损）/ cross_define_dataflow（静态类型语言数据流，严格 GD G3）/ cross_define_callgraph（动态语言调用图，近似有损）
    UNIQUE(project_id, from_det, to_det)
);
CREATE INDEX idx_gd_to ON gd_edges(project_id, to_det);
CREATE INDEX idx_gd_from ON gd_edges(project_id, from_det);

-- 等价类（§2.4.4，define_loop / find_duplicates / 同类传播用）
CREATE TABLE node_equivalence_classes (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER REFERENCES projects(id),
    class_key       TEXT NOT NULL,           -- 指纹归一化键
    member_node     TEXT NOT NULL,
    UNIQUE(project_id, class_key, member_node)
);

-- 测试结果（§4.5，debug_loop 用）
CREATE TABLE test_runs (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER REFERENCES projects(id),
    seq         INTEGER,                     -- 关联最近的判定 seq
    source_path TEXT, source_mtime TIMESTAMP,
    passed INTEGER, failed INTEGER, skipped INTEGER
);
CREATE TABLE test_cases (
    id          INTEGER PRIMARY KEY,
    test_run_id INTEGER REFERENCES test_runs(id),
    classname TEXT, name TEXT, file_path TEXT,
    status      TEXT                         -- passed / failed / skipped / error
);

-- 覆盖率映射（§4.5 精确档：哪个测试覆盖哪个 define）。无覆盖率数据时此表空，退启发关联。
CREATE TABLE coverage_map (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER REFERENCES projects(id),
    test_case   TEXT NOT NULL,   -- testcase 标识（classname::name）
    define_name TEXT NOT NULL,   -- 该测试覆盖的 define
    UNIQUE(project_id, test_case, define_name)
);
CREATE INDEX idx_cov_define ON coverage_map(project_id, define_name);

-- 任务范围（§2.3 task_scope_breach，opt-in）
CREATE TABLE task_scopes (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER REFERENCES projects(id),
    task_id         TEXT NOT NULL,
    allowed_glob    TEXT NOT NULL,           -- JSON array
    forbidden_glob  TEXT,
    severity        TEXT,                    -- strict / warn
    state           TEXT DEFAULT 'active',   -- active / completed
    created_at      TIMESTAMP
);

-- incident 两步响应状态机（§4.3）
CREATE TABLE incidents (
    id                INTEGER PRIMARY KEY,
    project_id        INTEGER REFERENCES projects(id),
    signal            TEXT NOT NULL,         -- define_loop / stuck_region / debug_loop / ...
    target_node       TEXT,
    state             TEXT DEFAULT 'open',   -- open / notified_agent / resolved / escalated_user
    agent_notified_at TIMESTAMP,
    post_notify_count INTEGER DEFAULT 0,     -- 提醒后又触发次数（对比 θ₂）
    escalated_at      TIMESTAMP,
    resolved_by       TEXT,                  -- test_green / back_in_scope / no_more_equiv / region_stabilized / ...
    details           JSONB,
    created_at TIMESTAMP, updated_at TIMESTAMP
);
```

**三类存储角色（R 成员 / 当前态投影 / 工程状态）**

| 类别 | 表 | 读写语义 | SDT 对位 |
|------|-----|---------|---------|
| **R 成员表**（append-only，受 R 非回撤约束） | `determinations`、`snapshots` | 只 INSERT，永不 UPDATE/DELETE | R 非回撤（§3.1）：R 的实现不可撤销 |
| **当前态投影表**（delete-then-insert，非 R 成员） | `call_edges`、`reexport_edges`、`gd_edges`、`node_equivalence_classes` | supersede 时整体重建（DELETE + INSERT） | 非 R 成员；承载 R 成员间的当前依赖关系投影（SDT §2.2.4：结构关系由 $\mathcal{G}_D$ 边承载，非 R 内嵌套） |
| **工程状态表**（可 UPDATE，非 R 成员） | `incidents`、`pending_deliveries`、`task_scopes`、`assist_state`、`safety_net_dismissals`、`projects`（元数据） | 状态可变（open→resolved 等） | 纯工程状态，无 SDT 对位 |

**边 DELETE 不违反 R 非回撤**：`determinations`/`snapshots` 是 R 成员，严格 append-only（R 非回撤，§3.1）。`call_edges`/`gd_edges`/`node_equivalence_classes` 是 R 成员间依赖关系的**当前态投影**，非 R 成员本身（SDT §2.2.4：结构关系由 $\mathcal{G}_D$ 边承载，非 R 内嵌套）。supersede 时 delete-then-insert 重建的是「当前态投影」，R 成员节点（determinations 行）永不删除。故投影表的 DELETE 不违反 R 非回撤。只有 R 成员表受 R 非回撤约束；投影表/状态表的 delete/update 是工程当前态维护，不涉及 R 非回撤。

**node_fingerprint 怎么算**：抽取 define 的结构特征（参数形态、返回、调用集合、副作用类别、体量级别），归一化为指纹。指纹是工程启发式相似度度量，借 §2.4.4 概念命名，非其判据实现（第五部分 §5.3 详列降级：只粗覆盖判据 (b)，(a)/(a')/(c) 未覆盖）。

### 4.2a 横向关系怎么算（调用图 → 𝒢_D 边 → ω/Γ_R）

横向关系分两步算：先静态分析建调用图，再用调用图 + 版本链建判定历史 $\mathcal{G}_D$，最后在 $\mathcal{G}_D$ 上算 ω/Γ_R/连通分量。

**第一步：调用图（静态分析，分语言）**

```python
def build_call_edges(project_id, file_path):
    """从一个文件抽取调用/import 边。静态分析，分语言。"""
    lang = detect_language(file_path)
    parser = get_parser(lang)              # Python:ast / TS:@typescript-eslint / Go:go/parser ...
    tree = parser.parse(file_path)
    edges = []
    for define in tree.defines():
        caller = qualified_name(define)
        for call in define.find_calls():
            callee = resolve_callee(call, tree.imports, project_symbols)
            if callee:                     # 能解析到项目内符号才建边
                edges.append((caller, callee, "call"))
        for imp in define.find_imports():
            edges.append((caller, imp.target, "import"))
    upsert_call_edges(project_id, edges)
```
- `resolve_callee`：解析不了的（动态分发/反射/字符串调用）**跳过**：动态调用漏边的来源（§3.7 局限）。
- 只建项目内边；增量更新（只重算变更文件出边）。

**第二步：判定历史 $\mathcal{G}_D$ 边（§3.3 两类边，跨 define 边按语言分层）**

```python
def build_gd_edges(project_id, det):   # det = 本次编辑判定
    """为新判定建 𝒢_D 入边。两类边对应 §3.3。"""
    # 1. 版本链边（严格无损）：消耗同一 define 的上一版本
    prev = previous_determination(project_id, det.file_path, det.define_name)
    if prev:
        insert_gd_edge(project_id, from_det=prev.id, to_det=det.id, edge_class="version_chain")
    # 2. 跨 define 边：本次编辑真正消耗的其它 define 当前版本
    #    默认全语言走调用图近似；数据流(GD G3)仅 analyze_dangling 按需调用
    #    此伪码展示设计意图分层，实际默认路径只走 callgraph 分支
    lang = detect_language(det.file_path)
    if lang in STATICALLY_TYPED and dataflow_requested:   # 按需，analyze_dangling
        deps = interprocedural_dataflow_deps(project_id, det)   # 真实数据依赖
        edge_class = "cross_define_dataflow"     # ρ(u)∩E_v≠∅ 严格对位
    else:                                  # 默认：全语言（含 TS）走调用图
        deps = callees_of(project_id, det.define_name)          # 调用图近似
        edge_class = "cross_define_callgraph"    # 近似，有损
    for dep in deps:
        producer = current_version_determination(project_id, dep)
        if producer:
            insert_gd_edge(project_id, from_det=producer.id, to_det=det.id, edge_class=edge_class)
```

**跨 define 边的两档**：

- **调用图档（默认，全语言含 TS）**：`callees_of` 来自 call_edges，调用图近似（调用 ≠ 真消耗，有假边），诚实标 `cross_define_callgraph`。实测 got 项目默认 ingest：gd_edges 168 条全 callgraph，0 条 dataflow。
- **数据流档（按需，TS + analyze_dangling + 工具链齐全）**：`interprocedural_dataflow_deps` 做过程间数据流分析：追 define A 的产物（返回值 / 输出参数 / 写入的状态）是否真的流进 det 这次编辑的 define 并被使用。只在真有值流动时建边。这**精确命中 GD G3** $\rho(u) \cap E_v \neq \emptyset$：A 的产物（$\rho$）进入 B 的元素集（$E$）。静态类型语言的类型信息让别名分析可解，准确率高。需工具链（tsconfig + node_modules/typescript + node）齐全；缺失则 degrade 回调用图。

**数据流档消除假边**：调用图会把「B 调用 A 但没用返回值」（日志、纯副作用调用）也建边，污染根因方向。数据流只在产物真被使用时建边，去掉这类假边：对 debug「指错方向代价高」的场景，这是关键收益。

**实现成本与按需定位（诚实）**：实测 ~4s/调用（每次重载 TS program）；全量 256 define ≈ 17 分钟 vs 调用图 2.2 秒（~450 倍代价）。默认走调用图是合理性能权衡。数据流作为按需工具（`analyze_dangling`）：用户深挖 bug 根因时，4s 换精度是合理交换——这是工具的正确定位，不是「成本不存在」。

**第三步：ω / Γ_R / 连通分量（在 𝒢_D 上，严格运算）**


```python
def predecessor_cone(project_id, det_id):
    """↓v：所有祖先判定（gd_edges 上溯）。"""
    return bfs_ancestors(project_id, det_id)   # 沿 from_det←to_det 上溯

def omega(project_id, u_det, v_det):
    """前驱重叠 ω = |↓u ∩ ↓v|（Math Ext §11.1b）。纯结构。"""
    return len(predecessor_cone(project_id, u_det) & predecessor_cone(project_id, v_det))

def gamma_r_related(project_id, u_det, v_det):
    """Γ_R：存在共同汇合 w，u、v 都是 w 的前驱（GD §7.1）。"""
    succ_u = successor_set(project_id, u_det)
    succ_v = successor_set(project_id, v_det)
    return len(succ_u & succ_v) > 0    # 共享下游汇合

def connected_component(project_id, det_id):
    """连通分量：无向可达（GD §9.2）。不同分量 = 因果独立。"""
    return undirected_reachable(project_id, det_id)
```

**第四步：影响域查询（组合）**

```python
def get_blast_radius(project_id, node, omega_threshold, depth=2):
    cur = current_version_determination(project_id, node)
    # 共享祖先(ω 大)——ω 不可传递(Math Ext Thm 11.1f.2)，只取直接，不链式扩散
    shared_ancestry = [d for d in antichain_peers(project_id, cur)
                       if omega(project_id, cur, d) > omega_threshold]
    # 共享下游(Γ_R)——可传递(GD Prop 7.2)，可沿关联链扩展(限 depth)
    shared_downstream = gamma_r_cluster(project_id, cur, depth)
    # 调用关系(call_edges，entity-theoretic 补充)
    callees, callers = call_neighbors(project_id, node)
    # 同类(§2.4.4)
    siblings = equivalence_class(project_id, node)
    return {"shared_ancestry": shared_ancestry,     # ω，不传递
            "shared_downstream": shared_downstream,  # Γ_R，可传递
            "calls": callees, "called_by": callers,
            "same_kind": siblings}
```

注意 ω（不传递）和 Γ_R（可传递）在遍历上的区别已编码（§3.2 性质落地）：shared_ancestry 不扩散，shared_downstream 可沿链扩展。

**根因方向**（§3.5）= shared_ancestry ∩ 最近改过 ∪ callees ∩ 最近改过（join determinations.seq）。

**跨语言覆盖**（决定调用图 → 跨 define 边的覆盖度）：

| 语言 | parser | 覆盖度 |
|---|---|---|
| Python | ast | 中高（动态特性多，getattr/反射漏） |
| TypeScript/JS | @typescript-eslint / acorn | 中高（回调/高阶函数部分漏） |
| Go | go/parser | 高（接口动态分发部分漏） |
| 其他 | 按需加 extractor | 未覆盖语言无跨 define 边（仅版本链边可用） |

**诚实边界**：ω/Γ_R/连通分量运算严格（纯 $\mathcal{G}_D$ 结构），但 $\mathcal{G}_D$ 的跨 define 边靠调用图静态识别，继承动态调用/数据流/非调用路径三重局限（§3.7）。版本链边无损，跨 define 边有损。运算严格、输入有损。

## 4.3 两步响应状态机

支撑：§1.4 / §7（前文）两步响应。每次 reconcile 后推进所有 active incident：

```python
def advance_incidents(project_id, reconcile_result):
    for inc in open_or_notified(project_id):
        if inc.state == "open":
            if inc.escalate_user_directly:    # 作弊类（test_tampering，§2.7）：跳过「先提醒 agent」
                inc.state = "escalated_user"
                inc.escalated_at = now()
                queue_user_notification(inc, with_lateral_context(inc.target_node))
                continue
            # 自纠类：首次确认 → 第一步提醒 agent（附横向关系，§3.6）
            inc.state = "notified_agent"
            inc.agent_notified_at = now()
            queue_agent_injection(inc, with_lateral_context(inc.target_node))

        elif inc.state == "notified_agent":
            if resolved(inc, reconcile_result):
                inc.state = "resolved"
                inc.resolved_by = resolve_reason(inc)
            elif signal_recurred(inc, reconcile_result):
                inc.post_notify_count += 1
                if inc.post_notify_count >= theta_2(inc.signal):
                    inc.state = "escalated_user"          # 第二步
                    inc.escalated_at = now()
                    queue_user_notification(inc, with_lateral_context(inc.target_node))

        elif inc.state == "escalated_user":
            if resolved(inc, reconcile_result):
                inc.state = "resolved"
                inc.resolved_by = resolve_reason(inc)
```

**resolve 判据**（各信号客观性不同，第五部分 §5.3 详列）：

| 信号 | resolve 判据 | 客观性 |
|---|---|---|
| debug_loop | 关联测试转绿 | 客观 |
| task_scope_breach / boundary_breach | 编辑回到范围内 | 客观（路径布尔） |
| define_loop | 不再绕回旧等价类 | 客观（指纹） |
| stuck_region | 区域稳定（停改 / $d_J$ 减小） | 推断 |
| find_duplicates | 重复消除 / 用户标接受（查等价类成员数，状态判断，非停改推断） | 客观 |

**θ₁ / θ₂ 初值**（初值待 dogfooding 校准）：

| 信号 | θ₁ | θ₂ | 备注 |
|---|---|---|---|
| stuck_region | 5 | 3 | 版本链长达 5 触发 |
| define_loop | 1 | 2 | 出现回环即提醒 |
| debug_loop | 5（同 stuck_region） | 3 | stuck_region 增强；共用结构阈值 |
| regression | 1 | 2 | 绿转红一次即报 |
| test_tampering | 1 | 1 | 走 escalate_user_directly 旁路直接上报用户；θ₂ 不参与复发升级 |
| task_scope_breach / boundary_breach | 1 | 1 | 单次判定，越界即提醒 |
| find_duplicates | 1 | 1 | — |

## 4.4 hook 集成（两步响应送达）

支撑：§1.4 第一步送达 agent。默认用 hook，以 Claude Code 为例：

```json
{
  "hooks": {
    "PostToolUse": [{
      "matcher": "Edit|Write",
      "hooks": [{
        "type": "http",
        "url": "http://localhost:PORT/buer/post-edit",
        "async": false
      }]
    }]
  }
}
```

agent 每次编辑后，hook POST 编辑事件到 BUER。BUER reconcile + 推进状态机，若有 notified_agent 级信号，hook 响应体把提醒文本（含横向关系线索，§3.6）注入 agent 上下文，agent 下一步推理时看到。

**其他 agent**：Cursor（onPostEdit）、Cline（tool call hook）机制类似，适配方式同理。**无 hook 环境**：退回 agent 主动调 `check_drift` 工具（§4.7），可靠性低一些但仍工作。

**测试结果实时捕获（可选增强）**：PostToolUse matcher=Bash 可捕获测试命令的 stdout/stderr，补 JUnit XML 落盘的延迟。opt-in。

## 4.5 测试接入（JUnit XML + 覆盖率，testcase↔define 关联分两档）

支撑：§2.2 debug_loop。reconcile 后扫描测试产物，解析入 test_runs / test_cases：

```python
def scan_test_results(project_id):
    # 1. JUnit XML：测试通过/失败（debug_loop 的持续失败判定）
    for path in locate_junit_xml(project_id):     # test_report_path 或常见位置启发
        if already_ingested(path, mtime(path)): continue
        run = parse_junit_xml(path)               # testsuites→testsuite→testcase，跨框架通用
        insert_test_run_and_cases(run, nearest_seq(project_id, mtime(path)))
    # 2. 覆盖率（可选）：testcase↔define 精确关联
    for cov in locate_coverage(project_id):       # coverage.xml / lcov，标准产物
        if already_ingested(cov, mtime(cov)): continue
        ingest_coverage_map(project_id, parse_coverage(cov))   # 哪些 define 被哪些测试覆盖
```

**持续失败判定**（debug_loop 用）：某 testcase 在某 define 修改窗口跨越的所有 test_runs 中 status=failed。

**testcase ↔ define 关联（两档，与数据流分层同构）**：

- **精确档（有覆盖率数据）**：coverage.xml / lcov 记录每个测试执行了哪些 define（行级覆盖 → 映射到 define）。testcase↔define 用覆盖率**精确映射**：测试 X 失败时，BUER 知道它实际覆盖了哪些 define，debug_loop 把失败精确归到这些 define。这消除启发档的归错风险。
- **启发档（无覆盖率，仅 JUnit）**：测试文件路径 / testcase 名与 define 的模块/名匹配（validate_token ↔ test_validate_token，靠命名猜）。易归错，第五部分 §5.3 标降级。无匹配退「项目整体测试持续失败」弱关联。

**为什么覆盖率值得接入**：覆盖率文件是标准测试产物（pytest --cov / jest --coverage / go test -cover），解析成本同 JUnit XML。它把 testcase↔define 从「命名启发」（易错，归错 define 则 debug_loop 指错方向）升到「精确映射」。debug_loop 的核心是「这个 define 改了 N 次测试一直不过」，关联错则整个信号失真：覆盖率是消除这个失真的唯一精确来源。

**前提分层**：
- 无测试 → debug_loop 不可用，退 stuck_region（§2.2 主力仍工作）。
- 有测试、无覆盖率 → debug_loop 启用，testcase↔define 启发档（标降级）。
- 有测试、有覆盖率 → debug_loop 启用，testcase↔define 精确档。

onboarding 检测测试 + 覆盖率配置并提示（§4.8）：「检测到测试但无覆盖率，开启 --cov 可让 debug 定位更准」。

## 4.6 reconcile 流程

支撑：所有信号的数据更新入口。agent 每次编辑（或周期触发）调一次：

```python
def reconcile(project_id, changed_files):
    affected = []
    for f in changed_files:
        # boundary 检查（§2.4，单次即判）
        if not f.startswith(project.root_path):
            write_incident("boundary_breach", target=f); continue
        # 抽取 define + 算指纹
        for d in extract_defines(f):
            fp = compute_fingerprint(d)
            seq = next_seq(project_id)
            det = insert_determination(project_id, seq, f, d.name, fp, d.edit_type)
            affected.append(d)
            update_equivalence_class(project_id, d.name, fp)   # 等价类（find_duplicates / define_loop）
        build_call_edges(project_id, f)                        # 调用图（§4.2a 第一步）
    # 判定历史 𝒢_D 边（§4.2a 第二步：版本链边 + 跨 define 边）
    for d in affected:
        build_gd_edges(project_id, d.determination)
    # task_scope 检查（§2.3，若有 active scope）
    check_task_scopes(project_id, affected)
    # 测试结果
    scan_test_results(project_id)
    # 信号检测（仅对 affected，增量）
    detect_signals(project_id, affected)
    # 测试状态变化检测（§2.6 regression / §2.7 tampering）
    detect_test_transitions(project_id, affected)
    # 推进两步响应状态机
    advance_incidents(project_id, ReconcileResult(affected))
```

**测试状态变化检测**（regression / tampering，§2.6-§2.7，零边际成本：复用已接入的测试结果 + 版本链）：

```python
def detect_test_transitions(project_id, affected):
    for tc in changed_status_testcases(project_id):   # 本次 reconcile 状态变化的 testcase
        if tc.prev == "passed" and tc.now == "failed":
            # 绿转红：regression（§2.6），但需排除「测试自己改坏」
            if not test_define_edited(project_id, tc):  # 被测代码改导致
                write_incident("regression", target=tc, reason="代码改动导致测试失败")
        elif tc.prev == "failed" and tc.now == "passed":
            # 红转绿：区分真修复 vs tampering（§2.7）
            if test_define_edited(project_id, tc) and not tested_code_edited(project_id, tc):
                # 转绿那次只改了测试、没改被测代码 → tampering 嫌疑
                write_incident("test_tampering", target=tc, escalate_user_directly=True)

# 悬空引用（§2.8）：在 build_call_edges 解析 callee 时副产
def detect_dangling_refs(project_id, det):
    if detect_language(det.file_path) not in STATICALLY_TYPED:
        return                                  # 动态语言不做（§2.8 边界）
    for callee in unresolved_callees(project_id, det):   # build_call_edges 解析不到的
        if persistently_undefined(project_id, callee):   # 跨多次编辑仍无定义且非已知库
            write_incident("dangling_ref", target=callee)
```

**自动派生**（守 §1.5）：reconcile 全部输入来自 BUER 能观察到的：文件变更、文件内容、git、junit xml、覆盖率。不要求 agent 声明（task_scope 除外，opt-in）。test tampering 的 `escalate_user_directly` 标记使其跳过「先提醒 agent」，直接上报用户（§2.7，作弊类例外）。

## 4.7 MCP 工具集

| 工具 | 功能 | 支撑 |
|---|---|---|
| `check_drift()` | 返回当前 incident 摘要（friendly）；无 hook 时 agent 主动调 | §4.4 回退送达 |
| `set_task_scope(project_root, allowed_globs, forbidden_globs=[])` | 声明任务范围（opt-in）；allowed 白名单 + forbidden 黑名单（禁止区优先于 allowed）；当前为单一当前任务范围 | §2.3 |
| `clear_task_scope(project_root)` | 结束任务范围 | §2.3 |
| `get_lateral_context(node)` | 查某节点的影响域 + 同类（影响域/根因方向） | §3.3-§3.5 |
| `find_duplicates()` | 列结构重复的节点对 | §2.5 |
| `acknowledge_incident(id, mark)` | 标记已处理 / 误报 / 接受 | 用户介入 |
| `project_overview()` | onboarding：项目结构概览 + 测试配置检测 | §4.8 |
| `get_savings()` | 节省估算报告 | §4.7（下） |

**注 — set_task_scope 当前实现**：签名为 `set_task_scope(project_root, allowed_globs, forbidden_globs=[])`。`task_id`（并发任务管理）和 `severity`（strict / warn）未实现，留后续；当前为单一当前任务范围，重新调用即覆盖。

**默认输出 friendly**（守 §1.5）：给 vibe coder 的是人话；结构指标在 raw 参数（opt-in）和 audit 层备查。

## 4.8 savings、lifecycle、onboarding

**savings 节省估算**（让 vibe coder 感知价值）：
- 口径对齐两步响应：BUER 的价值 = 第一步触发的 agent 自纠（resolved by agent，省下继续打转的开销）+ 第二步及时升级（用户早介入，省下继续烧的开销）。
- 估算 = Σ（被拦截的 incident 预计若不拦会多耗的编辑次数 × 每次编辑的 token/时间经验值）。
- 诚实标：反事实估算，非实测；数字带「约」，配项目周期总成本作分母（让用户看比例）。

**lifecycle（重估后收窄角色）**：
- 原服务对象（项目级 signature）已砍，lifecycle 的主要剩余用途是**阈值校准**：growth 期项目快速变化，stuck_region / define_loop 的 θ 应放宽；stable 期收紧。
- 不再需要复杂的 growth/stable 状态机，简化为：项目早期（判定数少 / 变化快）用宽阈值，成熟后用常规阈值。

**onboarding（project_overview，第一天反馈）**：
- 输出：项目结构概览（文件数、主要模块、识别出的 hub 节点即高影响域节点）、当前监测启用的信号、**安全网检测**（测试 / 覆盖率 / git，缺失则提示，§2.9）。
- 解决 §1.1 痛点：装 BUER 第一天就有「它看到了什么、能帮我什么」的反馈。

## 4.9 辅助：一键补测试

支撑 §2.9 的处置。把「加测试」从会失控的模糊任务（实践中常拖几十轮对话）变成结构化、有边界、有监测的工作流。

**为什么需要结构化**：vibe coder 让 agent「加测试覆盖率」会失控，因为任务没框定：补哪些、到哪算完没边界，agent 东补一个西补一个，用户反复确认。BUER 的结构数据正好能框定。

**工作流**：

```python
def assist_add_tests(project_id, target=None):
    # 1. 没覆盖的 define 清单（覆盖率数据 + define 全集）
    uncovered = defines_without_coverage(project_id)
    # 2. 按结构重要性排序（BUER 独有：不是盲目按文件，按该先测哪个）
    ranked = sort_by_importance(uncovered, by=[
        "change_frequency",   # 被反复改的（stuck_region 历史）—— 易出 bug，优先
        "blast_radius",       # 影响域大的（caller 多 / hub）—— 坏了波及广，优先
        "is_core",            # 核心节点
    ])
    # 3. 结构化任务注入 agent（已有 hook），目标可设（核心 define 覆盖 / 覆盖率阈值）
    task = build_test_task(ranked, goal=target or "cover core defines")
    inject_to_agent(task)
    # 4. 进度：补一个、覆盖率勾一个，客观判完成（核心 define 都覆盖 → 完成）
    # 5. 过程监测：stuck_region/define_loop 照常（防 agent 补测试时打转）
    #    find_duplicates 对测试目录放宽（测试天然相似，只报几乎完全复制）
```

**BUER 独有价值**（vs 直接让 agent「加覆盖率」）：结构化目标（补哪些）+ 重要性优先级（先补常改/影响大的，防 agent 挑简单的刷数字）+ 进度度量（补到哪，免反复确认）+ loop 监测（补测试时打转照样报）。这四个正是「加覆盖率」拖几十轮缺的。

**压测出的收窄**：
- **空壳测试**（`assert True` / 无断言）：agent 可能为刷覆盖率写空测试。BUER 能识别明显空壳（无断言），标「这些可能是空壳」；但「断言有没有意义」是语义，BUER 不判（守定位）。诚实：BUER 确保「有测试 + 有断言」，不确保「测试有意义」：后者用户/agent 把关。
- **补测试不免检**：补测试期间 stuck_region / regression 照常工作，不是免检通道。
- **补前先提交**：补测试是大批改动，触发前联动提示「先 git commit 一个回退点」（§4.10）。

**守三边界**：用户触发（点「补测试」，非 BUER 自作主张）；BUER 给清单+优先级+目标（结构），测试由 agent 写（语义）；可拒绝/调整清单。

## 4.10 辅助：提交时机 + 一键提交

支撑 §2.9 git 面。解决「vibe coder 不习惯提交 → 改崩回不去」。

**机制**：
- BUER 识别**好提交点**：一组相关编辑告一段落（结构稳定，一段时间没再改这块）+ 测试绿（若有）。这是 BUER 独有的：它知道「一个完整改动单元」何时形成，比机械定时或 vibe coder 凭感觉强。
- 提示（候选非断言）：「validate_token 这组改动看起来告一段落了，距上次提交已改 12 处，要提交吗？」
- 一键提交：BUER 给 commit 范围建议（这批改的文件）+ 结构化 message（`touched validate_token, create_token in auth/`），用户/agent 可补语义。

**压测出的收窄**：
- **半成品提交风险**：BUER 判断「结构稳定」≠「逻辑完整」（不懂语义）。所以措辞是「看起来告一段落，要提交吗」（候选 + 疑问），不断言「这是完整的」，避免诱导半成品提交。
- **message 不完美**：BUER 不懂语义，写不出「修复 token 过期 bug」式的 message，只能给结构描述。诚实：结构 message 为主，提示用户/agent 可补：总比 vibe coder 不提交强。

**守三边界 + 宁缺毋滥**：只在好提交点提示（不是每次编辑）；建议非强制（可不提交）；可关闭。

## 4.11 辅助仲裁（防辅助变噪音）

§1.6 的「宁缺毋滥」在工程上靠仲裁实现。但辅助分两类不同时间尺度，**不放同一队列**：

- **健康提示**（安全网预警 §2.9）：低频，在 onboarding / 阶段性健康检查出现，有自己的节奏（提示一次为主、可永久关闭）。不参与事中仲裁：它和事中辅助几乎不会同时竞争。
- **事中辅助**（提交时机 §4.10 / 影响域预览 §3.8）：高频，每个编辑决策点都可能触发，需仲裁。

事中辅助仲裁：

```python
def arbitrate_inline_assists(project_id, candidates):
    # 事中辅助同时想出现 → 最多出一个，按优先级
    PRIORITY = ["commit", "blast_radius"]   # 提交 > 影响域（提交是兜底，优先）
    active = [a for a in candidates if a.should_fire(project_id)]
    if not active: return None
    return min(active, key=lambda a: PRIORITY.index(a.kind))   # 只出优先级最高的一个
```

- 报警（agent 行为问题）、健康提示、事中辅助三条独立通道；只有事中辅助受仲裁，报警该报就报，健康提示按自己节奏。
- 每个事中辅助的 `should_fire` 是高门槛事件（提交：好提交点 + 久未提交；影响域：改高影响域 define），不是每步触发。
- 事中辅助可全局调频（用户嫌多 → 调高门槛 / 关部分）。

---

# 第五部分　SDT 映射与严格性

本部分给严格性审计：BUER 每个信号/关系背后的 SDT 依据集中列出，逐条对 SDT 原文验证（防漂移），并给 Trade-off 总账与降级清单。前四部分的【依据】指针都指向这里。

## 5.1 BUER 在 SDT 框架的位置

BUER 是 **software project kind 的 entity theory**，纯 DI/SDT，不依赖 Bio。

依据 [DI Lemma 3]：$\mathcal{G}_D$ 层的特征是 SDT 元层构造，哪个特征组合构成某 kind 的 signature 由该 kind 的 entity theory 定。DI 原文明确把 software process 列为合法 kind：同一 $\mathcal{G}_D$ 特征「在 software processes 中捕获通过计算步骤的算法状态保持」。

依据 [DI line 175]：生物有机体的 signature（要求递归同构子图、排除 Type II split）**不是 software process 的候选 signature**，后者用不同的特征组合。这从 DI 原文直接支持 BUER 不套用 Bio signature、自定义 software project signature：即砍 Bio 五特征的根据。

BUER 借用 [DI Main Theorem] 的核心思路——kind 同一性由 $\mathcal{G}_D$ 特征承载——作为设计类比：漂移信号对应「signature 特征失效」，信号分级响应是「partial failure」与「persistent failure」的实用近似。这是工程类比，不是定理的严格实例化。区别有三：（1）BUER 不构造正向软件项目 signature（三 case 中 determinate 的「满足条件」），对工具无应用价值；（2）BUER 不区分 termination 与 ontological indeterminacy——两者运作等价（持续异常、需升级），归一处理；（3）漂移信号是「signature 失败」的启发代理，非精确实例化（无完整签名图），不宣称三 case 判决在 BUER 内严格成立。三条引用 [DI Lemma 3] / [DI line 175] / [DI Main Theorem] 核对原文全部准确，保留不动；改的仅是应用措辞。

## 5.2 SDT apparatus 速查

BUER 用到的 SDT 原生概念，全部来自 SDT 主文档 / GD / Math Ext / DI：

| 概念 | 形式 | 来源 | BUER 用途 |
|---|---|---|---|
| $\mathcal{G}_D$ 四元组 | $(V, \to, \rho, \lambda)$ | [GD §1] | 项目结构图（判定历史） |
| 前驱重叠 $\omega(u,v)=\lvert\downarrow u\cap\downarrow v\rvert$ | 反链上共享祖先数 | [Math Ext §11.1b] | 横向关系：共享祖先（§3.2） |
| $\Gamma_R$ R-关联 | 共享下游汇合，可传递 | [GD §7.1, Prop 7.2] | 横向关系：共享下游（§3.2） |
| 连通分量 | 无向可达 = 因果独立 | [GD §9.2] | 横向关系：因果独立簇 |
| 反链 / 极大反链 | ≺-incomparable 节点集 | [Math Ext §11.1a] | 横向节点（空间切片） |
| 调用图（输入层） | caller → callee（静态分析） | entity-theoretic；喂 $\mathcal{G}_D$ 跨 define 边识别（§3.3） | 横向关系的输入构建 |
| 产物函数 | $\rho: V \to \mathcal{P}_{\text{fin}}(\mathcal{S})$ | [GD §1] | 范围控制（§2.3） |
| 任意尺度 R-member | $e$ 可为任意尺度的 realized structure | [SDT §2.1.2] | define 节点作原子元素 |
| 结构层等价 | $L_1 \sim L_2$ 三判据 | [SDT §2.4.4] | define_loop / find_duplicates / 同类 |
| 偏序 | $\prec$ 良基（WF），depth $d(v)$ | [GD §1 WF] | 时序关联（§3.2） |
| Jaccard 距离 | $d_J(u,v) = \|\text{anc}\triangle\| / \|\text{anc}\cup\|$ | [Math Ext §12.1.2] | stuck_region / define_loop 内部判据 |
| C 关系（非消耗） | $S$ 作分析对象被多 $L$ 引用，非排他 | [SDT §2.2] | 引用关系（§3.2） |

## 5.3 每个信号/关系的 SDT 依据 + 忠实/降级

逐条列。【忠实】= 严格对位 SDT；【降级】= 工程近似或启发，标明理由。

**define_loop**（§2.1）
- 【忠实】「绕回旧状态」= [SDT §2.4.4] 结构层等价（两版本对应 $L$ 满足三判据）。
- 【降级】node_fingerprint 是工程启发式哈希，借 §2.4.4 概念命名，非其判据实现：实际只粗覆盖判据 (b)（P-配置/属性模式），判据 (a) E-结构同构、(a') determination type（$|E|,|S|$）、(c) C-过滤等价均未覆盖。精确实现三判据开销极大，指纹是可接受的保守近似。

**stuck_region**（§2.2，主力）
- 【忠实】「同区域反复改」= 同一 define 版本链长（[GD] chain）；「未绕回」= 无 §2.4.4 等价回环；「幅度大/收敛判断」= 相邻版本 $d_J$（[Math Ext §12.1.2]）。
- 【降级】resolve 判据「区域稳定」是行为停止推断，非客观确认（agent 可能转去做别的而非真解决）。stuck_region 是注意力信号非判决：纯结构无法区分 debug loop 与正常复杂开发。

**debug_loop**（§2.2，增强）
- 【忠实】结构部分（stuck_region）同上；测试转绿是客观 resolve 判据。
- 【降级】testcase ↔ define 关联分两档（§4.5）：有覆盖率数据用精确映射（无降级），无覆盖率退路径/名称启发（此档降级，易归错 define）。需项目配置 junit xml（opt-in 前提），覆盖率为可选增强（标准产物，接入成本同 junit xml）。

**task_scope_breach**（§2.3，opt-in）
- 【依据·entity-theoretic】范围约束本身是工程约束，**非 SDT 定理**：虽然可用 [GD] $\rho(D)$ / $E(D)$ / R-member 的词汇表达「编辑落在范围内」，但借 SDT 词汇表达不等于有 SDT 定理支撑。「agent 应限定范围」是工程要求，与 [SDT §2.4.2] use-exclusivity 无关（那是 $e$ 不能参与两个 $L$，与范围控制是两回事）。诚实标 entity-theoretic，不计忠实。
- 【降级】依赖用户/agent 声明，非自动派生（§1.5 唯一例外）；无声明退 boundary 检查（§2.4）。

**boundary_breach**（§2.4）
- 【忠实】基于 R-member 是否落在项目根定义的集合内。
- 【降级】压测推翻了早先「最干净、误报极低」的断言：monorepo / 多根工作区、符号链接、项目外合法配置都是真误报源（§2.4）。需支持多项目根配置 + 向上解析符号链接，否则误报。单项目清晰根目录时仍干净。

**find_duplicates**（§2.5）
- 【忠实】「结构等价」= [SDT §2.4.4] 等价类，完全重复 = $\epsilon_R = 0$。
- 【降级】node_fingerprint 同 define_loop 降级：工程启发式哈希，借 §2.4.4 等价类命名，只粗覆盖判据 (b)，(a)/(a')/(c) 未覆盖，非 §2.4.4 等价类的实现；「该不该合并」不判断（报事实不做判断，守定位）；默认排除测试/样板/生成代码目录（真误报控制）。
- 【忠实·resolve】resolve 判据：查等价类成员数是否降回 ≤1（该指纹类的重复真消除），或用户标接受——均为状态判断，客观；不依赖「停改推断」（重复是状态，两个等价 define 还在仓库里就不 resolve，不管有没有人再碰它）。

**regression**（§2.6，前提：有测试）
- 【依据·entity-theoretic】核心信号是**测试状态 passed→failed**：这是运行时执行结果，是 entity-theoretic 事实，**不是** $\mathcal{G}_D$ 的结构判定（不要把它说成「判定历史状态变化」，那是过度 SDT 化）。BUER 用到的 SDT 数据只是辅助：用版本链确认「转红那次是被测代码改了」（区分 regression 与测试自己改坏）。所以 regression = entity-theoretic 测试信号 + 版本链辅助判定来源。
- 【降级】testcase↔define 关联同 debug_loop（有覆盖率精确档 / 无则启发档）；需求变更导致的合法绿转红用疑问句措辞缓解。前提（有测试）不满足时不报：诚实边界，非缺陷。

**test tampering**（§2.7，前提：有测试 + agent 改测试）
- 【依据·混合】触发信号是测试 failed→passed（entity-theoretic 运行结果）。但**区分「转绿是因为改了测试 define 还是改了被测 define」用的是版本链**（$\mathcal{G}_D$，SDT）：这一步是 SDT 数据的客观判定。所以 tampering = entity-theoretic 测试信号触发 + 版本链（SDT）做关键区分。版本链区分这部分是严格的（编辑了哪个 define 是判定历史的客观事实）。
- 【降级】合法修正错测试也会触发（缓解：仅「只动测试、没动被测代码」时报 + 疑问措辞）。**上报方式例外**：作弊类直接上报用户，不走「先提醒 agent」（§2.7）：这是两步响应的设计例外，已在 advance_incidents 编码。

**悬空引用**（§2.8，前提：静态类型语言）
- 【依据·entity-theoretic】核心是**调用图 callee 解析**（静态分析，entity-theoretic，§4.2a 副产品）+ 外部已知库表。判据「callee 解析不到 define 全集 ∪ 已知库」里，define 全集是 $\mathcal{G}_D$ 节点集（SDT），但「已知库」是外部表、解析机制是静态分析：所以这是 entity-theoretic 工程信号，用 $\mathcal{G}_D$ 节点集作其中一个比对集合，不宜整体标为「基于 $\mathcal{G}_D$」。
- 【降级】「持续未定义」是误报控制（排除自顶向下还没写）；「静态类型语言」是诚实边界（动态语言合法动态引用太多，不报）。前提不满足时不报，非缺陷。

**横向关系**（§3.2-§3.3，第二次修正）
- 【忠实】运算层严格：前驱重叠 $\omega$ = [Math Ext §11.1b]；$\Gamma_R$ 关联 = [GD §7.1]（可传递 Prop 7.2）；连通分量 = [GD §9.2]；$\omega$ 不可传递 = [Math Ext Thm 11.1f.2]；同类 = [SDT §2.4.4]；时序 = [GD ≺ 偏序]。这些是 SDT/GD/Math Ext 定理，BUER 在判定历史 $\mathcal{G}_D$ 上严格计算。
- 【降级·按需】输入层：$\mathcal{G}_D$ 的**跨 define 边默认全语言走调用图近似**（调用 ≠ 消耗，有假边）。版本链边无损。数据流档（对位 GD G3 $\rho(u)\cap E_v\neq\emptyset$）仅 `analyze_dangling` 按需调用，且需 TS 工具链（tsconfig + node_modules/typescript + node）齐全时生效；缺失则 degrade 回调用图。故 **G3 严格是按需窄场景能力，非默认主体**。漂移来源：调用 ≠ 依赖（假边）+ 动态调用漏 + 非调用/非数据流路径（全局状态/数据库）——默认全语言均有。
- 【澄清】数据流是结构分析（追值流动），与 $\sigma$ 的语义理解不同，做数据流不破「不理解语义」定位。
- 影响域（§3.3）：【忠实】$\omega$/$\Gamma_R$/连通分量计算严格。【降级】受 $\mathcal{G}_D$ 跨 define 边输入局限。
- 根因方向（§3.5）：【降级】$\omega$ 共享祖先 + 时序 + 调用的启发组合，提示价值高但不保证命中。措辞「方向」非「结论」。
- 同类传播（§3.6）：【降级】同类不一定同 bug，措辞「可能」。
- **不做：联合约束能力 $\sigma$**（[Math Ext §11.1d]）：SDT 有此运算，但依赖 $G$（$G_{\text{co}}$ 配置兼容），落到代码即理解调用语义。BUER 刻意不理解语义（守定位 + 语义理解性价比不足：debug 回溯用 $\omega$+时序+调用已够，$\sigma$ 精度过剩且引入语义猜测污染结构事实的确定性）。不实现，非能力缺陷。

**辅助功能的 SDT 地位**（§1.6 / §2.9 / §3.8 / §4.9-§4.11）
辅助功能大多是**已有 SDT 数据的事中应用**或**纯 entity-theoretic 工程**，不新增独立 SDT 依据：
- 影响域预览（§3.8）：复用横向关系数据（$\omega$/$\Gamma_R$/调用图，依据同横向关系）。事前用法，SDT 地位继承横向关系（运算严格、输入分语言）。
- 一键补测试（§4.9）：重要性排序复用 stuck_region 历史（版本链）、blast_radius（横向关系）、hub（$\mathcal{G}_D$ 度数）：这些有 SDT 依据；工作流编排本身是工程，无 SDT 对位。
- 提交辅助（§4.10）：「结构稳定」判定用版本链（一段时间无新判定，依据 ≺/chain）；commit message 是工程，无 SDT。
- 安全网预警（§2.9）：测试/git 有无是 entity-theoretic 项目状态检测，**无 SDT 对位**，纯工程健康提示。
- 辅助仲裁（§4.11）：纯工程，无 SDT。
诚实标注：辅助不是 SDT 信号，是结构数据（部分有 SDT 依据）+ 工程编排的组合。它们的价值在工程实用性，不在 SDT 严格性；不为它们声称 SDT 对位。

**两步响应 resolve 判据客观性分层**（§4.3）
- 客观：debug_loop（测试转绿）、task_scope/boundary（路径布尔）、define_loop（指纹不再回环）、find_duplicates（查等价类成员数是否降回 ≤1——状态判据，非停改推断）。
- 推断：stuck_region（区域稳定）。
- 【降级】推断类 resolve 可能假阳性（误判已解决）；可接受：避免重复报警的价值大于偶尔误判的代价。

## 5.4 Trade-off 总账

| 部分 | 忠实 | 降级 | 偏离 |
|---|---|---|---|
| §5.1 BUER 定位（DI software project entity theory，砍 Bio 有 DI line 175 背书） | 3 | 0 | 0 |
| define_loop | 1 | 1 | 0 |
| stuck_region | 1 | 1 | 0 |
| debug_loop（结构 + 覆盖率精确档） | 2 | 1 | 0 |
| task_scope_breach（entity-theoretic 工程约束，借 SDT 词汇） | 0 | 3 | 0 |
| boundary_breach（加 monorepo/符号链接降级） | 1 | 1 | 0 |
| find_duplicates | 1 | 1 | 0 |
| regression（entity-theoretic 测试信号 + 版本链辅助） | 0 | 2 | 0 |
| test tampering（测试信号触发 + 版本链 SDT 区分） | 1 | 1 | 0 |
| 悬空引用（entity-theoretic 调用解析 + define 全集比对） | 0 | 2 | 0 |
| 横向关系运算（$\omega$/$\Gamma_R$/连通分量/同类/时序，严格 SDT） | 5 | 0 | 0 |
| 横向关系输入·版本链边（无损） | 1 | 0 | 0 |
| 横向关系输入·跨 define 边·数据流档（按需·analyze_dangling·需 TS 工具链） | 0 | 1 | 0 |
| 横向关系输入·跨 define 边·调用图档（默认全语言，近似有损） | 0 | 1 | 0 |
| 影响域 / 根因方向 / 同类传播 | 1 | 2 | 0 |
| 联合约束 $\sigma$（不做，依赖语义） | 0 | 1 | 0 |
| 两步响应 resolve | 4 | 1 | 0 |
| 辅助功能（影响域预览/补测试/提交：复用已有 SDT 数据 + 工程编排） | 0 | 0 | 0 |
| 辅助功能（安全网预警/仲裁：纯 entity-theoretic 工程，无 SDT 对位） | 0 | 0 | 0 |
| **合计** | **21** | **19** | **0** |

**零偏离**。

**横向关系忠实度说明**：横向关系的 SDT 严格性集中在运算层（$\omega$/$\Gamma_R$/连通分量是定理）。输入层**默认全语言调用图近似**（有损）。数据流（对位 GD G3）是按需窄场景能力（`analyze_dangling` + TS 工具链），非默认主体，移入降级列（忠实 22→21，降级 18→19）。各处【忠实】【降级】标注的依据见 §5.3，对 SDT 原文的逐条验证见 §5.6。开发过程中的定性调整（含曾用托词排除数据流、后纠正、后订正数据流实测默认不跑）不在此正文展开。

## 5.5 降级清单（诚实标注）

| # | 位置 | 降级内容 | 理由 / 边界 |
|---|---|---|---|
| 1 | define_loop / find_duplicates | node_fingerprint 是工程启发式哈希，借 §2.4.4 等价类命名，只粗覆盖判据 (b)，(a)/(a')/(c) 未覆盖；非 §2.4.4 判据实现 | 精确实现 §2.4.4 三判据开销极大；指纹是可接受的保守近似；边界已明 |
| 2 | stuck_region | 注意力信号非判决，纯结构不能区分 debug loop 与复杂开发 | 诚实定位，不冒充功能判断 |
| 3 | stuck_region resolve | 「区域稳定」是行为停止推断 | 可能假阳性，代价可接受 |
| 4 | debug_loop（仅启发档） | 无覆盖率数据时 testcase↔define 用路径/名称启发，易归错 | 有覆盖率则升精确档（无此降级）；覆盖率是可选增强，标准产物，接入成本同 junit xml |
| 5 | debug_loop | 需 junit xml 配置（opt-in 前提） | 无则退 stuck_region |
| 6 | task_scope_breach | 范围约束是工程约束非 SDT 定理 | SDT 给词汇，约束是工程要求 |
| 7 | task_scope_breach | 依赖声明非自动派生 | §1.5 唯一例外，不声明退 boundary |
| 8 | 根因方向 | 结构 + 时序启发，不保证命中根因 | 措辞「方向」非「结论」 |
| 9 | 同类传播 | 同类不一定同 bug | 措辞「可能」 |
| 10 | 横向关系输入·跨 define 边（默认全语言） | 默认全语言（含 TS）走调用图近似（调用 ≠ 消耗，有假边） | 调用图假边 + 动态调用漏；TS 亦如此（实测 got 默认 ingest：168 条全 callgraph，0 条 dataflow） |
| 11 | 联合约束 $\sigma$（不做） | SDT 有此运算（Math Ext §11.1d），BUER 不实现 | 依赖 $G$（语义理解）；守「不理解语义」定位；debug 回溯用 $\omega$+时序+调用已够，$\sigma$ 精度过剩且引入语义猜测 |
| 12 | 横向关系输入·跨 define 边·数据流档（按需） | 数据流（对位 GD G3）仅 `analyze_dangling` 按需调用；需 TS 工具链（tsconfig + node_modules/typescript + node）齐全；缺失则 degrade 回调用图 | 实测 ~4s/调用（每次重载 TS program），~450 倍代价；默认走调用图是合理性能权衡；数据流是深挖根因时按需精度增强，非默认主体 |

## 5.6 防漂移验证记录

本文档每个 SDT 依据都对原文逐条验证（非凭记忆）。验证记录：

| 锚点 | SDT 原文位置 | 验证结论 |
|---|---|---|
| software project 是合法 kind | DI Lemma 3 | ✓ 原文明列 software processes |
| 砍 Bio signature 的根据 | DI line 175 | ✓ 原文明说 Bio signature 不适用 software process |
| DI Main Theorem 类比定位 | DI Main Theorem | 引用准确（原文三 case 内容已核）；应用降为类比——BUER 不构造正向 signature、不严格实例化三 case，详见 §5.1 |
| 前驱重叠 $\omega$ | Math Ext §11.1b | ✓ $\omega(u,v)=\lvert\downarrow u\cap\downarrow v\rvert$，反链上共享祖先 |
| $\omega$ 不可传递 | Math Ext Thm 11.1f.2 | ✓ 自反对称非传递（反例 a1/a2/a3 构造） |
| $\Gamma_R$ 关联 + 传递 | GD §7.1, Prop 7.2 | ✓ 共享下游汇合，可传递 |
| 连通分量因果独立 | GD §9.2 | ✓ 不同分量无 ≺、无共享产物/元素 |
| 反链 = 空间切片 | Math Ext §11.1a | ✓ ≺-incomparable 节点 |
| 联合约束 $\sigma$ 依赖 G | Math Ext §11.1d | ✓ 确认依赖 $G_{\text{co}}$，BUER 不做（语义） |
| 过程间数据流对位 GD G3 | GD G3（line 86） | ✓ 数据流「A 产物流进 B 并被用」精确命中 $\rho(u)\cap E_v\neq\emptyset$；按需（analyze_dangling + TS 工具链）时对位严格；**非默认主体** |
| 调用图 SDT 地位 | GD G3 / §2.2 C 关系 | 调用图非 G3 非纯 C，**默认全语言**作跨 define 边近似（降级）；数据流是按需补充，不替代默认路径 |
| 产物函数 / R-member | GD §1（line 77） | ✓ $\rho: V\to\mathcal{P}_{\text{fin}}(\mathcal{S})$ |
| 任意尺度 R-member | SDT §2.1.2（line 241） | ✓ 「members of $E$ may be of any scale」（修正：早期误引为 §2.1.3，§2.1.2 才对） |
| 结构层等价三判据 | SDT §2.4.4（line 508） | ✓ 三判据 (a)(a')(b)(c) |
| Jaccard 距离 | Math Ext §12.1.2（line 1833） | ✓ 公式核对一致 |
| 偏序良基 | GD WF（line 102） | ✓ 良基，depth $d(v)$ |
| C 关系非消耗多对多 | SDT §2.2（line 245） | ✓ 分析对象非排他 |

**一处引用修正**：早期版本把「任意尺度 R-member」引为 SDT §2.1.3，验证发现 §2.1.3 是 possible configurations $P$，任意尺度 R-member 实际在 §2.1.2（Element $E$）。本文档已用 §2.1.2。

**与 SDT 文档无冲突，零漂移。**

---

# 附：与前版本的关系

v2.0 是 v1.1.r8 的 B 方案干净重组。r1-r7 的演进历史（框架三次重定位、各版本修订记录）不进设计正文。v2.0 只描述当前设计。

核心定位：BUER 是 software project kind 的 entity theory（纯 DI/SDT），用结构数据两面帮 vibe coder + agent：**报警**（事后检测 agent 打转/越界/改坏，先提醒 agent 自纠、再升级用户）+ **辅助**（事中帮框定：补测试、预览影响域、提交时机、安全网预警）。不验证功能正确、不给解决方案、不判项目方向；辅助守「建议非决定、结构非语义、可拒绝」三边界。

# 采样点删除与长窗口插值分析：增量索引设计（讨论文档，不含实现）

本文件只做设计论证，不实现任何增量分析器。数值口径的唯一可执行参照是全量扫描实现 `analysis_ref.py:11`；物理快照事务的真实入口在 `lake.py:27`、`lake.py:56`、`lake.py:68` 与 `catalog.py:22`。所有结论都能用仓库现有命令重跑（见第 10 节）。

## 1. 问题与设计目标

删除坏点后，长窗口统计不能再遍历整站采样。现有查询 `analyse` 每次都 `lake.read()` 取当前 head 的全部可见行（`analysis_ref.py:14`），按 `(t, id)` 排序（`analysis_ref.py:15`-`analysis_ref.py:17`），同站同刻只保留排序后首个即最小 id 行作为胜者（`analysis_ref.py:19`-`analysis_ref.py:21`），再逐对相邻胜者判断 `valid` 且间隔 `gap <= G=10`（`analysis_ref.py:23`-`analysis_ref.py:26`），对窗口 `[L,R)` 做半开截断与精确分式积分（`analysis_ref.py:27`-`analysis_ref.py:34`）。

设计目标（边界见 `CONTRACT.md` 第 41-46 行）：

1. 首次启用可全量建索引；此后单点删除与物理重写的维护只定位**同刻候选、时间邻点和必要索引路径**，不重新读全部可见行、不整站重排。
2. 窗口查询只做索引 seek、前缀聚合与常数条边界边截断，**不枚举窗口内全部采样点或片段**，也不用后台全量重建或逐时刻网格代替。
3. 重写只改变物理位置到逻辑 id 的映射；逻辑候选、胜者与片段不重复入账、不被撤回。
4. 分析索引、物理映射、分析绑定的 head 与目录快照在**同一 SQLite 事务**生效；事务内可读己写，外层回滚后随 `catalog.py:31`-`catalog.py:33` 的 ROLLBACK 一并恢复；重复删除不多计；`PUBLISH_CONFLICT`、`STALE_LAYOUT` 不留分析副作用。
5. 不新增采样、不做并发恢复、不建历史分析索引；`Lake.read` 历史路径（`lake.py:18`-`lake.py:25`）保持原样。

## 2. 现有源码中的接入点（保留原样，仅标注钩子位置）

| 现有行为 | 源码位置 | 分析层挂钩（拟议，不改造原结构与锁范围） |
| --- | --- | --- |
| seed 写对象并提交快照 1 | `lake.py:10`-`lake.py:16`；提交 SQL 在 `catalog.py:48`-`catalog.py:58` | 提交前全量构建一次分析索引，与快照同事务 |
| 删除：源快照校验 | `lake.py:28`-`lake.py:30` | 不优化，原有 `Objects.get` 校验读保留，属原业务成本 |
| 删除：墓碑判断与幂等返回 | `lake.py:32`-`lake.py:37` | 进入写事务后挂局部维护；已墓碑则分析层零写入 |
| prepare：读输入载荷、过滤墓碑、写重写文件 | `lake.py:43`-`lake.py:53`；不可变写入见 `objects.py:13`-`objects.py:28` | 复用已读载荷与已组输出行，在返回的计划里暂存 物理位置→逻辑id 对应，不分析、不发布 |
| publish：head 冲突检查与快照替换 | `lake.py:57`-`lake.py:66` | 同事务只提交物理映射；冲突在 `lake.py:59` 抛出，整事务回滚，分析零副作用 |
| rewrite：嵌套写事务封装 | `lake.py:68`-`lake.py:71`；嵌套 BEGIN 规则在 `catalog.py:23`-`catalog.py:26` | 钩子自动加入外层事务，锁范围不变 |
| write 上下文 | `catalog.py:22`-`catalog.py:34` | 分析 DML 与目录 DML 共用同一连接与同一 BEGIN/COMMIT/ROLLBACK |
| 全量参照查询 | `analysis_ref.py:11`-`analysis_ref.py:37` | 签名与返回结构不变；拟议查询保持 `(lake, station, L, R)` |
| 窗口合法性 | `analysis_ref.py:12`-`analysis_ref.py:13` | 同样先抛 `ValueError("BAD_WINDOW")`，不触碰任何索引 |
| 对象读取 | `objects.py:30`-`objects.py:32` | 索引维护不再调用它读无关载荷；物理映射建立后删除路径零载荷读 |

`catalog.commit` 强制处于写事务中（`catalog.py:49`-`catalog.py:50`），因此分析表的写入天然只能在同一事务里完成；`Catalog.write` 对嵌套调用只加入外层（`catalog.py:24`-`catalog.py:26`），异常由最外层统一回滚（`catalog.py:31`-`catalog.py:34`）。这满足“同事务生效、事务内可读、外层回滚恢复”的全部要求，无需新锁。

## 3. 逻辑模型：候选、胜者、边与片段

严格复刻参照口径：

- **候选**：一个采样行，由全局唯一正整数 `id` 标识（字段契约见 `CONTRACT.md` 第 30 行）。物理位置 `(fid, pos)` 只是它在某个快照下的存放点；重写移动位置不改变 `id`。
- **同刻候选组**：键为 `(station, t)`，组内按 `id` 升序。**先在当前可见候选中选最小 id 胜者，再判断该胜者的 `valid`**；无效胜者是断点，不能跳过它另取有效候选（`CONTRACT.md` 第 31 行；参照实现以排序+取首条体现，`analysis_ref.py:19`-`analysis_ref.py:21`）。
- **边**：同站时间序相邻的两个胜者 `a → b`，仅当两边均 `valid` 且 `b.t - a.t <= G` 时成立。边只覆盖时间区间 `[a.t, b.t)`，不向外推。
- **片段**：本设计把一条成立的边称为一个可插值片段；胜者链上不成立的邻接是空隙。
- **窗口聚合**：对 `[L,R)` 半开截断。边区间 `[a.t,b.t)` 与窗口取交 `[max(L,a.t), min(R,b.t))`，`lo < hi` 才计入；梯形面积 `(ylo+yhi)*(hi-lo)/2`，斜率与端值都用 `Fraction` 精确计算（对照 `analysis_ref.py:27`-`analysis_ref.py:34`）。`covered` 是整数时长，`integral` 为约分分式，`mean=integral/covered`，零覆盖为 `null`（`analysis_ref.py:35`-`analysis_ref.py:37`）。

关键单调性：方案边界内不新增采样（`CONTRACT.md` 第 43 行），所以 `(station, t, id)` 的全集在首次构建后**静态不变**；删除只翻转候选可见性，重写只改物理映射。这让静态序号、静态候选组和静态边位都可以在首次构建时固定下来。

## 4. 方案 A（推荐）：逻辑 id 映射 + 同刻组 + 胜者链邻点索引 + Fenwick 片段聚合

### 4.1 持久状态（新建分析表，与目录同库同事务）

```text
ax_meta(singleton PK, head_sid)                 # 分析绑定的 head，随每次 commit 同事务更新
ax_sample(id PK, station, t, reading, valid)    # 静态逻辑采样，首次构建写入，之后不改
ax_phys(id PK, fid, pos)                        # id 的当前物理位置；重写只改这张表
ax_cand(station, t, rank, id PK)                # (station,t) 组内按 id 排序的静态序号与成员
ax_alive(id PK, alive INTEGER)                  # 可见位；删除=0，同事务随墓碑更新
ax_winner(station, t PK, id)                    # 该时刻当前可见最小 id 胜者；无可见行则删行
ax_edge(lo_rank PK, hi_rank, active INTEGER,
        length, covered_full, int_num, int_den)# 胜者链邻边（按站点内时间秩）；active=成立
ax_bit(station, bucket PK, cov, num, den)       # Fenwick：仅装 active 边的全长覆盖与积分
```

- `rank`：站点内按 `t` 排序后的静态时间秩（同刻候选共享一个时刻槽）。构建期一次排定，删除不改秩，邻点 seek 即对秩做前驱/后继查询。
- 分式存储：`ax_edge`/`ax_bit` 各桶始终存**约分后**的 `num/den`；参照实现中所有项分母整除 `2*gap` 且 `gap<=10`，截断项分母不超过 20，桶分母有界，更新时用 `Fraction` 合并后再落库，杜绝浮点。
- `ax_phys` 让删除从物理位置到逻辑 id 是一次主键 seek，从而删除路径不再需要 `Objects.get` 读载荷（原业务的校验读仍在 `lake.py:29`，单列成本）。

### 4.2 首次全量构建（唯一允许的全量遍历）

伪代码，整体包在 seed 的同一个 `catalog.write()` 内：

```text
BUILD(lake):
    view = lake.read()                      # 唯一一次全量读，等价参照的 analysis_ref.py:14
    rows = 排序(view.rows, station, t, id)  # 复刻 analysis_ref.py:15-17
    写 ax_sample / ax_phys（位置取 row.file,row.pos）
    按 (station,t) 分组：写 ax_cand(rank, id)，组首可见者写 ax_winner
    for 每站按 t 相邻胜者 a,b：
        写 ax_edge 行（active 初始按 valid 与 gap<=10 判定）
    对所有 active 边做 Fenwick 点更新，写 ax_bit
    写 ax_meta.head_sid = 1
    # 不自行 COMMIT；由外层 lake.seed 的 catalog.commit(catalog.py:48) 一起提交
```

### 4.3 单点删除的局部维护

```text
ON_DELETE(fid, pos):                        # 已在 lake.py:31 的写事务内、已通过原校验
    id = ax_phys.seek(fid, pos)             # 一次主键 seek
    if ax_alive[id] == 0: return            # 幂等：对应 lake.py:35-36，分析零写入
    组 = ax_cand 中同 (station,t) 的全部候选  # k=同刻候选数，静态小集合
    ax_alive[id] = 0
    旧胜者 = ax_winner(station,t)
    新胜者 = 组内仍 alive 的最小 id（无则该时刻无胜者）
    受影响时刻集合 T = {t} ∪ {t 在胜者链上的时间前驱, 时间后继}
    撤回 T 内部旧邻边（ax_edge.active=0；Fenwick 点更新减去旧全长值）
    改 ax_winner（换胜者或删行）
    在新胜者链上重连 T 内部邻接，按 valid 与 gap<=10 建新边/激活边，Fenwick 点更新加回
    # 同事务随后执行 catalog.commit(catalog.py:48-58)；异常则 catalog.py:31-33 整体回滚
```

三种删除形态都被覆盖，且与契约第 31 行一致：

1. **删胜者、同刻还有候选**：最小 id 胜者换成下一候选（ties 中删 id20 显露 id30）。只重算该时刻与其前后时刻的至多 3 条邻边。
2. **删胜者、同刻无候选**：时刻槽消失，前后时刻变为新相邻（bridge 中删无效 id60 后 t=12 与 t=20 接通；回滚步中删 id30 后 t=0 与 t=10 间隔恰为 10 仍接通）。
3. **删非胜者**：只翻 `ax_alive`，`ax_winner` 不变、边不变（bridge 第二步删隐藏候选 id30，数值保持 110）。

重复删除在第 2 行即返回，保证不多计。

### 4.4 重写：只维护物理映射

`prepare`（`lake.py:39`-`lake.py:54`）已经读入全部输入载荷（`lake.py:43`）并按 layout 组好输出行（`lake.py:51`）。分析层不额外 `Objects.get`：

```text
ON_PREPARE(plan, layout, dead):
    plan.phys_staging = [(输出fid, 新pos, 逻辑id) for 每个输出行]
        # 逻辑 id 直接取自 lake.py:51 已构造的行对象；墓碑行在该处已被过滤
    # 不写任何分析表：prepare 未发布，分析不变（CONTRACT.md 第 46 行）

ON_PUBLISH(plan):                           # 在 lake.py:57 的写事务内
    if head != plan.base: raise PUBLISH_CONFLICT   # lake.py:59，本事务稍后整体回滚
    批量 UPSERT ax_phys 的 m 条新映射（m=本次重写存活行数）
    ax_sample / ax_cand / ax_winner / ax_edge / ax_bit 全部零写入
    ax_meta.head_sid = 新 sid               # 与 catalog.commit 的 head 更新同事务
```

纯位置重排因此是 O(m) 的映射写入，不碰任何片段；外层回滚时这批 `ax_phys` 行随 ROLLBACK 撤销（layout 的 rollback_publish 步实测事务内可见、回滚后恢复）。历史 `ax_phys` 不做版本化——历史分析本就不在范围内，历史 `Lake.read` 照旧（`lake.py:18`）。

### 4.5 查询：前缀聚合 + 常数条边界截断，不枚举内部片段

```text
QUERY(station, L, R):
    if 非整数或 L>R: raise ValueError("BAD_WINDOW")   # 对齐 analysis_ref.py:12
    pL, pR = 对 (station,t) 索引做两次 seek：
             首个 t>=L 的时刻秩、首个 t>=R 的时刻秩
    if 站点不存在或 pR-pL < 1: 返回零覆盖、[0,1]、null
    # 内部完整边：秩区间 [pL, pR) 内、且整条边落在 [L,R) 的边，用 Fenwick 两个前缀之差
    cov0, I0 = BIT.prefix(pR-1) - BIT.prefix(pL)     # O(log p)，不读任何单条边
    # 至多两条被窗口边界切开的边，单独精确截断
    for 边界边 e in {pL-1 处入边, pR-1 处出边}（去重、存在且 active）:
        lo=max(L,e.a.t); hi=min(R,e.b.t)
        if lo<hi: 用 Fraction 算梯形面积，累加 cov、I     # 公式同 analysis_ref.py:30-34
    返回 snapshot=ax_meta.head_sid, covered=cov,
         integral=约分(I), mean=约分(I/cov) 或 null
```

长窗口内可能跨过上千条片段，但内部片段的总量由 Fenwick 前缀在 O(log p) 内合出，只访问 O(log p) 个桶行；真正按公式计算的只有至多两条边界边。这就是“不枚举窗内全部片段”的落点。空窗与缺站由两次 seek 落空直接判定（gap 样例的三个零覆盖窗口）。

## 5. 方案 B（可行但落选）：时间秩段树（canonical 覆盖）+ 同构候选表

状态与方案 A 的 `ax_sample/ax_phys/ax_cand/ax_alive/ax_winner/ax_edge` 完全相同，区别只在片段聚合结构：

- 用每站一棵静态段树代替 Fenwick。叶子是胜者链的边槽，节点存该区间内 active 边的全长 `covered` 与积分分式和。
- 边激活/失效是叶到根的 O(log p) 点更新。
- 窗口 `[pL,pR)` 的内部完整边分解成 O(log p) 个 canonical 节点，直接取节点和，再同样对至多两条边界边做 Fraction 截断。

段树同样满足全部访问边界：维护只走同刻组、邻点与一条根路径；查询不枚举内部片段。与方案 A 的差异仅在成本常数与写入放大，见第 7 节。

### 为什么排除其他组织

- **逐时刻网格（按 t 建桶数组）**：被 `CONTRACT.md` 第 44 行明确禁止，且 t 为 64 位整数，空间不可行。
- **只建 `(station,t)` 二级索引、查询仍扫窗内胜者行**：查询代价随窗内点数增长，违反“不得枚举窗口内全部采样点”。
- **块/根号分解（sqrt decomposition）**：窗内完整块仍要逐块相加，块数随窗口线性增长，本质是受限枚举，不满足长窗口访问边界。
- **后台全量重建**：契约第 44 行禁止，且删除后无法保证同事务读己写。

## 6. 事务、可见性与冲突的伪代码总览

所有分析 DML 与目录 DML 共用 `Catalog.db` 连接，沿用 `catalog.py:22`-`catalog.py:34` 的嵌套规则：

```text
删除（lake.delete → catalog.write，lake.py:31）
  BEGIN IMMEDIATE（或加入外层）
    catalog.state → 原校验（lake.py:32-35）
    ON_DELETE 的全部 ax_* 变更
    catalog.commit：snapshots/files/tombstones/head（catalog.py:53-57）
  COMMIT                                        # 索引与墓碑一起生效
  任一步异常 → ROLLBACK：ax_* 与目录同时恢复（STALE_LAYOUT、观察器回滚均如此）

重写（lake.rewrite → 外层 write，lake.py:69）
  BEGIN IMMEDIATE
    prepare（含 phys_staging，不发布分析变更）
    publish 检查 head==base（lake.py:59）
      冲突 → 异常逃出 → ROLLBACK，暂存映射与目录都不留痕（PUBLISH_CONFLICT）
      通过 → ax_phys 批量映射 + catalog.commit + ax_meta.head 同事务
  COMMIT

事务内查询
  同一连接上 QUERY 立即读到本事务未提交的 ax_*（SQLite 读己写），
  对照 ties/layout 的 inside 答案 snapshot 已推进而外层 head 回滚后复原
```

幂等与冲突的对应关系：重复删除命中 `lake.py:35` 的墓碑分支，分析层在 `ON_DELETE` 第 2 行返回；`STALE_LAYOUT`（`lake.py:33`-`lake.py:34`）与 `PUBLISH_CONFLICT`（`lake.py:59`）都在分析钩子生效前或随事务回滚撤销，实测冲突后答案保持冲突前数值（见第 8 节 conflict/ties 录制）。

## 7. 访问与空间成本（原业务成本与新增分析成本分列）

记号：`p`=该站点时刻（胜者槽）数，`k`=该时刻同刻候选数，`m`=本次重写存活行数，`N`=全库可见采样行数，`S`=站点数，`e`=站点当前 active 边数（`e < p`）。

### 7.1 新增分析层成本

| 操作 | 访问（索引行级） | 写放大 | 空间 |
| --- | --- | --- | --- |
| 首次构建 | 全量读 N 行一次（唯一豁免） | O(N log p) 次桶/节点写 | 两方案均 O(N)：sample/phys/cand/alive/winner 各一行级；A 的 BIT 每站 O(p)，B 的段树每站约 2·2^⌈log p⌉ |
| 单点删除 | 1 次 phys seek + 读 k 个候选 + O(1) 次邻点 seek + O(1) 条边维护 | A：每条边 O(log p) 个 BIT 桶，常数条边 → O(log p)；B：每条边一条根路径 O(log p) | 仅 alive/winner/edge/bit 原地更新，O(1) 行 |
| 重写发布 | 零载荷读；m 次 ax_phys UPSERT，winner/edge/bit 零访问 | O(m) | 每存活行一版当前映射，总量仍 O(N) |
| 窗口查询 | 2 次时刻 seek；A 为 2 个 Fenwick 前缀即 O(log p) 桶读，B 为 O(log p) 个 canonical 节点；另读至多 2 条边界边做 Fraction 截断 | 只读 | 不随窗口增长 |

分式桶的分母有界（边项分母整除 `2·gap ≤ 20`），每次合并是有界整数运算；64 位输入下积分分子可能扩位，按 Python 大整数/Fraction 存储，参照实现本身也不压在 64 位内。

### 7.2 必须单列、未被消除的原业务成本

- `Lake.delete` 的源快照 `catalog.state`（`lake.py:28`、`catalog.py:39`-`catalog.py:46`）与 `lake.py:29` 的 `Objects.get` 位置校验：每次删除仍读该文件整份载荷，录制 `operation_gets` 中即体现为 `a:4`、`b:5`。
- `prepare` 为布局校验必须读全部输入载荷（`lake.py:43`-`lake.py:47`），录制为 `a:4 + b:5`；分析层只是搭车，不新增读取。
- 历史 `Lake.read` 仍按文件枚举并过滤墓碑（`lake.py:21`-`lake.py:24`），不在优化范围。
- 参照查询 `analysis_ref.py:14` 的全量读只存在于观察器与回归中；上线后拟议查询不再产生这类 `query_gets`。录制里的计数是**全扫描实测**，第 8 节 access 文字给的是**方案 A 的预测访问**，两者按契约第 52、56 行分开标注。

### 7.3 方案 A 与 B 的选型

- 空间：A 每站恰好 p 个 BIT 桶；B 需约 2 的幂补齐，约 2p–4p 个节点，且每个节点都存一组分式和。A 省约 2–4 倍。
- 写入：删除改常数条边，A 每条边触碰 log p 桶、B 每条边写一条根路径（同样 log p 节点但节点更宽、分式合并更多）。A 的删除写放大更小。
- 查询：A 用两个前缀之差，聚合读 2·log p；B 是 O(log p) 个 canonical 节点（约 2 log p），常数相近；两者边界截断完全相同。
- 工程：BIT 只有一张 `(station,bucket)` 表、UPSERT 语义简单，和“半开区间 = 两前缀之差”的窗口模型直接同构；段树需要维护静态树形与节点区间元数据。

**选定方案 A**：在同样满足访问边界的前提下空间更省、删除写入更小、表结构更少；段树仅在将来需要任意区间合并其他可逆聚合时才有优势，当前需求不需要。

## 8. 与录制实测的逐项核对（含 scale 长窗口）

第 9 节证据块中每个 case 的 `actual` 是 `analysis_probe.run(sample(name))` 的完整原始步骤（观察器实现见 `analysis_probe.py:26`-`analysis_probe.py:86`），`predicted` 与它同步数、同 op/head/answers/inside，另附 `affected`（逻辑候选与片段变化）和 `access`（方案 A 的预测路径与原因）。核对要点：

- **ties**：删 id20 后积分 54→24、`[2,8)` 积分变 `[84,5]`，与契约第 37 行手算锚点一致；回滚步 `inside` snapshot=3 而回滚后 head 复原为 2，证明事务内可读己写、ROLLBACK 恢复；提交删除 id30 后 `[4,1]`、`[1,3]`，重复删除步 head 不增、数值不变。
- **bridge**：删无效胜者 id60 使覆盖 12→20、积分 110，接通的是 t=12→t=20（gap=8）；随后删非胜者 id30 数值不动，印证“先选胜者再判 valid、非胜者删除不改链”。B 站始终 `10/[1000,1]/[100,1]`。
- **layout**：prepare 后 head=1 且数值不变；回滚发布的 `inside` 与回滚后答案同为 54（纯物理映射切换不动逻辑片段）；旧布局删除报 STALE_LAYOUT 且答案不变；按新位置 `rewrite-r-0:1` 删除经 `ax_phys` 解析到 id20，得到与 ties 完全相同的 24；history 仍读快照 1 的 9 行，分析层零介入。
- **conflict**：prepare 后删除使 head 推进到 2，publish 报 PUBLISH_CONFLICT 并回滚，最终答案保持 24，暂存映射不多计。
- **gap**：t=10→t=21 间隔 11>G 不成边；`[5,21)` 得 `5/[75,2]/[15,2]`，`[10,21)`、空窗、缺站全部零覆盖 null，对应查询 seek 落空与边界截断路径。
- **scale（不进证据块，单独说明）**：`analysis_probe.py scale` 的实测为窗口 `[-10^6,10^6)` 答案 `covered=10, integral=[0,1], mean=[0,1]`，而参照的 `query_gets` 合计读取 **4098 个物理行**（main 2 + noise 4096，断言在 `analysis_check.py:76`-`analysis_check.py:78`）。方案 A 下该查询只对站点 A 做两次时刻 seek 与两个 BIT 前缀：4096 条 Z 站记录落在别的 `station` 键空间，**没有任何索引路径会访问它们的载荷或桶**；A 站内部无论多少完整边都不逐条读出。这就是推荐索引对“大量无关点 + 长窗口”的访问保证。

## 9. 嵌入证据（重跑命令见第 10 节；actual 为原始录制，计数是全扫描观察而非索引成本）

<!-- interpolation-evidence -->
~~~json
{
  "ties": {
    "actual": [
      {
        "op": "seed",
        "snapshot": 1,
        "operation_gets": [],
        "transactions": [
          "BEGIN IMMEDIATE",
          "COMMIT"
        ],
        "head": 1,
        "answers": [
          {
            "snapshot": 1,
            "covered": 12,
            "integral": [
              54,
              1
            ],
            "mean": [
              9,
              2
            ]
          },
          {
            "snapshot": 1,
            "covered": 6,
            "integral": [
              42,
              1
            ],
            "mean": [
              7,
              1
            ]
          },
          {
            "snapshot": 1,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "query_gets": [
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          }
        ]
      },
      {
        "op": "delete",
        "snapshot": 2,
        "operation_gets": [
          {
            "file": "a",
            "rows": 4
          }
        ],
        "transactions": [
          "BEGIN IMMEDIATE",
          "COMMIT"
        ],
        "head": 2,
        "answers": [
          {
            "snapshot": 2,
            "covered": 12,
            "integral": [
              24,
              1
            ],
            "mean": [
              2,
              1
            ]
          },
          {
            "snapshot": 2,
            "covered": 6,
            "integral": [
              84,
              5
            ],
            "mean": [
              14,
              5
            ]
          },
          {
            "snapshot": 2,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "query_gets": [
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          }
        ]
      },
      {
        "op": "delete",
        "snapshot": 2,
        "operation_gets": [
          {
            "file": "a",
            "rows": 4
          }
        ],
        "transactions": [
          "BEGIN IMMEDIATE",
          "COMMIT"
        ],
        "head": 2,
        "answers": [
          {
            "snapshot": 2,
            "covered": 12,
            "integral": [
              24,
              1
            ],
            "mean": [
              2,
              1
            ]
          },
          {
            "snapshot": 2,
            "covered": 6,
            "integral": [
              84,
              5
            ],
            "mean": [
              14,
              5
            ]
          },
          {
            "snapshot": 2,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "query_gets": [
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          }
        ]
      },
      {
        "op": "rollback_delete",
        "inside": [
          {
            "snapshot": 3,
            "covered": 12,
            "integral": [
              4,
              1
            ],
            "mean": [
              1,
              3
            ]
          },
          {
            "snapshot": 3,
            "covered": 6,
            "integral": [
              0,
              1
            ],
            "mean": [
              0,
              1
            ]
          },
          {
            "snapshot": 3,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "error": "OBSERVER_ROLLBACK",
        "operation_gets": [
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          }
        ],
        "transactions": [
          "BEGIN IMMEDIATE",
          "ROLLBACK"
        ],
        "head": 2,
        "answers": [
          {
            "snapshot": 2,
            "covered": 12,
            "integral": [
              24,
              1
            ],
            "mean": [
              2,
              1
            ]
          },
          {
            "snapshot": 2,
            "covered": 6,
            "integral": [
              84,
              5
            ],
            "mean": [
              14,
              5
            ]
          },
          {
            "snapshot": 2,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "query_gets": [
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          }
        ]
      },
      {
        "op": "delete",
        "snapshot": 3,
        "operation_gets": [
          {
            "file": "b",
            "rows": 5
          }
        ],
        "transactions": [
          "BEGIN IMMEDIATE",
          "COMMIT"
        ],
        "head": 3,
        "answers": [
          {
            "snapshot": 3,
            "covered": 12,
            "integral": [
              4,
              1
            ],
            "mean": [
              1,
              3
            ]
          },
          {
            "snapshot": 3,
            "covered": 6,
            "integral": [
              0,
              1
            ],
            "mean": [
              0,
              1
            ]
          },
          {
            "snapshot": 3,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "query_gets": [
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          }
        ]
      }
    ],
    "predicted": [
      {
        "op": "seed",
        "head": 1,
        "answers": [
          {
            "snapshot": 1,
            "covered": 12,
            "integral": [
              54,
              1
            ],
            "mean": [
              9,
              2
            ]
          },
          {
            "snapshot": 1,
            "covered": 6,
            "integral": [
              42,
              1
            ],
            "mean": [
              7,
              1
            ]
          },
          {
            "snapshot": 1,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "affected": "一次性全量建站：A 站六个时刻胜者（t=0/5/10/12/15/20，t=5 由 id20 胜 id30）、三条有效边 0-5、5-10、10-12；B 站两点一边。",
        "access": "首次允许的全量构建：读全部 9 个可见物理行一次，写 ax_sample/ax_phys/ax_cand/ax_winner 与 BIT；之后查询不再读载荷。录制里的 query_gets 是全量参照 analysis_ref.py:14 的成本，须与索引访问分开标注。"
      },
      {
        "op": "delete",
        "head": 2,
        "answers": [
          {
            "snapshot": 2,
            "covered": 12,
            "integral": [
              24,
              1
            ],
            "mean": [
              2,
              1
            ]
          },
          {
            "snapshot": 2,
            "covered": 6,
            "integral": [
              84,
              5
            ],
            "mean": [
              14,
              5
            ]
          },
          {
            "snapshot": 2,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "affected": "删除胜者 id20（a:1）：同刻 id30 显露为 t=5 新胜者（先选最小 id 再判 valid 的口径不变）；邻边 0-5、5-10 用新读数 4 重算，10-12 不变，片段数仍为 3，积分 54→24。",
        "access": "ax_phys 一次定位 (a,1)→id20；读同刻候选组 k=2 与 t=0、t=10 两个时间邻点；仅两条边的 BIT 桶做 O(log p) 更新，不枚举 A 站其他点。operation_gets 的 a:4 是 lake.py:29 原有位置校验读，属原业务成本。"
      },
      {
        "op": "delete",
        "head": 2,
        "answers": [
          {
            "snapshot": 2,
            "covered": 12,
            "integral": [
              24,
              1
            ],
            "mean": [
              2,
              1
            ]
          },
          {
            "snapshot": 2,
            "covered": 6,
            "integral": [
              84,
              5
            ],
            "mean": [
              14,
              5
            ]
          },
          {
            "snapshot": 2,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "affected": "重复删除同一墓碑：lake.py:35-36 直接返回当前 head，无新快照；id20 已死，同刻组、胜者与三条边均无变化，数值保持 24。",
        "access": "ax_phys 命中后查 alive 位即返回，分析层零写入、BIT 零更新；录制的 a:4 仍只是 lake.py:29 的原业务校验载荷读。"
      },
      {
        "op": "rollback_delete",
        "head": 2,
        "answers": [
          {
            "snapshot": 2,
            "covered": 12,
            "integral": [
              24,
              1
            ],
            "mean": [
              2,
              1
            ]
          },
          {
            "snapshot": 2,
            "covered": 6,
            "integral": [
              84,
              5
            ],
            "mean": [
              14,
              5
            ]
          },
          {
            "snapshot": 2,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "inside": [
          {
            "snapshot": 3,
            "covered": 12,
            "integral": [
              4,
              1
            ],
            "mean": [
              1,
              3
            ]
          },
          {
            "snapshot": 3,
            "covered": 6,
            "integral": [
              0,
              1
            ],
            "mean": [
              0,
              1
            ]
          },
          {
            "snapshot": 3,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "affected": "外层事务内删除 t=5 现任胜者 id30：同刻再无候选，t=5 胜者节点移除，t=0 与 t=10 变为相邻（间隔=10 仍可插值），旧两边撤回、新边 0-10 建立，片段 3→2；ROLLBACK 后 t=5 胜者与原三条边整体恢复为提交态。",
        "access": "事务内沿同一连接读到新边（读己写）：同刻 k=1、两次邻点 seek、三条边级联 O(log p) BIT 更新；catalog.py:31-33 的 ROLLBACK 让 ax_* 行随目录一起恢复。录制 operation_gets 含参照查询的全扫描读，不是索引路径成本。"
      },
      {
        "op": "delete",
        "head": 3,
        "answers": [
          {
            "snapshot": 3,
            "covered": 12,
            "integral": [
              4,
              1
            ],
            "mean": [
              1,
              3
            ]
          },
          {
            "snapshot": 3,
            "covered": 6,
            "integral": [
              0,
              1
            ],
            "mean": [
              0,
              1
            ]
          },
          {
            "snapshot": 3,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "affected": "提交与回滚内相同的逻辑变更：id30 删除生效，t=5 无胜者、边 0-10 与 10-12 留存，积分 [4,1]；重复删除不会再次入账。",
        "access": "ax_phys 定位 (b,0)→id30 后仅走一次同刻/邻点局部维护，BIT 变更路径与上一步事务内完全相同但随 COMMIT 持久化；B 站键空间不受影响，故 B 答案始终为 1000。"
      }
    ]
  },
  "bridge": {
    "actual": [
      {
        "op": "seed",
        "snapshot": 1,
        "operation_gets": [],
        "transactions": [
          "BEGIN IMMEDIATE",
          "COMMIT"
        ],
        "head": 1,
        "answers": [
          {
            "snapshot": 1,
            "covered": 12,
            "integral": [
              54,
              1
            ],
            "mean": [
              9,
              2
            ]
          },
          {
            "snapshot": 1,
            "covered": 6,
            "integral": [
              42,
              1
            ],
            "mean": [
              7,
              1
            ]
          },
          {
            "snapshot": 1,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "query_gets": [
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          }
        ]
      },
      {
        "op": "delete",
        "snapshot": 2,
        "operation_gets": [
          {
            "file": "a",
            "rows": 4
          }
        ],
        "transactions": [
          "BEGIN IMMEDIATE",
          "COMMIT"
        ],
        "head": 2,
        "answers": [
          {
            "snapshot": 2,
            "covered": 20,
            "integral": [
              110,
              1
            ],
            "mean": [
              11,
              2
            ]
          },
          {
            "snapshot": 2,
            "covered": 6,
            "integral": [
              42,
              1
            ],
            "mean": [
              7,
              1
            ]
          },
          {
            "snapshot": 2,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "query_gets": [
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          }
        ]
      },
      {
        "op": "delete",
        "snapshot": 3,
        "operation_gets": [
          {
            "file": "b",
            "rows": 5
          }
        ],
        "transactions": [
          "BEGIN IMMEDIATE",
          "COMMIT"
        ],
        "head": 3,
        "answers": [
          {
            "snapshot": 3,
            "covered": 20,
            "integral": [
              110,
              1
            ],
            "mean": [
              11,
              2
            ]
          },
          {
            "snapshot": 3,
            "covered": 6,
            "integral": [
              42,
              1
            ],
            "mean": [
              7,
              1
            ]
          },
          {
            "snapshot": 3,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "query_gets": [
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          }
        ]
      }
    ],
    "predicted": [
      {
        "op": "seed",
        "head": 1,
        "answers": [
          {
            "snapshot": 1,
            "covered": 12,
            "integral": [
              54,
              1
            ],
            "mean": [
              9,
              2
            ]
          },
          {
            "snapshot": 1,
            "covered": 6,
            "integral": [
              42,
              1
            ],
            "mean": [
              7,
              1
            ]
          },
          {
            "snapshot": 1,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "affected": "一次性建站：无效胜者 id60 是 t=15 断点，12-15、15-20 均不成边；A 站三条边、覆盖 12。",
        "access": "全量构建一次读 9 行并写全部 ax_* 结构；录制 query_gets 为参照全扫描，与后续索引访问分开记账。"
      },
      {
        "op": "delete",
        "head": 2,
        "answers": [
          {
            "snapshot": 2,
            "covered": 20,
            "integral": [
              110,
              1
            ],
            "mean": [
              11,
              2
            ]
          },
          {
            "snapshot": 2,
            "covered": 6,
            "integral": [
              42,
              1
            ],
            "mean": [
              7,
              1
            ]
          },
          {
            "snapshot": 2,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "affected": "删除无效断点 id60（a:3）：t=15 无同刻候选，胜者节点移除，t=12 与 t=20 成为相邻有效点，间隔 8≤10，新增边 12-20；片段 3→4，覆盖 12→20、积分 54→110。",
        "access": "ax_phys 定位 id60，同刻 k=1、邻点 t=12/t=20 两次 seek；旧边本不存在，仅一条新边做 O(log p) BIT 写入，无整站重排。"
      },
      {
        "op": "delete",
        "head": 3,
        "answers": [
          {
            "snapshot": 3,
            "covered": 20,
            "integral": [
              110,
              1
            ],
            "mean": [
              11,
              2
            ]
          },
          {
            "snapshot": 3,
            "covered": 6,
            "integral": [
              42,
              1
            ],
            "mean": [
              7,
              1
            ]
          },
          {
            "snapshot": 3,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "affected": "删除隐藏候选 id30（b:0）：id20 仍是 t=5 最小 id 胜者，胜者集合、四条边与全部数值保持 110 不变。",
        "access": "只读同刻候选组 k=2 并翻转一个 alive 位，ax_winner 与 BIT 零写入；证明删除非胜者不会误改片段。"
      }
    ]
  },
  "layout": {
    "actual": [
      {
        "op": "seed",
        "snapshot": 1,
        "operation_gets": [],
        "transactions": [
          "BEGIN IMMEDIATE",
          "COMMIT"
        ],
        "head": 1,
        "answers": [
          {
            "snapshot": 1,
            "covered": 12,
            "integral": [
              54,
              1
            ],
            "mean": [
              9,
              2
            ]
          },
          {
            "snapshot": 1,
            "covered": 6,
            "integral": [
              42,
              1
            ],
            "mean": [
              7,
              1
            ]
          },
          {
            "snapshot": 1,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "query_gets": [
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          }
        ]
      },
      {
        "op": "prepare",
        "plan": {
          "base": 1,
          "inputs": [
            "a",
            "b"
          ],
          "outputs": [
            "rewrite-r-0",
            "rewrite-r-1"
          ]
        },
        "operation_gets": [
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          }
        ],
        "transactions": [],
        "head": 1,
        "answers": [
          {
            "snapshot": 1,
            "covered": 12,
            "integral": [
              54,
              1
            ],
            "mean": [
              9,
              2
            ]
          },
          {
            "snapshot": 1,
            "covered": 6,
            "integral": [
              42,
              1
            ],
            "mean": [
              7,
              1
            ]
          },
          {
            "snapshot": 1,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "query_gets": [
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          }
        ]
      },
      {
        "op": "rollback_publish",
        "inside": [
          {
            "snapshot": 2,
            "covered": 12,
            "integral": [
              54,
              1
            ],
            "mean": [
              9,
              2
            ]
          },
          {
            "snapshot": 2,
            "covered": 6,
            "integral": [
              42,
              1
            ],
            "mean": [
              7,
              1
            ]
          },
          {
            "snapshot": 2,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "error": "OBSERVER_ROLLBACK",
        "operation_gets": [
          {
            "file": "rewrite-r-0",
            "rows": 4
          },
          {
            "file": "rewrite-r-1",
            "rows": 5
          },
          {
            "file": "rewrite-r-0",
            "rows": 4
          },
          {
            "file": "rewrite-r-1",
            "rows": 5
          },
          {
            "file": "rewrite-r-0",
            "rows": 4
          },
          {
            "file": "rewrite-r-1",
            "rows": 5
          }
        ],
        "transactions": [
          "BEGIN IMMEDIATE",
          "ROLLBACK"
        ],
        "head": 1,
        "answers": [
          {
            "snapshot": 1,
            "covered": 12,
            "integral": [
              54,
              1
            ],
            "mean": [
              9,
              2
            ]
          },
          {
            "snapshot": 1,
            "covered": 6,
            "integral": [
              42,
              1
            ],
            "mean": [
              7,
              1
            ]
          },
          {
            "snapshot": 1,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "query_gets": [
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          }
        ]
      },
      {
        "op": "publish",
        "snapshot": 2,
        "operation_gets": [],
        "transactions": [
          "BEGIN IMMEDIATE",
          "COMMIT"
        ],
        "head": 2,
        "answers": [
          {
            "snapshot": 2,
            "covered": 12,
            "integral": [
              54,
              1
            ],
            "mean": [
              9,
              2
            ]
          },
          {
            "snapshot": 2,
            "covered": 6,
            "integral": [
              42,
              1
            ],
            "mean": [
              7,
              1
            ]
          },
          {
            "snapshot": 2,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "query_gets": [
          {
            "file": "rewrite-r-0",
            "rows": 4
          },
          {
            "file": "rewrite-r-1",
            "rows": 5
          },
          {
            "file": "rewrite-r-0",
            "rows": 4
          },
          {
            "file": "rewrite-r-1",
            "rows": 5
          },
          {
            "file": "rewrite-r-0",
            "rows": 4
          },
          {
            "file": "rewrite-r-1",
            "rows": 5
          }
        ]
      },
      {
        "op": "delete",
        "error": "STALE_LAYOUT",
        "operation_gets": [
          {
            "file": "a",
            "rows": 4
          }
        ],
        "transactions": [
          "BEGIN IMMEDIATE",
          "ROLLBACK"
        ],
        "head": 2,
        "answers": [
          {
            "snapshot": 2,
            "covered": 12,
            "integral": [
              54,
              1
            ],
            "mean": [
              9,
              2
            ]
          },
          {
            "snapshot": 2,
            "covered": 6,
            "integral": [
              42,
              1
            ],
            "mean": [
              7,
              1
            ]
          },
          {
            "snapshot": 2,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "query_gets": [
          {
            "file": "rewrite-r-0",
            "rows": 4
          },
          {
            "file": "rewrite-r-1",
            "rows": 5
          },
          {
            "file": "rewrite-r-0",
            "rows": 4
          },
          {
            "file": "rewrite-r-1",
            "rows": 5
          },
          {
            "file": "rewrite-r-0",
            "rows": 4
          },
          {
            "file": "rewrite-r-1",
            "rows": 5
          }
        ]
      },
      {
        "op": "delete",
        "snapshot": 3,
        "operation_gets": [
          {
            "file": "rewrite-r-0",
            "rows": 4
          }
        ],
        "transactions": [
          "BEGIN IMMEDIATE",
          "COMMIT"
        ],
        "head": 3,
        "answers": [
          {
            "snapshot": 3,
            "covered": 12,
            "integral": [
              24,
              1
            ],
            "mean": [
              2,
              1
            ]
          },
          {
            "snapshot": 3,
            "covered": 6,
            "integral": [
              84,
              5
            ],
            "mean": [
              14,
              5
            ]
          },
          {
            "snapshot": 3,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "query_gets": [
          {
            "file": "rewrite-r-0",
            "rows": 4
          },
          {
            "file": "rewrite-r-1",
            "rows": 5
          },
          {
            "file": "rewrite-r-0",
            "rows": 4
          },
          {
            "file": "rewrite-r-1",
            "rows": 5
          },
          {
            "file": "rewrite-r-0",
            "rows": 4
          },
          {
            "file": "rewrite-r-1",
            "rows": 5
          }
        ]
      },
      {
        "op": "history",
        "history": {
          "snapshot": 1,
          "rows": [
            {
              "file": "a",
              "pos": 0,
              "value": {
                "id": 10,
                "reading": 0,
                "station": "A",
                "t": 0,
                "valid": true
              }
            },
            {
              "file": "a",
              "pos": 1,
              "value": {
                "id": 20,
                "reading": 10,
                "station": "A",
                "t": 5,
                "valid": true
              }
            },
            {
              "file": "a",
              "pos": 2,
              "value": {
                "id": 40,
                "reading": 0,
                "station": "A",
                "t": 10,
                "valid": true
              }
            },
            {
              "file": "a",
              "pos": 3,
              "value": {
                "id": 60,
                "reading": 5,
                "station": "A",
                "t": 15,
                "valid": false
              }
            },
            {
              "file": "b",
              "pos": 0,
              "value": {
                "id": 30,
                "reading": 4,
                "station": "A",
                "t": 5,
                "valid": true
              }
            },
            {
              "file": "b",
              "pos": 1,
              "value": {
                "id": 50,
                "reading": 4,
                "station": "A",
                "t": 12,
                "valid": true
              }
            },
            {
              "file": "b",
              "pos": 2,
              "value": {
                "id": 70,
                "reading": 10,
                "station": "A",
                "t": 20,
                "valid": true
              }
            },
            {
              "file": "b",
              "pos": 3,
              "value": {
                "id": 80,
                "reading": 100,
                "station": "B",
                "t": 0,
                "valid": true
              }
            },
            {
              "file": "b",
              "pos": 4,
              "value": {
                "id": 90,
                "reading": 100,
                "station": "B",
                "t": 10,
                "valid": true
              }
            }
          ]
        },
        "operation_gets": [
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          }
        ],
        "transactions": [],
        "head": 3,
        "answers": [
          {
            "snapshot": 3,
            "covered": 12,
            "integral": [
              24,
              1
            ],
            "mean": [
              2,
              1
            ]
          },
          {
            "snapshot": 3,
            "covered": 6,
            "integral": [
              84,
              5
            ],
            "mean": [
              14,
              5
            ]
          },
          {
            "snapshot": 3,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "query_gets": [
          {
            "file": "rewrite-r-0",
            "rows": 4
          },
          {
            "file": "rewrite-r-1",
            "rows": 5
          },
          {
            "file": "rewrite-r-0",
            "rows": 4
          },
          {
            "file": "rewrite-r-1",
            "rows": 5
          },
          {
            "file": "rewrite-r-0",
            "rows": 4
          },
          {
            "file": "rewrite-r-1",
            "rows": 5
          }
        ]
      }
    ],
    "predicted": [
      {
        "op": "seed",
        "head": 1,
        "answers": [
          {
            "snapshot": 1,
            "covered": 12,
            "integral": [
              54,
              1
            ],
            "mean": [
              9,
              2
            ]
          },
          {
            "snapshot": 1,
            "covered": 6,
            "integral": [
              42,
              1
            ],
            "mean": [
              7,
              1
            ]
          },
          {
            "snapshot": 1,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "affected": "一次性建站，逻辑 id 与物理位置的初始对应登记入 ax_phys；A 三一边、B 一边。",
        "access": "全量构建读 9 行一次；其后所有查询只走索引。录制 query_gets 是参照 analysis_ref.py:14 的全扫描成本。"
      },
      {
        "op": "prepare",
        "head": 1,
        "answers": [
          {
            "snapshot": 1,
            "covered": 12,
            "integral": [
              54,
              1
            ],
            "mean": [
              9,
              2
            ]
          },
          {
            "snapshot": 1,
            "covered": 6,
            "integral": [
              42,
              1
            ],
            "mean": [
              7,
              1
            ]
          },
          {
            "snapshot": 1,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "affected": "prepare 只生成 rewrite-r-0/1 两个新文件：九个逻辑 id 全部存活，胜者、同刻候选与边零变化，head 仍为 1。",
        "access": "复用 lake.py:43 已读入的输入载荷和 lake.py:51 已组好的输出行，在内存计划里暂存 9 条 (新文件,新位置)→id 映射；分析表不发布不修改，额外载荷读为零。"
      },
      {
        "op": "rollback_publish",
        "head": 1,
        "answers": [
          {
            "snapshot": 1,
            "covered": 12,
            "integral": [
              54,
              1
            ],
            "mean": [
              9,
              2
            ]
          },
          {
            "snapshot": 1,
            "covered": 6,
            "integral": [
              42,
              1
            ],
            "mean": [
              7,
              1
            ]
          },
          {
            "snapshot": 1,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "inside": [
          {
            "snapshot": 2,
            "covered": 12,
            "integral": [
              54,
              1
            ],
            "mean": [
              9,
              2
            ]
          },
          {
            "snapshot": 2,
            "covered": 6,
            "integral": [
              42,
              1
            ],
            "mean": [
              7,
              1
            ]
          },
          {
            "snapshot": 2,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "affected": "回滚发布：事务内物理映射切到重写文件，逻辑候选与边完全不变，故事务内答案与提交态同为 54；ROLLBACK 后映射恢复到 a/b。",
        "access": "事务内仅 O(m=9) 条 ax_phys 插入与当前位置切换，BIT 零写入；同一连接上的查询经新映射仍命中相同逻辑 id。录制里 rewrite 文件的 gets 全是参照查询全扫描，非索引成本。"
      },
      {
        "op": "publish",
        "head": 2,
        "answers": [
          {
            "snapshot": 2,
            "covered": 12,
            "integral": [
              54,
              1
            ],
            "mean": [
              9,
              2
            ]
          },
          {
            "snapshot": 2,
            "covered": 6,
            "integral": [
              42,
              1
            ],
            "mean": [
              7,
              1
            ]
          },
          {
            "snapshot": 2,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "affected": "发布成功：快照 2 仅切换物理映射，九个逻辑 id、胜者与边不变，三个窗口数值与快照 1 一致。",
        "access": "同一 catalog.write 事务内提交 ax_phys 映射与 ax_meta.head；winner/BIT 零变更，满足重写只改物理映射、不重复入账。"
      },
      {
        "op": "delete",
        "head": 2,
        "answers": [
          {
            "snapshot": 2,
            "covered": 12,
            "integral": [
              54,
              1
            ],
            "mean": [
              9,
              2
            ]
          },
          {
            "snapshot": 2,
            "covered": 6,
            "integral": [
              42,
              1
            ],
            "mean": [
              7,
              1
            ]
          },
          {
            "snapshot": 2,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "affected": "按旧布局删除 a:1：a 已不在当前文件集，lake.py:33-34 抛 STALE_LAYOUT 且事务回滚；逻辑候选、片段与答案均不变。",
        "access": "分析钩子在 STALE_LAYOUT 抛出后不执行，ax_* 零写入；录制的 a:4 仅为 lake.py:29 的原业务校验读。"
      },
      {
        "op": "delete",
        "head": 3,
        "answers": [
          {
            "snapshot": 3,
            "covered": 12,
            "integral": [
              24,
              1
            ],
            "mean": [
              2,
              1
            ]
          },
          {
            "snapshot": 3,
            "covered": 6,
            "integral": [
              84,
              5
            ],
            "mean": [
              14,
              5
            ]
          },
          {
            "snapshot": 3,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "affected": "按新位置删除 rewrite-r-0:1：经映射解析到逻辑 id20，与 ties 中删 a:1 等价——id30 显露、两条邻边重算，积分变为 24。",
        "access": "一次 ax_phys seek 即由新物理位置取得 id20，随后同刻 k=2、邻点两次 seek 与两条边 O(log p) BIT 更新；不读取 rewrite 文件载荷。"
      },
      {
        "op": "history",
        "head": 3,
        "answers": [
          {
            "snapshot": 3,
            "covered": 12,
            "integral": [
              24,
              1
            ],
            "mean": [
              2,
              1
            ]
          },
          {
            "snapshot": 3,
            "covered": 6,
            "integral": [
              84,
              5
            ],
            "mean": [
              14,
              5
            ]
          },
          {
            "snapshot": 3,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "affected": "history 读取快照 1：历史走 lake.py:18-25 原路径，分析层无历史索引、不介入；当前 head=3 的窗口答案保持 24。",
        "access": "ax_* 零访问；录制的 a:4/b:5 是原业务历史 Lake.read 的载荷读，单列于原业务成本。"
      }
    ]
  },
  "conflict": {
    "actual": [
      {
        "op": "seed",
        "snapshot": 1,
        "operation_gets": [],
        "transactions": [
          "BEGIN IMMEDIATE",
          "COMMIT"
        ],
        "head": 1,
        "answers": [
          {
            "snapshot": 1,
            "covered": 12,
            "integral": [
              54,
              1
            ],
            "mean": [
              9,
              2
            ]
          },
          {
            "snapshot": 1,
            "covered": 6,
            "integral": [
              42,
              1
            ],
            "mean": [
              7,
              1
            ]
          },
          {
            "snapshot": 1,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "query_gets": [
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          }
        ]
      },
      {
        "op": "prepare",
        "plan": {
          "base": 1,
          "inputs": [
            "a",
            "b"
          ],
          "outputs": [
            "rewrite-r-0",
            "rewrite-r-1"
          ]
        },
        "operation_gets": [
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          }
        ],
        "transactions": [],
        "head": 1,
        "answers": [
          {
            "snapshot": 1,
            "covered": 12,
            "integral": [
              54,
              1
            ],
            "mean": [
              9,
              2
            ]
          },
          {
            "snapshot": 1,
            "covered": 6,
            "integral": [
              42,
              1
            ],
            "mean": [
              7,
              1
            ]
          },
          {
            "snapshot": 1,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "query_gets": [
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          }
        ]
      },
      {
        "op": "delete",
        "snapshot": 2,
        "operation_gets": [
          {
            "file": "a",
            "rows": 4
          }
        ],
        "transactions": [
          "BEGIN IMMEDIATE",
          "COMMIT"
        ],
        "head": 2,
        "answers": [
          {
            "snapshot": 2,
            "covered": 12,
            "integral": [
              24,
              1
            ],
            "mean": [
              2,
              1
            ]
          },
          {
            "snapshot": 2,
            "covered": 6,
            "integral": [
              84,
              5
            ],
            "mean": [
              14,
              5
            ]
          },
          {
            "snapshot": 2,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "query_gets": [
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          }
        ]
      },
      {
        "op": "publish",
        "error": "PUBLISH_CONFLICT",
        "operation_gets": [],
        "transactions": [
          "BEGIN IMMEDIATE",
          "ROLLBACK"
        ],
        "head": 2,
        "answers": [
          {
            "snapshot": 2,
            "covered": 12,
            "integral": [
              24,
              1
            ],
            "mean": [
              2,
              1
            ]
          },
          {
            "snapshot": 2,
            "covered": 6,
            "integral": [
              84,
              5
            ],
            "mean": [
              14,
              5
            ]
          },
          {
            "snapshot": 2,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "query_gets": [
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          },
          {
            "file": "a",
            "rows": 4
          },
          {
            "file": "b",
            "rows": 5
          }
        ]
      }
    ],
    "predicted": [
      {
        "op": "seed",
        "head": 1,
        "answers": [
          {
            "snapshot": 1,
            "covered": 12,
            "integral": [
              54,
              1
            ],
            "mean": [
              9,
              2
            ]
          },
          {
            "snapshot": 1,
            "covered": 6,
            "integral": [
              42,
              1
            ],
            "mean": [
              7,
              1
            ]
          },
          {
            "snapshot": 1,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "affected": "一次性建站；A 三一边、B 一边。",
        "access": "全量构建一次；录制 query_gets 为参照全扫描成本，与索引访问分开。"
      },
      {
        "op": "prepare",
        "head": 1,
        "answers": [
          {
            "snapshot": 1,
            "covered": 12,
            "integral": [
              54,
              1
            ],
            "mean": [
              9,
              2
            ]
          },
          {
            "snapshot": 1,
            "covered": 6,
            "integral": [
              42,
              1
            ],
            "mean": [
              7,
              1
            ]
          },
          {
            "snapshot": 1,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "affected": "prepare 暂存重写计划与 9 条物理映射，未发布：head=1，胜者与边不变。",
        "access": "复用 prepare 已读载荷，ax_* 不写入；查询仍经旧映射命中相同逻辑 id。"
      },
      {
        "op": "delete",
        "head": 2,
        "answers": [
          {
            "snapshot": 2,
            "covered": 12,
            "integral": [
              24,
              1
            ],
            "mean": [
              2,
              1
            ]
          },
          {
            "snapshot": 2,
            "covered": 6,
            "integral": [
              84,
              5
            ],
            "mean": [
              14,
              5
            ]
          },
          {
            "snapshot": 2,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "affected": "删除 a:1 的 id20 生效（快照 2）：id30 显露，两条邻边重算，积分 54→24；与 ties 同一步同形。",
        "access": "ax_phys 定位加同刻 k=2、邻点 seek，两条边 O(log p) BIT 更新；原业务 a:4 校验读单列。"
      },
      {
        "op": "publish",
        "head": 2,
        "answers": [
          {
            "snapshot": 2,
            "covered": 12,
            "integral": [
              24,
              1
            ],
            "mean": [
              2,
              1
            ]
          },
          {
            "snapshot": 2,
            "covered": 6,
            "integral": [
              84,
              5
            ],
            "mean": [
              14,
              5
            ]
          },
          {
            "snapshot": 2,
            "covered": 10,
            "integral": [
              1000,
              1
            ],
            "mean": [
              100,
              1
            ]
          }
        ],
        "affected": "publish 因 base=1≠head=2 抛 PUBLISH_CONFLICT：事务回滚，暂存映射不生效，胜者/边保持快照 2 的 24，不多计任何变更。",
        "access": "冲突在 catalog.write 内抛出，本事务内全部 ax_* 写入随 catalog.py:31-33 ROLLBACK 撤销；录制 operation_gets 为空，分析层同样零持久写入。"
      }
    ]
  },
  "gap": {
    "actual": [
      {
        "op": "seed",
        "snapshot": 1,
        "operation_gets": [],
        "transactions": [
          "BEGIN IMMEDIATE",
          "COMMIT"
        ],
        "head": 1,
        "answers": [
          {
            "snapshot": 1,
            "covered": 5,
            "integral": [
              75,
              2
            ],
            "mean": [
              15,
              2
            ]
          },
          {
            "snapshot": 1,
            "covered": 0,
            "integral": [
              0,
              1
            ],
            "mean": null
          },
          {
            "snapshot": 1,
            "covered": 0,
            "integral": [
              0,
              1
            ],
            "mean": null
          },
          {
            "snapshot": 1,
            "covered": 0,
            "integral": [
              0,
              1
            ],
            "mean": null
          }
        ],
        "query_gets": [
          {
            "file": "edge",
            "rows": 3
          },
          {
            "file": "edge",
            "rows": 3
          },
          {
            "file": "edge",
            "rows": 3
          },
          {
            "file": "edge",
            "rows": 3
          }
        ]
      }
    ],
    "predicted": [
      {
        "op": "seed",
        "head": 1,
        "answers": [
          {
            "snapshot": 1,
            "covered": 5,
            "integral": [
              75,
              2
            ],
            "mean": [
              15,
              2
            ]
          },
          {
            "snapshot": 1,
            "covered": 0,
            "integral": [
              0,
              1
            ],
            "mean": null
          },
          {
            "snapshot": 1,
            "covered": 0,
            "integral": [
              0,
              1
            ],
            "mean": null
          },
          {
            "snapshot": 1,
            "covered": 0,
            "integral": [
              0,
              1
            ],
            "mean": null
          }
        ],
        "affected": "建站：胜者 t=0/10/21，仅 0-10 成边（10→21 间隔 11>G=10）；[5,21) 截出半条边得 75/2，其余三个窗口零覆盖、缺站返回 null。",
        "access": "一次性读 3 行建站；四次查询均为零载荷读：BIT 前缀 O(log p) 加至多两条边界边的 Fraction 截断，空窗与缺站由索引 seek 落空直接判定，不枚举任何片段。"
      }
    ]
  }
}
~~~

## 10. 重跑清单与验证入口调整

- 原业务回归保持不变：`python3 checks.py` 输出 `BASELINE_OK`（断言见 `checks.py:10`-`checks.py:37`）。
- 分析基线：`python3 analysis_check.py baseline` 输出 `ANALYSIS_BASELINE_OK`，含手算锚点、事务边界、scale 的 4098 行全扫描断言与 24 点随机删除的独立中点积分判定（`analysis_check.py:43`-`analysis_check.py:97`）。
- 录制观察：`python3 analysis_probe.py all` 逐行打印五个 case；`python3 analysis_probe.py scale` 打印长窗口与无关站点记录。
- 文档核对：按 `CONTRACT.md` 第 15 行，交付时把 `.pointwise-runtime.json` 的 verify 从 `["python3","analysis_check.py","baseline"]` 改为 `["python3","analysis_check.py","verify"]`，demo 保持 `["python3","analysis_probe.py","all"]`。verify 会重放证据 actual、核对 predicted 数值与 inside、步骤覆盖以及四个源码模块的引用行号有效性（`analysis_check.py:100`-`analysis_check.py:131`）；`affected/access`、复杂度与事务伪代码仍需人工审查，检查通过不代表设计被自动证明正确（`CONTRACT.md` 第 57 行）。

## 11. 明确不做的事

不实现增量分析器与任何新表代码；不新增采样、不改字段与 G；不做并发、崩溃恢复、迁移、回收；不建历史分析索引、不改 `Lake.read`；不改造快照目录结构与原锁范围；不删除或修改任何现有源码、参照、样例、观察器与断言（`CONTRACT.md` 第 43、46、58 行）。本轮交付物仅为本文档与第 10 节的 verify 入口调整。

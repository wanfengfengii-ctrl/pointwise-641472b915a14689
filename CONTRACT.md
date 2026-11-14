# 采样点删除与窗口插值分析：讨论工作区

## 仓库和真实入口

Python 标准库负责行文件与精确算术，SQLite 保存快照目录。lake.py 调用 catalog.py 和 objects.py；这三个生产文件保持原样。samples.py、replay.py、checks.py 和 examples/move.json 保留原业务样例，不要求旧样例拥有分析字段。
analysis_ref.analyse(lake,station,L,R) 是可运行的全量分析参照；analysis_samples.py 提供本题采样行和操作序列；analysis_probe.py 记录真实调用及查询结果，不实现增量索引。

从仓库根目录运行：
- python3 checks.py 核对原业务，输出 BASELINE_OK。
- python3 replay.py all 输出原四种重写与删除时序；python3 replay.py --input examples/move.json 输出指定操作轨迹。
- python3 analysis_probe.py all 每行输出一个 {"case":名称,"actual":步骤数组}，all 可换为 ties、bridge、layout、conflict 或 gap。另有 python3 analysis_probe.py scale，展示长窗口和4096条无关站点记录，初始查询 covered=10、integral=[0,1]、mean=[0,1]，参照实际读取4098条物理行；文档说明推荐索引为何不访问这些无关载荷。
- python3 analysis_check.py baseline 核对原回归、公开手算值、事务边界和有限随机删除的独立中点积分判定。
- python3 analysis_check.py verify 在上述核对后重算 DESIGN.md 的证据。

.pointwise-runtime.json 初始 verify 指向 analysis_check.py baseline，demo 指向 analysis_probe.py all。文档交付时将 verify 改为 ["python3","analysis_check.py","verify"]，demo 保持不变。

## 原业务接口和事务

Lake() 默认使用内存 SQLite 与内存 Objects。持久适配为 Lake(Catalog("work/catalog.db"),Objects("work/objects"))，调用者先创建 work 目录。对象按文件名不可变，文件名永久唯一且只含字母、数字、减号和下划线；相同文件名只允许相同规范内容。
Lake.seed(files) 接收文件名到行对象数组的映射；空库创建快照1，快照0为空，重复初始化报 ALREADY_SEEDED。
Lake.read(snapshot=None) 固定一次 head 或指定历史快照，返回 {"snapshot":整数,"rows":[{"file":文件名,"pos":零起行号,"value":原对象}]}，按文件名及行号排列，排除该快照位置墓碑；历史一直可读。
Lake.delete(snapshot,fid,pos) 先验证源快照，NO_SNAPSHOT 优先；文件不在源快照、位置非整数或越界报 BAD_POSITION，当前布局不再包含该文件报 STALE_LAYOUT。已删位置返回当前 head，不新增快照；首次删除原子发布下一快照。错误顺序与返回值沿源码，不修复跨重写的旧位置删除。
Lake.prepare(job,inputs,layout) 固定当前 head，inputs 必须互异、非空且属于当前文件集，否则 BAD_INPUTS。layout 是输出组数组，每组为 [输入文件名,原行号] 数组，必须恰好列出所有输入物理位置一次，包括墓碑，否则 BAD_LAYOUT。准备时过滤基准墓碑，写出 rewrite-JOB-组号；空组和零行输入合法。返回 {"base":整数,"inputs":[文件名],"outputs":[文件名]}；调用者用新 job，不篡改计划。
Lake.publish(plan) 仅当 head 等于 base 时替换输入，否则 PUBLISH_CONFLICT；输入不合法报 BAD_PLAN。成功新增快照，输入墓碑随输入移除，输出可为空；原快照不变。
Lake.rewrite(job,inputs,layout) 在同一目录写事务内封装 prepare/publish，保持已有行为及锁范围。
Catalog.write 的 BEGIN IMMEDIATE 在嵌套调用时加入外层；异常逃出最外层则回滚。Catalog.commit 写快照、文件集、墓碑及 head，Objects 不在该事务中。外层回滚可留下不可达对象，但不改变已提交读取结果。

## 分析口径和公开输入输出

只对本题合法采样数据启用分析；不把原业务样例改造为采样数据。seed 的每行含全局唯一正整数 id、非空 station 字符串、整数 t、整数 reading、布尔 valid，字段在重写时不变。id、t、reading 及窗口端点均在有符号64位整数范围内，布尔不作整数。所有输入保证合法；无需增加数据清洗接口。
同站同刻只取当前可见 id 最小的行，先选胜者再判断 valid；无效胜者是断点，不能跳过它另取有效候选。按时间排序后，仅相邻胜者均有效且间隔不超过固定 G=10 时，在两点间线性插值。不向两端外推。删除无效断点可能重新接通两侧，删除胜者可能显露同刻另一候选，删除非胜者也须正确保留候选状态。
查询 analyse(lake,station,L,R) 只分析当前 head 的 [L,R)，返回 {"snapshot":整数,"covered":整数时长,"integral":[分子,分母],"mean":[分子,分母]或null}。分母为正且约分，零为 [0,1]；mean 为 integral/covered，covered=0 时为 null。窗口可截断片段，积分保持精确，不允许浮点近似。空窗与不存在站点返回零覆盖、零积分和 null。L>R 或非整数端点抛 ValueError("BAD_WINDOW")，不改变状态。
原参照入口直接可调用，不要求实现新的 analyse 函数。拟议查询保持其签名及结果。

公开手算锚点：
- 默认 A 的 [0,20) 初始 covered=12、integral=[54,1]、mean=[9,2]；[2,8) 为 6、[42,1]、[7,1]。
- 删除 a:1 的 id20 后，同刻 id30 显露，[0,20) 变为 12、[24,1]、[2,1]；[2,8) 积分为 [84,5]。重复删除不变。再删 b:0 的 id30 后，[0,20) 为 12、[4,1]、[1,3]。
- 从初始状态只删除无效 a:3 的 id60，[0,20) 为 20、[110,1]、[11,2]；再删隐藏候选 b:0 不改变分析值。B 的 [0,20) 始终为 10、[1000,1]、[100,1]。
- gap 的三点为 (0,0)、(10,10)、(21,30)，均有效；[5,21) 为 5、[75,2]、[15,2]，[10,21)、空窗及不存在站点均零覆盖。

## 方案边界

首次启用前无在途操作，可全量读取当前采样构建索引。之后只发生单点位置删除和已有物理重写，不新增采样、修订字段或改变 G；调用串行，不要求并发、崩溃恢复、迁移、回收或历史分析索引。历史 Lake.read 保持原样。
新增分析维护只能定位同刻候选、时间邻点及必要索引路径，不得每次删除重新读全部可见行、重排或重算整个站点。窗口查询不得枚举窗口内全部采样点或片段，也不得用后台全量重建或逐时刻网格代替索引。
这些访问约束针对新增分析层；原 Catalog.state/commit 的快照清单复制、Lake.delete 的原有载荷位置验证、prepare 的输入载荷读取不在本题优化。须单列原业务成本与新增分析成本，不能把原有扫描宣称为已经消除。重写可利用已读取的输入及输出建立物理位置到逻辑 id 的对应，不得为分析扫描无关载荷。
只改变文件和位置的重写不能重复入账或撤回逻辑采样；prepare 尚未发布不改变分析，PUBLISH_CONFLICT 与 STALE_LAYOUT 均不改变分析。分析索引、物理映射、分析绑定的 head 必须随目录同一事务生效；外层事务内查询应看到本事务结果，外层回滚后恢复原值，重复删除不多计。允许提出窄的内部钩子，不要求改造快照目录结构或原锁范围。

## 可重跑观察及文档交付

analysis_probe.run(spec) 接收 {"files":映射,"windows":[[station,L,R],...],"operations":[操作,...]}。seed 自动先执行一次；支持 delete、prepare、publish、history、rollback_delete、rollback_publish，字段与原接口相同；history 用 snapshot，publish 使用最近 prepare 的计划。两类 rollback 操作在真实外层 write 内执行，记录事务内查询后主动抛出 OBSERVER_ROLLBACK，随后记录回滚后查询。history 原样保存指定快照读取。
python3 analysis_probe.py --input PACK.json 读取名称到上述 spec 的映射，每行返回一个 case/actual。自定义样例格式与 analysis_samples.sample 返回值一致。
每步含 op、返回的 snapshot/plan/history 或 error、head、answers；answers 按 windows 顺序保存全量参照结果，inside 若存在则为外层回滚前的查询结果。operation_gets 是操作期间每次 Objects.get 返回的文件和物理行数，含 rollback 内查询；query_gets 是操作后附加查询的读取，两者分开。transactions 只列操作期间真实 BEGIN IMMEDIATE、COMMIT、ROLLBACK，不是持久成本统计；获取完整行文件包含墓碑载荷。计数不代表数据库页访问、索引访问或耗时。
layout 覆盖 prepare、外层回滚发布、成功重排、旧布局删除拒绝、按新位置删除及旧快照读取；conflict 覆盖 prepare 后删除导致发布失败。原业务回归另保留不带分析字段的同值异行、空历史及同步重写语义。

交付带 文件名:行号 的 DESIGN.md，串起物理位置删除、快照提交、重写和参照查询。比较两种满足访问边界的索引组织，选定一种；说明候选与邻接变化、窗口截断、精确汇总、物理映射接入、持久状态及事务伪代码。给出依赖站点点数、同刻候选数、片段数和本次重写行数的访问及空间成本；覆盖大量无关点与长窗口，说明查询为何不枚举内部片段。不要求理论全证明或实现增量分析器。
文档用 <!-- interpolation-evidence --> 标记后一个 ~~~json 代码块嵌入证据，顶层恰为 ties、bridge、layout、conflict、gap。每项 actual 为 run(sample(name)) 的完整原始步骤数组；predicted 同步数，每步有 op、head、answers，存在 inside 时也须保留同形 inside。另加非空文字 affected 和 access，逐步预测会改变或保持哪些逻辑候选/片段，以及推荐索引实际读取/修改的路径和原因。数值预测应等于参照；实际全扫描计数与方案访问预测分开标注。
analysis_check.py verify 重放 actual、核对预测数值、步骤覆盖及四个源码模块的有效行号；affected/access 的正确性、复杂度与事务伪代码仍需人工审查，检查通过不能冒充设计正确。
保留业务源码、参照、样例、观察器与原断言，只交讨论文档及运行清单 verify 的调整。

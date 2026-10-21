# agent-service 的插件宿主

这份文档说明 agent-service 现在由什么组成：一个插件宿主（`app/host/`），一个固定的 agent 循环（`app/agent/`），一组插件（她的功能在 `app/living/<插件>/`，world 的在 `app/world/<插件>/`，运维用的在 `app/plugins/`），以及每个 App 一份的清单（`app/manifests/`）。它取代了原来的三份 dataflow 框架文档（`dataflow-framework-overview.md`、`dataflow-framework.md`、`dataflow-node-contract.md`）。

换掉 dataflow 框架的原因有三条。第一，那张图早就不驱动业务了：业务实际上是"一个钟触发一个节点，节点里调一长串普通函数"，图上的边、持久投递和出口都只剩一两个使用者，大部分框架代码在每次启动时空转。第二，加一个工具、一段输入或一个住户都要改中心文件（一个函数里做了十来个功能的事，工具和住户是写死的列表），改动面和功能本身不成比例。第三，包之间有双向依赖，基础层反过来 import 业务。宿主加插件的结构把这三件事分别解决掉：唤醒统一由宿主的轮次队列管，一轮由固定循环跑，功能各自是插件、只经服务交互，一个 App 由哪些插件组成写在清单里。

## 1. 分层和依赖方向

从下往上一共四层，下层永远不 import 上层：

| 层 | 位置 | 做什么 |
| --- | --- | --- |
| 存储、消息、基建、能力 | `app/data/`、`app/messaging/`、`app/infra/`、`app/capabilities/` | 表和读写原语、事务；参与者之间的具名收件箱；broker、配置、出站队列；对外部系统的封装（模型网关之外的 HTTP、Redis、搜索、天气、沙箱） |
| 一轮（agent 层） | `app/agent/` | 契约类型（`contracts.py`）、一轮的固定循环（`round.py`）、单次模型调用（`core.py`、`adapters/`）、prompt 段落和严格渲染（`prompts.py`）、历史的读取裁剪存储（`continuity.py`） |
| 宿主 | `app/host/` | 插件生命周期、服务注册、轮次队列和钟、管理路由、收件箱和出站队列的登记，以及按清单组装一轮 |
| 插件和清单 | `app/living/<插件>/`、`app/world/<插件>/`、`app/plugins/`；`app/manifests/` | 业务功能；一个 App 由哪些插件、agent、参与者组成 |

`app/nodes/`（运维接口的处理函数）和 `app/skills/`（说明文档的加载和渲染）是插件调用的业务模块，按插件的规则管。

方向这样定，是因为每一层只该知道它下面的东西。agent 层要用的类型（一轮由什么组成、一个 agent 怎么调）都放在 `app/agent/contracts.py`，宿主 import 它们来组装，所以 agent 层不需要知道宿主。宿主不 import 任何插件：清单里写的是插件的模块名，宿主到启动时才按名字 import（`Host.for_app`），所以一个进程只加载它自己清单里的插件，world 的进程里不出现她的任何代码，反过来也一样。存储和消息机制在最下面，宿主启停消息机制、插件经宿主开设收件箱，它们不依赖宿主和 agent 层。这些方向由 CI 检查固定（第 9 节），写反一条 import 就失败。

## 2. 一个 App 是一份清单

同一个镜像跑出两个 App：`agent-service`（三姐妹的生活）和 `world`。`app/deployment.py` 的 `APPS` 把 App 名映射到清单模块，进程按环境变量 `APP_NAME` 找到自己的清单，`app.main` 的 lifespan 用 `Host.for_app(APP_NAME)` 起宿主。

清单是一个 `App`（`app.host.App`），只含数据：

- `plugins`：插件模块名，按这个顺序 setup（被依赖的插件先起，见第 3 节）；
- `agents`：每个 agent 的定义（`AgentSpec`）：由哪些 prompt 段落组成、用什么模型、trace 名、调用预算、Langfuse session 的格式；子 agent 和单次调用还列出自己能用的工具。名字取原来的 prompt id（`living_life_moment`、`world_round`、`world_perception` ……），回放和 trace 按这个名字认调用；
- `profiles`：一个参与者的一轮由什么组成（`Profile`）：主 agent 和它能调的子 agent，工具（顺序就是给模型的顺序），状态段和输入段，一轮开始前的步骤、检查、提交的步骤和提交之后的步骤，历史放在哪、怎么裁，成本怎么记，哪些插件可以被开关关掉；
- `participants`：参与者和他们用的 profile。她这边是三姐妹，world 那边是 world 自己。

宿主在所有插件 setup 完、碰任何外部服务之前，拿清单去对插件登记的东西（`app/host/assembly.py`）：清单列了而没有插件登记的名字报 `UnknownContribution`，插件登记了而清单没放的报 `UnplacedContribution`，重名报 `DuplicateContribution`，每种错误一条消息列全。顺序之所以由清单集中写，而不是按插件拼，是因为顺序本身是请求的一部分：工具的顺序就是给模型的工具 schema 的顺序，也是前缀缓存的一部分，而且她的工具里有一个属于别的插件、却排在中间。新登记的东西必须在清单里明确放位置，宿主才起得来。

所以给她的一轮加一个工具或一段输入，只改那个插件和清单两处：插件在 setup 里登记，清单把插件加进 `plugins`、把名字写进 profile 对应的位置。`tests/living/radio` 是一个只在测试里存在的插件，`tests/living/test_a_new_feature.py` 用它演示这件事，并且证明两处缺一处宿主都起不来。

## 3. 插件

一个插件是一个 `Plugin(name, setup, requires=(), provides=())`，模块里叫 `PLUGIN`。`setup(ctx)` 是同步的，只登记、不做任何 IO：这样清单或插件写错时，进程在碰数据库和 broker 之前就失败，测试里也能在没有任何外部服务的情况下把整个 App 起一遍。每一次登记都返回一个撤销函数，宿主停下时按插件的反序、每个插件内按登记的反序把它们全部撤掉；setup 之外再登记会被拒绝，因为宿主没有办法启动一个迟到的登记。

`ctx` 上能登记的东西分几组：

- **轮次和后台工作。** `ctx.turns(participant, line)` 拿到一条轮次队列（第 4 节）；`ctx.wake(turns, source, every=...)` 让一个钟定时给队列发信号；`ctx.clock(name, seconds, tick)` 是不排参与者轮次的周期工作（比如对账）；`ctx.task(name, run)` 是一直跑的后台任务，宿主停下时取消它。业务代码自己不起任务和线程，原因是宿主不知道这些任务：停的时候没人取消，出错了也没人看见。
- **进出口。** `ctx.route(...)` 是管理用的 HTTP 路由，可以要求内部凭据、核对泳道；`ctx.inbox(name, on_message=..., ...)` 开设一个具名收件箱，返回一个 `Inbox`：调用它就撤掉登记，`succeeded(ids)` 问通信机制这些消息里哪些在这个收件箱已经记下了处理成功，`attempt_limit` 是一条消息最多处理几次；`ctx.inboxes_at_start(open_them)` 开设名字到启动时才知道的收件箱（三姐妹各一个，名字从人设表读）；`ctx.outbound(data_type, queue)` 登记一类出站消息发到哪条队列，业务代码只调 `app.infra.outbound.publish`。
- **一轮的组成。** `ctx.tool(tool, trim=..., ends_round=...)`（`trim` 说它的结果在历史里怎么裁：读到的材料过一阵换成一句短的说明，`kept` 跟她自己说的话留得一样久；`ends_round` 说调它就结束这一轮）；`ctx.segment(segment)` 是 agent prompt 的一个段落，对应一个 Langfuse prompt；`ctx.section(name, section)` 是一段状态或输入；`ctx.round_start`、`ctx.check`、`ctx.commit`、`ctx.after_commit` 是一轮各阶段的步骤；`ctx.round_log` 说一轮怎么读上一轮。它们放在一轮的哪个位置，由清单的 profile 决定。
- **组装好的东西。** `ctx.profile(name)` 是宿主按清单填好的 profile，交给固定循环跑；`ctx.participants(profile=...)` 是用这个 profile 的参与者，插件按它为每个人登记队列和收件箱；`ctx.agent(name)` 是清单里的一个 agent，带上它能用的工具，子 agent 和单次调用用它。
- **服务。** `ctx.provide(name, service)` 交出一个服务（必须在 `provides` 里列出）；`ctx.service(name)` 取一个别的插件提供的服务（必须在 `requires` 里列出）。宿主按 `requires` / `provides` 排 setup 的顺序，缺了、重了、成环了都在启动时报错。`ctx.on_stop(undo)` 在停下时做一件收尾的事。

插件之间不互相 import，只经服务交互。服务的接口是 Protocol，她这边写在 `app/living/services.py`，world 那边写在 `app/world/services.py`，连同接口里出现的数据类型。插件在 setup 里取到服务，作为构造参数交给自己的插件对象，工具、段和步骤是这个对象的方法。这样一个插件的测试直接给替身，不需要起别的插件；一个插件改了内部实现，只要服务的接口不变，用它的插件不受影响。每一边还有几个各插件共用的模块：她这边是 `common.py`（不读库、不调模型的纯函数）和 `services.py`，world 那边是 `services.py`、`agents.py`、`volume.py`。插件目录之外不放别的模块，否则几个插件可以绕开服务共用它。

## 4. 唤醒和轮次队列

一个参与者的轮次排在队列上，一次只跑一轮。一个参与者可以有几条队列，每条一个用途，彼此不挡：她的一轮（常规节奏和提前叫醒）一条，日记一条，每周回看一条；world 的一轮一条。实现在 `app/host/turns.py`。

信号只是"请给我排一轮"，不带要做的事。信号来自钟（`ctx.wake`）或者一次投递（world 的收件箱每收到一条要叫醒它的消息就发一个）。轮到它的时候，唤醒来源（`WakeSource`）的 `due` 看那一刻的真实状态决定这一轮跑不跑、跑什么，`run` 去跑。之所以在轮到时才判断，而不是在信号到达时，是因为排在一轮卡住的轮次后面等了十五分钟的信号，不能拿十五分钟前的状态去跑。同一个来源的信号怎么合并由来源声明（`Coalesce`）：日记和回看每个信号都排；她的常规节奏和提前叫醒在已经有一个排着或在跑时丢掉新的；world 的收件箱让一轮在跑时到达的投递并进下一轮、共享它的结果。

一轮的时刻在开始跑时读，不在信号到达时读：她这边是拿到队列的那一刻、在读任何状态之前；world 是问完通信机制哪些消息已经处理过、不再接受新投递并进来、读完待处理的消息之后。队列有占用上限（`hold_limit`）：一轮占着队列超过它就被取消、按崩溃处理，这是为了把"永远不结束"变成一次失败，而不是一轮的预算。队列是进程内的锁，前提是这个 App 单副本运行；滚动发布时新旧两个进程会短暂重叠。

钟是生产环境的副作用：非 prod 泳道默认不启动钟（`app/infra/lane_policy.py`），要在 coe 或 ppe 泳道验证钟驱动的行为，设 `DATAFLOW_ENABLE_TIME_SOURCES=1`。

## 5. 一轮：固定循环

`app.agent.round.run_round` 用宿主组装好的 profile 跑一个参与者的一轮，顺序固定：一轮开始前的步骤；读上一轮和历史；状态段和输入段，按 profile 的顺序拼起来（返回 `None` 的段不出现）；主 agent 和子 agent 的段落，这一轮内不变；主 agent 带着 profile 的工具跑模型和工具的循环；记成本；检查；提交的步骤在一个事务里执行；提交之后的步骤，每一步声明自己失败时是让这一轮失败（`FailRound`）还是记日志、计数、照常算这一轮（`Continue`）。存历史就是提交之后的一个步骤。

循环里没有任何业务名字：哪个工具结束一轮由工具自己声明，段落和步骤由插件登记、由清单排位置。参与者看到的话（标记消息的文字、工具参数不对时给模型的说明）也由清单给，不写在循环里。

prompt 是段落拼出来的，每个插件提供的段落是一个单独的 Langfuse prompt，段落声明自己要哪些变量。渲染是严格的：模板里的变量和代码给的变量对不上，不管是多了还是少了，这一轮都以 `PromptConfigError` 失败，记 ERROR 日志和指标。Langfuse 本身对两种不匹配都不报错，少的变量会以字面的 `{{var}}` 发给模型，所以检查放在代码这边。

一轮内的状态按类型存：插件在自己的模块里定义一个类，用 `current_round().state(类)` 或 `call.state(类)` 取，第一次取时创建。profile 可以声明哪些插件能被开关关掉（`Switches`），一轮开始时读一次 Dynamic Config，被关掉的插件的工具、段和步骤在这一轮里都不出现，子 agent 用同一份开关。

## 6. 工具和失败

工具是插件对象上的一个异步方法，用 `Tool(...)` 包起来、`ctx.tool` 登记。工具的失败按 `app.agent.tools.outcome.tool_error` 装饰器的规则变成模型看得到的结果，而不是让这一轮崩掉：

| 工具里抛出的 | 模型看到的 `kind` | 意思 |
| --- | --- | --- |
| `CapabilityInvalidArg` | `invalid_args` | 参数不对，下一次调用要改参数 |
| `CapabilityNotFound` | `not_found` | 要的东西不存在，要换个做法 |
| 其他 `Exception` | `tool_error` | 工具出错了（超时、上游故障），`detail` 里带原始异常的类型；模型可以重试、换工具或者不再用它 |

`CancelledError` 这类 `BaseException` 不被包住，原样往上走，因为它们表示进程在停或调用被取消，必须让上层收尾。之所以把其他异常也交给模型而不是让一轮失败，是因为一次工具调用出错不该让整轮白跑（2026-05-13 之前，这个分支把异常原样抛出，一个工具出错就杀掉了整轮）。一个工具不想走这套规则（它的失败必须让这一轮失败），就不用这个装饰器，让异常抛出去。模型自己的调用错误由循环处理，不经过工具（`app.agent.tooling.dispatch`）：调了一个不存在的工具，或者参数对不上工具的签名（多了、少了、名字错了），都作为这次工具调用的结果交回模型，让它在同一轮里改了再调。

能力层（`app/capabilities/`）对外部系统的失败抛 `app.capabilities.errors` 里的异常，而不是返回 `None` 或 `False`，让调用方分得清"没有"和"出错了"：

| 异常 | 含义 |
| --- | --- |
| `CapabilityInvalidArg` | 调用参数不对（校验失败），不是能力本身的问题 |
| `CapabilityNotFound` | 要的资源不存在 |
| `CapabilityTimeout` | 上游超时；模型调用的重试也认它（`app.agent.RETRYABLE_EXCEPTIONS`） |
| `CapabilityRateLimited` | 上游限流 |
| `CapabilityCallFailed` | 上游的其他失败（5xx、协议错误、连接被拒） |

它们都继承 `CapabilityError`，带一个可选的 `meta` 字典放结构化的上下文。确实有业务含义的空值（没配置搜索后端时返回空列表、缓存未命中）在函数的 docstring 里写明，其余的失败一律抛异常。

## 7. 存储、消息和出站

**存储。** 表由 `Data` 类定义（`app/data/schema.py`），启动时迁移器按类建表、加列（`app/data/migrator.py`），表名由类名决定。读写用 `app.data.persist` 的函数：读是 `fetch_all` / `fetch_one` / `fetch_value` 加 `from_row`，写是 `insert_append`（追加一个版本）和 `insert_idempotent`（按自然键只写一次）；她的经历这类要按提交顺序读的记录用 `app.data.ordered`。几条写入要一起落地时包一层 `async with tx():`。业务代码不开会话、不碰事务里的会话，因为拿到会话就能在事务里执行任意语句，绕开上面这几个函数的约束；状态一律追加写入，不覆盖，保留历史。

**消息。** 参与者之间只通过具名收件箱交流（`app/messaging/`）：`send` 发给某个参与者，`ask` 向某个参与者提问并同步等回答或"没有回答"，`send_at` 指定时刻再送达（可以发给自己，world 用它安排自己下次醒来）。收件箱由拥有者经宿主开设，拥有者不在线时消息一直保留；投递至少一次，接收方按消息 id 去重；处理失败按收件箱的次数重试，用完进本泳道的死信，可以查看、重放（`/admin/messaging/dead-letters`）。每一条消息都先记一行再交给 broker，记录失败这次发送就算失败。一切按进程的部署泳道隔离，不回退到 prod。

**出站。** 她说出去的话和撤回经出站队列交给渠道服务（lark-outbound、chat-response-worker）。插件用 `ctx.outbound` 登记一类消息发到哪条队列，业务代码只调 `app.infra.outbound.publish`。业务里调 `publish` 的地方由 CI 钉住数目（grep-gate 的 Gap 8），现在只有两处，都在 `app/living/speaking/`（说话和撤回）：交出去之后才知道结果，所以它们不放在事务里，结果靠对账收回来。

## 8. 加一个功能

1. 想清楚它属于哪一边、是不是一个新插件。新插件建一个目录 `app/living/<插件>/` 或 `app/world/<插件>/`，模块里写插件对象和 `setup`，`PLUGIN = Plugin(...)`；它要用别的插件的东西，就在对应的 `services.py` 里加一个 Protocol（或用已有的），在 `requires` 里列上名字。
2. 在 setup 里登记它的工具、段落、段和步骤；它的 prompt 段落发到 Langfuse，先发泳道的 label。
3. 改清单：插件加进 `plugins`，名字写进 profile（或 agent）里对应的位置。宿主会拒绝没放位置的登记，也会拒绝列了却没人登记的名字。
4. 测试：插件对象用服务的替身直接测；要验证它在 App 里装得上，用 `tests/conftest.py` 的 `app_host` fixture 按真实清单起宿主（只跑 setup 和清单校验，不碰外部服务）；改变了发给模型的请求或写入的，看回放（`tests/replay/`）的比对结果，有意的改动重新录基准并在提交说明里写清楚。
5. 跑 CI 的那几个检查（第 9 节）和全量测试，然后按项目规则在 coe 泳道上实际跑一次，再谈上线。

## 9. CI 守着的规则

`.github/workflows/grep-gate.yml` 在每个 PR 上跑这些检查。脚本只用标准库，规则自己的测试在 `apps/agent-service/tests/unit/scripts/`，每条都有一个故意违反的合成用例证明它会报出文件和行。

| 检查 | 规则 | 为什么 |
| --- | --- | --- |
| `scripts/check_world_life_imports.py` | `app/world` 和 `app/living` 互不 import，两个 App 的清单也一样 | world 和她是两个独立的引擎，只经通信机制交流；清单 import 了哪边，进程就加载哪边 |
| `scripts/check_plugin_boundaries.py` | 插件只 import 自己目录和本边的共享模块；共享模块和包本身不 import 插件；`app/living`、`app/world` 下插件目录之外只有共享模块 | 插件之间只经服务交互，替换一个插件不牵动别的插件 |
| `scripts/check_framework_private.py` | 业务代码和清单只用 `app.host` 包导出的名字，不 import `app.agent.continuity`、`app.messaging.receiving`、`app.data.session`；任何代码不跨包 import 下划线开头的名字或模块 | 框架内部会改，业务一 import 就把它钉住了；要的东西由包导出或由宿主经 `ctx` 交给插件 |
| `scripts/check_package_cycles.py` | 包之间没有环；下层不 import 上层（存储和消息 < agent 层 < 宿主 < 插件），宿主和 agent 层不 import 任何插件 | 第 1 节的依赖方向 |
| `scripts/check_business_tasks_and_sessions.py` | 业务代码不起任务和线程，不碰事务里的会话 | 后台工作交给宿主，宿主停下时才能收拾；数据库只经存储的读写函数 |
| `manifests-resolve` 任务 | 两个 App 的宿主在连不上任何外部服务的情况下完成 setup 和清单校验 | 清单写错在 PR 上就失败，而不是等到下一次部署 |
| Gap 7 / 8 / 11 / 13 / 14 / 16 / 18 | 业务目录（`app/living`、`app/world`、`app/plugins`、`app/nodes`、`app/skills`）不自己写重试循环、不直接读上下文变量和 Redis、不直接用 httpx、不手动 nack；`publish` 的调用点数目固定 | 这些事由框架和能力层做，业务各写一份就会各有各的毛病 |

改宿主、agent 层的契约类型和固定循环、存储的核心模块、清单、这些检查本身或这份文档，按 `docs/governance/framework-layer.md` 在 PR 正文里引用 spec。

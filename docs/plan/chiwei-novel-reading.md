# 赤尾读小说：文件一视同仁，读是她对一个文件做的事

## Problem

第一版把"读小说"做成了一条**书专用侧通道**：飞书私聊按 txt/epub 后缀识别 → 专用 HTTP 端点 `/api/internal/book/ingest` → 专用表（BookMeta/BookPage）解析分页**注册成一本"书"** → 失败时 channel-server 拿一句写死的话冒充赤尾回真人。这条路把"书"抬成了系统要识别、要注册的一等公民，重复造了一套图片管线早已解决的"非文本内容存取"基建，那句写死的回复又让系统替她说话——既违反"赤尾是个人"，也违反"底座对所有内容一视同仁"。

## Goal

赤尾收到的一个文件**就是一个文件**，跟图片走**同一条媒体轨**：飞书下载 → 存对象存储 → 在 `common_message.content` 留个引用。系统不判它是不是书、不分后缀、不注册、零专用端点、永不替她说话。"书"只存在于她**读**的行为里：她在自己的 life 轮里挑一个收到过的文件去读，异步阅读 agent 把那个文件从对象存储取出来、**读的时候才**解码分页、一页页往后读，揉出她那条单条滚动的第一人称印象，照旧注入她的 life stimulus 和 chat 上下文。

## Non-goals

- 不建书架 / 书库 / 任何"她有哪些书"的注册表——她有哪些文件，就是她对话里收到过哪些文件。
- 不预解析、不持久化书页（BookPage）；分页是读的时候现算的，不落库。
- 不做完成 gate、不做读后感报告、不做章节梗概 / 人物表 / 检索索引（结构化理解明禁）。
- 同一本书重发两次 = 两个独立文件、两份独立印象，不做内容判重。
- 不把文件正文内联进 chat 上下文（小说太长）——她在对话里**看见"你发了个文件《X》"这件事**，正文是她单独去读时才取。
- 系统在这条链上**永不**向真人发任何话（成功不发、失败也不发）；她对"收到文件 / 文件读不了"的任何反应，都来自她自己的 agent 循环。

## Key design decisions

1. **统一的是"文件先成为一条普通消息内容项"，对象存储只是同款附件缓存。** source of truth 是 `common_message.content` 里那条文件项（无条件落、跟图片项一样），把字节缓存进对象存储是和图片**同一套** best-effort enrichment（同样 fire-and-forget、绝不 gate 入站、缓存没成也不影响这条消息存在）。选"复用这套"而非"书/文件专用管线"，因为图片那条轨已经解决"消息带了个非文本的东西、先落库再按需取字节"；并且优先**抽出图片和文件共用的那步"下载附件 → 存对象存储"**（图片=会压缩的调用方、文件=原样存的调用方），而不是给文件克隆一条 image-pipeline 变体。

2. **存储里没有"书"，读的时候现算。** 选"读时从对象存储取文件、现解码现分页"而非"入库时预解析成持久书页 + 注册 book_id"，因为分页是由文件内容确定的（同内容同分页、跨轮稳定），持久化书页就是把"注册"这个被否决的动作换个地方又做一遍。

3. **一个文件的身份 = 她收到它的那一次（附件实例），不是对象存储的 key。** 选"用收到该文件的那条消息 + 内容项身份当自然键"而非"用对象存储 key / 内容 hash"，因为对象存储可能按内容去重、把多次重发指到同一份字节——若拿它当身份，同名文件 / 重发 / 去重会把本该独立的多份印象合并，破坏"重发两次就是两个文件、两份印象"。对象存储的 key 只是这次附件实例的字节载荷。

4. **这条底座链永不向真人说话。** 选"成功失败都静默"而非"失败回一句提示"，因为系统替赤尾吐任何话都是把她降格成带校验报错的上传服务；她知道有这本书靠你跟她的真实对话，她的任何反应靠她自己的 agent 循环。

5. **她感知到的是文件的存在（名字），不是正文。** 选"文件作为普通消息内容项进入她的对话感知（recent_chats 里看到《X》）"而非"把正文内联进 LLM 上下文"，因为小说太长无法内联，而"读"本身是她单独发起的一程动作。

6. **读哪个文件，从她现有上下文边界内可见的文件里认，不另设遗忘窗口。** 选"在她已经能看见的那些消息里的文件项按名字找"而非"查注册表"或"按最近 N 天筛"，因为注册表已经没了、而新加一个 recency 阈值就是用工程替她遗忘（违宪）；0/1/多命中的处理沿用旧 read_book 的精神（不替她选）。

## Caller coverage

（下面是动手前 grep 出的调用面，作覆盖清单用；不写行号和预设字段名——那是实现时才定的、会过期。实现前各自再 grep 一次确认。）

被删除的（拆侧通道 + 拆注册物）：
- `book_ingest_node` / `BookIngestRequest`：仅 `app/wiring/book.py` 引用 → 整条 wire + 节点 + 端点删除。
- `ingest_book` / `derive_book_id` / `BookMeta` / `BookPage`：现状是 `book.py` 内自闭环 + `book_ingest.py` 调用 → 随注册物删除。
- channel-server `selectBookFile` / `forwardBookFile` / `makeBookForwardDeps`：仅 `handlers.ts` 的书分流块调用 → 该块删除，换成文件入轨的 enqueue。

被改写（当前行为 → 改后行为）：
- `find_book_meta`（调用方在 `context.py` / `reading.py` / `life_wake.py` / `book_ingest.py`）：当前查书注册表拿书名+总页数 → 改后不再有书注册表，总页数读时现算、书名由印象自带。
- `find_books_by_title`（调用方 `life_tools.py` 的 read_book 工具）：当前在书注册表里按书名找 → 改后在她现有上下文边界内可见的文件里按名字找。
- `read_page`（调用方 `reading.py`）：当前从持久书页表按页号取 → 改后对"已从对象存储取到的文件内容"按页号现切。
- `ReadingTriggered`（emit 方 `life_tools.py`、消费方 `reading_node` / `reading.py`）：当前携带 book_id → 改后携带"收到该文件的附件实例身份" + 文件名。
- `BookImpression`（`book_impression.py` + 注入方 `life_wake.py` / `context.py`）：当前自然键含 book_id → 改后改用附件实例身份（见决策 3）。

## Data & deployment impact

- **表**：`data_book_meta` / `data_book_page` 不再存在（删 Data 类）；`data_book_impression` / `data_reading_triggered` 自然键改字段（book_id → 文件引用）。coe-world-life2 的 chiwei-test 已建过这四张旧表 → 命中 framework migrator 删列/改键 fail-closed 整批回滚的坑（见 reference_chiwei_data_schema_migrate_footgun），切换前需手动把旧表 DROP 掉让新 schema 重建，MQ 里若有旧 schema 的 ReadingTriggered 遗留消息也要清。
- **对象存储**：文件正文现在落 TOS（经 tool-service），多一份存储；txt/epub 解码（ebooklib）从入库时挪到读时。
- **跨服务部署**：这条轨现在**也动 tool-service**（新增"文件"源类型：下载飞书文件 + 原样传 TOS、不压缩）。一次完整验证要同步部署 channel-server（含 recall-worker / chat-response-worker 同镜像）、tool-service、agent-service。
- **Langfuse**：`book_reading_impression` prompt（薄契约 persona_name/persona_lite）保留，必要时按"读一个文件"措辞微调。
- **部署杀进程**：部署会中断在跑的异步阅读轮，部署前确认没有在读的任务。

## Tasks

### Task 1：文件入轨——和图片同一套，先成消息项、再 best-effort 缓存字节

**目标**：真人发来的任何文件，走正常入站链路**无条件**先成为 `common_message.content` 里的一条普通文件内容项（source of truth）；字节用图片同款的 best-effort enrichment 缓存进对象存储（同样 fire-and-forget、缓存没成不影响这条消息存在、绝不 gate 入站）；彻底拆掉书侧通道。
**产出**：抽出图片与文件共用的"下载附件 → 存对象存储"那一步（图片=会压缩的调用方、文件=原样存的调用方），别给文件克隆一条 image-pipeline 变体；channel-server inbound 对文件消息触发这条缓存（删掉 `book-ingest.ts` 与 handlers 里的书分流块）；agent-service 侧 `common_message.content` 的文件项能携带"附件实例身份 + 对象存储引用"。
**验收**：在 coe 发一个文件给 dev bot → 该文件**先**出现在 `common_message.content` 里（即便对象存储缓存还没回填也已在）、随后带上可解析的对象存储引用；`/api/internal/book/ingest` 端点不复存在；系统未向真人发任何话。切换前已按 Data&deployment 把 coe 旧四表 DROP、清 MQ 旧 schema 遗留消息。

### Task 2：她按需读一个文件——读取对着文件、读时取字节、边界闭合

**目标**：把阅读能力从"读一本注册的书（book_id → BookPage）"改写成"读一个她收到过的文件"。读的时候由 agent-service 经现有 image_client / tool-service 那条路拿到该附件实例的字节（presigned URL 或下载），现解码现分页（txt/epub）、按连续前沿一页页读；read_book 工具改成在她现有上下文边界内可见的文件里按名字认（0/1/多命中沿用旧精神、不替她选）。
**产出**：改写后的 `run_reading_round` / `read` 工具底层对着"读时取到的文件字节"现切页、总页数现算；`ReadingTriggered` 携带附件实例身份 + 文件名；read_book 在她可见消息里解析候选；删除 `BookMeta`/`BookPage`/`ingest_book`/`derive_book_id`/`find_book_meta`/`find_books_by_title`/`read_page` 这套注册物。明确取字节的服务边界：谁给 URL/bytes、字节还没缓存进对象存储（上传 pending / 失败）时整程 fail-soft（印象不动）。
**验收**：对一个已缓存进对象存储的文件触发一程阅读 → 连续前沿进度推进、读到真书尾判 finished、中间取不到内容当数据缺损 fail-soft；对一个尚未/从未入对象存储的附件触发 → 整程 fail-soft、印象不动、不抛穿透；全程不依赖任何书注册表。

### Task 3：滚动印象挂在附件实例上 + 注入

**目标**：单条滚动印象的身份从"注册的 book_id"改挂到**附件实例身份**（决策 3：收到该文件的那条消息 + 内容项，不是对象存储 key），并在 life 醒来的 stimulus 和 chat inner_context 里照旧渲染"当前在读的印象"，注入不再依赖任何书注册表查询（书名由印象自带）。
**产出**：`BookImpression` 自然键改用附件实例身份；`life_wake.py` 与 `memory/context.py` 的注入点渲染当前阅读印象、去掉 `find_book_meta` 依赖。
**验收**：跑完一程阅读后，印象按附件实例持久化、并被渲染进 life stimulus 和 chat inner_context；同一附件实例再读一程在原条上覆盖重写、不新增第二条；**同一份内容分两条消息重发 → 两个附件实例 → 两份独立印象、不合并**。

# AI服务

AI服务是Inner Bot Server的核心AI处理模块，基于FastAPI构建，提供智能聊天、文本处理和工具调用等功能。

## 概述

AI服务作为飞书机器人的"大脑"，负责处理用户的自然语言输入，通过集成OpenAI模型和丰富的工具系统，为用户提供智能化的对话体验。

### 主要功能

- **AI聊天服务**：支持流式对话，集成"赤尾"角色设定，提供拟人化的聊天体验
- **工具调用系统**：内置多种工具，支持网络搜索、数学计算、时间查询等功能
- **文本提取服务**：提供批量文本实体提取功能
- **事件系统**：支持跨服务通信和分布式处理
- **内存服务**：集成记忆检索，提供上下文感知的对话体验

## 系统架构

### 整体架构图

```mermaid
graph TB
    subgraph "客户端层"
        A[飞书客户端] --> B[Main Server]
        C[Web界面] --> B
        D[API客户端] --> B
    end
    
    subgraph "服务层"
        B --> E[AI Service]
        E --> F[Memory Service]
        E --> G[Search Service]
    end
    
    subgraph "AI Service 核心组件"
        E --> H[API Layer]
        H --> I[Chat Service]
        H --> J[Extraction Service]
        
        I --> K[Message Context]
        I --> L[Model Service]
        I --> M[Tool Manager]
        
        L --> O[OpenAI Client]
        M --> P[Builtin Tools]
    end
    
    subgraph "数据存储层"
        Q[PostgreSQL]
        R[Redis]
        S[Qdrant Vector DB]
        T[Memory Service DB]
    end
    
    subgraph "外部服务"
        U[OpenAI API]
        V[Search APIs]
        W[其他工具APIs]
    end
    
    N --> F
    F --> T
    O --> U
    P --> V
    P --> W
    
    E --> Q
    E --> R
    E --> S
    
    style E fill:#e1f5fe
    style I fill:#f3e5f5
    style M fill:#e8f5e8
```

### 服务内部架构

```mermaid
graph TB
    subgraph "AI Service Internal Architecture"
        subgraph "API层 (FastAPI)"
            A1[Chat Router] --> A2[SSE Streaming]
            A3[Extraction Router] --> A4[Batch Processing]
            A5[Health Check] --> A6[Service Status]
        end
        
        subgraph "服务层"
            B1[Chat Service] --> B2[Message Processing]
            B3[Model Service] --> B4[OpenAI Integration]
            B5[Context Service] --> B6[Memory Integration]
        end
        
        subgraph "工具系统"
            C1[Tool Manager] --> C2[Tool Registry]
            C3[Schema Generator] --> C4[Function Schemas]
            C5[Builtin Tools] --> C6[Custom Tools]
        end
        
        subgraph "事件系统"
            D1[Event System] --> D2[Redis Pub/Sub]
            D3[Group Stream] --> D4[Message Queue]
            D5[Event Handler] --> D6[Response Future]
        end
        
        subgraph "数据访问层"
            E1[ORM Models] --> E2[CRUD Operations]
            E3[Qdrant Client] --> E4[Vector Search]
        end
    end
    
    A1 --> B1
    A3 --> B1
    B1 --> C1
    B1 --> D1
    B1 --> E1
    B3 --> E3
    B5 --> E5
    
    style B1 fill:#e3f2fd
    style C1 fill:#f1f8e9
    style D1 fill:#fce4ec
    style E1 fill:#fff3e0
```

## 技术栈

- **框架**：FastAPI
- **AI模型**：OpenAI GPT系列
- **数据库**：PostgreSQL（ORM）、Qdrant（向量检索）
- **通信**：Redis（事件系统）
- **工具系统**：基于装饰器的工具注册和管理
- **工作流引擎**：LangGraph

## 数据流图

### 聊天处理流程

```mermaid
sequenceDiagram
    participant Client as 客户端
    participant API as API Layer
    participant Chat as Chat Service
    participant Context as Message Context
    participant Memory as Memory Service
    participant Model as Model Service
    participant Tools as Tool Manager
    participant OpenAI as OpenAI API
    
    Client->>API: POST /chat/sse
    API->>Chat: 创建聊天会话
    Chat->>Context: 构建消息上下文
    Context->>Memory: 获取历史对话
    Memory-->>Context: 返回上下文数据
    Context-->>Chat: 构建完整上下文
    
    Chat->>Model: 流式生成回复
    Model->>OpenAI: 发送请求
    
    loop 流式响应
        OpenAI-->>Model: 返回chunk
        Model-->>Chat: 处理chunk
        
        alt 工具调用
            Chat->>Tools: 执行工具
            Tools-->>Chat: 返回结果
            Chat->>Model: 继续生成
        end
        
        Chat-->>API: 发送chunk
        API-->>Client: SSE推送
    end
    
    Chat->>Memory: 保存对话记录
```

### 工具调用流程

```mermaid
flowchart TD
    A[用户消息] --> B[消息解析]
    B --> C{需要工具调用?}
    
    C -->|是| D[解析工具调用]
    C -->|否| E[直接生成回复]
    
    D --> F[查找工具]
    F --> G{工具存在?}
    
    G -->|是| H[验证参数]
    G -->|否| I[返回错误]
    
    H --> J{参数有效?}
    J -->|是| K[执行工具]
    J -->|否| L[参数错误]
    
    K --> M[获取工具结果]
    M --> N[构建回复上下文]
    N --> O[生成最终回复]
    
    E --> O
    I --> O
    L --> O
    O --> P[返回结果]
    
    style K fill:#e8f5e8
    style O fill:#e3f2fd
```

### 事件系统架构

```mermaid
graph TB
    subgraph "事件发布者"
        A[Chat Service]
        B[Tool Manager]
        C[External Service]
    end
    
    subgraph "事件系统核心"
        D[Event System]
        E[Redis Pub/Sub]
        F[Group Stream Manager]
    end
    
    subgraph "事件处理模式"
        G[广播模式]
        H[请求-响应模式]
        I[分组顺序消费]
    end
    
    subgraph "事件订阅者"
        J[Memory Service]
        K[Analytics Service]
        L[Notification Service]
    end
    
    A --> D
    B --> D
    C --> D
    
    D --> E
    D --> F
    
    E --> G
    E --> H
    F --> I
    
    G --> J
    G --> K
    G --> L
    
    H --> J
    I --> J
    
    style D fill:#ffecb3
    style E fill:#f3e5f5
    style F fill:#e8f5e8
```

## 项目结构

```
ai-service/app/
├── api/                    # API路由层
│   ├── chat.py            # 聊天API（SSE流式接口）
│   ├── extraction.py      # 文本提取API
│   └── router.py          # 路由汇总和健康检查
├── agents/                # AI代理系统
│   └── bangumi/           # 番剧相关代理
├── config/                # 配置管理
│   ├── config.py          # 主配置文件
│   ├── logging_config.json # 日志配置
│   ├── memory_config.py   # 内存服务配置
│   ├── memory_service.py  # 内存服务实现
│   └── openai_config.py   # OpenAI配置
├── core/                  # 核心功能模块
│   ├── clients/           # 外部客户端
│   │   └── openai.py      # OpenAI客户端封装
│   ├── event_system.py    # 事件系统核心
│   ├── events.py          # 事件处理和初始化
│   ├── group_stream.py    # 分组流式处理
│   └── example_*.py       # 示例代码
├── langgraph_infra/       # LangGraph工作流引擎
│   ├── model_builder.py   # 模型构建器
│   └── exceptions.py      # 异常处理
├── orm/                   # 数据访问层
│   ├── base.py            # 基础ORM配置
│   ├── crud.py            # CRUD操作
│   └── models.py          # 数据模型定义
├── services/              # 业务服务层
│   ├── chat/              # 聊天服务
│   │   ├── context.py     # 消息上下文管理
│   │   ├── message.py     # 消息处理核心
│   │   ├── model.py       # 模型服务
│   │   ├── prompt.py      # 提示词管理
│   │   └── prompt.md      # 角色设定文件
│   ├── search/            # 搜索服务
│   │   └── web.py         # 网络搜索工具
│   ├── chat_service.py    # 聊天服务主入口
│   ├── meta_info.py       # 元信息服务
│   └── qdrant.py          # 向量数据库服务
├── tools/                 # 工具系统
│   ├── builtin_tools.py   # 内置工具集合
│   ├── decorators.py      # 工具装饰器
│   ├── manager.py         # 工具管理器
│   ├── registry.py        # 工具注册中心
│   ├── schema_generator.py # Schema生成器
│   ├── startup.py         # 启动初始化
│   └── README.md          # 工具系统详细文档
├── types/                 # 类型定义
│   ├── chat.py            # 聊天相关类型
│   └── memory.py          # 内存相关类型
├── utils/                 # 工具函数
│   ├── decorators/        # 装饰器工具
│   ├── logger/            # 日志工具
│   ├── middlewares/       # 中间件
│   └── split_word.py      # 分词工具
└── main.py                # 应用入口
```

## 核心模块详解

### API层 (api/)

```mermaid
graph LR
    subgraph "API路由层"
        A[FastAPI App] --> B[API Router]
        B --> C[Chat Router]
        B --> D[Extraction Router]
        B --> E[Health Router]
        
        C --> F[SSE Streaming]
        D --> G[Batch Processing]
        E --> H[Status Check]
    end
    
    style A fill:#e1f5fe
    style C fill:#f3e5f5
    style D fill:#e8f5e8
```

**聊天API (chat.py)**

- `/chat/sse`: SSE流式聊天接口，支持实时对话
- 支持工具调用和多轮对话
- 集成状态机管理对话流程

**文本提取API (extraction.py)**

- `/extract_batch`: 批量文本实体提取
- 支持自定义模型和参数配置

### 聊天服务架构

```mermaid
graph TB
    subgraph "聊天服务层"
        A[Chat Service] --> B[Message Processing]
        A --> C[Context Management]
        A --> D[Model Integration]
        
        B --> E[AIChatService]
        C --> F[MessageContext]
        D --> G[ModelService]
        
        E --> H[Stream Processing]
        F --> I[Memory Retrieval]
        G --> J[OpenAI Client]
    end
    
    subgraph "外部依赖"
        K[Memory Service]
        L[Tool Manager]
        M[Redis Lock]
    end
    
    I --> K
    H --> L
    E --> M
    
    style A fill:#e3f2fd
    style E fill:#f1f8e9
    style F fill:#fce4ec
```

**核心组件**

- `message.py`: 聊天消息处理核心，支持流式响应和工具调用
- `context.py`: 消息上下文管理，处理对话历史和记忆检索
- `model.py`: 模型服务，管理OpenAI客户端和请求
- `prompt.py`: 提示词管理，支持动态提示词生成

**角色设定**

- 内置"赤尾"角色，一个活泼可爱的AI助手
- 支持多轮对话、工具调用和情感化交互

### 工具系统架构

```mermaid
graph TB
    subgraph "工具系统"
        A[Tool Manager] --> B[Tool Registry]
        A --> C[Schema Generator]
        A --> D[Tool Executor]
        
        B --> E[Builtin Tools]
        B --> F[Custom Tools]
        
        C --> G[Function Schema]
        C --> H[Parameter Validation]
        
        D --> I[Sync Tools]
        D --> J[Async Tools]
    end
    
    subgraph "工具类型"
        K[数学计算]
        L[网络搜索]
        M[时间查询]
        N[文本处理]
        O[数据转换]
    end
    
    E --> K
    E --> L
    E --> M
    E --> N
    E --> O
    
    style A fill:#e8f5e8
    style B fill:#fff3e0
    style C fill:#f3e5f5
```

基于装饰器的工具注册和管理系统，详细文档请参考 [tools/README.md](tools/README.md)

**特性**

- 装饰器自动注册：`@tool`装饰器
- 自动Schema生成：从函数签名生成OpenAI函数调用schema
- 类型安全：基于Python类型注解
- 异步支持：同时支持同步和异步工具

**内置工具**

- 网络搜索、数学计算、时间查询
- 文本分析、数据格式转换
- 工具帮助和动态扩展

### 事件系统

```mermaid
graph TB
    subgraph "事件系统架构"
        A[Event System] --> B[Event Publisher]
        A --> C[Event Subscriber]
        A --> D[Event Router]
        
        B --> E[Broadcast Events]
        B --> F[Request-Response]
        B --> G[Group Stream]
        
        C --> H[Event Handlers]
        C --> I[Response Futures]
        
        D --> J[Redis Channels]
        D --> K[Message Queue]
    end
    
    subgraph "事件类型"
        L[Chat Events]
        M[Tool Events]
        N[System Events]
    end
    
    E --> L
    F --> M
    G --> N
    
    style A fill:#ffecb3
    style B fill:#e8f5e8
    style C fill:#f3e5f5
```

支持三种事件模式：

1. **广播模式**：通知类事件，无需响应
2. **请求-响应模式**：需要处理结果的事件
3. **分组顺序消费**：支持分组隔离和顺序处理

### 数据层架构

```mermaid
graph TB
    subgraph "数据访问层"
        A[ORM Layer] --> B[PostgreSQL]
        C[Vector Layer] --> D[Qdrant]
        E[Cache Layer] --> F[Redis]
        G[Memory Layer] --> H[Memory Service]
    end
    
    subgraph "数据模型"
        I[Chat Models]
        J[User Models]
        K[Vector Models]
        L[Cache Models]
    end
    
    A --> I
    A --> J
    C --> K
    E --> L
    
    subgraph "数据操作"
        M[CRUD Operations]
        N[Vector Search]
        O[Cache Operations]
        P[Memory Operations]
    end
    
    B --> M
    D --> N
    F --> O
    H --> P
    
    style A fill:#e3f2fd
    style C fill:#f1f8e9
    style E fill:#fce4ec
    style G fill:#fff3e0
```

## 配置说明

### 环境变量

```bash

# 数据库配置
POSTGRES_USER=your_db_user
POSTGRES_PASSWORD=your_db_password
POSTGRES_DB=your_db_name
POSTGRES_HOST=localhost

# Redis配置
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=your_redis_password

# 内存服务配置
MEMORY_BASE_URL=http://localhost:8002

# 搜索服务配置
SEARCH_API_KEY=your_search_key
```

### 配置文件

- `config/config.py`: 主配置文件，使用pydantic_settings管理
- `config/logging_config.json`: 日志配置
- `config/memory_config.py`: 内存服务配置

## 启动服务

### 开发环境

```bash
# 安装依赖
pip install -r requirements.txt

# 启动服务
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 生产环境

```bash
# 使用Docker
docker build -t ai-service .
docker run -p 8000:8000 ai-service
```

## API文档

启动服务后，访问以下地址查看API文档：

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## 健康检查

```bash
# 检查服务状态
curl http://localhost:8000/health

# 响应示例
{
    "status": "ok",
    "timestamp": "2024-01-01T12:00:00Z",
    "service": "ai-service"
}
```

## 开发指南

### 服务启动流程

```mermaid
graph TD
    A[应用启动] --> B[初始化事件系统]
    B --> C[启动工具系统]
    C --> D[注册API路由]
    D --> E[添加中间件]
    E --> F[服务就绪]
    
    F --> G[接收请求]
    G --> H[处理业务逻辑]
    H --> I[返回响应]
    
    style A fill:#e1f5fe
    style F fill:#e8f5e8
    style I fill:#f3e5f5
```

### 添加新工具

```python
from app.tools import tool

@tool(description="你的工具描述")
async def your_tool(param: str) -> str:
    """
    工具功能说明
    
    Args:
        param: 参数说明
        
    Returns:
        结果说明
    """
    # 实现你的工具逻辑
    return f"处理结果: {param}"
```

### 扩展聊天功能

1. 修改 `services/chat/prompt.md` 调整角色设定
2. 在 `services/chat/context.py` 中扩展上下文处理
3. 在 `services/chat/message.py` 中添加新的消息处理逻辑

### 事件处理

```python
from app.core import get_event_system

# 获取事件系统
event_system = get_event_system()

# 注册事件处理器
@event_system.subscribe("your.event")
async def handle_event(data):
    # 处理事件
    pass

# 发布事件
await event_system.publish("your.event", {"data": "value"})
```

## 性能优化

### 优化策略图

```mermaid
graph TB
    subgraph "性能优化策略"
        A[连接池管理] --> B[OpenAI客户端池]
        C[缓存策略] --> D[工具结果缓存]
        C --> E[模型响应缓存]
        F[异步处理] --> G[并发请求处理]
        F --> H[流式响应]
        I[负载均衡] --> J[服务实例分布]
        I --> K[请求分发]
    end
    
    subgraph "监控指标"
        L[响应时间]
        M[并发数]
        N[错误率]
        O[资源使用率]
    end
    
    B --> L
    D --> L
    G --> M
    H --> M
    J --> N
    K --> O
    
    style A fill:#e3f2fd
    style C fill:#f1f8e9
    style F fill:#fce4ec
    style I fill:#fff3e0
```

- 使用连接池管理OpenAI客户端
- 支持模型缓存和请求合并
- 异步处理提高并发性能
- 工具调用结果缓存

## 监控和日志

- 结构化日志记录
- 请求追踪和性能监控
- 错误处理和重试机制
- 健康检查和服务状态监控

### 监控架构

```mermaid
graph TB
    subgraph "监控系统"
        A[日志收集] --> B[ELK Stack]
        C[指标收集] --> D[Prometheus]
        E[链路追踪] --> F[Jaeger]
        G[健康检查] --> H[Service Health]
    end
    
    subgraph "告警系统"
        I[错误告警]
        J[性能告警]
        K[资源告警]
    end
    
    B --> I
    D --> J
    D --> K
    
    style A fill:#e8f5e8
    style C fill:#f3e5f5
    style E fill:#fff3e0
    style G fill:#ffecb3
```

## 部署架构

```mermaid
graph TB
    subgraph "容器化部署"
        A[Docker Image] --> B[Kubernetes Pod]
        B --> C[Service]
        C --> D[Ingress]
    end
    
    subgraph "服务发现"
        E[Service Registry]
        F[Load Balancer]
        G[Health Check]
    end
    
    subgraph "配置管理"
        H[ConfigMap]
        I[Secret]
        J[Environment]
    end
    
    B --> E
    C --> F
    B --> G
    
    B --> H
    B --> I
    B --> J
    
    style A fill:#e1f5fe
    style E fill:#e8f5e8
    style H fill:#f3e5f5
```

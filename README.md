# wxbot

wxbot 是一个面向企业微信自建应用的智能机器人服务。项目使用 FastAPI 接收企业微信回调，用 LangChain 1.x 构建 Agent 对话能力，并通过 Redis 保存记忆、业务数据和定时任务状态。

## 功能特性

- 企业微信自建应用消息接入，支持文本消息和主动消息发送。
- LangChain 1.x Agent Runtime，支持工具调用、短期记忆、多 Agent 路由和 sub-agent 委托。
- Agent 插件化管理，每个插件拥有独立目录、配置、人设、工具和业务逻辑。
- 内置 GitHub demo 插件：`demo_agent`、`sub_agent_demo`、`ai_schedule_manager`。
- APScheduler 定时任务，支持 Redis 持久化和多进程 leader lock。
- Redis 是必需依赖；Redis 连接或 LangGraph Redis checkpoint 初始化失败时，程序会直接退出。

## 目录结构

```text
app/
  main.py                 FastAPI 入口
  agents/                 Agent runtime 与调度
  agent_plugins/          Agent 插件目录
  common/                 插件基础类与加载器
  config/                 YAML 配置加载
  memory/                 LangGraph Redis checkpointer
  scheduler/              定时任务
  tasks/                  内置定时任务
  tools/                  共享 LangChain tools
  wecom/                  企业微信协议、加解密、消息发送
config.example.yaml       配置示例
docker-compose.yml        Redis Stack 与 wxbot 服务
Dockerfile                容器镜像
main.py                   兼容启动入口
```

GitHub demo 口径建议保留：

- `app/agent_plugins/demo_agent`
- `app/agent_plugins/sub_agent_demo`
- `app/agent_plugins/ai_schedule_manager`
- `app/tasks/morning_job.py`
- `app/tasks/today_60s.py`
- `app/tools/basic.py`
- `app/tools/schedule.py`
- `app/tools/wecom.py`

## 快速开始

1. 创建配置文件：

```bash
cp config.example.yaml config.yaml
```

2. 修改 `config.yaml` 中的企业微信、DashScope 和 Redis 配置。

3. 安装依赖：

```bash
python -m pip install -r requirements.txt
```

4. 启动服务：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 4455
```

服务启动后可访问：

- `GET /health`：查看 Redis、scheduler、agent registry 等状态。
- 企业微信接收消息 URL：`https://your-domain.com/<agent_id>/wechat`。

## 配置说明

每个企业微信应用通过 `wechat.agents.<agent_id>.plugins` 声明当前应用启用的 Agent 插件白名单：

```yaml
wechat:
  agents:
    "1000001":
      name: "Demo Agent"
      agent_id: 1000001
      corp_secret: "YOUR_CORP_SECRET"
      token: "YOUR_TOKEN"
      encoding_aes_key: "YOUR_ENCODING_AES_KEY"
      plugins:
        - demo_agent
        - sub_agent_demo
      fetch_user_info: false
      include_time_info: true
```

`plugins` 表示当前企业微信应用允许加载和使用哪些 Agent 插件。工具绑定不写在这里，而是写在每个插件自己的 `agent_config.yaml` 中。

`demo_agent` 配置了 `sub_agents: [sub_agent_demo]`，因此会获得 `call_agent_sub_agent_demo` 委托工具。`sub_agent_demo` 必须同时写在当前应用的 `plugins` 中，否则不会生成委托工具。

`sub_agent_demo` 也可以独立作为普通 Agent 使用：

```yaml
wechat:
  agents:
    "1000003":
      name: "Sub Agent Demo"
      agent_id: 1000003
      plugins:
        - sub_agent_demo
```

模型配置使用 DashScope 的 OpenAI-compatible 接口：

```yaml
api_keys:
  dashscope: "YOUR_DASHSCOPE_API_KEY"

models:
  dashscope:
    model: "qwen3.6-flash"
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key_config: "dashscope"
    max_tokens: 8192
```

## Agent 插件

每个 Agent 插件位于 `app/agent_plugins/<plugin_key>/`：

```text
app/agent_plugins/demo_agent/
  agent_config.yaml
  tools.py
  logic.py
  __init__.py
```

`agent_config.yaml` 常用字段：

- `key`：插件唯一标识，必须与目录语义一致。
- `status`：`enable` 或 `disable`。
- `category`：插件分类；带 `sub_agents` 的 `category: chat` 插件会优先作为 orchestrator。
- `route_keywords`：没有 orchestrator 时的确定性入口路由关键词。
- `custom_tools`：当前插件 `tools.py` 中暴露的直接工具。
- `global_tools`：`app/tools/` 中的共享工具，例如 `time_query`、`help_text`。
- `sub_agents`：当前 Agent 可委托调用的其他 Agent。
- `system_prompt`：Agent 系统提示词。

内置 demo 插件：

- `demo_agent`：主 demo Agent，演示系统状态、插件加载、上下文和 sub-agent 委托。
- `sub_agent_demo`：用于演示被委托调用的子 Agent，也可以独立作为普通 Agent 使用。
- `ai_schedule_manager`：自然语言创建、查询、删除和修改提醒。

## 路由与工具

消息处理流程默认是先选 Agent，再由 Agent 决定是否调用工具。

如果当前应用启用了带 `sub_agents` 的 `category: chat` 插件，Runtime 会优先选择它作为 orchestrator。orchestrator 会获得 `call_agent_<key>` 形式的 sub-agent 工具，由模型决定是否委托专业 Agent。

sub-agent 必须同时满足：插件存在、`status: enable`、写在当前应用 `plugins` 中、写在 orchestrator 的 `sub_agents` 中。

`custom_tools` 和 `global_tools` 是 Agent 被选中之后可用的工具集合。模型会结合 `system_prompt` 和用户消息自行判断是否调用工具、调用哪个工具，以及如何组织最终回复。

## Redis

Redis 是必需依赖：

- 企业微信 token、去重标记等运行时状态使用 Redis。
- APScheduler 使用 Redis jobstore 和 leader lock。
- LangGraph checkpoint 使用 Redis Stack 的 RedisJSON + RediSearch。

如果 Redis 连接失败，或 LangGraph Redis checkpoint 初始化失败，服务会记录错误并退出。`redis.db` 建议使用 `0`，Redis Stack checkpoint 索引不支持非 0 DB。

## Docker

构建镜像：

```bash
docker build -t wxbot:latest -f Dockerfile .
```

启动服务：

```bash
docker compose up -d
```

`docker-compose.yml` 默认包含 Redis Stack。

## 开发检查

```bash
python -m compileall app main.py
python -c "from app.common.loader import load_agent_plugins; r=load_agent_plugins(); print(sorted(r.plugins.keys()))"
```

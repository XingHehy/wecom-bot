# wxbot

wxbot 是一个面向企业微信自建应用的智能机器人服务。项目使用 FastAPI 接收企业微信回调，用 LangChain 1.x 构建多 Agent 对话能力，并通过 Redis 保存记忆、业务数据和定时任务状态。

## 功能特性

- 企业微信自建应用消息接入，支持文本、图片和主动消息发送。
- LangChain 1.x Agent Runtime，支持工具调用、短期记忆和多 Agent 路由。
- Agent 插件化管理，每个插件拥有独立目录、配置、人设、工具和业务逻辑。
- 内置系统演示插件和定时提醒插件，便于作为 GitHub demo 和二次开发模板。
- APScheduler 定时任务，支持 Redis 持久化和多进程 leader lock。
- Redis 区分 LangGraph checkpoint 与业务 keyspace，便于长期运行和排查。

## 目录结构

```text
app/
  main.py                 FastAPI 入口
  agents/                 Agent runtime 与调度
  agent_plugins/          Agent 插件目录
  common/                 插件基础类与加载器
  config/                 YAML 配置加载
  memory/                 LangGraph checkpointer
  scheduler/              定时任务
  tools/                  全局 LangChain tools
  wecom/                  企业微信协议、加解密、消息发送
config.example.yaml       配置示例
docker-compose.yml        Redis Stack 与 wxbot 服务
Dockerfile                容器镜像
main.py                   兼容启动入口
```

## 快速开始

1. 创建配置文件：

```bash
cp config.example.yaml config.yaml
```

2. 修改 `config.yaml` 中的企业微信、模型、Redis 和业务账号配置。

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
- 企业微信回调地址：按企业微信后台配置指向当前服务的回调接口。

## 配置说明

每个企业微信应用通过 `wechat.agents.<agent_id>.plugins` 绑定 Agent 插件：

```yaml
wechat:
  agents:
    "1000001":
      name: "系统演示"
      agent_id: 1000001
      corp_secret: "YOUR_CORP_SECRET"
      token: "YOUR_TOKEN"
      encoding_aes_key: "YOUR_ENCODING_AES_KEY"
      plugins:
        - demo_conversation
```

`plugins` 表示当前企业微信应用启用哪些 Agent 插件。LangChain 工具绑定不写在这里，而是写在每个插件自己的 `agent_config.yaml` 中：

```yaml
custom_tools:
  - create_reminder
global_tools:
  - time_query
```

模型配置支持 OpenAI-compatible 接口，例如 DashScope 和火山方舟：

```yaml
api_keys:
  dashscope: "YOUR_DASHSCOPE_API_KEY"
  ark: "YOUR_ARK_API_KEY"

models:
  dashscope:
    model: "qwen-plus"
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key_config: "dashscope"

  ark:
    model: "doubao-1-5-pro-256k-250115"
    base_url: "https://ark.cn-beijing.volces.com/api/v3"
    api_key_config: "ark"
```

## Agent 插件

每个 Agent 插件位于 `app/agent_plugins/<plugin_key>/`：

```text
app/agent_plugins/demo_conversation/
  agent_config.yaml       插件元信息、人设、模型参数、路由关键词、工具绑定
  tools.py                插件专属工具
  logic.py                可选路由和自定义逻辑
  __init__.py
```

`agent_config.yaml` 常用字段：

- `key`：插件唯一标识，必须与目录语义一致。
- `status`：`enable` 或 `disable`。
- `category`：插件分类，例如 `utility`、`schedule`。
- `route_keywords`：进入模型前的确定性路由关键词。
- `custom_tools`：当前插件 `tools.py` 中暴露的工具。
- `global_tools`：`app/tools/` 中的共享工具。
- `system_prompt`：Agent 系统提示词。

当前仓库默认保留两个 Agent 插件：

- `demo_conversation`：系统演示插件，展示插件加载、上下文摘要和基础工具调用链路。
- `schedule_manager`：定时提醒插件，展示业务工具调用和 APScheduler 集成。

## 路由与工具

消息处理流程是先选 Agent，再由 Agent 决定是否调用工具。

`route_keywords` 用于入口分流。Runtime 会在调用大模型前，根据当前企业微信应用启用的 `plugins` 计算路由分数，命中关键词时直接把消息交给对应 Agent。它适合提醒、查询、系统状态这类高置信业务入口。

`custom_tools` 和 `global_tools` 是 Agent 被选中之后可用的工具集合。模型会结合 `system_prompt` 和用户消息自行判断是否调用工具、调用哪个工具，以及如何组织最终回复。工具本身不负责选择 Agent。

如果一个企业微信应用只启用一个插件，路由关键词不是必须的；如果启用多个插件，建议为业务插件配置清晰的 `route_keywords`。例如同时启用 `demo_conversation` 和 `schedule_manager` 时，系统状态类消息走 demo，提醒类消息走 schedule。

## Docker

构建镜像：

```bash
docker build -t wxbot:latest -f Dockerfile .
```

启动服务：

```bash
docker compose up -d
```

停止服务：

```bash
docker compose down
```

`docker-compose.yml` 默认包含 Redis Stack。LangGraph Redis checkpoint 需要 RedisJSON 和 RediSearch，生产环境请使用兼容 Redis Stack 的 Redis 服务。

## 开发检查

```bash
python -m compileall app main.py
python -c "import app.main; print(app.main.app.title)"
python -c "from app.common.loader import load_agent_plugins; r=load_agent_plugins(); print(sorted(r.plugins.keys()))"
```

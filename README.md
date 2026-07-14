# X2Feishu Monitor

轮询指定 X 用户的公开帖子，通过可选的 OpenAI 兼容接口生成译文，并使用飞书群自定义机器人 Webhook 推送交互式卡片。服务以单进程方式持续运行，不监听端口；运行时仅使用 `requests` 作为第三方依赖，状态保存在 SQLite。

## 工作流程

1. 首次启动读取目标用户最新帖子，将其 ID 保存为监控基线，不推送历史内容。
2. 按配置间隔调用 `GET /2/users/{id}/tweets`，通过 `since_id` 读取新增帖子。
3. 可选调用翻译服务生成目标语言译文。
4. 将原文、译文、发布时间和原文按钮组合成飞书卡片，从旧到新发送。
5. 每张卡片成功发送后立即保存对应帖子 ID。
6. 网络或外部服务异常时保留原游标，在下个周期继续处理。

默认排除回复和转发。SQLite 数据存储在 Docker 命名卷 `monitor-data` 中，重建或重启容器不会丢失监控进度。

## 服务器要求

- Linux 服务器
- Docker Engine
- Docker Compose V2
- 能够访问 `api.x.com` 与 `open.feishu.cn`

项目不需要开放公网端口。

## 配置

复制环境变量模板：

```bash
cp .env.example .env
```

编辑 `.env`，至少填写以下内容：

```dotenv
X_BEARER_TOKEN=你的App-Only-Bearer-Token
X_USER_ID=目标用户的数字ID
X_USERNAME=目标用户名
FEISHU_WEBHOOK_URL=飞书群自定义机器人V2-WebHook
FEISHU_KEYWORD=机器人安全设置中的关键词
```

主要配置项：

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `POLL_INTERVAL_SECONDS` | `300` | 轮询间隔，最小 30 秒 |
| `REQUEST_TIMEOUT_SECONDS` | `20` | 单次 HTTP 请求超时 |
| `X_INCLUDE_REPLIES` | `false` | 是否推送回复 |
| `X_INCLUDE_RETWEETS` | `false` | 是否推送转发 |
| `X_MAX_RESULTS` | `100` | 每页最大读取数量，允许 5–100 |
| `X_MAX_PAGES_PER_POLL` | `10` | 单轮最大分页数，超过时停止推进游标 |
| `INITIAL_SINCE_ID` | 空 | 可选的首次监控基线；留空时自动读取当前最新帖子 |
| `DISPLAY_UTC_OFFSET` | `+08:00` | 飞书消息中的时间显示偏移 |
| `TRANSLATION_ENABLED` | `false` | 是否启用帖子译文 |
| `TRANSLATION_API_URL` | 空 | OpenAI 兼容的 Chat Completions 完整地址 |
| `TRANSLATION_API_KEY` | 空 | 翻译接口密钥；本地无鉴权接口可留空 |
| `TRANSLATION_MODEL` | 空 | 翻译接口使用的模型名称 |
| `TRANSLATION_TARGET_LANGUAGE` | `简体中文` | 译文目标语言 |
| `TRANSLATION_TIMEOUT_SECONDS` | `30` | 单次翻译请求超时，允许 1–120 秒 |
| `LOG_LEVEL` | `INFO` | 标准日志级别 |

`FEISHU_KEYWORD` 必须与飞书自定义机器人安全设置中的关键词完全一致，程序会把关键词放入每条推送消息。`.env` 包含密钥，已被 Git 忽略，不应上传或发送给他人。

### 启用翻译

翻译接口需要兼容 `POST /v1/chat/completions` 的请求和响应格式。示例配置：

```dotenv
TRANSLATION_ENABLED=true
TRANSLATION_API_URL=https://你的服务地址/v1/chat/completions
TRANSLATION_API_KEY=你的翻译接口密钥
TRANSLATION_MODEL=你的模型名称
TRANSLATION_TARGET_LANGUAGE=简体中文
TRANSLATION_TIMEOUT_SECONDS=30
```

翻译失败不会阻塞推送：卡片仍会包含原文，并显示翻译暂时不可用。翻译成功后、飞书发送失败前的译文会临时保存在 SQLite，下轮重试同一帖子时不会重复调用翻译接口。

## 部署

先验证飞书 Webhook，群内应收到一张连接测试卡片：

```bash
docker compose run --rm monitor --test-feishu
```

飞书群收到连接测试消息后，验证 X API 并建立首次基线：

```bash
docker compose run --rm monitor --once
```

首次单轮执行只保存当前最新帖子，不发送历史帖子。随后启动常驻服务：

```bash
docker compose up -d --build
```

查看运行状态和日志：

```bash
docker compose ps
docker compose logs -f --tail=200 monitor
```

停止服务：

```bash
docker compose down
```

`docker compose down` 会保留 SQLite 命名卷。只有明确需要清空监控进度时才使用 `docker compose down -v`；清空后再次启动会重新建立基线。

## 本地验证

安装依赖并运行测试：

```bash
uv sync --locked
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q src tests
```

## 运行保障

- X 的 GET 请求对连接错误、限流和服务端错误进行有限次数退避重试。
- 飞书 POST 不自动重试，避免 HTTP 响应丢失时立即产生重复消息。
- 翻译失败时降级发送原文卡片，不会阻塞后续帖子。
- 已生成但尚未成功推送的译文会缓存到 SQLite，避免重试产生额外翻译费用。
- 飞书失败时不更新帖子游标，下个周期会再次处理该帖子。
- 每轮结束写入 SQLite 心跳，Docker 健康检查会识别停止工作的主循环。
- 收到 `SIGTERM` 或 `SIGINT` 后安全退出，Docker 配置为 `restart: unless-stopped`。
- 日志不会输出 Bearer Token 或完整飞书 Webhook。

## 接口资料

- [X Get Posts 官方文档](https://docs.x.com/x-api/users/get-posts)
- [飞书自定义机器人使用指南](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)
- [uv Docker 官方指南](https://docs.astral.sh/uv/guides/integration/docker/)

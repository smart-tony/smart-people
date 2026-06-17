# 晨报推送工作台部署说明

## 一、推荐部署方式

使用 Docker Compose 部署：

- `weekly-push-tool`：FastAPI 后端 + 前端静态页面 + Playwright 抓取
- `caddy`：自动 HTTPS + 域名反向代理
- `data/`：保存候选池、URL 缓存、用户工作区自动保存内容
- `config/`：保存来源和 prompt 配置

## 二、服务器准备

推荐配置：

- Ubuntu 22.04 / 24.04
- 2 核 4G 起步
- Docker + Docker Compose
- 一个解析到服务器 IP 的域名

安装 Docker 后，把整个 `weekly-push-tool` 文件夹上传到服务器。

## 三、配置环境变量

复制环境变量模板：

```bash
cp .env.example .env
```

编辑 `.env`：

```text
APP_DOMAIN=你的域名
LLM_API_KEY=你的 DeepSeek 或 OpenAI 兼容 API Key
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
```

确认域名已经解析到服务器公网 IP。

## 四、启动

```bash
docker compose up -d --build
```

查看日志：

```bash
docker compose logs -f weekly-push-tool
```

访问：

```text
https://你的域名
```

健康检查：

```text
https://你的域名/api/health
```

应该看到：

```json
{
  "status": "ok",
  "llm_configured": true
}
```

## 五、用户编辑内容保存在哪里

用户在浏览器里编辑的完整工作区会自动保存到：

```text
data/workspaces/default.json
```

包含：

- 基础信息
- 摘要和结语
- 周报条目
- 推送设置
- 当前来源模块
- 抓取条数
- 候选池
- 候选勾选状态

候选池历史保存在：

```text
data/drafts/
```

URL 处理缓存保存在：

```text
data/cache/processed_urls.txt
```

## 六、更新代码

```bash
docker compose down
docker compose up -d --build
```

`data/` 和 `config/` 是挂载目录，正常更新不会丢。

## 七、验收清单

1. 打开首页能看到“晨报推送工作台”
2. `/api/health` 返回 `status: ok`
3. 修改标题后等待 1 秒，刷新页面，标题仍保留
4. 换浏览器打开同一网址，能加载服务器保存的工作区
5. 点击“来源管理”后可以获取候选
6. 缓存命中导致无新文章时，页面会回退显示最近候选
7. 勾选候选并导入，刷新后条目仍保留
8. 可以导出 HTML
9. 可以复制企微推送 JSON

## 八、后续建议

正式多人使用前建议增加：

- 登录账号
- 按用户隔离 `workspace_id`
- 定期备份 `data/`
- 管理员页面查看历史草稿

# 晨间星闻 Docker 部署指南

## 前置条件

- Docker + Docker Compose（macOS 装 Docker Desktop，Linux 装 docker-ce + docker-compose-plugin）
- DeepSeek API Key（在 https://platform.deepseek.com 获取）
- curl（用于健康检查和 cron 触发）

## 一、快速启动（3 步）

```bash
# 1. 进入项目目录
cd /Users/Shared/weekly-push-tool

# 2. 配置 API Key
cp .env.example .env
# 编辑 .env，把 LLM_API_KEY 改成真实的 DeepSeek key
vim .env

# 3. 启动
docker compose up -d
```

访问：
- 日报页面：http://localhost:8000/daily
- 管理后台：http://localhost:8000/admin
- 健康检查：http://localhost:8000/api/health

## 二、目录结构与持久化

```
weekly-push-tool/
├── data/          ← SQLite 数据库 + 缓存（挂载到容器，持久化）
│   ├── weeks.db
│   ├── drafts/
│   └── cache/
├── config/        ← 配置文件（只读挂载）
│   ├── llm.config.json      LLM 模型配置
│   ├── sources.config.json  抓取源
│   └── prompts.config.json  LLM 提示词
├── static/        ← 静态资源（logo、banner 图片，打包进镜像）
├── Dockerfile
├── docker-compose.yml
└── .env           ← 环境变量（API Key 等敏感信息）
```

## 三、定时抓取

### 方式一：主机 cron（推荐）

在宿主机添加 cron，每 15 分钟触发一次全量抓取：

```bash
crontab -e
```

添加：
```
# 晨间星闻 — 每15分钟抓取
*/15 * * * * curl -s -X POST http://localhost:8000/api/admin/scrape-all >> /tmp/morning-scrape.log 2>&1

# 每天 9:00 发布当天内容
0 9 * * * curl -s -X POST http://localhost:8000/api/admin/publish-all >> /tmp/morning-publish.log 2>&1
```

### 方式二：容器内置自动刷新

服务自带每 2 小时自动刷新（`server.py` 的 `_auto_refresh_loop`），无需额外配置。

### 方式三：容器内 loop 脚本

```bash
# 进入容器，启动持续抓取模式
docker exec -d morning-news python cron_scraper.py --loop
```

## 四、管理操作

### 手动抓取

```bash
# 抓取全部模块
curl -X POST http://localhost:8000/api/admin/scrape-all

# 抓取单个模块
curl -X POST http://localhost:8000/api/admin/scrape/logistics-daily

# 查看状态
curl http://localhost:8000/api/admin/status
```

### 发布今天内容

```bash
# 一键发布（标记今天候选为已发布，前端可见）
curl -X POST http://localhost:8000/api/admin/publish-all
```

### 清理旧数据

```bash
# 清理 7 天前的抓取草稿
curl -X POST "http://localhost:8000/api/admin/clear-drafts?days=7"
```

## 五、推送配置（运小星企微）

> 注意：推送代码尚未实现。当前 `load_publish_config()` 只读取配置但不触发推送。
> 如需企微推送，需要额外开发推送脚本。

预留环境变量（已在 .env.example 中）：

| 变量 | 说明 | 示例 |
|------|------|------|
| PUBLISH_ADMIN_BASE_URL | 运小星 Admin API 地址 | http://bot.by56.com:8000/admin |
| PUBLISH_ADMIN_USER | Admin 用户名 | admin |
| PUBLISH_ADMIN_PASSWORD | Admin 密码 | your-password |
| PUBLISH_DEFAULT_USER_ID | 默认推送用户 ID | 18820271886 |

## 六、访问认证（可选）

设置 Basic Auth 保护管理后台：

```bash
# 编辑 .env
APP_USERNAME=admin
APP_PASSWORD=mypassword123

# 重启生效
docker compose restart
```

不设置则管理后台无密码保护。

## 七、更新代码

```bash
cd /Users/Shared/weekly-push-tool
git pull
docker compose up -d --build    # 重建镜像
```

## 八、常用维护命令

```bash
# 查看日志
docker compose logs -f --tail=50

# 重启服务
docker compose restart

# 停止
docker compose down

# 停止并删除数据（危险！）
docker compose down -v

# 进入容器调试
docker exec -it morning-news bash

# 查看 SQLite 数据
docker exec morning-news sqlite3 /app/data/weeks.db "SELECT COUNT(*) FROM items"
docker exec morning-news sqlite3 /app/data/weeks.db \
  "SELECT task_type, COUNT(*) FROM items WHERE scraped_at LIKE '$(date +%Y-%m-%d)%' GROUP BY task_type"
```

## 九、迁移到其他机器

```bash
# 在旧机器上
cd /Users/Shared/weekly-push-tool
tar czf morning-news-backup.tar.gz data/ config/ .env

# 把备份和代码拷到新机器
scp morning-news-backup.tar.gz user@new-machine:~/weekly-push-tool/
cd ~/weekly-push-tool && tar xzf morning-news-backup.tar.gz

# 启动
docker compose up -d
```

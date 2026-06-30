# 晨间星闻 Docker 镜像
# 构建: docker build -t morning-news .
# 运行: docker run -p 8000:8000 --env-file .env morning-news

FROM python:3.12-slim

LABEL description="晨间星闻 - 百运日报推送工具"

# Playwright 系统依赖（Chromium 渲染 JS 页面用）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖（利用 Docker 缓存层）
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt requests

# Playwright Chromium
RUN playwright install chromium && playwright install-deps chromium

# 复制应用代码
COPY backend/ ./backend/
COPY config/ ./config/
COPY static/ ./static/
COPY *.html ./
COPY cron_scraper.py ./

# 创建数据目录（挂载点）
RUN mkdir -p /app/data/drafts /app/data/cache

EXPOSE 8000

# 环境变量默认值
ENV APP_HOST=0.0.0.0
ENV PORT=8000

CMD ["python", "-m", "uvicorn", "backend.server:app", "--host", "0.0.0.0", "--port", "8000"]

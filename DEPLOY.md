# 百运周报素材看板 · 部署指南

## 服务器信息

- 地址: `root@120.79.167.211`
- 目录: `/opt/weekly-push-tool`
- 端口: `8000`

## 一、首次部署

```bash
# 1. SSH 到服务器
ssh root@120.79.167.211

# 2. 克隆代码（首次）或拉取更新
cd /opt
git clone https://github.com/smart-tony/smart-people.git weekly-push-tool
# 或: git clone <你的git地址> weekly-push-tool

# 3. 安装依赖
cd /opt/weekly-push-tool
pip install -r backend/requirements.txt
playwright install chromium --with-deps

# 4. 配置 API Key（如果需要物流源的 LLM 分析）
cp config/llm.config.example.json config/llm.config.json
# 编辑 config/llm.config.json 填入 DeepSeek API Key

# 5. 启动
cd backend
uvicorn server:app --host 0.0.0.0 --port 8000
```

## 二、生产运行（systemd 保活）

```bash
# 创建服务文件
cat > /etc/systemd/system/weekly-briefing.service << 'EOF'
[Unit]
Description=百运周报素材看板
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/weekly-push-tool/backend
ExecStart=/usr/bin/python3 -m uvicorn server:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# 启用并启动
systemctl daemon-reload
systemctl enable weekly-briefing
systemctl start weekly-briefing
systemctl status weekly-briefing
```

## 三、Nginx 反向代理（用域名访问）

```bash
cat > /etc/nginx/sites-available/weekly-briefing << 'EOF'
server {
    listen 80;
    server_name 你的域名.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
EOF

ln -s /etc/nginx/sites-available/weekly-briefing /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

## 四、更新代码

```bash
ssh root@120.79.167.211
cd /opt/weekly-push-tool
git pull
systemctl restart weekly-briefing
```

## 五、验证

```bash
curl http://localhost:8000/api/health
# → {"status":"ok"}

curl http://localhost:8000/api/briefing | head
# → 返回 50 条 AI 精选
```

## 六、只用 IP 访问的话

直接访问: `http://120.79.167.211:8000/briefing`

需要开放防火墙端口:
```bash
ufw allow 8000
```

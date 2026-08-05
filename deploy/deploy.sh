#!/bin/bash
set -e

# ============================================
# 嘟嘟哒 2.0 - 阿里云服务器一键部署脚本
# ============================================

echo "========================================"
echo "  Dududa 2.0 Server Deployment"
echo "========================================"
echo ""

# --------------- 检查环境 ----------------
if ! command -v docker &> /dev/null; then
    echo "[1/6] Installing Docker..."
    curl -fsSL https://get.docker.com | bash
    systemctl enable docker
    systemctl start docker
else
    echo "[1/6] Docker already installed"
fi

if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
    echo "  Installing docker-compose..."
    apt-get update && apt-get install -y docker-compose-plugin
fi

# --------------- 配置域名 ----------------
echo ""
echo "[2/6] Domain configuration"
if [ ! -f .env ]; then
    cp .env.example .env
    echo "  Created .env - PLEASE EDIT IT with your domain/QQ info:"
    echo "    nano .env"
    echo ""
    read -p "  Press Enter after editing .env..." _
fi
source .env

# --------------- SSL 证书 ----------------
echo ""
echo "[3/6] SSL certificate for ${DUDUDA_DOMAIN}"

mkdir -p ssl

if [ ! -f "ssl/live/${DUDUDA_DOMAIN}/fullchain.pem" ]; then
    # Standalone certbot
    docker run --rm \
        -v $(pwd)/ssl:/etc/letsencrypt \
        -v $(pwd)/nginx/default.conf:/tmp/nginx.conf:ro \
        -p 80:80 \
        certbot/certbot certonly --standalone \
        --non-interactive --agree-tos \
        --email ${CERT_EMAIL:-admin@${DUDUDA_DOMAIN}} \
        -d ${DUDUDA_DOMAIN}
    
    echo "  SSL certificate obtained!"
else
    echo "  SSL certificate already exists"
fi

# 更新 nginx 配置中的域名
if [ -n "${DUDUDA_DOMAIN}" ] && [ "${DUDUDA_DOMAIN}" != "your-domain.com" ]; then
    # 启用 HTTPS server block
    sed -i "s/your-domain.com/${DUDUDA_DOMAIN}/g" nginx/default.conf
    sed -i 's/# server {/server {/' nginx/default.conf
    sed -i 's/#     listen 443/    listen 443/' nginx/default.conf
    sed -i 's/#     ssl_certificate/    ssl_certificate/' nginx/default.conf
    sed -i 's/#     ssl_certificate_key/    ssl_certificate_key/' nginx/default.conf
    sed -i 's/#     location/    location/' nginx/default.conf
    sed -i 's/#         proxy_pass/        proxy_pass/' nginx/default.conf
    sed -i 's/#         proxy_set_header/        proxy_set_header/' nginx/default.conf
    sed -i 's/#     }/    }/' nginx/default.conf
    sed -i 's/# }/}/' nginx/default.conf
    echo "  Nginx HTTPS enabled for ${DUDUDA_DOMAIN}"
fi

# --------------- 构建 & 启动 ----------------
echo ""
echo "[4/6] Building Docker image..."
docker compose build

echo ""
echo "[5/6] Starting services..."
docker compose up -d

# --------------- 安装 AstrBot + QQ 协议 ----------------
echo ""
echo "[6/6] Setting up QQ bot (AstrBot + Lagrange)"

# Install AstrBot on host
if ! command -v astrbot &> /dev/null; then
    pip install astrbot
fi

# Init AstrBot config
if [ ! -d ~/.astrbot ]; then
    astrbot start &
    ASTRBOT_PID=$!
    sleep 5
    kill $ASTRBOT_PID 2>/dev/null || true
fi

# Copy Dududa plugin
rm -rf ~/.astrbot/plugins/dududa20 2>/dev/null || true
cp -r ../packages/adapters/astrbot ~/.astrbot/plugins/dududa20

# Copy config
cp astrbot/config.yaml ~/.astrbot/config.yaml
cp astrbot/metadata.yaml ~/.astrbot/plugins/dududa20/metadata.yaml

# --------------- 完成 ----------------
echo ""
echo "========================================"
echo "  Deployment Complete!"
echo "========================================"
echo ""
echo "  Control Plane: http://${DUDUDA_DOMAIN:-localhost:8000}"
echo "  Health check:  http://${DUDUDA_DOMAIN:-localhost:8000}/health"
echo ""
echo "  Next steps:"
echo "    1. Login QQ: astrbot login --platform lagrange"
echo "    2. Start bot: astrbot start"
echo "    3. Test in your QQ group!"
echo ""
echo "  Logs: docker compose logs -f"
echo "  Stop: docker compose down"
echo "  Restart: docker compose restart"
echo ""
set -e
echo "========================================"
echo "  Dududa 2.0 - Native Deployment"
echo "========================================"

echo ""
echo "[1/6] Installing Python 3.12 (15~20 min)..."
sudo dnf install -y gcc openssl-devel bzip2-devel libffi-devel zlib-devel readline-devel sqlite-devel wget
cd /tmp
wget https://mirrors.aliyun.com/python/3.12.9/Python-3.12.9.tgz
tar -xzf Python-3.12.9.tgz
cd Python-3.12.9
./configure --enable-optimizations --prefix=/usr/local/python3.12
make -j$(nproc)
sudo make altinstall
sudo ln -sf /usr/local/python3.12/bin/python3.12 /usr/local/bin/python3.12
sudo ln -sf /usr/local/python3.12/bin/pip3.12 /usr/local/bin/pip3.12

echo ""
echo "[2/6] Installing Nginx..."
sudo dnf install -y nginx
sudo systemctl enable nginx

echo ""
echo "[3/6] Installing Python deps..."
sudo pip3.12 install fastapi "uvicorn[standard]" pydantic

echo ""
echo "[4/6] systemd service..."
sudo tee /etc/systemd/system/dududa20.service > /dev/null << 'UNIT'
[Unit]
Description=Dududa 2.0 Control Plane
After=network.target
[Service]
Type=simple
User=admin
WorkingDirectory=/opt/dududa20-prototype
ExecStart=/usr/local/bin/python3.12 -c "from packages.control_plane import run_server; run_server(host='127.0.0.1', port=8000)"
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
[Install]
WantedBy=multi-user.target
UNIT

sudo tee /etc/nginx/conf.d/dududa20.conf > /dev/null << 'NGX'
server {
    listen 80;
    server_name _;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 120s;
        proxy_buffering off;
    }
}
NGX

echo ""
echo "[5/6] Starting..."
sudo systemctl daemon-reload
sudo systemctl enable dududa20
sudo systemctl start dududa20
sudo systemctl restart nginx

echo ""
echo "[6/6] QQ bot (AstrBot)..."
sudo pip3.12 install astrbot
mkdir -p /home/admin/.astrbot
rm -rf /home/admin/.astrbot/plugins/dududa20 2>/dev/null || true
cp -r /opt/dududa20-prototype/packages/adapters/astrbot /home/admin/.astrbot/plugins/dududa20
cp /opt/dududa20-prototype/deploy/astrbot/metadata.yaml /home/admin/.astrbot/plugins/dududa20/

echo ""
echo "========================================"
echo "  DONE!"
echo "========================================"
echo ""
echo "  验证: curl http://localhost:8000/health"
echo "  状态: sudo systemctl status dududa20"
echo "  日志: sudo journalctl -u dududa20 -f"
echo ""
echo "  QQ 登录: astrbot login --platform lagrange"
echo "  QQ 启动: astrbot start"

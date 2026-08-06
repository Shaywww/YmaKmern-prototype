#!/bin/bash
# Dududa 管理面防火墙收敛（文档清单第 7 项）：
#   6185 AstrBot Dashboard（宿主进程，INPUT 链）
#   3001/6099 NapCat（podman 发布端口，raw PREROUTING 在 DNAT 前拦截，防容器 IP 漂移）
# 仅放行受信来源：本机回环 / 私网 / CGNAT / 运维公网 IP；其余一律 DROP。
# 幂等：可重复执行。
set -u

TRUSTED=(127.0.0.1 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 27.227.43.186/32)

# ---------- 1) INPUT：6185（AstrBot Dashboard 宿主进程） ----------
iptables -N DUDUDA-INPUT 2>/dev/null
iptables -F DUDUDA-INPUT
for s in "${TRUSTED[@]}"; do
  iptables -A DUDUDA-INPUT -s "$s" -j ACCEPT
done
iptables -A DUDUDA-INPUT -j DROP
iptables -D INPUT -p tcp -m multiport --dports 6185,3001,6099 -j DUDUDA-INPUT 2>/dev/null
iptables -I INPUT 1 -p tcp -m multiport --dports 6185,3001,6099 -j DUDUDA-INPUT

# ---------- 2) raw PREROUTING：3001/6099 在 DNAT 之前拦截（nat 表禁 DROP） ----------
iptables -t raw -N DUDUDA-PRE 2>/dev/null
iptables -t raw -F DUDUDA-PRE
for s in "${TRUSTED[@]}" 10.88.0.0/16; do
  iptables -t raw -A DUDUDA-PRE -s "$s" -j RETURN
done
iptables -t raw -A DUDUDA-PRE -j DROP
# 清掉 nat 表旧残留
iptables -t nat -D PREROUTING -p tcp -m multiport --dports 3001,6099 -j DUDUDA-PRE 2>/dev/null
iptables -t nat -F DUDUDA-PRE 2>/dev/null
iptables -t nat -X DUDUDA-PRE 2>/dev/null
iptables -t raw -D PREROUTING -p tcp -m multiport --dports 3001,6099 -j DUDUDA-PRE 2>/dev/null
iptables -t raw -I PREROUTING 1 -p tcp -m multiport --dports 3001,6099 -j DUDUDA-PRE

# ---------- 3) FORWARD：纵深防御（任意容器 IP，podman 子网内） ----------
iptables -N DUDUDA-FWD 2>/dev/null
iptables -F DUDUDA-FWD
for s in "${TRUSTED[@]}" 10.88.0.0/16; do
  iptables -A DUDUDA-FWD -s "$s" -j ACCEPT
done
iptables -A DUDUDA-FWD -j DROP
iptables -D FORWARD -p tcp -d 10.88.0.2 -m multiport --dports 3001,6099 -j DUDUDA-FWD 2>/dev/null
iptables -D FORWARD -p tcp -d 10.88.0.2 -m multiport --dports 6185,3001,6099 -j DUDUDA-FWD 2>/dev/null
iptables -D FORWARD -p tcp -d 10.88.0.0/16 -m multiport --dports 3001,6099 -j DUDUDA-FWD 2>/dev/null
iptables -I FORWARD 1 -p tcp -d 10.88.0.0/16 -m multiport --dports 3001,6099 -j DUDUDA-FWD

echo "DUDUDA-FW OK"

#!/bin/bash
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${YELLOW}=== Локальный запуск агрегатора (БЕЗ ботов) ===${NC}"
echo -e "${GREEN}Продакшен боты продолжают работать — конфликтов не будет${NC}"
echo ""

# 1. Проверка локальной PostgreSQL
if ! docker ps --format '{{.Names}}' | grep -q "manager_bot_postgres"; then
    echo -e "${YELLOW}[*] Запускаю локальный PostgreSQL...${NC}"
    docker-compose -f docker-compose.local.yml up -d
    sleep 3
fi

# 2. SSH туннель для MinIO
if ! lsof -Pi :9000 -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo -e "${YELLOW}[*] Открываю SSH туннель для MinIO (порт 9000)...${NC}"
    cat > /tmp/minio_tunnel_safe.exp << 'EOF'
#!/usr/bin/expect -f
set timeout -1
spawn ssh -o StrictHostKeyChecking=no -N -L 9000:localhost:9000 root@46.173.25.54
expect "password:"
send "cJgwQW5O%oGL\r"
expect eof
EOF
    chmod +x /tmp/minio_tunnel_safe.exp
    /usr/bin/expect -f /tmp/minio_tunnel_safe.exp >/dev/null 2>&1 &
    SSH_TUNNEL_PID=$!
    sleep 3
    echo -e "${GREEN}[+] SSH туннель запущен (PID: $SSH_TUNNEL_PID)${NC}"
else
    echo -e "${GREEN}[+] SSH туннель на 9000 уже активен${NC}"
    SSH_TUNNEL_PID=""
fi

# 3. Запуск backend (без ботов!)
echo -e "${YELLOW}[*] Запускаю локальный backend (port 3001, ENABLE_BOTS=false)...${NC}"
cd "$(dirname "$0")"
source .venv/bin/activate
ENABLE_BOTS=false python main.py &
BACKEND_PID=$!
echo -e "${GREEN}[+] Backend PID: $BACKEND_PID${NC}"

# 4. Запуск frontend
echo -e "${YELLOW}[*] Запускаю локальный frontend (port 8080)...${NC}"
cd ../rus-dialog-space
npm run dev &
FRONTEND_PID=$!
echo -e "${GREEN}[+] Frontend PID: $FRONTEND_PID${NC}"

echo ""
echo -e "${GREEN}=== Всё запущено! ===${NC}"
echo -e "Frontend: http://localhost:8080"
echo -e "Backend:  http://localhost:3001"
echo -e "API Docs: http://localhost:3001/docs"
echo ""
echo -e "${YELLOW}Важно:${NC}"
echo -e "  • Новые сообщения с Telegram/VK приходят на ПРОДАКШЕН"
echo -e "  • Локально видны только старые данные + моки"
echo -e "  • Чтобы увидеть новые сообщения — обнови страницу или перезапусти скрипт"
echo ""
echo -e "Для остановки нажмите ${YELLOW}Ctrl+C${NC}"

trap 'echo -e "\n${YELLOW}[*] Останавливаю процессы...${NC}"; kill $BACKEND_PID $FRONTEND_PID 2>/dev/null || true; [ -n "$SSH_TUNNEL_PID" ] && kill $SSH_TUNNEL_PID 2>/dev/null || true; exit 0' INT TERM
wait

#!/bin/bash
# 启动本地服务器并打开 SUIT UP
# 摄像头必须在 https:// 或 localhost 下才能访问，所以不能直接双击 index.html
cd "$(dirname "$0")"
PORT="${1:-8080}"

if lsof -ti :"$PORT" >/dev/null 2>&1; then
  echo "端口 $PORT 已被占用，换一个：./start.sh 8090"
  exit 1
fi

echo "SUIT UP 启动中 → http://localhost:$PORT"
echo "（Ctrl+C 停止）"
sleep 0.6 && open "http://localhost:$PORT/index.html" &
python3 -m http.server "$PORT" --bind 127.0.0.1

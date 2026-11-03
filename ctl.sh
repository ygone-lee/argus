#!/bin/sh
# Argus 보드 서버 관리: ./ctl.sh start|stop|restart|status|open
# 훅이 SessionStart·UserPromptSubmit 때 서버가 꺼져 있으면 알아서 띄우므로 평소엔 쓸 일이 없다.
HERE=$(cd "$(dirname "$0")" && pwd)
PORT=${CSL_PORT:-7799}
DATA=${CSL_DATA_DIR:-$HOME/.claude/session-log}
PY=/usr/bin/python3

pid() { lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null; }

case "$1" in
  start)
    if [ -n "$(pid)" ]; then echo "이미 실행 중 (pid $(pid))"; exit 0; fi
    mkdir -p "$DATA"
    nohup "$PY" "$HERE/csl/server.py" >>"$DATA/server.log" 2>&1 &
    sleep 0.5; echo "시작: http://127.0.0.1:$PORT (pid $(pid))" ;;
  stop)
    P=$(pid); if [ -n "$P" ]; then kill $P && echo "종료 (pid $P)"; else echo "실행 중 아님"; fi ;;
  restart)
    "$0" stop; sleep 0.5; "$0" start ;;
  status)
    P=$(pid); if [ -n "$P" ]; then echo "실행 중 (pid $P) http://127.0.0.1:$PORT"; else echo "실행 중 아님"; fi ;;
  open)
    [ -z "$(pid)" ] && "$0" start
    open "http://127.0.0.1:$PORT" ;;
  *)
    echo "사용법: $0 start|stop|restart|status|open"; exit 1 ;;
esac

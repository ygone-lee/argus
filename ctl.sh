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
    if [ -n "$(pid)" ]; then echo "Already running (pid $(pid)): http://127.0.0.1:$PORT"; exit 0; fi
    mkdir -p "$DATA"
    nohup "$PY" "$HERE/csl/server.py" >>"$DATA/server.log" 2>&1 &
    # 떠서 포트를 열 때까지 잠깐 기다린다(최대 5초) — 바로 열면 브라우저가 「연결할 수 없음」을 보였다
    i=0; while [ -z "$(pid)" ] && [ $i -lt 25 ]; do sleep 0.2; i=$((i + 1)); done
    if [ -n "$(pid)" ]; then echo "Started: http://127.0.0.1:$PORT (pid $(pid))"
    else echo "The server didn't start — see the end of $DATA/server.log"; tail -n 5 "$DATA/server.log" 2>/dev/null; exit 1; fi ;;
  stop)
    P=$(pid); if [ -n "$P" ]; then kill $P && echo "Stopped (pid $P)"; else echo "Not running"; fi ;;
  restart)
    "$0" stop; sleep 0.5; "$0" start ;;
  status)
    P=$(pid); if [ -n "$P" ]; then echo "Running (pid $P): http://127.0.0.1:$PORT"; else echo "Not running — start it with: ./ctl.sh open"; fi ;;
  open)
    [ -z "$(pid)" ] && { "$0" start || exit 1; }
    open "http://127.0.0.1:$PORT" ;;
  *)
    echo "Usage: $0 start | stop | restart | status | open"
    echo "  open     start the board if needed and open it in your browser"
    echo "  status   is the board running?"
    exit 1 ;;
esac

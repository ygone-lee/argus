#!/bin/sh
# Argus 명령 하나로: ./ctl.sh open|status|start|stop|restart|autostart|install|check|uninstall|backfill|demo
# 사용자가 치는 명령은 모두 여기로 — 안에서 맥 기본 파이썬(/usr/bin/python3)을 부른다(pyenv·가상환경의 python3 이 훅에 박히지 않게).
# 훅이 SessionStart·UserPromptSubmit 때 서버가 꺼져 있으면 알아서 띄운다. 재부팅 뒤 Claude 세션이 아직 없을 때는
# ./ctl.sh open 으로 직접 띄우거나, ./ctl.sh autostart on 으로 로그인할 때 저절로 뜨게(macOS LaunchAgent) 해 둔다.
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
  autostart)
    case "${2:-status}" in on|off|status) exec "$PY" "$HERE/install.py" --set-autostart "${2:-status}" ;;
      *) echo "Usage: $0 autostart on | off | status"; exit 1 ;; esac ;;
  install)
    shift; exec "$PY" "$HERE/install.py" "$@" ;;
  check)
    exec "$PY" "$HERE/install.py" --check ;;
  uninstall)
    exec "$PY" "$HERE/install.py" --uninstall ;;
  backfill)
    exec "$PY" "$HERE/csl/backfill.py" --days "${2:-7}" ;;
  demo)
    shift; exec "$PY" "$HERE/demo/run.py" "$@" ;;
  *)
    echo "Usage: $0 <command>"
    echo
    echo "  open                      start the board if needed and open it in your browser"
    echo "  status                    is the board running?"
    echo "  start | stop | restart    start, stop or restart the board server"
    echo "  autostart on|off|status   start the board when you log in, stop doing that, or show it"
    echo "  install [options]         add or update Argus's hooks in Claude Code (safe to run again)"
    echo "  check                     show what is installed and running (changes nothing)"
    echo "  uninstall                 remove the hooks and login autostart (your data is kept)"
    echo "  backfill [days]           bring in conversations from before the install (default 7 days)"
    echo "  demo                      open a demo board with made-up sessions at http://127.0.0.1:7798"
    [ -z "$1" ] || [ "$1" = help ] || [ "$1" = -h ] || [ "$1" = --help ] || exit 1 ;;
esac

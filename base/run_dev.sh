#!/usr/bin/env bash
# Dev-запуск воркера ВНУТРИ контейнера:
#   Xvfb + xterm + xeyes  ->  Input Injector (xtest)  ->  Signaling Adapter (ximagesrc)
#
#   cd /worker/workers/base && ./run_dev.sh
#
# Потом открой http://localhost:9090, нажми WASD (появятся буквы в xterm),
# кликни по видео (захват мыши) и води мышью (глаза xeyes следят за курсором).
# Остановка: Ctrl+C (все фоновые процессы завершаются).
#
# Переменные: CG_DISPLAY (по умолчанию :99), CG_RESOLUTION (1280x720x24), CAPTURE_FPS (30).
set -uo pipefail
cd "$(dirname "$0")"

DISPLAY_ID="${CG_DISPLAY:-:99}"
RESOLUTION="${CG_RESOLUTION:-1280x720x24}"
NUM="${DISPLAY_ID#:}"

if [ "$DISPLAY_ID" = ":0" ]; then
    echo "CG_DISPLAY=:0 при --network host может быть экраном хоста. Используй :99." >&2
    exit 1
fi

export DISPLAY="$DISPLAY_ID"
export CAPTURE_DISPLAY="$DISPLAY_ID"
export VIDEO_SOURCE="${VIDEO_SOURCE:-ximagesrc}"

PIDS=()
cleanup() {
    trap - EXIT INT TERM
    for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null; done
    wait 2>/dev/null
}
trap cleanup EXIT INT TERM

# --- убираем хвосты от прошлых запусков (в контейнере это безопасно) ---
pkill -x Xvfb 2>/dev/null
pkill -f '[i]nput_injector.py' 2>/dev/null
pkill -f '[s]ignaling_adapter.py' 2>/dev/null
sleep 0.5
rm -f "/tmp/.X${NUM}-lock" "/tmp/.X11-unix/X${NUM}" /tmp/input.sock

# --- X-сервер ---
Xvfb "$DISPLAY_ID" -screen 0 "$RESOLUTION" -nolisten tcp >/tmp/xvfb.log 2>&1 &
PIDS+=($!)
wait_for_x() {
    for _ in $(seq 1 100); do
        if command -v xdpyinfo >/dev/null 2>&1; then
            xdpyinfo >/dev/null 2>&1 && return 0
        elif [ -S "/tmp/.X11-unix/X${NUM}" ]; then   # xdpyinfo нет (пакет x11-utils) — ждём сокет
            sleep 1
            return 0
        fi
        sleep 0.1
    done
    return 1
}
wait_for_x || { echo "Xvfb не поднялся, см. /tmp/xvfb.log" >&2; exit 1; }
echo "[run_dev] Xvfb $DISPLAY_ID ($RESOLUTION) запущен"

# --- демо-приложения ---
# Без оконного менеджера фокус клавиатуры следует за указателем: пока курсор над xterm,
# WASD печатаются в нём. Курсор стартует в центре экрана — это область xterm.
xterm -font 10x20 -geometry 128x24+0+0 -bg black -fg '#33ff33' -e bash --norc >/tmp/xterm.log 2>&1 &
PIDS+=($!)
xeyes -geometry 320x200+480+500 >/tmp/xeyes.log 2>&1 &
PIDS+=($!)

# --- Input Injector (XTEST: пишет прямо в Xvfb, хост не затрагивается) ---
python3 input_injector.py --backend xtest > >(sed -u 's/^/[injector] /') 2>&1 &
PIDS+=($!)
sleep 1.5

echo "[run_dev] Открой http://localhost:9090"
# --- Signaling Adapter (главный процесс, логи в этот терминал) ---
python3 signaling_adapter.py
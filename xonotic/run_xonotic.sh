#!/usr/bin/env bash
# Dev-запуск воркера с Xonotic (milestone подзадачи 4.Б из дока):
#   Xvfb  ->  Xonotic (llvmpipe, окно во весь экран)  ->  Input Injector (xtest)  ->  Signaling Adapter
#
#   cd /worker && bash run_xonotic.sh
#
# Открой http://localhost:9090, обнови страницу — должно быть видно меню Xonotic.
# Управление: WASD, стрелки, Esc/Enter/Tab, цифры — клавиатурой; клик по видео
# захватывает мышь (курсор двигает пункты меню); Esc отпускает мышь обратно в браузер.
#
# Переменные: CG_DISPLAY (:99), CG_RESOLUTION (1280x720x24), CAPTURE_FPS (30),
#             XONOTIC_DIR (/opt/xonotic), XONOTIC_FULLSCREEN (0|1, по умолчанию 0),
#             XONOTIC_ARGS (дополнительные аргументы командной строки движка).
set -uo pipefail
cd "$(dirname "$0")"

DISPLAY_ID="${CG_DISPLAY:-:99}"
RESOLUTION="${CG_RESOLUTION:-1280x720x24}"
NUM="${DISPLAY_ID#:}"
WIDTH="${RESOLUTION%%x*}"
HEIGHT="$(echo "$RESOLUTION" | cut -d x -f2)"
XONOTIC_DIR="${XONOTIC_DIR:-/opt/xonotic}"
XONOTIC_FULLSCREEN="${XONOTIC_FULLSCREEN:-0}"

if [ "$DISPLAY_ID" = ":0" ]; then
    echo "CG_DISPLAY=:0 при --network host может быть экраном хоста. Используй :99." >&2
    exit 1
fi
if [ ! -d "$XONOTIC_DIR" ]; then
    echo "Не найдена папка игры: $XONOTIC_DIR (образ собран из xonotic-worker?)" >&2
    exit 1
fi

export DISPLAY="$DISPLAY_ID"
export CAPTURE_DISPLAY="$DISPLAY_ID"
export VIDEO_SOURCE="ximagesrc"
export LIBGL_ALWAYS_SOFTWARE=1
export HOME="${XONOTIC_HOME:-/root}"   # ~/.xonotic — конфиги и логи движка

PIDS=()
cleanup() {
    trap - EXIT INT TERM
    for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null; done
    wait 2>/dev/null
}
trap cleanup EXIT INT TERM

# --- убираем хвосты от прошлых запусков (в контейнере это безопасно) ---
pkill -x Xvfb 2>/dev/null
pkill -f 'xonotic-linux' 2>/dev/null
pkill -f '[i]nput_injector.py' 2>/dev/null
pkill -f '[s]ignaling_adapter.py' 2>/dev/null
sleep 0.5
rm -f "/tmp/.X${NUM}-lock" "/tmp/.X11-unix/X${NUM}" /tmp/input.sock

# --- X-сервер: +extension GLX на некоторых сборках Xvfb выключен по умолчанию ---
Xvfb "$DISPLAY_ID" -screen 0 "$RESOLUTION" +extension GLX +render -nolisten tcp >/tmp/xvfb.log 2>&1 &
PIDS+=($!)
for _ in $(seq 1 50); do
    xdpyinfo >/dev/null 2>&1 && break
    sleep 0.1
done
xdpyinfo >/dev/null 2>&1 || { echo "Xvfb не поднялся, см. /tmp/xvfb.log" >&2; exit 1; }
echo "[run_xonotic] Xvfb $DISPLAY_ID ($RESOLUTION) запущен"

if command -v glxinfo >/dev/null 2>&1; then
    RENDERER="$(DISPLAY=$DISPLAY_ID glxinfo -B 2>/dev/null | grep 'OpenGL renderer' || true)"
    echo "[run_xonotic] $RENDERER"
    case "$RENDERER" in
        *llvmpipe*|*softpipe*|*swrast*) : ;;  # ожидаемо: программный рендер
        "") echo "[run_xonotic] ВНИМАНИЕ: glxinfo не вернул renderer — GLX может не работать" >&2 ;;
        *) echo "[run_xonotic] ВНИМАНИЕ: рендерер не похож на software (см. строку выше)" >&2 ;;
    esac
fi

# --- ищем бинарь движка: сначала .sh-обёртку (сама выбирает 32/64 бит), потом сырой 64-бит ---
BIN=""
for candidate in xonotic-linux-glx.sh xonotic-linux64-glx xonotic-linux-sdl.sh xonotic-linux64-sdl; do
    if [ -x "$XONOTIC_DIR/$candidate" ]; then BIN="$XONOTIC_DIR/$candidate"; break; fi
done
if [ -z "$BIN" ]; then
    echo "Не нашёл исполняемый файл движка в $XONOTIC_DIR (xonotic-linux*-glx/sdl)." >&2
    echo "Содержимое папки:" >&2
    ls -la "$XONOTIC_DIR" >&2
    exit 1
fi
echo "[run_xonotic] Бинарь движка: $BIN"

# --- Xonotic: окно без рамки (нет WM — окно просто рисуется с 0,0) точно под разрешение
# Xvfb. vid_fullscreen оставляем выключенным по умолчанию: XRandR-переключение режима
# в Xvfb не гарантировано, а windowed-режим не требует modeset вообще.
"$BIN" -nosound -basedir "$XONOTIC_DIR" \
    +vid_fullscreen "$XONOTIC_FULLSCREEN" +vid_width "$WIDTH" +vid_height "$HEIGHT" \
    ${XONOTIC_ARGS:-} > >(sed -u 's/^/[xonotic] /') 2>&1 &
PIDS+=($!)

echo "[run_xonotic] Жду первый кадр от движка (загрузка pk3 может занять 10-60с)..."
for _ in $(seq 1 200); do
    kill -0 "${PIDS[-1]}" 2>/dev/null || { echo "Движок упал при старте, см. лог выше" >&2; exit 1; }
    xwininfo -root -tree -display "$DISPLAY_ID" 2>/dev/null | grep -qi xonotic && break
    sleep 0.3
done

# --- Input Injector (XTEST: пишет прямо в Xvfb, хост не затрагивается) ---
python3 input_injector.py --backend xtest > >(sed -u 's/^/[injector] /') 2>&1 &
PIDS+=($!)
sleep 1.5

echo "[run_xonotic] Открой http://localhost:9090"
# --- Signaling Adapter (главный процесс, логи в этот терминал) ---
python3 signaling_adapter.py
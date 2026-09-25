# workers/xonotic/Dockerfile — подзадача 4.А: образ на базе gstreamer-worker-base
# с установленной игрой Xonotic (раздел 12.4 архитектурного документа).
#
# Сборка (из папки workers/xonotic, где лежит xonotic-0.8.6.zip):
#   docker build --build-arg BASE_IMAGE=gst-test -t xonotic-worker .
#
# Запуск (тот же набор флагов, что для base-образа):
#   docker run --rm -it --network host --device /dev/uinput --cap-add SYS_ADMIN \
#       -v "$(pwd):/worker/workers/xonotic" xonotic-worker bash
#   -> внутри контейнера: bash run_xonotic.sh
#
# GPU не используется: рендер идёт через Mesa llvmpipe (CPU, LIBGL_ALWAYS_SOFTWARE=1).
# Это медленно, но не требует ни /dev/dri, ни драйверов — годится для проверки, что
# игра вообще запускается и реагирует на наш ввод (XTEST). Для играбельной скорости
# в prod-профиле понадобится проброс GPU — это отдельная задача.

ARG BASE_IMAGE=gst-test
FROM ${BASE_IMAGE}

# Рантайм-зависимости Linux-сборки Xonotic (раздел "System requirements" на xonotic.org:
# glibc/libc6 2.29+, libSDL2, libvorbisfile, libcurl, libpng) + Mesa для software-рендера.
# На Ubuntu 24.04 (noble) libcurl4/libpng16-16 переехали под суффикс t64 (time_t-транзишн);
# сами пакеты libcurl4/libpng16-16 остались как транзишн-пустышки, поэтому указываем t64
# напрямую, чтобы сборка не зависела от того, есть ли ещё транзишн-пакет в репах.
RUN apt-get update && apt-get install -y --no-install-recommends \
    unzip \
    libgl1-mesa-dri \
    libglx-mesa0 \
    mesa-utils \
    libsdl2-2.0-0 \
    libvorbisfile3 \
    libcurl4t64 \
    libpng16-16t64 \
    libjpeg-turbo8 \
    && rm -rf /var/lib/apt/lists/*

# Заставляет Mesa сразу использовать llvmpipe (CPU), не пытаясь найти /dev/dri.
# Без этой переменной старт в контейнере без GPU может быть медленнее или падать
# на попытке открыть DRI-устройство, которого нет.
ENV LIBGL_ALWAYS_SOFTWARE=1
ENV GAME_ID=xonotic

# Игру кладёшь сам: скачай xonotic-0.8.6.zip (~1.2 ГБ, https://xonotic.org/download)
# в эту же папку (workers/xonotic/) ДО docker build — архив слишком большой, чтобы
# качать его на каждой сборке, и на многих серверах-зеркалах нет гарантий аптайма.
COPY xonotic-*.zip /tmp/xonotic.zip

# Распаковываем и переносим в /opt/xonotic независимо от точного имени внутренней
# папки в архиве (в разных сборках она называется по-разному: Xonotic/, xonotic-0.8.6/...).
RUN mkdir -p /opt/xonotic-extract \
    && unzip -q /tmp/xonotic.zip -d /opt/xonotic-extract \
    && rm /tmp/xonotic.zip \
    && XON_DIR="$(find /opt/xonotic-extract -mindepth 1 -maxdepth 1 -type d | head -1)" \
    && if [ -z "$XON_DIR" ]; then echo 'В архиве не нашлось папки верхнего уровня' >&2; exit 1; fi \
    && mv "$XON_DIR" /opt/xonotic \
    && rmdir /opt/xonotic-extract \
    && find /opt/xonotic -maxdepth 1 -iname 'xonotic-linux*' -exec chmod +x {} \; \
    && echo '--- /opt/xonotic (верхний уровень) ---' \
    && ls -la /opt/xonotic \
    && echo '--- исполняемые файлы xonotic-linux* ---' \
    && find /opt/xonotic -maxdepth 1 -iname 'xonotic-linux*'

WORKDIR /worker
COPY run_xonotic.sh /worker/run_xonotic.sh

# run_dev.sh (из base-образа) поднимает демо xterm/xeyes; run_xonotic.sh — саму игру.
CMD ["bash"]
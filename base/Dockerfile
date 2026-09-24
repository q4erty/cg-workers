# workers/base/Dockerfile
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV DISPLAY=:0
ENV PULSE_SERVER=unix:/run/pulse/native

RUN apt-get update && apt-get install -y \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    gstreamer1.0-x \
    gstreamer1.0-alsa \
    gstreamer1.0-pulseaudio \
    gstreamer1.0-nice \
    xvfb \
    pulseaudio \
    python3 \
    python3-pip \
    python3-gi \
    python3-gst-1.0 \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0 \
    gir1.2-gst-plugins-bad-1.0 \
    x11-utils \
    xinput \
    xdotool \
    xterm \
    x11-apps \
    xfonts-base \
    && pip3 install --break-system-packages --no-cache-dir \
        fastapi uvicorn evdev python-xlib protobuf grpcio-tools \
    && rm -rf /var/lib/apt/lists/*

# grpcio-tools нужен для генерации input_pb2.py из input.proto
# (input_bridge.load_proto() делает это автоматически при первом запуске).

WORKDIR /worker
COPY signaling_adapter.py input_bridge.py input_injector.py input.proto run_dev.sh /worker/

EXPOSE 9090

# Запуск контейнера для input-injector:
#   docker run --rm -it --network host --device /dev/uinput --cap-add SYS_ADMIN \
#       -v "$(pwd):/worker" gst-test bash
# Dev-запуск всего воркера (Xvfb + xterm/xeyes + инжектор + адаптер): ./run_dev.sh
CMD ["bash"]
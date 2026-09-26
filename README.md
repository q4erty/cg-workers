# Interactive WebRTC Streaming Worker

A headless, containerized worker for cloud gaming and interactive application streaming via WebRTC. The service captures a virtual display, encodes video with ultra-low latency, and injects remote user input back into the virtual environment.

## 🎯 Purpose

The worker acts as a remote execution environment for GUI applications and games. It runs a virtual X11 display server, launches the target application inside it, and streams rendered frames to a web browser in real time. Simultaneously, it receives input events from the browser and translates them into native OS-level or X11-level hardware events, making the remote application behave as if the user is sitting in front of it.

## 🛠 Technology Stack

- **Core Logic:** Python 3
- **Media Processing:** GStreamer (H.264 encoding, WebRTC bin)
- **Virtual Display:** Xvfb (X Virtual Framebuffer)
- **Input Injection:**
    - Linux kernel `uinput` subsystem (evdev)
    - X11 `XTEST` extension (via `python-xlib`)
- **Serialization:** Protocol Buffers (Protobuf)
- **Networking:** WebRTC (peer-to-peer media), HTTP (signaling and input)
- **Containerization:** Docker

## 🏗 Architecture Overview

The service consists of three main pipelines:

### 1. Video Pipeline
Captures the virtual display framebuffer, converts the color space, encodes frames into H.264 with zero-latency tuning, and packages them into RTP payloads for WebRTC delivery.

### 2. Input Pipeline
Receives input states from the client, deserializes them, and injects them into the virtual environment. Tracks the current state of pressed keys and calculates diffs to generate accurate down/up events. Supports two injection backends:
- **Kernel-level:** Creates virtual input devices visible to the entire OS.
- **X11-level:** Injects events directly into the X server, no kernel privileges required.

### 3. Signaling & Bridge Layer
Handles the WebRTC handshake (SDP offer/answer, ICE candidates) to establish the peer-to-peer connection. Exposes an HTTP API to receive input payloads from the browser and forward them to the input pipeline.

## ✨ Key Features

- **Ultra-low latency:** Optimized GStreamer pipelines and WebRTC for real-time interaction.
- **Anti-sticking protection:** Browser-side heartbeat and injector-side watchdog automatically release keys if network packets are lost.
- **State-based input processing:** The injector compares current and previous key states to generate accurate hardware events, regardless of whether the protocol sends states or deltas.
- **Safe testing modes:** Dry-run mode allows testing the full input pipeline without affecting the host system.

## ⚙️ Deployment Concept

The worker is designed to run as a Docker container on a **Linux host**. Deployment requires:

- The `uinput` kernel module loaded on the host (for kernel-level input injection).
- Host network access and passthrough of the `/dev/uinput` device to the container.
- Elevated container capabilities for kernel device interaction.

Detailed deployment instructions and configuration will be provided as the project matures.


```bash
docker run --rm -it --network host --device /dev/uinput --cap-add SYS_ADMIN \
    -v "$(pwd)/workers/xonotic:/worker/workers/xonotic" xonotic-worker bash
```

```bash
cd /worker
bash run_xonotic.sh
```

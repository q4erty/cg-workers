#!/usr/bin/env python3
"""
Общий код Signaling Adapter и Input Injector.

  * load_proto()    — загружает input_pb2; если файла нет (или он от другой версии
                      protobuf), генерирует его из input.proto через grpc_tools.protoc.
  * InputForwarder  — превращает dev-JSON от браузера ({keys_down, keys_up, mouse})
                      в Protobuf PlayerInput и шлёт в Unix DGRAM socket инжектора.

Модель ввода: клавиатура и кнопки мыши передаются СОСТОЯНИЕМ (pressed_keys, button_mask).
InputForwarder хранит текущее состояние пользователя, а Input Injector считает разницу
и сам генерирует key-down / key-up.
"""
import importlib
import logging
import os
import socket
import subprocess
import sys
import threading
import time

log = logging.getLogger('input-bridge')

HERE = os.path.dirname(os.path.abspath(__file__))
SOCK_PATH = os.environ.get('INPUT_SOCK', '/tmp/input.sock')
# Тот же таймаут, что у watchdog инжектора: если клиент молчит дольше — считаем, что он пропал
STALE_AFTER_S = float(os.environ.get('INPUT_HELD_TIMEOUT_S', '3'))


class InputUnavailable(RuntimeError):
    """Input Injector недоступен (не запущен / сокет не принимает)."""


def load_proto():
    """Возвращает модуль input_pb2, при необходимости генерируя его из input.proto."""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    try:
        return importlib.import_module('input_pb2')
    except Exception as first_error:  # нет файла ИЛИ gencode от другой версии protobuf
        log.info('input_pb2 недоступен (%s), генерирую из input.proto', first_error)

    sys.modules.pop('input_pb2', None)
    cmd = [
        sys.executable, '-m', 'grpc_tools.protoc',
        f'-I{HERE}', f'--python_out={HERE}', os.path.join(HERE, 'input.proto'),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        detail = getattr(e, 'stderr', '') or str(e)
        raise RuntimeError(
            'Не удалось сгенерировать input_pb2.py. Нужен пакет grpcio-tools:\n'
            '  pip3 install --break-system-packages grpcio-tools\n'
            f'Команда: {" ".join(cmd)}\n{detail}'
        ) from e
    importlib.invalidate_caches()
    return importlib.import_module('input_pb2')


class InputForwarder:
    """Потокобезопасный отправитель PlayerInput в Unix DGRAM socket инжектора."""

    def __init__(self, sock_path: str = SOCK_PATH):
        self._sock_path = sock_path
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._sock.settimeout(0.5)
        self._lock = threading.Lock()
        self._seq = 0
        self._pressed: set[int] = set()
        self._buttons = 0
        self._last_submit = time.monotonic()
        self._pb = None

    def _proto(self):
        if self._pb is None:
            self._pb = load_proto()
        return self._pb

    def _send(self, msg) -> None:
        self._seq += 1
        msg.sequence = self._seq
        msg.timestamp_ms = int(time.time() * 1000)
        try:
            self._sock.sendto(msg.SerializeToString(), self._sock_path)
        except OSError as e:  # FileNotFoundError, ConnectionRefusedError, timeout...
            raise InputUnavailable(
                f'Input Injector недоступен ({self._sock_path}): {e}. '
                'Запусти: python3 input_injector.py'
            ) from e

    def _send_keyboard(self) -> None:
        msg = self._proto().PlayerInput()
        msg.keyboard.SetInParent()          # без этого пустой snapshot «нет нажатых» не выставит oneof
        msg.keyboard.pressed_keys.extend(sorted(self._pressed))
        self._send(msg)

    def _send_mouse(self, dx: int, dy: int, wheel: int) -> None:
        msg = self._proto().PlayerInput()
        msg.mouse.SetInParent()
        msg.mouse.delta_x = dx
        msg.mouse.delta_y = dy
        msg.mouse.button_mask = self._buttons
        msg.mouse.wheel_delta = wheel
        self._send(msg)

    def submit(self, keys_down, keys_up, mouse, heartbeat: bool) -> dict:
        """
        keys_down / keys_up — списки Linux key codes.
        mouse — dict {dx, dy, buttons|None, wheel} или None.
        heartbeat — повторно отправить текущее состояние (защита от залипания:
                    инжектор отпускает всё, если долго нет пакетов).
        """
        with self._lock:
            packets_before = self._seq

            # Клиент пропал, пока что-то было зажато: инжектор уже отпустил всё по watchdog,
            # поэтому забываем и наше состояние — иначе зависшая клавиша «воскреснет»
            # в первом же snapshot новой страницы.
            now = time.monotonic()
            if (self._pressed or self._buttons) and now - self._last_submit > STALE_AFTER_S:
                log.warning('Клиент молчал %.1f с — сбрасываю состояние ввода (было: keys=%s buttons=%d)',
                            now - self._last_submit, sorted(self._pressed), self._buttons)
                self._pressed.clear()
                self._buttons = 0
            self._last_submit = now

            # Сначала нажатия, потом отпускания — по отдельным пакетам, чтобы «тап»
            # (down+up в одном запросе) не потерялся.
            if keys_down:
                self._pressed |= set(keys_down)
                self._send_keyboard()
            if keys_up:
                self._pressed -= set(keys_up)
                self._send_keyboard()

            if mouse is not None:
                new_buttons = mouse.get('buttons')
                if new_buttons is not None:
                    new_buttons &= 0b111
                buttons_changed = new_buttons is not None and new_buttons != self._buttons
                if new_buttons is not None:
                    self._buttons = new_buttons
                dx, dy, wheel = mouse.get('dx', 0), mouse.get('dy', 0), mouse.get('wheel', 0)
                if dx or dy or wheel or buttons_changed:
                    self._send_mouse(dx, dy, wheel)

            if heartbeat:
                self._send_keyboard()
                if self._buttons:
                    self._send_mouse(0, 0, 0)

            return {
                'status': 'ok',
                'packets': self._seq - packets_before,
                'pressed': sorted(self._pressed),
                'buttons': self._buttons,
            }
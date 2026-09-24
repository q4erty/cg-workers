#!/usr/bin/env python3
"""
Input Injector (подзадача 2.В): эмуляция клавиатуры и мыши через /dev/uinput.

Принимает Protobuf PlayerInput (см. input.proto) через Unix DGRAM socket
(по умолчанию /tmp/input.sock) и пишет события в виртуальные устройства
  cg-virtual-keyboard  — KEY_W/A/S/D/SPACE/LEFTSHIFT
  cg-virtual-mouse     — BTN_LEFT/RIGHT/MIDDLE + REL_X/REL_Y/REL_WHEEL

Клавиатура и кнопки приходят СОСТОЯНИЕМ (pressed_keys / button_mask). Инжектор
сравнивает его с предыдущим и шлёт И key-down (value=1), И key-up (value=0).
Баг из примера в доке (только key-down → клавиша «залипает») здесь исключён.

Защита от залипания:
  * при SIGTERM/SIGINT и при выходе всё отпускается;
  * watchdog: если что-то зажато, а пакетов нет дольше INPUT_HELD_TIMEOUT_S
    (по умолчанию 3 с) — всё отпускается. Клиент шлёт heartbeat, пока держит клавишу.

Backend'ы (--backend или переменная INJECT_BACKEND):
  uinput   — ядро хоста (/dev/uinput). Xvfb эти устройства НЕ видит (у него нет подхвата
             evdev), подходит для реального Xorg/Wayland. События видит и хост-сессия.
  xtest    — расширение XTEST прямо в X-сервер (Xvfb :99). /dev/uinput и права не нужны,
             хост не затрагивается. Это рабочий вариант для игры внутри Xvfb.
  dry-run  — ничего не пишет, только логи (INJECTOR_DRY_RUN=1).

Запуск:
  python3 input_injector.py --selftest                        # uinput: KEY_W down/up
  DISPLAY=:99 python3 input_injector.py --selftest --backend xtest   # с проверкой в X-сервере
  DISPLAY=:99 python3 input_injector.py --backend xtest       # рабочий режим для Xvfb
"""
import argparse
import logging
import os
import signal
import socket
import sys
import threading
import time

from input_bridge import SOCK_PATH, load_proto

log = logging.getLogger('input-injector')

# --- Linux input-event-codes.h (сверяются с python-evdev при создании устройств) ---
KEY_W, KEY_A, KEY_S, KEY_D, KEY_SPACE, KEY_LEFTSHIFT = 17, 30, 31, 32, 57, 42
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112
REL_X, REL_Y, REL_WHEEL = 0x00, 0x01, 0x08

# Клавиши, которые умеет виртуальная клавиатура. Расширять — добавлением сюда
# (и в KEYMAP на клиенте). Остальные коды игнорируются с одним предупреждением.
KEYBOARD_KEYS = {
    KEY_W: 'KEY_W',
    KEY_A: 'KEY_A',
    KEY_S: 'KEY_S',
    KEY_D: 'KEY_D',
    KEY_SPACE: 'KEY_SPACE',
    KEY_LEFTSHIFT: 'KEY_LEFTSHIFT',
}
# (бит в button_mask, код кнопки, имя)
MOUSE_BUTTONS = (
    (0x01, BTN_LEFT, 'BTN_LEFT'),
    (0x02, BTN_RIGHT, 'BTN_RIGHT'),
    (0x04, BTN_MIDDLE, 'BTN_MIDDLE'),
)

KEYBOARD_NAME = 'cg-virtual-keyboard'
MOUSE_NAME = 'cg-virtual-mouse'


# ---------------------------------------------------------------------------
# Backend'ы: куда реально пишутся события
# ---------------------------------------------------------------------------

class DryRunBackend:
    """Ничего не пишет в систему — события только логируются самим Injector'ом."""

    def key(self, code: int, value: int): pass
    def button(self, code: int, value: int): pass
    def rel(self, dx: int, dy: int, wheel: int): pass
    def sync_keyboard(self): pass
    def sync_mouse(self): pass
    def close(self): pass


class UInputBackend:
    """Два виртуальных устройства на python-evdev."""

    def __init__(self):
        from evdev import UInput, ecodes as e   # ленивый импорт: dry-run работает без evdev

        # Проверка согласованности наших констант с python-evdev
        expected = {
            'KEY_W': KEY_W, 'KEY_A': KEY_A, 'KEY_S': KEY_S, 'KEY_D': KEY_D,
            'KEY_SPACE': KEY_SPACE, 'KEY_LEFTSHIFT': KEY_LEFTSHIFT,
            'BTN_LEFT': BTN_LEFT, 'BTN_RIGHT': BTN_RIGHT, 'BTN_MIDDLE': BTN_MIDDLE,
            'REL_X': REL_X, 'REL_Y': REL_Y, 'REL_WHEEL': REL_WHEEL,
        }
        for name, value in expected.items():
            actual = getattr(e, name)
            if actual != value:
                raise RuntimeError(f'{name}: в коде {value}, в python-evdev {actual}')

        self._e = e
        self._kb = UInput({e.EV_KEY: sorted(KEYBOARD_KEYS)}, name=KEYBOARD_NAME)
        try:
            self._mouse = UInput(
                {
                    e.EV_KEY: [BTN_LEFT, BTN_RIGHT, BTN_MIDDLE],
                    e.EV_REL: [REL_X, REL_Y, REL_WHEEL],
                },
                name=MOUSE_NAME,
            )
        except Exception:
            self._kb.close()
            raise

    def key(self, code: int, value: int):
        self._kb.write(self._e.EV_KEY, code, value)

    def button(self, code: int, value: int):
        self._mouse.write(self._e.EV_KEY, code, value)

    def rel(self, dx: int, dy: int, wheel: int):
        if dx:
            self._mouse.write(self._e.EV_REL, REL_X, dx)
        if dy:
            self._mouse.write(self._e.EV_REL, REL_Y, dy)
        if wheel:
            self._mouse.write(self._e.EV_REL, REL_WHEEL, wheel)

    def sync_keyboard(self):
        self._kb.syn()

    def sync_mouse(self):
        self._mouse.syn()

    def close(self):
        for dev in (self._kb, self._mouse):
            try:
                dev.close()
            except Exception:
                log.exception('Ошибка при закрытии виртуального устройства')


class XTestBackend:
    """
    Ввод через расширение XTEST напрямую в X-сервер (python-xlib).
    Работает с обычным Xvfb: не нужны /dev/uinput, SYS_ADMIN и privileged.
    """

    # X keycode = Linux evdev code + 8 (раскладка xkb «evdev», её использует Xvfb)
    X_KEYCODE_OFFSET = 8
    X_BUTTON = {BTN_LEFT: 1, BTN_MIDDLE: 2, BTN_RIGHT: 3}
    X_WHEEL_UP, X_WHEEL_DOWN = 4, 5

    def __init__(self, display_name: str | None = None):
        from Xlib import X, display
        from Xlib.ext import xtest

        self._X = X
        self._xtest = xtest
        self.display_name = display_name or os.environ.get('DISPLAY')
        self._display = display.Display(self.display_name)   # DisplayConnectionError, если X нет
        if not self._display.has_extension('XTEST'):
            self._display.close()
            raise RuntimeError('X-сервер без расширения XTEST')

    def _fake(self, event_type, detail=0, x=0, y=0):
        self._xtest.fake_input(self._display, event_type, detail=detail, x=x, y=y)

    def key(self, code: int, value: int):
        self._fake(self._X.KeyPress if value else self._X.KeyRelease, code + self.X_KEYCODE_OFFSET)

    def button(self, code: int, value: int):
        self._fake(self._X.ButtonPress if value else self._X.ButtonRelease, self.X_BUTTON[code])

    def rel(self, dx: int, dy: int, wheel: int):
        if dx or dy:
            self._fake(self._X.MotionNotify, detail=1, x=dx, y=dy)   # detail=1 — относительное движение
        if wheel:
            btn = self.X_WHEEL_UP if wheel > 0 else self.X_WHEEL_DOWN
            for _ in range(abs(wheel)):                               # колесо в X — это «клики» 4/5
                self._fake(self._X.ButtonPress, btn)
                self._fake(self._X.ButtonRelease, btn)

    def sync_keyboard(self):
        self._display.sync()

    def sync_mouse(self):
        self._display.sync()

    def close(self):
        try:
            self._display.close()
        except Exception:
            log.exception('Ошибка при закрытии соединения с X-сервером')


BACKENDS = ('uinput', 'xtest', 'dry-run')


def make_backend(name: str, display_name: str | None = None):
    if name == 'dry-run':
        return DryRunBackend()
    if name == 'uinput':
        return UInputBackend()
    if name == 'xtest':
        return XTestBackend(display_name)
    raise ValueError(f'неизвестный backend: {name} (доступны: {", ".join(BACKENDS)})')


# ---------------------------------------------------------------------------
# Логика: состояние -> события
# ---------------------------------------------------------------------------

class Injector:
    def __init__(self, backend):
        self.backend = backend
        self.pressed: set[int] = set()
        self.buttons = 0
        self.last_rx = time.monotonic()
        self._last_seq: int | None = None
        self._warned_keys: set[int] = set()
        self._warned_gamepad = False

    # ----- вход -----

    def handle(self, msg) -> None:
        self.last_rx = time.monotonic()
        self._track_sequence(msg.sequence)
        kind = msg.WhichOneof('input')
        if kind == 'keyboard':
            self.apply_keyboard(msg.keyboard.pressed_keys)
        elif kind == 'mouse':
            m = msg.mouse
            self.apply_mouse(m.delta_x, m.delta_y, m.button_mask, m.wheel_delta)
        elif kind == 'gamepad':
            if not self._warned_gamepad:
                log.warning('Gamepad пока не поддерживается, пакеты игнорируются')
                self._warned_gamepad = True
        else:
            log.warning('PlayerInput #%d без payload', msg.sequence)

    def _track_sequence(self, seq: int) -> None:
        last = self._last_seq
        if last is not None:
            if seq > last + 1:
                log.warning('Потеряно пакетов: %d (ожидали #%d, пришёл #%d)', seq - last - 1, last + 1, seq)
            elif seq <= last:
                log.info('sequence сбросился (#%d после #%d) — перезапуск клиента', seq, last)
        self._last_seq = seq

    # ----- клавиатура -----

    def apply_keyboard(self, keys) -> None:
        wanted: set[int] = set()
        for code in keys:
            if code in KEYBOARD_KEYS:
                wanted.add(code)
            elif code not in self._warned_keys:
                self._warned_keys.add(code)
                log.warning('Клавиша с кодом %d не поддерживается виртуальной клавиатурой, игнорирую', code)

        released = sorted(self.pressed - wanted)
        pressed = sorted(wanted - self.pressed)
        for code in released:
            self.backend.key(code, 0)
            log.info('%s up', KEYBOARD_KEYS[code])
        for code in pressed:
            self.backend.key(code, 1)
            log.info('%s down', KEYBOARD_KEYS[code])
        if released or pressed:
            self.backend.sync_keyboard()
        self.pressed = wanted

    # ----- мышь -----

    def apply_mouse(self, dx: int, dy: int, mask: int, wheel: int) -> None:
        mask &= 0b111
        touched = False

        if dx or dy or wheel:
            self.backend.rel(dx, dy, wheel)
            log.debug('mouse rel dx=%d dy=%d wheel=%d', dx, dy, wheel)
            touched = True

        changed = mask ^ self.buttons
        for bit, code, name in MOUSE_BUTTONS:
            if changed & bit:
                value = 1 if mask & bit else 0
                self.backend.button(code, value)
                log.info('%s %s', name, 'down' if value else 'up')
                touched = True
        self.buttons = mask

        if touched:
            self.backend.sync_mouse()

    # ----- защита от залипания -----

    def release_all(self, reason: str = 'release_all') -> None:
        for code in sorted(self.pressed):
            self.backend.key(code, 0)
            log.info('%s up (%s)', KEYBOARD_KEYS.get(code, code), reason)
        if self.pressed:
            self.backend.sync_keyboard()
        self.pressed = set()

        released_button = False
        for bit, code, name in MOUSE_BUTTONS:
            if self.buttons & bit:
                self.backend.button(code, 0)
                log.info('%s up (%s)', name, reason)
                released_button = True
        if released_button:
            self.backend.sync_mouse()
        self.buttons = 0

    def check_watchdog(self, timeout_s: float) -> None:
        if not (self.pressed or self.buttons):
            return
        idle = time.monotonic() - self.last_rx
        if idle > timeout_s:
            log.warning('Нет пакетов %.1f с, а клавиши/кнопки зажаты — отпускаю всё', idle)
            self.release_all('watchdog')
            self.last_rx = time.monotonic()


# ---------------------------------------------------------------------------
# Главный цикл
# ---------------------------------------------------------------------------

def serve(injector: Injector, pb, sock_path: str, held_timeout_s: float, stop: threading.Event) -> None:
    if os.path.exists(sock_path):
        os.unlink(sock_path)                       # хвост от прошлого запуска
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(sock_path)
    os.chmod(sock_path, 0o666)
    sock.settimeout(0.25)
    log.info('Слушаю PlayerInput на %s (watchdog %.1f с)', sock_path, held_timeout_s)

    try:
        while not stop.is_set():
            try:
                data, _ = sock.recvfrom(4096)
            except TimeoutError:
                injector.check_watchdog(held_timeout_s)
                continue
            try:
                msg = pb.PlayerInput.FromString(data)
            except Exception as e:
                log.warning('Не удалось разобрать PlayerInput (%d байт): %s', len(data), e)
                continue
            injector.handle(msg)
    finally:
        injector.release_all('shutdown')
        sock.close()
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass


def list_virtual_devices() -> str:
    """Kernel-уровень: показывает наши устройства из /proc/bus/input/devices."""
    try:
        with open('/proc/bus/input/devices', encoding='utf-8', errors='replace') as f:
            blocks = f.read().strip().split('\n\n')
    except OSError as e:
        return f'(не могу прочитать /proc/bus/input/devices: {e})'
    ours = [b for b in blocks if 'cg-virtual' in b]
    return '\n\n'.join(ours) if ours else '(устройств cg-virtual-* в ядре не найдено)'


def explain_uinput_error(e: BaseException) -> None:
    print(f'\n[FAIL] Не удалось создать UInput: {type(e).__name__}: {e}\n', file=sys.stderr)
    print('Что проверить по порядку:', file=sys.stderr)
    print('  1. На ХОСТЕ загружен модуль:     sudo modprobe uinput   (и есть /dev/uinput)', file=sys.stderr)
    print('  2. Контейнер запущен с:          --device /dev/uinput --cap-add SYS_ADMIN', file=sys.stderr)
    print('  3. Внутри контейнера:            ls -l /dev/uinput   (нужен доступ на запись; работаем от root)', file=sys.stderr)
    print('  4. Если Permission denied при root: проверь SELinux/AppArmor или запусти с --privileged для диагностики', file=sys.stderr)


def explain_xtest_error(e: BaseException) -> None:
    print(f'\n[FAIL] Не удалось подключиться к X-серверу: {type(e).__name__}: {e}\n', file=sys.stderr)
    print('Что проверить по порядку:', file=sys.stderr)
    print('  1. Xvfb запущен:                 Xvfb :99 &', file=sys.stderr)
    print('  2. DISPLAY указывает на него:    export DISPLAY=:99   (или --display :99)', file=sys.stderr)
    print('  3. Установлен python-xlib:       pip3 install --break-system-packages python-xlib', file=sys.stderr)
    print('  Не используй :0 при --network host: это может быть реальный экран хоста.', file=sys.stderr)


def _verify_in_x_server(backend: XTestBackend) -> bool:
    """Второе соединение слушает root-окно и проверяет, что X-сервер реально получил KEY_W."""
    from Xlib import X, display

    watcher = display.Display(backend.display_name)
    try:
        root = watcher.screen().root
        root.change_attributes(event_mask=X.KeyPressMask | X.KeyReleaseMask)
        watcher.sync()

        backend.key(KEY_W, 1)
        backend.sync_keyboard()
        time.sleep(0.05)
        backend.key(KEY_W, 0)
        backend.sync_keyboard()

        got = []
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and len(got) < 2:
            while watcher.pending_events():
                ev = watcher.next_event()
                if ev.type in (X.KeyPress, X.KeyRelease):
                    got.append(('KeyPress' if ev.type == X.KeyPress else 'KeyRelease', ev.detail))
            time.sleep(0.02)
    finally:
        watcher.close()

    expected = KEY_W + XTestBackend.X_KEYCODE_OFFSET
    print(f'       X-сервер получил: {got}  (ожидали KeyPress и KeyRelease с keycode={expected})')
    return got == [('KeyPress', expected), ('KeyRelease', expected)]


def selftest(backend_name: str, display_name: str | None) -> int:
    """Подзадача 6.а: создать backend и записать KEY_W down/up."""
    try:
        backend = make_backend(backend_name, display_name)
    except ImportError as e:
        pkg = 'python-xlib' if backend_name == 'xtest' else 'evdev'
        print(f'[FAIL] {pkg} не установлен: {e}\n  pip3 install --break-system-packages {pkg}', file=sys.stderr)
        return 2
    except Exception as e:
        if backend_name == 'xtest':
            explain_xtest_error(e)
        else:
            explain_uinput_error(e)
        return 2

    try:
        if backend_name == 'xtest':
            print(f'[ OK ] Подключились к X-серверу {backend.display_name}, расширение XTEST есть')
            if not _verify_in_x_server(backend):
                print('[FAIL] X-сервер не получил ожидаемые KeyPress/KeyRelease', file=sys.stderr)
                return 3
            print('[ OK ] KEY_W down + KEY_W up дошли до X-сервера')
            return 0

        print('[ OK ] Созданы виртуальные устройства:', KEYBOARD_NAME, '+', MOUSE_NAME)
        time.sleep(0.3)   # даём системе зарегистрировать устройства
        backend.key(KEY_W, 1)
        backend.sync_keyboard()
        time.sleep(0.05)
        backend.key(KEY_W, 0)
        backend.sync_keyboard()
        print('[ OK ] KEY_W down + KEY_W up записаны без ошибок (Permission denied нет)')
        if backend_name == 'uinput':
            print('\nУстройства в ядре (/proc/bus/input/devices):\n')
            print(list_virtual_devices())
            print('\nВнимание: uinput-устройства создаются в ядре ХОСТА — хост-система тоже может увидеть эти события.')
        return 0
    finally:
        backend.close()


def main() -> int:
    parser = argparse.ArgumentParser(description='Cloud Gaming Input Injector')
    parser.add_argument('--selftest', action='store_true', help='создать backend и записать KEY_W down/up')
    parser.add_argument('--backend', choices=BACKENDS,
                        default=os.environ.get('INJECT_BACKEND', 'uinput'),
                        help='куда писать события (по умолчанию %(default)s)')
    parser.add_argument('--display', default=None, help='X display для backend xtest (по умолчанию $DISPLAY)')
    parser.add_argument('--dry-run', action='store_true',
                        default=os.environ.get('INJECTOR_DRY_RUN') == '1',
                        help='то же, что --backend dry-run')
    parser.add_argument('--sock', default=SOCK_PATH, help='путь Unix DGRAM socket (по умолчанию %(default)s)')
    parser.add_argument('-v', '--verbose', action='store_true', help='логировать и движения мыши (DEBUG)')
    args = parser.parse_args()
    if args.dry_run:
        args.backend = 'dry-run'

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )

    if args.selftest:
        return selftest(args.backend, args.display)

    held_timeout_s = float(os.environ.get('INPUT_HELD_TIMEOUT_S', '3'))
    pb = load_proto()

    try:
        backend = make_backend(args.backend, args.display)
    except ImportError as e:
        pkg = 'python-xlib' if args.backend == 'xtest' else 'evdev'
        log.error('%s не установлен: %s (pip3 install --break-system-packages %s)', pkg, e, pkg)
        return 2
    except Exception as e:
        if args.backend == 'xtest':
            explain_xtest_error(e)
        else:
            explain_uinput_error(e)
        return 2

    if args.backend == 'dry-run':
        log.warning('DRY-RUN: события только логируются, никуда не пишутся')
    elif args.backend == 'xtest':
        log.info('Backend xtest: ввод идёт в X-сервер %s', backend.display_name)
    else:
        log.info('Виртуальные устройства созданы: %s, %s', KEYBOARD_NAME, MOUSE_NAME)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    injector = Injector(backend)
    try:
        serve(injector, pb, args.sock, held_timeout_s, stop)
    finally:
        backend.close()
    log.info('Остановлен')
    return 0


if __name__ == '__main__':
    sys.exit(main())
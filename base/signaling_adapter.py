#!/usr/bin/env python3
"""
Signaling Adapter (dev, non-trickle ICE) для GStreamer-воркера.
Управляет webrtcbin программно через GStreamer Promise API.

Эндпоинты:
  GET  /         — демо-страница с <video> и захватом клавиатуры
  GET  /ready    — readiness probe (200, когда pipeline в PLAYING)
  POST /sdp      — приём SDP Offer, возврат SDP Answer
  POST /ice      — приём remote ICE candidate
  POST /input    — dev-ввод: {keys_down:[...], keys_up:[...], mouse:{dx,dy,buttons,wheel}}
                   -> Protobuf PlayerInput -> Unix DGRAM socket Input Injector'а.
                   Оставлен для curl/отладки; браузер использует Data Channel 'input'
                   с тем же JSON — см. ниже.
  POST /restart  — см. примечание про ICE Restart ниже

WebRTC Data Channel (создаёт браузер, offerer — он же, до createOffer):
  'input'   — ordered — тот же JSON, что и тело POST /input (одна из причин не
              городить отдельный формат: код валидации и всё остальное общее).
  'metrics' — unordered, maxRetransmits=0 — воркер раз в секунду шлёт JSON:
              {"fps": N, "bitrate_kbps": N, "width": N, "height": N, "ts": ms}.
              Точка отсчёта fps/bitrate — сам пайплайн (пробы на падах), а не
              что там браузер реально получил, так что это не то же самое, что
              getStats() на клиенте, а скорее «что воркер закодировал за секунду».

Про ICE Restart:
  В этой архитектуре offerer — браузер (worker только отвечает), поэтому
  инициировать restart может только клиент: pc.restartIce() + новый createOffer()
  + повторный POST /sdp на том же соединении. Серверный /restart-эндпоинт тут
  нечего было бы делать — POST /restart ниже прямо это объясняет, а не
  притворяется, что что-то реально перезапускает.
  Повторный offer с тем же payload type переиспользует текущий webrtcbin (без
  пересборки видео-пайплайна) — иначе каждый reconnect убивал бы data channels.

Совместимость: Ubuntu 24.04 / GStreamer 1.24.x / Python 3.12.

Про payload type (pt):
  Воркер — answerer. Браузер в offer предлагает H.264 под своими номерами
  (например, 126, 97, 105, 103), а rtph264pay по умолчанию шлёт pt=96.
  webrtcbin сравнивает caps пада (payload=96) с кодеками из offer, не находит
  совместимого transceiver'а и отвечает «только приём, inactive» (VP8, a=inactive).
  Поэтому pt берётся ИЗ offer и пайплайн пересобирается под него.

  STUN для dev не нужен: host-кандидатов хватает. Если нужен — задай
  переменную окружения STUN_SERVER=stun://stun.l.google.com:19302

Источник видео (переменные окружения):
  VIDEO_SOURCE=videotestsrc (по умолчанию, шар) | ximagesrc (захват экрана Xvfb)
  CAPTURE_DISPLAY=:99   — какой X display снимать (по умолчанию $DISPLAY, иначе :99)
  CAPTURE_FPS=30
  Не используй :0 при --network host: это может быть экран ХОСТА.

Про /input:
  Клавиши — Linux key codes (KEY_W=17, KEY_A=30, KEY_S=31, KEY_D=32, KEY_SPACE=57,
  KEY_LEFTSHIFT=42), а не JS-коды. Адаптер хранит состояние «что зажато» и шлёт
  инжектору snapshot; key-down/key-up генерирует уже инжектор.
"""
import json
import logging
import os
import re
import threading
import time
from typing import Annotated

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstSdp', '1.0')
gi.require_version('GstWebRTC', '1.0')
from gi.repository import GLib, Gst, GstSdp, GstWebRTC

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, ValidationError

from input_bridge import InputForwarder, InputUnavailable, load_proto

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s'
)
log = logging.getLogger('signaling-adapter')

Gst.init(None)

DEFAULT_H264_PT = 96
GATHERING_TIMEOUT_S = 15 if os.environ.get('STUN_SERVER') else 10

VIDEO_SOURCE = os.environ.get('VIDEO_SOURCE', 'videotestsrc')
if VIDEO_SOURCE not in ('videotestsrc', 'ximagesrc'):
    raise SystemExit(f'VIDEO_SOURCE={VIDEO_SOURCE!r}: допустимо videotestsrc или ximagesrc')
CAPTURE_DISPLAY = os.environ.get('CAPTURE_DISPLAY') or os.environ.get('DISPLAY') or ':99'
CAPTURE_FPS = int(os.environ.get('CAPTURE_FPS', '30'))

# Какой пакет ставить, если GStreamer-элемент не найден
_ELEMENT_HINTS = {
    'ximagesrc': 'gstreamer1.0-plugins-good и gstreamer1.0-x',
    'videotestsrc': 'gstreamer1.0-plugins-base',
    'videoconvert': 'gstreamer1.0-plugins-base',
    'capsfilter': 'gstreamer1.0-plugins-base',
    'x264enc': 'gstreamer1.0-plugins-ugly',
    'rtph264pay': 'gstreamer1.0-plugins-good',
    'webrtcbin': 'gstreamer1.0-plugins-bad и gstreamer1.0-nice',
}


def _make(factory: str, name: str) -> Gst.Element:
    """Создаёт элемент GStreamer; если плагина нет — ошибка с понятной подсказкой."""
    el = Gst.ElementFactory.make(factory, name)
    if el is None:
        raise RuntimeError(
            f'Не найден GStreamer-элемент {factory!r}. '
            f'Установи: apt-get install {_ELEMENT_HINTS.get(factory, "gstreamer1.0-plugins-*")}'
        )
    return el


def _parse_sdp(text: str) -> GstSdp.SDPMessage:
    """Парсит SDP-строку штатным API GstSdp.sdp_message_parse_buffer."""
    ret, msg = GstSdp.SDPMessage.new()
    if ret != GstSdp.SDPResult.OK:
        raise ValueError(f'SDPMessage.new() failed: {ret}')
    res = GstSdp.sdp_message_parse_buffer(text.encode('utf-8'), msg)
    if res != GstSdp.SDPResult.OK:
        raise ValueError(f'sdp_message_parse_buffer failed: {res}')
    return msg


def _pick_h264_pt(offer_sdp: str) -> int | None:
    """
    Выбирает из video-секции offer payload type H.264, совместимый с нашим
    энкодером: packetization-mode=1 (rtph264pay работает в нём по умолчанию)
    и профиль baseline. Приоритет у constrained baseline (profile-level-id 42e0xx).
    Возвращает None, если подходящего кодека в offer нет.
    """
    in_video = False
    h264_pts: list[int] = []
    fmtp: dict[int, dict[str, str]] = {}

    for raw in offer_sdp.splitlines():
        line = raw.strip()
        if line.startswith('m='):
            in_video = line.startswith('m=video')
            continue
        if not in_video:
            continue
        m = re.match(r'a=rtpmap:(\d+)\s+H264/90000', line, re.IGNORECASE)
        if m:
            h264_pts.append(int(m.group(1)))
            continue
        m = re.match(r'a=fmtp:(\d+)\s+(.*)', line)
        if m:
            params: dict[str, str] = {}
            for kv in m.group(2).split(';'):
                if '=' in kv:
                    k, v = kv.split('=', 1)
                    params[k.strip().lower()] = v.strip().lower()
            fmtp[int(m.group(1))] = params

    best_pt: int | None = None
    best_score = -1
    for pt in h264_pts:
        params = fmtp.get(pt, {})
        if params.get('packetization-mode') != '1':
            continue
        profile_level_id = params.get('profile-level-id', '')
        if profile_level_id.startswith('42e0'):      # constrained baseline
            score = 2
        elif profile_level_id.startswith('42'):      # baseline
            score = 1
        else:                                        # main/high — не наш энкодер
            continue
        if score > best_score:
            best_pt, best_score = pt, score
    return best_pt


class MetricsCollector:
    """
    Считает кадры и байты по пробам на падах пайплайна. Пробы вызываются
    GStreamer'ом на потоке стриминга — не на GLib-потоке, откуда их потом читает
    таймер метрик, — поэтому счётчики защищены локом.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._frames = 0
        self._bytes = 0

    def on_frame(self, _pad, _info):
        with self._lock:
            self._frames += 1
        return Gst.PadProbeReturn.OK

    def on_payload(self, _pad, info):
        buf = info.get_buffer()
        size = buf.get_size() if buf is not None else 0
        with self._lock:
            self._bytes += size
        return Gst.PadProbeReturn.OK

    def sample(self) -> tuple[int, int]:
        """(кадров, байт) с прошлого вызова; счётчики обнуляются."""
        with self._lock:
            frames, nbytes = self._frames, self._bytes
            self._frames = 0
            self._bytes = 0
        return frames, nbytes


class WebRTCSession:
    """Один пайплайн с webrtcbin. Работает как answerer (браузер шлёт offer)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pt = DEFAULT_H264_PT
        self.input_channel = None
        self.metrics_channel = None
        self._metrics_timer_id = None
        self._loop = GLib.MainLoop()
        threading.Thread(target=self._loop.run, daemon=True).start()
        self._build(DEFAULT_H264_PT)

    def _build(self, pt: int):
        """Программная сборка пайплайна (обходит баги gst-parse)."""
        self._gathering_done = threading.Event()
        self._state = 'NEW'
        self._pt = pt
        # Пересобирается весь пайплайн — старые data channel-объекты (если были)
        # принадлежат уничтоженному webrtcbin'у, они больше не рабочие.
        self.input_channel = None
        self.metrics_channel = None
        if self._metrics_timer_id is not None:
            GLib.source_remove(self._metrics_timer_id)
            self._metrics_timer_id = None

        self.pipeline = Gst.Pipeline.new('cg-worker-pipeline')

        if VIDEO_SOURCE == 'ximagesrc':
            src = _make('ximagesrc', 'ximagesrc')
            src.set_property('display-name', CAPTURE_DISPLAY)
            src.set_property('use-damage', False)      # Xvfb не всегда корректно репортит damage
            src.set_property('show-pointer', True)     # курсор виден в кадре
            log.info('Video source: ximagesrc, display=%s, %d fps', CAPTURE_DISPLAY, CAPTURE_FPS)
            if CAPTURE_DISPLAY == ':0':
                log.warning('display :0 при --network host может оказаться экраном ХОСТА, используй :99')
        else:
            src = _make('videotestsrc', 'videotestsrc')
            src.set_property('is-live', True)
            src.set_property('pattern', 'ball')
            log.info('Video source: videotestsrc (ball), %d fps', CAPTURE_FPS)

        fps_caps = _make('capsfilter', 'fps-caps')
        fps_caps.set_property('caps', Gst.Caps.from_string(f'video/x-raw,framerate={CAPTURE_FPS}/1'))

        conv = _make('videoconvert', 'videoconvert')

        raw_caps = _make('capsfilter', 'raw-caps')
        raw_caps.set_property('caps', Gst.Caps.from_string('video/x-raw,format=I420'))

        enc = _make('x264enc', 'x264enc')
        enc.set_property('tune', 4)                    # zerolatency (bitmask 0x4)
        enc.set_property('speed-preset', 1)            # ultrafast
        enc.set_property('bitrate', 6000)
        enc.set_property('key-int-max', CAPTURE_FPS)   # keyframe раз в секунду

        h264_caps = _make('capsfilter', 'h264-caps')
        h264_caps.set_property('caps', Gst.Caps.from_string(
            'video/x-h264,profile=baseline,stream-format=byte-stream'))

        pay = _make('rtph264pay', 'rtph264pay')
        pay.set_property('config-interval', -1)
        pay.set_property('pt', pt)                     # ← pt из offer браузера

        self.webrtcbin = _make('webrtcbin', 'sendrecv')
        self.webrtcbin.set_property('bundle-policy', 'max-bundle')
        stun = os.environ.get('STUN_SERVER')
        if stun:
            self.webrtcbin.set_property('stun-server', stun)

        for el in (src, fps_caps, conv, raw_caps, enc, h264_caps, pay, self.webrtcbin):
            self.pipeline.add(el)

        # Цепочка: src -> fps_caps -> conv -> raw_caps -> enc -> h264_caps -> pay
        chain = [src, fps_caps, conv, raw_caps, enc, h264_caps, pay]
        for a, b in zip(chain, chain[1:]):
            if not a.link(b):
                raise RuntimeError(
                    f'Не удалось слинковать {a.get_name()} -> {b.get_name()}'
                )

        # Request-pad создаёт transceiver сам; add-transceiver вручную НЕ нужен
        # (второй transceiver остался бы «фантомным»).
        self._sink_pad = self.webrtcbin.request_pad_simple('sink_%u')
        if self._sink_pad is None:
            raise RuntimeError('webrtcbin не выдал request pad sink_%u')
        ret = pay.get_static_pad('src').link(self._sink_pad)
        if ret != Gst.PadLinkReturn.OK:
            raise RuntimeError(f'Линк pay -> webrtcbin завершился с кодом {ret}')

        # Метрики (раздел 2.4 дока): считаем кадры на сырых буферах (после конвертера,
        # до энкодера — это и есть реальный fps пайплайна) и байты уже на RTP-payload'е
        # (это и есть реальный исходящий битрейт в webrtcbin). Раз в секунду отдаём их
        # в data channel 'metrics', см. _emit_metrics.
        self._metrics = MetricsCollector()
        self._video_src_pad = raw_caps.get_static_pad('src')
        self._video_src_pad.add_probe(Gst.PadProbeType.BUFFER, self._metrics.on_frame)
        pay.get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER, self._metrics.on_payload)
        self._metrics_timer_id = GLib.timeout_add(1000, self._emit_metrics)

        # Сигналы webrtcbin
        self.webrtcbin.connect('on-ice-candidate', self._on_ice_candidate)
        self.webrtcbin.connect('notify::ice-gathering-state', self._on_gathering)
        self.webrtcbin.connect('notify::connection-state', self._on_connection_state)
        self.webrtcbin.connect('on-data-channel', self._on_data_channel)

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect('message', self._on_bus_message)

        self.pipeline.set_state(Gst.State.PLAYING)
        log.info('Pipeline built (H.264 pt=%d), transitioning to PLAYING', pt)

    def _reset(self, pt: int):
        """Пересобирает пайплайн (сменился payload type или предыдущий упал в ERROR)."""
        log.info('Resetting pipeline (pt=%d)', pt)
        self.pipeline.get_bus().remove_signal_watch()
        self.pipeline.set_state(Gst.State.NULL)
        self._build(pt)

    def _wait_for_caps(self, timeout: float = 5.0):
        """Ждёт, пока на sink-пад webrtcbin придут caps (нужны для create-answer)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._sink_pad.get_current_caps() is not None:
                return
            if self._state == 'ERROR':
                raise RuntimeError(
                    'pipeline перешёл в ERROR до первого кадра, см. строку "GStreamer ERROR" выше'
                    + (f' (ximagesrc: доступен ли X display {CAPTURE_DISPLAY}?)'
                       if VIDEO_SOURCE == 'ximagesrc' else '')
                )
            time.sleep(0.02)
        raise TimeoutError(
            'sink-пад webrtcbin не получил caps за 5с (источник не выдал первый кадр?)'
        )

    # ---------- Мост HTTP-поток -> GLib-поток ----------

    def _on_glib(self, fn, timeout=10):
        """Выполняет функцию в GLib MainLoop и ждёт результат."""
        result = {}
        done = threading.Event()

        def wrapper():
            try:
                result['value'] = fn()
            except Exception as e:
                result['error'] = e
            finally:
                done.set()
            return False

        GLib.idle_add(wrapper)
        if not done.wait(timeout):
            raise TimeoutError('GLib task timed out')
        if 'error' in result:
            raise result['error']
        return result.get('value')

    # ---------- Сигналы webrtcbin ----------

    def _on_ice_candidate(self, _bin, mline_index, candidate):
        log.info('Local ICE candidate [mline=%s]: %s', mline_index, candidate)

    def _on_gathering(self, bin, _pspec):
        state = bin.get_property('ice-gathering-state')
        log.info('ICE gathering state: %s', state.value_nick)
        if state == GstWebRTC.WebRTCICEGatheringState.COMPLETE:
            self._gathering_done.set()

    def _on_connection_state(self, bin, _pspec):
        state = bin.get_property('connection-state')
        log.info('Peer connection state: %s', state.value_nick)

    def _on_data_channel(self, _webrtcbin, channel):
        """Браузер создаёт оба канала до createOffer(); они приходят сюда при ответе."""
        label = channel.get_property('label')
        log.info('Data channel открыт: %s', label)
        channel.connect('on-close', lambda _ch, l=label: log.info('Data channel закрыт: %s', l))
        channel.connect('on-error', lambda _ch, err, l=label: log.warning('Data channel %s: ошибка %s', l, err))
        if label == 'input':
            self.input_channel = channel
            channel.connect('on-message-string', self._on_input_message)
        elif label == 'metrics':
            self.metrics_channel = channel
        else:
            log.warning('Неизвестный data channel %r — игнорирую', label)

    def _on_input_message(self, _channel, message: str):
        """Ввод с data channel 'input' — тот же JSON, что тело POST /input."""
        try:
            body = json.loads(message)
        except (TypeError, ValueError) as e:
            log.warning('Input DC: не удалось разобрать JSON (%s): %r', e, message[:200])
            return
        try:
            _apply_input(body)
        except ValidationError as e:
            log.warning('Input DC: невалидный payload: %s', e)
        except InputUnavailable as e:
            log.warning('Input DC: инжектор недоступен: %s', e)
        except Exception:
            log.exception('Input DC: ошибка обработки сообщения')

    def _emit_metrics(self) -> bool:
        """GLib-таймер, раз в секунду. Возврат True = 'вызывай меня снова'."""
        channel = self.metrics_channel
        if channel is None or self._state != 'PLAYING':
            return GLib.SOURCE_CONTINUE
        if channel.get_property('ready-state') != GstWebRTC.WebRTCDataChannelState.OPEN:
            return GLib.SOURCE_CONTINUE

        frames, nbytes = self._metrics.sample()
        width = height = None
        caps = self._video_src_pad.get_current_caps()
        if caps is not None and caps.get_size() > 0:
            s = caps.get_structure(0)
            ok_w, w = s.get_int('width')
            ok_h, h = s.get_int('height')
            if ok_w and ok_h:
                width, height = w, h

        payload = json.dumps({
            'fps': frames,                              # таймер тикает раз в секунду
            'bitrate_kbps': round(nbytes * 8 / 1000, 1),
            'width': width, 'height': height,
            'ts': int(time.time() * 1000),
        })
        try:
            channel.emit('send-string', payload)
        except Exception:
            log.exception('Metrics DC: send-string упал')
        return GLib.SOURCE_CONTINUE

    def _on_bus_message(self, _bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            log.error('GStreamer ERROR: %s | debug: %s', err, dbg)
            self._state = 'ERROR'
        elif msg.type == Gst.MessageType.STATE_CHANGED and msg.src == self.pipeline:
            new_state = msg.parse_state_changed()[1]
            if new_state == Gst.State.PLAYING:
                self._state = 'PLAYING'
                log.info('Pipeline is PLAYING')

    # ---------- Бизнес-логика ----------

    def is_ready(self) -> bool:
        return self._state == 'PLAYING'

    def handle_offer(self, offer_sdp: str) -> dict:
        """Принимает SDP Offer от клиента, возвращает SDP Answer."""
        mlines = [l for l in offer_sdp.splitlines() if l.startswith('m=')]
        log.info('Offer has %d m-line(s): %s', len(mlines), mlines)
        if not mlines:
            raise ValueError(
                'Offer без m-line: на клиенте нужен addTransceiver/createDataChannel'
            )

        pt = _pick_h264_pt(offer_sdp)
        if pt is None:
            raise ValueError(
                'В offer нет подходящего H.264 (packetization-mode=1, baseline). '
                'Браузер без H.264? Проверьте chrome://webrtc-internals'
            )
        log.info('Selected H.264 payload type from offer: %d', pt)

        with self._lock:
            # Полная пересборка нужна, только когда её нельзя избежать: сменился payload
            # type (нужно перенастроить rtph264pay) или предыдущий пайплайн упал в ERROR.
            # Повторный offer с тем же pt — будь то перезагрузка страницы (новый
            # RTCPeerConnection) или настоящий ICE restart (pc.restartIce() на том же
            # соединении) — обрабатывается заново на ТОМ ЖЕ webrtcbin: set-remote-description
            # с новым offer сам заводит новые ICE-креды и генерирует новые local-кандидаты.
            # Это не только дешевле, но и не убивает уже открытые data channel'ы 'input'/
            # 'metrics' на каждый reconnect.
            needs_reset = self._state == 'ERROR' or pt != self._pt
            if needs_reset:
                self._on_glib(lambda: self._reset(pt))
            self._gathering_done.clear()

            self._wait_for_caps()
            self._on_glib(lambda: self._negotiate(offer_sdp))

            if not self._gathering_done.wait(GATHERING_TIMEOUT_S):
                raise TimeoutError(
                    f'ICE gathering не завершился за {GATHERING_TIMEOUT_S}с'
                )

            answer_sdp = self._on_glib(self._local_sdp)
            log.info('Answer m-lines: %s', [
                l for l in answer_sdp.splitlines()
                if l.startswith(('m=', 'a=sendonly', 'a=inactive', 'a=recvonly'))
            ])
            return {'sdp': answer_sdp, 'type': 'answer'}

    def _negotiate(self, offer_sdp: str):
        """Устанавливает remote description и создаёт answer."""
        msg = _parse_sdp(offer_sdp)
        offer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.OFFER, msg
        )

        # set-remote-description
        promise = Gst.Promise.new()
        self.webrtcbin.emit('set-remote-description', offer, promise)
        if promise.wait() != Gst.PromiseResult.REPLIED:
            raise RuntimeError('set-remote-description failed')

        # create-answer
        promise = Gst.Promise.new()
        self.webrtcbin.emit('create-answer', None, promise)
        if promise.wait() != Gst.PromiseResult.REPLIED:
            raise RuntimeError('create-answer failed')
        reply = promise.get_reply()
        if reply is None:
            raise RuntimeError('create-answer returned no reply')
        answer = reply.get_value('answer')
        if answer is None:
            raise RuntimeError('create-answer reply has no "answer" value')

        # set-local-description
        promise = Gst.Promise.new()
        self.webrtcbin.emit('set-local-description', answer, promise)
        if promise.wait() != Gst.PromiseResult.REPLIED:
            raise RuntimeError('set-local-description failed')
        log.info('Answer created, waiting for ICE gathering...')

    def _local_sdp(self) -> str:
        """Возвращает локальный SDP (answer)."""
        desc = (
            self.webrtcbin.get_property('local-description')
            or self.webrtcbin.get_property('pending-local-description')
        )
        if desc is None:
            raise RuntimeError('local-description is None')
        return desc.sdp.as_text()

    def add_ice_candidate(self, candidate: str, mline_index: int = 0):
        """Добавляет remote ICE candidate (для будущего trickle-режима)."""
        def _do():
            self.webrtcbin.emit('add-ice-candidate', mline_index, candidate)
            return True
        return self._on_glib(_do)


# ---------------------------------------------------------------------------
# FastAPI приложение
# ---------------------------------------------------------------------------

session = WebRTCSession()
input_forwarder = InputForwarder()
app = FastAPI(title='CG Worker Signaling Adapter (dev)')

try:
    load_proto()   # fail-fast: сразу видно, если нет input_pb2 / grpcio-tools
    log.info('Protobuf input_pb2 загружен, /input готов (инжектор: %s)', os.environ.get('INPUT_SOCK', '/tmp/input.sock'))
except Exception as _proto_error:
    log.warning('/input работать не будет: %s', _proto_error)


class SDPRequest(BaseModel):
    sdp: str
    type: str = 'offer'


class ICERequest(BaseModel):
    candidate: str
    sdpMLineIndex: int = 0


# Linux key codes лежат в диапазоне 1..767 (KEY_MAX = 0x2ff)
LinuxKeyCode = Annotated[int, Field(ge=1, le=767)]


class MouseInput(BaseModel):
    dx: int = Field(0, ge=-32768, le=32767)
    dy: int = Field(0, ge=-32768, le=32767)
    # Состояние кнопок: bit0=left, bit1=right, bit2=middle. None = не менять.
    buttons: int | None = Field(None, ge=0, le=7)
    wheel: int = Field(0, ge=-127, le=127)


class InputRequest(BaseModel):
    keys_down: list[LinuxKeyCode] = Field(default_factory=list)
    keys_up: list[LinuxKeyCode] = Field(default_factory=list)
    mouse: MouseInput | None = None
    # Повторно отправить текущее состояние (клиент шлёт раз в секунду, пока что-то зажато)
    heartbeat: bool = False


def _apply_input(body: dict) -> dict:
    """
    Общая точка входа для ввода: валидирует dict тем же InputRequest, что и
    POST /input, и передаёт в Input Injector. Используется и HTTP-эндпоинтом,
    и обработчиком data channel 'input' — один код валидации на оба пути.
    Поднимает pydantic.ValidationError / InputUnavailable / RuntimeError — их
    разбирают уже вызывающие (HTTP отвечает статусом, DC-обработчик — логом).
    """
    req = InputRequest.model_validate(body)
    return input_forwarder.submit(
        keys_down=req.keys_down,
        keys_up=req.keys_up,
        mouse=req.mouse.model_dump() if req.mouse else None,
        heartbeat=req.heartbeat,
    )


@app.get('/ready')
def ready():
    """Readiness probe: возвращает 200 когда pipeline в PLAYING."""
    if session.is_ready():
        return {'status': 'READY', 'pipeline': 'PLAYING'}
    raise HTTPException(503, 'pipeline not playing yet')


@app.post('/sdp')
def sdp_exchange(req: SDPRequest):
    """Принимает SDP Offer, возвращает SDP Answer."""
    if req.type != 'offer':
        raise HTTPException(400, 'Worker is answerer: send type=offer')
    try:
        return session.handle_offer(req.sdp)
    except ValueError as e:
        # некорректный offer — ошибка клиента, не воркера
        log.warning('Bad offer: %s', e)
        raise HTTPException(400, str(e))
    except Exception as e:
        log.exception('SDP negotiation failed')
        raise HTTPException(500, str(e))


@app.post('/ice')
def add_ice(req: ICERequest):
    """Принимает remote ICE candidate (для будущего trickle-режима)."""
    try:
        session.add_ice_candidate(req.candidate, req.sdpMLineIndex)
        return {'status': 'ok'}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post('/input')
def post_input(req: InputRequest):
    """
    Ввод через обычный HTTP — для curl/отладки и как запасной путь, если data
    channel 'input' почему-то не открылся. Браузер по умолчанию шлёт то же самое
    через data channel (ниже задержка, не нужен отдельный TCP-хендшейк на пакет).
    """
    try:
        return _apply_input(req.model_dump())
    except InputUnavailable as e:
        raise HTTPException(503, str(e))
    except RuntimeError as e:   # load_proto(): нет input_pb2 / grpcio-tools
        log.error('Input protobuf недоступен: %s', e)
        raise HTTPException(500, str(e))


@app.post('/restart')
def ice_restart():
    """
    Не 'ICE Restart', который что-то делает на сервере — тут нечему: offerer в этой
    архитектуре браузер, а не воркер, так что инициировать restart может только он.
    Настоящий ICE restart — это со стороны клиента pc.restartIce() и повторный
    createOffer() + POST /sdp на этом же соединении; handle_offer() уже умеет
    переиспользовать текущий webrtcbin для такого повторного offer (см. docstring
    модуля), не трогая data channel'ы. Этот эндпоинт оставлен, чтобы явно сказать
    об этом вызывающему, а не молча возвращать 404.
    """
    raise HTTPException(
        501,
        'Серверного ICE Restart здесь нет: offerer — браузер. '
        'Вызови pc.restartIce() + createOffer() и отправь новый offer на POST /sdp.'
    )


# ---------------------------------------------------------------------------
# Демо-страница
# ---------------------------------------------------------------------------

DEMO_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>CG Worker dev stream</title>
    <style>
        body { margin: 0; background: #111; color: #eee; font-family: monospace; }
        video { width: 100vw; height: 82vh; background: #000; cursor: crosshair; }
        .line { padding: 4px 12px; font-size: 14px; }
        #input { color: #8f8; }
        #metrics { color: #6cf; }
    </style>
</head>
<body tabindex="0">
    <video id="v" autoplay playsinline muted></video>
    <div id="status" class="line">init…</div>
    <div id="input" class="line">input: —</div>
    <div id="metrics" class="line">metrics: —</div>
    <script>
        const video = document.getElementById('v');
        const statusEl = document.getElementById('status');
        const metricsEl = document.getElementById('metrics');
        const set = t => statusEl.textContent = t;

        // Оба data channel создаём ДО createOffer() — так они попадают в первый же
        // offer одним SDP-раундом, без отдельной renegotiation. 'input' ordered (нам
        // важен порядок down/up), 'metrics' unordered+maxRetransmits=0 (свежее важнее
        // полноты, отстающий пакет с fps никому не нужен).
        let inputChannel = null;
        let metricsChannel = null;

        async function start() {
            const pc = new RTCPeerConnection();   // localhost: STUN не нужен
            window.pc = pc;

            inputChannel = pc.createDataChannel('input', { ordered: true });
            metricsChannel = pc.createDataChannel('metrics', { ordered: false, maxRetransmits: 0 });

            inputChannel.onopen = () => { set('input channel: open'); showInput(); };
            inputChannel.onclose = () => showInput();
            metricsChannel.onmessage = e => {
                try {
                    const m = JSON.parse(e.data);
                    metricsEl.textContent = `metrics: ${m.fps} fps · ${m.bitrate_kbps} kbps` +
                        (m.width ? ` · ${m.width}x${m.height}` : '');
                } catch (err) { /* мусор в канале — просто игнорируем один кадр метрик */ }
            };

            // Без transceiver offer получается без видео m-line
            pc.addTransceiver('video', { direction: 'recvonly' });

            pc.ontrack = e => {
                video.srcObject = e.streams[0];
                video.play().catch(() => {});
            };
            pc.onconnectionstatechange = () => {
                set('connection: ' + pc.connectionState);
            };

            const offer = await pc.createOffer();
            await pc.setLocalDescription(offer);

            // non-trickle: ждём завершения ICE gathering (предохранитель 5с)
            await Promise.race([
                new Promise(res => {
                    if (pc.iceGatheringState === 'complete') return res();
                    pc.addEventListener('icegatheringstatechange', () => {
                        if (pc.iceGatheringState === 'complete') res();
                    });
                }),
                new Promise(res => setTimeout(res, 5000)),
            ]);

            set('sending offer to worker /sdp …');
            const r = await fetch('/sdp', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    sdp: pc.localDescription.sdp,
                    type: 'offer'
                }),
            });
            if (!r.ok) {
                set('OFFER FAILED: HTTP ' + r.status + ' ' + await r.text());
                return;
            }

            const answer = await r.json();
            await pc.setRemoteDescription(answer);
            set('answer set, waiting for connection…');
        }

        start().catch(e => set('error: ' + e));

        // ------------------------------------------------------------------
        // Ввод: KeyboardEvent.code -> Linux key code (input-event-codes.h).
        // Мышь: pointer lock (клик по видео, Esc — выйти). Уходит через data
        // channel 'input', пока он не открыт (первые доли секунды) — через
        // POST /input, чтобы не терять нажатия в момент подключения.
        // ------------------------------------------------------------------
        const KEYMAP = {
            // движение + модификаторы
            KeyW: 17, KeyA: 30, KeyS: 31, KeyD: 32, Space: 57,
            ShiftLeft: 42, ControlLeft: 29, AltLeft: 56,
            // меню/консоль
            Escape: 1, Enter: 28, Tab: 15, Backspace: 14, Backquote: 41,
            ArrowUp: 103, ArrowDown: 108, ArrowLeft: 105, ArrowRight: 106,
            // быстрое переключение оружия
            Digit1: 2, Digit2: 3, Digit3: 4, Digit4: 5, Digit5: 6,
            Digit6: 7, Digit7: 8, Digit8: 9, Digit9: 10, Digit0: 11,
            // остальные буквы
            KeyQ: 16, KeyE: 18, KeyR: 19, KeyT: 20, KeyY: 21, KeyP: 25,
            KeyF: 33, KeyG: 34, KeyH: 35,
            KeyZ: 44, KeyX: 45, KeyC: 46, KeyV: 47, KeyB: 48,
        };
        const MOUSE_BIT = { 0: 1, 2: 2, 1: 4 };  // MouseEvent.button -> бит button_mask (left/right/middle)
        const inputEl = document.getElementById('input');
        const held = new Set();                  // KeyboardEvent.code зажатых клавиш
        let mouseButtons = 0;                    // текущая маска кнопок мыши
        const locked = () => document.pointerLockElement === video;

        function showInput() {
            const parts = [...held];
            if (mouseButtons) parts.push('mouse:' + mouseButtons);
            const via = (inputChannel && inputChannel.readyState === 'open') ? 'DC' : 'HTTP';
            inputEl.textContent = 'input[' + via + ']: ' + (parts.length ? parts.join(' + ') : '—') +
                (locked() ? '   [мышь захвачена, Esc — отпустить]' : '   [клик по видео — захватить мышь]');
        }
        showInput();

        // HTTP-путь — запасной (до открытия DC, или если DC вдруг отвалился). Запросы
        // идут строго по очереди: иначе down и up могут обогнать друг друга. Сам DC
        // ordered:true, ему такая очередь не нужна — там порядок гарантирует браузер.
        let httpQueue = Promise.resolve();
        function sendInput(payload) {
            if (inputChannel && inputChannel.readyState === 'open') {
                try {
                    inputChannel.send(JSON.stringify(payload));
                    return;
                } catch (e) {
                    inputEl.textContent = 'input DC error, падаю на HTTP: ' + e;
                    // не return — уходим в HTTP-путь ниже как запасной
                }
            }
            httpQueue = httpQueue
                .then(() => fetch('/input', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                    keepalive: true,
                }))
                .then(async r => {
                    if (!r.ok) inputEl.textContent = 'input error: HTTP ' + r.status + ' ' + await r.text();
                })
                .catch(e => { inputEl.textContent = 'input error: ' + e; });
        }

        // ---- клавиатура ----
        window.addEventListener('keydown', e => {
            const code = KEYMAP[e.code];
            if (code === undefined) return;
            e.preventDefault();                       // Space не должен скроллить страницу
            if (e.repeat || held.has(e.code)) return; // автоповтор браузера нам не нужен
            held.add(e.code);
            showInput();
            sendInput({ keys_down: [code] });
        });

        window.addEventListener('keyup', e => {
            const code = KEYMAP[e.code];
            if (code === undefined) return;
            e.preventDefault();
            if (!held.delete(e.code)) return;
            showInput();
            sendInput({ keys_up: [code] });
        });

        // ---- мышь (pointer lock) ----
        let accX = 0, accY = 0, accWheel = 0, flushTimer = null;

        function flushMouse() {                  // отправляет накопленное движение (~60 раз/с)
            flushTimer = null;
            const dx = Math.trunc(accX), dy = Math.trunc(accY), wheel = Math.trunc(accWheel);
            accX -= dx; accY -= dy; accWheel -= wheel;   // дробный остаток не теряем
            if (dx || dy || wheel) sendInput({ mouse: { dx, dy, wheel, buttons: mouseButtons } });
        }
        function queueMouse() {
            if (flushTimer === null) flushTimer = setTimeout(flushMouse, 16);
        }
        function sendButtons() {
            flushMouse();                        // сначала накопленное движение, потом клик
            sendInput({ mouse: { buttons: mouseButtons } });
            showInput();
        }

        video.addEventListener('click', () => { if (!locked()) video.requestPointerLock(); });

        document.addEventListener('pointerlockchange', () => {
            if (!locked() && mouseButtons) { mouseButtons = 0; sendButtons(); }  // вышли с зажатой кнопкой
            showInput();
        });
        document.addEventListener('mousemove', e => {
            if (!locked()) return;
            accX += e.movementX; accY += e.movementY;
            queueMouse();
        });
        document.addEventListener('mousedown', e => {
            if (!locked()) return;
            e.preventDefault();
            mouseButtons |= MOUSE_BIT[e.button] || 0;
            sendButtons();
        });
        document.addEventListener('mouseup', e => {
            const bit = MOUSE_BIT[e.button] || 0;
            if (!(mouseButtons & bit)) return;   // не ограничиваем locked: кнопку надо отпустить в любом случае
            mouseButtons &= ~bit;
            sendButtons();
        });
        document.addEventListener('wheel', e => {
            if (!locked()) return;
            e.preventDefault();
            accWheel += e.deltaY > 0 ? -1 : (e.deltaY < 0 ? 1 : 0);   // вверх = положительное (REL_WHEEL)
            queueMouse();
        }, { passive: false });
        document.addEventListener('contextmenu', e => { if (locked()) e.preventDefault(); });

        // Потеряли фокус — keyup/mouseup мы уже не увидим: отпускаем всё, чтобы ничего не залипло
        function releaseAll() {
            const codes = [...held].map(c => KEYMAP[c]);
            held.clear();
            if (codes.length) sendInput({ keys_up: codes });
            if (mouseButtons) { mouseButtons = 0; sendInput({ mouse: { buttons: 0 } }); }
            showInput();
        }
        window.addEventListener('blur', releaseAll);
        document.addEventListener('visibilitychange', () => { if (document.hidden) releaseAll(); });

        // Heartbeat: пока что-то зажато, раз в секунду подтверждаем состояние.
        // Если страница умрёт, инжектор через 3 с сам отпустит всё (watchdog).
        setInterval(() => { if (held.size || mouseButtons) sendInput({ heartbeat: true }); }, 1000);
    </script>
</body>
</html>
"""


@app.get('/', response_class=HTMLResponse)
def index():
    """Демо-страница с <video> и WebRTC-клиентом."""
    return DEMO_PAGE


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    uvicorn.run(app, host='0.0.0.0', port=9090, log_level='info')
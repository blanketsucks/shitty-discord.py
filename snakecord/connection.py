import os
import http
import base64
import json
import time
import platform
import functools
import asyncio
import urllib.parse

from enum import IntEnum

from .events import EventPusher
from .utils import JsonStructure, JsonField, cstruct
from .exceptions import BadWsHttpResponse


class WebsocketOpcode(IntEnum):
    CONT = 0x00
    TEXT = 0x01
    BINARY = 0x02
    CLOSE = 0x08
    PING = 0x09
    PONG = 0x0A


class WebsocketFrame(cstruct):
    byteorder = '>'
    fbyte: cstruct.UnsignedChar
    sbyte: cstruct.UnsignedChar

    @staticmethod
    def get_fin(byte):
        return byte & 0b10000000

    @staticmethod
    def get_rsv1(byte):
        return byte & 0b01000000

    @staticmethod
    def get_rsv2(byte):
        return byte & 0b00100000

    @staticmethod
    def get_rsv3(byte):
        return byte & 0b00010000

    @staticmethod
    def get_opcode(byte):
        return byte & 0b00001111

    @staticmethod
    def get_mask(byte):
        return byte & 0b10000000

    @staticmethod
    def get_length(byte):
        return byte & 0b01111111

    @staticmethod
    def apply_mask(mask, data):
        for i in range(len(data)):
            data[i] ^= mask[i % 4]

    @classmethod
    def create_frame(
        cls, data, *, opcode=WebsocketOpcode.TEXT,
        fin=True, rsv1=False, rsv2=False, rsv3=False,
        masked=True
    ):
        buffer = bytearray(2)

        if fin:
            buffer[0] |= 0b10000000

        if rsv1:
            buffer[0] |= 0b01000000

        if rsv2:
            buffer[0] |= 0b00100000

        if rsv3:
            buffer[0] |= 0b00010000

        buffer[0] |= opcode

        if masked:
            buffer[1] |= 0b10000000

        length = len(data)
        if length <= 125:
            buffer[1] |= length
        else:
            if length <= 0xFFFF:
                buffer[1] |= 126
                size = cstruct.UnsignedShort.size
            else:
                buffer[1] |= 127
                size = cstruct.UnsignedLongLong.size

            buffer.extend(length.to_bytes(size, 'big', signed=False))

        if masked:
            data = bytearray(data)
            mask = os.urandom(4)
            buffer.extend(mask)
            cls.apply_mask(mask, data)

        buffer.extend(data)

        return buffer


class WebsocketProtocolState(IntEnum):
    WAITING_FBYTE = 0
    WAITING_SBYTE = 1
    WAITING_LENGTH = 2
    WAITING_DATA = 3


class WebsocketProtocol(asyncio.Protocol):
    def __init__(self, connection):
        self.connection = connection
        self.state = WebsocketProtocolState.WAITING_FBYTE
        self.frame = WebsocketFrame()
        self.headers = b''
        self.have_headers = asyncio.Event()

    def create_frames(self, data):
        position = 0
        while True:
            if position >= len(data):
                return

            if self.state == WebsocketProtocolState.WAITING_FBYTE:
                self.frame.fbyte = data[position]
                position += 1
                self.state = WebsocketProtocolState.WAITING_SBYTE

            if position >= len(data):
                return

            if self.state == WebsocketProtocolState.WAITING_SBYTE:
                self.frame.data = b''

                self.frame.sbyte = data[position]
                position += 1
                self.frame.length = WebsocketFrame.get_length(self.frame.sbyte)

                if self.frame.length > 125:
                    self.frame.length_buffer = b''
                    self.state = WebsocketProtocolState.WAITING_LENGTH

                    if self.frame.length == 126:
                        self.frame.bytes_needed = cstruct.UnsignedShort.size
                    elif self.frame.length == 127:
                        self.frame.bytes_needed = cstruct.UnsignedLongLong.size
                else:
                    self.frame.bytes_needed = self.frame.length
                    self.state = WebsocketProtocolState.WAITING_DATA

            if position >= len(data):
                return

            if self.state == WebsocketProtocolState.WAITING_LENGTH:
                length_bytes = data[position:position + self.frame.bytes_needed]
                position += len(length_bytes)
                self.frame.bytes_needed -= len(length_bytes)
                self.frame.length_buffer += length_bytes

                if self.frame.bytes_needed == 0:
                    self.frame.length = int.from_bytes(self.frame.length_buffer, 'big', signed=False)
                    self.frame.bytes_needed = self.frame.length
                    self.state = WebsocketProtocolState.WAITING_DATA

            if position >= len(data):
                return

            if self.state == WebsocketProtocolState.WAITING_DATA:
                data_bytes = data[position:position + self.frame.bytes_needed]
                position += len(data_bytes)
                self.frame.bytes_needed -= len(data_bytes)
                self.frame.data += data_bytes

                if self.frame.bytes_needed == 0:
                    self.connection.push_event('ws_frame_receive', self.frame)
                    self.frame = WebsocketFrame()
                    self.state = WebsocketProtocolState.WAITING_FBYTE

            if position >= len(data):
                return

    def data_received(self, data):
        if not self.have_headers.is_set():
            try:
                index = data.index(b'\r\n\r\n')
                self.headers += data[:index + 4]
                self.have_headers.set()
                self.create_frames(data[index + 4:])
            except ValueError:
                self.headers += data
        else:
            self.create_frames(data)


class DiscordResponse(JsonStructure):
    __json_fields__ = {
        'opcode': JsonField('op'),
        'sequence': JsonField('s'),
        'event_name': JsonField('t'),
        'data': JsonField('d')
    }


class BaseConnection(EventPusher):
    def __init__(self, endpoint, pusher):
        super().__init__(pusher.loop)

        self.endpoint = endpoint
        self.loop = pusher.loop
        self.pusher = pusher

        self.register_listener('connection_stale', self.connection_stale)
        self.register_listener('ws_frame_receive', self.ws_frame_receive)
        self.register_listener('ws_receive', self.ws_receive)

        self.heartbeat_handler = HeartbeatHandler(self)

        self.transport = None
        self.protocol = None

        self.sec_ws_key = base64.b64encode(os.urandom(16))

    @property
    def heartbeat_payload(self):
        raise NotImplementedError

    async def connection_stale(self):
        raise NotImplementedError

    def ws_frame_receive(self, frame):
        if WebsocketFrame.get_opcode(frame.fbyte) == WebsocketOpcode.TEXT:
            response = DiscordResponse.unmarshal(frame.data)
            self.push_event('ws_receive', response)

    async def ws_receive(self, response):
        raise NotImplementedError

    def form_headers(self, meth, path, headers):
        parts = ['%s %s HTTP/1.0' % (meth, path)]

        for name, value in headers.items():
            parts.append('%s: %s' % (name, value))

        parts.append('\r\n')

        return '\r\n'.join(parts).encode()

    def iter_headers(self):
        offset = 0
        while True:
            index = self.protocol.headers.index(b'\r\n', offset) + 2
            data = self.protocol.headers[offset:index]
            offset = index
            if data == b'\r\n':
                return
            yield [value.strip().lower() for value in data.split(b':', 1)]

    async def connect(self, **kwargs):
        headers = kwargs.pop('headers', {})

        url = urllib.parse.urlparse(self.endpoint)

        self.transport, self.protocol = await self.loop.create_connection(
            lambda: WebsocketProtocol(self), url.hostname, **kwargs
        )

        headers.update({
            'Host': url.hostname,
            'Connection': 'Upgrade',
            'Upgrade': 'websocket',
            'Sec-WebSocket-Key': self.sec_ws_key.decode(),
            'Sec-WebSocket-Version': 13
        })

        path = (url.path + url.params) or '/'
        self.transport.write(self.form_headers('GET', path, headers))

        await self.protocol.have_headers.wait()
        headers = self.iter_headers()
        status, = next(headers)

        status_code = status.split(b' ')[1].decode()
        if int(status_code) != http.HTTPStatus.SWITCHING_PROTOCOLS:
            raise BadWsHttpResponse(
                'status code',
                http.HTTPStatus.SWITCHING_PROTOCOLS,
                status_code
            )

        headers = dict(headers)

        connection = headers.get(b'connection').decode()
        if connection != 'upgrade':
            raise BadWsHttpResponse('connection', 'upgrade', connection)

        upgrade = headers.get(b'upgrade').decode()
        if upgrade != 'websocket':
            raise BadWsHttpResponse('upgrade', 'websocket', upgrade)

    def send(self, data, *args, **kwargs):
        data = WebsocketFrame.create_frame(data, *args, **kwargs)
        self.transport.write(data)

    def send_json(self, data):
        self.send(json.dumps(data).encode())


class HeartbeatHandler:
    def __init__(self, connection, *, timeout=10):
        self.connection = connection
        self.loop = connection.loop
        self.timeout = timeout

        self.heartbeat_interval = float('inf')
        self.heartbeats_sent = 0
        self.heartbeats_acked = 0
        self.last_sent = float('inf')
        self.last_acked = float('inf')

        self.current_handle = None
        self.stopped = False

    def do_heartbeat(self):
        if self.stopped:
            return

        func = functools.partial(self.loop.create_task, self.send_heartbeat())
        self.current_handle = self.loop.call_later(self.heartbeat_interval, func)

    async def send_heartbeat(self):
        if self.stopped:
            return

        paylod = self.connection.heartbeat_payload
        self.last_sent = time.perf_counter()
        self.connection.send_json(paylod)

        await self.wait_ack()

        self.do_heartbeat()

    async def wait_ack(self):
        try:
            await self.connection.wait(
                'heartbeat_ack',
                timeout=self.timeout,
            )
            self.last_acked = time.perf_counter()
            self.heartbeats_acked += 1
        except asyncio.TimeoutError:
            self.stop()
            self.connection.push_event('connection_stale')

    def start(self):
        self.loop.create_task(self.send_heartbeat())

    def stop(self):
        self.stopped = True
        if self.current_handle is not None:
            self.current_handle.cancel()

    @property
    def latency(self):
        return self.last_acked - self.last_sent


class ShardOpcode(IntEnum):
    DISPATCH = 0
    HEARTBEAT = 1
    IDENTIFY = 2
    PRESENCE_UPDATE = 3
    VOICE_STATE_UPDATE = 4
    RESUME = 6
    RECONNECT = 7
    REQUEST_GUILD_MEMBERS = 8
    INVALID_SESSION = 9
    HELLO = 10
    HEARTBEAT_ACK = 11


class Shard(BaseConnection):
    def __init__(self, endpoint, pusher, shard_id):
        super().__init__(endpoint, pusher)
        self.id = shard_id

    @property
    def identify_payload(self):
        payload = {
            'op': ShardOpcode.IDENTIFY,
            'd': {
                'token': self.pusher.token,
                # 'intents': self.manager.intents,
                'properties': {
                    '$os': platform.system(),
                    '$browser': 'wrapper-we-dont-name-for',
                    '$device': '^'
                }
            }
        }
        if self.pusher.multi_sharded:
            payload['shard'] = [self.id, len(self.pusher.shards)]
        return payload

    @property
    def heartbeat_payload(self):
        payload = {
            'op': ShardOpcode.HEARTBEAT,
            'd': None
        }
        return payload

    async def ws_receive(self, response):
        if response.opcode == ShardOpcode.HELLO:
            self.send_json(self.identify_payload)
            interval = response.data['heartbeat_interval'] / 1000
            self.heartbeat_handler.heartbeat_interval = interval
            self.heartbeat_handler.start()
        elif response.opcode == ShardOpcode.HEARTBEAT_ACK:
            self.push_event('heartbeat_ack')
        elif response.opcode == ShardOpcode.DISPATCH:
            self.pusher.push_event(response.event_name, response.data)


class VoiceConnectionOpcode(IntEnum):
    IDENTIFY = 0
    SELECT = 1
    READY = 2
    HEARTBEAT = 3
    SESSION_DESCRIPTION = 4
    SPEAKING = 5
    HEARTBEAT_ACK = 6
    RESUME = 7
    HELLO = 8
    RESUMED = 9
    CLIENT_DISCONNECT = 13


class SpeakingState(IntEnum):
    NONE = 0
    VOICE = 1
    SOUNDSHARE = 2
    PRIORITY = 4


"""
class VoiceWSProtocol(ConnectionBase):
    def __init__(self, voice_connection):
        self.voice_connection = voice_connection
        super().__init__(voice_connection.client, voice_connection.endpoint)

    @property
    def heartbeat_payload(self):
        payload = {
            'op': VoiceConnectionOpcode.HEARTBEAT,
            'd': 0
        }
        return payload

    @property
    def identify_payload(self):
        payload = {
            'op': VoiceConnectionOpcode.IDENTIFY,
            'd': {
                'server_id': self.voice_connection.guild_id,
                'user_id': self.voice_connection.voice_state.member.user.id,
                'session_id': self.voice_connection.voice_state.session_id,
                'token': self.voice_connection.token
            }
        }
        return payload

    @property
    def select_payload(self):
        payload = {
            'op': VoiceConnectionOpcode.SELECT,
            'd': {
                'protocol': 'udp',
                'data': {
                    'address': self.voice_connection.protocol.ip,
                    'port': self.voice_connection.protocol.port,
                    'mode': self.voice_connection.mode
                }
            }
        }
        return payload

    # async def send_speaking(self, state=SpeakingState.VOICE):
    #     payload = {
    #        'op': VoiceConnectionOpcode.SPEAKING,
    #        'd': {
    #            'speaking': state.value,
    #            'delay': 0
    #        }
    #    }
    #    await self.websocket.send_json(payload)

    async def select(self):
        await self.websocket.send_json(self.select_payload)

    async def dispatch(self, resp):
        if resp.opcode == VoiceConnectionOpcode.HELLO:
            await self.websocket.send_json(self.identify_payload)

            self.websocket.heartbeat_interval = \
                resp.data['heartbeat_interval'] / 1000
            self.websocket.do_heartbeat()
        elif resp.opcode == VoiceConnectionOpcode.HEARTBEAT_ACK:
            self.websocket.heartbeat_ack()
        else:
            await self.voice_connection.dispatch(resp)


class VoiceUDPProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self.ip = None
        self.port = None
        self.mode = None
        self.selected = False
        self.voice_connection = None

    async def _datagram_received(self, data):
        if not self.selected:
            end = data.index(0, 4)
            ip = data[4:end]
            self.ip = ip.decode()

            port = data[-2:]
            self.port = int.from_bytes(port, 'big')

            await self.voice_connection.ws.select()
            self.selected = True
        else:
            await self.voice_connection.datagram_received(data)

    def datagram_received(self, data, addr):
        self.voice_connection.loop.create_task(self._datagram_received(data))
"""

"""
SPIKE Prime BLE protocol: framing (COBS + XOR), CRC and message types.

Modified from the SPIKE Prime protocol docs and example by the LEGO Group,
Copyright 2024 the LEGO Group, Apache License 2.0 (with the LEGO Group's
section 6 modification): https://github.com/LEGO/spike-prime-docs
"""

from __future__ import annotations

import struct
from binascii import crc32
from dataclasses import dataclass

SERVICE_UUID = "0000fd02-0000-1000-8000-00805f9b34fb"
RX_CHAR_UUID = "0000fd02-0001-1000-8000-00805f9b34fb"  # we write to the hub
TX_CHAR_UUID = "0000fd02-0002-1000-8000-00805f9b34fb"  # hub notifies us

# --- Framing -----------------------------------------------------------------

DELIMITER = 0x02
NO_DELIMITER = 0xFF
COBS_CODE_OFFSET = DELIMITER
MAX_BLOCK_SIZE = 84
XOR = 3


def cobs_encode(data: bytes) -> bytearray:
    buffer = bytearray()
    code_index = block = 0

    def begin_block():
        nonlocal code_index, block
        code_index = len(buffer)
        buffer.append(NO_DELIMITER)
        block = 1

    begin_block()
    for byte in data:
        if byte > DELIMITER:
            buffer.append(byte)
            block += 1
        if byte <= DELIMITER or block > MAX_BLOCK_SIZE:
            if byte <= DELIMITER:
                buffer[code_index] = byte * MAX_BLOCK_SIZE + block + COBS_CODE_OFFSET
            begin_block()
    buffer[code_index] = block + COBS_CODE_OFFSET
    return buffer


def cobs_decode(data: bytes) -> bytearray:
    buffer = bytearray()

    def unescape(code: int):
        if code == 0xFF:
            return None, MAX_BLOCK_SIZE + 1
        value, block = divmod(code - COBS_CODE_OFFSET, MAX_BLOCK_SIZE)
        if block == 0:
            block = MAX_BLOCK_SIZE
            value -= 1
        return value, block

    value, block = unescape(data[0])
    for byte in data[1:]:
        block -= 1
        if block > 0:
            buffer.append(byte)
            continue
        if value is not None:
            buffer.append(value)
        value, block = unescape(byte)
    return buffer


def pack(payload: bytes) -> bytes:
    """Encode a message payload into a frame ready to send."""
    buffer = cobs_encode(payload)
    for i in range(len(buffer)):
        buffer[i] ^= XOR
    buffer.append(DELIMITER)
    return bytes(buffer)


def unpack(frame: bytes) -> bytes:
    """Decode a complete frame (ending in DELIMITER) into a message payload."""
    start = 1 if frame[0] == 0x01 else 0  # optional priority byte
    return bytes(cobs_decode(bytes(b ^ XOR for b in frame[start:-1])))


def crc(data: bytes, seed: int = 0, align: int = 4) -> int:
    remainder = len(data) % align
    if remainder:
        data += b"\x00" * (align - remainder)
    return crc32(data, seed)


# --- Messages ----------------------------------------------------------------


class Message:
    ID: int = -1

    def serialize(self) -> bytes:
        raise NotImplementedError

    def __repr__(self) -> str:
        fields = ", ".join(f"{k}={v!r}" for k, v in vars(self).items())
        return f"{type(self).__name__}({fields})"


class StatusResponse(Message):
    def __init__(self, success: bool):
        self.success = success

    @classmethod
    def deserialize(cls, data: bytes):
        return cls(data[1] == 0x00)


class InfoRequest(Message):
    ID = 0x00

    def serialize(self):
        return bytes([self.ID])


class InfoResponse(Message):
    ID = 0x01

    @classmethod
    def deserialize(cls, data: bytes):
        msg = cls()
        (
            _,
            msg.rpc_major,
            msg.rpc_minor,
            msg.rpc_build,
            msg.firmware_major,
            msg.firmware_minor,
            msg.firmware_build,
            msg.max_packet_size,
            msg.max_message_size,
            msg.max_chunk_size,
            msg.product_group_device,
        ) = struct.unpack("<BBBHBBHHHHH", data)
        return msg


class StartFileUploadRequest(Message):
    ID = 0x0C

    def __init__(self, file_name: str, slot: int, crc: int):
        self.file_name, self.slot, self.crc = file_name, slot, crc

    def serialize(self):
        name = self.file_name.encode("utf8")
        if len(name) > 31:
            raise ValueError("file name too long (max 31 bytes)")
        return struct.pack(f"<B{len(name) + 1}sBI", self.ID, name, self.slot, self.crc)


class StartFileUploadResponse(StatusResponse):
    ID = 0x0D


class TransferChunkRequest(Message):
    ID = 0x10

    def __init__(self, running_crc: int, chunk: bytes):
        self.running_crc, self.chunk = running_crc, chunk

    def serialize(self):
        size = len(self.chunk)
        return struct.pack(f"<BIH{size}s", self.ID, self.running_crc, size, self.chunk)


class TransferChunkResponse(StatusResponse):
    ID = 0x11


class ProgramFlowRequest(Message):
    ID = 0x1E

    def __init__(self, stop: bool, slot: int):
        self.stop, self.slot = stop, slot

    def serialize(self):
        return struct.pack("<BBB", self.ID, self.stop, self.slot)


class ProgramFlowResponse(StatusResponse):
    ID = 0x1F


class ProgramFlowNotification(Message):
    ID = 0x20

    def __init__(self, stop: bool):
        self.stop = stop

    @classmethod
    def deserialize(cls, data: bytes):
        return cls(bool(data[1]))


class ConsoleNotification(Message):
    ID = 0x21

    def __init__(self, text: str):
        self.text = text

    @classmethod
    def deserialize(cls, data: bytes):
        return cls(data[1:].rstrip(b"\0").decode("utf8", errors="replace"))


class ClearSlotRequest(Message):
    ID = 0x46

    def __init__(self, slot: int):
        self.slot = slot

    def serialize(self):
        return struct.pack("<BB", self.ID, self.slot)


class ClearSlotResponse(StatusResponse):
    ID = 0x47


class DeviceNotificationRequest(Message):
    """Ask the hub to stream sensor data every interval_ms (0 = stop)."""

    ID = 0x28

    def __init__(self, interval_ms: int):
        self.interval_ms = interval_ms

    def serialize(self):
        return struct.pack("<BH", self.ID, self.interval_ms)


class DeviceNotificationResponse(StatusResponse):
    ID = 0x29


# --- Sensor data (inside DeviceNotification) ---------------------------------

PORTS = "ABCDEF"
HUB_FACES = ["top", "front", "right", "bottom", "back", "left"]
MOTOR_TYPES = {0x30: "medium", 0x31: "large", 0x41: "small"}
COLORS = {
    -1: "none", 0: "black", 1: "magenta", 2: "purple", 3: "blue", 4: "azure",
    5: "turquoise", 6: "green", 7: "yellow", 8: "orange", 9: "red", 10: "white",
}


def _face(value: int) -> str:
    return HUB_FACES[value] if value < len(HUB_FACES) else str(value)


def _port(value: int) -> str:
    return PORTS[value] if value < len(PORTS) else str(value)


@dataclass
class Battery:
    percent: int


@dataclass
class Imu:
    face_up: str
    yaw_face: str
    yaw: float  # degrees
    pitch: float
    roll: float
    accel_x: int  # milli-g (about 1000 = gravity)
    accel_y: int
    accel_z: int
    gyro_x: int
    gyro_y: int
    gyro_z: int


@dataclass
class Display:
    pixels: list[int]  # 25 brightness values, row by row


@dataclass
class Motor:
    port: str
    type: str
    absolute_position: int  # degrees, -180..179
    power: int  # -10000..10000
    speed: int  # -100..100
    position: int  # degrees since start


@dataclass
class ForceSensor:
    port: str
    force: int  # 0..100
    pressed: bool


@dataclass
class ColorSensor:
    port: str
    color: str
    reflection: int  # reflected light, percent
    red: int  # 0..1023
    green: int
    blue: int


@dataclass
class DistanceSensor:
    port: str
    distance_mm: int  # 40..2000, -1 when nothing detected


@dataclass
class ColorMatrix:
    port: str
    pixels: list[int]  # 9 values: brightness in high nibble, color in low nibble


def _battery(v):
    return Battery(v[1])


def _imu(v):
    # The hub sends yaw/pitch/roll in tenths of a degree.
    yaw, pitch, roll = (x / 10 for x in v[3:6])
    return Imu(_face(v[1]), _face(v[2]), yaw, pitch, roll, *v[6:])


def _display(v):
    return Display(list(v[1:]))


def _motor(v):
    return Motor(_port(v[1]), MOTOR_TYPES.get(v[2], hex(v[2])), *v[3:])


def _force(v):
    return ForceSensor(_port(v[1]), v[2], bool(v[3]))


def _color(v):
    return ColorSensor(_port(v[1]), COLORS.get(v[2], str(v[2])), *v[3:])


def _distance(v):
    return DistanceSensor(_port(v[1]), v[2])


def _color_matrix(v):
    return ColorMatrix(_port(v[1]), list(v[2:]))


# message id -> (struct format, parser)
DEVICE_MESSAGES = {
    0x00: ("<BB", _battery),
    0x01: ("<BBBhhhhhhhhh", _imu),
    0x02: ("<B25B", _display),
    0x0A: ("<BBBhhbi", _motor),
    0x0B: ("<BBBB", _force),
    # Firmware 1.8 sends a reflection byte after the color that the docs don't list.
    0x0C: ("<BBbBHHH", _color),
    0x0D: ("<BBh", _distance),
    0x0E: ("<BB9B", _color_matrix),
}


class DeviceNotification(Message):
    """A snapshot of every sensor, sent periodically by the hub."""

    ID = 0x3C

    def __init__(self, readings: list):
        self.readings = readings

    @classmethod
    def deserialize(cls, data: bytes):
        (size,) = struct.unpack_from("<H", data, 1)
        payload = data[3 : 3 + size]
        readings = []
        while payload:
            entry = DEVICE_MESSAGES.get(payload[0])
            if entry is None:
                break  # unknown device type: we can't know its size, so stop here
            fmt, parse = entry
            n = struct.calcsize(fmt)
            if len(payload) < n:
                break
            readings.append(parse(struct.unpack(fmt, payload[:n])))
            payload = payload[n:]
        return cls(readings)


_INCOMING = {
    m.ID: m
    for m in (
        InfoResponse,
        StartFileUploadResponse,
        TransferChunkResponse,
        ProgramFlowResponse,
        ProgramFlowNotification,
        ConsoleNotification,
        ClearSlotResponse,
        DeviceNotificationResponse,
        DeviceNotification,
    )
}


def deserialize(payload: bytes) -> Message | None:
    """Parse an incoming payload; returns None for message types we ignore."""
    cls = _INCOMING.get(payload[0])
    return cls.deserialize(payload) if cls else None

"""Connect to a SPIKE Prime hub over Bluetooth and run programs on it."""

from __future__ import annotations

import asyncio
from typing import Callable, TypeVar

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from . import protocol as p

T = TypeVar("T", bound=p.Message)


class SpikeHub:
    """
    Usage:
        async with SpikeHub() as hub:
            await hub.run_program(source_code)
            await hub.wait_until_program_stops()
    """

    def __init__(
        self,
        name: str | None = None,
        scan_timeout: float = 10.0,
        on_console: Callable[[str], None] = lambda text: print(text, end="", flush=True),
    ):
        self.name = name
        self.scan_timeout = scan_timeout
        self.on_console = on_console
        self.info: p.InfoResponse | None = None
        self.device_name: str | None = None
        self._client: BleakClient | None = None
        self._rx_buffer = bytearray()
        self._pending: tuple[int, asyncio.Future] | None = None
        self._on_sensors: Callable[[list], None] | None = None
        self._program_stopped = asyncio.Event()
        self._disconnected = asyncio.Event()

    # --- connection --------------------------------------------------------

    async def connect(self) -> None:
        def matches(device: BLEDevice, adv: AdvertisementData) -> bool:
            if p.SERVICE_UUID not in [u.lower() for u in adv.service_uuids]:
                return False
            return self.name is None or (device.name or "") == self.name

        print(f"Scanning for a SPIKE hub (up to {self.scan_timeout:.0f}s)...")
        device = await BleakScanner.find_device_by_filter(matches, timeout=self.scan_timeout)
        if device is None:
            raise RuntimeError(
                "No SPIKE hub found. Turn the hub on, press its Bluetooth button "
                "(it should blink), and close the SPIKE web app if it is connected."
            )

        self.device_name = device.name
        print(f"Found {device.name} ({device.address}). Connecting...")
        self._client = BleakClient(device, disconnected_callback=lambda _: self._on_disconnect())
        await self._client.connect()
        await self._client.start_notify(p.TX_CHAR_UUID, self._on_data)

        self.info = await self._request(p.InfoRequest(), p.InfoResponse)
        print(
            f"Connected. Firmware {self.info.firmware_major}.{self.info.firmware_minor}"
            f".{self.info.firmware_build}"
        )

    async def disconnect(self) -> None:
        if self._client and self._client.is_connected:
            await self._client.disconnect()

    @property
    def connected(self) -> bool:
        return self._client is not None and not self._disconnected.is_set()

    async def __aenter__(self) -> SpikeHub:
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    # --- programs ----------------------------------------------------------

    async def upload_program(self, source: str, slot: int = 0) -> None:
        """Store MicroPython source code in a program slot (0-19) on the hub."""
        data = source.encode("utf8")
        await self._request(p.ClearSlotRequest(slot), p.ClearSlotResponse)  # may fail if empty; fine

        reply = await self._request(
            p.StartFileUploadRequest("program.py", slot, p.crc(data)), p.StartFileUploadResponse
        )
        if not reply.success:
            raise RuntimeError("Hub refused the file upload")

        running_crc = 0
        step = self.info.max_chunk_size
        for i in range(0, len(data), step):
            chunk = data[i : i + step]
            running_crc = p.crc(chunk, running_crc)
            reply = await self._request(p.TransferChunkRequest(running_crc, chunk), p.TransferChunkResponse)
            if not reply.success:
                raise RuntimeError(f"Chunk transfer failed at byte {i}")

    async def start_program(self, slot: int = 0) -> None:
        self._program_stopped.clear()
        reply = await self._request(p.ProgramFlowRequest(stop=False, slot=slot), p.ProgramFlowResponse)
        if not reply.success:
            raise RuntimeError("Hub failed to start the program")

    async def stop_program(self, slot: int = 0) -> None:
        await self._request(p.ProgramFlowRequest(stop=True, slot=slot), p.ProgramFlowResponse)

    async def run_program(self, source: str, slot: int = 0) -> None:
        """Upload and start a program."""
        await self.upload_program(source, slot)
        await self.start_program(slot)

    @property
    def program_stopped(self) -> bool:
        """True once the running program ended (for example the centre button stopped it)."""
        return self._program_stopped.is_set()

    async def wait_until_program_stops(self) -> None:
        stopped = asyncio.create_task(self._program_stopped.wait())
        disconnected = asyncio.create_task(self._disconnected.wait())
        await asyncio.wait({stopped, disconnected}, return_when=asyncio.FIRST_COMPLETED)
        for task in (stopped, disconnected):
            task.cancel()

    # --- sensors -----------------------------------------------------------

    async def start_sensor_stream(self, on_sensors: Callable[[list], None], interval_ms: int = 100) -> None:
        """
        Call on_sensors(readings) every interval_ms with a list of readings
        (Battery, Imu, Display, Motor, ForceSensor, ColorSensor, DistanceSensor,
        ColorMatrix) from spike.protocol, for the hub and everything plugged in.
        """
        self._on_sensors = on_sensors
        reply = await self._request(p.DeviceNotificationRequest(interval_ms), p.DeviceNotificationResponse)
        if not reply.success:
            raise RuntimeError("Hub refused to stream sensor data")

    async def stop_sensor_stream(self) -> None:
        await self._request(p.DeviceNotificationRequest(0), p.DeviceNotificationResponse)
        self._on_sensors = None

    async def wait_until_disconnected(self) -> None:
        await self._disconnected.wait()

    # --- low level ---------------------------------------------------------

    async def _send(self, message: p.Message) -> None:
        frame = p.pack(message.serialize())
        size = self.info.max_packet_size if self.info else len(frame)
        for i in range(0, len(frame), size):
            await self._client.write_gatt_char(p.RX_CHAR_UUID, frame[i : i + size], response=False)

    async def _request(self, message: p.Message, response_type: type[T], timeout: float = 5.0) -> T:
        future = asyncio.get_running_loop().create_future()
        self._pending = (response_type.ID, future)
        try:
            await self._send(message)
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending = None

    def _on_data(self, _char, data: bytearray) -> None:
        # Notifications can be fragmented; buffer until the frame delimiter.
        self._rx_buffer.extend(data)
        while (end := self._rx_buffer.find(p.DELIMITER)) != -1:
            frame = bytes(self._rx_buffer[: end + 1])
            del self._rx_buffer[: end + 1]
            if len(frame) > 1:
                self._handle(p.deserialize(p.unpack(frame)))

    def _handle(self, message: p.Message | None) -> None:
        if message is None:
            return
        if self._pending and message.ID == self._pending[0] and not self._pending[1].done():
            self._pending[1].set_result(message)
        elif isinstance(message, p.ConsoleNotification):
            self.on_console(message.text)
        elif isinstance(message, p.DeviceNotification) and self._on_sensors:
            self._on_sensors(message.readings)
        elif isinstance(message, p.ProgramFlowNotification) and message.stop:
            self._program_stopped.set()

    def _on_disconnect(self) -> None:
        print("\nHub disconnected.")
        self._disconnected.set()

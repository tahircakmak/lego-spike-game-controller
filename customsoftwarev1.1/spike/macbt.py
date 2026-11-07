"""
A paired MINDSTORMS hub's Bluetooth serial channel on macOS, opened through IOBluetooth.

macOS makes a /dev/cu.<hub name> port for a paired HubOS 2 hub, but on current macOS that port
can stay completely silent even while the hub shows as Connected: the serial channel behind it
never opens. Opening the hub's Serial Port Profile channel (RFCOMM channel 1) directly through
IOBluetooth works, so LegacyHub uses this for paired hubs on macOS (and pyserial for the USB cable).

MacBluetoothLink is read like a pyserial port (read, in_waiting), so LegacyHub's reader thread
works unchanged. IOBluetooth itself only works on the main thread, and only hands over data while
that thread's macOS run loop turns, so the link keeps it turning from an asyncio task (the
program's asyncio loop runs on the main thread). Needs pyobjc-framework-IOBluetooth.
"""

from __future__ import annotations

import asyncio
import re
import sys
import threading
import time

SPP_CHANNEL = 1          # the hub's Serial Port Profile (HubOS 2's JSON messages and its REPL)
ATTEMPT_TIMEOUT_S = 4    # wait this long for the channel to open, twice (the second time on a fresh link)
_ADDRESS = re.compile(r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}")


def _plain(text) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def bluetooth_address(port: str) -> str | None:
    """
    The Bluetooth address behind a port like /dev/cu.LEGOHub2CAB3356B372 (a paired device), or None
    for anything else (the USB cable, other systems). An address like 2C:AB:33:56:B3:72 is taken as is.
    """
    if sys.platform != "darwin":
        return None
    if _ADDRESS.fullmatch(port):
        return port.replace(":", "-")
    name = port.rsplit("/", 1)[-1]
    if not name.startswith(("cu.", "tty.")):
        return None  # not a macOS serial port name: don't touch Bluetooth at all
    name = name.split(".", 1)[1]
    if "usbmodem" in name or "usbserial" in name:
        return None
    try:
        from IOBluetooth import IOBluetoothDevice
    except ImportError:
        return None
    for device in IOBluetoothDevice.pairedDevices() or []:
        # macOS names the port after the device, without spaces or colons: "LEGO Hub 2C:AB:..." -> LEGOHub2CAB...
        if _plain(device.name()) == _plain(name) or (_plain(device.addressString()) and
                                                     _plain(device.addressString()) in _plain(name)):
            return device.addressString()
    return None


_delegate_class = None


def _delegate():
    """The IOBluetooth delegate class (made once: an Objective-C class name can only be registered once)."""
    global _delegate_class
    if _delegate_class is None:
        from Foundation import NSObject

        class LegoHubRFCOMMDelegate(NSObject):
            def rfcommChannelData_data_length_(self, channel, data, length):
                try:
                    chunk = bytes(data[:length])
                except TypeError:
                    chunk = bytes(data.as_buffer(length))
                self.link._received(chunk)

            def rfcommChannelOpenComplete_status_(self, channel, status):
                self.link._open_complete(status)

            def rfcommChannelClosed_(self, channel):
                self.link._hub_closed()

        _delegate_class = LegoHubRFCOMMDelegate
    return _delegate_class


class MacBluetoothLink:
    """The hub's serial channel. Open it with `await MacBluetoothLink.open(address)` on the main thread."""

    def __init__(self, address: str):
        self.address = address
        self._buffer = bytearray()
        self._wake = threading.Condition()
        self._status = None
        self._closed = False
        self._channel = None
        self._mtu = 127

    @classmethod
    async def open(cls, address: str) -> MacBluetoothLink:
        """Open the channel (up to about 2 x ATTEMPT_TIMEOUT_S). Raises OSError if it doesn't open."""
        link = cls(address)
        link._run_loop_task = asyncio.ensure_future(link._turn_run_loop())  # kept: a task nobody holds can vanish
        try:
            await link._open_channel()
        except BaseException:
            link.close()
            raise
        return link

    # --- like pyserial -------------------------------------------------------

    @property
    def in_waiting(self) -> int:
        return len(self._buffer)

    def read(self, size: int = 1) -> bytes:
        """Called from LegacyHub's reader thread: waits up to 0.1 s for data, like pyserial's timeout."""
        with self._wake:
            if not self._buffer and not self._closed:
                self._wake.wait(0.1)
            if not self._buffer and self._closed:
                raise OSError("the Bluetooth channel closed")
            data = bytes(self._buffer[:size])
            del self._buffer[:size]
            return data

    def write(self, data: bytes) -> int:
        """Called on the main thread (LegacyHub sends from its asyncio loop)."""
        if self._closed or self._channel is None:
            raise OSError("the Bluetooth channel is closed")
        for i in range(0, len(data), self._mtu):
            piece = bytes(data[i:i + self._mtu])
            if self._channel.writeSync_length_(piece, len(piece)) != 0:
                self.close()
                raise OSError("writing to the hub over Bluetooth failed")
        return len(data)

    def close(self) -> None:
        with self._wake:
            already, self._closed = self._closed, True
            self._wake.notify_all()
        if not already and self._channel is not None:
            try:
                self._channel.closeChannel()
            except Exception:
                pass

    # --- called by the delegate, on the main thread --------------------------

    def _received(self, chunk: bytes) -> None:
        with self._wake:
            self._buffer.extend(chunk)
            self._wake.notify_all()

    def _open_complete(self, status: int) -> None:
        self._status = status

    def _hub_closed(self) -> None:
        self.close()

    # --- on the main thread ----------------------------------------------------

    async def _turn_run_loop(self) -> None:
        """IOBluetooth hands over data and events only while the main thread's run loop turns."""
        from Foundation import NSDate, NSRunLoop

        while not self._closed:
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.004))
            await asyncio.sleep(0.001)
        for _ in range(20):  # let the channel finish closing
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.005))
            await asyncio.sleep(0.005)

    async def _open_channel(self) -> None:
        from IOBluetooth import IOBluetoothDevice

        async def wait(seconds, until):
            end = time.monotonic() + seconds
            while time.monotonic() < end and not until():
                await asyncio.sleep(0.02)

        device = IOBluetoothDevice.deviceWithAddressString_(self.address)
        if device is None:
            raise OSError(f"no Bluetooth device {self.address}")
        delegate = _delegate().alloc().init()
        delegate.link = self
        self._delegate = delegate  # keep it alive while the channel is open
        if not device.isConnected():  # paired but not connected (for example the hub was off): connect first
            status = device.openConnection()
            await wait(10, device.isConnected)
            if not device.isConnected():
                raise OSError(f"the Mac couldn't connect to it (IOBluetooth status {status}). Switch it on and "
                              "press its Bluetooth button")
        for attempt in (1, 2):
            if attempt == 2:  # a fresh link: a half-open channel from an earlier try can block it
                device.closeConnection()
                await asyncio.sleep(1)
                device.openConnection()
                await wait(2, device.isConnected)
            self._status = None
            result = device.openRFCOMMChannelAsync_withChannelID_delegate_(None, SPP_CHANNEL, delegate)
            status, channel = result if isinstance(result, tuple) else (result, None)
            if status != 0 or channel is None:
                self._status = status
                continue
            await wait(ATTEMPT_TIMEOUT_S, lambda: self._status is not None or self._closed)
            if self._status == 0:
                self._channel = channel
                self._mtu = max(16, channel.getMTU() or 127)
                return
        raise OSError(f"its serial channel didn't open (IOBluetooth status {self._status})")

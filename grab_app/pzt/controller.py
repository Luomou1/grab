from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass

import serial
from serial.tools import list_ports

from grab_app.config import PZT_BAUD_RATES, PZT_CHANNELS, PZT_DEFAULT_IP, PZT_UDP_PORT
from grab_app.pzt.protocol import (
    build_closed_loop_packet,
    build_read_move_packet,
    build_send_move_packet,
    parse_read_move_response,
)


class PZTError(RuntimeError):
    pass


class _Transport:
    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def read_response(self, timeout_s: float) -> bytes:
        raise NotImplementedError

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _SerialTransport(_Transport):
    def __init__(self, port: str, baudrate: int) -> None:
        self._serial = serial.Serial(port=port, baudrate=baudrate, timeout=1.0)

    def write(self, data: bytes) -> None:
        self._serial.write(data)
        self._serial.flush()

    def flush(self) -> None:
        self._serial.reset_input_buffer()
        self._serial.reset_output_buffer()

    def read_response(self, timeout_s: float) -> bytes:
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            if self._serial.in_waiting >= 11:
                return self._serial.read(11)
            time.sleep(0.01)
        if self._serial.in_waiting:
            return self._serial.read(min(11, self._serial.in_waiting))
        return b""

    def close(self) -> None:
        if self._serial.is_open:
            self._serial.close()


class _UdpTransport(_Transport):
    def __init__(self, host: str, port: int = PZT_UDP_PORT) -> None:
        self._target = (host, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("", port))
        self._sock.settimeout(1.0)

    def write(self, data: bytes) -> None:
        self._sock.sendto(data, self._target)

    def flush(self) -> None:
        self._sock.setblocking(False)
        try:
            while True:
                self._sock.recvfrom(4096)
        except BlockingIOError:
            pass
        finally:
            self._sock.setblocking(True)
            self._sock.settimeout(1.0)

    def read_response(self, timeout_s: float) -> bytes:
        self._sock.settimeout(timeout_s)
        try:
            data, _ = self._sock.recvfrom(64)
            return data
        except socket.timeout:
            return b""

    def close(self) -> None:
        self._sock.close()


@dataclass
class PZTConnectionInfo:
    mode: str
    endpoint: str
    baudrate: int | None = None


class PZTController:
    """按旧 MATLAB GUI 的 PZT 字节协议实现，不扩展额外控制命令。"""

    def __init__(self) -> None:
        self._transport: _Transport | None = None
        self._lock = threading.RLock()
        self.info: PZTConnectionInfo | None = None

    @staticmethod
    def list_serial_ports() -> list[str]:
        return [item.device for item in list_ports.comports()]

    @staticmethod
    def baud_rates() -> tuple[int, ...]:
        return PZT_BAUD_RATES

    @property
    def connected(self) -> bool:
        return self._transport is not None

    def connect_serial(self, port: str, baudrate: int) -> None:
        if not port:
            raise PZTError("串口不能为空")
        self.close()
        self._transport = _SerialTransport(port, baudrate)
        self.info = PZTConnectionInfo("serial", port, baudrate)
        self._initialize_closed_loop()

    def connect_udp(self, host: str = PZT_DEFAULT_IP) -> None:
        self.close()
        self._transport = _UdpTransport(host)
        self.info = PZTConnectionInfo("udp", host, PZT_UDP_PORT)
        self._initialize_closed_loop()

    def _initialize_closed_loop(self) -> None:
        for channel in PZT_CHANNELS:
            self.set_closed_loop(channel)
            time.sleep(0.05)

    def set_closed_loop(self, channel: int) -> None:
        self._write(build_closed_loop_packet(channel))

    def send_move(self, channel: int, move_um: float) -> None:
        self._write(build_send_move_packet(channel, move_um))
        time.sleep(0.05)

    def read_move(self, channel: int, timeout_s: float = 0.08) -> float | None:
        with self._lock:
            transport = self._require_transport()
            try:
                transport.flush()
            except Exception:
                return None
            transport.write(build_read_move_packet(channel))
            data = transport.read_response(timeout_s)
            return parse_read_move_response(data)

    def _write(self, data: bytes) -> None:
        with self._lock:
            self._require_transport().write(data)

    def _require_transport(self) -> _Transport:
        if self._transport is None:
            raise PZTError("PZT 未连接")
        return self._transport

    def close(self) -> None:
        with self._lock:
            if self._transport is not None:
                try:
                    self._transport.close()
                finally:
                    self._transport = None
                    self.info = None


from __future__ import annotations

from dataclasses import dataclass
import re
import threading
from typing import Dict, Iterable, List

try:
    import snap7
    from snap7 import util
except ImportError as exc:  # pragma: no cover - depends on local environment
    snap7 = None
    util = None
    SNAP7_IMPORT_ERROR = exc
else:
    SNAP7_IMPORT_ERROR = None


SUPPORTED_DATA_TYPES = ("REAL", "INT", "DINT", "WORD", "DWORD", "BYTE", "BOOL", "S7STRING")


@dataclass(frozen=True)
class PLCVariable:
    name: str
    db_number: int
    start: int
    data_type: str = "REAL"
    bit_index: int = 0
    enabled: bool = True

    def normalized_type(self) -> str:
        return self.data_type.upper().strip()

    def validate(self) -> None:
        data_type = self.normalized_type()
        if data_type not in SUPPORTED_DATA_TYPES:
            raise ValueError(f"不支持的数据类型: {self.data_type}")
        if self.db_number < 1:
            raise ValueError("DB 块号必须大于 0。")
        if self.start < 0:
            raise ValueError("起始字节偏移不能小于 0。")
        if data_type == "BOOL" and not 0 <= self.bit_index <= 7:
            raise ValueError("BOOL 类型的位索引必须在 0 到 7 之间。")


class SiemensPLCClient:
    def __init__(self, ip_address: str, rack: int = 0, slot: int = 1) -> None:
        self.ip_address = ip_address.strip()
        self.rack = rack
        self.slot = slot
        self._client = None
        self._lock = threading.Lock()

    @property
    def is_connected(self) -> bool:
        with self._lock:
            if self._client is None:
                return False
            try:
                return bool(self._client.get_connected())
            except Exception:
                return False

    def connect(self) -> None:
        if snap7 is None:
            raise RuntimeError(
                "python-snap7 is not installed; cannot connect to the configured PLC."
            ) from SNAP7_IMPORT_ERROR
        if not self.ip_address:
            raise ValueError("PLC IP is required.")

        with self._lock:
            if self._client is None:
                self._client = snap7.Client()

            if self._client.get_connected():
                return

            self._client.connect(self.ip_address, self.rack, self.slot)
            if not self._client.get_connected():
                raise ConnectionError("PLC connection failed. Check IP, rack, slot, and network.")

    def disconnect(self) -> None:
        with self._lock:
            if self._client is None:
                return
            try:
                if self._client.get_connected():
                    self._client.disconnect()
            finally:
                self._client.destroy()
                self._client = None

    def read_value(self, variable: PLCVariable) -> float:
        variable.validate()
        data_type = variable.normalized_type()

        with self._lock:
            if self._client is None or not self._client.get_connected():
                raise ConnectionError("PLC is not connected.")

            if data_type == "S7STRING":
                size = self._read_s7string_item_size_locked(variable)
            else:
                size = self._required_bytes(data_type)
            raw = self._client.db_read(variable.db_number, variable.start, size)

        value = self._decode(raw, variable)
        return float(value)

    def read_text(self, variable: PLCVariable) -> str:
        variable.validate()
        data_type = variable.normalized_type()

        if data_type == "S7STRING":
            with self._lock:
                if self._client is None or not self._client.get_connected():
                    raise ConnectionError("PLC is not connected.")
                size = self._read_s7string_item_size_locked(variable)
                raw = self._client.db_read(variable.db_number, variable.start, size)
            return self._decode_s7string_text(raw, 0)

        numeric_value = self.read_value(variable)
        if data_type in {"INT", "DINT", "WORD", "DWORD", "BYTE", "BOOL"}:
            return str(int(round(numeric_value)))
        if numeric_value.is_integer():
            return str(int(numeric_value))
        return f"{numeric_value:.6f}".rstrip("0").rstrip(".")

    def read_values(self, variables: Iterable[PLCVariable]) -> Dict[str, float]:
        results: Dict[str, float] = {}
        for variable in variables:
            if variable.enabled:
                results[variable.name] = self.read_value(variable)
        return results

    def write_bool(self, variable: PLCVariable, value: bool) -> None:
        variable.validate()
        if variable.normalized_type() != "BOOL":
            raise ValueError("只有 BOOL 类型支持写入置零。")

        with self._lock:
            if self._client is None or not self._client.get_connected():
                raise ConnectionError("PLC is not connected.")

            raw = bytearray(self._client.db_read(variable.db_number, variable.start, 1))
            util.set_bool(raw, 0, variable.bit_index, bool(value))
            self._client.db_write(variable.db_number, variable.start, raw)

    def write_value(self, variable: PLCVariable, value) -> None:
        variable.validate()
        data_type = variable.normalized_type()

        with self._lock:
            if self._client is None or not self._client.get_connected():
                raise ConnectionError("PLC is not connected.")

            if data_type == "BOOL":
                raw = bytearray(self._client.db_read(variable.db_number, variable.start, 1))
                util.set_bool(raw, 0, variable.bit_index, bool(value))
            elif data_type == "REAL":
                raw = bytearray(4)
                util.set_real(raw, 0, float(value))
            elif data_type == "INT":
                raw = bytearray(2)
                util.set_int(raw, 0, int(value))
            elif data_type == "DINT":
                raw = bytearray(4)
                util.set_dint(raw, 0, int(value))
            elif data_type == "WORD":
                raw = bytearray(2)
                util.set_word(raw, 0, int(value))
            elif data_type == "DWORD":
                raw = bytearray(4)
                util.set_dword(raw, 0, int(value))
            elif data_type == "BYTE":
                raw = bytearray([int(value) & 0xFF])
            elif data_type == "S7STRING":
                header = bytearray(self._client.db_read(variable.db_number, variable.start, 2))
                if len(header) < 2:
                    raise ValueError("S7STRING 头部长度不足，无法写入。")
                max_length = max(0, int(header[0]))
                raw = bytearray(max(2, 2 + max_length))
                raw[0] = max_length
                text = str(value)
                payload = text.encode("latin-1", errors="ignore")[:max_length]
                raw[1] = len(payload)
                raw[2 : 2 + len(payload)] = payload
            else:
                raise ValueError(f"不支持的数据类型: {variable.data_type}")

            self._client.db_write(variable.db_number, variable.start, raw)

    def read_series(self, variable: PLCVariable, count: int) -> List[float]:
        variable.validate()
        data_type = variable.normalized_type()

        if data_type == "S7STRING":
            with self._lock:
                if self._client is None or not self._client.get_connected():
                    raise ConnectionError("PLC is not connected.")

                item_size = self._read_s7string_item_size_locked(variable)
                raw = self._client.db_read(variable.db_number, variable.start, item_size)
            text = self._decode_s7string_text(raw, 0)
            return self._parse_s7string_series_text(text)

        if count <= 0:
            return []

        with self._lock:
            if self._client is None or not self._client.get_connected():
                raise ConnectionError("PLC is not connected.")

            item_size = self._required_bytes(data_type)
            raw = self._client.db_read(variable.db_number, variable.start, item_size * count)

        values: List[float] = []
        for index in range(count):
            offset = index * item_size
            values.append(float(self._decode_at_offset(raw, variable, offset)))
        return values

    @staticmethod
    def _required_bytes(data_type: str) -> int:
        byte_map = {
            "REAL": 4,
            "INT": 2,
            "DINT": 4,
            "WORD": 2,
            "DWORD": 4,
            "BYTE": 1,
            "BOOL": 1,
            "S7STRING": 2,
        }
        return byte_map[data_type]

    def _read_s7string_item_size_locked(self, variable: PLCVariable) -> int:
        header = self._client.db_read(variable.db_number, variable.start, 2)
        if len(header) < 2:
            raise ValueError("S7STRING 头部长度不足，无法解析。")
        max_length = max(0, int(header[0]))
        return max(2, 2 + max_length)

    @staticmethod
    def _decode_s7string_text(raw: bytearray, offset: int) -> str:
        if offset + 2 > len(raw):
            raise ValueError("S7STRING 数据长度不足，无法解析。")
        max_length = max(0, int(raw[offset]))
        current_length = max(0, int(raw[offset + 1]))
        available_length = max(0, len(raw) - offset - 2)
        text_length = min(current_length, max_length, available_length)
        payload = bytes(raw[offset + 2 : offset + 2 + text_length])
        return payload.decode("latin-1", errors="ignore").strip().strip("\x00")

    @staticmethod
    def _parse_s7string_series_text(text: str) -> List[float]:
        if not text:
            return []
        normalized = text.replace("，", ",").replace("；", ",").replace(";", ",")
        normalized = normalized.replace("\r", ",").replace("\n", ",")
        tokens = [token.strip() for token in re.split(r"[,\s]+", normalized) if token.strip()]
        values: List[float] = []
        for token in tokens:
            try:
                values.append(float(token))
            except ValueError as exc:
                raise ValueError(f"S7STRING 内容 '{token}' 不是可绘制的数值。") from exc
        return values

    @staticmethod
    def _decode(raw: bytearray, variable: PLCVariable) -> float:
        return SiemensPLCClient._decode_at_offset(raw, variable, 0)

    @staticmethod
    def _decode_at_offset(raw: bytearray, variable: PLCVariable, offset: int) -> float:
        data_type = variable.normalized_type()

        if data_type == "REAL":
            return util.get_real(raw, offset)
        if data_type == "INT":
            return util.get_int(raw, offset)
        if data_type == "DINT":
            return util.get_dint(raw, offset)
        if data_type == "WORD":
            return util.get_word(raw, offset)
        if data_type == "DWORD":
            return util.get_dword(raw, offset)
        if data_type == "BYTE":
            return raw[offset]
        if data_type == "BOOL":
            return 1.0 if util.get_bool(raw, offset, variable.bit_index) else 0.0
        if data_type == "S7STRING":
            text = SiemensPLCClient._decode_s7string_text(raw, offset)
            if not text:
                return 0.0
            try:
                return float(text)
            except ValueError as exc:
                raise ValueError(f"S7STRING 内容 '{text}' 不是可绘制的数值。") from exc

        raise ValueError(f"不支持的数据类型: {variable.data_type}")

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import ctypes
import json
import os
import re

os.environ.setdefault("QT_API", "pyqt5")

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
import math
from pathlib import Path
import queue
import sys
import threading
import time
from typing import Deque, Dict, Iterable, List, Optional, Tuple

from ctypes import wintypes

from matplotlib import rcParams
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
from matplotlib.collections import LineCollection
from matplotlib.figure import Figure
from matplotlib.ticker import AutoLocator, FuncFormatter, MultipleLocator
from qtpy import QtCore, QtGui, QtWidgets

from plc_comm import PLCVariable, SUPPORTED_DATA_TYPES, SiemensPLCClient


WM_ENTERSIZEMOVE = 0x0231
WM_EXITSIZEMOVE = 0x0232
rcParams["axes.unicode_minus"] = False

DEMO_BATCH_POINT_COUNT = 600
FIXED_POLL_INTERVAL_MS = 200
DISPLAY_VALUE_DECIMALS = 3
SIGNAL_GROUP_CHANNELS: Dict[int, Tuple[int, ...]] = {
    1: (0, 1, 2, 3),
    2: (4, 5, 6, 7),
}


def runtime_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def normalize_display_precision(value: float, decimals: int = DISPLAY_VALUE_DECIMALS) -> float:
    return float(f"{float(value):.{max(0, int(decimals))}f}")


def prepare_plot_payloads(
    snapshots: Dict[int, Dict[str, object]],
    display_point_limit: int,
) -> Dict[int, Dict[str, object]]:
    prepared: Dict[int, Dict[str, object]] = {}

    for index, snapshot in snapshots.items():
        now = time.time()
        times = list(snapshot["times"])
        values = list(snapshot["values"])
        visible = bool(snapshot["visible"])
        lower_limit = float(snapshot.get("lower_limit", -50.0))
        upper_limit = float(snapshot.get("upper_limit", 50.0))
        upper_limit_color = str(snapshot.get("upper_limit_color", snapshot.get("limit_color", "#ff6b6b")))
        lower_limit_color = str(snapshot.get("lower_limit_color", snapshot.get("limit_color", "#ff6b6b")))
        y_tick_step = max(0.1, float(snapshot.get("y_tick_step", 10.0)))
        center_line_mode = str(snapshot.get("center_line_mode", "zero")).strip().lower()
        center_line_y = 0.0
        center_line_visible = True
        baseline_plc_value = snapshot.get("baseline_plc_value")
        if center_line_mode == "first_point":
            if values:
                center_line_y = float(values[0])
            else:
                center_line_visible = False
        elif center_line_mode == "plc_point":
            if baseline_plc_value is None:
                center_line_visible = False
            else:
                center_line_y = float(baseline_plc_value)
        elif center_line_mode == "custom_value":
            center_line_y = max(0.1, min(10.0, float(snapshot.get("center_line_custom_value", 1.0))))
        else:
            center_line_mode = "zero"
            center_line_y = 0.0
        if lower_limit >= upper_limit:
            lower_limit = upper_limit - 1.0

        payload: Dict[str, object] = {
            "name": str(snapshot["name"]),
            "address": str(snapshot["address"]),
            "latest_text": f"{float(snapshot['latest_value']):.3f}",
            "target_label": str(snapshot.get("target_label", "指定值")),
            "target_value_text": str(snapshot.get("target_value_text", "--")),
            "visible": visible,
            "color": str(snapshot["color"]),
            "highlight_limit_exceeded": bool(snapshot.get("highlight_limit_exceeded", True)),
            "y_tick_step": y_tick_step,
            "center_line_y": center_line_y,
            "center_line_visible": center_line_visible and visible,
            "x": [],
            "y": [],
            "xlim": (now - 10.0, now),
            "ylim": (lower_limit - 1.0, upper_limit + 1.0),
            "lower_limit": lower_limit,
            "upper_limit": upper_limit,
            "upper_limit_color": upper_limit_color,
            "lower_limit_color": lower_limit_color,
            "state": "Waiting",
        }

        if not visible:
            payload["state"] = "Hidden"
            prepared[index] = payload
            continue

        if not times:
            min_y = min(-1.0, lower_limit)
            max_y = max(1.0, upper_limit)
            if center_line_visible:
                min_y = min(min_y, center_line_y)
                max_y = max(max_y, center_line_y)
            if max_y <= min_y:
                max_y = min_y + 1.0
            pad = max(1.0, (max_y - min_y) * 0.12)
            payload["ylim"] = (min_y - pad, max_y + pad)
            prepared[index] = payload
            continue

        if len(times) > display_point_limit:
            step = max(1, len(times) // display_point_limit)
            times = times[::step]
            values = values[::step]
            if times[-1] != snapshot["times"][-1]:
                times.append(snapshot["times"][-1])
                values.append(snapshot["values"][-1])

        min_x = float(times[0])
        max_x = float(times[-1])
        if max_x <= min_x:
            max_x = min_x + 1.0

        min_y = min(float(min(values)), lower_limit)
        max_y = max(float(max(values)), upper_limit)
        if center_line_visible:
            min_y = min(min_y, center_line_y)
            max_y = max(max_y, center_line_y)
        if max_y <= min_y:
            pad = max(1.0, abs(max_y) * 0.2 + 1.0)
            min_y -= pad
            max_y += pad
        else:
            pad = (max_y - min_y) * 0.15
            min_y -= pad
            max_y += pad

        payload["x"] = times
        payload["y"] = values
        payload["xlim"] = (min_x, max_x)
        payload["ylim"] = (min_y, max_y)
        payload["state"] = ""
        prepared[index] = payload

    return prepared


@dataclass
class WaveformChannel:
    name: str
    color: str
    target_label: str = "指定值"
    db_number: int = 1
    start: int = 0
    data_type: str = "REAL"
    bit_index: int = 0
    visible: bool = True
    upper_limit: float = 50.0
    lower_limit: float = -50.0
    upper_limit_color: str = "#ff6b6b"
    lower_limit_color: str = "#ff6b6b"
    upper_limit_db_number: int = 1
    upper_limit_start: int = 0
    upper_limit_data_type: str = "REAL"
    upper_limit_bit_index: int = 0
    upper_limit_read_enabled: bool = False
    lower_limit_db_number: int = 1
    lower_limit_start: int = 0
    lower_limit_data_type: str = "REAL"
    lower_limit_bit_index: int = 0
    lower_limit_read_enabled: bool = False
    baseline_db_number: int = 1
    baseline_start: int = 0
    baseline_data_type: str = "REAL"
    baseline_bit_index: int = 0
    baseline_read_enabled: bool = False
    baseline_value: Optional[float] = None
    target_db_number: int = 1
    target_start: int = 0
    target_data_type: str = "REAL"
    target_bit_index: int = 0
    target_enabled: bool = False
    target_value_text: str = "--"
    latest_value: float = 0.0
    times: Deque[float] = field(default_factory=deque)
    values: Deque[float] = field(default_factory=deque)

    def to_variable(self) -> PLCVariable:
        return PLCVariable(
            name=self.name,
            db_number=self.db_number,
            start=self.start,
            data_type=self.data_type,
            bit_index=self.bit_index,
            enabled=self.visible,
        )

    def address_text(self) -> str:
        data_type = self.data_type.upper()
        if data_type == "BOOL":
            suffix = f".{self.bit_index}"
        elif data_type == "S7STRING":
            suffix = " (S7STRING)"
        else:
            suffix = ""
        return f"DB{self.db_number}.DBB{self.start}{suffix}"

    def target_variable(self) -> PLCVariable:
        return PLCVariable(
            name=f"{self.name}_target",
            db_number=self.target_db_number,
            start=self.target_start,
            data_type=self.target_data_type,
            bit_index=self.target_bit_index,
            enabled=self.target_enabled,
        )

    def upper_limit_variable(self) -> PLCVariable:
        return PLCVariable(
            name=f"{self.name}_upper_limit",
            db_number=self.upper_limit_db_number,
            start=self.upper_limit_start,
            data_type=self.upper_limit_data_type,
            bit_index=self.upper_limit_bit_index,
            enabled=self.upper_limit_read_enabled,
        )

    def lower_limit_variable(self) -> PLCVariable:
        return PLCVariable(
            name=f"{self.name}_lower_limit",
            db_number=self.lower_limit_db_number,
            start=self.lower_limit_start,
            data_type=self.lower_limit_data_type,
            bit_index=self.lower_limit_bit_index,
            enabled=self.lower_limit_read_enabled,
        )

    def baseline_variable(self) -> PLCVariable:
        return PLCVariable(
            name=f"{self.name}_baseline",
            db_number=self.baseline_db_number,
            start=self.baseline_start,
            data_type=self.baseline_data_type,
            bit_index=self.baseline_bit_index,
            enabled=self.baseline_read_enabled,
        )


@dataclass
class PLCSignalConfig:
    db_number: int = 1
    start: int = 200
    bit_index: int = 0

    def to_variable(self, name: str) -> PLCVariable:
        return PLCVariable(
            name=name,
            db_number=self.db_number,
            start=self.start,
            data_type="BOOL",
            bit_index=self.bit_index,
            enabled=True,
        )

    def address_text(self) -> str:
        return f"DB{self.db_number}.DBB{self.start}.{self.bit_index}"


@dataclass
class SNReadConfig:
    name: str = "编号"
    db_number: int = 1
    start: int = 0
    data_type: str = "S7STRING"
    bit_index: int = 0
    enabled: bool = False

    def to_variable(self) -> PLCVariable:
        return PLCVariable(
            name=self.name,
            db_number=self.db_number,
            start=self.start,
            data_type=self.data_type,
            bit_index=self.bit_index,
            enabled=self.enabled,
        )

    def address_text(self) -> str:
        data_type = self.data_type.upper()
        if data_type == "BOOL":
            suffix = f".{self.bit_index}"
        elif data_type == "S7STRING":
            suffix = " (S7STRING)"
        else:
            suffix = ""
        return f"DB{self.db_number}.DBB{self.start}{suffix}"


@dataclass
class HeartbeatConfig:
    name: str = "心跳"
    db_number: int = 1
    start: int = 202
    data_type: str = "BOOL"
    bit_index: int = 0
    enabled: bool = False
    high_value: str = "1"
    low_value: str = "0"
    high_interval_s: float = 1.0
    low_interval_s: float = 1.0

    def to_variable(self) -> PLCVariable:
        return PLCVariable(
            name=self.name,
            db_number=self.db_number,
            start=self.start,
            data_type=self.data_type,
            bit_index=self.bit_index,
            enabled=self.enabled,
        )

    def address_text(self) -> str:
        data_type = self.data_type.upper()
        if data_type == "BOOL":
            suffix = f".{self.bit_index}"
        elif data_type == "S7STRING":
            suffix = " (S7STRING)"
        else:
            suffix = ""
        return f"DB{self.db_number}.DBB{self.start}{suffix}"


@dataclass
class SaveSettingsConfig:
    root_dir: str = "reports"
    date_folder_format: str = "%Y%m%d"
    use_time_subfolder: bool = True
    time_folder_format: str = "%H%M%S"
    filename_pattern: str = "{item_id}"


@dataclass
class AutoSaveRequest:
    request_id: int
    sn: str
    channel_indices: Tuple[int, ...]
    group_id: Optional[int] = None
    save_timestamp: Optional[float] = None


class CollapsibleSection(QtWidgets.QFrame):
    def __init__(self, title: str, expanded: bool = False, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("CollapsibleSection")

        outer_layout = QtWidgets.QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        self.toggle_button = QtWidgets.QToolButton(self)
        self.toggle_button.setObjectName("SectionToggle")
        self.toggle_button.setText(title)
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(expanded)
        self.toggle_button.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.toggle_button.setArrowType(QtCore.Qt.DownArrow if expanded else QtCore.Qt.RightArrow)
        self.toggle_button.toggled.connect(self.set_expanded)
        outer_layout.addWidget(self.toggle_button)

        self.content_widget = QtWidgets.QWidget(self)
        self.content_widget.setObjectName("CollapsibleBody")
        self.content_widget.setVisible(expanded)
        self.content_layout = QtWidgets.QVBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(12, 10, 12, 12)
        self.content_layout.setSpacing(10)
        outer_layout.addWidget(self.content_widget)

    def set_content_widget(self, widget: QtWidgets.QWidget) -> None:
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            child = item.widget()
            if child is not None:
                child.setParent(None)
        self.content_layout.addWidget(widget)

    def set_expanded(self, expanded: bool) -> None:
        self.toggle_button.blockSignals(True)
        self.toggle_button.setChecked(expanded)
        self.toggle_button.blockSignals(False)
        self.toggle_button.setArrowType(QtCore.Qt.DownArrow if expanded else QtCore.Qt.RightArrow)
        self.content_widget.setVisible(expanded)


class PlotCard(QtWidgets.QFrame):
    DISPLAY_POINT_LIMIT = 140
    limitsChanged = QtCore.Signal(int, float, float)
    upperLimitColorChanged = QtCore.Signal(int, str)
    lowerLimitColorChanged = QtCore.Signal(int, str)

    def __init__(self, channel_index: int, colors: Dict[str, str], ui_scale: float, parent=None) -> None:
        super().__init__(parent)
        self.channel_index = channel_index
        self.colors = colors
        self.ui_scale = ui_scale

        self.setObjectName("PlotCard")
        self.setProperty("selected", False)
        self.setMinimumHeight(max(205, int(210 * ui_scale)))

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        header_layout = QtWidgets.QHBoxLayout()
        header_layout.setSpacing(8)
        layout.addLayout(header_layout)

        text_layout = QtWidgets.QVBoxLayout()
        text_layout.setSpacing(2)
        header_layout.addLayout(text_layout, 1)

        self.name_label = QtWidgets.QLabel(f"波形 {channel_index + 1}")
        self.name_label.setObjectName("PlotTitle")
        text_layout.addWidget(self.name_label)

        self.address_label = QtWidgets.QLabel("DB1.DBB0")
        self.address_label.setObjectName("PlotAddress")
        text_layout.addWidget(self.address_label)

        self.latest_badge = QtWidgets.QLabel("--")
        self.latest_badge.setAlignment(QtCore.Qt.AlignCenter)
        self.latest_badge.setObjectName("ValueBadge")
        self.latest_badge.setMinimumWidth(max(90, int(96 * ui_scale)))
        self.latest_badge.hide()

        self.limit_controls_updating = False
        control_panel = QtWidgets.QVBoxLayout()
        control_panel.setSpacing(6)
        header_layout.addLayout(control_panel)

        limit_row = QtWidgets.QHBoxLayout()
        limit_row.setSpacing(4)
        control_panel.addLayout(limit_row)

        self.target_value_title = QtWidgets.QLabel("指定值")
        self.target_value_title.setObjectName("InlineLabel")
        limit_row.addWidget(self.target_value_title)

        self.target_value_badge = QtWidgets.QLabel("--")
        self.target_value_badge.setObjectName("InlineValueBadge")
        self.target_value_badge.setAlignment(QtCore.Qt.AlignCenter)
        self.target_value_badge.setMinimumWidth(max(74, int(82 * ui_scale)))
        self.target_value_badge.setToolTip("指定值: --")
        limit_row.addWidget(self.target_value_badge)

        upper_label = QtWidgets.QLabel("上限")
        upper_label.setObjectName("InlineLabel")
        limit_row.addWidget(upper_label)

        self.upper_limit_spin = QtWidgets.QDoubleSpinBox()
        self.upper_limit_spin.setObjectName("InlineLimitSpin")
        self.upper_limit_spin.setRange(-1000000.0, 1000000.0)
        self.upper_limit_spin.setDecimals(3)
        self.upper_limit_spin.setSingleStep(1.0)
        self.upper_limit_spin.setMinimumWidth(max(72, int(78 * ui_scale)))
        self.upper_limit_spin.valueChanged.connect(self._emit_limit_change)
        limit_row.addWidget(self.upper_limit_spin)

        self.upper_limit_color_button = QtWidgets.QPushButton("颜色")
        self.upper_limit_color_button.setObjectName("LimitColorButton")
        self.upper_limit_color_button.setMinimumWidth(max(48, int(54 * ui_scale)))
        self.upper_limit_color_button.clicked.connect(self._choose_upper_limit_color)
        limit_row.addWidget(self.upper_limit_color_button)

        lower_label = QtWidgets.QLabel("下限")
        lower_label.setObjectName("InlineLabel")
        limit_row.addWidget(lower_label)

        self.lower_limit_spin = QtWidgets.QDoubleSpinBox()
        self.lower_limit_spin.setObjectName("InlineLimitSpin")
        self.lower_limit_spin.setRange(-1000000.0, 1000000.0)
        self.lower_limit_spin.setDecimals(3)
        self.lower_limit_spin.setSingleStep(1.0)
        self.lower_limit_spin.setMinimumWidth(max(72, int(78 * ui_scale)))
        self.lower_limit_spin.valueChanged.connect(self._emit_limit_change)
        limit_row.addWidget(self.lower_limit_spin)

        self.lower_limit_color_button = QtWidgets.QPushButton("颜色")
        self.lower_limit_color_button.setObjectName("LimitColorButton")
        self.lower_limit_color_button.setMinimumWidth(max(48, int(54 * ui_scale)))
        self.lower_limit_color_button.clicked.connect(self._choose_lower_limit_color)
        limit_row.addWidget(self.lower_limit_color_button)


        self.figure = Figure(figsize=(4.2, 2.8), dpi=100, constrained_layout=False)
        self.axis = self.figure.add_subplot(111)
        self.figure.subplots_adjust(left=0.12, right=0.98, top=0.96, bottom=0.18)
        self.canvas = FigureCanvasQTAgg(self.figure)
        plot_shell = QtWidgets.QHBoxLayout()
        plot_shell.setSpacing(6)
        layout.addLayout(plot_shell, 1)

        self.y_axis_label = QtWidgets.QLabel("力\n值")
        self.y_axis_label.setObjectName("AxisLabelVertical")
        self.y_axis_label.setAlignment(QtCore.Qt.AlignCenter)
        self.y_axis_label.setMinimumWidth(max(22, int(24 * ui_scale)))
        plot_shell.addWidget(self.y_axis_label)

        plot_body = QtWidgets.QVBoxLayout()
        plot_body.setSpacing(4)
        plot_shell.addLayout(plot_body, 1)
        plot_body.addWidget(self.canvas, 1)

        self.x_axis_label = QtWidgets.QLabel("时间")
        self.x_axis_label.setObjectName("AxisLabelHorizontal")
        self.x_axis_label.setAlignment(QtCore.Qt.AlignCenter)
        plot_body.addWidget(self.x_axis_label)

        self._plot_initialized = False
        self._last_header: Optional[Tuple[str, ...]] = None
        self._last_hover_index: Optional[int] = None
        self.current_x_values: List[float] = []
        self.current_y_values: List[float] = []
        self.current_lower_limit = -50.0
        self.current_upper_limit = 50.0
        self.current_upper_limit_color = self.colors["danger"]
        self.current_lower_limit_color = self.colors["danger"]
        self.current_y_tick_step = 10.0
        self.current_center_line_y = 0.0
        self.current_center_line_visible = True
        self._set_color_button_style(self.upper_limit_color_button, self.current_upper_limit_color)
        self._set_color_button_style(self.lower_limit_color_button, self.current_lower_limit_color)
        self._setup_plot()
        self.canvas.mpl_connect("motion_notify_event", self._on_mouse_move)
        self.canvas.mpl_connect("figure_leave_event", self._on_mouse_leave)

    @staticmethod
    def _format_system_time(timestamp: float, with_ms: bool = False) -> str:
        timestamp = max(0.0, float(timestamp))
        base_seconds = int(timestamp)
        time_text = time.strftime("%H:%M:%S", time.localtime(base_seconds))
        if with_ms:
            milliseconds = int(round((timestamp - base_seconds) * 1000))
            if milliseconds >= 1000:
                milliseconds = 0
                base_seconds += 1
                time_text = time.strftime("%H:%M:%S", time.localtime(base_seconds))
            return f"{time_text}.{milliseconds:03d}"
        return time_text

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def _setup_plot(self) -> None:
        self.figure.patch.set_facecolor(self.colors["panel"])
        self.axis.clear()
        self.axis.set_facecolor(self.colors["plot"])
        self.axis.grid(True, color="#24313d", linestyle="--", linewidth=0.8, alpha=0.90)
        self.axis.set_title("", color=self.colors["text"], pad=8, fontsize=max(8, int(10 * self.ui_scale)))
        self.axis.set_xlabel("")
        self.axis.set_ylabel("")
        self.axis.tick_params(colors=self.colors["muted"], labelsize=max(6, int(7 * self.ui_scale)))
        self.axis.xaxis.set_major_formatter(FuncFormatter(lambda value, _pos: self._format_system_time(value)))
        self.axis.yaxis.set_major_formatter(FuncFormatter(self._format_y_tick))
        self.axis.yaxis.set_major_locator(AutoLocator())
        for spine in self.axis.spines.values():
            spine.set_color(self.colors["border"])
            spine.set_linewidth(1.1)

        now = time.time()
        self.axis.set_xlim(now - 10.0, now)
        self.axis.set_ylim(-1.0, 1.0)
        (self.base_line,) = self.axis.plot(
            [],
            [],
            color=self.colors["accent"],
            linewidth=1.9,
            antialiased=True,
            solid_capstyle="round",
            solid_joinstyle="round",
            zorder=3.0,
        )
        self.line_collection = LineCollection([], linewidths=2.0)
        self.line_collection.set_color(self.colors["danger"])
        self.line_collection.set_capstyle("round")
        self.line_collection.set_joinstyle("round")
        self.line_collection.set_antialiaseds([True])
        self.line_collection.set_zorder(3.2)
        self.axis.add_collection(self.line_collection)
        self.upper_limit_line = self.axis.axhline(
            self.current_upper_limit,
            color=self.current_upper_limit_color,
            linewidth=0.75,
            alpha=0.95,
            zorder=2,
        )
        self.lower_limit_line = self.axis.axhline(
            self.current_lower_limit,
            color=self.current_lower_limit_color,
            linewidth=0.75,
            alpha=0.95,
            zorder=2,
        )
        self.center_line = self.axis.axhline(
            self.current_center_line_y,
            color=self.colors["accent"],
            linewidth=1.6,
            linestyle="--",
            alpha=0.96,
            zorder=2.4,
        )
        self.center_line.set_visible(self.current_center_line_visible)
        self.hover_vline = self.axis.axvline(0.0, color=self.colors["accent_soft"], linewidth=0.8, alpha=0.65)
        self.hover_vline.set_visible(False)
        self.hover_hline = self.axis.axhline(0.0, color=self.colors["accent_soft"], linewidth=0.8, alpha=0.65)
        self.hover_hline.set_visible(False)
        (self.hover_marker,) = self.axis.plot([], [], marker="o", markersize=4, color=self.colors["accent_soft"], linestyle="None")
        self.hover_marker.set_visible(False)
        self.state_text = self.axis.text(
            0.5,
            0.5,
            "Waiting",
            transform=self.axis.transAxes,
            ha="center",
            va="center",
            color=self.colors["muted"],
            fontsize=max(9, int(10 * self.ui_scale)),
        )
        self._plot_initialized = True
        self.canvas.draw_idle()

    def _style_axis(self, title: str) -> None:
        if not self._plot_initialized:
            self._setup_plot()
        self.axis.set_title("", color=self.colors["text"], pad=8, fontsize=max(8, int(10 * self.ui_scale)))

    def _format_y_tick(self, value: float, _pos: float) -> str:
        step = max(0.1, float(self.current_y_tick_step))
        step_text = f"{step:.6f}".rstrip("0").rstrip(".")
        decimals = 0
        if "." in step_text:
            decimals = min(6, len(step_text.split(".", 1)[1]))
        if decimals <= 0:
            return f"{value:.0f}"
        return f"{value:.{decimals}f}"

    def _apply_axis_scale_settings(
        self,
        y_tick_step: float,
        center_line_y: float,
        center_line_visible: bool,
    ) -> None:
        step = max(0.1, float(y_tick_step))
        self.current_y_tick_step = step
        self.current_center_line_y = float(center_line_y)
        self.current_center_line_visible = bool(center_line_visible)
        self.axis.yaxis.set_major_locator(MultipleLocator(step))
        self.center_line.set_color(self.colors["accent"])
        self.center_line.set_linewidth(1.6)
        self.center_line.set_linestyle("--")
        self.center_line.set_alpha(0.96)
        self.center_line.set_zorder(2.4)
        self.center_line.set_ydata([self.current_center_line_y, self.current_center_line_y])
        self.center_line.set_visible(self.current_center_line_visible)

    def _set_color_button_style(self, button: QtWidgets.QPushButton, color: str) -> None:
        button.setStyleSheet(
            f"background:{color}; color:{self.colors['bg']}; border:1px solid {self.colors['border']}; border-radius:8px; padding:4px 8px; font-weight:700;"
        )

    def sync_limit_controls(
        self,
        lower_limit: float,
        upper_limit: float,
        upper_limit_color: str,
        lower_limit_color: str,
    ) -> None:
        self.limit_controls_updating = True
        self.lower_limit_spin.setValue(float(lower_limit))
        self.upper_limit_spin.setValue(float(upper_limit))
        self.current_upper_limit_color = str(upper_limit_color)
        self.current_lower_limit_color = str(lower_limit_color)
        self._set_color_button_style(self.upper_limit_color_button, self.current_upper_limit_color)
        self._set_color_button_style(self.lower_limit_color_button, self.current_lower_limit_color)
        self.limit_controls_updating = False

    def _emit_limit_change(self) -> None:
        if self.limit_controls_updating:
            return
        lower_limit = float(self.lower_limit_spin.value())
        upper_limit = float(self.upper_limit_spin.value())
        if lower_limit >= upper_limit:
            return
        self.limitsChanged.emit(self.channel_index, lower_limit, upper_limit)

    def _choose_upper_limit_color(self) -> None:
        color = QtWidgets.QColorDialog.getColor(QtGui.QColor(self.current_upper_limit_color), self, "选择上限颜色")
        if not color.isValid():
            return
        color_name = color.name()
        self.current_upper_limit_color = color_name
        self._set_color_button_style(self.upper_limit_color_button, color_name)
        self.upperLimitColorChanged.emit(self.channel_index, color_name)

    def _choose_lower_limit_color(self) -> None:
        color = QtWidgets.QColorDialog.getColor(QtGui.QColor(self.current_lower_limit_color), self, "选择下限颜色")
        if not color.isValid():
            return
        color_name = color.name()
        self.current_lower_limit_color = color_name
        self._set_color_button_style(self.lower_limit_color_button, color_name)
        self.lowerLimitColorChanged.emit(self.channel_index, color_name)

    def _set_limit_lines(
        self,
        lower_limit: float,
        upper_limit: float,
        upper_limit_color: str,
        lower_limit_color: str,
        visible: bool,
    ) -> None:
        self.current_lower_limit = float(lower_limit)
        self.current_upper_limit = float(upper_limit)
        self.current_upper_limit_color = str(upper_limit_color)
        self.current_lower_limit_color = str(lower_limit_color)
        self.upper_limit_line.set_color(self.current_upper_limit_color)
        self.lower_limit_line.set_color(self.current_lower_limit_color)
        self.lower_limit_line.set_ydata([self.current_lower_limit, self.current_lower_limit])
        self.upper_limit_line.set_ydata([self.current_upper_limit, self.current_upper_limit])
        self.lower_limit_line.set_visible(visible)
        self.upper_limit_line.set_visible(visible)

    def _build_alert_segments(
        self,
        x_values: List[float],
        y_values: List[float],
        lower_limit: float,
        upper_limit: float,
        highlight_limit_exceeded: bool,
    ) -> List[List[Tuple[float, float]]]:
        segments: List[List[Tuple[float, float]]] = []
        if len(x_values) < 2 or not highlight_limit_exceeded:
            return segments

        for left_x, left_y, right_x, right_y in zip(x_values[:-1], y_values[:-1], x_values[1:], y_values[1:]):
            exceeded = (
                float(left_y) > upper_limit
                or float(left_y) < lower_limit
                or float(right_y) > upper_limit
                or float(right_y) < lower_limit
            )
            if exceeded:
                segments.append([(float(left_x), float(left_y)), (float(right_x), float(right_y))])
        return segments

    def _draw_empty(self, title: str, message: str) -> None:
        self._style_axis(title)
        self.current_x_values = []
        self.current_y_values = []
        self._last_hover_index = None
        self.base_line.set_data([], [])
        self.base_line.set_visible(False)
        self.line_collection.set_segments([])
        self._set_limit_lines(
            self.current_lower_limit,
            self.current_upper_limit,
            self.current_upper_limit_color,
            self.current_lower_limit_color,
            False,
        )
        self.state_text.set_text(message)
        self.state_text.set_visible(True)
        self._hide_hover()
        now = time.time()
        self.axis.set_xlim(now - 10.0, now)
        self.axis.set_ylim(-1.0, 1.0)
        self.canvas.draw_idle()

    def update_snapshot(self, snapshot: Optional[Dict[str, object]]) -> None:
        if snapshot is None:
            header = (f"波形 {self.channel_index + 1}", "未配置", "--", "指定值")
            if self._last_header != header:
                self.name_label.setText(header[0])
                self.address_label.setText(header[1])
                self.latest_badge.setText(header[2])
                self.target_value_title.setText(header[3])
                self._last_header = header
            self.target_value_badge.setText("--")
            self.target_value_badge.setToolTip("指定值: --")
            self._apply_axis_scale_settings(self.current_y_tick_step, 0.0, False)
            self._draw_empty(f"CH{self.channel_index + 1}", "Unconfigured")
            return

        name = str(snapshot["name"])
        address = str(snapshot["address"])
        visible = bool(snapshot["visible"])
        color = str(snapshot["color"])
        latest_text = str(snapshot["latest_text"])
        target_label = str(snapshot.get("target_label", "指定值"))
        target_value_text = str(snapshot.get("target_value_text", "--"))
        header = (name, address, latest_text, target_label)
        if self._last_header != header:
            self.name_label.setText(name)
            self.address_label.setText(address)
            self.latest_badge.setText(latest_text)
            self.target_value_title.setText(target_label)
            self._last_header = header
        self.target_value_badge.setText(target_value_text)
        self.target_value_badge.setToolTip(f"{target_label}: {target_value_text}")

        self._style_axis(f"CH{self.channel_index + 1}")

        x_values = list(snapshot["x"])
        y_values = list(snapshot["y"])
        state = str(snapshot["state"])
        xlim = snapshot["xlim"]
        ylim = snapshot["ylim"]
        lower_limit = float(snapshot.get("lower_limit", self.current_lower_limit))
        upper_limit = float(snapshot.get("upper_limit", self.current_upper_limit))
        upper_limit_color = str(snapshot.get("upper_limit_color", self.current_upper_limit_color))
        lower_limit_color = str(snapshot.get("lower_limit_color", self.current_lower_limit_color))
        highlight_limit_exceeded = bool(snapshot.get("highlight_limit_exceeded", True))
        y_tick_step = float(snapshot.get("y_tick_step", self.current_y_tick_step))
        center_line_y = float(snapshot.get("center_line_y", self.current_center_line_y))
        center_line_visible = bool(snapshot.get("center_line_visible", True)) and visible
        self.sync_limit_controls(lower_limit, upper_limit, upper_limit_color, lower_limit_color)
        self._apply_axis_scale_settings(y_tick_step, center_line_y, center_line_visible)

        if visible and not state:
            self.current_x_values = x_values
            self.current_y_values = y_values
            self.base_line.set_data(x_values, y_values)
            self.base_line.set_color(color)
            self.base_line.set_visible(True)
            alert_segments = self._build_alert_segments(
                x_values,
                y_values,
                lower_limit,
                upper_limit,
                highlight_limit_exceeded,
            )
            self.line_collection.set_segments(alert_segments)
            self.line_collection.set_visible(bool(alert_segments))
            self._set_limit_lines(lower_limit, upper_limit, upper_limit_color, lower_limit_color, True)
            self.state_text.set_visible(False)
            self.axis.set_xlim(*xlim)
            self.axis.set_ylim(*ylim)
        else:
            self.current_x_values = []
            self.current_y_values = []
            self.base_line.set_data([], [])
            self.base_line.set_visible(False)
            self.line_collection.set_segments([])
            self.line_collection.set_visible(False)
            self._set_limit_lines(lower_limit, upper_limit, upper_limit_color, lower_limit_color, visible)
            self.state_text.set_text(state or ("Waiting" if visible else "Hidden"))
            self.state_text.set_visible(True)
            self._hide_hover()
            self.axis.set_xlim(*xlim)
            self.axis.set_ylim(*ylim)

        self.canvas.draw_idle()

    def _hide_hover(self) -> None:
        self._last_hover_index = None
        self.hover_vline.set_visible(False)
        self.hover_hline.set_visible(False)
        self.hover_marker.set_visible(False)
        QtWidgets.QToolTip.hideText()

    def _on_mouse_leave(self, _event) -> None:
        if self._last_hover_index is not None or self.hover_marker.get_visible():
            self._hide_hover()
            self.canvas.draw_idle()

    def _on_mouse_move(self, event) -> None:
        if event.inaxes != self.axis or not self.current_x_values or event.xdata is None or event.ydata is None:
            if self._last_hover_index is not None or self.hover_marker.get_visible():
                self._hide_hover()
                self.canvas.draw_idle()
            return

        nearest_index = min(
            range(len(self.current_x_values)),
            key=lambda idx: abs(self.current_x_values[idx] - float(event.xdata)),
        )
        if self._last_hover_index == nearest_index and self.hover_marker.get_visible():
            return
        self._last_hover_index = nearest_index
        nearest_x = float(self.current_x_values[nearest_index])
        nearest_y = float(self.current_y_values[nearest_index])

        self.hover_vline.set_xdata([nearest_x, nearest_x])
        self.hover_vline.set_visible(True)
        self.hover_hline.set_ydata([nearest_y, nearest_y])
        self.hover_hline.set_visible(True)
        self.hover_marker.set_data([nearest_x], [nearest_y])
        self.hover_marker.set_visible(True)
        tooltip_text = f"Time: {self._format_system_time(nearest_x, with_ms=True)}\nValue: {nearest_y:.3f}"
        local_pos = QtCore.QPoint(
            int(event.x) + 14,
            max(0, min(self.canvas.height(), self.canvas.height() - int(event.y))) + 14,
        )
        global_pos = self.canvas.mapToGlobal(local_pos)
        QtWidgets.QToolTip.showText(global_pos, tooltip_text, self.canvas)
        self.canvas.draw_idle()

    def save_image(self, file_path: str) -> None:
        self.figure.savefig(file_path, dpi=180, facecolor=self.figure.get_facecolor(), bbox_inches="tight")


class WaveformMonitorWindow(QtWidgets.QMainWindow):
    MAX_WAVEFORMS = 8
    CONFIG_FILE_NAME = "waveform_config.json"
    COLORS = {
        "bg": "#11161c",
        "panel": "#1a232d",
        "panel_alt": "#202b35",
        "plot": "#0d1318",
        "border": "#3d4c5c",
        "accent": "#ffb300",
        "accent_soft": "#ffd166",
        "text": "#edf2f7",
        "muted": "#9caabd",
        "success": "#36d399",
        "danger": "#ff6b6b",
    }
    LINE_COLORS = [
        "#00d7ff",
        "#00d7ff",
        "#00d7ff",
        "#00d7ff",
        "#00d7ff",
        "#00d7ff",
        "#00d7ff",
        "#00d7ff",
    ]

    def __init__(self) -> None:
        super().__init__()
        self.ui_scale = self._compute_ui_scale()
        self.base_dir = runtime_base_dir()
        self.config_path = self.base_dir / self.CONFIG_FILE_NAME
        self.COLORS = dict(type(self).COLORS)

        self.plc_client: Optional[SiemensPLCClient] = None
        self.channel_lock = threading.Lock()
        self.sample_queue: "queue.Queue[Dict[str, object]]" = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.sampling_active = False
        self.demo_mode_enabled = False
        self.sample_origin = time.perf_counter()
        self.start_signal_configs = [
            PLCSignalConfig(db_number=1, start=200, bit_index=0),
            PLCSignalConfig(db_number=1, start=202, bit_index=0),
        ]
        self.complete_signal_configs = [
            PLCSignalConfig(db_number=1, start=201, bit_index=0),
            PLCSignalConfig(db_number=1, start=203, bit_index=0),
        ]
        self.sn_read_configs = [
            SNReadConfig(name="A组编号"),
            SNReadConfig(name="B组编号"),
        ]
        self.heartbeat_config = HeartbeatConfig()
        self.save_settings = SaveSettingsConfig()
        self.limit_exceed_red_enabled = True
        self.y_tick_step = 10.0
        self.center_line_mode = "zero"
        self.center_line_custom_value = 1.0
        self.reset_start_signal_enabled = [False, False]
        self.reset_complete_signal_enabled = [False, False]
        self.heartbeat_thread: Optional[threading.Thread] = None
        self.heartbeat_stop_event = threading.Event()
        self.connection_thread: Optional[threading.Thread] = None
        self.connection_attempt_id = 0
        self.active_connection_attempt_id: Optional[int] = None
        self.connection_in_progress = False
        self.auto_connect_enabled = True
        self.resume_sampling_after_connect = True
        self.pending_connect_reason = "connecting"
        self.pending_connect_auto_start = True
        self.batch_start_timestamps: Dict[int, float] = {}
        self.start_signal_active = [False, False]
        self.complete_signal_active = [False, False]
        self.last_sidebar_width = 390
        self.plot_dirty = True
        self.table_dirty = True
        self.selected_channel_index = 0
        self.channels: List[WaveformChannel] = self._create_default_channels()
        self.dirty_plot_indices = set(range(self.MAX_WAVEFORMS))
        self.plot_prepare_queue: "queue.Queue[Dict[int, Dict[str, object]]]" = queue.Queue()
        self.plot_prepare_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="plot_prepare")
        self.plot_prepare_future: Optional[Future] = None
        self.plot_prepare_pending_indices = set(range(self.MAX_WAVEFORMS))
        self.prepared_plot_payloads: Dict[int, Dict[str, object]] = {}
        self.auto_save_request_seq = 0
        self.pending_auto_save_requests: Deque[AutoSaveRequest] = deque()
        self.pending_auto_save_scheduled = False
        self.auto_save_in_progress = False
        self.window_interacting = False

        self.channel_menu: Optional[QtWidgets.QMenu] = None
        self.plot_cards: List[PlotCard] = []

        self._build_window()
        self._build_menu_bar()
        startup_status = self._load_local_config()
        self._refresh_channel_table()
        self._rebuild_channel_menu()
        self._select_channel(0)
        self._refresh_plot_cards()
        self._update_connection_badge("connecting" if not self.demo_mode_enabled else False)
        self._update_sampling_badge(False)
        self._set_status(startup_status or "系统就绪，可先配置数据源或启用演示模式。")

        self.reconnect_timer = QtCore.QTimer(self)
        self.reconnect_timer.setSingleShot(True)
        self.reconnect_timer.timeout.connect(self._attempt_scheduled_connection)

        self.sample_timer = QtCore.QTimer(self)
        self.sample_timer.timeout.connect(self._process_samples)
        self.sample_timer.start(80)

        self.interaction_settle_timer = QtCore.QTimer(self)
        self.interaction_settle_timer.setSingleShot(True)
        self.interaction_settle_timer.timeout.connect(self._end_window_interaction)

        self.table_timer = QtCore.QTimer(self)
        self.table_timer.timeout.connect(self._refresh_table_if_needed)
        self.table_timer.start(250)

        self.plot_timer = QtCore.QTimer(self)
        self.plot_timer.timeout.connect(self._refresh_plots)
        self.plot_timer.start(280)

        self.resume_sampling_after_connect = not self.demo_mode_enabled
        if not self.demo_mode_enabled:
            QtCore.QTimer.singleShot(0, self._startup_auto_connect)

    def _compute_ui_scale(self) -> float:
        screen = QtWidgets.QApplication.primaryScreen()
        if screen is None:
            return 1.0
        rect = screen.availableGeometry()
        scale = min(rect.width() / 1920.0, rect.height() / 1080.0)
        return max(0.85, min(scale, 1.0))

    def _apply_theme_styles(self) -> None:
        self.setStyleSheet(
            f"""
            QMainWindow, QWidget {{
                background: {self.COLORS['bg']};
                color: {self.COLORS['text']};
                font-size: {max(9, int(9 * self.ui_scale))}pt;
            }}
            QFrame#Sidebar, QFrame#PlotArea, QFrame#HeaderPanel {{
                background: {self.COLORS['panel']};
                border: 1px solid {self.COLORS['border']};
                border-radius: 14px;
            }}
            QFrame#PlotCard {{
                background: {self.COLORS['panel_alt']};
                border: 1px solid {self.COLORS['border']};
                border-radius: 14px;
            }}
            QFrame#PlotCard[selected="true"] {{
                border: 2px solid {self.COLORS['accent']};
            }}
            QLabel#PlotTitle {{
                font-size: {max(10, int(10 * self.ui_scale))}pt;
                font-weight: 700;
                color: {self.COLORS['text']};
                background: transparent;
            }}
            QLabel#PlotAddress {{
                color: {self.COLORS['muted']};
                background: transparent;
            }}
            QLabel#ValueBadge {{
                background: {self.COLORS['plot']};
                border: 1px solid {self.COLORS['border']};
                border-radius: 10px;
                color: {self.COLORS['accent_soft']};
                font-weight: 700;
                padding: 6px 10px;
            }}
            QLabel#AxisLabelVertical, QLabel#AxisLabelHorizontal {{
                color: {self.COLORS['accent_soft']};
                background: transparent;
                font-weight: 700;
            }}
            QLabel#InlineLabel {{
                color: {self.COLORS['muted']};
                background: transparent;
                font-size: {max(7, int(7 * self.ui_scale))}pt;
            }}
            QLabel#InlineValueBadge {{
                background: {self.COLORS['plot']};
                border: 1px solid {self.COLORS['border']};
                border-radius: 8px;
                color: {self.COLORS['accent_soft']};
                font-weight: 700;
                padding: 4px 8px;
            }}
            QLabel#TitleLabel {{
                font-size: {max(15, int(16 * self.ui_scale))}pt;
                font-weight: 800;
                color: {self.COLORS['text']};
            }}
            QLabel#HintLabel {{
                color: {self.COLORS['muted']};
            }}
            QLabel#Badge {{
                color: {self.COLORS['bg']};
                border-radius: 10px;
                font-weight: 700;
                padding: 5px 10px;
            }}
            QLabel#StatusLabel {{
                color: {self.COLORS['muted']};
                background: transparent;
                line-height: 1.4;
            }}
            QGroupBox {{
                background: {self.COLORS['panel']};
                border: 1px solid {self.COLORS['border']};
                border-radius: 12px;
                margin-top: 12px;
                padding-top: 12px;
                font-weight: 700;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
                color: {self.COLORS['accent_soft']};
            }}
            QFrame#CollapsibleSection {{
                background: {self.COLORS['panel']};
                border: 1px solid {self.COLORS['border']};
                border-radius: 12px;
            }}
            QToolButton#SectionToggle {{
                background: {self.COLORS['panel']};
                border: none;
                border-radius: 12px;
                padding: 10px 12px;
                color: {self.COLORS['accent_soft']};
                font-weight: 700;
                text-align: left;
            }}
            QToolButton#SectionToggle:hover {{
                color: {self.COLORS['text']};
            }}
            QWidget#CollapsibleBody {{
                background: transparent;
                border-top: 1px solid {self.COLORS['border']};
                border-bottom-left-radius: 12px;
                border-bottom-right-radius: 12px;
            }}
            QLineEdit, QSpinBox, QComboBox, QAbstractSpinBox {{
                background: {self.COLORS['plot']};
                border: 1px solid {self.COLORS['border']};
                border-radius: 8px;
                padding: 5px 8px;
                color: {self.COLORS['text']};
            }}
            QDoubleSpinBox#InlineLimitSpin {{
                padding: 3px 6px;
                font-size: {max(7, int(7 * self.ui_scale))}pt;
            }}
            QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{
                border: 1px solid {self.COLORS['accent']};
            }}
            QPushButton {{
                background: {self.COLORS['panel_alt']};
                border: 1px solid {self.COLORS['border']};
                border-radius: 10px;
                padding: 6px 10px;
                color: {self.COLORS['text']};
                font-weight: 600;
            }}
            QPushButton:hover {{
                border: 1px solid {self.COLORS['accent']};
            }}
            QPushButton#AccentButton {{
                background: {self.COLORS['accent']};
                color: {self.COLORS['bg']};
                border: 1px solid {self.COLORS['accent']};
            }}
            QTableWidget {{
                background: {self.COLORS['plot']};
                border: 1px solid {self.COLORS['border']};
                border-radius: 10px;
                gridline-color: {self.COLORS['border']};
                selection-background-color: #24486b;
                selection-color: {self.COLORS['text']};
            }}
            QHeaderView::section {{
                background: {self.COLORS['panel_alt']};
                color: {self.COLORS['accent_soft']};
                border: none;
                border-bottom: 1px solid {self.COLORS['border']};
                padding: 6px;
                font-weight: 700;
            }}
            QMenuBar {{
                background: {self.COLORS['panel_alt']};
            }}
            QMenuBar::item:selected, QMenu::item:selected {{
                background: {self.COLORS['accent']};
                color: {self.COLORS['bg']};
            }}
            QMenu {{
                background: {self.COLORS['panel_alt']};
                border: 1px solid {self.COLORS['border']};
                color: {self.COLORS['text']};
            }}
            QScrollArea {{
                border: none;
                background: transparent;
            }}
            """
        )
        self.statusBar().setStyleSheet(
            f"QStatusBar{{background:{self.COLORS['panel_alt']}; color:{self.COLORS['muted']}; border-top:1px solid {self.COLORS['border']};}}"
        )

    def _build_window(self) -> None:
        self.setWindowTitle("工业波形图显示软件")
        self.setMinimumSize(1320, 860)

        screen = QtWidgets.QApplication.primaryScreen()
        if screen is not None:
            rect = screen.availableGeometry()
            width = max(1320, int(rect.width() * 0.9))
            height = max(860, int(rect.height() * 0.9))
            self.resize(width, height)

        self._apply_theme_styles()

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)

        outer_layout = QtWidgets.QHBoxLayout(central)
        outer_layout.setContentsMargins(12, 4, 12, 12)
        outer_layout.setSpacing(12)

        self.main_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.main_splitter.setChildrenCollapsible(True)
        self.main_splitter.setCollapsible(0, True)
        self.main_splitter.setCollapsible(1, False)
        self.main_splitter.setHandleWidth(10)
        self.main_splitter.splitterMoved.connect(self._handle_splitter_moved)
        outer_layout.addWidget(self.main_splitter)

        self.sidebar = QtWidgets.QFrame()
        self.sidebar.setObjectName("Sidebar")
        self.sidebar.setMinimumWidth(0)
        self.sidebar.setMaximumWidth(720)
        self.main_splitter.addWidget(self.sidebar)
        self.sidebar.hide()

        self.plot_area = QtWidgets.QFrame()
        self.plot_area.setObjectName("PlotArea")
        self.main_splitter.addWidget(self.plot_area)
        self.main_splitter.setSizes([0, 1280])

        sidebar_shell_layout = QtWidgets.QVBoxLayout(self.sidebar)
        sidebar_shell_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_shell_layout.setSpacing(0)

        self.sidebar_scroll = QtWidgets.QScrollArea()
        self.sidebar_scroll.setObjectName("SidebarScroll")
        self.sidebar_scroll.setWidgetResizable(True)
        self.sidebar_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.sidebar_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.sidebar_scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        sidebar_shell_layout.addWidget(self.sidebar_scroll)

        self.sidebar_content = QtWidgets.QWidget()
        self.sidebar_content.setObjectName("SidebarContent")
        self.sidebar_scroll.setWidget(self.sidebar_content)

        sidebar_layout = QtWidgets.QVBoxLayout(self.sidebar_content)
        sidebar_layout.setContentsMargins(12, 12, 12, 12)
        sidebar_layout.setSpacing(10)
        sidebar_layout.setSizeConstraint(QtWidgets.QLayout.SetMinAndMaxSize)

        plc_group = QtWidgets.QGroupBox("数据源通讯")
        sidebar_layout.addWidget(plc_group)
        plc_form = QtWidgets.QFormLayout(plc_group)
        plc_form.setLabelAlignment(QtCore.Qt.AlignLeft)
        plc_form.setFormAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)

        self.ip_edit = QtWidgets.QLineEdit("192.168.0.1")
        plc_form.addRow("PLC IP", self.ip_edit)

        self.rack_spin = QtWidgets.QSpinBox()
        self.rack_spin.setRange(0, 32)
        plc_form.addRow("机架", self.rack_spin)

        self.slot_spin = QtWidgets.QSpinBox()
        self.slot_spin.setRange(0, 32)
        self.slot_spin.setValue(1)
        plc_form.addRow("插槽", self.slot_spin)

        self.start_signal_db_spins: List[QtWidgets.QSpinBox] = []
        self.start_signal_byte_spins: List[QtWidgets.QSpinBox] = []
        self.start_signal_bit_spins: List[QtWidgets.QSpinBox] = []
        self.start_signal_reset_checkboxes: List[QtWidgets.QCheckBox] = []
        self.complete_signal_db_spins: List[QtWidgets.QSpinBox] = []
        self.complete_signal_byte_spins: List[QtWidgets.QSpinBox] = []
        self.complete_signal_bit_spins: List[QtWidgets.QSpinBox] = []
        self.complete_signal_reset_checkboxes: List[QtWidgets.QCheckBox] = []

        for group_id in (1, 2):
            start_signal_row = QtWidgets.QHBoxLayout()
            start_signal_row.setSpacing(4)
            start_signal_config = self.start_signal_configs[group_id - 1]
            start_signal_db_spin = QtWidgets.QSpinBox()
            start_signal_db_spin.setRange(1, 65535)
            start_signal_db_spin.setValue(start_signal_config.db_number)
            start_signal_byte_spin = QtWidgets.QSpinBox()
            start_signal_byte_spin.setRange(0, 65535)
            start_signal_byte_spin.setValue(start_signal_config.start)
            start_signal_bit_spin = QtWidgets.QSpinBox()
            start_signal_bit_spin.setRange(0, 7)
            start_signal_bit_spin.setValue(start_signal_config.bit_index)
            start_signal_row.addWidget(QtWidgets.QLabel("DB"))
            start_signal_row.addWidget(start_signal_db_spin)
            start_signal_row.addWidget(QtWidgets.QLabel("字节"))
            start_signal_row.addWidget(start_signal_byte_spin)
            start_signal_row.addWidget(QtWidgets.QLabel("位"))
            start_signal_row.addWidget(start_signal_bit_spin)
            start_signal_reset_checkbox = QtWidgets.QCheckBox("置零")
            start_signal_row.addWidget(start_signal_reset_checkbox)
            plc_form.addRow(f"开始信号{group_id}", start_signal_row)
            self.start_signal_db_spins.append(start_signal_db_spin)
            self.start_signal_byte_spins.append(start_signal_byte_spin)
            self.start_signal_bit_spins.append(start_signal_bit_spin)
            self.start_signal_reset_checkboxes.append(start_signal_reset_checkbox)

            complete_signal_row = QtWidgets.QHBoxLayout()
            complete_signal_row.setSpacing(4)
            complete_signal_config = self.complete_signal_configs[group_id - 1]
            complete_signal_db_spin = QtWidgets.QSpinBox()
            complete_signal_db_spin.setRange(1, 65535)
            complete_signal_db_spin.setValue(complete_signal_config.db_number)
            complete_signal_byte_spin = QtWidgets.QSpinBox()
            complete_signal_byte_spin.setRange(0, 65535)
            complete_signal_byte_spin.setValue(complete_signal_config.start)
            complete_signal_bit_spin = QtWidgets.QSpinBox()
            complete_signal_bit_spin.setRange(0, 7)
            complete_signal_bit_spin.setValue(complete_signal_config.bit_index)
            complete_signal_row.addWidget(QtWidgets.QLabel("DB"))
            complete_signal_row.addWidget(complete_signal_db_spin)
            complete_signal_row.addWidget(QtWidgets.QLabel("字节"))
            complete_signal_row.addWidget(complete_signal_byte_spin)
            complete_signal_row.addWidget(QtWidgets.QLabel("位"))
            complete_signal_row.addWidget(complete_signal_bit_spin)
            complete_signal_reset_checkbox = QtWidgets.QCheckBox("置零")
            complete_signal_row.addWidget(complete_signal_reset_checkbox)
            plc_form.addRow(f"完成信号{group_id}", complete_signal_row)
            self.complete_signal_db_spins.append(complete_signal_db_spin)
            self.complete_signal_byte_spins.append(complete_signal_byte_spin)
            self.complete_signal_bit_spins.append(complete_signal_bit_spin)
            self.complete_signal_reset_checkboxes.append(complete_signal_reset_checkbox)

        self.demo_checkbox = QtWidgets.QCheckBox("启用演示模式")
        self.demo_checkbox.stateChanged.connect(self._toggle_demo_mode)
        plc_form.addRow("", self.demo_checkbox)

        plc_buttons = QtWidgets.QGridLayout()
        self.connect_button = QtWidgets.QPushButton("连接数据源")
        self.connect_button.setObjectName("AccentButton")
        self.connect_button.clicked.connect(self._connect_plc)
        plc_buttons.addWidget(self.connect_button, 0, 0)

        self.disconnect_button = QtWidgets.QPushButton("断开连接")
        self.disconnect_button.clicked.connect(self._disconnect_plc)
        plc_buttons.addWidget(self.disconnect_button, 0, 1)

        self.start_button = QtWidgets.QPushButton("开始采样")
        self.start_button.setObjectName("AccentButton")
        self.start_button.clicked.connect(self._start_sampling)
        plc_buttons.addWidget(self.start_button, 1, 0)

        self.stop_button = QtWidgets.QPushButton("停止采样")
        self.stop_button.clicked.connect(self._stop_sampling)
        plc_buttons.addWidget(self.stop_button, 1, 1)
        plc_form.addRow(plc_buttons)

        channel_group = QtWidgets.QGroupBox("8 通道总览")
        sidebar_layout.addWidget(channel_group)
        channel_layout = QtWidgets.QVBoxLayout(channel_group)

        self.channel_table = QtWidgets.QTableWidget(len(self.channels), 5)
        self.channel_table.setMinimumHeight(max(220, int(240 * self.ui_scale)))
        self.channel_table.setHorizontalHeaderLabels(["名称", "地址", "类型", "状态", "最新值"])
        self.channel_table.verticalHeader().setVisible(False)
        self.channel_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.channel_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.channel_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.channel_table.setAlternatingRowColors(False)
        self.channel_table.horizontalHeader().setStretchLastSection(True)
        self.channel_table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        self.channel_table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        self.channel_table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        self.channel_table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)
        self.channel_table.horizontalHeader().setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeToContents)
        self.channel_table.itemSelectionChanged.connect(self._handle_channel_table_selection)
        channel_layout.addWidget(self.channel_table, 1)

        self.sn_section = CollapsibleSection("编号读取选项", expanded=False)
        sidebar_layout.addWidget(self.sn_section)
        sn_content = QtWidgets.QWidget()
        sn_layout = QtWidgets.QVBoxLayout(sn_content)
        sn_layout.setContentsMargins(0, 0, 0, 0)
        sn_layout.setSpacing(10)

        self.sn_name_edits: List[QtWidgets.QLineEdit] = []
        self.sn_db_spins: List[QtWidgets.QSpinBox] = []
        self.sn_start_spins: List[QtWidgets.QSpinBox] = []
        self.sn_type_combos: List[QtWidgets.QComboBox] = []
        self.sn_bit_spins: List[QtWidgets.QSpinBox] = []
        self.sn_enabled_checkboxes: List[QtWidgets.QCheckBox] = []

        for group_id in (1, 2):
            sn_group = QtWidgets.QGroupBox("A组编号" if group_id == 1 else "B组编号")
            group_form = QtWidgets.QFormLayout(sn_group)
            config = self.sn_read_configs[group_id - 1]

            sn_name_edit = QtWidgets.QLineEdit(config.name)
            group_form.addRow("读取名称", sn_name_edit)

            sn_db_spin = QtWidgets.QSpinBox()
            sn_db_spin.setRange(1, 65535)
            sn_db_spin.setValue(config.db_number)
            group_form.addRow("DB 块号", sn_db_spin)

            sn_start_spin = QtWidgets.QSpinBox()
            sn_start_spin.setRange(0, 65535)
            sn_start_spin.setValue(config.start)
            group_form.addRow("起始字节", sn_start_spin)

            sn_type_combo = QtWidgets.QComboBox()
            sn_type_combo.addItems(list(SUPPORTED_DATA_TYPES))
            sn_type_combo.setCurrentText(config.data_type)
            sn_type_combo.currentTextChanged.connect(
                lambda _text, current_group=group_id: self._sync_sn_bit_state(current_group)
            )
            group_form.addRow("数据类型", sn_type_combo)

            sn_bit_spin = QtWidgets.QSpinBox()
            sn_bit_spin.setRange(0, 7)
            sn_bit_spin.setValue(config.bit_index)
            group_form.addRow("BOOL 位", sn_bit_spin)

            sn_enabled_checkbox = QtWidgets.QCheckBox("启用编号读取")
            sn_enabled_checkbox.setChecked(config.enabled)
            group_form.addRow("", sn_enabled_checkbox)

            self.sn_name_edits.append(sn_name_edit)
            self.sn_db_spins.append(sn_db_spin)
            self.sn_start_spins.append(sn_start_spin)
            self.sn_type_combos.append(sn_type_combo)
            self.sn_bit_spins.append(sn_bit_spin)
            self.sn_enabled_checkboxes.append(sn_enabled_checkbox)
            sn_layout.addWidget(sn_group)

        self.sn_apply_button = QtWidgets.QPushButton("应用编号设置")
        self.sn_apply_button.setObjectName("AccentButton")
        self.sn_apply_button.clicked.connect(self._apply_sn_read_config)
        sn_layout.addWidget(self.sn_apply_button)
        self.sn_section.set_content_widget(sn_content)
        self._sync_sn_bit_state()

        self.editor_section = CollapsibleSection("通道编辑", expanded=False)
        sidebar_layout.addWidget(self.editor_section)
        editor_content = QtWidgets.QWidget()
        editor_form = QtWidgets.QFormLayout(editor_content)

        self.name_edit = QtWidgets.QLineEdit()
        editor_form.addRow("Display Name", self.name_edit)

        self.db_spin = QtWidgets.QSpinBox()
        self.db_spin.setRange(1, 65535)
        editor_form.addRow("DB 块号", self.db_spin)

        self.start_spin = QtWidgets.QSpinBox()
        self.start_spin.setRange(0, 65535)
        editor_form.addRow("数组起始字节", self.start_spin)

        self.type_combo = QtWidgets.QComboBox()
        self.type_combo.addItems(list(SUPPORTED_DATA_TYPES))
        self.type_combo.currentTextChanged.connect(self._sync_bit_state)
        editor_form.addRow("数据类型", self.type_combo)

        self.bit_spin = QtWidgets.QSpinBox()
        self.bit_spin.setRange(0, 7)
        editor_form.addRow("BOOL 位", self.bit_spin)

        self.visible_checkbox = QtWidgets.QCheckBox("Show this waveform")
        editor_form.addRow("", self.visible_checkbox)

        editor_buttons = QtWidgets.QHBoxLayout()
        self.apply_button = QtWidgets.QPushButton("应用修改")
        self.apply_button.setObjectName("AccentButton")
        self.apply_button.clicked.connect(self._apply_channel_changes)
        editor_buttons.addWidget(self.apply_button)

        self.save_selected_button = QtWidgets.QPushButton("Save Current Chart")
        self.save_selected_button.clicked.connect(self._save_selected_plot)
        editor_buttons.addWidget(self.save_selected_button)
        editor_form.addRow(editor_buttons)
        self.editor_section.set_content_widget(editor_content)

        self.heartbeat_section = CollapsibleSection("心跳设置", expanded=False)
        sidebar_layout.addWidget(self.heartbeat_section)
        heartbeat_content = QtWidgets.QWidget()
        heartbeat_form = QtWidgets.QFormLayout(heartbeat_content)

        self.heartbeat_name_edit = QtWidgets.QLineEdit(self.heartbeat_config.name)
        heartbeat_form.addRow("变量名称", self.heartbeat_name_edit)

        self.heartbeat_db_spin = QtWidgets.QSpinBox()
        self.heartbeat_db_spin.setRange(1, 65535)
        self.heartbeat_db_spin.setValue(self.heartbeat_config.db_number)
        heartbeat_form.addRow("DB 块号", self.heartbeat_db_spin)

        self.heartbeat_start_spin = QtWidgets.QSpinBox()
        self.heartbeat_start_spin.setRange(0, 65535)
        self.heartbeat_start_spin.setValue(self.heartbeat_config.start)
        heartbeat_form.addRow("起始字节", self.heartbeat_start_spin)

        self.heartbeat_type_combo = QtWidgets.QComboBox()
        self.heartbeat_type_combo.addItems(list(SUPPORTED_DATA_TYPES))
        self.heartbeat_type_combo.setCurrentText(self.heartbeat_config.data_type)
        self.heartbeat_type_combo.currentTextChanged.connect(self._sync_heartbeat_bit_state)
        heartbeat_form.addRow("数据类型", self.heartbeat_type_combo)

        self.heartbeat_bit_spin = QtWidgets.QSpinBox()
        self.heartbeat_bit_spin.setRange(0, 7)
        self.heartbeat_bit_spin.setValue(self.heartbeat_config.bit_index)
        heartbeat_form.addRow("BOOL 位", self.heartbeat_bit_spin)

        self.heartbeat_high_value_edit = QtWidgets.QLineEdit(self.heartbeat_config.high_value)
        heartbeat_form.addRow("发送值1", self.heartbeat_high_value_edit)

        self.heartbeat_low_value_edit = QtWidgets.QLineEdit(self.heartbeat_config.low_value)
        heartbeat_form.addRow("发送值0", self.heartbeat_low_value_edit)

        self.heartbeat_high_interval_spin = QtWidgets.QDoubleSpinBox()
        self.heartbeat_high_interval_spin.setRange(0.1, 3600.0)
        self.heartbeat_high_interval_spin.setDecimals(2)
        self.heartbeat_high_interval_spin.setSingleStep(0.1)
        self.heartbeat_high_interval_spin.setValue(self.heartbeat_config.high_interval_s)
        heartbeat_form.addRow("发1间隔(秒)", self.heartbeat_high_interval_spin)

        self.heartbeat_low_interval_spin = QtWidgets.QDoubleSpinBox()
        self.heartbeat_low_interval_spin.setRange(0.1, 3600.0)
        self.heartbeat_low_interval_spin.setDecimals(2)
        self.heartbeat_low_interval_spin.setSingleStep(0.1)
        self.heartbeat_low_interval_spin.setValue(self.heartbeat_config.low_interval_s)
        heartbeat_form.addRow("发0间隔(秒)", self.heartbeat_low_interval_spin)

        self.heartbeat_enabled_checkbox = QtWidgets.QCheckBox("启用心跳")
        self.heartbeat_enabled_checkbox.setChecked(self.heartbeat_config.enabled)
        heartbeat_form.addRow("", self.heartbeat_enabled_checkbox)

        self.heartbeat_apply_button = QtWidgets.QPushButton("应用心跳设置")
        self.heartbeat_apply_button.setObjectName("AccentButton")
        self.heartbeat_apply_button.clicked.connect(self._apply_heartbeat_config)
        heartbeat_form.addRow(self.heartbeat_apply_button)
        self.heartbeat_section.set_content_widget(heartbeat_content)
        self._sync_heartbeat_bit_state()

        self.limit_section = CollapsibleSection("上下限功能", expanded=False)
        sidebar_layout.addWidget(self.limit_section)
        limit_content = QtWidgets.QWidget()
        limit_form = QtWidgets.QFormLayout(limit_content)

        self.limit_exceed_red_checkbox = QtWidgets.QCheckBox("超出上下限时曲线变红")
        self.limit_exceed_red_checkbox.setChecked(self.limit_exceed_red_enabled)
        limit_form.addRow("", self.limit_exceed_red_checkbox)

        self.limit_channel_label = QtWidgets.QLabel()
        self.limit_channel_label.setObjectName("HintLabel")
        limit_form.addRow("Current Channel", self.limit_channel_label)

        self.limit_upper_db_spin = QtWidgets.QSpinBox()
        self.limit_upper_db_spin.setRange(1, 65535)
        limit_form.addRow("上限 DB块号", self.limit_upper_db_spin)

        self.limit_upper_start_spin = QtWidgets.QSpinBox()
        self.limit_upper_start_spin.setRange(0, 65535)
        limit_form.addRow("上限 起始字节", self.limit_upper_start_spin)

        self.limit_upper_type_combo = QtWidgets.QComboBox()
        self.limit_upper_type_combo.addItems(list(SUPPORTED_DATA_TYPES))
        self.limit_upper_type_combo.currentTextChanged.connect(self._sync_limit_read_bit_state)
        limit_form.addRow("上限 数据类型", self.limit_upper_type_combo)

        self.limit_upper_bit_spin = QtWidgets.QSpinBox()
        self.limit_upper_bit_spin.setRange(0, 7)
        limit_form.addRow("上限 BOOL位", self.limit_upper_bit_spin)

        self.limit_upper_enabled_checkbox = QtWidgets.QCheckBox("启用上限读取")
        limit_form.addRow("", self.limit_upper_enabled_checkbox)

        self.limit_lower_db_spin = QtWidgets.QSpinBox()
        self.limit_lower_db_spin.setRange(1, 65535)
        limit_form.addRow("下限 DB块号", self.limit_lower_db_spin)

        self.limit_lower_start_spin = QtWidgets.QSpinBox()
        self.limit_lower_start_spin.setRange(0, 65535)
        limit_form.addRow("下限 起始字节", self.limit_lower_start_spin)

        self.limit_lower_type_combo = QtWidgets.QComboBox()
        self.limit_lower_type_combo.addItems(list(SUPPORTED_DATA_TYPES))
        self.limit_lower_type_combo.currentTextChanged.connect(self._sync_limit_read_bit_state)
        limit_form.addRow("下限 数据类型", self.limit_lower_type_combo)

        self.limit_lower_bit_spin = QtWidgets.QSpinBox()
        self.limit_lower_bit_spin.setRange(0, 7)
        limit_form.addRow("下限 BOOL位", self.limit_lower_bit_spin)

        self.limit_lower_enabled_checkbox = QtWidgets.QCheckBox("启用下限读取")
        limit_form.addRow("", self.limit_lower_enabled_checkbox)

        self.limit_baseline_db_spin = QtWidgets.QSpinBox()
        self.limit_baseline_db_spin.setRange(1, 65535)
        limit_form.addRow("基准点 DB块号", self.limit_baseline_db_spin)

        self.limit_baseline_start_spin = QtWidgets.QSpinBox()
        self.limit_baseline_start_spin.setRange(0, 65535)
        limit_form.addRow("基准点 起始字节", self.limit_baseline_start_spin)

        self.limit_baseline_type_combo = QtWidgets.QComboBox()
        self.limit_baseline_type_combo.addItems(list(SUPPORTED_DATA_TYPES))
        self.limit_baseline_type_combo.currentTextChanged.connect(self._sync_limit_read_bit_state)
        limit_form.addRow("基准点 数据类型", self.limit_baseline_type_combo)

        self.limit_baseline_bit_spin = QtWidgets.QSpinBox()
        self.limit_baseline_bit_spin.setRange(0, 7)
        limit_form.addRow("基准点 BOOL位", self.limit_baseline_bit_spin)

        self.limit_baseline_enabled_checkbox = QtWidgets.QCheckBox("启用基准点读取")
        limit_form.addRow("", self.limit_baseline_enabled_checkbox)

        self.limit_apply_button = QtWidgets.QPushButton("应用上下限设置")
        self.limit_apply_button.setObjectName("AccentButton")
        self.limit_apply_button.clicked.connect(self._apply_limit_display_config)
        limit_form.addRow(self.limit_apply_button)
        self.limit_section.set_content_widget(limit_content)
        self._sync_limit_read_bit_state()

        self.target_section = CollapsibleSection("指定值参数", expanded=False)
        sidebar_layout.addWidget(self.target_section)
        target_content = QtWidgets.QWidget()
        target_form = QtWidgets.QFormLayout(target_content)

        self.target_channel_label = QtWidgets.QLabel()
        self.target_channel_label.setObjectName("HintLabel")
        target_form.addRow("Current Channel", self.target_channel_label)

        self.target_label_edit = QtWidgets.QLineEdit("指定值")
        target_form.addRow("显示文字", self.target_label_edit)

        self.target_db_spin = QtWidgets.QSpinBox()
        self.target_db_spin.setRange(1, 65535)
        target_form.addRow("DB 块号", self.target_db_spin)

        self.target_start_spin = QtWidgets.QSpinBox()
        self.target_start_spin.setRange(0, 65535)
        target_form.addRow("起始字节", self.target_start_spin)

        self.target_type_combo = QtWidgets.QComboBox()
        self.target_type_combo.addItems(list(SUPPORTED_DATA_TYPES))
        self.target_type_combo.currentTextChanged.connect(self._sync_target_bit_state)
        target_form.addRow("数据类型", self.target_type_combo)

        self.target_bit_spin = QtWidgets.QSpinBox()
        self.target_bit_spin.setRange(0, 7)
        target_form.addRow("BOOL 位", self.target_bit_spin)

        self.target_enabled_checkbox = QtWidgets.QCheckBox("启用指定值读取")
        target_form.addRow("", self.target_enabled_checkbox)

        self.target_apply_button = QtWidgets.QPushButton("应用指定值")
        self.target_apply_button.setObjectName("AccentButton")
        self.target_apply_button.clicked.connect(self._apply_target_value_config)
        target_form.addRow(self.target_apply_button)
        self.target_section.set_content_widget(target_content)
        self._sync_target_bit_state()

        self.scale_section = CollapsibleSection("刻度值选项", expanded=False)
        sidebar_layout.addWidget(self.scale_section)
        scale_content = QtWidgets.QWidget()
        scale_form = QtWidgets.QFormLayout(scale_content)

        self.y_tick_step_spin = QtWidgets.QDoubleSpinBox()
        self.y_tick_step_spin.setRange(0.1, 1000000.0)
        self.y_tick_step_spin.setDecimals(4)
        self.y_tick_step_spin.setSingleStep(0.1)
        self.y_tick_step_spin.setValue(self.y_tick_step)
        scale_form.addRow("纵轴刻度间隔", self.y_tick_step_spin)

        self.center_line_mode_combo = QtWidgets.QComboBox()
        self.center_line_mode_combo.addItem("0点绘制", "zero")
        self.center_line_mode_combo.addItem("根据第一个点绘制", "first_point")
        self.center_line_mode_combo.addItem("根据PLC基准点绘制", "plc_point")
        self.center_line_mode_combo.addItem("根据指定值绘制", "custom_value")
        self.center_line_mode_combo.currentIndexChanged.connect(self._sync_center_line_custom_state)
        scale_form.addRow("中心线基准", self.center_line_mode_combo)

        self.center_line_custom_value_spin = QtWidgets.QDoubleSpinBox()
        self.center_line_custom_value_spin.setRange(0.1, 10.0)
        self.center_line_custom_value_spin.setDecimals(1)
        self.center_line_custom_value_spin.setSingleStep(0.1)
        self.center_line_custom_value_spin.setValue(self.center_line_custom_value)
        scale_form.addRow("中心线指定值", self.center_line_custom_value_spin)

        self.scale_apply_button = QtWidgets.QPushButton("应用刻度值")
        self.scale_apply_button.setObjectName("AccentButton")
        self.scale_apply_button.clicked.connect(self._apply_scale_settings)
        scale_form.addRow(self.scale_apply_button)
        self.scale_section.set_content_widget(scale_content)
        self._sync_center_line_custom_state()

        self.save_section = CollapsibleSection("保存设置", expanded=False)
        sidebar_layout.addWidget(self.save_section)
        save_content = QtWidgets.QWidget()
        save_form = QtWidgets.QFormLayout(save_content)

        save_root_row_widget = QtWidgets.QWidget()
        save_root_row = QtWidgets.QHBoxLayout(save_root_row_widget)
        save_root_row.setContentsMargins(0, 0, 0, 0)
        save_root_row.setSpacing(6)
        self.save_root_dir_edit = QtWidgets.QLineEdit(self.save_settings.root_dir)
        save_root_row.addWidget(self.save_root_dir_edit, 1)
        self.save_root_dir_button = QtWidgets.QPushButton("浏览")
        self.save_root_dir_button.clicked.connect(self._choose_save_root_dir)
        save_root_row.addWidget(self.save_root_dir_button)
        save_form.addRow("Output Folder", save_root_row_widget)

        self.save_date_folder_format_edit = QtWidgets.QLineEdit(self.save_settings.date_folder_format)
        save_form.addRow("日期目录格式", self.save_date_folder_format_edit)

        self.save_use_time_subfolder_checkbox = QtWidgets.QCheckBox("按时间创建子目录")
        self.save_use_time_subfolder_checkbox.setChecked(self.save_settings.use_time_subfolder)
        self.save_use_time_subfolder_checkbox.toggled.connect(self._sync_save_time_folder_state)
        save_form.addRow("", self.save_use_time_subfolder_checkbox)

        self.save_time_folder_format_edit = QtWidgets.QLineEdit(self.save_settings.time_folder_format)
        save_form.addRow("时间目录格式", self.save_time_folder_format_edit)

        self.save_filename_pattern_edit = QtWidgets.QLineEdit(self.save_settings.filename_pattern)
        save_form.addRow("PDF Filename Pattern", self.save_filename_pattern_edit)

        self.save_pattern_hint_label = QtWidgets.QLabel(
            "占位符：{sn}  {date}  {time}  {datetime}  {group}\n"
            "也支持 strftime，例如：%H%M%S_{sn}，其中 %f 表示毫秒(3位)"
        )
        self.save_pattern_hint_label.setObjectName("HintLabel")
        self.save_pattern_hint_label.setWordWrap(True)
        save_form.addRow("", self.save_pattern_hint_label)

        self.save_apply_button = QtWidgets.QPushButton("应用保存设置")
        self.save_apply_button.setObjectName("AccentButton")
        self.save_apply_button.clicked.connect(self._apply_save_settings)
        save_form.addRow(self.save_apply_button)
        self.save_section.set_content_widget(save_content)
        self._sync_save_time_folder_state()

        self.theme_section = CollapsibleSection("主题功能", expanded=False)
        sidebar_layout.addWidget(self.theme_section)
        theme_content = QtWidgets.QWidget()
        theme_form = QtWidgets.QFormLayout(theme_content)

        self.theme_bg_button = QtWidgets.QPushButton()
        self.theme_bg_button.clicked.connect(lambda: self._choose_theme_color("bg"))
        theme_form.addRow("总背景", self.theme_bg_button)

        self.theme_panel_button = QtWidgets.QPushButton()
        self.theme_panel_button.clicked.connect(lambda: self._choose_theme_color("panel"))
        theme_form.addRow("面板层", self.theme_panel_button)

        self.theme_plot_button = QtWidgets.QPushButton()
        self.theme_plot_button.clicked.connect(lambda: self._choose_theme_color("plot"))
        theme_form.addRow("图表背景", self.theme_plot_button)

        self.theme_apply_button = QtWidgets.QPushButton("应用主题")
        self.theme_apply_button.setObjectName("AccentButton")
        self.theme_apply_button.clicked.connect(self._apply_theme_config)
        theme_form.addRow(self.theme_apply_button)

        self.theme_reset_button = QtWidgets.QPushButton("恢复主题")
        self.theme_reset_button.clicked.connect(self._reset_theme_config)
        theme_form.addRow(self.theme_reset_button)
        self.theme_section.set_content_widget(theme_content)
        self._set_theme_color_button(self.theme_bg_button, self.COLORS["bg"])
        self._set_theme_color_button(self.theme_panel_button, self.COLORS["panel"])
        self._set_theme_color_button(self.theme_plot_button, self.COLORS["plot"])

        self.status_label = QtWidgets.QLabel()
        self.status_label.setObjectName("StatusLabel")
        self.status_label.setWordWrap(True)
        sidebar_layout.addWidget(self.status_label)
        sidebar_layout.addStretch(1)

        plot_layout = QtWidgets.QVBoxLayout(self.plot_area)
        plot_layout.setContentsMargins(12, 12, 12, 12)
        plot_layout.setSpacing(10)

        header_panel = QtWidgets.QFrame()
        header_panel.setObjectName("HeaderPanel")
        plot_layout.addWidget(header_panel)
        header_layout = QtWidgets.QHBoxLayout(header_panel)
        header_layout.setContentsMargins(14, 12, 14, 12)

        self.sidebar_toggle_button = QtWidgets.QPushButton("收起设置")
        self.sidebar_toggle_button.setObjectName("SidebarToggleButton")
        self.sidebar_toggle_button.clicked.connect(self._toggle_sidebar)
        header_layout.addWidget(self.sidebar_toggle_button, 0, QtCore.Qt.AlignLeft)

        title_layout = QtWidgets.QVBoxLayout()
        header_layout.addLayout(title_layout, 1)

        title_label = QtWidgets.QLabel("工业波形图显示软件")
        title_label.setObjectName("TitleLabel")
        title_layout.addWidget(title_label)

        badge_layout = QtWidgets.QHBoxLayout()
        badge_layout.setSpacing(8)
        header_layout.addLayout(badge_layout)

        self.connection_badge = QtWidgets.QLabel()
        self.connection_badge.setObjectName("Badge")
        badge_layout.addWidget(self.connection_badge)

        self.sampling_badge = QtWidgets.QLabel()
        self.sampling_badge.setObjectName("Badge")
        badge_layout.addWidget(self.sampling_badge)

        self.plot_scroll = QtWidgets.QScrollArea()
        self.plot_scroll.setWidgetResizable(True)
        plot_layout.addWidget(self.plot_scroll, 1)

        self.plot_grid_widget = QtWidgets.QWidget()
        self.plot_scroll.setWidget(self.plot_grid_widget)
        plot_grid = QtWidgets.QGridLayout(self.plot_grid_widget)
        plot_grid.setContentsMargins(0, 0, 0, 0)
        plot_grid.setSpacing(12)

        for index in range(self.MAX_WAVEFORMS):
            card = PlotCard(index, self.COLORS, self.ui_scale)
            card.limitsChanged.connect(self._apply_card_limits)
            card.upperLimitColorChanged.connect(self._apply_card_upper_limit_color)
            card.lowerLimitColorChanged.connect(self._apply_card_lower_limit_color)
            self.plot_cards.append(card)
            plot_grid.addWidget(card, index % 4, index // 4)

        for row in range(4):
            plot_grid.setRowStretch(row, 1)
        for column in range(2):
            plot_grid.setColumnStretch(column, 1)

        self._update_sidebar_toggle_button()

    def _build_menu_bar(self) -> None:
        self.channel_menu = None
        menu_bar = self.menuBar()
        menu_bar.clear()
        menu_bar.setNativeMenuBar(False)
        menu_bar.setFixedHeight(0)
        menu_bar.hide()

    def _create_default_channels(self) -> List[WaveformChannel]:
        channels: List[WaveformChannel] = []
        for index in range(self.MAX_WAVEFORMS):
            channel = WaveformChannel(
                name=f"波形 {index + 1}",
                color=self.LINE_COLORS[index % len(self.LINE_COLORS)],
                db_number=1,
                start=index * 4,
                data_type="REAL",
            )
            channels.append(channel)
        return channels

    @staticmethod
    def _coerce_int(value: object, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _coerce_float(value: object, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _set_spin_value(self, spin_box: QtWidgets.QSpinBox, value: object) -> None:
        coerced = self._coerce_int(value, spin_box.value())
        spin_box.setValue(max(spin_box.minimum(), min(spin_box.maximum(), coerced)))

    def _set_double_spin_value(self, spin_box: QtWidgets.QDoubleSpinBox, value: object) -> None:
        coerced = self._coerce_float(value, spin_box.value())
        spin_box.setValue(max(spin_box.minimum(), min(spin_box.maximum(), coerced)))

    @staticmethod
    def _set_combo_data_value(combo_box: QtWidgets.QComboBox, value: object) -> None:
        target = str(value).strip().lower()
        for index in range(combo_box.count()):
            if str(combo_box.itemData(index)).strip().lower() == target:
                combo_box.setCurrentIndex(index)
                return
        if combo_box.count():
            combo_box.setCurrentIndex(0)

    def _signal_config_payload(self, group_id: int, signal_type: str) -> Dict[str, object]:
        index = group_id - 1
        if signal_type == "start":
            return {
                "db_number": int(self.start_signal_db_spins[index].value()),
                "start": int(self.start_signal_byte_spins[index].value()),
                "bit_index": int(self.start_signal_bit_spins[index].value()),
                "reset_after_trigger": bool(self.start_signal_reset_checkboxes[index].isChecked()),
            }
        return {
            "db_number": int(self.complete_signal_db_spins[index].value()),
            "start": int(self.complete_signal_byte_spins[index].value()),
            "bit_index": int(self.complete_signal_bit_spins[index].value()),
            "reset_after_trigger": bool(self.complete_signal_reset_checkboxes[index].isChecked()),
        }

    def _load_signal_config_to_ui(self, group_id: int, signal_type: str, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        index = group_id - 1
        if signal_type == "start":
            self._set_spin_value(self.start_signal_db_spins[index], payload.get("db_number"))
            self._set_spin_value(self.start_signal_byte_spins[index], payload.get("start"))
            self._set_spin_value(self.start_signal_bit_spins[index], payload.get("bit_index"))
            self.start_signal_reset_checkboxes[index].setChecked(bool(payload.get("reset_after_trigger", False)))
            return
        self._set_spin_value(self.complete_signal_db_spins[index], payload.get("db_number"))
        self._set_spin_value(self.complete_signal_byte_spins[index], payload.get("start"))
        self._set_spin_value(self.complete_signal_bit_spins[index], payload.get("bit_index"))
        self.complete_signal_reset_checkboxes[index].setChecked(bool(payload.get("reset_after_trigger", False)))

    def _sn_read_payload(self, group_id: int) -> Dict[str, object]:
        index = group_id - 1
        return {
            "name": self.sn_name_edits[index].text().strip() or ("A组编号" if group_id == 1 else "B组编号"),
            "db_number": int(self.sn_db_spins[index].value()),
            "start": int(self.sn_start_spins[index].value()),
            "data_type": self.sn_type_combos[index].currentText().strip().upper(),
            "bit_index": int(self.sn_bit_spins[index].value()),
            "enabled": bool(self.sn_enabled_checkboxes[index].isChecked()),
        }

    def _load_sn_read_config_to_ui(self, group_id: int, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        index = group_id - 1
        name = payload.get("name")
        if isinstance(name, str) and name.strip():
            self.sn_name_edits[index].setText(name.strip())
        self._set_spin_value(self.sn_db_spins[index], payload.get("db_number"))
        self._set_spin_value(self.sn_start_spins[index], payload.get("start"))
        sn_type = str(payload.get("data_type", self.sn_read_configs[index].data_type)).strip().upper()
        if sn_type not in SUPPORTED_DATA_TYPES:
            sn_type = self.sn_read_configs[index].data_type
        self.sn_type_combos[index].setCurrentText(sn_type)
        self._set_spin_value(self.sn_bit_spins[index], payload.get("bit_index"))
        self.sn_enabled_checkboxes[index].setChecked(bool(payload.get("enabled", False)))

    def _sync_sn_read_configs_from_ui(self) -> None:
        configs: List[SNReadConfig] = []
        for group_id in (1, 2):
            payload = self._sn_read_payload(group_id)
            config = SNReadConfig(
                name=str(payload["name"]),
                db_number=int(payload["db_number"]),
                start=int(payload["start"]),
                data_type=str(payload["data_type"]),
                bit_index=int(payload["bit_index"]),
                enabled=bool(payload["enabled"]),
            )
            if config.data_type != "BOOL":
                config.bit_index = 0
                self.sn_bit_spins[group_id - 1].setValue(0)
            configs.append(config)
        self.sn_read_configs = configs

    @staticmethod
    def _normalize_theme_color(value: object, default: str) -> str:
        color = QtGui.QColor(str(value).strip()) if value is not None else QtGui.QColor()
        if color.isValid():
            return color.name()
        fallback = QtGui.QColor(default)
        return fallback.name() if fallback.isValid() else "#000000"

    @staticmethod
    def _contrast_text_color(color_value: str) -> str:
        color = QtGui.QColor(color_value)
        if not color.isValid():
            return "#ffffff"
        return "#11161c" if color.lightnessF() >= 0.6 else "#edf2f7"

    def _set_theme_color_button(self, button: QtWidgets.QPushButton, color_value: str) -> None:
        normalized = self._normalize_theme_color(color_value, "#000000")
        button.setProperty("theme_color", normalized)
        button.setText(normalized.upper())
        button.setStyleSheet(
            f"background:{normalized}; color:{self._contrast_text_color(normalized)}; border:1px solid {self.COLORS['border']}; border-radius:10px; padding:6px 10px; font-weight:700;"
        )

    def _theme_button_color(self, button: QtWidgets.QPushButton, fallback: str) -> str:
        return self._normalize_theme_color(button.property("theme_color"), fallback)

    def _sync_save_time_folder_state(self) -> None:
        enabled = bool(self.save_use_time_subfolder_checkbox.isChecked())
        self.save_time_folder_format_edit.setEnabled(enabled)

    def _sync_center_line_custom_state(self) -> None:
        mode = str(self.center_line_mode_combo.currentData() or "zero").strip().lower()
        self.center_line_custom_value_spin.setEnabled(mode == "custom_value")

    def _choose_save_root_dir(self) -> None:
        current_path = self._resolve_save_root_dir(self.save_root_dir_edit.text().strip() or self.save_settings.root_dir)
        selected = QtWidgets.QFileDialog.getExistingDirectory(self, "选择保存根目录", str(current_path))
        if selected:
            self.save_root_dir_edit.setText(selected)

    def _choose_theme_color(self, role: str) -> None:
        role_map = {
            "bg": ("总背景颜色", self.theme_bg_button, self.COLORS["bg"]),
            "panel": ("面板层颜色", self.theme_panel_button, self.COLORS["panel"]),
            "plot": ("图表背景颜色", self.theme_plot_button, self.COLORS["plot"]),
        }
        title, button, current_color = role_map[role]
        color = QtWidgets.QColorDialog.getColor(QtGui.QColor(current_color), self, title)
        if not color.isValid():
            return
        self._set_theme_color_button(button, color.name())

    def _refresh_theme_visuals(self) -> None:
        self._apply_theme_styles()
        self._set_theme_color_button(self.theme_bg_button, self.COLORS["bg"])
        self._set_theme_color_button(self.theme_panel_button, self.COLORS["panel"])
        self._set_theme_color_button(self.theme_plot_button, self.COLORS["plot"])
        self._update_connection_badge()
        self._update_sampling_badge(self.sampling_active)
        all_indices = set(range(self.MAX_WAVEFORMS))
        self.plot_dirty = True
        self.dirty_plot_indices.update(all_indices)
        self.plot_prepare_pending_indices.update(all_indices)
        self._enqueue_plot_prepare(all_indices)
        self._refresh_plots()

    def _apply_theme_config(self) -> None:
        bg_color = self._theme_button_color(self.theme_bg_button, self.COLORS["bg"])
        panel_color = self._theme_button_color(self.theme_panel_button, self.COLORS["panel"])
        plot_color = self._theme_button_color(self.theme_plot_button, self.COLORS["plot"])

        self.COLORS["bg"] = bg_color
        self.COLORS["panel"] = panel_color
        self.COLORS["panel_alt"] = panel_color
        self.COLORS["plot"] = plot_color

        self._set_theme_color_button(self.theme_bg_button, bg_color)
        self._set_theme_color_button(self.theme_panel_button, panel_color)
        self._set_theme_color_button(self.theme_plot_button, plot_color)
        self._refresh_theme_visuals()

        saved, error = self._save_local_config()
        if saved:
            self._set_status("主题颜色已更新。")
        else:
            self._set_status(f"主题颜色已更新，但保存失败：{error}")

    def _reset_theme_config(self) -> None:
        self.COLORS = dict(type(self).COLORS)
        self._refresh_theme_visuals()

        saved, error = self._save_local_config()
        if saved:
            self._set_status("主题已恢复为出厂颜色。")
        else:
            self._set_status(f"主题已恢复为出厂颜色，但保存失败：{error}")

    def _build_config_payload(self) -> Dict[str, object]:
        with self.channel_lock:
            channels = [
                {
                    "name": channel.name,
                    "target_label": channel.target_label,
                    "db_number": channel.db_number,
                    "start": channel.start,
                    "data_type": channel.data_type,
                    "bit_index": channel.bit_index,
                    "visible": channel.visible,
                    "upper_limit": channel.upper_limit,
                    "lower_limit": channel.lower_limit,
                    "upper_limit_color": channel.upper_limit_color,
                    "lower_limit_color": channel.lower_limit_color,
                    "upper_limit_db_number": channel.upper_limit_db_number,
                    "upper_limit_start": channel.upper_limit_start,
                    "upper_limit_data_type": channel.upper_limit_data_type,
                    "upper_limit_bit_index": channel.upper_limit_bit_index,
                    "upper_limit_read_enabled": channel.upper_limit_read_enabled,
                    "lower_limit_db_number": channel.lower_limit_db_number,
                    "lower_limit_start": channel.lower_limit_start,
                    "lower_limit_data_type": channel.lower_limit_data_type,
                    "lower_limit_bit_index": channel.lower_limit_bit_index,
                    "lower_limit_read_enabled": channel.lower_limit_read_enabled,
                    "baseline_db_number": channel.baseline_db_number,
                    "baseline_start": channel.baseline_start,
                    "baseline_data_type": channel.baseline_data_type,
                    "baseline_bit_index": channel.baseline_bit_index,
                    "baseline_read_enabled": channel.baseline_read_enabled,
                    "target_db_number": channel.target_db_number,
                    "target_start": channel.target_start,
                    "target_data_type": channel.target_data_type,
                    "target_bit_index": channel.target_bit_index,
                    "target_enabled": channel.target_enabled,
                }
                for channel in self.channels
            ]

        return {
            "version": 1,
            "plc": {
                "ip": self.ip_edit.text().strip(),
                "rack": int(self.rack_spin.value()),
                "slot": int(self.slot_spin.value()),
                "demo_mode": bool(self.demo_checkbox.isChecked()),
                "start_signal_1": self._signal_config_payload(1, "start"),
                "complete_signal_1": self._signal_config_payload(1, "complete"),
                "start_signal_2": self._signal_config_payload(2, "start"),
                "complete_signal_2": self._signal_config_payload(2, "complete"),
            },
            "sn_read_1": self._sn_read_payload(1),
            "sn_read_2": self._sn_read_payload(2),
            "heartbeat": {
                "name": self.heartbeat_config.name,
                "db_number": self.heartbeat_config.db_number,
                "start": self.heartbeat_config.start,
                "data_type": self.heartbeat_config.data_type,
                "bit_index": self.heartbeat_config.bit_index,
                "enabled": self.heartbeat_config.enabled,
                "high_value": self.heartbeat_config.high_value,
                "low_value": self.heartbeat_config.low_value,
                "high_interval_s": self.heartbeat_config.high_interval_s,
                "low_interval_s": self.heartbeat_config.low_interval_s,
            },
            "limit_display": {
                "exceed_curve_red": self.limit_exceed_red_enabled,
            },
            "scale_settings": {
                "y_tick_step": self.y_tick_step,
                "center_line_mode": self.center_line_mode,
                "center_line_custom_value": self.center_line_custom_value,
            },
            "save_settings": {
                "root_dir": self.save_settings.root_dir,
                "date_folder_format": self.save_settings.date_folder_format,
                "use_time_subfolder": self.save_settings.use_time_subfolder,
                "time_folder_format": self.save_settings.time_folder_format,
                "filename_pattern": self.save_settings.filename_pattern,
            },
            "theme": {
                "bg": self.COLORS["bg"],
                "panel": self.COLORS["panel"],
                "plot": self.COLORS["plot"],
            },
            "channels": channels,
        }

    def _save_local_config(self) -> Tuple[bool, Optional[str]]:
        try:
            payload = self._build_config_payload()
            self.config_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            return False, str(exc)
        return True, None

    def _load_local_config(self) -> Optional[str]:
        if not self.config_path.exists():
            return None

        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return f"本地配置读取失败，已使用默认参数：{exc}"

        plc_payload = payload.get("plc", {})
        if isinstance(plc_payload, dict):
            ip_address = plc_payload.get("ip")
            if isinstance(ip_address, str) and ip_address.strip():
                self.ip_edit.setText(ip_address.strip())
            self._set_spin_value(self.rack_spin, plc_payload.get("rack"))
            self._set_spin_value(self.slot_spin, plc_payload.get("slot"))
            self.demo_checkbox.setChecked(bool(plc_payload.get("demo_mode", False)))
            self._load_signal_config_to_ui(1, "start", plc_payload.get("start_signal_1", plc_payload.get("start_signal", {})))
            self._load_signal_config_to_ui(
                1,
                "complete",
                plc_payload.get("complete_signal_1", plc_payload.get("complete_signal", {})),
            )
            self._load_signal_config_to_ui(2, "start", plc_payload.get("start_signal_2", {}))
            self._load_signal_config_to_ui(2, "complete", plc_payload.get("complete_signal_2", {}))

        self._load_sn_read_config_to_ui(1, payload.get("sn_read_1", payload.get("sn_read", payload.get("ns_read", {}))))
        self._load_sn_read_config_to_ui(2, payload.get("sn_read_2", {}))

        heartbeat_payload = payload.get("heartbeat", {})
        if isinstance(heartbeat_payload, dict):
            heartbeat_name = heartbeat_payload.get("name")
            if isinstance(heartbeat_name, str) and heartbeat_name.strip():
                self.heartbeat_name_edit.setText(heartbeat_name.strip())
            self._set_spin_value(self.heartbeat_db_spin, heartbeat_payload.get("db_number"))
            self._set_spin_value(self.heartbeat_start_spin, heartbeat_payload.get("start"))
            heartbeat_type = str(heartbeat_payload.get("data_type", self.heartbeat_config.data_type)).strip().upper()
            if heartbeat_type not in SUPPORTED_DATA_TYPES:
                heartbeat_type = self.heartbeat_config.data_type
            self.heartbeat_type_combo.setCurrentText(heartbeat_type)
            self._set_spin_value(self.heartbeat_bit_spin, heartbeat_payload.get("bit_index"))
            high_value = heartbeat_payload.get("high_value")
            if high_value is not None:
                self.heartbeat_high_value_edit.setText(str(high_value))
            low_value = heartbeat_payload.get("low_value")
            if low_value is not None:
                self.heartbeat_low_value_edit.setText(str(low_value))
            self._set_double_spin_value(self.heartbeat_high_interval_spin, heartbeat_payload.get("high_interval_s"))
            self._set_double_spin_value(self.heartbeat_low_interval_spin, heartbeat_payload.get("low_interval_s"))
            self.heartbeat_enabled_checkbox.setChecked(bool(heartbeat_payload.get("enabled", False)))

        limit_display_payload = payload.get("limit_display", {})
        if isinstance(limit_display_payload, dict):
            self.limit_exceed_red_enabled = bool(limit_display_payload.get("exceed_curve_red", True))
        self.limit_exceed_red_checkbox.setChecked(self.limit_exceed_red_enabled)

        scale_settings_payload = payload.get("scale_settings", {})
        if isinstance(scale_settings_payload, dict):
            self.y_tick_step = max(0.1, self._coerce_float(scale_settings_payload.get("y_tick_step"), self.y_tick_step))
            center_line_mode = str(scale_settings_payload.get("center_line_mode", self.center_line_mode)).strip().lower()
            if center_line_mode in {"zero", "first_point", "plc_point", "custom_value"}:
                self.center_line_mode = center_line_mode
            self.center_line_custom_value = max(
                0.1,
                min(
                    10.0,
                    self._coerce_float(
                        scale_settings_payload.get("center_line_custom_value"),
                        self.center_line_custom_value,
                    ),
                ),
            )
        self._set_double_spin_value(self.y_tick_step_spin, self.y_tick_step)
        self._set_combo_data_value(self.center_line_mode_combo, self.center_line_mode)
        self._set_double_spin_value(self.center_line_custom_value_spin, self.center_line_custom_value)
        self._sync_center_line_custom_state()

        save_settings_payload = payload.get("save_settings", {})
        if isinstance(save_settings_payload, dict):
            root_dir = str(save_settings_payload.get("root_dir", self.save_settings.root_dir)).strip()
            filename_pattern = str(
                save_settings_payload.get("filename_pattern", self.save_settings.filename_pattern)
            ).strip()
            self.save_settings = SaveSettingsConfig(
                root_dir=root_dir or SaveSettingsConfig().root_dir,
                date_folder_format=str(
                    save_settings_payload.get("date_folder_format", self.save_settings.date_folder_format)
                ).strip(),
                use_time_subfolder=bool(
                    save_settings_payload.get("use_time_subfolder", self.save_settings.use_time_subfolder)
                ),
                time_folder_format=str(
                    save_settings_payload.get("time_folder_format", self.save_settings.time_folder_format)
                ).strip(),
                filename_pattern=filename_pattern or SaveSettingsConfig().filename_pattern,
            )
        self.save_root_dir_edit.setText(self.save_settings.root_dir)
        self.save_date_folder_format_edit.setText(self.save_settings.date_folder_format)
        self.save_use_time_subfolder_checkbox.setChecked(self.save_settings.use_time_subfolder)
        self.save_time_folder_format_edit.setText(self.save_settings.time_folder_format)
        self.save_filename_pattern_edit.setText(self.save_settings.filename_pattern)
        self._sync_save_time_folder_state()

        theme_payload = payload.get("theme", {})
        if isinstance(theme_payload, dict):
            bg_color = self._normalize_theme_color(theme_payload.get("bg"), self.COLORS["bg"])
            panel_color = self._normalize_theme_color(theme_payload.get("panel"), self.COLORS["panel"])
            plot_color = self._normalize_theme_color(theme_payload.get("plot"), self.COLORS["plot"])
            self.COLORS["bg"] = bg_color
            self.COLORS["panel"] = panel_color
            self.COLORS["panel_alt"] = panel_color
            self.COLORS["plot"] = plot_color
        self._set_theme_color_button(self.theme_bg_button, self.COLORS["bg"])
        self._set_theme_color_button(self.theme_panel_button, self.COLORS["panel"])
        self._set_theme_color_button(self.theme_plot_button, self.COLORS["plot"])
        self._apply_theme_styles()

        self._sync_signal_configs_from_ui()
        self._sync_sn_read_configs_from_ui()
        self._sync_sn_bit_state()
        self.heartbeat_config = HeartbeatConfig(
            name=self.heartbeat_name_edit.text().strip() or "心跳",
            db_number=int(self.heartbeat_db_spin.value()),
            start=int(self.heartbeat_start_spin.value()),
            data_type=self.heartbeat_type_combo.currentText().strip().upper(),
            bit_index=int(self.heartbeat_bit_spin.value()),
            enabled=bool(self.heartbeat_enabled_checkbox.isChecked()),
            high_value=self.heartbeat_high_value_edit.text().strip() or "1",
            low_value=self.heartbeat_low_value_edit.text().strip() or "0",
            high_interval_s=float(self.heartbeat_high_interval_spin.value()),
            low_interval_s=float(self.heartbeat_low_interval_spin.value()),
        )
        self._sync_heartbeat_bit_state()
        self.limit_exceed_red_enabled = bool(self.limit_exceed_red_checkbox.isChecked())

        loaded_channels = self._create_default_channels()
        channel_payload = payload.get("channels", [])
        if isinstance(channel_payload, list):
            for index, item in enumerate(channel_payload[: self.MAX_WAVEFORMS]):
                if not isinstance(item, dict):
                    continue

                default_channel = loaded_channels[index]
                name = item.get("name")
                if not isinstance(name, str) or not name.strip():
                    name = default_channel.name

                data_type = str(item.get("data_type", default_channel.data_type)).strip().upper()
                if data_type not in SUPPORTED_DATA_TYPES:
                    data_type = default_channel.data_type

                candidate = WaveformChannel(
                    name=name.strip(),
                    color=default_channel.color,
                    target_label=str(item.get("target_label", default_channel.target_label)).strip() or default_channel.target_label,
                    db_number=max(1, self._coerce_int(item.get("db_number"), default_channel.db_number)),
                    start=max(0, self._coerce_int(item.get("start"), default_channel.start)),
                    data_type=data_type,
                    bit_index=self._coerce_int(item.get("bit_index"), default_channel.bit_index),
                    visible=bool(item.get("visible", default_channel.visible)),
                    upper_limit=self._coerce_float(item.get("upper_limit"), default_channel.upper_limit),
                    lower_limit=self._coerce_float(item.get("lower_limit"), default_channel.lower_limit),
                    upper_limit_color=str(
                        item.get("upper_limit_color", item.get("limit_color", default_channel.upper_limit_color))
                        or default_channel.upper_limit_color
                    ),
                    lower_limit_color=str(
                        item.get("lower_limit_color", item.get("limit_color", default_channel.lower_limit_color))
                        or default_channel.lower_limit_color
                    ),
                    upper_limit_db_number=max(
                        1, self._coerce_int(item.get("upper_limit_db_number"), default_channel.upper_limit_db_number)
                    ),
                    upper_limit_start=max(0, self._coerce_int(item.get("upper_limit_start"), default_channel.upper_limit_start)),
                    upper_limit_data_type=str(item.get("upper_limit_data_type", default_channel.upper_limit_data_type)).strip().upper(),
                    upper_limit_bit_index=self._coerce_int(item.get("upper_limit_bit_index"), default_channel.upper_limit_bit_index),
                    upper_limit_read_enabled=bool(item.get("upper_limit_read_enabled", default_channel.upper_limit_read_enabled)),
                    lower_limit_db_number=max(
                        1, self._coerce_int(item.get("lower_limit_db_number"), default_channel.lower_limit_db_number)
                    ),
                    lower_limit_start=max(0, self._coerce_int(item.get("lower_limit_start"), default_channel.lower_limit_start)),
                    lower_limit_data_type=str(item.get("lower_limit_data_type", default_channel.lower_limit_data_type)).strip().upper(),
                    lower_limit_bit_index=self._coerce_int(item.get("lower_limit_bit_index"), default_channel.lower_limit_bit_index),
                    lower_limit_read_enabled=bool(item.get("lower_limit_read_enabled", default_channel.lower_limit_read_enabled)),
                    baseline_db_number=max(1, self._coerce_int(item.get("baseline_db_number"), default_channel.baseline_db_number)),
                    baseline_start=max(0, self._coerce_int(item.get("baseline_start"), default_channel.baseline_start)),
                    baseline_data_type=str(item.get("baseline_data_type", default_channel.baseline_data_type)).strip().upper(),
                    baseline_bit_index=self._coerce_int(item.get("baseline_bit_index"), default_channel.baseline_bit_index),
                    baseline_read_enabled=bool(item.get("baseline_read_enabled", default_channel.baseline_read_enabled)),
                    target_db_number=max(1, self._coerce_int(item.get("target_db_number"), default_channel.target_db_number)),
                    target_start=max(0, self._coerce_int(item.get("target_start"), default_channel.target_start)),
                    target_data_type=str(item.get("target_data_type", default_channel.target_data_type)).strip().upper(),
                    target_bit_index=self._coerce_int(item.get("target_bit_index"), default_channel.target_bit_index),
                    target_enabled=bool(item.get("target_enabled", default_channel.target_enabled)),
                )
                if candidate.data_type != "BOOL":
                    candidate.bit_index = 0
                if candidate.upper_limit_data_type not in SUPPORTED_DATA_TYPES:
                    candidate.upper_limit_data_type = default_channel.upper_limit_data_type
                if candidate.upper_limit_data_type != "BOOL":
                    candidate.upper_limit_bit_index = 0
                if candidate.lower_limit_data_type not in SUPPORTED_DATA_TYPES:
                    candidate.lower_limit_data_type = default_channel.lower_limit_data_type
                if candidate.lower_limit_data_type != "BOOL":
                    candidate.lower_limit_bit_index = 0
                if candidate.baseline_data_type not in SUPPORTED_DATA_TYPES:
                    candidate.baseline_data_type = default_channel.baseline_data_type
                if candidate.baseline_data_type != "BOOL":
                    candidate.baseline_bit_index = 0
                if candidate.target_data_type not in SUPPORTED_DATA_TYPES:
                    candidate.target_data_type = default_channel.target_data_type
                if candidate.target_data_type != "BOOL":
                    candidate.target_bit_index = 0
                if candidate.lower_limit >= candidate.upper_limit:
                    candidate.upper_limit = default_channel.upper_limit
                    candidate.lower_limit = default_channel.lower_limit

                try:
                    candidate.to_variable().validate()
                except Exception:
                    continue

                loaded_channels[index] = candidate

        with self.channel_lock:
            self.channels = loaded_channels

        return f"已加载本地配置：{self.config_path.name}"

    def _set_status(self, message: str) -> None:
        self.status_label.setText(message)
        self.statusBar().showMessage(message)

    def _snapshot_channels_for_prepare(self, indices: List[int]) -> Dict[int, Dict[str, object]]:
        with self.channel_lock:
            return {
                index: {
                    "name": channel.name,
                    "address": channel.address_text(),
                    "times": list(channel.times),
                    "values": list(channel.values),
                    "visible": channel.visible,
                    "color": channel.color,
                    "latest_value": channel.latest_value,
                    "target_label": channel.target_label,
                    "target_value_text": channel.target_value_text,
                    "upper_limit": channel.upper_limit,
                    "lower_limit": channel.lower_limit,
                    "upper_limit_color": channel.upper_limit_color,
                    "lower_limit_color": channel.lower_limit_color,
                    "highlight_limit_exceeded": self.limit_exceed_red_enabled,
                    "y_tick_step": self.y_tick_step,
                    "center_line_mode": self.center_line_mode,
                    "center_line_custom_value": self.center_line_custom_value,
                    "baseline_plc_value": channel.baseline_value,
                }
                for index, channel in enumerate(self.channels)
                if index in indices
            }

    def _enqueue_plot_prepare(self, indices) -> None:
        self.plot_prepare_pending_indices.update(indices)
        if self.window_interacting:
            return
        if self.plot_prepare_future is not None and not self.plot_prepare_future.done():
            return

        pending_indices = sorted(self.plot_prepare_pending_indices)
        if not pending_indices:
            return

        snapshots = self._snapshot_channels_for_prepare(pending_indices)
        self.plot_prepare_pending_indices.clear()
        self.plot_prepare_future = self.plot_prepare_executor.submit(
            prepare_plot_payloads,
            snapshots,
            PlotCard.DISPLAY_POINT_LIMIT,
        )
        self.plot_prepare_future.add_done_callback(self._on_plot_prepare_done)

    def _on_plot_prepare_done(self, future: Future) -> None:
        try:
            prepared = future.result()
        except Exception:
            prepared = {}
        self.plot_prepare_queue.put(prepared)
        self.plot_prepare_future = None

    def _drain_prepared_plot_queue(self) -> None:
        changed = False
        while True:
            try:
                prepared = self.plot_prepare_queue.get_nowait()
            except queue.Empty:
                break
            if prepared:
                self.prepared_plot_payloads.update(prepared)
                changed = True
        if changed:
            self.plot_dirty = True

    def _queue_auto_save_request(
        self,
        sn: str,
        channel_indices: Iterable[int],
        group_id: Optional[int] = None,
        save_timestamp: Optional[float] = None,
    ) -> None:
        self.auto_save_request_seq += 1
        self.pending_auto_save_requests.append(
            AutoSaveRequest(
                request_id=self.auto_save_request_seq,
                sn=sn,
                channel_indices=tuple(int(index) for index in channel_indices),
                group_id=group_id,
                save_timestamp=save_timestamp,
            )
        )

    def _can_run_pending_auto_save(self) -> bool:
        if not self.pending_auto_save_requests:
            return False
        if self.window_interacting or self.auto_save_in_progress:
            return False
        if self.plot_dirty or self.prepared_plot_payloads or self.plot_prepare_pending_indices:
            return False
        if self.plot_prepare_future is not None and not self.plot_prepare_future.done():
            return False
        return True

    def _schedule_pending_auto_save(self) -> None:
        if self.pending_auto_save_scheduled or not self._can_run_pending_auto_save():
            return
        self.pending_auto_save_scheduled = True
        QtCore.QTimer.singleShot(0, self._perform_pending_auto_save)

    def _perform_pending_auto_save(self) -> None:
        self.pending_auto_save_scheduled = False
        if not self._can_run_pending_auto_save():
            if self.pending_auto_save_requests and not self.window_interacting:
                self.pending_auto_save_scheduled = True
                QtCore.QTimer.singleShot(120, self._perform_pending_auto_save)
            return

        if not self.pending_auto_save_requests:
            return

        request = self.pending_auto_save_requests[0]
        self.auto_save_in_progress = True
        output_path = self._build_report_pdf_path(request.sn, request.group_id, request.save_timestamp)
        try:
            self._save_report_pdf(output_path, request.sn, list(request.channel_indices))
        except Exception as exc:
            self._set_status(f"自动保存 PDF 失败：{exc}")
        else:
            self._set_status(
                f"{self._group_display_name(request.group_id)}收到完成信号后已自动保存 PDF：{output_path}"
            )
        finally:
            if self.pending_auto_save_requests and self.pending_auto_save_requests[0].request_id == request.request_id:
                self.pending_auto_save_requests.popleft()
            self.auto_save_in_progress = False
            self._schedule_pending_auto_save()

    def _begin_window_interaction(self) -> None:
        if self.window_interacting:
            return
        self.window_interacting = True
        self.plot_timer.stop()
        self.table_timer.stop()
        self.sample_timer.stop()
        self.centralWidget().setUpdatesEnabled(False)
        self.plot_scroll.viewport().setUpdatesEnabled(False)
        self.channel_table.setUpdatesEnabled(False)

    def _end_window_interaction(self) -> None:
        if not self.window_interacting:
            return
        self.window_interacting = False
        self.centralWidget().setUpdatesEnabled(True)
        self.plot_scroll.viewport().setUpdatesEnabled(True)
        self.channel_table.setUpdatesEnabled(True)
        self.sample_timer.start(80)
        self.table_timer.start(250)
        self.plot_timer.start(280)
        self._process_samples()
        self._enqueue_plot_prepare(self.plot_prepare_pending_indices)
        self._refresh_table_if_needed()
        self._refresh_plots()

    def _schedule_interaction_settle(self) -> None:
        self._begin_window_interaction()
        self.interaction_settle_timer.start(180)

    def _refresh_plot_cards(self) -> None:
        all_indices = set(range(self.MAX_WAVEFORMS))
        self.plot_prepare_pending_indices.update(all_indices)
        self.dirty_plot_indices.update(all_indices)
        self._enqueue_plot_prepare(all_indices)
        self._refresh_plots()

    def _refresh_channel_table(self) -> None:
        self.channel_table.blockSignals(True)
        self.channel_table.setRowCount(len(self.channels))
        for row, channel in enumerate(self.channels):
            row_values = [
                channel.name,
                channel.address_text(),
                channel.data_type,
                "显示" if channel.visible else "隐藏",
                f"{channel.latest_value:.3f}",
            ]
            for column, text in enumerate(row_values):
                item = self.channel_table.item(row, column)
                if item is None:
                    item = QtWidgets.QTableWidgetItem(text)
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemIsEditable)
                    self.channel_table.setItem(row, column, item)
                elif item.text() != text:
                    item.setText(text)
        self.channel_table.blockSignals(False)
        self.table_dirty = False

    def _is_sidebar_collapsed(self) -> bool:
        sizes = self.main_splitter.sizes()
        return (not self.sidebar.isVisible()) or bool(sizes and sizes[0] <= 24)

    def _update_sidebar_toggle_button(self) -> None:
        if self._is_sidebar_collapsed():
            self.sidebar_toggle_button.setText("设置")
            self.sidebar_toggle_button.setToolTip("展开左侧设置菜单")
        else:
            self.sidebar_toggle_button.setText("收起设置")
            self.sidebar_toggle_button.setToolTip("收起左侧设置菜单")

    def _handle_splitter_moved(self, _pos: int, _index: int) -> None:
        sizes = self.main_splitter.sizes()
        if sizes and sizes[0] > 60:
            self.last_sidebar_width = sizes[0]
            if not self.sidebar.isVisible():
                self.sidebar.show()
        elif sizes and sizes[0] <= 24 and self.sidebar.isVisible():
            self.sidebar.hide()
        self._update_sidebar_toggle_button()

    def _preferred_sidebar_width(self, total_width: int) -> int:
        scrollbar_width = self.sidebar_scroll.verticalScrollBar().sizeHint().width()
        content_width = max(
            self.last_sidebar_width,
            self.sidebar.sizeHint().width(),
            self.sidebar_scroll.sizeHint().width() + 12,
            self.sidebar_content.sizeHint().width() + scrollbar_width + 16,
            420,
        )
        max_width = min(self.sidebar.maximumWidth(), max(320, total_width - 360))
        return max(320, min(content_width, max_width))

    def _toggle_sidebar(self) -> None:
        sizes = self.main_splitter.sizes()
        total_width = max(1, sum(sizes), self.main_splitter.width())
        if self._is_sidebar_collapsed():
            self.sidebar.show()
            self.sidebar_content.adjustSize()
            target_width = self._preferred_sidebar_width(total_width)
            self.last_sidebar_width = target_width
            self.main_splitter.setSizes([target_width, max(1, total_width - target_width)])
        else:
            if sizes and sizes[0] > 60:
                self.last_sidebar_width = sizes[0]
            self.main_splitter.setSizes([0, total_width])
            self.sidebar.hide()
        self._update_sidebar_toggle_button()

    def _refresh_table_if_needed(self) -> None:
        if self.table_dirty:
            self._refresh_channel_table()

    def _rebuild_channel_menu(self) -> None:
        if self.channel_menu is None:
            return
        self.channel_menu.clear()
        for index, channel in enumerate(self.channels):
            action = self.channel_menu.addAction(channel.name)
            action.triggered.connect(lambda _checked=False, row=index: self._select_channel(row))

    def _handle_channel_table_selection(self) -> None:
        row = self.channel_table.currentRow()
        if row >= 0:
            self._select_channel(row, update_table=False)

    def _select_channel(self, index: int, update_table: bool = True) -> None:
        if not (0 <= index < len(self.channels)):
            return
        self.selected_channel_index = index
        channel = self.channels[index]
        self._sync_editor_to_channel(channel)
        for row, card in enumerate(self.plot_cards):
            card.set_selected(row == index)

        if update_table:
            self.channel_table.blockSignals(True)
            self.channel_table.selectRow(index)
            self.channel_table.setCurrentCell(index, 0)
            self.channel_table.blockSignals(False)

    def _sync_editor_to_channel(self, channel: WaveformChannel) -> None:
        self.name_edit.setText(channel.name)
        self.db_spin.setValue(channel.db_number)
        self.start_spin.setValue(channel.start)
        self.type_combo.setCurrentText(channel.data_type)
        self.bit_spin.setValue(channel.bit_index)
        self.visible_checkbox.setChecked(channel.visible)
        self._sync_bit_state()
        self._sync_target_editor_to_channel(channel)

    def _sync_target_editor_to_channel(self, channel: WaveformChannel) -> None:
        self.target_channel_label.setText(channel.name)
        self.target_label_edit.setText(channel.target_label)
        self.target_db_spin.setValue(channel.target_db_number)
        self.target_start_spin.setValue(channel.target_start)
        self.target_type_combo.setCurrentText(channel.target_data_type)
        self.target_bit_spin.setValue(channel.target_bit_index)
        self.target_enabled_checkbox.setChecked(channel.target_enabled)
        self._sync_target_bit_state()

        self.limit_channel_label.setText(channel.name)
        self.limit_upper_db_spin.setValue(channel.upper_limit_db_number)
        self.limit_upper_start_spin.setValue(channel.upper_limit_start)
        self.limit_upper_type_combo.setCurrentText(channel.upper_limit_data_type)
        self.limit_upper_bit_spin.setValue(channel.upper_limit_bit_index)
        self.limit_upper_enabled_checkbox.setChecked(channel.upper_limit_read_enabled)
        self.limit_lower_db_spin.setValue(channel.lower_limit_db_number)
        self.limit_lower_start_spin.setValue(channel.lower_limit_start)
        self.limit_lower_type_combo.setCurrentText(channel.lower_limit_data_type)
        self.limit_lower_bit_spin.setValue(channel.lower_limit_bit_index)
        self.limit_lower_enabled_checkbox.setChecked(channel.lower_limit_read_enabled)
        self.limit_baseline_db_spin.setValue(channel.baseline_db_number)
        self.limit_baseline_start_spin.setValue(channel.baseline_start)
        self.limit_baseline_type_combo.setCurrentText(channel.baseline_data_type)
        self.limit_baseline_bit_spin.setValue(channel.baseline_bit_index)
        self.limit_baseline_enabled_checkbox.setChecked(channel.baseline_read_enabled)
        self._sync_limit_read_bit_state()

    def _sync_target_bit_state(self) -> None:
        is_bool = self.target_type_combo.currentText().upper() == "BOOL"
        self.target_bit_spin.setEnabled(is_bool)
        if not is_bool:
            self.target_bit_spin.setValue(0)

    def _sync_limit_read_bit_state(self) -> None:
        upper_is_bool = self.limit_upper_type_combo.currentText().upper() == "BOOL"
        self.limit_upper_bit_spin.setEnabled(upper_is_bool)
        if not upper_is_bool:
            self.limit_upper_bit_spin.setValue(0)

        lower_is_bool = self.limit_lower_type_combo.currentText().upper() == "BOOL"
        self.limit_lower_bit_spin.setEnabled(lower_is_bool)
        if not lower_is_bool:
            self.limit_lower_bit_spin.setValue(0)

        baseline_is_bool = self.limit_baseline_type_combo.currentText().upper() == "BOOL"
        self.limit_baseline_bit_spin.setEnabled(baseline_is_bool)
        if not baseline_is_bool:
            self.limit_baseline_bit_spin.setValue(0)

    def _apply_target_value_config(self) -> None:
        if not (0 <= self.selected_channel_index < len(self.channels)):
            return

        index = self.selected_channel_index
        channel_name = self.channels[index].name
        target_label = self.target_label_edit.text().strip() or "指定值"
        try:
            config = PLCVariable(
                name=f"{channel_name}_target",
                db_number=int(self.target_db_spin.value()),
                start=int(self.target_start_spin.value()),
                data_type=self.target_type_combo.currentText().strip().upper(),
                bit_index=int(self.target_bit_spin.value()),
                enabled=bool(self.target_enabled_checkbox.isChecked()),
            )
            if config.enabled:
                config.validate()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "配置错误", str(exc))
            return

        if config.normalized_type() != "BOOL":
            config = PLCVariable(
                name=config.name,
                db_number=config.db_number,
                start=config.start,
                data_type=config.data_type,
                bit_index=0,
                enabled=config.enabled,
            )
            self.target_bit_spin.setValue(0)

        with self.channel_lock:
            channel = self.channels[index]
            channel.target_label = target_label
            channel.target_db_number = config.db_number
            channel.target_start = config.start
            channel.target_data_type = config.data_type
            channel.target_bit_index = config.bit_index
            channel.target_enabled = config.enabled
            channel.target_value_text = "--"

        self.plot_dirty = True
        self.dirty_plot_indices.add(index)
        self.plot_prepare_pending_indices.add(index)
        self._enqueue_plot_prepare({index})

        saved, error = self._save_local_config()
        if config.enabled:
            base_message = f"{channel_name} 指定值读取已更新。"
        else:
            base_message = f"{channel_name} 指定值读取已关闭。"
        if saved:
            self._set_status(base_message)
        else:
            self._set_status(f"{base_message} 但保存失败：{error}")

    def _sync_sn_bit_state(self, group_id: Optional[int] = None) -> None:
        indices = range(2) if group_id is None else [max(0, min(1, int(group_id) - 1))]
        for index in indices:
            is_bool = self.sn_type_combos[index].currentText().upper() == "BOOL"
            self.sn_bit_spins[index].setEnabled(is_bool)
            if not is_bool:
                self.sn_bit_spins[index].setValue(0)

    def _apply_sn_read_config(self) -> None:
        try:
            self._sync_sn_bit_state()
            configs: List[SNReadConfig] = []
            for group_id in (1, 2):
                payload = self._sn_read_payload(group_id)
                config = SNReadConfig(
                    name=str(payload["name"]),
                    db_number=int(payload["db_number"]),
                    start=int(payload["start"]),
                    data_type=str(payload["data_type"]),
                    bit_index=int(payload["bit_index"]),
                    enabled=bool(payload["enabled"]),
                )
                config.to_variable().validate()
                configs.append(config)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "配置错误", str(exc))
            return

        self.sn_read_configs = configs
        saved, error = self._save_local_config()
        if saved:
            self._set_status("已更新编号读取选项。")
        else:
            self._set_status(f"已更新编号读取选项，但保存失败：{error}")

    def _sync_heartbeat_bit_state(self) -> None:
        is_bool = self.heartbeat_type_combo.currentText().upper() == "BOOL"
        self.heartbeat_bit_spin.setEnabled(is_bool)
        if not is_bool:
            self.heartbeat_bit_spin.setValue(0)

    @staticmethod
    def _parse_heartbeat_value(value_text: str, data_type: str):
        text = str(value_text).strip()
        normalized_type = data_type.strip().upper()
        if not text:
            raise ValueError("心跳发送值不能为空。")

        if normalized_type == "BOOL":
            lowered = text.lower()
            if lowered in {"1", "true", "on", "yes"}:
                return True
            if lowered in {"0", "false", "off", "no"}:
                return False
            raise ValueError("BOOL 心跳值只能填写 1/0 或 true/false。")

        if normalized_type == "BYTE":
            value = int(text)
            if not 0 <= value <= 255:
                raise ValueError("BYTE 心跳值必须在 0 到 255 之间。")
            return value

        if normalized_type == "INT":
            value = int(text)
            if not -32768 <= value <= 32767:
                raise ValueError("INT 心跳值必须在 -32768 到 32767 之间。")
            return value

        if normalized_type == "DINT":
            value = int(text)
            if not -2147483648 <= value <= 2147483647:
                raise ValueError("DINT 心跳值必须在 32 位整数范围内。")
            return value

        if normalized_type == "WORD":
            value = int(text)
            if not 0 <= value <= 65535:
                raise ValueError("WORD 心跳值必须在 0 到 65535 之间。")
            return value

        if normalized_type == "DWORD":
            value = int(text)
            if not 0 <= value <= 4294967295:
                raise ValueError("DWORD 心跳值必须在 0 到 4294967295 之间。")
            return value

        if normalized_type == "REAL":
            return float(text)

        if normalized_type == "S7STRING":
            return text

        raise ValueError(f"不支持的心跳数据类型：{data_type}")

    def _apply_heartbeat_config(self) -> None:
        try:
            config = HeartbeatConfig(
                name=self.heartbeat_name_edit.text().strip() or "心跳",
                db_number=int(self.heartbeat_db_spin.value()),
                start=int(self.heartbeat_start_spin.value()),
                data_type=self.heartbeat_type_combo.currentText().strip().upper(),
                bit_index=int(self.heartbeat_bit_spin.value()),
                enabled=bool(self.heartbeat_enabled_checkbox.isChecked()),
                high_value=self.heartbeat_high_value_edit.text().strip() or "1",
                low_value=self.heartbeat_low_value_edit.text().strip() or "0",
                high_interval_s=float(self.heartbeat_high_interval_spin.value()),
                low_interval_s=float(self.heartbeat_low_interval_spin.value()),
            )
            config.to_variable().validate()
            self._parse_heartbeat_value(config.high_value, config.data_type)
            self._parse_heartbeat_value(config.low_value, config.data_type)
            if config.high_interval_s <= 0 or config.low_interval_s <= 0:
                raise ValueError("心跳间隔必须大于 0 秒。")
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "配置错误", str(exc))
            return

        if config.data_type != "BOOL":
            config.bit_index = 0
            self.heartbeat_bit_spin.setValue(0)

        self.heartbeat_config = config
        if self.plc_client is not None and self.plc_client.is_connected:
            self._restart_heartbeat()
        else:
            self._stop_heartbeat()

        saved, error = self._save_local_config()
        if config.enabled:
            base_message = f"已更新心跳设置：{config.address_text()}，发送 {config.high_value}/{config.low_value}。"
        else:
            base_message = "已更新心跳设置，并关闭心跳输出。"
        if saved:
            if config.enabled and (self.plc_client is None or not self.plc_client.is_connected):
                base_message = f"{base_message} 连接后会自动开始。"
            self._set_status(base_message)
        else:
            self._set_status(f"{base_message} 但保存失败：{error}")

    def _apply_limit_display_config(self) -> None:
        self.limit_exceed_red_enabled = bool(self.limit_exceed_red_checkbox.isChecked())
        if not (0 <= self.selected_channel_index < len(self.channels)):
            return

        channel_name = self.channels[self.selected_channel_index].name
        try:
            upper_variable = PLCVariable(
                name=f"{channel_name}_upper_limit",
                db_number=int(self.limit_upper_db_spin.value()),
                start=int(self.limit_upper_start_spin.value()),
                data_type=self.limit_upper_type_combo.currentText().strip().upper(),
                bit_index=int(self.limit_upper_bit_spin.value()),
                enabled=bool(self.limit_upper_enabled_checkbox.isChecked()),
            )
            lower_variable = PLCVariable(
                name=f"{channel_name}_lower_limit",
                db_number=int(self.limit_lower_db_spin.value()),
                start=int(self.limit_lower_start_spin.value()),
                data_type=self.limit_lower_type_combo.currentText().strip().upper(),
                bit_index=int(self.limit_lower_bit_spin.value()),
                enabled=bool(self.limit_lower_enabled_checkbox.isChecked()),
            )
            baseline_variable = PLCVariable(
                name=f"{channel_name}_baseline",
                db_number=int(self.limit_baseline_db_spin.value()),
                start=int(self.limit_baseline_start_spin.value()),
                data_type=self.limit_baseline_type_combo.currentText().strip().upper(),
                bit_index=int(self.limit_baseline_bit_spin.value()),
                enabled=bool(self.limit_baseline_enabled_checkbox.isChecked()),
            )
            if upper_variable.enabled:
                upper_variable.validate()
            if lower_variable.enabled:
                lower_variable.validate()
            if baseline_variable.enabled:
                baseline_variable.validate()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "配置错误", str(exc))
            return

        if upper_variable.normalized_type() != "BOOL":
            upper_variable = PLCVariable(
                name=upper_variable.name,
                db_number=upper_variable.db_number,
                start=upper_variable.start,
                data_type=upper_variable.data_type,
                bit_index=0,
                enabled=upper_variable.enabled,
            )
            self.limit_upper_bit_spin.setValue(0)

        if lower_variable.normalized_type() != "BOOL":
            lower_variable = PLCVariable(
                name=lower_variable.name,
                db_number=lower_variable.db_number,
                start=lower_variable.start,
                data_type=lower_variable.data_type,
                bit_index=0,
                enabled=lower_variable.enabled,
            )
            self.limit_lower_bit_spin.setValue(0)

        if baseline_variable.normalized_type() != "BOOL":
            baseline_variable = PLCVariable(
                name=baseline_variable.name,
                db_number=baseline_variable.db_number,
                start=baseline_variable.start,
                data_type=baseline_variable.data_type,
                bit_index=0,
                enabled=baseline_variable.enabled,
            )
            self.limit_baseline_bit_spin.setValue(0)

        with self.channel_lock:
            channel = self.channels[self.selected_channel_index]
            channel.upper_limit_db_number = upper_variable.db_number
            channel.upper_limit_start = upper_variable.start
            channel.upper_limit_data_type = upper_variable.data_type
            channel.upper_limit_bit_index = upper_variable.bit_index
            channel.upper_limit_read_enabled = upper_variable.enabled
            channel.lower_limit_db_number = lower_variable.db_number
            channel.lower_limit_start = lower_variable.start
            channel.lower_limit_data_type = lower_variable.data_type
            channel.lower_limit_bit_index = lower_variable.bit_index
            channel.lower_limit_read_enabled = lower_variable.enabled
            channel.baseline_db_number = baseline_variable.db_number
            channel.baseline_start = baseline_variable.start
            channel.baseline_data_type = baseline_variable.data_type
            channel.baseline_bit_index = baseline_variable.bit_index
            channel.baseline_read_enabled = baseline_variable.enabled
            channel.baseline_value = None

        all_indices = set(range(self.MAX_WAVEFORMS))
        self.plot_dirty = True
        self.dirty_plot_indices.update(all_indices)
        self.plot_prepare_pending_indices.update(all_indices)
        self._enqueue_plot_prepare(all_indices)

        saved, error = self._save_local_config()
        limit_mode_text = "已启用超出上下限时曲线变红。" if self.limit_exceed_red_enabled else "已关闭超出上下限时曲线变红。"
        read_mode_text = (
            f"{channel_name} 的上下限/基准点 PLC 读取设置已更新。"
            if (upper_variable.enabled or lower_variable.enabled or baseline_variable.enabled)
            else f"{channel_name} 的上下限/基准点 PLC 读取已关闭。"
        )
        base_message = f"{limit_mode_text} {read_mode_text}"
        if saved:
            self._set_status(base_message)
        else:
            self._set_status(f"{base_message} 但保存失败：{error}")

    def _apply_scale_settings(self) -> None:
        self.y_tick_step = max(0.1, float(self.y_tick_step_spin.value()))
        center_line_mode = str(self.center_line_mode_combo.currentData() or "zero").strip().lower()
        if center_line_mode not in {"zero", "first_point", "plc_point", "custom_value"}:
            center_line_mode = "zero"
        self.center_line_mode = center_line_mode
        self.center_line_custom_value = max(0.1, min(10.0, float(self.center_line_custom_value_spin.value())))
        self._sync_center_line_custom_state()

        all_indices = set(range(self.MAX_WAVEFORMS))
        self.plot_dirty = True
        self.dirty_plot_indices.update(all_indices)
        self.plot_prepare_pending_indices.update(all_indices)
        self._enqueue_plot_prepare(all_indices)

        saved, error = self._save_local_config()
        if self.center_line_mode == "zero":
            mode_text = "0点绘制"
        elif self.center_line_mode == "first_point":
            mode_text = "根据第一个点绘制"
        elif self.center_line_mode == "custom_value":
            mode_text = f"根据指定值绘制({self.center_line_custom_value:.1f})"
        else:
            mode_text = "根据PLC基准点绘制"
        base_message = f"刻度值设置已更新，纵轴间隔 {self.y_tick_step:g}，中心线基准：{mode_text}。"
        if saved:
            self._set_status(base_message)
        else:
            self._set_status(f"{base_message} 但保存失败：{error}")

    def _apply_save_settings(self) -> None:
        default_settings = SaveSettingsConfig()
        self.save_settings = SaveSettingsConfig(
            root_dir=self.save_root_dir_edit.text().strip() or default_settings.root_dir,
            date_folder_format=self.save_date_folder_format_edit.text().strip(),
            use_time_subfolder=bool(self.save_use_time_subfolder_checkbox.isChecked()),
            time_folder_format=self.save_time_folder_format_edit.text().strip(),
            filename_pattern=self.save_filename_pattern_edit.text().strip() or default_settings.filename_pattern,
        )
        self.save_root_dir_edit.setText(self.save_settings.root_dir)
        self.save_date_folder_format_edit.setText(self.save_settings.date_folder_format)
        self.save_time_folder_format_edit.setText(self.save_settings.time_folder_format)
        self.save_filename_pattern_edit.setText(self.save_settings.filename_pattern)
        self._sync_save_time_folder_state()

        saved, error = self._save_local_config()
        folder_mode_text = "日期目录下" if not self.save_settings.use_time_subfolder else "日期/时间目录下"
        base_message = (
            f"保存设置已更新，PDF 将保存到 {folder_mode_text}，文件名格式：{self.save_settings.filename_pattern}"
        )
        if saved:
            self._set_status(base_message)
        else:
            self._set_status(f"{base_message}，但保存失败：{error}")

    def _sync_signal_configs_from_ui(self) -> None:
        self.start_signal_configs = [
            PLCSignalConfig(
                db_number=int(self.start_signal_db_spins[index].value()),
                start=int(self.start_signal_byte_spins[index].value()),
                bit_index=int(self.start_signal_bit_spins[index].value()),
            )
            for index in range(2)
        ]
        self.complete_signal_configs = [
            PLCSignalConfig(
                db_number=int(self.complete_signal_db_spins[index].value()),
                start=int(self.complete_signal_byte_spins[index].value()),
                bit_index=int(self.complete_signal_bit_spins[index].value()),
            )
            for index in range(2)
        ]
        self.reset_start_signal_enabled = [
            bool(self.start_signal_reset_checkboxes[index].isChecked())
            for index in range(2)
        ]
        self.reset_complete_signal_enabled = [
            bool(self.complete_signal_reset_checkboxes[index].isChecked())
            for index in range(2)
        ]

    @staticmethod
    def _group_channel_indices(group_id: Optional[int]) -> Tuple[int, ...]:
        if group_id in SIGNAL_GROUP_CHANNELS:
            return SIGNAL_GROUP_CHANNELS[int(group_id)]
        return tuple(range(8))

    @staticmethod
    def _group_display_name(group_id: Optional[int]) -> str:
        if group_id == 1:
            return "A组"
        if group_id == 2:
            return "B组"
        return "全部通道"

    @staticmethod
    def _group_id_from_channel_index(index: int) -> int:
        return 1 if int(index) < 4 else 2

    def _stop_heartbeat(self) -> None:
        self.heartbeat_stop_event.set()
        thread = self.heartbeat_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self.heartbeat_thread = None
        self.heartbeat_stop_event.clear()

    def _restart_heartbeat(self) -> None:
        self._stop_heartbeat()
        config = self.heartbeat_config
        client = self.plc_client
        if not config.enabled or client is None or not client.is_connected:
            return

        self.heartbeat_stop_event.clear()
        self.heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            daemon=True,
            name="plc-heartbeat",
        )
        self.heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        send_high = True
        while not self.heartbeat_stop_event.is_set():
            config = self.heartbeat_config
            client = self.plc_client
            if not config.enabled or client is None or not client.is_connected:
                return

            variable = config.to_variable()
            try:
                if send_high:
                    write_value = self._parse_heartbeat_value(config.high_value, config.data_type)
                    interval_s = max(0.1, float(config.high_interval_s))
                else:
                    write_value = self._parse_heartbeat_value(config.low_value, config.data_type)
                    interval_s = max(0.1, float(config.low_interval_s))
                client.write_value(variable, write_value)
            except Exception as exc:
                self.sample_queue.put({"kind": "heartbeat_error", "message": f"心跳写入失败：{exc}"})
                return

            send_high = not send_high
            if self.heartbeat_stop_event.wait(interval_s):
                return

    def _apply_card_limits(self, index: int, lower_limit: float, upper_limit: float) -> None:
        if not (0 <= index < len(self.channels)) or lower_limit >= upper_limit:
            return
        with self.channel_lock:
            channel = self.channels[index]
            channel.lower_limit = float(lower_limit)
            channel.upper_limit = float(upper_limit)
        self.plot_dirty = True
        self.dirty_plot_indices.add(index)
        self.plot_prepare_pending_indices.add(index)
        self._enqueue_plot_prepare({index})
        saved, error = self._save_local_config()
        if saved:
            self._set_status(f"{self.channels[index].name} 上下限已更新。")
        else:
            self._set_status(f"{self.channels[index].name} 上下限已更新，但保存失败：{error}")

    def _apply_card_upper_limit_color(self, index: int, color: str) -> None:
        if not (0 <= index < len(self.channels)):
            return
        with self.channel_lock:
            self.channels[index].upper_limit_color = str(color)
        self.plot_dirty = True
        self.dirty_plot_indices.add(index)
        self.plot_prepare_pending_indices.add(index)
        self._enqueue_plot_prepare({index})
        saved, error = self._save_local_config()
        if saved:
            self._set_status(f"{self.channels[index].name} 上限颜色已更新。")
        else:
            self._set_status(f"{self.channels[index].name} 上限颜色已更新，但保存失败：{error}")

    def _apply_card_lower_limit_color(self, index: int, color: str) -> None:
        if not (0 <= index < len(self.channels)):
            return
        with self.channel_lock:
            self.channels[index].lower_limit_color = str(color)
        self.plot_dirty = True
        self.dirty_plot_indices.add(index)
        self.plot_prepare_pending_indices.add(index)
        self._enqueue_plot_prepare({index})
        saved, error = self._save_local_config()
        if saved:
            self._set_status(f"{self.channels[index].name} 下限颜色已更新。")
        else:
            self._set_status(f"{self.channels[index].name} 下限颜色已更新，但保存失败：{error}")

    def _toggle_demo_mode(self) -> None:
        self.demo_mode_enabled = self.demo_checkbox.isChecked()
        if self.demo_mode_enabled:
            self.auto_connect_enabled = False
            self.resume_sampling_after_connect = False
            if hasattr(self, "reconnect_timer"):
                self.reconnect_timer.stop()
            self._clear_active_connection_attempt()
            self._set_status("演示模式已开启，无外部数据源时也能查看 8 通道波形变化。")
        else:
            self.auto_connect_enabled = True
            self.resume_sampling_after_connect = True
            self._set_status("演示模式已关闭，采样将优先读取外部数据源。")
            if hasattr(self, "reconnect_timer"):
                QtCore.QTimer.singleShot(0, self._startup_auto_connect)

    @staticmethod
    def _is_connection_related_error(exc: Exception) -> bool:
        if isinstance(exc, ConnectionError):
            return True
        message = str(exc).lower()
        keywords = (
            "connection",
            "connected",
            "disconnect",
            "timed out",
            "timeout",
            "tcp",
            "iso",
            "network",
            "socket",
            "plc 尚未连接",
            "plc 连接已断开",
            "无法继续采样",
        )
        return any(keyword in message for keyword in keywords)

    def _startup_auto_connect(self) -> None:
        if self.demo_mode_enabled or not self.auto_connect_enabled:
            return
        self.resume_sampling_after_connect = True
        self._schedule_connection_attempt("connecting", auto_start_sampling=True, delay_ms=0)

    def _schedule_connection_attempt(self, reason: str, auto_start_sampling: bool, delay_ms: int = 0) -> None:
        if self.demo_mode_enabled or not self.auto_connect_enabled:
            return
        if self.connection_in_progress:
            return
        self.pending_connect_reason = reason
        self.pending_connect_auto_start = auto_start_sampling
        self._update_connection_badge(reason)
        if delay_ms <= 0:
            QtCore.QTimer.singleShot(0, self._attempt_scheduled_connection)
            return
        self.reconnect_timer.start(max(250, int(delay_ms)))

    def _attempt_scheduled_connection(self) -> None:
        if self.demo_mode_enabled or not self.auto_connect_enabled or self.connection_in_progress:
            return

        ip_address = self.ip_edit.text().strip()
        rack = int(self.rack_spin.value())
        slot = int(self.slot_spin.value())
        if not ip_address:
            self._update_connection_badge(False)
            self._set_status("PLC IP 为空，无法自动连接。")
            return

        self.connection_attempt_id += 1
        attempt_id = self.connection_attempt_id
        reason = self.pending_connect_reason
        auto_start_sampling = self.pending_connect_auto_start
        self.active_connection_attempt_id = attempt_id
        self.connection_in_progress = True
        self._update_connection_badge(reason)
        if reason == "reconnecting":
            self._set_status("PLC 掉线后正在重新连接...")
        else:
            self._set_status("正在连接数据源...")

        self.connection_thread = threading.Thread(
            target=self._connection_worker,
            args=(attempt_id, ip_address, rack, slot, reason, auto_start_sampling),
            daemon=True,
            name="plc-connect",
        )
        self.connection_thread.start()

    def _connection_worker(
        self,
        attempt_id: int,
        ip_address: str,
        rack: int,
        slot: int,
        reason: str,
        auto_start_sampling: bool,
    ) -> None:
        try:
            client = SiemensPLCClient(ip_address, rack, slot)
            client.connect()
        except Exception as exc:
            self.sample_queue.put(
                {
                    "kind": "connect_failed",
                    "attempt_id": attempt_id,
                    "reason": reason,
                    "auto_start_sampling": auto_start_sampling,
                    "message": str(exc),
                }
            )
            return

        self.sample_queue.put(
            {
                "kind": "connect_success",
                "attempt_id": attempt_id,
                "reason": reason,
                "auto_start_sampling": auto_start_sampling,
                "client": client,
                "ip_address": ip_address,
                "rack": rack,
                "slot": slot,
            }
        )

    def _clear_active_connection_attempt(self) -> None:
        self.connection_in_progress = False
        self.active_connection_attempt_id = None
        self.connection_thread = None

    def _disconnect_plc_client(self) -> None:
        self._stop_heartbeat()
        if self.plc_client is not None:
            try:
                self.plc_client.disconnect()
            except Exception:
                pass
            finally:
                self.plc_client = None

    def _handle_plc_connection_lost(self, message: str) -> None:
        self._disconnect_plc_client()
        self._update_connection_badge("reconnecting" if self.auto_connect_enabled else False)
        if self.auto_connect_enabled and not self.demo_mode_enabled:
            self._set_status(f"{message} 正在尝试重新连接...")
            self._schedule_connection_attempt(
                "reconnecting",
                auto_start_sampling=self.resume_sampling_after_connect,
                delay_ms=3000,
            )
        else:
            self._set_status(message)

    def _connect_plc(self) -> None:
        if self.sampling_active:
            QtWidgets.QMessageBox.warning(self, "提示", "请先停止采样，再重新连接。")
            return
        self.auto_connect_enabled = True
        self.resume_sampling_after_connect = False
        self.reconnect_timer.stop()
        self._schedule_connection_attempt("connecting", auto_start_sampling=False, delay_ms=0)

    def _disconnect_plc(self) -> None:
        self._stop_sampling()
        self.auto_connect_enabled = False
        self.resume_sampling_after_connect = False
        self.reconnect_timer.stop()
        self._clear_active_connection_attempt()
        try:
            self._disconnect_plc_client()
        except Exception as exc:
            self._set_status(f"PLC 断开时出现提示：{exc}")
        self._update_connection_badge(False)
        self._set_status("PLC 已断开连接。")

    def _start_sampling(self, remember_resume: bool = True) -> None:
        if self.sampling_active:
            self._set_status("采样已经在运行。")
            return

        if not self.demo_mode_enabled and (self.plc_client is None or not self.plc_client.is_connected):
            QtWidgets.QMessageBox.warning(self, "提示", "请先连接数据源，或者启用演示模式。")
            return

        self._sync_signal_configs_from_ui()
        try:
            for group_id in (1, 2):
                self.start_signal_configs[group_id - 1].to_variable(f"开始信号{group_id}").validate()
                self.complete_signal_configs[group_id - 1].to_variable(f"完成信号{group_id}").validate()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "配置错误", f"PLC 信号配置错误：{exc}")
            return

        self.start_signal_active = [False, False]
        self.complete_signal_active = [False, False]
        self.batch_start_timestamps = {}

        self.stop_event.clear()
        self.sampling_active = True
        self.worker_thread = threading.Thread(target=self._sampling_loop, daemon=True)
        self.worker_thread.start()
        if remember_resume and not self.demo_mode_enabled:
            self.resume_sampling_after_connect = True

        mode_text = "演示模式" if self.demo_mode_enabled else "实时采样"
        self._update_sampling_badge(True)
        saved, error = self._save_local_config()
        if saved:
            self._set_status(f"开始采样：{mode_text}")
        else:
            self._set_status(f"开始采样，但本地配置保存失败：{error}")

    def _stop_sampling(self, clear_resume: bool = True) -> None:
        if clear_resume:
            self.resume_sampling_after_connect = False
        if not self.sampling_active:
            self._update_sampling_badge(False)
            return

        self.stop_event.set()
        thread = self.worker_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

        self.worker_thread = None
        self.sampling_active = False
        self._update_sampling_badge(False)
        self._set_status("采样已停止。")

    def _generate_demo_batch_series(self, point_count: int, batch_duration_s: float) -> List[Tuple[int, List[float]]]:
        series_payload: List[Tuple[int, List[float]]] = []
        specs = self._channel_specs_snapshot()
        for index, variable in specs:
            if not variable.enabled:
                series_payload.append((index, []))
                continue

            values: List[float] = []
            denominator = max(1, point_count - 1)
            for point_index in range(point_count):
                progress = point_index / denominator
                phase_time = progress * batch_duration_s
                values.append(self._generate_demo_value(phase_time, index))
            series_payload.append((index, values))
        return series_payload

    def _read_plc_batch_series(
        self,
        client: SiemensPLCClient,
        channel_indices: Iterable[int],
    ) -> Tuple[List[Tuple[int, List[float]]], int, Dict[int, str], Dict[int, float], Dict[int, float], Dict[int, float]]:
        series_payload: List[Tuple[int, List[float]]] = []
        target_values: Dict[int, str] = {}
        upper_limit_values: Dict[int, float] = {}
        lower_limit_values: Dict[int, float] = {}
        baseline_values: Dict[int, float] = {}
        specs = self._channel_specs_snapshot(channel_indices)
        actual_point_count = 0
        for index, variable in specs:
            if not variable.enabled:
                series_payload.append((index, []))
            else:
                if variable.normalized_type() == "S7STRING":
                    values = client.read_series(variable, 0)
                else:
                    values = [client.read_value(variable)]
                actual_point_count = max(actual_point_count, len(values))
                series_payload.append((index, values))

            with self.channel_lock:
                channel = self.channels[index]
                target_enabled = channel.target_enabled
                target_variable = channel.target_variable() if target_enabled else None
                upper_limit_enabled = channel.upper_limit_read_enabled
                upper_limit_variable = channel.upper_limit_variable() if upper_limit_enabled else None
                lower_limit_enabled = channel.lower_limit_read_enabled
                lower_limit_variable = channel.lower_limit_variable() if lower_limit_enabled else None
                baseline_enabled = channel.baseline_read_enabled
                baseline_variable = channel.baseline_variable() if baseline_enabled else None
            if target_variable is not None:
                try:
                    target_text = client.read_text(target_variable).strip()
                except Exception:
                    target_text = "--"
                target_values[index] = target_text or "--"
            else:
                target_values[index] = "--"

            if upper_limit_variable is not None:
                try:
                    upper_limit_values[index] = float(client.read_value(upper_limit_variable))
                except Exception:
                    pass

            if lower_limit_variable is not None:
                try:
                    lower_limit_values[index] = float(client.read_value(lower_limit_variable))
                except Exception:
                    pass
            if baseline_variable is not None:
                try:
                    baseline_values[index] = float(client.read_value(baseline_variable))
                except Exception:
                    pass
        return series_payload, actual_point_count, target_values, upper_limit_values, lower_limit_values, baseline_values

    def _sampling_loop(self) -> None:
        interval_s = FIXED_POLL_INTERVAL_MS / 1000.0
        active_batch_starts: Dict[int, Optional[float]] = {1: None, 2: None}
        start_signal_active = [False, False]
        complete_signal_active = [False, False]
        signal_states_initialized = [False, False]
        demo_batch_active = False
        demo_active_batch_start: Optional[float] = None
        demo_batch_duration_s = 3.0
        demo_idle_duration_s = 1.5
        demo_next_start_time = time.time()

        while not self.stop_event.is_set():
            cycle_start = time.perf_counter()
            current_time = time.time()
            try:
                if self.demo_mode_enabled:
                    if not demo_batch_active and current_time >= demo_next_start_time:
                        demo_active_batch_start = current_time
                        demo_batch_active = True
                        self.sample_queue.put(
                            {
                                "kind": "batch_start",
                                "timestamp": demo_active_batch_start,
                                "channel_indices": self._group_channel_indices(None),
                            }
                        )
                    elif (
                        demo_batch_active
                        and demo_active_batch_start is not None
                        and current_time - demo_active_batch_start >= demo_batch_duration_s
                    ):
                        point_count = DEMO_BATCH_POINT_COUNT
                        series_payload = self._generate_demo_batch_series(point_count, demo_batch_duration_s)
                        self.sample_queue.put(
                            {
                                "kind": "batch_complete",
                                "start_timestamp": demo_active_batch_start,
                                "finish_timestamp": current_time,
                                "point_count": point_count,
                                "series": series_payload,
                                "channel_indices": self._group_channel_indices(None),
                            }
                        )
                        demo_active_batch_start = None
                        demo_batch_active = False
                        demo_next_start_time = current_time + demo_idle_duration_s
                else:
                    client = self.plc_client
                    if client is None or not client.is_connected:
                        raise ConnectionError("连接已断开，无法继续采样。")

                    for group_id in (1, 2):
                        channel_indices = self._group_channel_indices(group_id)
                        start_variable = self.start_signal_configs[group_id - 1].to_variable(f"开始信号{group_id}")
                        complete_variable = self.complete_signal_configs[group_id - 1].to_variable(f"完成信号{group_id}")
                        start_state = bool(round(client.read_value(start_variable)))
                        complete_state = bool(round(client.read_value(complete_variable)))

                        if not signal_states_initialized[group_id - 1]:
                            start_signal_active[group_id - 1] = start_state
                            complete_signal_active[group_id - 1] = complete_state
                            signal_states_initialized[group_id - 1] = True
                            continue

                        if start_state and not start_signal_active[group_id - 1]:
                            active_batch_starts[group_id] = current_time
                            self.sample_queue.put(
                                {
                                    "kind": "batch_start",
                                    "timestamp": current_time,
                                    "group_id": group_id,
                                    "channel_indices": channel_indices,
                                }
                            )
                            if self.reset_start_signal_enabled[group_id - 1]:
                                client.write_bool(start_variable, False)

                        if complete_state and not complete_signal_active[group_id - 1]:
                            if active_batch_starts.get(group_id) is None:
                                start_signal_active[group_id - 1] = start_state
                                complete_signal_active[group_id - 1] = complete_state
                                continue
                            (
                                series_payload,
                                actual_point_count,
                                target_values,
                                upper_limit_values,
                                lower_limit_values,
                                baseline_values,
                            ) = self._read_plc_batch_series(client, channel_indices)
                            report_sn = ""
                            auto_save_error = ""
                            try:
                                report_sn = self._read_report_sn_from_client(client, group_id)
                            except Exception as exc:
                                auto_save_error = str(exc)
                            self.sample_queue.put(
                                {
                                    "kind": "batch_complete",
                                    "group_id": group_id,
                                    "channel_indices": channel_indices,
                                    "start_timestamp": active_batch_starts.get(group_id) or current_time,
                                    "finish_timestamp": current_time,
                                    "point_count": actual_point_count,
                                    "series": series_payload,
                                    "target_values": target_values,
                                    "upper_limit_values": upper_limit_values,
                                    "lower_limit_values": lower_limit_values,
                                    "baseline_values": baseline_values,
                                    "auto_save_requested": True,
                                    "report_sn": report_sn,
                                    "auto_save_error": auto_save_error,
                                }
                            )
                            if self.reset_complete_signal_enabled[group_id - 1]:
                                client.write_bool(complete_variable, False)
                            active_batch_starts[group_id] = None

                        start_signal_active[group_id - 1] = start_state
                        complete_signal_active[group_id - 1] = complete_state
            except Exception as exc:
                if not self.demo_mode_enabled and self._is_connection_related_error(exc):
                    self.sample_queue.put({"kind": "connection_lost", "message": f"PLC 掉线：{exc}"})
                else:
                    self.sample_queue.put({"kind": "error", "message": f"采样失败：{exc}"})
                self.sample_queue.put({"kind": "stopped"})
                return

            elapsed = time.perf_counter() - cycle_start
            wait_seconds = max(0.0, interval_s - elapsed)
            if self.stop_event.wait(wait_seconds):
                return

    def _channel_specs_snapshot(self, indices: Optional[Iterable[int]] = None) -> List[Tuple[int, PLCVariable]]:
        selected = None if indices is None else set(int(index) for index in indices)
        with self.channel_lock:
            return [
                (index, channel.to_variable())
                for index, channel in enumerate(self.channels)
                if selected is None or index in selected
            ]

    def _generate_demo_value(self, timestamp: float, index: int) -> float:
        phase = index * 0.9
        amplitude = 18 + index * 5
        carrier = math.sin(timestamp * (0.9 + index * 0.08) + phase)
        envelope = math.cos(timestamp * 0.18 + phase / 2.0) * 0.35
        pulse = math.sin(timestamp * 2.1 + phase) * 0.12
        return round(amplitude * (carrier + envelope + pulse), 3)

    @staticmethod
    def _build_batch_timestamps(start_timestamp: float, finish_timestamp: float, count: int) -> List[float]:
        if count <= 0:
            return []
        start_timestamp = float(start_timestamp)
        finish_timestamp = max(float(finish_timestamp), start_timestamp)
        if count == 1:
            return [finish_timestamp]
        step = (finish_timestamp - start_timestamp) / float(count - 1)
        return [start_timestamp + step * index for index in range(count)]

    @staticmethod
    def _resolve_upper_limit_value(raw_limit: float, baseline_value: Optional[float], use_baseline: bool) -> float:
        raw = float(raw_limit)
        if use_baseline and baseline_value is not None:
            return normalize_display_precision(float(baseline_value) + abs(raw))
        return normalize_display_precision(raw)

    @staticmethod
    def _resolve_lower_limit_value(raw_limit: float, baseline_value: Optional[float], use_baseline: bool) -> float:
        raw = float(raw_limit)
        if use_baseline and baseline_value is not None:
            return normalize_display_precision(float(baseline_value) - abs(raw))
        return normalize_display_precision(raw)

    def _process_samples(self) -> None:
        if self.window_interacting:
            return
        changed_indices = set()
        while True:
            try:
                message = self.sample_queue.get_nowait()
            except queue.Empty:
                break

            kind = str(message.get("kind", ""))
            if kind == "connect_success":
                attempt_id = message.get("attempt_id")
                client = message.get("client")
                if attempt_id != self.active_connection_attempt_id or not isinstance(client, SiemensPLCClient):
                    if isinstance(client, SiemensPLCClient):
                        try:
                            client.disconnect()
                        except Exception:
                            pass
                    continue
                self._clear_active_connection_attempt()
                self.reconnect_timer.stop()
                self._disconnect_plc_client()
                self.plc_client = client
                self._restart_heartbeat()
                self._update_connection_badge(True)
                ip_address = str(message.get("ip_address", self.ip_edit.text().strip()))
                rack = int(message.get("rack", self.rack_spin.value()))
                slot = int(message.get("slot", self.slot_spin.value()))
                reason = str(message.get("reason", "connecting"))
                if reason == "reconnecting":
                    status_message = f"重连成功：{ip_address}，机架={rack}，插槽={slot}"
                else:
                    status_message = f"已连接：{ip_address}，机架={rack}，插槽={slot}"
                if self.heartbeat_config.enabled:
                    status_message = f"{status_message}，心跳已启动。"
                self._set_status(status_message)
                if bool(message.get("auto_start_sampling", False)) and not self.sampling_active and not self.demo_mode_enabled:
                    self._start_sampling(remember_resume=True)
            elif kind == "connect_failed":
                attempt_id = message.get("attempt_id")
                if attempt_id != self.active_connection_attempt_id:
                    continue
                self._clear_active_connection_attempt()
                reason = str(message.get("reason", "connecting"))
                auto_start_sampling = bool(message.get("auto_start_sampling", False))
                error_text = str(message.get("message", "未知错误"))
                next_reason = "reconnecting" if reason != "connecting" else "connecting"
                if self.auto_connect_enabled and not self.demo_mode_enabled:
                    self._update_connection_badge(next_reason)
                    self._set_status(f"PLC 连接失败：{error_text}，3 秒后自动重试。")
                    self._schedule_connection_attempt(next_reason, auto_start_sampling=auto_start_sampling, delay_ms=3000)
                else:
                    self._update_connection_badge(False)
                    self._set_status(f"PLC 连接失败：{error_text}")
            elif kind == "connection_lost":
                self._clear_active_connection_attempt()
                self._handle_plc_connection_lost(str(message.get("message", "连接已断开。")))
            elif kind == "batch_start":
                start_timestamp = float(message.get("timestamp", time.time()))
                group_id = self._coerce_int(message.get("group_id"), 0) or None
                channel_indices = tuple(int(index) for index in message.get("channel_indices", self._group_channel_indices(group_id)))
                with self.channel_lock:
                    for index in channel_indices:
                        if not (0 <= index < len(self.channels)):
                            continue
                        channel = self.channels[index]
                        channel.times.clear()
                        channel.values.clear()
                        channel.latest_value = 0.0
                        channel.target_value_text = "--"
                        channel.baseline_value = None
                        changed_indices.add(index)
                if group_id is not None:
                    self.batch_start_timestamps[group_id] = start_timestamp
                self._set_status(
                    f"收到开始信号{group_id or ''}，已清空{self._group_display_name(group_id)}。"
                    f" 开始时间：{time.strftime('%H:%M:%S', time.localtime(start_timestamp))}"
                )
            elif kind == "batch_complete":
                group_id = self._coerce_int(message.get("group_id"), 0) or None
                default_start = self.batch_start_timestamps.get(group_id, time.time()) if group_id is not None else time.time()
                start_timestamp = float(message.get("start_timestamp", default_start))
                finish_timestamp = float(message.get("finish_timestamp", time.time()))
                point_count = max(0, int(message.get("point_count", 0)))
                series_payload = message.get("series", [])
                target_values = message.get("target_values", {})
                upper_limit_values = message.get("upper_limit_values", {})
                lower_limit_values = message.get("lower_limit_values", {})
                baseline_values = message.get("baseline_values", {})
                channel_indices = tuple(int(index) for index in message.get("channel_indices", self._group_channel_indices(group_id)))
                channel_index_set = set(channel_indices)
                with self.channel_lock:
                    for index in channel_indices:
                        if not (0 <= index < len(self.channels)):
                            continue
                        channel = self.channels[index]
                        target_text = "--"
                        if isinstance(target_values, dict):
                            candidate = target_values.get(index, "--")
                            target_text = str(candidate).strip() or "--"
                        channel.target_value_text = target_text
                        channel.baseline_value = None
                        if isinstance(baseline_values, dict) and index in baseline_values:
                            channel.baseline_value = float(baseline_values[index])
                        use_baseline_for_limits = channel.baseline_read_enabled and channel.baseline_value is not None
                        new_upper_limit = channel.upper_limit
                        if isinstance(upper_limit_values, dict) and index in upper_limit_values:
                            new_upper_limit = self._resolve_upper_limit_value(
                                float(upper_limit_values[index]),
                                channel.baseline_value,
                                use_baseline_for_limits,
                            )
                        new_lower_limit = channel.lower_limit
                        if isinstance(lower_limit_values, dict) and index in lower_limit_values:
                            new_lower_limit = self._resolve_lower_limit_value(
                                float(lower_limit_values[index]),
                                channel.baseline_value,
                                use_baseline_for_limits,
                            )
                        if new_lower_limit < new_upper_limit:
                            channel.upper_limit = new_upper_limit
                            channel.lower_limit = new_lower_limit
                    for index, values in series_payload:
                        if index in channel_index_set and 0 <= index < len(self.channels):
                            channel = self.channels[index]
                            channel.times.clear()
                            channel.values.clear()
                            numeric_values = [float(value) for value in values]
                            timestamps = self._build_batch_timestamps(start_timestamp, finish_timestamp, len(numeric_values))
                            for timestamp, value in zip(timestamps, numeric_values):
                                channel.times.append(timestamp)
                                channel.values.append(value)
                            channel.latest_value = numeric_values[-1] if numeric_values else 0.0
                            changed_indices.add(index)
                if group_id is not None:
                    self.batch_start_timestamps.pop(group_id, None)
                status_message = (
                    f"收到完成信号{group_id or ''}，{self._group_display_name(group_id)}按 {point_count} 个采样值在 "
                    f"{time.strftime('%H:%M:%S', time.localtime(start_timestamp))} 到 "
                    f"{time.strftime('%H:%M:%S', time.localtime(finish_timestamp))} 之间均分生成波形。"
                )
                if bool(message.get("auto_save_requested", False)):
                    report_sn = str(message.get("report_sn", "")).strip()
                    auto_save_error = str(message.get("auto_save_error", "")).strip()
                    if report_sn:
                        self._queue_auto_save_request(report_sn, channel_indices, group_id, finish_timestamp)
                        status_message = f"{status_message} 已准备自动保存 PDF。"
                    else:
                        error_text = auto_save_error or "编号读取失败，无法自动保存 PDF。"
                        status_message = f"{status_message} 自动保存未执行：{error_text}"
                ng_debug_messages = self._emit_ng_debug_info(channel_indices, group_id)
                if ng_debug_messages:
                    status_message = f"{status_message} 调试: {' | '.join(ng_debug_messages)}"
                self._set_status(status_message)
            elif kind == "error":
                self._set_status(str(message.get("message", "采样发生未知错误。")))
                if not self.demo_mode_enabled:
                    self._update_connection_badge(False)
            elif kind == "heartbeat_error":
                heartbeat_message = str(message.get("message", "心跳发送发生未知错误。"))
                if self.auto_connect_enabled and not self.demo_mode_enabled:
                    self._handle_plc_connection_lost(heartbeat_message)
                else:
                    self._stop_heartbeat()
                    self._set_status(heartbeat_message)
            elif kind == "stopped":
                self.sampling_active = False
                self.worker_thread = None
                self._update_sampling_badge(False)

        if changed_indices:
            self.table_dirty = True
            self.dirty_plot_indices.update(changed_indices)
            self.plot_prepare_pending_indices.update(changed_indices)
            self._enqueue_plot_prepare(changed_indices)

    def _refresh_plots(self) -> None:
        if self.window_interacting:
            return
        self._drain_prepared_plot_queue()
        if self.plot_prepare_pending_indices:
            self._enqueue_plot_prepare(self.plot_prepare_pending_indices)

        if not self.plot_dirty and not self.prepared_plot_payloads:
            self._schedule_pending_auto_save()
            return

        dirty_indices = sorted(self.prepared_plot_payloads)
        if not dirty_indices:
            if self.plot_prepare_pending_indices or (
                self.plot_prepare_future is not None and not self.plot_prepare_future.done()
            ):
                return
            self.plot_dirty = False
            self._schedule_pending_auto_save()
            return

        for index in dirty_indices:
            if not (0 <= index < len(self.plot_cards)):
                continue
            card = self.plot_cards[index]
            snapshot = self.prepared_plot_payloads.get(index)
            card.update_snapshot(snapshot)
            self.prepared_plot_payloads.pop(index, None)
            self.dirty_plot_indices.discard(index)
        self.plot_dirty = False
        self._schedule_pending_auto_save()

    def _sync_bit_state(self) -> None:
        is_bool = self.type_combo.currentText().upper() == "BOOL"
        self.bit_spin.setEnabled(is_bool)
        if not is_bool:
            self.bit_spin.setValue(0)

    def _apply_channel_changes(self) -> None:
        index = self.selected_channel_index
        if not (0 <= index < len(self.channels)):
            QtWidgets.QMessageBox.information(self, "提示", "请先选择要编辑的通道。")
            return

        try:
            name = self.name_edit.text().strip() or f"波形 {index + 1}"
            db_number = int(self.db_spin.value())
            start = int(self.start_spin.value())
            data_type = self.type_combo.currentText().strip().upper()
            bit_index = int(self.bit_spin.value())
            visible = self.visible_checkbox.isChecked()
            PLCVariable(
                name=name,
                db_number=db_number,
                start=start,
                data_type=data_type,
                bit_index=bit_index,
                enabled=visible,
            ).validate()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "配置错误", str(exc))
            return

        with self.channel_lock:
            channel = self.channels[index]
            channel.name = name
            channel.db_number = db_number
            channel.start = start
            channel.data_type = data_type
            channel.bit_index = bit_index if data_type == "BOOL" else 0
            channel.visible = visible

        self.plot_dirty = True
        self.table_dirty = True
        self.dirty_plot_indices.add(index)
        self.plot_prepare_pending_indices.add(index)
        self._enqueue_plot_prepare({index})
        self._rebuild_channel_menu()
        self._select_channel(index)
        saved, error = self._save_local_config()
        if saved:
            self._set_status(f"已更新通道配置并保存到本地：{name}")
        else:
            self._set_status(f"已更新通道配置，但本地保存失败：{error}")

    @staticmethod
    def _sanitize_filename_component(value: str) -> str:
        cleaned = re.sub(r'[\\/:*?"<>|]+', "_", value.strip())
        cleaned = cleaned.strip(" .")
        return cleaned

    def _read_report_sn_from_client(self, client: SiemensPLCClient, group_id: int) -> str:
        config = self.sn_read_configs[group_id - 1]
        if not config.enabled:
            raise RuntimeError(f"请先在编号读取选项中启用并应用{self._group_display_name(group_id)}的编号读取配置。")

        variable = config.to_variable()
        variable.validate()

        raw_sn = client.read_text(variable).strip()
        sn = self._sanitize_filename_component(raw_sn)
        if not sn:
            raise RuntimeError(f"读取到的{self._group_display_name(group_id)}编号为空，无法保存 PDF。")
        return sn

    def _read_report_sn(self, group_id: int) -> str:
        client = self.plc_client
        if client is None or not client.is_connected:
            raise RuntimeError("保存 PDF 前请先连接数据源，以便读取编号。")
        return self._read_report_sn_from_client(client, group_id)

    def _resolve_save_root_dir(self, root_dir: Optional[str] = None) -> Path:
        raw_root_dir = (root_dir if root_dir is not None else self.save_settings.root_dir).strip()
        if not raw_root_dir:
            raw_root_dir = SaveSettingsConfig().root_dir
        resolved = Path(raw_root_dir).expanduser()
        if not resolved.is_absolute():
            resolved = self.base_dir / resolved
        return resolved

    @staticmethod
    def _format_save_time_text(pattern: str, timestamp: float) -> str:
        format_text = str(pattern).strip()
        if not format_text:
            return ""
        try:
            dt = datetime.fromtimestamp(timestamp)
            millisecond_placeholder = "__CODEx_MS_PLACEHOLDER__"
            rendered = dt.strftime(format_text.replace("%f", millisecond_placeholder))
            if millisecond_placeholder in rendered:
                rendered = rendered.replace(millisecond_placeholder, f"{dt.microsecond // 1000:03d}")
            return rendered
        except Exception:
            return format_text

    def _render_save_component(self, pattern: str, timestamp: float) -> str:
        format_text = str(pattern).strip()
        if not format_text:
            return ""
        rendered = self._format_save_time_text(format_text, timestamp)
        return self._sanitize_filename_component(rendered)

    def _build_report_filename_stem(
        self,
        sn: str,
        group_id: Optional[int] = None,
        save_timestamp: Optional[float] = None,
    ) -> str:
        timestamp = float(save_timestamp if save_timestamp is not None else time.time())
        safe_sn = self._sanitize_filename_component(sn) or "item"
        token_values = {
            "sn": safe_sn,
            "item_id": safe_sn,
            "date": self._format_save_time_text("%Y%m%d", timestamp),
            "time": self._format_save_time_text("%H%M%S", timestamp),
            "datetime": self._format_save_time_text("%Y%m%d_%H%M%S", timestamp),
            "group": f"G{int(group_id)}" if group_id in SIGNAL_GROUP_CHANNELS else "ALL",
        }
        pattern = str(self.save_settings.filename_pattern).strip() or "{item_id}"
        rendered = re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda match: token_values.get(match.group(1), ""), pattern)
        rendered = self._format_save_time_text(rendered, timestamp)
        stem = self._sanitize_filename_component(rendered)
        return stem or safe_sn

    def _build_report_pdf_path(
        self,
        sn: str,
        group_id: Optional[int] = None,
        save_timestamp: Optional[float] = None,
    ) -> Path:
        timestamp = float(save_timestamp if save_timestamp is not None else time.time())
        output_dir = self._resolve_save_root_dir()
        date_folder = self._render_save_component(self.save_settings.date_folder_format, timestamp)
        if date_folder:
            output_dir = output_dir / date_folder
        if self.save_settings.use_time_subfolder:
            time_folder = self._render_save_component(self.save_settings.time_folder_format, timestamp)
            if time_folder:
                output_dir = output_dir / time_folder
        output_dir.mkdir(parents=True, exist_ok=True)
        file_stem = self._build_report_filename_stem(sn, group_id, timestamp)
        output_path = output_dir / f"{file_stem}.pdf"
        if not output_path.exists():
            return output_path

        suffix = 1
        while True:
            candidate = output_dir / f"{file_stem}_{suffix:02d}.pdf"
            if not candidate.exists():
                return candidate
            suffix += 1

    def _capture_plot_card_image(self, card: PlotCard, scale: float = 1.0) -> QtGui.QImage:
        scale = max(1.0, float(scale))
        target_width = max(1, int(card.width() * scale))
        target_height = max(1, int(card.height() * scale))
        image = QtGui.QImage(target_width, target_height, QtGui.QImage.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(1.0)
        image.fill(QtCore.Qt.transparent)

        card.canvas.draw()
        painter = QtGui.QPainter(image)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
        painter.setRenderHint(QtGui.QPainter.TextAntialiasing, True)
        if hasattr(QtGui.QPainter, "HighQualityAntialiasing"):
            painter.setRenderHint(QtGui.QPainter.HighQualityAntialiasing, True)
        if hasattr(QtGui.QPainter, "LosslessImageRendering"):
            painter.setRenderHint(QtGui.QPainter.LosslessImageRendering, True)
        painter.scale(scale, scale)
        card.render(painter)
        painter.end()
        return image

    def _capture_plot_card_pixmap(self, card: PlotCard, scale: float = 1.0) -> QtGui.QPixmap:
        return QtGui.QPixmap.fromImage(self._capture_plot_card_image(card, scale=scale))

    @staticmethod
    def _is_channel_result_ng(channel: WaveformChannel) -> bool:
        if not channel.values:
            return False
        peak_value = normalize_display_precision(max(float(value) for value in channel.values))
        upper_limit = normalize_display_precision(float(channel.upper_limit))
        lower_limit = normalize_display_precision(float(channel.lower_limit))
        return peak_value > upper_limit or peak_value < lower_limit

    @staticmethod
    def _build_channel_ng_debug_message(channel: WaveformChannel) -> str:
        if not channel.values:
            return f"[DEBUG][NG] {channel.name}: 无数据"

        raw_peak_value = max(float(value) for value in channel.values)
        raw_upper_limit = float(channel.upper_limit)
        raw_lower_limit = float(channel.lower_limit)
        peak_value = normalize_display_precision(raw_peak_value)
        upper_limit = normalize_display_precision(raw_upper_limit)
        lower_limit = normalize_display_precision(raw_lower_limit)
        compare_upper = peak_value > upper_limit
        compare_lower = peak_value < lower_limit
        peak_minus_upper = peak_value - upper_limit
        peak_minus_lower = peak_value - lower_limit
        if compare_upper:
            diff_value = peak_value - upper_limit
            reason = "高于上限"
        elif compare_lower:
            diff_value = lower_limit - peak_value
            reason = "低于下限"
        else:
            diff_value = 0.0
            reason = "判定为OK"

        return (
            f"[DEBUG][NG] {channel.name}: 原因={reason}, "
            f"判定值(3位): 峰值={peak_value:.3f}, 上限={upper_limit:.3f}, 下限={lower_limit:.3f}, 差值={diff_value:.3f}, "
            f"比较结果: peak>upper={compare_upper}, peak<lower={compare_lower}; "
            f"原始值: 峰值={raw_peak_value:.15f}, 上限={raw_upper_limit:.15f}, 下限={raw_lower_limit:.15f}"
        )

    def _emit_ng_debug_info(self, channel_indices: Iterable[int], group_id: Optional[int] = None) -> List[str]:
        debug_messages: List[str] = []
        with self.channel_lock:
            for index in channel_indices:
                if not (0 <= int(index) < len(self.channels)):
                    continue
                channel = self.channels[int(index)]
                if self._is_channel_result_ng(channel):
                    debug_messages.append(self._build_channel_ng_debug_message(channel))

        for message in debug_messages:
            group_text = "" if group_id is None else f"[GROUP {group_id}] "
            print(f"{group_text}{message}")

        return debug_messages

    def _collect_report_results(
        self,
        channel_indices: Optional[Iterable[int]] = None,
    ) -> List[Tuple[str, str, str, bool]]:
        selected = None if channel_indices is None else set(int(index) for index in channel_indices)
        with self.channel_lock:
            return [
                (
                    channel.name,
                    channel.target_label,
                    channel.target_value_text,
                    self._is_channel_result_ng(channel),
                )
                for index, channel in enumerate(self.channels)
                if selected is None or index in selected
            ]

    def _prepare_report_assets(
        self,
        scale: float = 1.0,
        channel_indices: Optional[Iterable[int]] = None,
    ) -> Tuple[List[QtGui.QImage], List[Tuple[str, str, str, bool]], Dict[str, int]]:
        scale = max(1.0, float(scale))
        self._process_samples()
        self._refresh_plots()
        QtWidgets.QApplication.processEvents()

        selected_indices = (
            [index for index in channel_indices if 0 <= int(index) < len(self.plot_cards)]
            if channel_indices is not None
            else list(range(len(self.plot_cards)))
        )
        plot_images = [self._capture_plot_card_image(self.plot_cards[int(index)], scale=scale) for index in selected_indices]
        if not plot_images:
            raise RuntimeError("当前没有可导出的波形图。")
        report_results = self._collect_report_results(selected_indices)

        margin = int(44 * scale)
        gap = int(22 * scale)
        title_height = int(78 * scale)
        subtitle_height = int(56 * scale)
        summary_title_height = int(32 * scale)
        summary_header_height = int(32 * scale)
        summary_row_height = int(34 * scale)
        summary_columns = 2
        summary_rows = max(1, math.ceil(max(1, len(report_results)) / summary_columns))
        summary_table_height = summary_header_height + summary_rows * summary_row_height
        summary_block_height = summary_title_height + summary_table_height + int(18 * scale)
        content_width = max(plot_image.width() for plot_image in plot_images)
        image_width = content_width + margin * 2
        image_height = (
            margin
            + title_height
            + subtitle_height
            + summary_block_height
            + sum(plot_image.height() for plot_image in plot_images)
            + gap * max(0, len(plot_images) - 1)
            + margin
        )

        layout = {
            "margin": margin,
            "gap": gap,
            "title_height": title_height,
            "subtitle_height": subtitle_height,
            "summary_title_height": summary_title_height,
            "summary_header_height": summary_header_height,
            "summary_row_height": summary_row_height,
            "summary_columns": summary_columns,
            "summary_rows": summary_rows,
            "summary_table_height": summary_table_height,
            "summary_block_height": summary_block_height,
            "content_width": content_width,
            "image_width": image_width,
            "image_height": image_height,
            "scale_x1000": int(scale * 1000),
        }
        return plot_images, report_results, layout

    def _draw_report_content(
        self,
        painter: QtGui.QPainter,
        sn: str,
        plot_images: List[QtGui.QImage],
        report_results: List[Tuple[str, str, str, bool]],
        layout: Dict[str, int],
    ) -> None:
        scale = max(1.0, float(layout.get("scale_x1000", 1000)) / 1000.0)
        margin = int(layout["margin"])
        gap = int(layout["gap"])
        title_height = int(layout["title_height"])
        subtitle_height = int(layout["subtitle_height"])
        summary_title_height = int(layout["summary_title_height"])
        summary_header_height = int(layout["summary_header_height"])
        summary_row_height = int(layout["summary_row_height"])
        summary_columns = max(1, int(layout["summary_columns"]))
        summary_block_height = int(layout["summary_block_height"])
        content_width = int(layout["content_width"])
        image_width = int(layout["image_width"])

        def make_report_font(pixel_size: int, bold: bool = False) -> QtGui.QFont:
            font = QtGui.QFont()
            font.setPixelSize(max(1, int(pixel_size)))
            font.setBold(bold)
            return font

        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
        painter.setRenderHint(QtGui.QPainter.TextAntialiasing, True)
        if hasattr(QtGui.QPainter, "HighQualityAntialiasing"):
            painter.setRenderHint(QtGui.QPainter.HighQualityAntialiasing, True)
        if hasattr(QtGui.QPainter, "LosslessImageRendering"):
            painter.setRenderHint(QtGui.QPainter.LosslessImageRendering, True)

        title_font = make_report_font(max(int(18 * scale), int(20 * self.ui_scale * scale)), bold=True)
        painter.setFont(title_font)
        painter.setPen(QtGui.QColor("#1b2733"))
        painter.drawText(
            QtCore.QRect(0, margin - 8, image_width, title_height),
            QtCore.Qt.AlignCenter,
            "波形曲线报告",
        )

        info_font = make_report_font(max(int(9 * scale), int(10 * self.ui_scale * scale)))
        painter.setFont(info_font)
        painter.setPen(QtGui.QColor("#506070"))
        info_top = margin + title_height - 4
        info_line_height = max(1, subtitle_height // 2)
        painter.drawText(
            QtCore.QRect(margin, info_top, image_width - margin * 2, info_line_height),
            QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter,
            f"编号: {sn}",
        )
        painter.drawText(
            QtCore.QRect(margin, info_top + info_line_height, image_width - margin * 2, subtitle_height - info_line_height),
            QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter,
            f"日期时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        )

        summary_top = margin + title_height + subtitle_height
        summary_title_font = make_report_font(
            max(int(10 * scale), int(11 * self.ui_scale * scale)),
            bold=True,
        )
        painter.setFont(summary_title_font)
        painter.setPen(QtGui.QColor("#1b2733"))
        painter.drawText(
            QtCore.QRect(margin, summary_top, image_width - margin * 2, summary_title_height),
            QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
            "结果汇总",
        )

        table_top = summary_top + summary_title_height
        table_width = image_width - margin * 2
        cell_width = table_width // summary_columns
        header_font = make_report_font(
            max(int(9 * scale), int(9 * self.ui_scale * scale)),
            bold=True,
        )
        row_font = make_report_font(max(int(8 * scale), int(9 * self.ui_scale * scale)))

        header_bg = QtGui.QColor("#e7edf3")
        border_color = QtGui.QColor("#c6d2de")
        text_color = QtGui.QColor("#23303b")
        ok_bg = QtGui.QColor("#d1fae5")
        ok_text = QtGui.QColor("#15803d")
        ng_bg = QtGui.QColor("#fee2e2")
        ng_text = QtGui.QColor("#dc2626")

        for column in range(summary_columns):
            rect = QtCore.QRect(margin + column * cell_width, table_top, cell_width, summary_header_height)
            painter.fillRect(rect, header_bg)
            painter.setPen(border_color)
            painter.drawRect(rect)
            painter.setFont(header_font)
            painter.setPen(text_color)
            painter.drawText(
                rect.adjusted(int(10 * scale), 0, -int(10 * scale), 0),
                QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                "波形 / 参数 / 结果",
            )

        for index, (name, target_label, target_value_text, is_ng) in enumerate(report_results):
            row = index // summary_columns
            column = index % summary_columns
            rect = QtCore.QRect(
                margin + column * cell_width,
                table_top + summary_header_height + row * summary_row_height,
                cell_width,
                summary_row_height,
            )
            painter.setPen(border_color)
            painter.drawRect(rect)
            painter.setFont(row_font)
            painter.setPen(text_color)
            detail_label = str(target_label).strip() or "指定值"
            detail_value = str(target_value_text).strip() or "--"
            row_text = f"{name}  {detail_label}: {detail_value}"
            painter.drawText(
                rect.adjusted(int(10 * scale), 0, -int(90 * scale), 0),
                QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                row_text,
            )

            badge_rect = QtCore.QRect(
                rect.right() - int(78 * scale),
                rect.top() + int(5 * scale),
                int(66 * scale),
                rect.height() - int(10 * scale),
            )
            painter.fillRect(badge_rect, ng_bg if is_ng else ok_bg)
            painter.setPen(QtGui.QPen(border_color))
            painter.drawRect(badge_rect)
            painter.setPen(ng_text if is_ng else ok_text)
            painter.drawText(badge_rect, QtCore.Qt.AlignCenter, "NG" if is_ng else "OK")

        current_y = summary_top + summary_block_height
        for plot_image in plot_images:
            x = margin + max(0, (content_width - plot_image.width()) // 2)
            target_rect = QtCore.QRect(x, current_y, plot_image.width(), plot_image.height())
            painter.drawImage(target_rect, plot_image)
            current_y += plot_image.height() + gap

    def _build_report_image(
        self,
        sn: str,
        scale: float = 1.0,
        channel_indices: Optional[Iterable[int]] = None,
    ) -> QtGui.QImage:
        plot_images, report_results, layout = self._prepare_report_assets(scale=scale, channel_indices=channel_indices)
        report_image = QtGui.QImage(
            int(layout["image_width"]),
            int(layout["image_height"]),
            QtGui.QImage.Format_ARGB32_Premultiplied,
        )
        report_image.fill(QtGui.QColor("#ffffff"))

        painter = QtGui.QPainter(report_image)
        self._draw_report_content(painter, sn, plot_images, report_results, layout)
        painter.end()
        return report_image

        scale = max(1.0, float(scale))
        self._process_samples()
        self._refresh_plots()
        QtWidgets.QApplication.processEvents()

        pixmaps = [self._capture_plot_card_pixmap(card, scale=scale) for card in self.plot_cards]
        if not pixmaps:
            raise RuntimeError("当前没有可导出的波形图。")
        report_results = self._collect_report_results()

        margin = int(44 * scale)
        gap = int(22 * scale)
        title_height = int(78 * scale)
        subtitle_height = int(34 * scale)
        summary_title_height = int(32 * scale)
        summary_header_height = int(32 * scale)
        summary_row_height = int(34 * scale)
        summary_columns = 2
        summary_rows = max(1, math.ceil(max(1, len(report_results)) / summary_columns))
        summary_table_height = summary_header_height + summary_rows * summary_row_height
        summary_block_height = summary_title_height + summary_table_height + int(18 * scale)
        content_width = max(pixmap.width() for pixmap in pixmaps)
        image_width = content_width + margin * 2
        image_height = (
            margin
            + title_height
            + subtitle_height
            + summary_block_height
            + sum(pixmap.height() for pixmap in pixmaps)
            + gap * max(0, len(pixmaps) - 1)
            + margin
        )

        report_image = QtGui.QImage(image_width, image_height, QtGui.QImage.Format_ARGB32)
        report_image.fill(QtGui.QColor("#ffffff"))

        painter = QtGui.QPainter(report_image)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
        painter.setRenderHint(QtGui.QPainter.TextAntialiasing, True)

        title_font = QtGui.QFont()
        title_font.setPointSize(max(int(18 * scale), int(20 * self.ui_scale * scale)))
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QtGui.QColor("#1b2733"))
        painter.drawText(
            QtCore.QRect(0, margin - 8, image_width, title_height),
            QtCore.Qt.AlignCenter,
            "波形曲线报告",
        )

        info_font = QtGui.QFont()
        info_font.setPointSize(max(int(9 * scale), int(10 * self.ui_scale * scale)))
        painter.setFont(info_font)
        painter.setPen(QtGui.QColor("#506070"))
        painter.drawText(
            QtCore.QRect(margin, margin + title_height - 4, image_width - margin * 2, subtitle_height),
            QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter,
            f"编号: {sn}",
        )

        summary_top = margin + title_height + subtitle_height
        summary_title_font = QtGui.QFont()
        summary_title_font.setPointSize(max(int(10 * scale), int(11 * self.ui_scale * scale)))
        summary_title_font.setBold(True)
        painter.setFont(summary_title_font)
        painter.setPen(QtGui.QColor("#1b2733"))
        painter.drawText(
            QtCore.QRect(margin, summary_top, image_width - margin * 2, summary_title_height),
            QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
            "结果汇总",
        )

        table_top = summary_top + summary_title_height
        table_width = image_width - margin * 2
        cell_width = table_width // summary_columns
        header_font = QtGui.QFont()
        header_font.setPointSize(max(int(9 * scale), int(9 * self.ui_scale * scale)))
        header_font.setBold(True)
        row_font = QtGui.QFont()
        row_font.setPointSize(max(int(8 * scale), int(9 * self.ui_scale * scale)))

        header_bg = QtGui.QColor("#e7edf3")
        border_color = QtGui.QColor("#c6d2de")
        text_color = QtGui.QColor("#23303b")
        ok_bg = QtGui.QColor("#d1fae5")
        ok_text = QtGui.QColor("#15803d")
        ng_bg = QtGui.QColor("#fee2e2")
        ng_text = QtGui.QColor("#dc2626")

        for column in range(summary_columns):
            rect = QtCore.QRect(margin + column * cell_width, table_top, cell_width, summary_header_height)
            painter.fillRect(rect, header_bg)
            painter.setPen(border_color)
            painter.drawRect(rect)
            painter.setFont(header_font)
            painter.setPen(text_color)
            header_text = "波形 / 参数 / 结果" if column == 0 else ""
            painter.drawText(
                rect.adjusted(int(10 * scale), 0, -int(10 * scale), 0),
                QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                header_text,
            )

        for index, (name, target_label, target_value_text, is_ng) in enumerate(report_results):
            row = index // summary_columns
            column = index % summary_columns
            rect = QtCore.QRect(
                margin + column * cell_width,
                table_top + summary_header_height + row * summary_row_height,
                cell_width,
                summary_row_height,
            )
            painter.setPen(border_color)
            painter.drawRect(rect)
            painter.setFont(row_font)
            painter.setPen(text_color)
            detail_label = str(target_label).strip() or "指定值"
            detail_value = str(target_value_text).strip() or "--"
            row_text = f"{name}  {detail_label}: {detail_value}"
            painter.drawText(
                rect.adjusted(int(10 * scale), 0, -int(90 * scale), 0),
                QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                row_text,
            )

            badge_rect = QtCore.QRect(
                rect.right() - int(78 * scale),
                rect.top() + int(5 * scale),
                int(66 * scale),
                rect.height() - int(10 * scale),
            )
            painter.fillRect(badge_rect, ng_bg if is_ng else ok_bg)
            painter.setPen(QtGui.QPen(border_color))
            painter.drawRect(badge_rect)
            painter.setPen(ng_text if is_ng else ok_text)
            painter.drawText(badge_rect, QtCore.Qt.AlignCenter, "NG" if is_ng else "OK")

        current_y = summary_top + summary_block_height
        for pixmap in pixmaps:
            x = margin + max(0, (content_width - pixmap.width()) // 2)
            painter.drawPixmap(x, current_y, pixmap)
            current_y += pixmap.height() + gap

        painter.end()
        return report_image

    def _save_report_pdf(
        self,
        output_path: Path,
        sn: str,
        channel_indices: Optional[Iterable[int]] = None,
    ) -> None:
        report_scale = 3.0
        pdf_resolution = 400
        plot_images, report_results, layout = self._prepare_report_assets(
            scale=report_scale,
            channel_indices=channel_indices,
        )
        width_mm = float(layout["image_width"]) * 25.4 / float(pdf_resolution)
        height_mm = float(layout["image_height"]) * 25.4 / float(pdf_resolution)

        writer = QtGui.QPdfWriter(str(output_path))
        writer.setTitle("波形曲线报告")
        writer.setCreator("Waveform Monitor")
        writer.setResolution(pdf_resolution)
        writer.setPageSize(
            QtGui.QPageSize(
                QtCore.QSizeF(width_mm, height_mm),
                QtGui.QPageSize.Millimeter,
                "ForceValueReport",
            )
        )
        writer.setPageMargins(QtCore.QMarginsF(0.0, 0.0, 0.0, 0.0), QtGui.QPageLayout.Millimeter)

        painter = QtGui.QPainter(writer)
        if not painter.isActive():
            raise RuntimeError("PDF 画布创建失败。")

        page_rect = writer.pageLayout().paintRectPixels(pdf_resolution)
        painter.translate(page_rect.x(), page_rect.y())
        scale_x = page_rect.width() / max(1.0, float(layout["image_width"]))
        scale_y = page_rect.height() / max(1.0, float(layout["image_height"]))
        if abs(scale_x - 1.0) > 1e-6 or abs(scale_y - 1.0) > 1e-6:
            painter.scale(scale_x, scale_y)

        self._draw_report_content(painter, sn, plot_images, report_results, layout)
        painter.end()
        return

        report_scale = 2.2
        report_image = self._build_report_image(sn, scale=report_scale)
        buffer = QtCore.QBuffer()
        if not buffer.open(QtCore.QIODevice.WriteOnly):
            raise RuntimeError("报告缓存创建失败。")
        if not report_image.save(buffer, "PNG"):
            buffer.close()
            raise RuntimeError("报告图像生成失败。")

        image_bytes = bytes(buffer.data())
        buffer.close()

        with Image.open(io.BytesIO(image_bytes)) as source_image:
            pdf_image = source_image.convert("RGB")
        try:
            pdf_image.save(str(output_path), "PDF", resolution=300.0)
        finally:
            pdf_image.close()

    def _save_selected_plot(self) -> None:
        if not self.plot_cards:
            QtWidgets.QMessageBox.information(self, "提示", "当前没有可保存的波形图。")
            return

        group_id = self._group_id_from_channel_index(self.selected_channel_index)
        channel_indices = self._group_channel_indices(group_id)
        try:
            sn = self._read_report_sn(group_id)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "保存失败", str(exc))
            self._set_status(f"读取编号失败：{exc}")
            return

        output_path = self._build_report_pdf_path(sn, group_id, time.time())
        try:
            self._save_report_pdf(output_path, sn, channel_indices)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "保存失败", str(exc))
            self._set_status(f"PDF 报告保存失败：{exc}")
            return
        self._set_status(f"PDF 报告已保存到：{output_path}")

    def _set_badge(self, label: QtWidgets.QLabel, text: str, color: str) -> None:
        label.setText(text)
        label.setStyleSheet(
            f"background:{color}; color:{self.COLORS['bg']}; border-radius:10px; padding:6px 12px; font-weight:700;"
        )

    def _update_connection_badge(self, connected: Optional[object] = None) -> None:
        if connected is None:
            state = "connected" if (self.plc_client is not None and self.plc_client.is_connected) else "connecting"
        elif isinstance(connected, bool):
            state = "connected" if connected else "connecting"
        else:
            state = str(connected).strip().lower()
            if state in {"reconnecting", "disconnected", "retrying"}:
                state = "connecting"

        if state == "connected":
            self._set_badge(self.connection_badge, "PLC 已连接", self.COLORS["success"])
        else:
            self._set_badge(self.connection_badge, "正在连接PLC...", self.COLORS["accent_soft"])

    def _update_sampling_badge(self, running: bool = False) -> None:
        if running:
            self._set_badge(self.sampling_badge, "采样运行中", self.COLORS["accent"])
        else:
            self._set_badge(self.sampling_badge, "采样停止", self.COLORS["muted"])

    def moveEvent(self, event: QtGui.QMoveEvent) -> None:
        super().moveEvent(event)
        if self.isVisible() and not self.isMaximized() and not self.isFullScreen():
            self._schedule_interaction_settle()

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        if self.isVisible():
            self._schedule_interaction_settle()

    def nativeEvent(self, event_type, message):
        if sys.platform.startswith("win"):
            try:
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == WM_ENTERSIZEMOVE:
                    self._begin_window_interaction()
                elif msg.message == WM_EXITSIZEMOVE:
                    self.interaction_settle_timer.stop()
                    self._end_window_interaction()
            except Exception:
                pass
        return super().nativeEvent(event_type, message)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self.interaction_settle_timer.stop()
        self.reconnect_timer.stop()
        self._clear_active_connection_attempt()
        self._save_local_config()
        self._stop_sampling()
        self._stop_heartbeat()
        if self.plc_client is not None:
            try:
                self.plc_client.disconnect()
            except Exception:
                pass
        try:
            self.plot_prepare_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        event.accept()


def main() -> int:
    app = QtWidgets.QApplication.instance()
    owns_app = app is None
    if app is None:
        app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("工业波形图显示软件")
    window = WaveformMonitorWindow()
    window.showMinimized()
    if owns_app:
        return app.exec_()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

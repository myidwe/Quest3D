"""Korean Tk desktop controls. The controller owns processes and applied state."""

from collections.abc import Callable
from dataclasses import dataclass
import math
import tkinter as tk
from tkinter import ttk

from .desktop_theme import BACKGROUND, configure_theme, dark_titlebar


@dataclass(frozen=True)
class _Availability:
    start: bool
    stop: bool
    control: bool
    monitor: bool


def _availability(snapshot: dict, queued: bool = False) -> _Availability:
    idle = not snapshot.get("busy", False) and not queued
    phase = snapshot.get("phase")
    safe = phase in ("idle", "running", "error")
    running = bool(snapshot.get("running"))
    host_running = bool(snapshot.get("host_running"))
    ready = bool(snapshot.get("ready", running))
    stopped = not running and not host_running
    resume_host = phase == "running" and running and ready and not host_running and not snapshot.get("error")
    return _Availability(
        idle and ((phase in ("idle", "error") and stopped) or resume_host),
        idle and phase in ("idle", "running", "error", "conflict") and not stopped,
        idle and safe and running and ready,
        idle and safe and stopped,
    )


def _parse_depth(text: str) -> float:
    if not isinstance(text, str):
        raise ValueError("입체감은 0~4 사이의 숫자로 입력해 주세요.")
    try:
        value = float(text.strip().removesuffix("%").strip())
    except ValueError:
        raise ValueError("입체감은 0~4 사이의 숫자로 입력해 주세요.") from None
    if not math.isfinite(value) or not 0 <= value <= 4:
        raise ValueError("입체감은 0~4% 범위에서 조절할 수 있습니다.")
    return value


def _depth_text(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "—"
    return f"{value:.2f}"


def _text(value, fallback="") -> str:
    return value if isinstance(value, str) and value else fallback


def _monitor_choices(monitors) -> dict[str, str]:
    """Display labels are never used as Windows device identities."""
    output = {}
    for monitor in monitors if isinstance(monitors, list) else []:
        if not isinstance(monitor, dict) or not isinstance(monitor.get("device_name"), str):
            continue
        device = monitor["device_name"]
        if not device:
            continue
        label = _text(monitor.get("label"), device)
        # Include the device identity only for otherwise ambiguous display labels.
        if label in output and output[label] != device:
            previous = output.pop(label)
            output[f"{label} · {previous}"] = previous
            label = f"{label} · {device}"
        elif any(key.startswith(label + " · ") for key in output):
            label = f"{label} · {device}"
        output[label] = device
    return output


class DesktopWindow:
    """View only: commands are queued and success is read from later snapshots.

    The supplied callbacks own tray hiding and application shutdown. Construct on
    the Tk thread; controller.get_snapshot() and command() must never block it.
    """

    POLL_MS = 500

    def __init__(self, root: tk.Tk, controller, *, on_hide: Callable, on_exit: Callable):
        self.root, self.controller = root, controller
        self.on_hide, self.on_exit = on_hide, on_exit
        self._snapshot: dict = {}
        self._queued = False
        self._transient_notice = False
        self._destroyed = False
        self._after_id = None
        self._editing_depth = False
        self._depth_dirty = False
        self._setting_depth = False
        self._monitors: dict[str, str] = {}
        self._notice = tk.StringVar(root)
        self._depth = tk.StringVar(root)
        self._monitor = tk.StringVar(root)
        self._comfort = tk.BooleanVar(root)
        self._pin = tk.StringVar(root)
        self._details_open = False
        self._pairing_open = False
        self._depth.trace_add("write", self._depth_changed)

        root.title("Quest3D 데스크톱")
        root.configure(background=BACKGROUND)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        root.protocol("WM_DELETE_WINDOW", on_exit)
        root.bind("<Unmap>", self._on_unmap, add="+")
        root.bind("<Destroy>", self._on_destroy, add="+")
        self._styles()
        width = min(self._px(960), root.winfo_screenwidth() - 80)
        height = min(self._px(730), root.winfo_screenheight() - 100)
        root.geometry(f"{width}x{height}")
        root.minsize(min(self._px(780), width), min(self._px(640), height))
        self._build()
        self._poll()
        root.after(200, lambda: dark_titlebar(root))

    def _styles(self):
        self._fonts, self._scale = configure_theme(self.root)

    def _px(self, value):
        return round(value * self._scale)

    def _card(self, parent, row, **kwargs):
        card = ttk.Frame(parent, style="Q.Card.TFrame", padding=(self._px(24), self._px(16)), **kwargs)
        card.grid(row=row, column=0, sticky="ew", pady=(0, self._px(12)))
        return card

    def _build(self):
        p = self._px
        viewport = ttk.Frame(self.root, style="Q.TFrame")
        viewport.grid(row=0, column=0, sticky="nsew")
        viewport.columnconfigure(0, weight=1)
        viewport.rowconfigure(0, weight=1)
        self._canvas = tk.Canvas(viewport, background=BACKGROUND, highlightthickness=0)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        self._scrollbar = ttk.Scrollbar(viewport, orient="vertical", style="Q.Vertical.TScrollbar", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._scrollbar.set)
        shell = ttk.Frame(self._canvas, style="Q.TFrame", padding=(p(30), p(20)))
        self._canvas_window = self._canvas.create_window(0, 0, anchor="nw", window=shell)
        self._canvas.bind("<Configure>", self._resize_scroll_area, add="+")
        self.root.bind("<MouseWheel>", self._mouse_wheel, add="+")
        self.root.bind("<FocusIn>", self._reveal_focus, add="+")
        shell.columnconfigure(0, weight=1)
        self._shell = shell

        header = ttk.Frame(shell, style="Q.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, p(18)))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Quest3D", style="Q.Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="익숙한 PC 화면, 새로운 입체감.", style="Q.Subtitle.TLabel").grid(row=1, column=0, sticky="w", pady=(p(4), 0))
        self.hide_button = ttk.Button(header, text="트레이로 숨기기", style="Q.TButton", command=self.on_hide)
        self.hide_button.grid(row=0, column=1, rowspan=2, sticky="e")

        status = self._card(shell, 1)
        status.columnconfigure(0, weight=1)
        self.status_badge = ttk.Label(status, style="Q.Badge.TLabel", padding=(p(9), p(4)))
        self.status_badge.grid(row=0, column=1, sticky="e")
        self.phase_label = ttk.Label(status, style="Q.Status.TLabel")
        self.phase_label.grid(row=0, column=0, sticky="w")
        self.message_label = ttk.Label(status, style="Q.Muted.TLabel", wraplength=p(550))
        self.message_label.grid(row=1, column=0, sticky="ew", pady=(p(7), 0))
        self.primary_button = ttk.Button(status, text="PC 시작", style="Q.Primary.TButton", command=self._primary)
        self.primary_button.grid(row=1, column=1, sticky="e", padx=(p(24), 0), pady=(p(7), 0))
        self.connection_label = ttk.Label(status, style="Q.Accent.TLabel", wraplength=p(620))
        self.connection_label.grid(row=2, column=0, sticky="w", pady=(p(6), 0))
        self.stop_button = ttk.Button(status, text="PC 중지", style="Q.TButton", command=self._stop)
        self.stop_button.grid(row=4, column=1, sticky="e", pady=(p(12), 0))
        self.pair_toggle = ttk.Button(status, text="새 Quest 연결", style="Q.Ghost.TButton", command=self._toggle_pairing)
        self.pair_toggle.grid(row=2, column=1, sticky="e", pady=(p(6), 0))
        self.pairing_panel = ttk.Frame(status, style="Q.Card.TFrame")
        self.pairing_panel.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(p(6), 0))
        self.pairing_panel.columnconfigure(0, weight=1)
        ttk.Separator(self.pairing_panel, style="Q.TSeparator").grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, p(12)))
        self.pair_help = ttk.Label(self.pairing_panel, text="PC를 시작한 뒤 Quest 앱에서 새 PC를 추가하세요.\n이 PC 주소로 연결하면 표시되는 4자리 PIN을 입력해 주세요.", style="Q.Muted.TLabel", wraplength=p(580))
        self.pair_help.grid(row=1, column=0, columnspan=2, sticky="w")
        pair_row = ttk.Frame(self.pairing_panel, style="Q.Card.TFrame")
        pair_row.grid(row=2, column=0, sticky="w", pady=(p(12), 0))
        ttk.Label(pair_row, text="연결 PIN", style="Q.TLabel").grid(row=0, column=0, padx=(0, p(14)))
        self.pin_entry = ttk.Entry(pair_row, textvariable=self._pin, show="●", width=7,
                                  justify="center", style="Q.TEntry", font=self._fonts[0])
        self.pin_entry.grid(row=0, column=1, padx=(0, p(10)))
        self.pin_entry.bind("<Return>", self._pair)
        self.pair_button = ttk.Button(pair_row, text="연결 승인", style="Q.Primary.TButton", command=self._pair)
        self.pair_button.grid(row=0, column=2)
        self.pair_notice = ttk.Label(self.pairing_panel, style="Q.Muted.TLabel", wraplength=p(760))
        self.pair_notice.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(p(8), 0))
        self.pairing_panel.grid_remove()

        controls = self._card(shell, 2)
        controls.columnconfigure(1, weight=1)
        ttk.Label(controls, text="화면 보기", style="Q.Heading.TLabel").grid(row=0, column=0, sticky="w", padx=(0, p(36)))
        modes = ttk.Frame(controls, style="Q.Raised.TFrame", padding=p(3))
        modes.grid(row=0, column=1, sticky="w")
        self.mode_2d = ttk.Button(modes, text="2D 원본", width=10, style="Q.TButton", command=lambda: self._control(mode="2d"))
        self.mode_3d = ttk.Button(modes, text="3D 입체", width=10, style="Q.TButton", command=lambda: self._control(mode="3d"))
        self.mode_2d.grid(row=0, column=0, padx=(0, p(3)))
        self.mode_3d.grid(row=0, column=1)
        ttk.Label(controls, text="입체감", style="Q.Heading.TLabel").grid(row=1, column=0, sticky="w", pady=(p(20), 0))
        depth = ttk.Frame(controls, style="Q.Card.TFrame")
        depth.grid(row=1, column=1, sticky="w", pady=(p(20), 0))
        self.depth_minus = ttk.Button(depth, text="−", width=2, style="Q.TButton", command=lambda: self._adjust_depth(-0.05))
        self.depth_minus.grid(row=0, column=0, padx=(0, p(10)))
        self.depth_entry = ttk.Entry(depth, textvariable=self._depth, width=6, justify="center", style="Q.TEntry", font=self._fonts[5])
        self.depth_entry.grid(row=0, column=1)
        self.depth_entry.bind("<FocusIn>", lambda event: self._set_editing(True))
        self.depth_entry.bind("<FocusOut>", lambda event: self._set_editing(False))
        self.depth_entry.bind("<Return>", self._apply_depth)
        self.depth_entry.bind("<Escape>", self._reset_depth)
        ttk.Label(depth, text="%", style="Q.Muted.TLabel").grid(row=0, column=2, padx=(p(7), p(12)))
        self.depth_plus = ttk.Button(depth, text="+", width=2, style="Q.TButton", command=lambda: self._adjust_depth(0.05))
        self.depth_plus.grid(row=0, column=3, padx=(0, p(12)))
        self.depth_apply = ttk.Button(depth, text="적용", style="Q.Ghost.TButton", command=self._apply_depth)
        self.depth_apply.grid(row=0, column=4)
        self.depth_caption = ttk.Label(controls, style="Q.Muted.TLabel")
        self.depth_caption.grid(row=2, column=1, sticky="w", pady=(p(8), 0))
        comfort_row = ttk.Frame(controls, style="Q.Card.TFrame")
        comfort_row.grid(row=3, column=1, sticky="ew", pady=(p(6), 0))
        comfort_row.columnconfigure(1, weight=1)
        self.comfort_button = ttk.Checkbutton(comfort_row, text="윤곽을 편안하게", variable=self._comfort,
                                             style="Q.TCheckbutton", command=self._toggle_comfort)
        self.comfort_button.grid(row=0, column=0, sticky="w")
        self.comfort_help = ttk.Label(comfort_row, text="가장자리의 양안 차이를 줄입니다.", style="Q.Muted.TLabel", wraplength=p(400))
        self.comfort_help.grid(row=0, column=1, sticky="w", padx=(p(18), 0))

        source = self._card(shell, 3)
        source.columnconfigure(1, weight=1)
        ttk.Label(source, text="보낼 화면", style="Q.Heading.TLabel").grid(row=0, column=0, padx=(0, p(36)), sticky="w")
        self.monitor_combo = ttk.Combobox(source, textvariable=self._monitor, state="readonly", style="Q.TCombobox", width=22, font=self._fonts[0])
        self.monitor_combo.grid(row=0, column=1, sticky="ew")
        self.monitor_combo.bind("<<ComboboxSelected>>", self._select_monitor)
        self.refresh_button = ttk.Button(source, text="새로고침", style="Q.Ghost.TButton", command=self._refresh)
        self.refresh_button.grid(row=0, column=2, padx=(p(10), 0))
        self.source_caption = ttk.Label(source, text="화면 선택은 PC 전송을 중지한 뒤 바꿀 수 있어요.", style="Q.Muted.TLabel")
        self.source_caption.grid(row=1, column=1, columnspan=2, sticky="w", pady=(p(9), 0))

        footer = ttk.Frame(shell, style="Q.TFrame")
        footer.grid(row=4, column=0, sticky="ew", pady=(p(2), 0))
        footer.columnconfigure(0, weight=1)
        self.details_button = ttk.Button(footer, text="화질과 연결 정보", style="Q.TButton", command=self._toggle_details)
        self.details_button.grid(row=0, column=0, sticky="w")
        self.guide_button = ttk.Button(footer, text="사용 방법", style="Q.TButton", command=lambda: self._dispatch("open_guide"))
        self.guide_button.grid(row=0, column=1, sticky="e")
        self.details_panel = ttk.Frame(footer, style="Q.Card.TFrame", padding=p(20))
        self.details_panel.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(p(12), 0))
        self.details_panel.columnconfigure(0, weight=1)
        self.quality_label = ttk.Label(self.details_panel, style="Q.TLabel", wraplength=p(800))
        self.quality_label.grid(row=0, column=0, sticky="ew")
        self.metrics_label = ttk.Label(self.details_panel, style="Q.Muted.TLabel", wraplength=p(800))
        self.metrics_label.grid(row=1, column=0, sticky="ew", pady=(p(6), 0))
        self.connection_detail = ttk.Label(self.details_panel, style="Q.Muted.TLabel", wraplength=p(800))
        self.connection_detail.grid(row=2, column=0, sticky="ew", pady=(p(6), 0))
        support = ttk.Frame(self.details_panel, style="Q.Card.TFrame")
        support.grid(row=3, column=0, sticky="w", pady=(p(16), 0))
        self.logs_button = ttk.Button(support, text="로그 폴더", style="Q.TButton", command=lambda: self._dispatch("open_logs"))
        self.diagnostics_button = ttk.Button(support, text="진단 저장", style="Q.TButton", command=lambda: self._dispatch("export_diagnostics"))
        self.diagnostics_open = ttk.Button(support, text="저장한 진단 열기", style="Q.TButton", command=lambda: self._dispatch("open_diagnostics"))
        for column, button in enumerate((self.logs_button, self.diagnostics_button, self.diagnostics_open)):
            button.grid(row=0, column=column, padx=(0, p(10)), sticky="w")
        self.details_panel.grid_remove()
        self.notice_label = ttk.Label(footer, textvariable=self._notice, style="Q.Notice.TLabel", wraplength=p(800))
        self.notice_label.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(p(10), 0))
        self.notice_label.grid_remove()
        self._notice.trace_add("write", self._notice_changed)
        shell.bind("<Configure>", self._on_resize, add="+")

    def _notice_changed(self, *_):
        if self._notice.get():
            self.notice_label.grid()
        else:
            self.notice_label.grid_remove()
        self.root.after_idle(self._resize_scroll_area)

    def _toggle_details(self):
        self._details_open = not self._details_open
        if self._details_open:
            self.details_panel.grid()
        else:
            self.details_panel.grid_remove()
        self.details_button.configure(text="화질과 연결 정보 닫기" if self._details_open else "화질과 연결 정보")
        self.root.after_idle(self._resize_scroll_area)

    def _toggle_pairing(self):
        self._pairing_open = not self._pairing_open
        if self._pairing_open:
            self.pairing_panel.grid()
        else:
            self.pairing_panel.grid_remove()
            self._pin.set("")
        self.pair_toggle.configure(text="연결 안내 닫기" if self._pairing_open else "새 Quest 연결")
        self.root.after_idle(self._resize_scroll_area)

    def _pair(self, _event=None):
        snapshot = self._snapshot
        if self._queued or snapshot.get("busy") or not snapshot.get("host_running") or not snapshot.get("pairing_available", False):
            return "break"
        pin = self._pin.get().strip()
        if len(pin) != 4 or not pin.isascii() or not pin.isdigit():
            self._notice.set("Quest에 표시된 4자리 숫자를 입력해 주세요.")
            return "break"
        if self._dispatch("pair", pin=pin):
            self._pin.set("")
        return "break"

    def _reveal_focus(self, event):
        widget = event.widget
        if widget is self.root or not isinstance(widget, (ttk.Button, ttk.Entry, ttk.Combobox, ttk.Checkbutton)):
            return
        try:
            # Disclosure rows can change the requested height before Canvas has
            # received its next Configure event. Align its scroll range first.
            self._resize_scroll_area()
            if not widget.winfo_ismapped() or self._shell.winfo_reqheight() <= self._canvas.winfo_height():
                return
            # Keyboard focus remains visible even when advanced rows add height.
            top = widget.winfo_rooty() - self._canvas.winfo_rooty()
            bottom = top + widget.winfo_height()
            visible = self._canvas.winfo_height()
            if top < 0 or bottom > visible:
                content_y = self._canvas.canvasy(0) + top
                self._canvas.yview_moveto(max(0, content_y - self._px(16)) / self._shell.winfo_reqheight())
        except tk.TclError:
            pass

    @staticmethod
    def _enabled(widget, enabled):
        widget.state(["!disabled"] if enabled else ["disabled"])

    def _poll(self):
        if self._destroyed:
            return
        if self._transient_notice:
            self._notice.set("")
            self._transient_notice = False
        self._queued = False
        try:
            snapshot = self.controller.get_snapshot()
            if not isinstance(snapshot, dict):
                raise TypeError("상태 형식이 올바르지 않습니다.")
        except Exception as exc:
            # Unknown ownership must never enable a new start or stop button.
            snapshot = {"phase": "conflict", "busy": True, "message": "PC 상태를 확인하지 못했습니다.", "error": str(exc)}
        self._render(snapshot)
        if not self._destroyed:
            self._after_id = self.root.after(self.POLL_MS, self._poll)

    def _render(self, snapshot):
        previous_error = self._snapshot.get("error")
        self._snapshot = dict(snapshot)
        available = _availability(snapshot, self._queued)
        phase = snapshot.get("phase", "idle")
        titles = {"idle": "입체로 볼 준비가 됐어요", "starting": "화면을 준비하고 있어요",
                  "running": "PC 화면을 보내고 있어요", "stopping": "전송을 마무리하고 있어요",
                  "error": "확인이 필요해요", "conflict": "현재 실행 상태를 확인해 주세요"}
        if phase == "running" and not snapshot.get("host_running"):
            titles["running"] = "PC 시작을 이어갈 수 있어요" if available.start else "PC 준비 상태를 확인하고 있어요"
        self.phase_label.configure(text=titles.get(phase, "PC 상태 확인 중"))
        message = _text(snapshot.get("message"), "PC를 시작한 뒤 Quest 앱에서 연결해 주세요.")
        error = _text(snapshot.get("error"))
        if error and error != previous_error:
            self.root.after_idle(lambda: self._canvas.yview_moveto(0))
        if phase == "running" and snapshot.get("host_running") and not error:
            message = "Quest 앱에서 이 PC에 연결하면 감상을 시작할 수 있어요." if snapshot.get("connected") is not True else "보기 방식과 입체감을 편안하게 조절해 보세요."
        elif phase == "idle" and not error:
            message = "PC 시작을 누른 뒤 Quest 앱에서 이 PC에 연결하세요."
        self.message_label.configure(text=message + ("\n" + error if error and error not in message else ""),
                                     style="Q.Error.TLabel" if error else "Q.Muted.TLabel")
        connection = _text(snapshot.get("connection_text"),
            "Quest 스트림 연결됨" if snapshot.get("connected") is True else
            "Quest 연결 대기" if snapshot.get("connected") is False else "Quest 연결 상태 확인 전")
        address = _text(snapshot.get("pc_address"))
        self.connection_label.configure(text=f"이 PC  {address}" if address else "PC 주소를 확인하고 있어요.")
        self.connection_detail.configure(text=connection)
        badge = "연결됨" if snapshot.get("connected") is True and snapshot.get("host_running") else "연결 대기" if snapshot.get("host_running") else "준비 전"
        if phase in ("starting", "stopping", "checking"):
            badge = "준비 중" if phase != "stopping" else "중지 중"
        elif phase in ("error", "conflict"):
            badge = "확인 필요"
        self.status_badge.configure(text=badge, style="Q.ErrorBadge.TLabel" if phase in ("error", "conflict") else "Q.Badge.TLabel")
        # Keep the intended primary action stable during the local queue debounce.
        actions = _availability(snapshot)
        stopping_action = not actions.start and bool(snapshot.get("running") or snapshot.get("host_running"))
        primary_text = "시작하는 중…" if phase == "starting" else "중지하는 중…" if phase == "stopping" else "PC 중지" if stopping_action else "PC 시작"
        self.primary_button.configure(text=primary_text, style="Q.TButton" if stopping_action else "Q.Primary.TButton")
        self._enabled(self.primary_button, available.stop if stopping_action else available.start)
        if actions.start and actions.stop:
            self.stop_button.grid()
            self._enabled(self.stop_button, available.stop)
        else:
            self.stop_button.grid_remove()
        for mode, button in (("2d", self.mode_2d), ("3d", self.mode_3d)):
            button.configure(style="Q.Selected.TButton" if snapshot.get("mode") == mode else "Q.TButton")
            self._enabled(button, available.control)
        for button in (self.depth_minus, self.depth_plus, self.depth_apply, self.depth_entry, self.comfort_button):
            self._enabled(button, available.control)
        self._sync_depth(snapshot.get("depth_percent"))
        self.depth_caption.configure(text="0.05%씩 미세 조절 · 숫자를 입력해 적용할 수도 있어요.")
        self._comfort.set(snapshot.get("profile") == "comfort")
        self._monitors = _monitor_choices(snapshot.get("monitors"))
        self.monitor_combo.configure(values=tuple(self._monitors))
        selected = snapshot.get("selected_monitor")
        label = next((label for label, device in self._monitors.items() if device == selected), "화면을 선택해 주세요")
        self._monitor.set(label)
        self._enabled(self.monitor_combo, available.monitor and bool(self._monitors))
        self.source_caption.configure(text="화면 선택은 PC 전송을 중지한 뒤 바꿀 수 있어요." if snapshot.get("running") or snapshot.get("host_running") else "Quest에서 감상할 모니터를 선택하세요.")
        self._enabled(self.refresh_button, not snapshot.get("busy") and not self._queued)
        quality = "  ·  ".join(value for value in (_text(snapshot.get("eye_text")), _text(snapshot.get("quality_text"))) if value)
        self.quality_label.configure(text=quality or "화질 정보는 PC를 시작하면 표시됩니다.")
        self.metrics_label.configure(text=_text(snapshot.get("metrics_text"), "처리 상태를 확인하면 여기에 표시합니다."))
        for button in (self.guide_button, self.logs_button, self.diagnostics_button):
            self._enabled(button, not snapshot.get("busy") and not self._queued)
        self._enabled(self.diagnostics_open, bool(snapshot.get("last_diagnostics")) and not snapshot.get("busy") and not self._queued)
        pairing = bool(snapshot.get("host_running") and snapshot.get("pairing_available") and not snapshot.get("busy") and not self._queued)
        self._enabled(self.pin_entry, pairing)
        self._enabled(self.pair_button, pairing)
        self.pair_notice.configure(text=_text(snapshot.get("pairing_message"), "이미 연결한 Quest는 다시 승인할 필요가 없어요." if snapshot.get("host_running") else "PC를 먼저 시작하면 연결을 승인할 수 있어요."))

    def _dispatch(self, action, **kwargs):
        if self._queued:
            return False
        try:
            accepted = bool(self.controller.command(action, **kwargs))
        except Exception as exc:
            self._notice.set(f"요청을 전달하지 못했습니다: {exc}")
            return False
        if accepted:
            self._queued = True
            self._notice.set("요청을 전달했습니다. 적용 상태를 확인하고 있어요.")
        else:
            self._notice.set("현재 작업을 처리하고 있습니다. 잠시 뒤 다시 시도해 주세요.")
        self._transient_notice = True
        self._render(self._snapshot)
        return accepted

    def _primary(self):
        available = _availability(self._snapshot, self._queued)
        if available.start:
            self._dispatch("start")
        elif available.stop:
            self._dispatch("stop")

    def _stop(self):
        if _availability(self._snapshot, self._queued).stop:
            self._dispatch("stop")

    def _refresh(self):
        if not self._snapshot.get("busy") and not self._queued:
            self._dispatch("refresh")

    def _control(self, **kwargs):
        if not _availability(self._snapshot, self._queued).control:
            return False
        return self._dispatch("control", **kwargs)

    def _adjust_depth(self, delta):
        # The backend reads the newest applied depth and performs revision CAS.
        # Never turn an old UI snapshot into an absolute overwrite.
        return self._control(depth_delta=delta)

    def _apply_depth(self, _event=None):
        try:
            value = _parse_depth(self._depth.get())
        except ValueError as exc:
            self._notice.set(str(exc))
            return "break"
        if self._control(depth_percent=value):
            self._depth_dirty = False
        return "break"

    def _depth_changed(self, *_):
        if not self._setting_depth:
            self._depth_dirty = True

    def _set_editing(self, value):
        self._editing_depth = value

    def _sync_depth(self, value, *, force=False):
        if not force and (self._editing_depth or self._depth_dirty):
            return
        self._setting_depth = True
        try:
            self._depth.set(_depth_text(value))
        finally:
            self._setting_depth = False

    def _reset_depth(self, _event=None):
        self._depth_dirty = False
        self._sync_depth(self._snapshot.get("depth_percent"), force=True)
        self._notice.set("")
        return "break"

    def _toggle_comfort(self):
        target = "comfort" if self._comfort.get() else "linear"
        self._comfort.set(self._snapshot.get("profile") == "comfort")
        self._control(profile=target)

    def _select_monitor(self, _event=None):
        if _availability(self._snapshot, self._queued).monitor:
            device = self._monitors.get(self._monitor.get())
            if device and device != self._snapshot.get("selected_monitor"):
                self._dispatch("select_monitor", device_name=device)

    def _on_resize(self, event):
        if event.widget is not self._shell:
            return
        width = max(self._px(360), event.width - self._px(110))
        for label in (self.quality_label, self.metrics_label, self.notice_label, self.connection_detail, self.pair_notice):
            label.configure(wraplength=width)
        self.message_label.configure(wraplength=max(self._px(280), width - self._px(190)))
        self.connection_label.configure(wraplength=width)
        self.comfort_help.configure(wraplength=max(self._px(180), width - self._px(355)))
        self.pair_help.configure(wraplength=width)
        self._resize_scroll_area()

    def _resize_scroll_area(self, _event=None):
        width = self._canvas.winfo_width()
        height = self._canvas.winfo_height()
        needed = self._shell.winfo_reqheight()
        self._canvas.itemconfigure(self._canvas_window, width=width, height=max(height, needed))
        self._canvas.configure(scrollregion=(0, 0, width, max(height, needed)))
        if needed > height:
            self._scrollbar.grid(row=0, column=1, sticky="ns")
        else:
            self._scrollbar.grid_remove()
            self._canvas.yview_moveto(0)

    def _mouse_wheel(self, event):
        if self._shell.winfo_reqheight() <= self._canvas.winfo_height() or not event.delta:
            return
        amount = -int(event.delta / 120) if abs(event.delta) >= 120 else (-1 if event.delta > 0 else 1)
        self._canvas.yview_scroll(amount, "units")
        return "break"

    def _on_unmap(self, event):
        if event.widget is self.root and not self._destroyed and self.root.state() == "iconic":
            self.on_hide()

    def _on_destroy(self, event):
        if event.widget is self.root:
            self.close()

    def close(self):
        """Stop UI polling only; the application's exit callback owns shutdown."""
        self._destroyed = True
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

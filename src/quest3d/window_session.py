"""Select one live HWND for the persistent player; no implicit reselection/input."""
from .window_sources import Win32WindowProvider
from .window_process_capture import ProcessWindowCapture


def validate_window_options(args):
    hwnd = getattr(args, "window", None)
    if hwnd is None:
        if getattr(args, "experimental_window", False):
            raise ValueError("--experimental-window requires --window")
        return
    if type(hwnd) is not int or not 0 < hwnd <= 0x7FFFFFFFFFFFFFFF:
        raise ValueError("--window must be a positive native HWND")
    if getattr(args, "experimental_window", False) is not True:
        raise ValueError("--window requires --experimental-window for the isolated WGC candidate")
    if getattr(args, "bridge_protocol", 2) != 3:
        raise ValueError("--window requires --bridge-protocol 3 to preserve the window source lifetime")
    if any(getattr(args, option, None) for option in ("file", "rect", "inline_rect")):
        raise ValueError("--window cannot be combined with file or desktop/inline regions")
    if getattr(args, "experimental_hdr", False) or getattr(args, "hdr_tonemap", None):
        raise ValueError("Window WGC owns its color policy; desktop HDR flags do not apply")
    if getattr(args, "enable_input", False):
        raise ValueError("Window OS input is not implemented; do not enable monitor input for a window")


def selected_window_capture(args):
    validate_window_options(args)
    provider = Win32WindowProvider(include_titles=False)
    observation = provider.observe(args.window, include_occlusion=False)
    if observation.identity is None or not observation.top_level or observation.excluded:
        raise ValueError("Selected HWND is unavailable or is not an eligible top-level window")
    # The process capture revalidates this exact process/HWND lifetime before
    # accepting pixels and keeps it through minimize/restore/move. It never
    # resolves the number again into a silently substituted source.
    return ProcessWindowCapture(observation.identity, experimental_window=True)

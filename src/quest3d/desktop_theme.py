"""Shared native Tk theme for the desktop window and its dialogs."""
from __future__ import annotations

import ctypes
import os
import tkinter as tk
from tkinter import font as tkfont, ttk

BACKGROUND = '#101216'
PANEL = '#191D24'
RAISED = '#232A34'
TEXT = '#F1F5F9'
MUTED = '#AAB4C3'
ACCENT = '#8FE3CA'
BORDER = '#303947'


def dark_titlebar(root):
    """Keep Windows system controls and accessibility, with a dark caption."""
    if os.name != 'nt':
        return
    try:
        root.update_idletasks()
        user = ctypes.WinDLL('user32')
        user.GetParent.argtypes = [ctypes.c_void_p]
        user.GetParent.restype = ctypes.c_void_p
        hwnd = user.GetParent(root.winfo_id())
        dark = ctypes.c_int(1)
        dwm = ctypes.WinDLL('dwmapi')
        dwm.DwmSetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
        for attribute, color in ((35, 0x00161210), (36, 0x00F9F5F1)):
            value = ctypes.c_uint(color)
            dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
    except (OSError, AttributeError, tk.TclError):
        # Older Windows themes remain usable without this decoration.
        pass


def configure_theme(root):
    style = ttk.Style(root)
    if 'clam' in style.theme_names():
        style.theme_use('clam')
    families = set(tkfont.families(root))
    family = '맑은 고딕' if '맑은 고딕' in families else 'Malgun Gothic' if 'Malgun Gothic' in families else tkfont.nametofont('TkDefaultFont', root=root).actual('family')
    fonts = [tkfont.Font(root=root, family=family, size=size, weight=weight)
             for size, weight in [(11, 'normal'), (25, 'bold'), (12, 'bold'), (10, 'normal'), (19, 'bold'), (17, 'bold')]]
    normal, title, heading, small, status, depth = fonts
    scale = max(1.0, float(root.tk.call('tk', 'scaling')) / (96 / 72))
    px = lambda value: round(value * scale)
    for name, color in [('Q.TFrame', BACKGROUND), ('Q.Card.TFrame', PANEL), ('Q.Raised.TFrame', RAISED)]:
        style.configure(name, background=color, borderwidth=0, relief='flat')
    for name, fg, bg, font in [
        ('Q.TLabel', TEXT, PANEL, normal), ('Q.Muted.TLabel', MUTED, PANEL, small),
        ('Q.Heading.TLabel', TEXT, PANEL, heading), ('Q.Status.TLabel', TEXT, PANEL, status),
        ('Q.Title.TLabel', TEXT, BACKGROUND, title), ('Q.Subtitle.TLabel', MUTED, BACKGROUND, small),
        ('Q.Eyebrow.TLabel', MUTED, PANEL, small), ('Q.Accent.TLabel', ACCENT, PANEL, small),
        ('Q.Badge.TLabel', ACCENT, RAISED, small), ('Q.Error.TLabel', '#FFB9AD', PANEL, normal),
        ('Q.ErrorBadge.TLabel', '#FFB9AD', '#3D2B2B', small),
        ('Q.Notice.TLabel', ACCENT, BACKGROUND, small)]:
        style.configure(name, foreground=fg, background=bg, font=font)
    for name, bg, fg in [('Q.TButton', RAISED, TEXT), ('Q.Primary.TButton', ACCENT, BACKGROUND),
                          ('Q.Selected.TButton', '#29483F', '#BDF7E5'), ('Q.Ghost.TButton', PANEL, MUTED)]:
        style.configure(name, font=normal, padding=(px(17), px(11)), background=bg, foreground=fg,
                        borderwidth=0, relief='flat', focusthickness=1, focuscolor=ACCENT)
        active = '#B0EFDD' if name == 'Q.Primary.TButton' else '#35434F' if name == 'Q.TButton' else '#365C50' if name == 'Q.Selected.TButton' else RAISED
        style.map(name, background=[('disabled', '#20262E'), ('pressed', BORDER), ('active', active)],
                  foreground=[('disabled', '#7F8A9B')], bordercolor=[('focus', ACCENT)])
    style.configure('Q.TEntry', font=normal, fieldbackground=RAISED, foreground=TEXT,
                    insertcolor=ACCENT, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    padding=(px(10), px(10)), selectbackground='#365C50', selectforeground=TEXT)
    style.map('Q.TEntry', fieldbackground=[('disabled', '#20262E')], foreground=[('disabled', '#7F8A9B')],
              bordercolor=[('focus', ACCENT)])
    style.configure('Q.TCombobox', font=normal, fieldbackground=RAISED, background=RAISED,
                    foreground=TEXT, arrowcolor=MUTED, bordercolor=BORDER, lightcolor=BORDER,
                    darkcolor=BORDER, padding=(px(12), px(11)), arrowsize=px(14))
    style.map('Q.TCombobox', fieldbackground=[('readonly', RAISED), ('disabled', '#20262E')],
              foreground=[('disabled', '#7F8A9B'), ('readonly', TEXT)],
              selectbackground=[('readonly', RAISED)], selectforeground=[('readonly', TEXT)],
              bordercolor=[('focus', ACCENT)])
    style.configure('Q.TCheckbutton', background=PANEL, foreground=TEXT, font=normal,
                    padding=(0, px(10)), indicatorbackground=RAISED, indicatorforeground=ACCENT,
                    indicatorsize=px(14), indicatormargin=(0, 0, px(9), 0), focuscolor=ACCENT, borderwidth=0)
    style.map('Q.TCheckbutton', background=[('active', PANEL)], foreground=[('disabled', '#7F8A9B')],
              indicatorbackground=[('selected', '#365C50'), ('disabled', '#20262E')])
    style.configure('Q.TSeparator', background=BORDER)
    style.configure('Q.Vertical.TScrollbar', background=RAISED, troughcolor=BACKGROUND,
                    bordercolor=BACKGROUND, arrowcolor=MUTED, width=px(12), arrowsize=px(10))
    root.option_add('*TCombobox*Listbox.font', normal)
    root.option_add('*TCombobox*Listbox.background', RAISED)
    root.option_add('*TCombobox*Listbox.foreground', TEXT)
    root.option_add('*TCombobox*Listbox.selectBackground', '#365C50')
    root.option_add('*TCombobox*Listbox.selectForeground', TEXT)
    return fonts, scale

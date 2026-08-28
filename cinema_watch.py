#!/usr/bin/env python3
"""LOTTE CINEMA WATCH - 좌석 / 예매오픈 실시간 감시 콘솔.

    python lotte_watch.py

방향키로 지역 -> 지점 -> 영화 -> 요일 -> 상영관 -> 좌석을 골라 감시 조건을 만들고,
조건이 충족되면(원하는 자리가 비거나 예매가 열리면) 알립니다.

표준 라이브러리만 사용합니다 (Python 3.10+).
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import ssl
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta

VERSION = "0.2"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_FILE = os.path.join(APP_DIR, "profiles.json")
FAVORITE_FILE = os.path.join(APP_DIR, "favorites.json")
HISTORY_FILE = os.path.join(APP_DIR, "history.jsonl")
OPENLOG_FILE = os.path.join(APP_DIR, "openlog.jsonl")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
TICKETING_URL = "https://www.lottecinema.co.kr/NLCHS/Ticketing"

# 전역 환경설정 (settings.json 으로 저장). 향후 인증 정보 등도 여기에 확장.
DEFAULT_SETTINGS = {
    "banner": True,        # 시작 화면 브랜드 로고(ASCII 배너) 표시
    "banner_hold": 3.0,    # 정보성 배너를 몇 초 보여줄지
    "sound": True,         # 알림 소리 전역 스위치
    "sound_file": "",      # 알림음 WAV 파일명 (Windows Media 폴더 기준, 빈 값=기본)
}
SETTINGS: dict = dict(DEFAULT_SETTINGS)
WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]

# GetCinemaItems 의 DivisionCode=1 그룹이 지역 구분이다.


# ══════════════════════════════════════════════════════════════════════
#  터미널 기본기 (색상 / 커서 / 키입력 / 폭 계산)
# ══════════════════════════════════════════════════════════════════════
class C:
    """ANSI 색상. 지원하지 않는 터미널에서는 disable() 로 전부 빈 문자열이 된다."""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    BRED = "\033[91m"
    BGREEN = "\033[92m"
    BYELLOW = "\033[93m"
    BCYAN = "\033[96m"
    BWHITE = "\033[97m"
    ON_RED = "\033[41m"
    ON_BLUE = "\033[44m"

    @classmethod
    def disable(cls) -> None:
        for name in dir(cls):
            if name.isupper():
                setattr(cls, name, "")


class SwitchBrand(Exception):
    """메뉴에서 '영화관 변경' 을 고르면 브랜드 선택 화면으로 돌아간다."""


class BackToMenu(Exception):
    """어느 화면에서든 'm'(또는 텍스트 입력에서 ':m')을 누르면 첫 메뉴로 돌아간다."""


ANSI_RE = re.compile(r"\033\[[0-9;]*m")
UI = {"ansi": True, "unicode": True, "tty": True, "width": 74}


def setup_terminal() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    try:
        if not sys.stdin.isatty():
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    UI["tty"] = sys.stdout.isatty() and sys.stdin.isatty()
    if os.name == "nt":                      # Windows 콘솔 VT(ANSI) 활성화
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)   # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:
            UI["ansi"] = False
    if not UI["tty"] or os.environ.get("NO_COLOR"):
        UI["ansi"] = False
    if not UI["ansi"]:
        C.disable()
    try:                                      # 박스문자 출력 가능한지 확인
        "─│┌┐└┘├┤❯".encode(sys.stdout.encoding or "utf-8")
    except Exception:
        UI["unicode"] = False
    UI["width"] = max(64, min(shutil.get_terminal_size((80, 25)).columns - 2, 100))


def dwidth(text: str) -> int:
    """한글=2칸 기준 표시 폭 (ANSI 코드는 제외)."""
    plain = ANSI_RE.sub("", text)
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in plain)


def trim(text: str, width: int) -> str:
    if dwidth(text) <= width:
        return text
    out, used = "", 0
    for ch in ANSI_RE.sub("", text):
        w = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used + w > width - 1:
            return out + "…"
        out += ch
        used += w
    return out


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - dwidth(text))


def cell(text: str, width: int) -> str:
    return pad(trim(text, width), width)


def two_col(left: str, right: str, right_width: int = 22) -> str:
    """목록 한 줄을 '왼쪽 고정폭 + 오른쪽 설명' 두 칸으로 정렬한다.

    왼쪽 값이 길면 잘라내므로 오른쪽 열이 밀리지 않는다.
    """
    avail = UI["width"] - 10 - right_width
    return f"{cell(left, max(avail, 12))}{C.DIM}{right}{C.RESET}"


def starred(left: str, right: str, favorite: bool | None = None,
            right_width: int = 22) -> str:
    """즐겨찾기 별표 칸(2칸)을 앞에 붙인 두 칸 목록 줄."""
    if favorite is None:
        mark = ""
    else:
        mark = f"{C.BYELLOW}{glyph('★', '*')}{C.RESET} " if favorite else "  "
    avail = UI["width"] - 10 - right_width - (2 if favorite is not None else 0)
    return f"{mark}{cell(left, max(avail, 12))}{C.DIM}{right}{C.RESET}"


# 화면 제어 ------------------------------------------------------------
def hide_cursor() -> None:
    if UI["ansi"]:
        sys.stdout.write("\033[?25l")


def show_cursor() -> None:
    if UI["ansi"]:
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


def clear_screen() -> None:
    if UI["ansi"]:
        sys.stdout.write("\033[2J\033[H")
    else:
        print("\n" * 3)


def paint(lines: list[str]) -> None:
    """커서를 홈으로 옮겨 화면을 덮어쓴다 (스크롤 없이 제자리 갱신)."""
    if not UI["ansi"]:
        print("\n".join(lines), flush=True)
        return
    buf = ["\033[H"]
    for line in lines:
        buf.append(line + "\033[K\n")
    buf.append("\033[J")
    sys.stdout.write("".join(buf))
    sys.stdout.flush()


# 키 입력 --------------------------------------------------------------
if os.name == "nt":
    def read_key() -> str:
        import msvcrt
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            code = msvcrt.getwch()
            return {"H": "up", "P": "down", "K": "left", "M": "right",
                    "I": "pgup", "Q": "pgdn", "G": "home", "O": "end"}.get(code, "")
        if ch in ("\r", "\n"):
            return "enter"
        if ch == " ":
            return "space"
        if ch == "\x1b":
            return "esc"
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch in ("\x08", "\x7f"):
            return "backspace"
        return "char:" + ch
else:
    def read_key() -> str:
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                seq = sys.stdin.read(2)
                if seq == "[A":
                    return "up"
                if seq == "[B":
                    return "down"
                if seq == "[5":
                    sys.stdin.read(1)
                    return "pgup"
                if seq == "[6":
                    sys.stdin.read(1)
                    return "pgdn"
                if seq == "[H":
                    return "home"
                if seq == "[F":
                    return "end"
                return "esc"
            if ch in ("\r", "\n"):
                return "enter"
            if ch == " ":
                return "space"
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch in ("\x08", "\x7f"):
                return "backspace"
            return "char:" + ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ══════════════════════════════════════════════════════════════════════
#  UI 위젯
# ══════════════════════════════════════════════════════════════════════
def glyph(uni: str, ascii_: str) -> str:
    return uni if UI["unicode"] else ascii_


def box_top(title: str = "", width: int | None = None, color: str | None = None) -> str:
    w = width or UI["width"]
    color = C.CYAN if color is None else color
    tl, hz, tr = glyph("┌", "+"), glyph("─", "-"), glyph("┐", "+")
    if title:
        label = f" {title} "
        left = 2
        rest = w - 2 - left - dwidth(label)
        return f"{color}{tl}{hz * left}{C.RESET}{C.BOLD}{label}{C.RESET}{color}{hz * max(rest, 0)}{tr}{C.RESET}"
    return f"{color}{tl}{hz * (w - 2)}{tr}{C.RESET}"


def box_mid(width: int | None = None, color: str | None = None) -> str:
    w = width or UI["width"]
    color = C.CYAN if color is None else color
    return f"{color}{glyph('├', '+')}{glyph('─', '-') * (w - 2)}{glyph('┤', '+')}{C.RESET}"


def box_bottom(width: int | None = None, color: str | None = None) -> str:
    w = width or UI["width"]
    color = C.CYAN if color is None else color
    return f"{color}{glyph('└', '+')}{glyph('─', '-') * (w - 2)}{glyph('┘', '+')}{C.RESET}"


def box_row(text: str = "", width: int | None = None, color: str | None = None) -> str:
    w = width or UI["width"]
    color = C.CYAN if color is None else color
    v = glyph("│", "|")
    return f"{color}{v}{C.RESET} {cell(text, w - 4)} {color}{v}{C.RESET}"


BANNER = r"""
 ██╗      ██████╗ ████████╗████████╗███████╗
 ██║     ██╔═══██╗╚══██╔══╝╚══██╔══╝██╔════╝
 ██║     ██║   ██║   ██║      ██║   █████╗
 ██║     ██║   ██║   ██║      ██║   ██╔══╝
 ███████╗╚██████╔╝   ██║      ██║   ███████╗
 ╚══════╝ ╚═════╝    ╚═╝      ╚═╝   ╚══════╝
"""

# 프로그램 이름 배너 - 브랜드 선택 전 첫 화면에 뜬다 (특정 영화관 로고 대신).
PROGRAM_BANNER = r"""
 ██████╗██╗███╗   ██╗███████╗███╗   ███╗ █████╗
██╔════╝██║████╗  ██║██╔════╝████╗ ████║██╔══██╗
██║     ██║██╔██╗ ██║█████╗  ██╔████╔██║███████║
██║     ██║██║╚██╗██║██╔══╝  ██║╚██╔╝██║██╔══██║
╚██████╗██║██║ ╚████║███████╗██║ ╚═╝ ██║██║  ██║
 ╚═════╝╚═╝╚═╝  ╚═══╝╚══════╝╚═╝     ╚═╝╚═╝  ╚═╝
██╗    ██╗ █████╗ ████████╗ ██████╗██╗  ██╗
██║    ██║██╔══██╗╚══██╔══╝██╔════╝██║  ██║
██║ █╗ ██║███████║   ██║   ██║     ███████║
██║███╗██║██╔══██║   ██║   ██║     ██╔══██║
╚███╔███╔╝██║  ██║   ██║   ╚██████╗██║  ██║
 ╚══╝╚══╝ ╚═╝  ╚═╝   ╚═╝    ╚═════╝╚═╝  ╚═╝
"""

# 브랜드별 배너 - 첫 화면에서 고른 브랜드의 로고가 뜬다.
BRAND_BANNERS: dict[str, str] = {
    "lotte": r"""
 ██╗      ██████╗ ████████╗████████╗███████╗
 ██║     ██╔═══██╗╚══██╔══╝╚══██╔══╝██╔════╝
 ██║     ██║   ██║   ██║      ██║   █████╗
 ██║     ██║   ██║   ██║      ██║   ██╔══╝
 ███████╗╚██████╔╝   ██║      ██║   ███████╗
 ╚══════╝ ╚═════╝    ╚═╝      ╚═╝   ╚══════╝
""",
    "megabox": r"""
 ███╗   ███╗███████╗ ██████╗  █████╗
 ████╗ ████║██╔════╝██╔════╝ ██╔══██╗
 ██╔████╔██║█████╗  ██║  ███╗███████║
 ██║╚██╔╝██║██╔══╝  ██║   ██║██╔══██║
 ██║ ╚═╝ ██║███████╗╚██████╔╝██║  ██║
 ╚═╝     ╚═╝╚══════╝ ╚═════╝ ╚═╝  ╚═╝
""",
    "cgv": r"""
  ██████╗ ██████╗ ██╗   ██╗
 ██╔════╝██╔════╝ ██║   ██║
 ██║     ██║  ███╗██║   ██║
 ██║     ██║   ██║╚██╗ ██╔╝
 ╚██████╗╚██████╔╝ ╚████╔╝
  ╚═════╝ ╚═════╝   ╚═══╝
""",
}

BRAND_SUBTITLE = {"app": "C I N E M A   W A T C H",
                  "lotte": "L O T T E   C I N E M A   W A T C H",
                  "megabox": "M E G A B O X   W A T C H",
                  "cgv": "C G V   W A T C H"}

BANNER_ASCII = r"""
  _     ___ _____ _____ _____
 | |   / _ \_   _|_   _| ____|
 | |  | | | || |   | | |  _|
 | |__| |_| || |   | | | |___
 |_____\___/ |_|   |_| |_____|
"""

TIPS = [
    "취소표는 상영 1~2시간 전과 예매 마감 직전에 가장 많이 나옵니다.",
    "상영관과 요일을 좁힐수록 확인 주기가 빨라집니다.",
    "좌석표에서 통로는 빈칸으로 표시됩니다. 번호를 보고 고르세요.",
    "설정은 저장해 두면 다음 실행에서 바로 재시작할 수 있습니다.",
    "텔레그램 알림을 켜두면 자리를 비운 사이에도 놓치지 않습니다.",
]


# 현재 선택된 브랜드 (배너·헤더 색에 쓰인다)
ACTIVE = {"code": "lotte", "name": "CINEMA WATCH", "color": C.BRED}


def brand_art(code: str) -> str:
    if not UI["unicode"]:
        return BANNER_ASCII
    if code == "app":
        return PROGRAM_BANNER
    return BRAND_BANNERS.get(code) or BANNER


def print_banner(sub_lines: list[str], brand: str | None = None,
                 subtitle: str = "좌석 · 예매오픈 감시 콘솔", hold: bool = False) -> None:
    clear_screen()
    code = brand or ACTIVE["code"]
    if code in PROVIDERS:
        color = PROVIDERS[code].color()
    elif code == "app":
        color = C.BCYAN
    else:
        color = C.BRED
    if SETTINGS.get("banner", True):
        for line in brand_art(code).strip("\n").splitlines():
            print(f"{color}{C.BOLD}{line}{C.RESET}")
    print(f"{color}{C.BOLD}      {BRAND_SUBTITLE.get(code, 'C I N E M A   W A T C H')}"
          f"{C.RESET}{C.DIM}      {subtitle}{C.RESET}\n")
    inner = 46
    print(f"       {C.DIM}=[{C.RESET} {C.BWHITE}"
          f"{cell('cinema-watch v' + VERSION, inner)}{C.RESET}{C.DIM}]{C.RESET}")
    for line in sub_lines:
        print(f"{C.DIM}+ -- --=[{C.RESET} {cell(line, inner)}{C.DIM}]{C.RESET}")
    print(f"\n{C.DIM}   TIP  {random.choice(TIPS)}{C.RESET}\n")
    if hold and UI["tty"] and SETTINGS.get("banner", True):
        try:
            secs = float(SETTINGS.get("banner_hold", 0) or 0)
        except (TypeError, ValueError):
            secs = 0.0
        if secs > 0:
            time.sleep(secs)


def header(title: str, crumbs: list[str] | None = None) -> list[str]:
    line = (f"{ACTIVE['color']}{C.BOLD}{ACTIVE['name']}{C.RESET}  "
            f"{C.DIM}v{VERSION}{C.RESET}")
    out = [line]
    if crumbs:
        out.append(f"{C.DIM}   " + f" {glyph('›', '>')} ".join(crumbs) + f"{C.RESET}")
    out.append("")
    out.append(box_top(title))
    return out


def keyhint(*pairs: tuple[str, str]) -> str:
    parts = [f"{C.BOLD}{k}{C.RESET} {C.DIM}{v}{C.RESET}" for k, v in pairs]
    return "   " + "   ".join(parts)


class Chooser:
    """방향키로 고르는 목록. tty 가 아니면 번호 입력 방식으로 대체된다."""

    PAGE = 12

    def __init__(self, title: str, items: list, labeler, crumbs: list[str] | None = None,
                 multi: bool = False, hint: str = "", extra: tuple[str, object] | None = None,
                 preselect: set[int] | None = None, empty_means_all: bool = True):
        self.title = title
        self.items = items
        self.labeler = labeler
        self.crumbs = crumbs or []
        self.multi = multi
        self.hint = hint
        self.extra = extra              # (표시문구, 반환값) - 목록 맨 아래 추가 항목
        self.empty_means_all = empty_means_all
        self.filter = ""
        self.cursor = 0
        self.top = 0
        self.selected: set[int] = set(preselect or ())

    # -- 목록 --------------------------------------------------------
    def view(self) -> list[int]:
        if not self.filter:
            return list(range(len(self.items)))
        key = self.filter.replace(" ", "")
        return [i for i, it in enumerate(self.items)
                if key in ANSI_RE.sub("", self.labeler(it)).replace(" ", "")]

    def rows(self) -> list[str]:
        view = self.view()
        total = len(view) + (1 if self.extra else 0)
        self.cursor = max(0, min(self.cursor, max(total - 1, 0)))
        if self.cursor < self.top:
            self.top = self.cursor
        if self.cursor >= self.top + self.PAGE:
            self.top = self.cursor - self.PAGE + 1

        out: list[str] = []
        w = UI["width"] - 4
        end = min(self.top + self.PAGE, total)
        if self.top > 0:
            out.append(box_row(f"{C.DIM}   {glyph('▲', '^')} 위로 {self.top}개 더{C.RESET}"))
        for pos in range(self.top, end):
            focused = pos == self.cursor
            if self.extra and pos == len(view):
                label, mark = f"{C.YELLOW}{self.extra[0]}{C.RESET}", "  "
            else:
                idx = view[pos]
                label = self.labeler(self.items[idx])
                if self.multi:
                    on = idx in self.selected
                    mark = f"{C.BGREEN}{glyph('◉', '[x]')}{C.RESET} " if on \
                        else f"{C.DIM}{glyph('○', '[ ]')}{C.RESET} "
                else:
                    mark = ""
            arrow = f"{C.BGREEN}{C.BOLD}{glyph('❯', '>')}{C.RESET} " if focused else "  "
            text = f"{arrow}{mark}{label}"
            if focused:
                text = f"{arrow}{mark}{C.BOLD}{C.BWHITE}{ANSI_RE.sub('', label)}{C.RESET}" \
                    if not self.multi else text
            out.append(box_row(trim(text, w)))
        left = total - end
        if left > 0:
            out.append(box_row(f"{C.DIM}   {glyph('▼', 'v')} 아래로 {left}개 더{C.RESET}"))
        if not total:
            out.append(box_row(f"{C.DIM}   결과 없음 - Esc 로 검색어를 지우세요{C.RESET}"))
        return out

    def render(self) -> list[str]:
        title = self.title
        if self.filter:
            title += f"   [검색: {self.filter}]"
        lines = header(title, self.crumbs)
        lines += self.rows()
        lines.append(box_bottom())
        if self.hint:
            lines.append(f"{C.DIM}   {self.hint}{C.RESET}")
        if self.multi:
            lines.append(keyhint((glyph("↑↓", "up/dn"), "이동"), ("Space", "선택"),
                                 ("a", "전체"), ("Enter", "확인"), ("/", "검색"),
                                 ("m", "처음으로"), ("q", "종료")))
        else:
            lines.append(keyhint((glyph("↑↓", "up/dn"), "이동"), ("Enter", "선택"),
                                 ("/", "검색"), ("m", "처음으로"), ("q", "종료")))
        return lines

    # -- 실행 --------------------------------------------------------
    def run(self):
        if not UI["tty"]:
            return self._fallback()
        hide_cursor()
        try:
            while True:
                paint(self.render())
                key = read_key()
                view = self.view()
                total = len(view) + (1 if self.extra else 0)
                if key == "up":
                    self.cursor -= 1
                elif key == "down":
                    self.cursor += 1
                elif key == "pgup":
                    self.cursor -= self.PAGE
                elif key == "pgdn":
                    self.cursor += self.PAGE
                elif key == "home":
                    self.cursor = 0
                elif key == "end":
                    self.cursor = total - 1
                elif key == "space" and self.multi and total:
                    if not (self.extra and self.cursor == len(view)):
                        idx = view[self.cursor]
                        self.selected.symmetric_difference_update({idx})
                elif key == "char:a" and self.multi:
                    if len(self.selected) == len(self.items):
                        self.selected.clear()
                    else:
                        self.selected = set(range(len(self.items)))
                elif key == "char:/":
                    show_cursor()
                    paint(self.render() + [""])
                    self.filter = read_line(f"   {C.CYAN}검색{C.RESET} > ")
                    self.cursor = self.top = 0
                    hide_cursor()
                elif key == "esc":
                    self.filter = ""
                    self.cursor = self.top = 0
                elif key == "backspace":
                    self.filter = self.filter[:-1]
                elif key in ("char:m", "char:M"):
                    raise BackToMenu
                elif key in ("char:q", "char:Q"):
                    raise SystemExit(0)
                elif key == "enter":
                    if self.multi:
                        chosen = [self.items[i] for i in sorted(self.selected)]
                        if chosen:
                            return chosen
                        if not self.empty_means_all:    # 즐겨찾기 편집: 빈 선택도 유효
                            return []
                        if total:                       # 아무것도 안 고르면 전체
                            return list(self.items)
                    elif total:
                        if self.extra and self.cursor == len(view):
                            return self.extra[1]
                        return self.items[view[self.cursor]]
                self.cursor = max(0, min(self.cursor, max(total - 1, 0)))
        finally:
            show_cursor()

    def _fallback(self):
        """파이프 입력(테스트/스크립트)용 - 번호 또는 검색어를 한 줄로 받는다."""
        while True:
            print("\n" + "\n".join(ANSI_RE.sub("", l) for l in self.render()))
            raw = read_line("번호 입력 (콤마=다중, 문자=검색): ")
            if raw.lower() in ("q", "quit"):
                raise SystemExit(0)
            if raw.lower() in ("m", ":m"):
                raise BackToMenu
            view = self.view()
            if raw and all(c.isdigit() or c in ", " for c in raw):
                idxs = [int(x) for x in raw.replace(" ", ",").split(",") if x.strip()]
                picks = [self.items[view[i - 1]] for i in idxs if 1 <= i <= len(view)]
                if picks:
                    return picks if self.multi else picks[0]
            elif not raw and self.multi:
                return list(self.items)
            elif raw:
                self.filter = raw
                if not self.view():          # 검색 결과가 없으면 필터를 되돌린다
                    print(f"  '{raw}' 검색 결과가 없습니다.")
                    self.filter = ""


def read_line(prompt: str) -> str:
    try:
        return input(prompt).encode("utf-8", "replace").decode("utf-8", "replace").strip()
    except EOFError:
        raise SystemExit(0)


def form(title: str, fields: list[str], crumbs: list[str] | None = None) -> None:
    """입력 폼의 머리말을 그린다 (입력 자체는 한 줄씩 받는다)."""
    lines = header(title, crumbs)
    for text in fields:
        lines.append(box_row(text))
    lines.append(box_bottom())
    clear_screen()
    print("\n".join(lines))


def ask_text(label: str, default: str = "", note: str = "") -> str:
    hint = f" {C.DIM}[{default}]{C.RESET}" if default else ""
    if note:
        print(f"   {C.DIM}{note}{C.RESET}")
    val = read_line(f"   {C.CYAN}{glyph('❯', '>')}{C.RESET} {label}{hint}"
                    f" {C.DIM}(:m 처음으로){C.RESET}: ")
    if val.strip().lower() == ":m":
        raise BackToMenu
    return val or default


def ask_int(label: str, default: int, lo: int, hi: int) -> int:
    while True:
        raw = ask_text(label, str(default))
        if raw.isdigit() and lo <= int(raw) <= hi:
            return int(raw)
        print(f"   {C.YELLOW}{lo}~{hi} 사이 숫자를 입력하세요.{C.RESET}")


def ask_yes(question: str, default: bool = True) -> bool:
    opts = [("예", True), ("아니오", False)]
    if not default:
        opts.reverse()
    return Chooser(question, opts, lambda o: o[0]).run()[1]


# ══════════════════════════════════════════════════════════════════════
#  API
# ══════════════════════════════════════════════════════════════════════
class LotteApiError(RuntimeError):
    pass


class Api:
    """롯데시네마 내부 API(LCWS). multipart 의 paramList 필드에 JSON 한 덩어리."""

    BASE = "https://www.lottecinema.co.kr/LCWS"
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

    def __init__(self, min_interval: float = 0.6, timeout: float = 15.0, retries: int = 3):
        self.min_interval = min_interval
        self.timeout = timeout
        self.retries = retries
        self._lock = threading.Lock()
        self._last = 0.0
        self.calls = 0                      # 실제 발생한 HTTP 요청 수 (대시보드 표시용)
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def _throttle(self) -> None:
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait + random.uniform(0, 0.12))
            self._last = time.monotonic()

    def call(self, path: str, method: str, **params) -> dict:
        payload = {"MethodName": method, "channelType": "HO",
                   "osType": "Windows", "osVersion": "Chrome"}
        payload.update({k: v for k, v in params.items() if v is not None})
        boundary = "----LCWS" + uuid.uuid4().hex
        body = (f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="paramList"\r\n\r\n'
                f"{json.dumps(payload, ensure_ascii=False)}\r\n"
                f"--{boundary}--\r\n").encode("utf-8")
        req = urllib.request.Request(
            self.BASE + path, data=body, method="POST",
            headers={"User-Agent": self.UA,
                     "Content-Type": f"multipart/form-data; boundary={boundary}",
                     "Accept": "application/json, text/plain, */*",
                     "Accept-Language": "ko-KR,ko;q=0.9",
                     "Origin": "https://www.lottecinema.co.kr",
                     "Referer": TICKETING_URL})
        last_err: Exception | None = None
        for attempt in range(self.retries):
            self._throttle()
            self.calls += 1
            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
            except Exception as exc:
                last_err = exc
                time.sleep(min(2 ** attempt, 8) + random.uniform(0, 0.4))
                continue
            if str(data.get("IsOK", "true")).lower() == "false":
                raise LotteApiError(f"{method}: {data.get('ResultMessage')}")
            return data
        raise LotteApiError(f"{method}: 요청 실패 ({last_err})")

    def cinemas(self) -> list[dict]:
        return self.call("/Cinema/CinemaData.aspx", "GetCinemaItems")["Cinemas"]["Items"]

    def showtimes(self, cinema_key: str, play_date: str) -> list[dict]:
        data = self.call("/Ticketing/TicketingData.aspx", "GetPlaySequence",
                         playDate=play_date, cinemaID=cinema_key, representationMovieCode="")
        return (data.get("PlaySeqs") or {}).get("Items") or []

    def seats(self, cinema_id: int, screen_id: int, play_seq: int, play_date: str) -> dict:
        return self.call("/Ticketing/TicketingData.aspx", "GetSeats",
                         playDate=play_date, cinemaID=str(cinema_id),
                         screenID=str(screen_id), playSequence=str(play_seq))


# ══════════════════════════════════════════════════════════════════════
#  모델
# ══════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Cinema:
    cinema_id: str          # 브랜드별 지점 코드 (롯데 1016, 메가박스 1372, CGV 0056)
    name: str
    region_code: str
    key: str = ""           # 상영표 조회에 쓰는 브랜드별 식별자
    addr: str = ""
    region_name: str = ""
    brand: str = "lotte"


@dataclass(frozen=True)
class Show:
    cinema_id: int
    cinema_name: str
    movie_name: str
    movie_code: str
    screen_id: int
    screen_name: str
    play_date: str
    play_seq: int
    start: str
    end: str
    total_seats: int
    open_seats: int          # API 의 BookingSeatCount = 예매 가능(잔여) 좌석 수
    film: str
    bookable: bool

    @property
    def key(self) -> str:
        return f"{self.play_date}|{self.screen_id}|{self.play_seq}|{self.movie_code}"

    @property
    def weekday(self) -> int:
        return datetime.strptime(self.play_date, "%Y-%m-%d").weekday()

    def short(self) -> str:
        return (f"{self.play_date[5:]}({WEEKDAY_KR[self.weekday]}) {self.start} "
                f"{self.screen_name}")

    def label(self) -> str:
        return (f"{self.play_date}({WEEKDAY_KR[self.weekday]}) {self.start}  "
                f"{self.movie_name}  {self.screen_name} [{self.film}]  "
                f"잔여 {self.open_seats}/{self.total_seats}")

    @staticmethod
    def parse(d: dict) -> "Show":
        return Show(
            cinema_id=int(d.get("CinemaID") or 0),
            cinema_name=(d.get("CinemaNameKR") or "").strip(),
            movie_name=(d.get("MovieNameKR") or "").strip(),
            movie_code=str(d.get("RepresentationMovieCode") or d.get("MovieCode") or ""),
            screen_id=int(d.get("ScreenID") or 0),
            screen_name=(d.get("ScreenNameKR") or "").strip(),
            play_date=d.get("PlayDt") or "",
            play_seq=int(d.get("PlaySequence") or 0),
            start=d.get("StartTime") or "",
            end=d.get("EndTime") or "",
            total_seats=int(d.get("TotalSeatCount") or 0),
            open_seats=int(d.get("BookingSeatCount") or 0),
            film=(d.get("FilmNameKR") or "").strip(),
            bookable=str(d.get("IsBookingYN") or "Y").upper() == "Y")


@dataclass
class SeatMap:
    """SeatStatusCode 0=예매가능, 50=예매완료."""
    available: set[str] = field(default_factory=set)
    taken: set[str] = field(default_factory=set)
    rows: list[str] = field(default_factory=list)
    grid: dict[str, list[tuple[int, bool]]] = field(default_factory=dict)
    coord: dict[str, tuple[int, int]] = field(default_factory=dict)   # 라벨 -> (X, Y)
    total: int = 0

    @staticmethod
    def parse(d: dict) -> "SeatMap":
        sold = {str(b.get("SeatNo") or "").strip()
                for b in (d.get("BookingSeats") or {}).get("Items", [])}
        sm = SeatMap()
        for s in (d.get("Seats") or {}).get("Items", []):
            row = str(s.get("ShowSeatRow") or s.get("SeatRow") or "").strip().upper()
            col = int(s.get("ShowSeatColumn") or s.get("SeatColumn") or 0)
            if not row or not col:
                continue
            no = str(s.get("SeatNo") or "").strip()
            ok = int(s.get("SeatStatusCode") or 0) == 0 and no not in sold
            label = f"{row}{col}"
            (sm.available if ok else sm.taken).add(label)
            sm.grid.setdefault(row, []).append((col, ok))
            x, y = s.get("SeatXCoordinate"), s.get("SeatYCoordinate")
            if x is not None and y is not None:
                sm.coord[label] = (int(x), int(y))
        for row in sm.grid:
            sm.grid[row].sort()
        sm.rows = sorted(sm.grid)
        sm.total = len(sm.available) + len(sm.taken)
        return sm


# ══════════════════════════════════════════════════════════════════════
#  좌석 유틸
# ══════════════════════════════════════════════════════════════════════
_SEAT_RE = re.compile(r"^([A-Za-z]{1,2})\s*0*(\d{1,3})$")


def normalize_seat(token: str) -> str:
    m = _SEAT_RE.match(token.strip())
    if not m:
        raise ValueError(f"좌석 표기를 알 수 없습니다: {token!r} (예: G22)")
    return f"{m.group(1).upper()}{int(m.group(2))}"


def parse_seat_groups(text: str) -> list[list[str]]:
    """'G22,G23 | F22,F23' -> [['G22','G23'], ['F22','F23']]  (그룹끼리는 OR)"""
    text = text.replace("또는", "|").replace("or", "|").replace("/", "|").replace(";", "|")
    groups = []
    for chunk in text.split("|"):
        seats = [normalize_seat(t) for t in chunk.replace(" ", ",").split(",") if t.strip()]
        if seats:
            groups.append(seats)
    if not groups:
        raise ValueError("좌석이 하나도 입력되지 않았습니다.")
    return groups


def seat_sort_key(label: str):
    m = _SEAT_RE.match(label)
    return (m.group(1), int(m.group(2))) if m else (label, 0)


SWEET_DEPTH = 0.62      # 스크린에서 뒤로 62% 지점이 세로 기준 명당
SWEET_SIDE_WEIGHT = 1.0  # 좌우 중앙 이탈 가중치
SWEET_DEPTH_WEIGHT = 0.85


def sweet_scores(smap: SeatMap) -> dict[str, float]:
    """좌석 좌표로 '명당 점수'(0~1, 클수록 좋음)를 매긴다.

    API 의 SweetSpotYN 은 전 좌석이 'N' 이라 쓸 수 없어서 좌표로 직접 계산한다.
    X 는 좌우, Y 는 스크린에서 멀어지는 방향(A열이 최소값)임을 실측으로 확인했다.
    기준은 '좌우 정중앙 · 앞에서 62% 뒤'.
    """
    if not smap.coord:
        return {}
    xs = [x for x, _ in smap.coord.values()]
    ys = [y for _, y in smap.coord.values()]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    dx, dy = (x1 - x0) or 1, (y1 - y0) or 1
    raw: dict[str, float] = {}
    for label, (x, y) in smap.coord.items():
        nx = (x - x0) / dx
        ny = (y - y0) / dy
        dist = (((nx - 0.5) * SWEET_SIDE_WEIGHT) ** 2
                + ((ny - SWEET_DEPTH) * SWEET_DEPTH_WEIGHT) ** 2) ** 0.5
        raw[label] = dist
    worst = max(raw.values()) or 1.0
    return {label: 1.0 - d / worst for label, d in raw.items()}


def sweet_seats(smap: SeatMap, ratio: float = 0.2) -> set[str]:
    """명당 상위 ratio 비율의 좌석 집합."""
    scores = sweet_scores(smap)
    if not scores:
        return set()
    ranked = sorted(scores, key=lambda k: -scores[k])
    keep = max(1, round(len(ranked) * max(min(ratio, 1.0), 0.01)))
    return set(ranked[:keep])


def find_consecutive(smap: SeatMap, n: int, rows: list[str] | None = None,
                     allowed: set[str] | None = None) -> list[list[str]]:
    """같은 열에서 번호가 이어지는 n석 이상 블록 (통로로 끊기면 별개).

    allowed 를 주면 그 집합 안의 좌석만 이어붙인다(명당 영역 탐색용).
    """
    blocks: list[list[str]] = []
    for row in smap.rows:
        if rows and row not in rows:
            continue
        streak: list[str] = []
        for col, ok in smap.grid[row]:
            if allowed is not None and f"{row}{col}" not in allowed:
                ok = False
            if ok and streak and int(streak[-1][1:]) == col - 1:
                streak.append(f"{row}{col}")
            elif ok:
                if len(streak) >= n:
                    blocks.append(streak)
                streak = [f"{row}{col}"]
            else:
                if len(streak) >= n:
                    blocks.append(streak)
                streak = []
        if len(streak) >= n:
            blocks.append(streak)
    blocks.sort(key=lambda b: -len(b))
    return blocks


def seatmap_lines(smap: SeatMap, sweet: set[str] | None = None) -> list[str]:
    """좌석 번호에 맞춰 정렬해 그린다 (통로는 빈칸). sweet 를 주면 명당을 따로 표시."""
    cols = [c for row in smap.rows for c, _ in smap.grid[row]]
    if not cols:
        return ["(좌석 정보 없음)"]
    lo, hi = min(cols), max(cols)
    span = range(lo, hi + 1)
    tens = "".join(str(c // 10) if c % 5 == 0 and c >= 10 else " " for c in span)
    ones = "".join(str(c % 10) if c % 5 == 0 else "." for c in span)
    out = [f"{C.DIM}     {tens}{C.RESET}", f"{C.DIM}     {ones}{C.RESET}"]
    for row in smap.rows:
        seats = dict(smap.grid[row])
        cells = ""
        for c in span:
            if c not in seats:
                cells += " "
                continue
            label = f"{row}{c}"
            if not seats[c]:
                cells += f"{C.DIM}{glyph('·', 'X')}{C.RESET}"
            elif sweet and label in sweet:
                cells += f"{C.BYELLOW}{glyph('◆', '@')}{C.RESET}"
            else:
                cells += f"{C.BGREEN}{glyph('■', 'O')}{C.RESET}"
        out.append(f"{C.BOLD}{row:<2}{C.RESET}   {cells}")
    legend = (f"{C.DIM}   {glyph('■', 'O')} 예매가능 {len(smap.available)}석   "
              f"{glyph('·', 'X')} 불가 {len(smap.taken)}석   (숫자=좌석번호){C.RESET}")
    if sweet:
        legend = (f"{C.DIM}   {glyph('◆', '@')} 명당 {len(sweet & smap.available)}석 가능   "
                  f"{glyph('■', 'O')} 그 외 가능   {glyph('·', 'X')} 불가"
                  f"   (숫자=좌석번호){C.RESET}")
    out.append(legend)
    return out


# ══════════════════════════════════════════════════════════════════════
#  설정
# ══════════════════════════════════════════════════════════════════════
@dataclass
class Watch:
    mode: str = "seat"                    # seat | open | radar
    brand: str = "lotte"                  # lotte | megabox | cgv
    profile_name: str = ""
    cinema_id: str = ""
    cinema_name: str = ""
    region_code: str = "0001"
    cinema_key: str = ""
    movie: str = ""
    screens: list[str] = field(default_factory=list)
    weekdays: list[int] = field(default_factory=list)
    days_ahead: int = 14
    dates: list[str] = field(default_factory=list)
    time_from: str = ""
    time_to: str = ""
    people: int = 2
    seat_mode: str = "groups"             # groups | consecutive | any | sweet
    sweet_ratio: float = 0.2              # 명당 상위 비율
    seat_groups: list[list[str]] = field(default_factory=list)
    pref_rows: list[str] = field(default_factory=list)
    interval: int = 20
    sound: bool = True
    webhook: str = ""
    telegram: dict = field(default_factory=dict)
    open_browser: bool = False
    repeat_alert: bool = False
    adaptive: bool = True        # 적응형 폴링 (임박·취소잦은 회차를 더 자주)
    keep_history: bool = True    # 잔여석 변동을 history.jsonl 에 기록

    @property
    def cinema(self) -> Cinema:
        return Cinema(cinema_id=self.cinema_id, name=self.cinema_name,
                      region_code=self.region_code,
                      key=self.cinema_key or str(self.cinema_id), brand=self.brand)

    def seat_text(self) -> str:
        if self.seat_mode == "groups":
            return " 또는 ".join(",".join(g) for g in self.seat_groups)
        if self.seat_mode == "consecutive":
            rows = f" (열 {','.join(self.pref_rows)})" if self.pref_rows else ""
            return f"연속 {self.people}석{rows}"
        if self.seat_mode == "sweet":
            return f"명당 {self.people}석 (상위 {self.sweet_ratio:.0%})"
        return f"아무 자리 {self.people}석 이상"

    def fields(self) -> list[tuple[str, str]]:
        wd = "전체" if not self.weekdays else ",".join(WEEKDAY_KR[w] for w in sorted(self.weekdays))
        brand_name = PROVIDERS[self.brand].name if self.brand in PROVIDERS else self.brand
        rows = [("극장", f"{brand_name} {self.cinema_name}"),
                ("영화", self.movie or "전체")]
        if self.mode == "seat":
            rows += [("요일", f"{wd}  (오늘부터 {self.days_ahead}일)"),
                     ("상영관", ", ".join(self.screens) if self.screens else "전체"),
                     ("시간대", f"{self.time_from or '00:00'} ~ {self.time_to or '23:59'}"),
                     ("좌석", self.seat_text())]
        elif self.mode == "radar":
            rows += [("측정", "예매가 열려 있는 마지막 날짜(horizon)"),
                     ("기록", os.path.basename(OPENLOG_FILE))]
        else:
            rows += [("날짜", ", ".join(self.dates)),
                     ("상영관", ", ".join(self.screens) if self.screens else "전체")]
        alarm = "소리" if self.sound else "무음"
        if self.webhook:
            alarm += " + 웹훅"
        if self.telegram.get("token"):
            alarm += " + 텔레그램"
        rows.append(("알림", f"{self.interval}초 주기 / {alarm}"))
        return rows

    def summary_rows(self) -> list[str]:
        return [f"{C.DIM}{pad(k, 10)}{C.RESET}{C.BWHITE}{v}{C.RESET}" for k, v in self.fields()]


def load_settings() -> None:
    """settings.json 을 읽어 전역 SETTINGS 에 병합한다. 없거나 깨지면 기본값 유지."""
    if not os.path.exists(SETTINGS_FILE):
        return
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return
    for key in DEFAULT_SETTINGS:
        if key in data:
            SETTINGS[key] = data[key]


def save_settings() -> None:
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
            json.dump(SETTINGS, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"   {C.YELLOW}설정 저장 실패: {exc}{C.RESET}")


def load_profiles(brand: str = "") -> list[dict]:
    if not os.path.exists(PROFILE_FILE):
        return []
    try:
        with open(PROFILE_FILE, encoding="utf-8") as fh:
            rows = json.load(fh).get("profiles", [])
    except Exception:
        return []
    if brand:
        rows = [r for r in rows if r.get("brand", "lotte") == brand]
    return rows


def save_profile(cfg: Watch) -> None:
    profiles = [p for p in load_profiles() if p.get("profile_name") != cfg.profile_name]
    profiles.append(asdict(cfg))
    try:
        with open(PROFILE_FILE, "w", encoding="utf-8") as fh:
            json.dump({"profiles": profiles}, fh, ensure_ascii=False, indent=2)
        print(f"   {C.GREEN}저장 완료{C.RESET} {C.DIM}{PROFILE_FILE}{C.RESET}")
    except Exception as exc:
        print(f"   {C.YELLOW}설정 저장 실패: {exc}{C.RESET}")


class Favorites:
    """자주 가는 지점과 상영관. 브랜드별로 따로 관리하고 선택 목록 맨 위에 ★ 로 올라온다."""

    def __init__(self) -> None:
        self.cinemas: dict[str, list[str]] = {}     # {"lotte": ["1016"]}
        self.screens: dict[str, list[str]] = {}     # {"lotte:1016": ["21관"]}
        self.load()

    @staticmethod
    def _key(brand: str, cinema_id) -> str:
        return f"{brand}:{cinema_id}"

    def load(self) -> None:
        if not os.path.exists(FAVORITE_FILE):
            return
        try:
            with open(FAVORITE_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
            raw = data.get("cinemas", {})
            if isinstance(raw, list):               # 예전 형식(롯데 전용) 이전
                self.cinemas = {"lotte": [str(c) for c in raw]}
                self.screens = {f"lotte:{k}": list(v)
                                for k, v in (data.get("screens") or {}).items()}
            else:
                self.cinemas = {b: [str(c) for c in ids] for b, ids in raw.items()}
                self.screens = {str(k): list(v)
                                for k, v in (data.get("screens") or {}).items()}
        except Exception:
            pass

    def save(self) -> None:
        try:
            with open(FAVORITE_FILE, "w", encoding="utf-8") as fh:
                json.dump({"cinemas": self.cinemas, "screens": self.screens},
                          fh, ensure_ascii=False, indent=2)
        except Exception as exc:
            print(f"   {C.YELLOW}즐겨찾기 저장 실패: {exc}{C.RESET}")

    # -- 조회 --------------------------------------------------------
    def ids(self, brand: str) -> list[str]:
        return self.cinemas.get(brand, [])

    def has_cinema(self, cinema: Cinema) -> bool:
        return str(cinema.cinema_id) in self.ids(cinema.brand)

    def screens_of(self, cinema: Cinema) -> list[str]:
        return self.screens.get(self._key(cinema.brand, cinema.cinema_id), [])

    def has_screen(self, cinema: Cinema | None, screen: str) -> bool:
        return bool(cinema) and screen in self.screens_of(cinema)

    def count(self, brand: str = "") -> tuple[int, int]:
        if brand:
            ids = self.ids(brand)
            screens = sum(len(v) for k, v in self.screens.items()
                          if k.startswith(f"{brand}:"))
            return len(ids), screens
        return (sum(len(v) for v in self.cinemas.values()),
                sum(len(v) for v in self.screens.values()))

    # -- 편집 --------------------------------------------------------
    def set_cinema(self, cinema: Cinema, on: bool) -> None:
        ids = self.cinemas.setdefault(cinema.brand, [])
        cid = str(cinema.cinema_id)
        if on and cid not in ids:
            ids.append(cid)
        elif not on and cid in ids:
            ids.remove(cid)
        if not ids:
            self.cinemas.pop(cinema.brand, None)

    def set_screens(self, cinema: Cinema, names: list[str]) -> None:
        key = self._key(cinema.brand, cinema.cinema_id)
        if names:
            self.screens[key] = names
        else:
            self.screens.pop(key, None)

    def sort_cinemas(self, cinemas: list[Cinema]) -> list[Cinema]:
        fav = [c for c in cinemas if self.has_cinema(c)]
        rest = [c for c in cinemas if not self.has_cinema(c)]
        return fav + rest

    def sort_screens(self, cinema: Cinema | None,
                     items: list[tuple[str, int]]) -> list[tuple[str, int]]:
        fav = [s for s in items if self.has_screen(cinema, s[0])]
        rest = [s for s in items if not self.has_screen(cinema, s[0])]
        return fav + rest


def cfg_from_dict(d: dict) -> Watch:
    known = set(Watch.__dataclass_fields__)
    return Watch(**{k: v for k, v in d.items() if k in known})


# ══════════════════════════════════════════════════════════════════════
#  브랜드 프로바이더
#
#  브랜드마다 공개된 데이터의 깊이가 다르다 (2026-08-28 실측):
#    롯데시네마 - 지점 / 상영표+잔여석 / 좌석표까지 전부
#    메가박스   - 지점 / 상영표+잔여석 (좌석표는 예매 세션 뒤에 있어 불가)
#    CGV        - 지점만 (구 사이트 폐기, 신규 SPA 의 상영표 API 미확보)
# ══════════════════════════════════════════════════════════════════════
class Unsupported(RuntimeError):
    """이 브랜드에서 지원하지 않는 기능."""


class Provider:
    code = ""
    name = ""
    color_name = "BWHITE"
    title = "CINEMA WATCH"
    site = ""
    supports_seats = False       # 좌석표(좌석 단위) 조회 가능 여부
    supports_shows = True        # 상영시간표 조회 가능 여부
    note = ""

    @classmethod
    def color(cls) -> str:
        return getattr(C, cls.color_name, "")

    @property
    def calls(self) -> int:
        return 0

    def regions(self) -> list[tuple[str, str]]:
        raise NotImplementedError

    def cinemas(self) -> list[Cinema]:
        raise NotImplementedError

    def showtimes(self, cinema: Cinema, play_date: str) -> list[Show]:
        raise NotImplementedError

    def seats(self, show: Show) -> SeatMap:
        raise Unsupported(f"{self.name}는 좌석표 조회를 지원하지 않습니다.")


class LotteProvider(Provider):
    code = "lotte"
    name = "롯데시네마"
    color_name = "BRED"
    title = "LOTTE CINEMA WATCH"
    site = "lottecinema.co.kr"
    supports_seats = True
    note = "좌석 단위까지 전부 지원"

    REGIONS = {"0001": "서울", "0002": "경기/인천", "0003": "대전/충청",
               "0004": "광주/전라", "0005": "대구/경북", "0006": "강원",
               "0007": "제주", "0101": "부산/울산/경남"}

    def __init__(self, api: "Api | None" = None):
        self.api = api or Api()

    @property
    def calls(self) -> int:
        return self.api.calls

    def regions(self) -> list[tuple[str, str]]:
        return list(self.REGIONS.items())

    def cinemas(self) -> list[Cinema]:
        best: dict[str, Cinema] = {}
        for raw in self.api.cinemas():
            if int(raw.get("DivisionCode") or 0) != 1:      # 2 는 특별관 묶음(중복)
                continue
            cid = str(raw["CinemaID"])
            region = str(raw.get("DetailDivisionCode") or "0001")
            best.setdefault(cid, Cinema(
                cinema_id=cid, name=(raw.get("CinemaNameKR") or "").strip(),
                region_code=region, key=f"1|{int(region)}|{cid}",
                addr=(raw.get("CinemaAddrSummary") or "").strip(),
                region_name=self.REGIONS.get(region, ""), brand=self.code))
        return list(best.values())

    def showtimes(self, cinema: Cinema, play_date: str) -> list[Show]:
        return [Show.parse(d) for d in self.api.showtimes(cinema.key, play_date)]

    def seats(self, show: Show) -> SeatMap:
        return SeatMap.parse(self.api.seats(show.cinema_id, show.screen_id,
                                            show.play_seq, show.play_date))


class WebJson:
    """메가박스·CGV 용 간단한 JSON HTTP 클라이언트 (쿠키 유지 + 호출 간격 제한)."""

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

    def __init__(self, min_interval: float = 0.6, timeout: float = 15.0, retries: int = 3):
        import http.cookiejar
        self.min_interval = min_interval
        self.timeout = timeout
        self.retries = retries
        self.calls = 0
        self._lock = threading.Lock()
        self._last = 0.0
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            urllib.request.HTTPCookieProcessor(self._jar))

    def _throttle(self) -> None:
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait + random.uniform(0, 0.12))
            self._last = time.monotonic()

    def request(self, url: str, payload: dict | None = None, referer: str = "",
                as_json: bool = True):
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        headers = {"User-Agent": self.UA, "Accept": "application/json, text/plain, */*",
                   "Accept-Language": "ko-KR,ko;q=0.9"}
        if data is not None:
            headers["Content-Type"] = "application/json;charset=UTF-8"
            headers["X-Requested-With"] = "XMLHttpRequest"
        if referer:
            headers["Referer"] = referer
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data is not None else "GET")
        last: Exception | None = None
        for attempt in range(self.retries):
            self._throttle()
            self.calls += 1
            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    body = resp.read().decode("utf-8", "replace")
                return json.loads(body) if as_json else body
            except Exception as exc:
                last = exc
                time.sleep(min(2 ** attempt, 8) + random.uniform(0, 0.4))
        raise LotteApiError(f"요청 실패: {last}")

    def warmup(self, url: str) -> None:
        """세션 쿠키 확보용."""
        try:
            self.request(url, referer="", as_json=False)
        except Exception:
            pass


class MegaboxProvider(Provider):
    code = "megabox"
    name = "메가박스"
    color_name = "BYELLOW"
    title = "MEGABOX WATCH"
    site = "megabox.co.kr"
    supports_seats = True
    note = "좌석 단위까지 전부 지원"

    BASE = "https://www.megabox.co.kr"
    TIMETABLE = BASE + "/booking/timetable"
    SEATS = BASE + "/on/oh/ohz/PcntSeatChoi/selectSeatList.do"

    def __init__(self):
        self.http = WebJson()
        self._warm = False

    @property
    def calls(self) -> int:
        return self.http.calls

    def _ensure(self) -> None:
        if not self._warm:
            self.http.warmup(self.TIMETABLE)
            self._warm = True

    def _master(self) -> dict:
        self._ensure()
        return self.http.request(
            self.BASE + "/on/oh/ohb/PlayTime/selectPlayTimeMasterList.do",
            {"playDe": date.today().strftime("%Y%m%d")}, referer=self.TIMETABLE)

    def regions(self) -> list[tuple[str, str]]:
        seen: dict[str, str] = {}
        for row in self._master().get("areaBrchList") or []:
            seen.setdefault(str(row.get("areaCd")), str(row.get("areaCdNm") or ""))
        return list(seen.items())

    def cinemas(self) -> list[Cinema]:
        out: dict[str, Cinema] = {}
        for row in self._master().get("areaBrchList") or []:
            brch = str(row.get("brchNo") or "")
            if not brch:
                continue
            out.setdefault(brch, Cinema(
                cinema_id=brch, name=unescape_html(str(row.get("brchNm") or "")),
                region_code=str(row.get("areaCd") or ""), key=brch,
                region_name=str(row.get("areaCdNm") or ""), brand=self.code))
        return list(out.values())

    def showtimes(self, cinema: Cinema, play_date: str) -> list[Show]:
        self._ensure()
        data = self.http.request(self.BASE + "/on/oh/ohc/Brch/schedulePage.do",
                                 {"playDe": play_date.replace("-", ""),
                                  "brchNo1": cinema.key or str(cinema.cinema_id)},
                                 referer=self.TIMETABLE)
        rows = ((data.get("megaMap") or {}).get("movieFormList")) or []
        out = []
        for r in rows:
            if str(r.get("brchNo")) != str(cinema.key or cinema.cinema_id):
                continue
            out.append(Show(
                cinema_id=str(r.get("brchNo")),
                cinema_name=unescape_html(str(r.get("brchNm") or "")),
                movie_name=unescape_html(str(r.get("movieNm") or "")),
                movie_code=str(r.get("rpstMovieNo") or r.get("movieNo") or ""),
                screen_id=str(r.get("theabNo") or ""),
                screen_name=unescape_html(str(r.get("theabExpoNm") or "")),
                play_date=fmt_date(str(r.get("playDe") or "")),
                play_seq=str(r.get("playSchdlNo") or r.get("seq") or ""),
                start=str(r.get("playStartTime") or ""),
                end=str(r.get("playEndTime") or ""),
                total_seats=int(r.get("totSeatCnt") or 0),
                open_seats=int(r.get("restSeatCnt") or 0),
                film=unescape_html(str(r.get("playKindNm") or "")),
                bookable=str(r.get("bokdAbleAt") or "Y").upper() == "Y"))
        return out

    def seats(self, show: Show) -> SeatMap:
        """좌석표. 예매 좌석선택 화면이 쓰는 API 를 그대로 호출한다.

        로그인 없이 열리고, 상영표 warmup(세션 쿠키) 외에 추가 준비가 필요 없다.
        `seatStatCd` 가 `GERN_SELL` 이면 판매 가능, 그 외(SCT01·SCT04 …)는 나간 자리.
        실측: 강남 9층 리클라이너 1관 116석 중 72석 가능 = 상영표 잔여석과 일치.
        """
        self._ensure()
        data = self.http.request(self.SEATS,
                                 {"playSchdlNo": show.play_seq,
                                  "brchNo": str(show.cinema_id)},
                                 referer=self.TIMETABLE)
        sm = SeatMap()
        for s in data.get("seatListSD01") or []:
            if str(s.get("seatExpoAt") or "Y").upper() != "Y":
                continue                      # 화면에 안 그리는 자리(통로 등)
            row = str(s.get("rowNm") or "").strip().upper()
            col = int(s.get("seatNo") or 0)
            if not row or not col:
                continue
            ok = str(s.get("seatStatCd") or "") == "GERN_SELL"
            label = f"{row}{col}"
            (sm.available if ok else sm.taken).add(label)
            sm.grid.setdefault(row, []).append((col, ok))
            x, y = s.get("horzCoorVal"), s.get("vertCoorVal")
            if x is not None and y is not None:
                # 롯데와 같은 규칙: X=좌우, Y=스크린에서 멀어지는 방향(A열이 최소)
                sm.coord[label] = (int(x), int(y))
        for row in sm.grid:
            sm.grid[row].sort()
        sm.rows = sorted(sm.grid)
        sm.total = len(sm.available) + len(sm.taken)
        return sm


class CgvProvider(Provider):
    code = "cgv"
    name = "CGV"
    color_name = "BRED"
    title = "CGV WATCH"
    site = "cgv.co.kr"
    supports_seats = False
    supports_shows = True
    note = "상영표·잔여석까지 (좌석표는 미제공)"

    BASE = "https://cgv.co.kr"
    CO = "A420"
    RTCTL = "1"          # 발매통제범위코드. 1 이어야 조회가 열린다(실측).

    def __init__(self):
        self.http = WebJson()

    @property
    def calls(self) -> int:
        return self.http.calls

    def _regions_raw(self) -> list[dict]:
        data = self.http.request(f"{self.BASE}/api/v1/booking/searchRegnList?coCd={self.CO}",
                                 referer=f"{self.BASE}/cnm/movieBook")
        return data.get("data") or []

    def regions(self) -> list[tuple[str, str]]:
        return [(str(r.get("regnGrpCd")), str(r.get("regnGrpNm") or ""))
                for r in self._regions_raw()]

    def cinemas(self) -> list[Cinema]:
        out = []
        for region in self._regions_raw():
            for site in region.get("siteList") or []:
                out.append(Cinema(
                    cinema_id=str(site.get("siteNo") or ""),
                    name=str(site.get("siteNm") or ""),
                    region_code=str(region.get("regnGrpCd") or ""),
                    key=str(site.get("siteNo") or ""),
                    addr=str(site.get("bzplcOperStusNm") or ""),
                    region_name=str(region.get("regnGrpNm") or ""), brand=self.code))
        return out

    def showtimes(self, cinema: Cinema, play_date: str) -> list[Show]:
        """상영표. `searchMovScnInfo` 한 번이면 그 극장·그 날짜 전 회차가 온다.

        신규 SPA 의 JS 번들에서 엔드포인트를 확보했다. 호스트는 api.cgv.co.kr 이지만
        직접 호출하면 401 이고, cgv.co.kr/api/v1/booking/ 프록시로는 열린다.
        잔여석은 `frSeatCnt`, 총석은 `stcnt`(= cpSeatCnt).
        """
        site = cinema.key or str(cinema.cinema_id)
        ymd = play_date.replace("-", "")
        data = self.http.request(
            f"{self.BASE}/api/v1/booking/searchMovScnInfo?coCd={self.CO}"
            f"&siteNo={site}&scnYmd={ymd}&rtctlScopCd={self.RTCTL}",
            referer=f"{self.BASE}/cnm/movieBook")
        out = []
        for r in data.get("data") or []:
            out.append(Show(
                cinema_id=str(r.get("siteNo") or site),
                cinema_name=str(r.get("siteNm") or cinema.name),
                movie_name=str(r.get("movNm") or r.get("expoProdNm") or "").strip(),
                movie_code=str(r.get("movNo") or ""),
                screen_id=str(r.get("scnsNo") or ""),
                screen_name=str(r.get("expoScnsNm") or r.get("scnsNm") or "").strip(),
                play_date=fmt_date(str(r.get("scnYmd") or ymd)),
                play_seq=str(r.get("scnSseq") or ""),
                start=fmt_hhmm(str(r.get("scnsrtTm") or "")),
                end=fmt_hhmm(str(r.get("scnendTm") or "")),
                total_seats=int(r.get("stcnt") or r.get("cpSeatCnt") or 0),
                open_seats=int(r.get("frSeatCnt") or 0),
                film=str(r.get("movkndDsplNm") or ""),
                bookable=str(r.get("cntlYn") or "N").upper() != "Y"))
        return out


def unescape_html(text: str) -> str:
    """메가박스 응답에 섞여 오는 &#40; 같은 표기를 되돌린다."""
    import html
    return html.unescape(text).strip()


def fmt_date(compact: str) -> str:
    """'20260830' -> '2026-08-30'"""
    if len(compact) == 8 and compact.isdigit():
        return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"
    return compact


def fmt_hhmm(compact: str) -> str:
    """'1350' -> '13:50' (CGV 는 시각을 4자리 숫자로 준다)"""
    compact = compact.strip()
    if len(compact) == 4 and compact.isdigit():
        return f"{compact[:2]}:{compact[2:]}"
    return compact


PROVIDERS: dict[str, type[Provider]] = {
    "lotte": LotteProvider, "megabox": MegaboxProvider, "cgv": CgvProvider,
}


# ══════════════════════════════════════════════════════════════════════
#  조회 + 캐시
# ══════════════════════════════════════════════════════════════════════
class Catalog:
    def __init__(self, provider: "Provider"):
        self.provider = provider
        self._cinemas: list[Cinema] | None = None
        self._shows: dict[tuple[str, str], tuple[float, list[Show]]] = {}

    # 브랜드 공통 진입점 -------------------------------------------------
    @property
    def api(self) -> "Provider":            # 기존 호출부 호환
        return self.provider

    @property
    def brand(self) -> "Provider":
        return self.provider

    def regions(self) -> list[tuple[str, str]]:
        return self.provider.regions()

    def cinemas(self) -> list[Cinema]:
        if self._cinemas is None:
            self._cinemas = sorted(self.provider.cinemas(), key=lambda c: c.name)
        return self._cinemas

    def by_region(self, region_code: str) -> list[Cinema]:
        return [c for c in self.cinemas() if c.region_code == region_code]

    def seats(self, show: Show) -> SeatMap:
        return self.provider.seats(show)

    @staticmethod
    def merge_rows(shows: list[Show]) -> list[Show]:
        """같은 회차가 좌석 구역별로 여러 줄 오는 경우를 하나로 합친다.

        예) 9관 16:25 이 '씨네패밀리 18석' + '일반 342석' 두 줄로 내려오는데,
        GetSeats 는 360석짜리 좌석표 하나를 준다. 합치지 않으면 같은 회차의
        잔여석이 두 값 사이를 오가는 것처럼 보여 취소표 오탐이 난다.
        """
        merged: dict[str, Show] = {}
        for s in shows:
            cur = merged.get(s.key)
            if cur is None:
                merged[s.key] = s
                continue
            base = cur if cur.total_seats >= s.total_seats else s
            merged[s.key] = replace(base,
                                    total_seats=cur.total_seats + s.total_seats,
                                    open_seats=cur.open_seats + s.open_seats,
                                    bookable=cur.bookable or s.bookable)
        return list(merged.values())

    def shows(self, cinema: Cinema, play_date: str, ttl: float = 0.0) -> list[Show]:
        ck = (cinema.key or str(cinema.cinema_id), play_date)
        hit = self._shows.get(ck)
        if hit and ttl and time.monotonic() - hit[0] < ttl:
            return hit[1]
        shows = self.merge_rows(self.provider.showtimes(cinema, play_date))
        self._shows[ck] = (time.monotonic(), shows)
        return shows

    def cached_shows(self, cinema: Cinema, play_date: str) -> list[Show] | None:
        hit = self._shows.get((cinema.key or str(cinema.cinema_id), play_date))
        return hit[1] if hit else None

    def scan(self, cinema: Cinema, days: int) -> list[Show]:
        out: list[Show] = []
        today = date.today()
        for i in range(days):
            day = (today + timedelta(days=i)).isoformat()
            spin = glyph("■", "#") * (i + 1) + glyph("·", ".") * (days - i - 1)
            sys.stdout.write(f"\r   {C.CYAN}{spin}{C.RESET}  {day} 상영표 확인 중...")
            sys.stdout.flush()
            try:
                out.extend(self.shows(cinema, day))
            except LotteApiError:
                pass
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()
        return out


# ══════════════════════════════════════════════════════════════════════
#  필터 / 판정
# ══════════════════════════════════════════════════════════════════════
class ShowTracker:
    """회차별 잔여석 변동 이력.

    상영표(GetPlaySequence) 한 번이면 그 날짜 모든 회차의 잔여석을 알 수 있다.
    잔여석이 늘어난 순간 = 누군가 취소한 순간이므로, 이 신호를 좌석 조회의 방아쇠로 쓴다.
    """

    def __init__(self, cfg: Watch, keep: int = 40, log: bool = True):
        self.cfg = cfg
        self.last: dict[str, int] = {}
        self.events: dict[str, deque] = {}      # key -> deque[(ts, delta, open)]
        self.keep = keep
        self.log = log

    def observe(self, show: Show) -> int:
        """직전 관측 대비 잔여석 변화량. 처음 보는 회차는 0."""
        prev = self.last.get(show.key)
        self.last[show.key] = show.open_seats
        if prev is None or prev == show.open_seats:
            return 0
        delta = show.open_seats - prev
        self.events.setdefault(show.key, deque(maxlen=self.keep)).append(
            (time.time(), delta, show.open_seats))
        self._append_log(show, delta)
        return delta

    def _append_log(self, show: Show, delta: int) -> None:
        if not self.log:
            return
        row = {"t": datetime.now().isoformat(timespec="seconds"),
               "cinema": show.cinema_id, "movie": show.movie_name,
               "date": show.play_date, "start": show.start, "screen": show.screen_name,
               "seq": show.play_seq, "open": show.open_seats,
               "total": show.total_seats, "delta": delta}
        try:
            with open(HISTORY_FILE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            self.log = False                    # 한 번 실패하면 더 시도하지 않는다

    # -- 통계 --------------------------------------------------------
    def _within(self, key: str, seconds: float) -> list[tuple[float, int, int]]:
        cut = time.time() - seconds
        return [e for e in self.events.get(key, ()) if e[0] >= cut]

    def cancels(self, key: str, seconds: float = 3600) -> tuple[int, int]:
        """(취소 횟수, 늘어난 좌석 수) - 잔여석이 증가한 이벤트만."""
        ups = [e for e in self._within(key, seconds) if e[1] > 0]
        return len(ups), sum(e[1] for e in ups)

    def sold(self, key: str, seconds: float = 3600) -> int:
        return -sum(e[1] for e in self._within(key, seconds) if e[1] < 0)

    def last_change(self, key: str) -> float | None:
        events = self.events.get(key)
        return events[-1][0] if events else None

    def context(self, show: Show) -> str:
        """알림에 덧붙일 한 줄 맥락."""
        n, seats = self.cancels(show.key)
        sold = self.sold(show.key)
        bits = []
        if n:
            bits.append(f"최근 1시간 취소 {n}회(+{seats}석)")
        if sold:
            bits.append(f"판매 {sold}석")
        bits.append(f"잔여 {show.open_seats}/{show.total_seats}")
        return " · ".join(bits)


@dataclass
class Horizon:
    """어느 시점에 관측한 '예매가 열려 있는 범위'."""
    at: datetime
    cinema_id: int
    movie: str
    last_open: str          # 예매 가능한 마지막 날짜 (YYYY-MM-DD)
    days: int               # 오늘부터 며칠치
    full_last: str = ""     # 정규 시간표(회차가 많은 날)의 마지막 날짜
    full_days: int = 0

    def row(self) -> dict:
        return {"t": self.at.isoformat(timespec="seconds"), "cinema": self.cinema_id,
                "movie": self.movie, "last_open": self.last_open, "days": self.days,
                "full_last": self.full_last, "full_days": self.full_days}


class OpenRadar:
    """예매가 '언제' 열리는지를 데이터로 만든다.

    롯데시네마는 오픈 일정을 공지하지 않는다. 대신 관측할 수 있는 사실이 하나 있다 -
    지금 예매가 열려 있는 마지막 날짜(horizon). 이 값이 늘어난 순간이 곧 오픈 순간이다.
    horizon 을 주기적으로 재서 openlog.jsonl 에 남기면, 며칠 만에 요일·시각·폭 패턴이 보인다.

    예매 범위는 층이 나뉜다 (월드타워 실측):
        D+0~4  정규 시간표 100+회차     <- full_last
        D+5~9  주요 개봉작만 30회차 안팎
        D+10~  특별 상영 사전판매만      <- last_open
    그래서 '그 영화'(movie) 기준 horizon 을 따로 잰다.
    """

    FULL_RATIO = 0.5        # 최대 회차 수의 50% 이상이면 '정규 시간표가 열린 날'

    def __init__(self, cat: "Catalog"):
        self.cat = cat

    # -- 관측 --------------------------------------------------------
    def _count(self, cinema: Cinema, day: str, movie: str) -> int:
        try:
            shows = self.cat.shows(cinema, day, ttl=120)
        except LotteApiError:
            return -1
        if movie:
            key = movie.replace(" ", "")
            shows = [s for s in shows if key in s.movie_name.replace(" ", "")]
        return len(shows)

    def scan(self, cinema: Cinema, movie: str = "", max_days: int = 45,
             progress=None) -> Horizon:
        """이진탐색으로 예매 가능한 마지막 날짜를 찾는다 (45일 범위에 6~7회 호출)."""
        today = date.today()

        def day_of(offset: int) -> str:
            return (today + timedelta(days=offset)).isoformat()

        def open_at(offset: int) -> bool:
            if progress:
                progress(offset)
            return self._count(cinema, day_of(offset), movie) > 0

        if not open_at(0):
            lo = -1
            for probe in range(1, 8):           # 오늘 상영이 끝났을 수 있어 며칠 더 본다
                if open_at(probe):
                    lo = probe
                    break
            if lo < 0:
                return Horizon(datetime.now(), cinema.cinema_id, movie, "", 0)
        else:
            lo = 0

        hi = max_days
        if open_at(hi):
            lo = hi
        else:
            while lo + 1 < hi:
                mid = (lo + hi) // 2
                if open_at(mid):
                    lo = mid
                else:
                    hi = mid

        return Horizon(datetime.now(), cinema.cinema_id, movie, day_of(lo), lo)

    def profile(self, cinema: Cinema, movie: str = "", upto: int = 16,
                progress=None) -> list[tuple[str, int]]:
        """오늘부터 upto 일까지 날짜별 회차 수 (하루 1회 호출, 캐시 재사용)."""
        out: list[tuple[str, int]] = []
        for offset in range(upto + 1):
            day = (date.today() + timedelta(days=offset)).isoformat()
            if progress:
                progress(offset, upto)
            out.append((day, max(self._count(cinema, day, movie), 0)))
        return out

    @classmethod
    def full_horizon(cls, profile: list[tuple[str, int]]) -> tuple[str, int]:
        """정규 시간표(회차가 많은 날)가 열린 마지막 날."""
        counts = [n for _, n in profile]
        peak = max(counts) if counts else 0
        if peak <= 0:
            return "", 0
        idx = max(i for i, n in enumerate(counts) if n >= peak * cls.FULL_RATIO)
        return profile[idx][0], idx

    # -- 기록 --------------------------------------------------------
    @staticmethod
    def log(horizon: Horizon) -> None:
        try:
            with open(OPENLOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(horizon.row(), ensure_ascii=False) + "\n")
        except Exception:
            pass

    @staticmethod
    def history(cinema_id: int = 0, movie: str = "") -> list[dict]:
        if not os.path.exists(OPENLOG_FILE):
            return []
        rows = []
        try:
            with open(OPENLOG_FILE, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if cinema_id and int(row.get("cinema", 0)) != cinema_id:
                        continue
                    if movie and row.get("movie", "") != movie:
                        continue
                    rows.append(row)
        except Exception:
            return []
        return rows

    @staticmethod
    def open_events(rows: list[dict]) -> list[dict]:
        """horizon 이 늘어난 순간들 = 예매가 열린 순간들."""
        events, prev = [], None
        for row in sorted(rows, key=lambda r: r["t"]):
            cur = row.get("last_open") or ""
            if prev and cur > prev["last_open"]:
                events.append({"t": row["t"], "from": prev["last_open"], "to": cur,
                               "added": (date.fromisoformat(cur)
                                         - date.fromisoformat(prev["last_open"])).days,
                               "days": row.get("days", 0)})
            prev = row
        return events

    # -- 예측 --------------------------------------------------------
    @staticmethod
    def _next_slot(after: datetime, weekday: int, hour: int) -> datetime:
        """after 이후에 오는 '학습된 요일 · 시각'."""
        delta = (weekday - after.weekday()) % 7
        cand = datetime.combine(after.date() + timedelta(days=delta),
                                datetime.min.time()).replace(hour=hour)
        if cand <= after:
            cand += timedelta(days=7)
        return cand

    @classmethod
    def pattern(cls, events: list[dict]) -> dict | None:
        """오픈 이벤트들에서 주기·폭·요일·시각을 뽑는다."""
        if not events:
            return None
        times = [datetime.fromisoformat(e["t"]) for e in events]
        adds = sorted(e["added"] for e in events)
        step = adds[len(adds) // 2] or 1
        gaps = sorted((times[i] - times[i - 1]).total_seconds() / 86400
                      for i in range(1, len(times)))
        period = gaps[len(gaps) // 2] if gaps else 0.0
        hours = [t.hour for t in times]
        wdays = [t.weekday() for t in times]
        return {"step": step, "period": period,
                "hour": max(set(hours), key=hours.count),
                "weekday": max(set(wdays), key=wdays.count),
                "n": len(events),
                "kind": "rolling" if (period and period <= 2) else "batch"}

    def predict(self, cinema: Cinema, movie: str, target: str,
                snap: Horizon | None = None) -> dict:
        """target(YYYY-MM-DD) 예매가 언제 열릴지 추정.

        관측 이력이 있으면 그 패턴대로 '앞으로 굴려서' 목표일이 범위에 들어오는
        시점을 찾는다. 매일 조금씩 미는 방식(rolling)과 주 단위로 왕창 여는
        방식(batch)은 계산이 다르다.
        """
        rows = self.history(cinema.cinema_id, movie)
        events = self.open_events(rows)
        pat = self.pattern(events)
        out: dict = {"target": target, "events": len(events), "pattern": pat}

        if not snap or not snap.last_open:
            out.update(status="불명", confidence="없음", basis="",
                       text="현재 예매 범위를 알 수 없습니다. 먼저 스캔하세요.")
            return out
        if target <= snap.last_open:
            out.update(status="열림", text="이미 예매가 열려 있습니다.", basis="")
            return out

        target_d = date.fromisoformat(target)
        horizon_d = date.fromisoformat(snap.last_open)
        out["gap"] = (target_d - horizon_d).days

        if not pat:                      # 이력이 없을 때: 지금 범위(D+n)가 유지된다고 보고 역산
            est = target_d - timedelta(days=snap.days)
            out.update(status="추정", est_date=est.isoformat(), est_hour=None,
                       lead=snap.days, confidence="낮음",
                       text=f"{est.isoformat()}({WEEKDAY_KR[est.weekday()]}) 전후",
                       basis=(f"관측 이력 없음 · 지금 D+{snap.days}까지 열려 있다는 "
                              f"사실만으로 역산"))
            return out

        if pat["kind"] == "rolling":     # 매일 조금씩 미는 방식
            est_date = date.today() + timedelta(days=out["gap"])
            est = datetime.combine(est_date, datetime.min.time()).replace(hour=pat["hour"])
        else:                            # 주 단위 배치 오픈
            cur, when = horizon_d, datetime.now()
            for _ in range(30):
                when = self._next_slot(when, pat["weekday"], pat["hour"])
                cur = cur + timedelta(days=pat["step"])
                if cur >= target_d:
                    break
            est = when

        conf = "높음" if pat["n"] >= 5 else ("보통" if pat["n"] >= 3 else "낮음")
        kind_kr = "매일 조금씩" if pat["kind"] == "rolling" else             f"{round(pat['period'])}일마다 {pat['step']}일치씩"
        out.update(status="예상", est_date=est.date().isoformat(), est_hour=est.hour,
                   lead=snap.days, confidence=conf,
                   text=(f"{est:%Y-%m-%d}({WEEKDAY_KR[est.weekday()]}) "
                         f"{est.hour:02d}시 무렵"),
                   basis=(f"{kind_kr} 여는 패턴 · 관측 {pat['n']}회 · 신뢰도 {conf}"))
        return out


class Scheduler:
    """회차별 확인 간격을 상황에 맞게 조절한다 (적응형 폴링).

    - 상영이 임박할수록 자주: 취소표는 상영 직전에 몰린다
    - 취소가 잦았던 회차는 더 자주
    - 거의 매진이면 더 자주, 자리가 넉넉하면 덜
    - 잔여석 수가 그대로면 좌석 구성도 그대로일 가능성이 높아 건너뛴다 (주기적으로 강제 확인)
    """

    TIERS = ("임박", "오늘/내일", "3일내", "여유")
    FACTOR = {"임박": 1.0, "오늘/내일": 2.0, "3일내": 5.0, "여유": 12.0}
    MAX_INTERVAL = 1800
    FORCE_EVERY = {"임박": 2, "오늘/내일": 4, "3일내": 5, "여유": 6}
    # 잔여석 총량이 같아도 좌석 구성만 바뀔 수 있어(취소 1 + 예매 1) N번째마다 실제로 조회한다.
    # 임박 회차일수록 촘촘하게.

    def __init__(self, cfg: Watch):
        self.base = max(cfg.interval, 5)
        self.next_check: dict[str, float] = {}
        self.skips: dict[str, int] = {}
        self.last_open: dict[str, int] = {}

    @staticmethod
    def show_dt(show: Show) -> datetime:
        # CGV 는 심야 회차를 24:40 처럼 24시 이상으로 준다. strptime 은 이걸 못 읽으니
        # 날짜에 timedelta 를 더해 다음날로 넘긴다 (00:40 이 되어 임박 판정이 맞아진다).
        try:
            hh, mm = show.start.split(":")
            return (datetime.strptime(show.play_date, "%Y-%m-%d")
                    + timedelta(hours=int(hh), minutes=int(mm)))
        except (ValueError, AttributeError):
            return datetime.max

    def tier(self, show: Show, now: datetime | None = None) -> str:
        hours = (self.show_dt(show) - (now or datetime.now())).total_seconds() / 3600
        if hours <= 2:
            return "임박"
        if hours <= 24:
            return "오늘/내일"
        if hours <= 72:
            return "3일내"
        return "여유"

    def day_ttl(self, day: str) -> float:
        """상영표 캐시 수명. 오늘/내일은 매번 새로 받아 취소 신호를 놓치지 않는다."""
        gap = (date.fromisoformat(day) - date.today()).days
        if gap <= 1:
            return 0.0
        if gap <= 3:
            return max(self.base * 3, 60)
        return max(self.base * 10, 300)

    def interval_for(self, show: Show, tracker: ShowTracker) -> float:
        factor = self.FACTOR[self.tier(show)]
        cancels, _ = tracker.cancels(show.key)
        if cancels:
            factor *= 0.4                       # 취소가 잦은 회차는 바짝 붙는다
        ratio = show.open_seats / show.total_seats if show.total_seats else 1.0
        if ratio <= 0.05:
            factor *= 0.7                       # 거의 매진 = 취소표 노림 구간
        elif ratio >= 0.5:
            factor *= 2.0                       # 자리 넉넉하면 급할 것 없다
        return min(max(self.base * factor, self.base), self.MAX_INTERVAL)

    def should_check(self, show: Show, tracker: ShowTracker, delta: int,
                     now: float) -> tuple[bool, str]:
        """(확인할지, 이유). delta>0 이면 취소표가 나온 것이므로 무조건 즉시 확인."""
        key = show.key
        if delta > 0:
            self.skips[key] = 0
            return True, "취소표 감지"
        due = self.next_check.get(key, 0.0)
        if now < due:
            return False, "대기"
        if (self.last_open.get(key) == show.open_seats
                and self.skips.get(key, 0) < self.FORCE_EVERY[self.tier(show)]):
            self.skips[key] = self.skips.get(key, 0) + 1
            self.next_check[key] = now + self.interval_for(show, tracker)
            return False, "변화 없음"
        self.skips[key] = 0
        return True, "정기 확인"

    def mark_checked(self, show: Show, tracker: ShowTracker, now: float) -> None:
        self.last_open[show.key] = show.open_seats
        self.next_check[show.key] = now + self.interval_for(show, tracker)


def filter_shows(shows: list[Show], cfg: Watch, skip_past: bool = True) -> list[Show]:
    out = []
    movie = cfg.movie.replace(" ", "")
    today = date.today().isoformat()
    now = (datetime.now() + timedelta(minutes=5)).strftime("%H:%M")
    for s in shows:
        if skip_past and s.play_date == today and s.start and s.start <= now:
            continue
        if movie and movie not in s.movie_name.replace(" ", ""):
            continue
        if cfg.screens and s.screen_name not in cfg.screens:
            continue
        if cfg.weekdays and s.weekday not in cfg.weekdays:
            continue
        if cfg.time_from and s.start < cfg.time_from:
            continue
        if cfg.time_to and s.start > cfg.time_to:
            continue
        out.append(s)
    return sorted(out, key=lambda s: (s.play_date, s.start, s.screen_name))


def _pick_n(block: list[str], n: int) -> str:
    """연속 블록에서 필요한 N석만 잘라 표기 (블록이 더 길어도 실제 앉을 자리만 보여준다)."""
    seats = block[:max(n, 1)]
    return seats[0] if len(seats) == 1 else f"{seats[0]}~{seats[-1]}"


def evaluate_seats(cfg: Watch, smap: SeatMap) -> tuple[bool, str]:
    if cfg.seat_mode == "groups":
        for group in cfg.seat_groups:
            if all(seat in smap.available for seat in group):
                return True, ",".join(group)
        return False, ""
    if cfg.seat_mode == "consecutive":
        blocks = find_consecutive(smap, cfg.people, cfg.pref_rows or None)
        if blocks:
            return True, " / ".join(_pick_n(b, cfg.people) for b in blocks[:3])
        return False, ""
    if cfg.seat_mode == "sweet":
        spots = sweet_seats(smap, cfg.sweet_ratio)
        free = spots & smap.available
        if not free:
            return False, ""
        if cfg.people <= 1:
            scores = sweet_scores(smap)
            best = sorted(free, key=lambda k: -scores.get(k, 0))[:5]
            return True, f"명당 {', '.join(best)} (총 {len(free)}석)"
        blocks = find_consecutive(smap, cfg.people, allowed=free)
        if not blocks:
            return False, ""
        picks = " / ".join(_pick_n(b, cfg.people) for b in blocks[:3])
        return True, f"명당 {cfg.people}석: {picks}"
    avail = [s for s in smap.available
             if not cfg.pref_rows or _SEAT_RE.match(s).group(1) in cfg.pref_rows]
    if len(avail) >= cfg.people:
        head = ", ".join(sorted(avail, key=seat_sort_key)[:8])
        return True, f"{len(avail)}석 ({head}{' …' if len(avail) > 8 else ''})"
    return False, ""


# ══════════════════════════════════════════════════════════════════════
#  알림
# ══════════════════════════════════════════════════════════════════════
class Notifier:
    def __init__(self, cfg: Watch):
        # 회차별 cfg.sound 와 전역 설정을 모두 켰을 때만 소리를 낸다.
        self.sound = cfg.sound and SETTINGS.get("sound", True)
        self.webhook = cfg.webhook
        self.telegram = cfg.telegram or {}
        self.open_browser = cfg.open_browser

    def fire(self, headline: str, detail: str) -> None:
        if self.sound:
            threading.Thread(target=self._beep, daemon=True).start()
        text = f"{headline}\n{detail}\n{TICKETING_URL}"
        if self.webhook:
            threading.Thread(target=self._webhook, args=(text,), daemon=True).start()
        if self.telegram.get("token"):
            threading.Thread(target=self._telegram, args=(text,), daemon=True).start()
        if self.open_browser:
            try:
                import webbrowser
                webbrowser.open(TICKETING_URL)
            except Exception:
                pass

    def _beep(self) -> None:
        play_alert_sound()

    def _webhook(self, text: str) -> None:
        data = json.dumps({"content": text, "text": text}).encode()
        req = urllib.request.Request(self.webhook, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            pass

    def _telegram(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.telegram['token']}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": self.telegram.get("chat_id", ""),
                                       "text": text}).encode()
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10).read()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════
#  마법사
# ══════════════════════════════════════════════════════════════════════
def apply_cinema(cfg: Watch, cinema: Cinema, cat: Catalog) -> None:
    cfg.brand = cat.provider.code
    cfg.cinema_id, cfg.cinema_name = cinema.cinema_id, cinema.name
    cfg.region_code, cfg.cinema_key = cinema.region_code, cinema.key


def wiz_cinema(cat: Catalog, crumbs: list[str], fav: Favorites | None = None) -> Cinema:
    fav = fav or Favorites()
    favs = [c for c in cat.cinemas() if fav.has_cinema(c)]
    regions: list = list(cat.regions())
    if favs:
        regions.insert(0, ("★", f"즐겨찾는 지점 ({len(favs)}곳)"))
    region = Chooser("지역 선택", regions,
                     lambda r: starred(r[1], "" if r[0] == "★"
                                       else f"{len(cat.by_region(r[0]))}개 지점",
                                       r[0] == "★", right_width=12),
                     crumbs).run()

    cinemas = favs if region[0] == "★" else fav.sort_cinemas(cat.by_region(region[0]))
    return Chooser(f"지점 선택 - {region[1]}", cinemas,
                   lambda c: starred(c.name, c.addr, fav.has_cinema(c),
                                     right_width=30),
                   crumbs + [region[1]],
                   hint="'/' 검색 · ★ 는 즐겨찾기 (메뉴 > 즐겨찾기 관리)").run()


def wiz_movie(shows: list[Show], crumbs: list[str]) -> str:
    counts: dict[str, int] = {}
    for s in shows:
        counts[s.movie_name] = counts.get(s.movie_name, 0) + 1
    ranked = sorted(counts.items(), key=lambda x: -x[1])
    if not ranked:
        form("상영 영화", [f"{C.YELLOW}상영시간표가 아직 없습니다.{C.RESET}",
                       "영화 제목을 직접 입력하세요 (일부만 입력해도 됩니다)"], crumbs)
        return ask_text("영화 제목")
    picked = Chooser("상영 영화 선택", ranked,
                     lambda m: two_col(m[0], f"{m[1]:>3}회차", right_width=10), crumbs,
                     extra=(f"{glyph('✎', '*')} 직접 입력 (예매 시작 전 영화)", None)).run()
    if picked is None:
        form("영화 제목 직접 입력", ["제목 일부만 입력해도 됩니다. 예) 오디세이"], crumbs)
        return ask_text("영화 제목")
    return picked[0]


def wiz_weekdays(crumbs: list[str]) -> list[int]:
    items = list(enumerate(WEEKDAY_KR))
    chosen = Chooser("요일 선택 (여러 개 선택 가능)", items, lambda w: f"{w[1]}요일", crumbs,
                     multi=True,
                     hint="Space 로 고르고 Enter. 아무것도 안 고르면 전체 요일").run()
    return [] if len(chosen) == 7 else [w[0] for w in chosen]


def collect_screens(shows: list[Show], movie: str = "") -> list[tuple[str, int]]:
    screens: dict[str, int] = {}
    for s in shows:
        if movie and movie.replace(" ", "") not in s.movie_name.replace(" ", ""):
            continue
        screens[s.screen_name] = max(screens.get(s.screen_name, 0), s.total_seats)
    return sorted(screens.items())


def wiz_screens(shows: list[Show], movie: str, crumbs: list[str],
                cinema: Cinema | None = None, fav: Favorites | None = None) -> list[str]:
    fav = fav or Favorites()
    items = collect_screens(shows, movie)
    if not items:
        return []
    items = fav.sort_screens(cinema, items)             # 즐겨찾기를 맨 위로
    preselect = {i for i, s in enumerate(items) if fav.has_screen(cinema, s[0])}
    hint = "Space 로 고르고 Enter. 전체를 보려면 아무것도 고르지 말고 Enter"
    if preselect:
        hint = f"★ 즐겨찾는 상영관 {len(preselect)}개가 미리 선택돼 있습니다. " + hint
    chosen = Chooser("상영관 선택 (여러 개 선택 가능)", items,
                     lambda s: starred(s[0], f"{s[1]:>3}석",
                                       fav.has_screen(cinema, s[0]), right_width=10),
                     crumbs, multi=True, hint=hint, preselect=preselect).run()
    return [] if len(chosen) == len(items) else [c[0] for c in chosen]


def wiz_time_range(cfg: Watch, crumbs: list[str]) -> None:
    presets = [("제한 없음", ("", "")), ("오전 (~12:00)", ("", "12:00")),
               ("오후 (12:00~18:00)", ("12:00", "18:00")),
               ("저녁 (18:00~)", ("18:00", "")), ("직접 입력", None)]
    picked = Chooser("시간대 선택", presets, lambda p: p[0], crumbs).run()
    if picked[1] is None:
        form("시간대 직접 입력", ["비워두면 제한 없음. 예) 18:00"], crumbs)
        cfg.time_from = ask_text("이 시각 이후")
        cfg.time_to = ask_text("이 시각 이전")
    else:
        cfg.time_from, cfg.time_to = picked[1]


def show_seatmap_preview(cat: Catalog, cfg: Watch, shows: list[Show],
                         crumbs: list[str], show_sweet: bool = False) -> None:
    if not cat.provider.supports_seats:      # 좌석표가 없는 브랜드는 미리보기 없음
        return
    cands = [s for s in filter_shows(shows, cfg) if s.total_seats > 0]
    if not cands:
        return
    question = ("명당이 어디인지 좌석표로 확인할까요?" if show_sweet
                else "좌석 번호 확인을 위해 좌석표를 볼까요?")
    if not ask_yes(question, True):
        return
    pick = cands[0] if len(cands) == 1 else Chooser(
        "어느 회차의 좌석표를 볼까요", cands[:20], lambda s: s.label(), crumbs).run()
    clear_screen()
    print("\n".join(header(f"좌석표 - {pick.short()}", crumbs)))
    try:
        smap = cat.seats(pick)
        spots = sweet_seats(smap, cfg.sweet_ratio) if show_sweet else None
        for line in seatmap_lines(smap, spots):
            print(box_row(line))
        if spots:
            scores = sweet_scores(smap)
            best = sorted(spots, key=lambda k: -scores[k])[:8]
            print(box_row(f"{C.BYELLOW}명당 순위{C.RESET} {', '.join(best)}"))
    except LotteApiError as exc:
        print(box_row(f"{C.YELLOW}좌석표를 불러오지 못했습니다: {exc}{C.RESET}"))
    print(box_bottom())
    read_line(f"   {C.DIM}Enter 를 누르면 계속{C.RESET}")


def screen_seat_labels(cat: Catalog, cfg: Watch, shows: list[Show],
                       limit: int = 6) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for show in filter_shows(shows, cfg):
        if show.screen_name in out or len(out) >= limit:
            continue
        try:
            smap = cat.seats(show)
        except (LotteApiError, Unsupported):
            continue
        out[show.screen_name] = smap.available | smap.taken
    return out


def validate_seat_groups(cat: Catalog, cfg: Watch, shows: list[Show]) -> bool:
    """입력한 좌석이 실제 상영관에 있는지 확인 (없는 번호를 넣으면 영원히 안 울리므로)."""
    if not cat.provider.supports_seats:
        return True
    wanted = {s for g in cfg.seat_groups for s in g}
    print(f"   {C.DIM}상영관 좌석 배치와 대조하는 중...{C.RESET}")
    layouts = screen_seat_labels(cat, cfg, shows)
    if not layouts:
        return True
    ok = True
    for screen, labels in layouts.items():
        missing = sorted(wanted - labels, key=seat_sort_key)
        if missing:
            ok = False
            print(f"   {C.BRED}✕{C.RESET} [{screen}] 에 없는 좌석: "
                  f"{C.YELLOW}{', '.join(missing)}{C.RESET}")
    if ok:
        print(f"   {C.BGREEN}✓{C.RESET} 좌석 확인 완료 ({', '.join(layouts)})")
    return ok


def wiz_seats(cfg: Watch, crumbs: list[str], supports_seats: bool = True) -> None:
    if not supports_seats:
        form("인원 수", [
            "이 브랜드는 좌석표를 공개하지 않아 좌석 지정이 안 됩니다.",
            f"{C.DIM}대신 '잔여석이 N석 이상 생기면' 조건으로 감시합니다.{C.RESET}",
        ], crumbs)
        cfg.seat_mode = "any"
        cfg.people = ask_int("인원 수", cfg.people or 2, 1, 20)
        return
    modes = [("groups", "좌석 직접 지정", "예) G22,G23 또는 F22,F23"),
             ("sweet", "명당 자동 추천", "좌우 중앙 · 앞에서 62% 지점"),
             ("consecutive", "붙어있는 연속 N석", "통로로 끊기지 않은 자리"),
             ("any", "아무 자리나 N석 이상", "일단 자리만 나면 알림")]
    mode = Chooser("좌석 조건", modes,
                   lambda m: f"{pad(m[1], 22)}{C.DIM}{m[2]}{C.RESET}", crumbs).run()[0]
    cfg.seat_mode = mode
    if mode == "groups":
        while True:
            form("좌석 직접 지정", [
                "같이 앉을 자리는 콤마( , ) 로 묶고, 대안은 파이프( | ) 로 구분합니다.",
                f"{C.DIM}예) G22,G23 | F22,F23   -> 두 자리가 모두 빈 그룹이 생기면 알림{C.RESET}",
            ], crumbs)
            try:
                cfg.seat_groups = parse_seat_groups(ask_text("좌석", "G22,G23 | F22,F23"))
            except ValueError as exc:
                print(f"   {C.YELLOW}{exc}{C.RESET}")
                time.sleep(1.2)
                continue
            cfg.people = len(cfg.seat_groups[0])
            break
    elif mode == "sweet":
        form("명당 자동 추천", [
            "좌석 좌표로 '좌우 정중앙 · 앞에서 62% 뒤' 에 가까운 순으로 점수를 매깁니다.",
            f"{C.DIM}예) 월드타워 21관 상위 좌석 = G22, G23, G21, G24 …{C.RESET}",
        ], crumbs)
        cfg.people = ask_int("인원 수 (붙어있는 자리로 찾습니다)", cfg.people or 2, 1, 20)
        grades = [(0.1, "상위 10%", "가장 좋은 자리만"), (0.2, "상위 20%", "기본"),
                  (0.3, "상위 30%", "여유롭게"), (0.5, "상위 50%", "웬만하면 OK")]
        cfg.sweet_ratio = Chooser("명당 범위", grades,
                                  lambda g: two_col(g[1], g[2], right_width=18),
                                  crumbs).run()[0]
    else:
        form("인원 / 선호 열", ["연속석·잔여석 판정에 쓰입니다."], crumbs)
        cfg.people = ask_int("인원 수", cfg.people or 2, 1, 20)
        if mode == "consecutive":
            raw = ask_text("선호 열 (예: F,G,H / 비우면 전체)")
            cfg.pref_rows = [r.strip().upper().rstrip("열")
                             for r in raw.replace(" ", ",").split(",") if r.strip()]


def wiz_notify(cfg: Watch, crumbs: list[str]) -> None:
    presets = [(10, "10초  - 취소표 노림"), (20, "20초  - 기본"),
               (30, "30초  - 여유"), (60, "60초  - 넉넉히"), (0, "직접 입력")]
    picked = Chooser("확인 주기", presets, lambda p: p[1], crumbs).run()[0]
    if picked == 0:
        form("확인 주기 직접 입력", ["5~3600초"], crumbs)
        cfg.interval = ask_int("주기(초)", 20, 5, 3600)
    else:
        cfg.interval = picked

    cfg.sound = ask_yes("소리 알림을 켤까요?", True)
    cfg.open_browser = ask_yes("조건 충족 시 예매 페이지를 자동으로 열까요?", False)
    if ask_yes("웹훅 / 텔레그램 알림을 추가할까요?", False):
        form("외부 알림", ["필요 없는 항목은 그냥 Enter"], crumbs)
        cfg.webhook = ask_text("Discord/Slack 웹훅 URL")
        token = ask_text("텔레그램 봇 토큰")
        if token:
            cfg.telegram = {"token": token, "chat_id": ask_text("텔레그램 chat_id")}
    cfg.repeat_alert = ask_yes("조건이 유지되는 동안 계속 알릴까요? (아니오 = 바뀔 때만)", False)


def parse_date_input(raw: str) -> list[str]:
    today = date.today()
    out: list[str] = []
    for tok in re.split(r"[,\s]+", raw.strip()):
        if not tok:
            continue
        m = re.match(r"^(\d{4})[-./](\d{1,2})[-./](\d{1,2})$", tok)
        if m:
            y, mo, d = map(int, m.groups())
        else:
            m = re.match(r"^(\d{1,2})[-./](\d{1,2})$", tok)
            if not m:
                raise ValueError(f"날짜 형식을 알 수 없습니다: {tok!r} (예: 9/8)")
            mo, d = map(int, m.groups())
            y = today.year + (1 if (mo, d) < (today.month, today.day) else 0)
        out.append(date(y, mo, d).isoformat())
    if not out:
        raise ValueError("날짜가 입력되지 않았습니다.")
    return out


def wizard_seat(cat: Catalog, fav: Favorites) -> Watch:
    cfg = Watch(mode="seat")
    crumbs = ["좌석 감시"]
    cinema = wiz_cinema(cat, crumbs, fav)
    apply_cinema(cfg, cinema, cat)
    crumbs = crumbs + [cinema.name]

    ranges = [(7, "1주일"), (14, "2주일"), (30, "1개월"), (3, "3일")]
    cfg.days_ahead = Chooser("며칠 앞까지 감시할까요", ranges,
                             lambda r: f"{pad(r[1], 10)}{C.DIM}오늘부터 {r[0]}일{C.RESET}",
                             crumbs).run()[0]

    clear_screen()
    print("\n".join(header(f"{cinema.name} 상영표 수집", crumbs)))
    print(box_bottom())
    shows = cat.scan(cinema, min(cfg.days_ahead, 14))

    cfg.movie = wiz_movie(shows, crumbs)
    crumbs = crumbs + [cfg.movie]
    cfg.weekdays = wiz_weekdays(crumbs)
    cfg.screens = wiz_screens(shows, cfg.movie, crumbs, cinema, fav)
    wiz_time_range(cfg, crumbs)
    show_seatmap_preview(cat, cfg, shows, crumbs)

    while True:
        wiz_seats(cfg, crumbs, cat.provider.supports_seats)
        if cfg.seat_mode == "sweet":
            show_seatmap_preview(cat, cfg, shows, crumbs, show_sweet=True)
            break
        if cfg.seat_mode != "groups":
            break
        print()
        if validate_seat_groups(cat, cfg, shows):
            time.sleep(0.8)
            break
        print()
        if not ask_yes("좌석을 다시 입력할까요?", True):
            break

    wiz_notify(cfg, crumbs)
    return cfg


def browse_seats(cat: Catalog, fav: Favorites) -> None:
    """감시를 걸지 않고 회차 하나를 골라 좌석표(또는 잔여석)를 바로 본다.

    좌석표를 제공하는 브랜드(롯데·메가박스)는 좌석 배치를 그리고,
    상영표만 되는 브랜드(CGV)는 회차별 잔여석을 보여준다.
    """
    brand = cat.provider
    crumbs = ["좌석 확인"]
    cinema = wiz_cinema(cat, crumbs, fav)
    crumbs = crumbs + [cinema.name]

    # 날짜 고르기 (오늘부터 14일)
    today = date.today()
    days = [(today + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(14)]

    def day_label(d: str) -> str:
        dt = datetime.strptime(d, "%Y-%m-%d")
        delta = (dt.date() - today).days
        when = "오늘" if delta == 0 else ("내일" if delta == 1 else f"D+{delta}")
        return two_col(f"{d[5:]} ({WEEKDAY_KR[dt.weekday()]})", when, right_width=10)

    while True:
        day = Chooser("날짜 선택", days, day_label, crumbs).run()

        clear_screen()
        print("\n".join(header(f"{cinema.name} {day[5:]} 상영표", crumbs)))
        print(box_bottom())
        try:
            shows = cat.merge_rows(cat.shows(cinema, day))
        except (LotteApiError, Unsupported) as exc:
            print(f"   {C.BRED}상영표를 불러오지 못했습니다: {exc}{C.RESET}")
            read_line(f"   {C.DIM}Enter 로 계속{C.RESET}")
            return
        if not shows:
            print(f"   {C.YELLOW}이 날짜에는 상영 정보가 없습니다."
                  f" (아직 예매가 열리지 않았을 수 있습니다){C.RESET}")
            read_line(f"   {C.DIM}Enter 로 계속{C.RESET}")
            continue

        shows.sort(key=lambda s: (s.start, s.screen_name))

        def show_label(s: Show) -> str:
            if s.total_seats:
                ratio = s.open_seats / s.total_seats
                color = (C.BRED if ratio <= 0.05 else
                         C.BYELLOW if ratio <= 0.3 else C.BGREEN)
            else:
                color = C.DIM
            left = f"{s.start} {trim(s.movie_name, 22)}"
            right = (f"{trim(s.screen_name, 16)} "
                     f"{color}{s.open_seats:>3}{C.RESET}{C.DIM}/{s.total_seats}{C.RESET}")
            return f"{cell(left, 34)}{right}"

        picked = Chooser(f"{day[5:]} 회차 선택 ({len(shows)}개)", shows, show_label,
                         crumbs + [day[5:]],
                         hint="Enter 로 좌석을 봅니다 · / 로 영화 검색").run()
        view_one_show(cat, picked, crumbs + [day[5:]])
        if not ask_yes("다른 회차를 더 볼까요?", True):
            return


def view_one_show(cat: Catalog, show: Show, crumbs: list[str]) -> None:
    """회차 하나의 좌석표를 그린다. r 로 새로고침."""
    brand = cat.provider
    while True:
        clear_screen()
        lines = header(f"{show.movie_name}", crumbs)
        lines.append(box_row(f"{C.DIM}{pad('일시', 8)}{C.RESET}{C.BWHITE}"
                             f"{show.play_date} {show.start}~{show.end}{C.RESET}"))
        lines.append(box_row(f"{C.DIM}{pad('상영관', 8)}{C.RESET}{C.BWHITE}"
                             f"{show.screen_name}{C.RESET}"
                             f"{C.DIM}  {show.film}{C.RESET}"))
        lines.append(box_row(f"{C.DIM}{pad('잔여석', 8)}{C.RESET}"
                             f"{C.BGREEN}{show.open_seats}{C.RESET}"
                             f"{C.DIM} / {show.total_seats}석{C.RESET}"))
        lines.append(box_mid())
        print("\n".join(lines))

        if not brand.supports_seats:
            print(box_row(f"{C.DIM}{brand.name} 는 좌석표를 제공하지 않아"
                          f" 잔여석까지만 확인됩니다.{C.RESET}"))
            print(box_bottom())
            read_line(f"   {C.DIM}Enter 로 돌아가기{C.RESET}")
            return

        print(box_bottom())
        try:
            smap = cat.seats(show)
        except (LotteApiError, Unsupported) as exc:
            print(f"   {C.BRED}좌석표를 불러오지 못했습니다: {exc}{C.RESET}")
            read_line(f"   {C.DIM}Enter 로 돌아가기{C.RESET}")
            return

        sweet = sweet_seats(smap, 0.2) if smap.coord else set()
        print()
        print("\n".join(seatmap_lines(smap, sweet)))
        print(f"\n{C.DIM}   스크린 방향은 위쪽입니다. 통로는 빈칸.{C.RESET}")
        if smap.total and smap.total != show.total_seats:
            print(f"{C.DIM}   상영표 총석 {show.total_seats} · 좌석표 {smap.total}"
                  f"{C.RESET}")
        print(keyhint(("r", "새로고침"), ("Enter", "돌아가기")))
        key = read_key() if UI["tty"] else "enter"
        if key not in ("r", "R"):
            return


def wizard_open(cat: Catalog, fav: Favorites) -> Watch:
    cfg = Watch(mode="open")
    crumbs = ["예매오픈 감시"]
    cinema = wiz_cinema(cat, crumbs, fav)
    apply_cinema(cfg, cinema, cat)
    crumbs = crumbs + [cinema.name]

    clear_screen()
    print("\n".join(header(f"{cinema.name} 상영표 수집", crumbs)))
    print(box_bottom())
    shows = cat.scan(cinema, 7)

    cfg.movie = wiz_movie(shows, crumbs)
    crumbs = crumbs + [cfg.movie]

    while True:
        form("감시할 날짜", ["예) 9/8   또는   9/8, 9/9   또는   2026-09-08"], crumbs)
        try:
            cfg.dates = parse_date_input(ask_text("날짜"))
            break
        except ValueError as exc:
            print(f"   {C.YELLOW}{exc}{C.RESET}")
            time.sleep(1.2)

    if shows and ask_yes("특정 상영관만 볼까요?", False):
        cfg.screens = wiz_screens(shows, cfg.movie, crumbs, cinema, fav)

    wiz_notify(cfg, crumbs)
    return cfg


# ══════════════════════════════════════════════════════════════════════
#  라이브 대시보드
# ══════════════════════════════════════════════════════════════════════
class State:
    def __init__(self, cfg: Watch):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.started = time.time()
        self.cycle = 0
        self.phase = "준비 중"
        self.prog = (0, 0)
        self.checked = 0
        self.matched = 0
        self.last_done = "-"
        self.next_at = 0.0
        self.calls = 0
        self.hits: deque[tuple[str, str]] = deque(maxlen=4)
        self.changes: deque[tuple[str, str]] = deque(maxlen=4)
        self.notes: deque[str] = deque(maxlen=2)
        self.tiers: dict[str, int] = {}
        self.horizon = None
        self.events = 0
        self.watching = 0
        self.skipped = 0
        self.last_seat = "-"
        self.date_status: dict[str, str] = {}
        self.finished = False
        self.stop = threading.Event()

    def set(self, **kw) -> None:
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def hit(self, when: str, text: str) -> None:
        with self.lock:
            self.hits.appendleft((when, text))

    def change(self, when: str, text: str) -> None:
        with self.lock:
            self.changes.appendleft((when, text))

    def note(self, text: str) -> None:
        with self.lock:
            self.notes.appendleft(text)


def hms(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def bar(done: int, total: int, width: int = 22) -> str:
    if total <= 0:
        return f"{C.DIM}{glyph('░', '-') * width}{C.RESET}"
    filled = int(width * done / total)
    return (f"{C.BGREEN}{glyph('█', '#') * filled}{C.RESET}"
            f"{C.DIM}{glyph('░', '-') * (width - filled)}{C.RESET}")


def dashboard_lines(st: State) -> list[str]:
    cfg = st.cfg
    now = datetime.now()
    with st.lock:
        cycle, phase, (pi, pn) = st.cycle, st.phase, st.prog
        checked, matched, last_done = st.checked, st.matched, st.last_done
        hits, notes, next_at = list(st.hits), list(st.notes), st.next_at
        calls, date_status = st.calls, dict(st.date_status)
        changes, tiers = list(st.changes), dict(st.tiers)
        watching, skipped, last_seat = st.watching, st.skipped, st.last_seat
        horizon, events = st.horizon, st.events

    mode_name = {"seat": "좌석 감시", "open": "예매오픈 감시",
                 "radar": "오픈 패턴 관측"}.get(cfg.mode, cfg.mode)
    lines = [
        f"{C.BRED}{C.BOLD}LOTTE CINEMA WATCH{C.RESET}  {C.DIM}v{VERSION}{C.RESET}   "
        f"{C.ON_RED}{C.BWHITE} {mode_name} {C.RESET}",
        "",
        box_top("감시 조건"),
    ]
    for key, val in cfg.fields():
        lines.append(box_row(f"{C.DIM}{pad(key, 10)}{C.RESET}{C.BWHITE}{val}{C.RESET}"))

    lines.append(box_mid())
    spin = glyph("◐◓◑◒", "|/-\\")[int(time.time() * 4) % 4]
    live = f"{C.BGREEN}{spin}{C.RESET}" if not st.finished else f"{C.GREEN}✓{C.RESET}"
    lines.append(box_row(
        f"{C.DIM}{pad('현재시각', 10)}{C.RESET}{C.BWHITE}{now:%Y-%m-%d %H:%M:%S}{C.RESET}"
        f"     {C.DIM}가동{C.RESET} {hms(time.time() - st.started)}"))
    lines.append(box_row(
        f"{C.DIM}{pad('확인횟수', 10)}{C.RESET}{live} "
        f"{C.BYELLOW}{C.BOLD}#{cycle}{C.RESET} 번째 확인"
        f"     {C.DIM}API 호출{C.RESET} {calls}회"))

    if pn:
        lines.append(box_row(f"{C.DIM}{pad('진행', 10)}{C.RESET}{bar(pi, pn)} "
                             f"{pi}/{pn}  {C.DIM}{phase}{C.RESET}"))
    else:
        countdown = max(next_at - time.time(), 0)
        state = f"{C.DIM}{phase}{C.RESET}"
        if countdown > 0:
            state += f"   {C.DIM}다음 확인까지{C.RESET} {C.BWHITE}{int(countdown)}초{C.RESET}"
        lines.append(box_row(f"{C.DIM}{pad('진행', 10)}{C.RESET}{state}"))

    if cfg.mode == "seat":
        tier_text = " · ".join(f"{name} {tiers[name]}" for name in Scheduler.TIERS
                               if tiers.get(name)) or "-"
        lines.append(box_row(
            f"{C.DIM}{pad('감시대상', 10)}{C.RESET}{C.BWHITE}{watching}회차{C.RESET}   "
            f"{C.DIM}{tier_text}{C.RESET}"))
        saved = f"{C.DIM}생략{C.RESET} {skipped}   " if cfg.adaptive else ""
        lines.append(box_row(
            f"{C.DIM}{pad('직전결과', 10)}{C.RESET}{last_done}   "
            f"{C.DIM}좌석조회{C.RESET} {checked}   {saved}"
            f"{C.DIM}충족{C.RESET} "
            f"{(C.BGREEN if matched else C.DIM)}{matched}건{C.RESET}"))
        if cfg.adaptive:
            lines.append(box_row(
                f"{C.DIM}{pad('적응형', 10)}잔여석이 변하면 즉시 확인 · "
                f"마지막 좌석조회 {last_seat}{C.RESET}"))
    elif cfg.mode == "radar":
        if horizon:
            wd = WEEKDAY_KR[date.fromisoformat(horizon.last_open).weekday()]                 if horizon.last_open else "-"
            lines.append(box_row(
                f"{C.DIM}{pad('예매 끝', 10)}{C.RESET}{C.BWHITE}{horizon.last_open}"
                f"({wd}){C.RESET}   {C.DIM}오늘부터 D+{horizon.days}{C.RESET}"))
        lines.append(box_row(
            f"{C.DIM}{pad('포착', 10)}{C.RESET}오픈 순간 "
            f"{(C.BGREEN if events else C.DIM)}{events}회{C.RESET}"
            f"   {C.DIM}기록 {os.path.basename(OPENLOG_FILE)}{C.RESET}"))
    else:
        for day, status in sorted(date_status.items()):
            wd = WEEKDAY_KR[datetime.strptime(day, "%Y-%m-%d").weekday()]
            color = C.BGREEN if status.startswith("오픈") else C.DIM
            lines.append(box_row(f"{C.DIM}{pad(day + f'({wd})', 16)}{C.RESET}"
                                 f"{color}{status}{C.RESET}"))

    for note in notes:
        lines.append(box_row(f"{C.YELLOW}! {note}{C.RESET}"))

    if cfg.mode in ("seat", "radar") and (cfg.mode == "seat" or changes):
        lines.append(box_mid())
        title_txt = ("좌석 변동   " + f"{C.DIM}잔여석이 늘면 = 취소표{C.RESET}"
                     if cfg.mode == "seat" else "관측 기록")
        lines.append(box_row(f"{C.BOLD}{title_txt}{C.RESET}"))
        if changes:
            for when, text in changes:
                lines.append(box_row(f"  {C.CYAN}{glyph('◆', '*')}{C.RESET} "
                                     f"{C.DIM}{when}{C.RESET}  {text}"))
        else:
            lines.append(box_row(f"  {C.DIM}아직 변동 없음{C.RESET}"))

    lines.append(box_mid())
    lines.append(box_row(f"{C.BOLD}발견 기록{C.RESET}"))
    if hits:
        for when, text in hits:
            lines.append(box_row(f"  {C.BGREEN}{glyph('▶', '>')}{C.RESET} "
                                 f"{C.DIM}{when}{C.RESET}  {text}"))
    else:
        lines.append(box_row(f"  {C.DIM}아직 없음 - 조건이 충족되면 여기에 쌓입니다{C.RESET}"))
    lines.append(box_bottom())
    lines.append(keyhint(("Ctrl+C", "감시 중지 후 메뉴로")))
    return lines


# ══════════════════════════════════════════════════════════════════════
#  감시 루프 (워커 스레드)
# ══════════════════════════════════════════════════════════════════════
def worker_seat(cat: Catalog, cfg: Watch, note: Notifier, st: State) -> None:
    seen: dict[str, str] = {}
    warned: set[str] = set()
    tracker = ShowTracker(cfg, log=cfg.keep_history)
    sched = Scheduler(cfg)

    while not st.stop.is_set():
        st.set(cycle=st.cycle + 1)
        began = time.monotonic()

        today = date.today()
        days = [(today + timedelta(days=i)).isoformat() for i in range(cfg.days_ahead)]
        if cfg.weekdays:
            days = [d for d in days
                    if datetime.strptime(d, "%Y-%m-%d").weekday() in cfg.weekdays]

        # 1) 상영표 갱신 - 호출 1번에 그 날짜 전 회차의 잔여석이 딸려온다.
        st.set(phase="상영표 확인 중", prog=(0, len(days)))
        targets: list[Show] = []
        for i, day in enumerate(days, 1):
            if st.stop.is_set():
                return
            try:
                ttl = sched.day_ttl(day) if cfg.adaptive else 300
                targets += filter_shows(cat.shows(cfg.cinema, day, ttl=ttl), cfg)
            except LotteApiError as exc:
                st.note(f"{day} 상영표 조회 실패: {exc}")
            st.set(prog=(i, len(days)), calls=cat.provider.calls)

        # 2) 잔여석 변동 관측 - 늘어났으면 그 자리에서 누가 취소한 것.
        deltas: dict[str, int] = {}
        tiers: dict[str, int] = {}
        for show in targets:
            delta = tracker.observe(show)
            deltas[show.key] = delta
            tiers[sched.tier(show)] = tiers.get(sched.tier(show), 0) + 1
            if delta:
                arrow = f"{C.BGREEN}+{delta}{C.RESET}" if delta > 0 else f"{C.DIM}{delta}{C.RESET}"
                st.change(datetime.now().strftime("%H:%M:%S"),
                          f"{show.short()}  {show.open_seats - delta}"
                          f"{glyph('→', '->')}{show.open_seats}석 ({arrow})")

        live = [s for s in targets if s.open_seats > 0 and s.bookable]
        for s in targets:
            if s.open_seats <= 0:
                seen.pop(s.key, None)

        # 3) 확인할 회차 선별 - 취소표가 뜬 회차 먼저, 그다음 예정 시각이 된 회차.
        now = time.monotonic()
        queue: list[tuple[int, Show, str]] = []
        skipped = 0
        for show in live:
            if cfg.adaptive:
                go, why = sched.should_check(show, tracker, deltas.get(show.key, 0), now)
            else:
                go, why = True, "정기 확인"
            if go:
                rank = 0 if why == "취소표 감지" else 1
                queue.append((rank, show, why))
            else:
                skipped += 1
        queue.sort(key=lambda q: (q[0], Scheduler.show_dt(q[1])))

        # 4) 좌석 확인
        st.set(phase="좌석 확인 중", prog=(0, len(queue)), tiers=tiers,
               watching=len(live), skipped=skipped)
        checked = matched = 0
        for i, (rank, show, why) in enumerate(queue, 1):
            if st.stop.is_set():
                return
            checked += 1
            if not cat.provider.supports_seats:      # 잔여석만 아는 브랜드
                sched.mark_checked(show, tracker, time.monotonic())
                st.set(prog=(i, len(queue)), calls=cat.provider.calls)
                if show.open_seats < max(cfg.people, 1):
                    seen.pop(show.key, None)
                    continue
                matched += 1
                detail = f"잔여 {show.open_seats}석"
                if not cfg.repeat_alert and seen.get(show.key) == detail:
                    continue
                seen[show.key] = detail
                flag = f"{C.BYELLOW}[취소표]{C.RESET} " if rank == 0 else ""
                st.hit(datetime.now().strftime("%H:%M:%S"),
                       f"{flag}{C.BWHITE}{show.short()}{C.RESET}  "
                       f"{C.BGREEN}{detail}{C.RESET}")
                note.fire("[자리 발견] " + cfg.movie,
                          f"{show.label()}\n{tracker.context(show)}")
                continue
            try:
                smap = cat.seats(show)
            except (LotteApiError, Unsupported) as exc:
                st.note(f"좌석 조회 실패 [{show.short()}]: {exc}")
                st.set(prog=(i, len(queue)), calls=cat.provider.calls)
                continue
            sched.mark_checked(show, tracker, time.monotonic())
            st.set(last_seat=datetime.now().strftime("%H:%M:%S"))

            if cfg.seat_mode == "groups" and show.screen_name not in warned:
                exists = smap.available | smap.taken
                if all(any(s not in exists for s in g) for g in cfg.seat_groups):
                    warned.add(show.screen_name)
                    st.note(f"[{show.screen_name}] 에는 지정한 좌석이 없습니다 (번호 확인)")

            ok, detail = evaluate_seats(cfg, smap)
            st.set(prog=(i, len(queue)), calls=cat.provider.calls)
            if not ok:
                seen.pop(show.key, None)
                continue
            matched += 1
            if not cfg.repeat_alert and seen.get(show.key) == detail:
                continue
            seen[show.key] = detail
            flag = f"{C.BYELLOW}[취소표]{C.RESET} " if rank == 0 else ""
            st.hit(datetime.now().strftime("%H:%M:%S"),
                   f"{flag}{C.BWHITE}{show.short()}{C.RESET}  {C.BGREEN}{detail}{C.RESET}")
            note.fire("[좌석 발견] " + cfg.movie,
                      f"{show.label()}\n가능 좌석: {detail}\n{tracker.context(show)}")

        elapsed = time.monotonic() - began
        st.set(phase="대기 중", prog=(0, 0), checked=checked, matched=matched,
               watching=len(live), skipped=skipped, tiers=tiers, calls=cat.provider.calls,
               last_done=datetime.now().strftime("%H:%M:%S"),
               next_at=time.time() + max(cfg.interval - elapsed, 1))
        if elapsed > cfg.interval:
            st.note(f"한 바퀴에 {elapsed:.0f}초 - 상영관/요일을 좁히면 더 자주 확인합니다")
        st.stop.wait(max(cfg.interval - elapsed, 1.0))


def worker_open(cat: Catalog, cfg: Watch, note: Notifier, st: State) -> None:
    pending = list(cfg.dates)
    st.set(date_status={d: "미오픈" for d in pending})
    radar = OpenRadar(cat)

    def refresh_forecast() -> None:
        """현재 예매 범위를 재고, 아직 안 열린 날짜의 예상 오픈 시점을 붙인다."""
        try:
            snap = radar.scan(cfg.cinema, cfg.movie)
        except LotteApiError:
            return
        if not snap.last_open:
            return
        OpenRadar.log(snap)
        st.set(horizon=snap)
        for day in pending:
            pred = radar.predict(cfg.cinema, cfg.movie, day, snap)
            if pred["status"] in ("예상", "추정"):
                with st.lock:
                    st.date_status[day] = f"미오픈 · 예상 {pred['text']}"

    while pending and not st.stop.is_set():
        st.set(cycle=st.cycle + 1, phase="예매 오픈 확인 중", prog=(0, len(pending)))
        began = time.monotonic()
        if st.cycle == 1 or st.cycle % 30 == 0:
            st.set(phase="예매 범위 측정 중")
            refresh_forecast()
            st.set(phase="예매 오픈 확인 중")
        for i, day in enumerate(list(pending), 1):
            if st.stop.is_set():
                return
            try:
                shows = filter_shows(cat.shows(cfg.cinema, day), cfg)
                st.set(calls=cat.provider.calls)
            except LotteApiError as exc:
                st.note(f"{day} 조회 실패: {exc}")
                continue
            finally:
                st.set(prog=(i, len(pending)))

            bookable = [s for s in shows if s.bookable]
            if not bookable:
                continue
            with st.lock:
                st.date_status[day] = f"오픈됨 - {len(bookable)}회차"
            preview = ", ".join(f"{s.start} {s.screen_name}" for s in bookable[:4])
            st.hit(datetime.now().strftime("%H:%M:%S"),
                   f"{C.BWHITE}{day}{C.RESET} {C.BGREEN}예매 오픈{C.RESET} "
                   f"{len(bookable)}회차  {C.DIM}{preview}{C.RESET}")
            detail = "\n".join(f"{s.start}~{s.end}  {s.screen_name} [{s.film}]  "
                               f"잔여 {s.open_seats}/{s.total_seats}" for s in bookable[:15])
            note.fire(f"[예매 오픈] {cfg.movie} {day}",
                      f"{cfg.cinema_name}  {len(bookable)}회차\n{detail}")
            if not cfg.repeat_alert:
                pending.remove(day)

        if not pending:
            break
        elapsed = time.monotonic() - began
        st.set(phase="대기 중", prog=(0, 0),
               last_done=datetime.now().strftime("%H:%M:%S"),
               next_at=time.time() + max(cfg.interval - elapsed, 1))
        st.stop.wait(max(cfg.interval - elapsed, 1.0))
    st.set(phase="모든 날짜가 열렸습니다", finished=True, prog=(0, 0))


def worker_radar(cat: Catalog, cfg: Watch, note: Notifier, st: State) -> None:
    """예매 범위(horizon)를 주기적으로 재서 '열리는 순간'을 포착하고 기록한다."""
    radar = OpenRadar(cat)
    cinema = cfg.cinema
    prev: Horizon | None = None
    for row in OpenRadar.history(cinema.cinema_id, cfg.movie)[-1:]:
        prev = Horizon(datetime.fromisoformat(row["t"]), cinema.cinema_id, cfg.movie,
                       row.get("last_open", ""), int(row.get("days", 0)))

    while not st.stop.is_set():
        st.set(cycle=st.cycle + 1, phase="예매 범위 측정 중", prog=(0, 0))
        began = time.monotonic()
        try:
            snap = radar.scan(cinema, cfg.movie)
        except LotteApiError as exc:
            st.note(f"측정 실패: {exc}")
            snap = None

        if snap and snap.last_open:
            OpenRadar.log(snap)
            grew = prev and snap.last_open > prev.last_open
            if grew:
                added = (date.fromisoformat(snap.last_open)
                         - date.fromisoformat(prev.last_open)).days
                st.hit(datetime.now().strftime("%H:%M:%S"),
                       f"{C.BWHITE}예매 오픈{C.RESET} {prev.last_open[5:]}"
                       f"{glyph('→', '->')}{snap.last_open[5:]} "
                       f"{C.BGREEN}(+{added}일){C.RESET}")
                note.fire(f"[예매 오픈] {cfg.movie or '전체'} @ {cinema.name}",
                          f"{prev.last_open} -> {snap.last_open} (+{added}일)\n"
                          f"이제 D+{snap.days} 까지 예매할 수 있습니다.")
            elif prev is None:
                st.change(datetime.now().strftime("%H:%M:%S"),
                          f"기준점 기록: {snap.last_open} (D+{snap.days})")
            prev = snap
            events = OpenRadar.open_events(OpenRadar.history(cinema.cinema_id, cfg.movie))
            st.set(horizon=snap, events=len(events))

        elapsed = time.monotonic() - began
        st.set(phase="대기 중", calls=cat.provider.calls,
               last_done=datetime.now().strftime("%H:%M:%S"),
               next_at=time.time() + max(cfg.interval - elapsed, 1))
        st.stop.wait(max(cfg.interval - elapsed, 1.0))


def run_monitor(cat: Catalog, cfg: Watch) -> None:
    st = State(cfg)
    note = Notifier(cfg)
    target = {"seat": worker_seat, "open": worker_open, "radar": worker_radar}[cfg.mode]
    thread = threading.Thread(target=target, args=(cat, cfg, note, st), daemon=True)

    clear_screen()
    hide_cursor()
    thread.start()
    last_sig = None
    try:
        while thread.is_alive():
            if UI["ansi"]:                     # 제자리 갱신 (초시계까지 실시간)
                paint(dashboard_lines(st))
                time.sleep(0.25)
            else:                              # 색/커서 제어가 없는 환경: 변할 때만 출력
                sig = (st.cycle, st.phase, st.prog, len(st.hits))
                if sig != last_sig:
                    last_sig = sig
                    paint(dashboard_lines(st))
                time.sleep(1.0)
        paint(dashboard_lines(st))
    except KeyboardInterrupt:
        st.stop.set()
    finally:
        show_cursor()
        print()
        with st.lock:
            found = len(st.hits)
            cycles = st.cycle
        print(f"   {C.DIM}감시를 마쳤습니다 - {cycles}회 확인 / 발견 {found}건{C.RESET}\n")


# ══════════════════════════════════════════════════════════════════════
#  메인
# ══════════════════════════════════════════════════════════════════════
def confirm_and_run(cat: Catalog, cfg: Watch) -> None:
    clear_screen()
    lines = header("설정 확인")
    for row in cfg.summary_rows():
        lines.append(box_row(row))
    lines.append(box_bottom())
    print("\n".join(lines))
    if not ask_yes("이 설정으로 감시를 시작할까요?", True):
        print(f"   {C.DIM}취소했습니다.{C.RESET}")
        return
    if ask_yes("이 설정을 저장할까요?", True):
        form("설정 이름", ["다음 실행 때 '저장된 설정으로 시작' 에서 고를 수 있습니다"])
        cfg.profile_name = ask_text(
            "이름", f"{cfg.cinema_name}-{cfg.movie or '전체'}-"
                    f"{'좌석' if cfg.mode == 'seat' else '오픈'}")
        save_profile(cfg)
        time.sleep(0.6)
    run_monitor(cat, cfg)


def radar_report(cat: Catalog, cinema: Cinema, movie: str,
                 profile: list[tuple[str, int]], snap: Horizon,
                 crumbs: list[str]) -> list[str]:
    """예매 오픈 범위를 날짜별 막대로 보여준다."""
    radar = OpenRadar(cat)
    full_day, full_off = OpenRadar.full_horizon(profile)
    peak = max((n for _, n in profile), default=0) or 1
    lines = header(f"예매 오픈 범위 - {cinema.name} / {movie or '전체'}", crumbs)
    lines.append(box_row(f"{C.DIM}날짜별 회차 수 (오늘부터). 막대가 끊기는 지점이 "
                         f"아직 안 열린 구간입니다.{C.RESET}"))
    today = date.today()
    for day, count in profile:
        off = (date.fromisoformat(day) - today).days
        wd = WEEKDAY_KR[date.fromisoformat(day).weekday()]
        width = round(22 * count / peak)
        if count == 0:
            mark = f"{C.DIM}{glyph('░', '.') * 22}{C.RESET}  {C.DIM}미오픈{C.RESET}"
        else:
            color = C.BGREEN if count >= peak * OpenRadar.FULL_RATIO else C.YELLOW
            mark = (f"{color}{glyph('█', '#') * max(width, 1)}{C.RESET}"
                    f"{C.DIM}{glyph('░', '.') * (22 - max(width, 1))}{C.RESET}  {count}회차")
        lines.append(box_row(f"{C.DIM}D+{off:<2}{C.RESET} {day[5:]}({wd})  {mark}"))

    lines.append(box_mid())
    lines.append(box_row(f"{C.DIM}{pad('예매 끝', 10)}{C.RESET}{C.BWHITE}"
                         f"{snap.last_open or '-'}{C.RESET}  (D+{snap.days})"))
    if full_day:
        lines.append(box_row(f"{C.DIM}{pad('정규 편성', 10)}{C.RESET}{C.BWHITE}{full_day}"
                             f"{C.RESET}  (D+{full_off})   "
                             f"{C.DIM}회차가 많은 날 기준{C.RESET}"))

    rows = OpenRadar.history(cinema.cinema_id, movie)
    events = OpenRadar.open_events(rows)
    lines.append(box_row(f"{C.DIM}{pad('관측 기록', 10)}{C.RESET}"
                         f"{len(rows)}건 · 오픈 순간 {len(events)}회 포착"))
    for ev in events[-4:]:
        when = datetime.fromisoformat(ev["t"])
        lines.append(box_row(f"  {C.CYAN}{glyph('◆', '*')}{C.RESET} "
                             f"{when:%m-%d}({WEEKDAY_KR[when.weekday()]}) {when:%H:%M}  "
                             f"{ev['from'][5:]} {glyph('→', '->')} {ev['to'][5:]} "
                             f"{C.BGREEN}(+{ev['added']}일){C.RESET}"))
    if not events:
        lines.append(box_row(f"  {C.DIM}아직 오픈 순간을 포착한 기록이 없습니다 - "
                             f"상주 관측을 돌리면 쌓입니다{C.RESET}"))
    lines.append(box_bottom())
    return lines


def wizard_radar(cat: Catalog, fav: Favorites) -> None:
    """예매 오픈 패턴 분석 + (선택) 상주 관측."""
    crumbs = ["예매 오픈 패턴"]
    cinema = wiz_cinema(cat, crumbs, fav)
    crumbs = crumbs + [cinema.name]

    clear_screen()
    print("\n".join(header(f"{cinema.name} 상영표 수집", crumbs)))
    print(box_bottom())
    shows = cat.scan(cinema, 5)
    movie = wiz_movie(shows, crumbs)
    crumbs = crumbs + [movie]

    radar = OpenRadar(cat)
    clear_screen()
    print("\n".join(header("예매 범위 측정 중", crumbs)))
    print(box_bottom())

    def prog(off, upto=None):
        total = upto or 45
        sys.stdout.write(f"\r   {C.CYAN}{glyph('■', '#')}{C.RESET} D+{off} 확인 중...   ")
        sys.stdout.flush()

    snap = radar.scan(cinema, movie, progress=prog)
    profile = radar.profile(cinema, movie, upto=min(max(snap.days + 2, 8), 16),
                            progress=prog)
    sys.stdout.write("\r" + " " * 50 + "\r")
    OpenRadar.log(snap)

    clear_screen()
    print("\n".join(radar_report(cat, cinema, movie, profile, snap, crumbs)))

    if ask_yes("특정 날짜의 오픈 시점을 예측해볼까요?", True):
        while True:
            form("예측할 날짜", ["예) 9/8   또는   2026-09-08"], crumbs)
            try:
                targets = parse_date_input(ask_text("날짜"))
                break
            except ValueError as exc:
                print(f"   {C.YELLOW}{exc}{C.RESET}")
                time.sleep(1.2)
        clear_screen()
        lines = header("오픈 시점 예측", crumbs)
        for target in targets:
            pred = radar.predict(cinema, movie, target, snap)
            wd = WEEKDAY_KR[date.fromisoformat(target).weekday()]
            color = {"열림": C.BGREEN, "예상": C.BYELLOW}.get(pred["status"], C.DIM)
            lines.append(box_row(f"{C.BWHITE}{target}({wd}){C.RESET}  "
                                 f"{color}[{pred['status']}]{C.RESET} "
                                 f"{C.BWHITE}{pred['text']}{C.RESET}"))
            if pred.get("basis"):
                lines.append(box_row(f"{C.DIM}              {pred['basis']}{C.RESET}"))
        lines.append(box_mid())
        lines.append(box_row(f"{C.DIM}관측 기록이 쌓일수록 정확해집니다. "
                             f"상주 관측을 하루 이틀 돌려보세요.{C.RESET}"))
        lines.append(box_bottom())
        print("\n".join(lines))
        read_line(f"   {C.DIM}Enter 를 누르면 계속{C.RESET}")

    if ask_yes("상주 관측을 시작할까요? (오픈 순간을 포착해 기록합니다)", True):
        cfg = Watch(mode="radar", movie=movie)
        apply_cinema(cfg, cinema, cat)
        presets = [(300, "5분   - 오픈 시각까지 정밀하게"), (600, "10분  - 기본"),
                   (1800, "30분  - 며칠 두고 관측"), (3600, "1시간 - 느슨하게")]
        cfg.interval = Chooser("관측 주기", presets, lambda p: p[1], crumbs).run()[0]
        wiz_notify_light(cfg, crumbs)
        run_monitor(cat, cfg)


def wiz_notify_light(cfg: Watch, crumbs: list[str]) -> None:
    cfg.sound = ask_yes("소리 알림을 켤까요?", True)
    if ask_yes("웹훅 / 텔레그램 알림을 추가할까요?", False):
        form("외부 알림", ["필요 없는 항목은 그냥 Enter"], crumbs)
        cfg.webhook = ask_text("Discord/Slack 웹훅 URL")
        token = ask_text("텔레그램 봇 토큰")
        if token:
            cfg.telegram = {"token": token, "chat_id": ask_text("텔레그램 chat_id")}


def manage_favorites(cat: Catalog, fav: Favorites) -> None:
    """지점·상영관 즐겨찾기 편집. 등록해두면 선택 목록 맨 위에 ★ 로 올라온다."""
    crumbs = ["즐겨찾기 관리"]
    while True:
        rows: list = []
        brand = cat.provider.code
        for cid in fav.ids(brand):
            match = [c for c in cat.cinemas() if str(c.cinema_id) == cid]
            if match:
                rows.append(("edit", match[0]))
        menu = [("add", None)] + rows + [("done", None)]

        def label(item):
            kind, cinema = item
            if kind == "add":
                return f"{C.BGREEN}+ 지점 추가 / 상영관 편집{C.RESET}"
            if kind == "done":
                return f"{C.DIM}← 메뉴로 돌아가기{C.RESET}"
            screens = fav.screens_of(cinema)
            desc = ", ".join(screens) if screens else "상영관 미지정"
            return starred(cinema.name, desc, True, right_width=34)

        picked = Chooser(f"즐겨찾기 - {cat.provider.name} ({len(rows)}개 지점)",
                         menu, label, crumbs,
                         hint="Enter 로 편집 · 즐겨찾기는 favorites.json 에 저장됩니다").run()
        if picked[0] == "done":
            return
        cinema = picked[1] if picked[0] == "edit" else wiz_cinema(cat, crumbs, fav)

        clear_screen()
        print("\n".join(header(f"{cinema.name} 상영관 확인", crumbs)))
        print(box_bottom())
        items = collect_screens(cat.scan(cinema, 3))
        if not items:
            print(f"   {C.YELLOW}상영 정보가 없어 상영관 목록을 가져오지 못했습니다.{C.RESET}")
            fav.set_cinema(cinema, True)
            fav.save()
            time.sleep(1.2)
            continue

        items = fav.sort_screens(cinema, items)
        preselect = {i for i, s in enumerate(items)
                     if fav.has_screen(cinema, s[0])}
        chosen = Chooser(f"{cinema.name} 즐겨찾는 상영관", items,
                         lambda s: starred(s[0], f"{s[1]}석",
                                           fav.has_screen(cinema, s[0]),
                                           right_width=10),
                         crumbs + [cinema.name], multi=True, preselect=preselect,
                         empty_means_all=False,
                         hint="Space 로 토글 · 아무것도 안 고르면 상영관 즐겨찾기 해제").run()
        fav.set_screens(cinema, [c[0] for c in chosen])
        keep = bool(chosen) or ask_yes(f"{cinema.name} 지점을 즐겨찾기에 남길까요?", True)
        fav.set_cinema(cinema, keep)
        fav.save()
        print(f"   {C.GREEN}저장했습니다{C.RESET} "
              f"{C.DIM}{cinema.name}: {', '.join(c[0] for c in chosen) or '지점만'}{C.RESET}")
        time.sleep(0.8)


def pick_brand() -> Provider:
    """첫 화면 - 영화관 브랜드 선택. 고른 브랜드의 배너로 바뀐다."""
    print_banner(["multi-brand : 롯데시네마 · 메가박스 · CGV",
                  "no auth     : 공개 조회 API 사용",
                  "select a cinema brand to begin"],
                 brand="app", subtitle="한국 영화관 좌석 · 예매 감시 콘솔",
                 hold=True)
    items = [PROVIDERS[c] for c in ("lotte", "megabox", "cgv")]

    def label(cls):
        badge = (f"{C.BGREEN}좌석까지{C.RESET}" if cls.supports_seats
                 else (f"{C.BYELLOW}잔여석까지{C.RESET}" if cls.supports_shows
                       else f"{C.DIM}지점만{C.RESET}"))
        return f"{cell(cls.name, 14)}{cell(cls.site, 20)}{badge}  {C.DIM}{cls.note}{C.RESET}"

    chosen = Chooser("영화관 선택", items, label,
                     hint="브랜드마다 공개된 데이터의 깊이가 다릅니다").run()
    provider = chosen()
    ACTIVE.update(code=provider.code, color=provider.color(), name=provider.title)
    return provider


def main() -> None:
    setup_terminal()
    load_settings()
    while True:
        try:
            provider = pick_brand()
            run_brand(provider)
            return
        except (BackToMenu, SwitchBrand):
            continue                    # m 또는 '영화관 변경' -> 브랜드 선택으로


def run_brand(provider: Provider) -> None:
    cat = Catalog(provider)
    print_banner([f"brand     : {provider.name} ({provider.site})",
                  f"support   : {provider.note}",
                  "loading theater list ..."])
    try:
        count = len(cat.cinemas())
    except (LotteApiError, Unsupported) as exc:
        print(f"   {C.BRED}지점 목록을 불러오지 못했습니다: {exc}{C.RESET}")
        time.sleep(2)
        raise BackToMenu

    fav = Favorites()
    profiles = load_profiles(provider.code)
    modes = "seat-watch, open-watch" if provider.supports_shows else "(상영표 미지원)"
    fav_c, fav_s = fav.count(provider.code)
    print_banner([f"brand     : {provider.name}",
                  f"theaters  : {count}개 지점 로드 완료",
                  f"modules   : {modes}",
                  f"favorites : 지점 {fav_c} · 상영관 {fav_s}",
                  f"profiles  : 저장된 설정 {len(profiles)}개"],
                 hold=True)

    while True:
        try:
            if not menu_once(cat, fav):
                return
        except BackToMenu:
            continue


SOUND_CHOICES = [("Alarm01.wav", "알람 (기본)"), ("Ring01.wav", "벨소리"),
                 ("Windows Notify.wav", "알림음"), ("chimes.wav", "차임")]


def sound_path(name: str = "") -> str:
    """Windows Media 폴더의 알림음 경로. 없으면 빈 문자열."""
    if os.name != "nt":
        return ""
    media = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Media")
    for cand in ([name] if name else []) + [n for n, _ in SOUND_CHOICES]:
        path = os.path.join(media, cand)
        if os.path.exists(path):
            return path
    return ""


def play_alert_sound() -> None:
    """알림음.

    winsound.Beep 은 '메인보드 비프 스피커' 로 나가는데 요즘 PC엔 그 스피커가
    없어 아무 소리도 안 나는 경우가 흔하다(실측 확인). 그래서 사운드카드로
    확실히 나가는 WAV 재생을 쓴다. WAV 를 못 찾으면 MessageBeep 으로 폴백.
    """
    try:
        if os.name == "nt":
            import winsound
            path = sound_path(str(SETTINGS.get("sound_file", "")))
            if path:
                for _ in range(2):
                    winsound.PlaySound(path, winsound.SND_FILENAME)
                return
            for _ in range(3):
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                time.sleep(0.18)
        else:
            for _ in range(3):
                sys.stdout.write("\a")
                sys.stdout.flush()
                time.sleep(0.2)
    except Exception:
        pass


def settings_menu() -> None:
    """전역 환경설정 (settings.json). 배너·알림 소리. 향후 인증 등록 등으로 확장."""
    def onoff(flag: bool) -> str:
        return f"{C.BGREEN}켜짐{C.RESET}" if flag else f"{C.DIM}꺼짐{C.RESET}"

    while True:
        try:
            hold = float(SETTINGS.get("banner_hold", 3.0) or 0)
        except (TypeError, ValueError):
            hold = 0.0
        cur_file = str(SETTINGS.get("sound_file", ""))
        cur_tone = next((desc for name, desc in SOUND_CHOICES if name == cur_file),
                        "알람 (기본)")
        items = [
            ("banner", "시작 배너 로고", onoff(SETTINGS.get("banner", True))),
            ("hold", "배너 유지 시간", f"{hold:g}초"),
            ("sound", "알림 소리", onoff(SETTINGS.get("sound", True))),
            ("tone", "알림음 종류", cur_tone),
            ("test", "알림 소리 테스트", "지금 한 번 울려보기"),
            ("auth", "인증 등록", "준비 중"),
            ("back", "뒤로", ""),
        ]
        picked = Chooser("설정", items,
                         lambda it: two_col(it[1], it[2], right_width=26),
                         crumbs=["설정"],
                         hint="Enter 로 켜고 끄거나 값을 바꿉니다").run()
        key = picked[0]
        if key == "back":
            return
        if key == "banner":
            SETTINGS["banner"] = not SETTINGS.get("banner", True)
        elif key == "sound":
            SETTINGS["sound"] = not SETTINGS.get("sound", True)
        elif key == "hold":
            SETTINGS["banner_hold"] = ask_int("배너 유지 시간(초)",
                                              int(round(hold)), 0, 10)
        elif key == "tone":
            avail = [(n, d) for n, d in SOUND_CHOICES if sound_path(n)]
            if not avail:
                print(f"   {C.YELLOW}쓸 수 있는 알림음 파일을 찾지 못했습니다.{C.RESET}")
                read_line(f"   {C.DIM}Enter 로 계속{C.RESET}")
                continue
            name, _ = Chooser("알림음 종류", avail,
                              lambda it: two_col(it[1], it[0], right_width=22),
                              crumbs=["설정", "알림음"],
                              hint="고르면 바로 들려줍니다").run()
            SETTINGS["sound_file"] = name
            if SETTINGS.get("sound", True):
                play_alert_sound()
        elif key == "test":
            if SETTINGS.get("sound", True):
                play_alert_sound()
            else:
                print(f"   {C.YELLOW}알림 소리가 꺼져 있습니다. 먼저 켜주세요.{C.RESET}")
                read_line(f"   {C.DIM}Enter 로 계속{C.RESET}")
            continue
        elif key == "auth":
            print(f"   {C.DIM}인증 등록(로그인 연동)은 다음 버전에서 지원 예정입니다."
                  f"{C.RESET}")
            read_line(f"   {C.DIM}Enter 로 계속{C.RESET}")
            continue
        save_settings()


def show_brand_support(cat: Catalog) -> None:
    """브랜드별로 무엇까지 되는지 정리해서 보여준다."""
    clear_screen()
    lines = header("브랜드별 지원 현황")
    lines.append(box_row(f"{C.DIM}{cell('브랜드', 14)}{cell('지점', 8)}"
                         f"{cell('상영표·잔여석', 16)}좌석표{C.RESET}"))
    for code in ("lotte", "megabox", "cgv"):
        cls = PROVIDERS[code]
        yes, no = f"{C.BGREEN}O{C.RESET}", f"{C.DIM}X{C.RESET}"
        mark = "  " + cell(cls.name, 14) if code != cat.provider.code else             f"{C.BGREEN}{glyph('▶', '>')}{C.RESET} " + cell(cls.name, 14)
        lines.append(box_row(f"{mark}{cell(yes, 8 + 9)}"
                             f"{cell(yes if cls.supports_shows else no, 16 + 9)}"
                             f"{yes if cls.supports_seats else no}"))
    lines.append(box_mid())
    lines.append(box_row(f"{C.DIM}CGV 는 신규 SPA 의 상영표 API 로 잔여석까지 봅니다."
                         f"{C.RESET}"))
    lines.append(box_row(f"{C.DIM}좌석표는 예매 인증 뒤에 있어 아직 열지 못했습니다."
                         f"{C.RESET}"))
    lines.append(box_row(f"{C.DIM}메가박스는 좌석선택 화면의 API 로 좌석 단위까지"
                         f" 지원합니다.{C.RESET}"))
    lines.append(box_bottom())
    print("\n".join(lines))
    read_line(f"   {C.DIM}Enter 를 누르면 계속{C.RESET}")


def menu_once(cat: Catalog, fav: Favorites) -> bool:
    """메뉴 한 번을 처리한다. False 를 돌려주면 프로그램을 끝낸다."""
    brand = cat.provider
    profiles = load_profiles(brand.code)
    menu: list[tuple[str, str, str]] = []
    if brand.supports_shows:
        seat_desc = ("원하는 자리가 비면 알림 (취소표)" if brand.supports_seats
                     else "잔여석이 생기면 알림 (좌석 지정 불가)")
        menu.append(("seat", "좌석 감시", seat_desc))
        menu.append(("open", "예매오픈 감시", "특정 날짜 예매가 열리면 알림"))
        look = ("회차를 골라 좌석 배치를 바로 확인" if brand.supports_seats
                else "회차별 잔여석을 바로 확인")
        menu.append(("look", "좌석 확인", look))
        if profiles:
            menu.append(("saved", "저장된 설정으로 시작", f"{len(profiles)}개 저장됨"))
        menu.append(("radar", "예매 오픈 패턴", "언제 열리는지 측정·기록·예측"))
    else:
        menu.append(("info", "지원 현황 보기", f"{brand.name} 는 지점 목록만 확인됩니다"))
    fav_c, fav_s = fav.count(brand.code)
    menu.append(("fav", "즐겨찾기 관리", f"지점 {fav_c} · 상영관 {fav_s}"))
    menu.append(("brand", "영화관 변경", "다른 브랜드로 전환"))
    menu.append(("settings", "설정", "배너 · 알림 소리"))
    menu.append(("quit", "종료", ""))

    picked = Chooser("무엇을 할까요", menu,
                     lambda m: two_col(m[1], m[2], right_width=30),
                     hint="어느 화면에서든 m 을 누르면 이 메뉴로 돌아옵니다").run()[0]
    if picked == "quit":
        return False
    if picked == "brand":
        raise SwitchBrand
    if picked == "settings":
        settings_menu()
        return True
    if picked == "info":
        show_brand_support(cat)
        return True
    if picked == "look":
        browse_seats(cat, fav)
        return True
    if picked == "fav":
        manage_favorites(cat, fav)
    elif picked == "radar":
        wizard_radar(cat, fav)
    elif picked == "saved":
        prof = Chooser("저장된 설정", profiles,
                       lambda p: two_col(p.get("profile_name", "(이름없음)"),
                                         f"{p.get('cinema_name')} · "
                                         f"{p.get('movie') or '전체'}",
                                         right_width=32)).run()
        run_monitor(cat, cfg_from_dict(prof))
    else:
        cfg = wizard_seat(cat, fav) if picked == "seat" else wizard_open(cat, fav)
        confirm_and_run(cat, cfg)
    return True


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        show_cursor()
        print("\n   종료합니다.")
    except SystemExit:
        show_cursor()
        print()

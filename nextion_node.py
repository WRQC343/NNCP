#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
nextion_node.py - Nextion touchscreen control panel for HamVOIP / AllStar nodes.

Pages: main (quick connects, links, who's talking, weather), node keypad,
DTMF pad, system (temp, uptime, IP, restart/reboot/shutdown), weather.

The Pi does all the drawing: the Nextion only needs five background pictures
and three fonts loaded (see INSTALL_GUIDE.html). Touches come back as raw X/Y
coordinates (sendxy=1), so there are no buttons to wire up in Nextion Editor.

Works with Python 2.7 or 3.x, standard library only.

Config:  /etc/nextion-node.json
Usage:   nextion_node.py                  normal run
         nextion_node.py --test           no Asterisk/system commands, fake data
         nextion_node.py --port DEV       override serial port
         nextion_node.py --weather-test   print the weather it would show, then exit
         nextion_node.py --version        print the version
"""
from __future__ import print_function

import io
import json
import os
import re
import select
import socket
import subprocess
import sys
import termios
import threading
import time
import tty

PY3 = sys.version_info[0] >= 3
if PY3:
    from urllib.request import urlopen
    from urllib.parse import quote as urlquote
else:  # pragma: no cover
    from urllib2 import urlopen
    from urllib import quote as urlquote

VERSION = "2.9"
CONFIG_PATH = "/etc/nextion-node.json"

DEFAULT_CONFIG = {
    "node": "1999",
    "title": "",
    "serial_port": "/dev/ttyAMA0",
    "baud": 9600,
    "asterisk": "/usr/sbin/asterisk",
    "poll_seconds": 3,
    "keyed_poll_seconds": 1,
    "sleep_seconds": 600,
    "wake_on_key": True,
    "brightness": 100,
    "clock_24h": False,
    "boot_seconds": 4,                 # how long the boot screen shows when the panel starts
    "units": "F",
    "weather_zip": "",
    "weather_source": "auto",          # auto | script | openmeteo | off
    "weather_script": "/usr/local/sbin/weather.sh",
    "weather_minutes": 15,
    "forecast": True,
    "scroll_links": True,
    "scroll_step_seconds": 0.4,
    "status_files": {                  # trigger files that show ReConn / GPS on or off
        "reconn": {"path": "/etc/asterisk/local/ReConn/~ReConn_disabled",
                   "present_means": "disabled"},
        "gps": {"path": "/tmp/GPS.ENABLED", "present_means": "enabled"},
    },
    "status_poll_seconds": 2,
    "extras_dtmf": {                   # DTMF codes for the Extras page (blank = not set)
        "reconn_on": "*917", "reconn_off": "*918",
        "gps_on": "*A50", "gps_off": "*A51", "gps_update": "*A52",
    },
    "restart_asterisk_cmd": "",        # blank = astres.sh if present, else systemctl
    "reboot_cmd": "systemctl reboot",
    "shutdown_cmd": "systemctl poweroff",
    "astdb_paths": ["/var/log/asterisk/astdb.txt", "/var/lib/asterisk/astdb.txt",
                    "/tmp/astdb.txt"],
    "favorites": [
        {"label": "HUB", "node": "2001"},
        {"label": "NET 1", "node": "2002"},
        {"label": "NET 2", "node": "2003"},
        {"label": "LOCAL", "node": "2004"},
        {"label": "TEST", "node": "2005"},
        {"label": "WX", "node": "2006"},
    ],
}

# --------------------------------------------------------------------------
# Screen layout (800 x 480). make_images.py imports these so the pictures
# and the touch areas always line up.
# --------------------------------------------------------------------------
SCREEN_W, SCREEN_H = 800, 480       # 5" Nextion: NX8048T050 / NX8048K050 / NX8048P050
PIC_MAIN, PIC_KEYPAD, PIC_DTMF, PIC_SYSTEM, PIC_WEATHER, PIC_EXTRAS = 0, 1, 2, 3, 4, 5
PIC_BOOT = 6          # shown by the screen itself at power-on (page0 pic=6 in Nextion Editor)
FONT_SMALL, FONT_BIG, FONT_HUGE = 0, 1, 2      # 24px, 32px, 64px in Nextion Editor
SMALL_PX, BIG_PX, HUGE_PX = 24, 32, 64
HEADER_H = 58
# Boot screen text areas (the status box is baked into the picture with a "waiting" message)
BOOT_STATUS_PANEL = (150, 330, 500, 64)
BOOT_STATUS = (158, 338, 484, 48)
BOOT_NODE = (0, 404, 800, 32)
BOOT_CREDIT = (0, 438, 800, 30)      # "gmrs-link.com" - part of the boot picture itself
BOOT_VERSION = (640, 446, 148, 26)
DEG = u"\u00b0"

# name: (x, y, w, h)
HEADER_TEXT = {
    "title": (20, 8, 180, 42),
    "weather": (390, 8, 232, 42),
    "weather_wide": (208, 8, 414, 42),   # used when no status badges are set up
    "clock": (628, 8, 156, 42),
}
# Status badges in the main header: (key, text, box)
BADGES = [("reconn", "ReConn", (208, 13, 100, 32)), ("gps", "GPS", (316, 13, 64, 32))]
STATUS_NAMES = {"reconn": "ReConn", "gps": "GPS Tracking"}

MAIN_BUTTONS = [
    ("fav0", 12, 172, 254, 92), ("fav1", 273, 172, 254, 92), ("fav2", 534, 172, 254, 92),
    ("fav3", 12, 272, 254, 92), ("fav4", 273, 272, 254, 92), ("fav5", 534, 272, 254, 92),
    ("mode", 12, 372, 122, 96), ("keypad", 142, 372, 122, 96), ("dtmf", 272, 372, 122, 96),
    ("extras", 402, 372, 122, 96), ("system", 532, 372, 122, 96),
    ("discall", 662, 372, 126, 96),
    ("badges", 208, 0, 176, HEADER_H),  # invisible: tap the badges -> Extras page
    ("wx", 390, 0, 232, HEADER_H),     # invisible: tap the header weather
]
STATUS_BOX = (12, 68, 776, 92)
STATUS_DIVIDER_X = 612
MAIN_TEXT = {
    "links": (28, 108, 572, 40),
    "count": (214, 74, 220, 28),
}
RX_LIGHT = (626, 80, 74, 68)
TX_LIGHT = (708, 80, 74, 68)


def light_inner(box):
    x, y, w, h = box
    return (x + 6, y + 6, w - 12, h - 12)


# rough width of the 24px font, used to decide when the links line must scroll
_CHAR_W = {" ": 7, "(": 8, ")": 8, ".": 7, "|": 7, "+": 14, u"\u00b0": 9}


def text_px(text):
    return sum(_CHAR_W.get(c, 13 if c.isdigit() else 16 if c.isupper() else 12)
               for c in text)

ENTRY_BOX = (12, 10, 776, 72)
ENTRY_TEXT = (32, 22, 740, 48)

KEYPAD_BUTTONS = [
    ("k1", 12, 94, 152, 88), ("k2", 172, 94, 152, 88), ("k3", 332, 94, 152, 88),
    ("k4", 12, 190, 152, 88), ("k5", 172, 190, 152, 88), ("k6", 332, 190, 152, 88),
    ("k7", 12, 286, 152, 88), ("k8", 172, 286, 152, 88), ("k9", 332, 286, 152, 88),
    ("clr", 12, 382, 152, 88), ("k0", 172, 382, 152, 88), ("del", 332, 382, 152, 88),
    ("connect", 496, 94, 292, 88), ("monitor", 496, 190, 292, 88),
    ("disc", 496, 286, 292, 88), ("back", 496, 382, 292, 88),
]

_DTMF_GRID = [["1", "2", "3", "A"], ["4", "5", "6", "B"],
              ["7", "8", "9", "C"], ["*", "0", "#", "D"]]
DTMF_KEYS = {}
DTMF_BUTTONS = []
for _r, _row in enumerate(_DTMF_GRID):
    for _c, _ch in enumerate(_row):
        _name = "d" + {"*": "star", "#": "hash"}.get(_ch, _ch)
        DTMF_KEYS[_name] = _ch
        DTMF_BUTTONS.append((_name, 12 + _c * 126, 94 + _r * 96, 118, 88))
DTMF_BUTTONS += [
    ("send", 520, 94, 268, 88), ("del", 520, 190, 268, 88),
    ("clr", 520, 286, 268, 88), ("back", 520, 382, 268, 88),
]

SYSTEM_PANEL = (12, 70, 500, 398)
SYSTEM_ROWS = ["CPU TEMPERATURE", "UPTIME", "IP ADDRESS", "CPU LOAD", "ASTERISK"]
SYSTEM_ROW_H = 79


def system_value_box(i):
    x, y, w, h = SYSTEM_PANEL
    return (x + 20, y + i * SYSTEM_ROW_H + 34, w - 40, 40)


SYSTEM_BUTTONS = [
    ("restart", 524, 70, 264, 92), ("reboot", 524, 170, 264, 92),
    ("shutdown", 524, 270, 264, 92), ("back", 524, 370, 264, 92),
]

WX_NOW_PANEL = (12, 70, 380, 330)
WX_FC_PANEL = (404, 70, 384, 330)
WX_TEXT = {
    "place": (300, 8, 484, 42),
    "temp": (28, 90, 348, 84),
    "cond": (28, 180, 348, 44),
    "feels": (28, 236, 348, 34),
    "humid": (28, 276, 348, 34),
    "wind": (28, 316, 348, 34),
    "src": (28, 356, 348, 34),
    "updated": (16, 416, 380, 48),
}


def wx_day_boxes(i):
    y = 112 + i * 96
    return (420, y, 170, 40), (590, y, 186, 40), (420, y + 44, 356, 34)


WEATHER_BUTTONS = [("refresh", 412, 410, 184, 60), ("back", 604, 410, 184, 60)]

# Extras page: two groups of DTMF buttons for the node's own programs
EXTRAS_GROUPS = [
    ("RECONN", (12, 70, 776, 148), [("reconn_on", "ENABLE"), ("reconn_off", "DISABLE")]),
    ("GPS TRACKING", (12, 228, 776, 148),
     [("gps_on", "ENABLE"), ("gps_off", "DISABLE"), ("gps_update", "UPDATE")]),
]
EXTRAS_NAMES = {"reconn_on": "ReConn Enable", "reconn_off": "ReConn Disable",
                "gps_on": "GPS Tracking Enable", "gps_off": "GPS Tracking Disable",
                "gps_update": "GPS Tracking Update"}
EXTRAS_BUTTONS = []
for _title, (_gx, _gy, _gw, _gh), _items in EXTRAS_GROUPS:
    for _i, (_name, _label) in enumerate(_items):
        EXTRAS_BUTTONS.append((_name, _gx + 16 + _i * 252, _gy + 48, 240, 86))
EXTRAS_BUTTONS.append(("back", 584, 388, 204, 80))
EXTRAS_STATUS = (12, 388, 560, 80)
EXTRAS_STATE_KEYS = ["reconn", "gps"]     # same order as EXTRAS_GROUPS


def extras_state_box(i):
    _, (gx, gy, gw, gh), _ = EXTRAS_GROUPS[i]
    return (gx + 220, gy + 6, 300, 34)
EXTRAS_STATUS_TEXT = (28, 408, 532, 40)


def find_button(name, buttons):
    for b in buttons:
        if b[0] == name:
            return b
    return None


def extras_code_box(btn):
    _, x, y, w, h = btn
    return (x + 4, y + h - 32, w - 8, 26)


# Text boxes inside buttons, scaled to the button size (used by make_images.py too)
def fav_boxes(x, y, w, h):
    label = (x + 6, y + int(h * 0.12), w - 12, int(h * 0.44))
    node = (x + 6, y + int(h * 0.56), w - 12, int(h * 0.30))
    bar = (x + 16, y + h - 10, w - 32, 5)
    return label, node, bar


def two_line_boxes(x, y, w, h):
    small = (x, y + int(h * 0.12), w, int(h * 0.30))
    big = (x, y + int(h * 0.44), w, int(h * 0.44))
    return small, big


mode_boxes = two_line_boxes


def center_box(x, y, w, h, text_h):
    return (x, y + (h - text_h) // 2, w, text_h)


MAX_NODE_DIGITS = 7
MAX_DTMF = 24


def rgb565(r, g, b):
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


WHITE = rgb565(255, 255, 255)
GRAY = rgb565(140, 150, 165)
DIM = rgb565(70, 80, 95)
GREEN = rgb565(80, 220, 120)
AMBER = rgb565(255, 190, 60)
RED = rgb565(255, 90, 90)
CYAN = rgb565(110, 200, 255)
BOOT_PANEL_BG = rgb565(16, 28, 48)    # matches the status box in page6_boot.png
BADGE_ON = rgb565(30, 150, 80)
BADGE_OFF = rgb565(185, 45, 45)

TERM = b"\xff\xff\xff"
BAUDS = {
    9600: termios.B9600, 19200: termios.B19200, 38400: termios.B38400,
    57600: termios.B57600, 115200: termios.B115200,
}

WMO = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy",
    45: "Fog", 48: "Freezing fog", 51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Frz drizzle", 57: "Frz drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Freezing rain", 71: "Light snow", 73: "Snow",
    75: "Heavy snow", 77: "Snow grains", 80: "Showers", 81: "Showers",
    82: "Heavy showers", 85: "Snow showers", 86: "Snow showers",
    95: "T-storms", 96: "T-storms, hail", 99: "T-storms, hail",
}
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida",
    "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana",
    "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def log(*args):
    msg = " ".join([time.strftime("%Y-%m-%d %H:%M:%S")] + [u"%s" % a for a in args])
    try:
        print(msg)
    except UnicodeError:          # node's locale is ASCII-only (common on HamVOIP)
        print(msg.encode("ascii", "replace").decode("ascii"))
    sys.stdout.flush()


def clean_node(value):
    """Node numbers are digits only - also keeps anything odd out of the CLI."""
    value = re.sub(r"[^0-9]", "", str(value))
    return value[:MAX_NODE_DIGITS]


def clean_dtmf(value):
    return re.sub(r"[^0-9A-D*#]", "", str(value).upper())[:MAX_DTMF]


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        with io.open(path, encoding="utf-8", errors="replace") as f:
            cfg.update(json.load(f))
    else:
        try:
            with open(path, "w") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
            log("Wrote default config to", path, "- edit it and restart.")
        except (IOError, OSError) as e:
            log("Could not write default config:", e)
    cfg["node"] = clean_node(cfg["node"])
    favs = []
    for fav in cfg.get("favorites", [])[:6]:
        favs.append({"label": str(fav.get("label", ""))[:10],
                     "node": clean_node(fav.get("node", ""))})
    while len(favs) < 6:
        favs.append({"label": "", "node": ""})
    cfg["favorites"] = favs
    cfg["units"] = "C" if str(cfg.get("units", "F")).upper().startswith("C") else "F"
    return cfg


def run(args, timeout=10):
    """Run a command, return its output text or None on failure."""
    try:
        if PY3:
            out = subprocess.check_output(args, stderr=subprocess.STDOUT, timeout=timeout)
        else:
            out = subprocess.check_output(args, stderr=subprocess.STDOUT)
        return out.decode("utf-8", "replace")
    except Exception as e:
        log("Command failed:", " ".join(args), "-", str(e).splitlines()[0][:120])
        return None


# --------------------------------------------------------------------------
# Asterisk output parsing
# --------------------------------------------------------------------------
def parse_rpt_nodes(text):
    """Parse `rpt nodes <node>` output -> {node: 'T'|'R'|'C'}.

    T = transceive, R = monitor (receive only), C = still connecting.
    """
    if text is None:
        return None
    links = {}
    seen_header = False
    for line in text.splitlines():
        if "CONNECTED NODES" in line:
            seen_header = True
            continue
        if not seen_header or "<NONE>" in line:
            continue
        for item in line.split(","):
            m = re.match(r"^([A-Za-z]?)(\d+)$", item.strip())
            if m:
                links[m.group(2)] = (m.group(1) or "T").upper()
    return links


def parse_xnode(text):
    """Parse `rpt xnode <node>` -> {'rx':bool,'tx':bool,'keyed':[nodes]} or None.

    RPT_RXKEYED=1  -> local receiver has a signal (someone on the local radio)
    RPT_TXKEYED=1  -> local transmitter is keyed
    RPT_ALINKS=2,2001TU,2002TK  -> per link: T/R mode, K = that node is keyed
    """
    if not text or "RPT_" not in text:
        return None
    info = {"rx": False, "tx": False, "keyed": []}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("RPT_RXKEYED="):
            info["rx"] = line.split("=", 1)[1].strip() == "1"
        elif line.startswith("RPT_TXKEYED="):
            info["tx"] = line.split("=", 1)[1].strip() == "1"
        elif line.startswith("RPT_ALINKS="):
            for item in line.split("=", 1)[1].split(",")[1:]:
                m = re.match(r"^(\d+)([A-Z]*)$", item.strip())
                if m and m.group(2).endswith("K"):
                    info["keyed"].append(m.group(1))
    return info


# --------------------------------------------------------------------------
# Weather
# --------------------------------------------------------------------------
def http_get_json(url):
    try:
        resp = urlopen(url, timeout=12)
        return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log("Web request failed (%s), trying curl" % str(e)[:80])
    out = run(["curl", "-s", "--max-time", "12", url], timeout=15)
    if out:
        try:
            return json.loads(out)
        except ValueError:
            pass
    return None


def geocode(query):
    """'57201' or 'City, ST' -> (lat, lon, place name) or None."""
    query = query.strip()
    state = None
    name = query
    if "," in query:
        name, state = [s.strip() for s in query.split(",", 1)]
        state = US_STATES.get(state.upper(), state)
    url = ("https://geocoding-api.open-meteo.com/v1/search?name=%s&count=20&language=en"
           "&format=json" % urlquote(name))
    data = http_get_json(url)
    results = (data or {}).get("results") or []
    if not results:
        return None
    best = None
    for r in results:
        if r.get("country_code") != "US" and re.match(r"^\d{5}$", name):
            continue
        if state and state.lower() not in (r.get("admin1", "") + r.get("country", "")).lower():
            continue
        best = r
        break
    best = best or results[0]
    place = best.get("name", query)
    if best.get("admin1"):
        abbrev = [k for k, v in US_STATES.items() if v == best["admin1"]]
        place += ", " + (abbrev[0] if abbrev else best["admin1"])
    return best["latitude"], best["longitude"], place


def parse_weather_script(text):
    """weather.sh <zip> v  ->  e.g. '52(deg)F, Cloudy'  ->  (temp, condition) or None."""
    if not text:
        return None
    text = text.strip().splitlines()[-1] if text.strip() else ""
    if not text or "no report" in text.lower() or "usage" in text.lower():
        return None
    m = re.search(r"(-?\d+(?:\.\d+)?)", text)
    if not m:
        return None
    temp = str(int(round(float(m.group(1)))))
    parts = re.split(r"\s*[,/]\s*", text, maxsplit=1)      # "55F / Fair" or "52F, Cloudy"
    cond = parts[1].strip() if len(parts) > 1 else ""
    cond = re.sub(r"\s+", " ", cond).strip(" .")
    return temp, cond.title() if cond.islower() else cond


def day_name(iso_date, index):
    if index == 0:
        return "Today"
    if index == 1:
        return "Tomorrow"
    try:
        return time.strftime("%A", time.strptime(iso_date, "%Y-%m-%d"))
    except ValueError:
        return iso_date


def fetch_openmeteo(lat, lon, units):
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
           "&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,"
           "wind_speed_10m,wind_direction_10m"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
           "precipitation_probability_max"
           "&temperature_unit=%s&wind_speed_unit=%s&timezone=auto&forecast_days=3"
           % (lat, lon, "fahrenheit" if units == "F" else "celsius",
              "mph" if units == "F" else "kmh"))
    return http_get_json(url)


def build_weather(cfg, test_mode=False, cache=None):
    """Collect weather from weather.sh and/or Open-Meteo. Returns a dict."""
    source = cfg.get("weather_source", "auto")
    zipcode = str(cfg.get("weather_zip", "")).strip()
    units = cfg["units"]
    wx = {"ok": False, "temp": None, "cond": "", "feels": None, "humid": None,
          "wind": "", "days": [], "place": zipcode, "source": "", "time": time.time()}
    if source == "off" or not zipcode:
        return wx
    cache = cache if cache is not None else {}

    if source in ("auto", "script"):
        script = cfg.get("weather_script", "")
        if test_mode and not os.path.exists(script):
            out = "52%sF, Partly Cloudy" % DEG
        elif os.path.exists(script):
            out = run([script, zipcode, "v"], timeout=25)
        else:
            out = None
        parsed = parse_weather_script(out)
        if parsed:
            wx["temp"], wx["cond"] = parsed
            wx["ok"] = True
            wx["source"] = "weather.sh"

    need_om = source == "openmeteo" or (source == "auto" and not wx["ok"]) or cfg.get("forecast")
    if need_om and not test_mode:
        if not cache.get("geo"):              # retried every cycle until it works
            cache["geo"] = geocode(zipcode)
            if cache["geo"]:
                log("Weather location:", cache["geo"][2])
        geo = cache.get("geo")
        if geo:
            wx["place"] = geo[2]
            data = fetch_openmeteo(geo[0], geo[1], units)
            if data and "current" in data:
                cur = data["current"]
                if not wx["ok"]:
                    wx["temp"] = str(int(round(cur.get("temperature_2m", 0))))
                    wx["cond"] = WMO.get(cur.get("weather_code"), "")
                    wx["ok"] = True
                    wx["source"] = "Open-Meteo"
                else:
                    wx["source"] += " + Open-Meteo"
                if cur.get("apparent_temperature") is not None:
                    wx["feels"] = str(int(round(cur["apparent_temperature"])))
                if cur.get("relative_humidity_2m") is not None:
                    wx["humid"] = str(int(round(cur["relative_humidity_2m"])))
                if cur.get("wind_speed_10m") is not None:
                    d = cur.get("wind_direction_10m") or 0
                    wx["wind"] = "%d %s %s" % (round(cur["wind_speed_10m"]),
                                               "mph" if units == "F" else "km/h",
                                               COMPASS[int((d + 22.5) // 45) % 8])
            if data and "daily" in data and cfg.get("forecast"):
                dly = data["daily"]
                for i, iso in enumerate(dly.get("time", [])[:3]):
                    try:
                        pop = (dly.get("precipitation_probability_max") or [None] * 3)[i]
                        wx["days"].append({
                            "name": day_name(iso, i),
                            "hi": str(int(round(dly["temperature_2m_max"][i]))),
                            "lo": str(int(round(dly["temperature_2m_min"][i]))),
                            "cond": WMO.get(dly["weather_code"][i], ""),
                            "pop": pop,
                        })
                    except (KeyError, IndexError, TypeError):
                        pass
    elif need_om and test_mode:
        wx["place"] = "Test City"
        if not wx["ok"]:
            wx.update({"temp": "52", "cond": "Partly cloudy", "ok": True,
                       "source": "test"})
        wx.update({"feels": "48", "humid": "61", "wind": "12 mph NW"})
        wx["days"] = [{"name": "Today", "hi": "55", "lo": "38", "cond": "Partly cloudy",
                       "pop": 10},
                      {"name": "Tomorrow", "hi": "61", "lo": "42", "cond": "Showers", "pop": 60},
                      {"name": "Friday", "hi": "49", "lo": "33", "cond": "Clear", "pop": 0}]
    return wx


# --------------------------------------------------------------------------
# System info
# --------------------------------------------------------------------------
VCGENCMD = ["/opt/vc/bin/vcgencmd", "/usr/bin/vcgencmd"]


def cpu_temp_c():
    """Kernel thermal file first; HamVOIP kernels may refuse it, so fall back to vcgencmd."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000.0
    except (IOError, OSError, ValueError):
        pass
    for path in VCGENCMD:
        if os.path.exists(path):
            out = run([path, "measure_temp"], timeout=3)     # temp=43.5'C
            m = re.search(r"(-?\d+(?:\.\d+)?)", out or "")
            if m:
                return float(m.group(1))
    return None


def uptime_text():
    try:
        with open("/proc/uptime") as f:
            secs = int(float(f.read().split()[0]))
    except (IOError, OSError, ValueError):
        return "--"
    d, secs = divmod(secs, 86400)
    h, secs = divmod(secs, 3600)
    m = secs // 60
    return ("%dd %dh %dm" % (d, h, m)) if d else ("%dh %dm" % (h, m))


def ip_address():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))       # no packets are sent for UDP connect
        return s.getsockname()[0]
    except (OSError, socket.error):
        out = run(["hostname", "-i"], timeout=3)
        return out.strip().split()[0] if out and out.strip() else "No network"
    finally:
        s.close()


def load_text():
    try:
        with open("/proc/loadavg") as f:
            return "  ".join(f.read().split()[:3])
    except (IOError, OSError):
        return "--"


class Panel(object):
    def __init__(self, cfg, test_mode=False):
        self.cfg = cfg
        self.node = cfg["node"]
        self.test_mode = test_mode
        self.fd = None
        self.rx = bytearray()
        self.page = "main"
        self.asleep = False
        self.last_touch = time.time()
        self.monitor_mode = False
        self.entry = ""
        self.dtmf = ""
        self.dtmf_note = None
        self.links = {}
        self.fake_links = {}
        self.air = None                   # parse_xnode() result
        self.fake_air = {"rx": False, "tx": False, "keyed": []}
        self.asterisk_ok = True
        self.next_poll = 0
        self.next_keyed = 0
        self.next_sys = 0
        self.confirm = {}                 # button name -> deadline for "tap again"
        self.message = None
        self.message_until = 0
        self.last_clock = ""
        self.wx = None
        self.wx_new = None
        self.wx_cache = {}
        self.wx_busy = False
        self.next_wx = 0
        self.astdb = {}
        self.astdb_mtime = 0
        self.next_astdb = 0
        self.ticker = ""                  # full linked-nodes text when it scrolls
        self.ticker_pos = 0
        self.next_scroll = 0
        self.lights = (None, None)        # (rx, tx) as last drawn
        self.status = {}                  # "reconn"/"gps" -> True on, False off, None not set
        self.next_status = 0
        self.extras_note = None

    # ---------------- serial ----------------
    def open_serial(self):
        port = self.cfg["serial_port"]
        baud = int(self.cfg["baud"])
        self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY)
        tty.setraw(self.fd)
        attrs = termios.tcgetattr(self.fd)
        attrs[4] = attrs[5] = BAUDS.get(baud, termios.B9600)
        attrs[2] = (attrs[2] | termios.CLOCAL | termios.CREAD) & ~termios.CRTSCTS
        attrs[0] &= ~(termios.IXON | termios.IXOFF | termios.IXANY)
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        termios.tcflush(self.fd, termios.TCIOFLUSH)
        log("Opened", port, "at", baud, "baud")

    def send(self, cmd):
        if not PY3 and isinstance(cmd, str):
            cmd = cmd.decode("utf-8", "replace")
        data = cmd.encode("latin-1", "replace") + TERM
        while data:
            n = os.write(self.fd, data)
            data = data[n:]

    @staticmethod
    def quote(text):
        return u'"' + u"%s" % text.replace("\\", "/").replace('"', "'") + u'"'

    def text(self, box, txt, font, color, pic, align=1):
        """align: 0 left, 1 center, 2 right."""
        x, y, w, h = box
        # sta=0 -> background cropped from picture `pic`, so old text is wiped cleanly
        self.send(u"xstr %d,%d,%d,%d,%d,%d,%d,%d,1,0,%s" % (
            x, y, w, h, font, color, pic, align, self.quote(txt)))

    # ---------------- Asterisk ----------------
    def asterisk(self, cli):
        if self.test_mode:
            log("[test] asterisk -rx", repr(cli))
            return ""
        return run([self.cfg["asterisk"], "-rx", cli], timeout=8)

    def ilink(self, func, remote):
        """ilink 1 = disconnect, 2 = monitor, 3 = transceive, 6 = disconnect all."""
        cli = "rpt cmd %s ilink %d %s" % (self.node, func, remote or "0")
        log("CMD:", cli)
        self.asterisk(cli)
        if self.test_mode:
            if func in (2, 3):
                self.fake_links[remote] = "R" if func == 2 else "T"
            elif func == 1:
                self.fake_links.pop(remote, None)
            elif func == 6:
                self.fake_links.clear()
        self.next_poll = time.time() + 1.5

    def send_dtmf(self, digits):
        cli = "rpt fun %s %s" % (self.node, digits)
        log("DTMF:", cli)
        result = self.asterisk(cli)
        self.next_poll = time.time() + 1.5
        return result is not None

    def poll_links(self):
        if self.test_mode:
            links = dict(self.fake_links)
        else:
            links = parse_rpt_nodes(self.asterisk("rpt nodes %s" % self.node))
        self.asterisk_ok = links is not None
        if links != self.links:
            self.links = links
            log("Links:", "Asterisk not responding" if links is None else (links or "none"))
            if not self.asleep and self.page == "main":
                self.draw_links()
                for i in range(6):
                    self.draw_button("fav%d" % i)

    def poll_keyed(self):
        if self.test_mode:
            air = dict(self.fake_air)
        else:
            air = parse_xnode(self.asterisk("rpt xnode %s" % self.node))
        if air != self.air:
            old_keyed = set((self.air or {}).get("keyed", []))
            self.air = air
            if air and (air["keyed"] or air["rx"]):
                who = ", ".join(air["keyed"]) or "local radio"
                log("On the air:", who)
                if self.cfg.get("wake_on_key"):
                    self.wake("someone keyed up")    # also keeps it on during a QSO
            if not self.asleep and self.page == "main":
                self.draw_lights()
                changed = old_keyed ^ set((air or {}).get("keyed", []))
                for i, fav in enumerate(self.cfg["favorites"]):
                    if fav["node"] in changed:
                        self.draw_button("fav%d" % i)

    # ---------------- names ----------------
    def load_astdb(self):
        for path in self.cfg.get("astdb_paths", []):
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime == self.astdb_mtime:
                return
            db = {}
            try:
                with io.open(path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        parts = line.split("|")
                        if len(parts) >= 2 and parts[0].strip().isdigit():
                            db[parts[0].strip()] = parts[1].strip()
            except (IOError, OSError):
                continue
            self.astdb, self.astdb_mtime = db, mtime
            log("Loaded %d node names from %s" % (len(db), path))
            return

    def node_name(self, node):
        for fav in self.cfg["favorites"]:
            if fav["node"] == node and fav["label"]:
                return fav["label"]
        return self.astdb.get(node, "")

    # ---------------- weather ----------------
    def start_weather(self):
        if self.wx_busy or self.cfg.get("weather_source") == "off" \
                or not self.cfg.get("weather_zip"):
            return
        self.wx_busy = True

        def work():
            try:
                self.wx_new = build_weather(self.cfg, self.test_mode, self.wx_cache)
                w = self.wx_new
                log("Weather:", ("%s%s%s %s (%s)" % (w["temp"], DEG, self.cfg["units"],
                                                    w["cond"], w["source"]))
                    if w["ok"] else "unavailable")
            except Exception as e:
                log("Weather error:", e)
            finally:
                self.wx_busy = False

        t = threading.Thread(target=work)
        t.daemon = True
        t.start()

    def weather_header(self):
        w = self.wx
        if not w or not w["ok"]:
            return ""
        txt = u"%s%s%s" % (w["temp"], DEG, self.cfg["units"])
        if w["cond"]:
            txt += u"  " + w["cond"]
        return txt

    # ---------------- drawing helpers ----------------
    def find(self, name, buttons):
        for b in buttons:
            if b[0] == name:
                return b
        return None

    def highlight(self, btn):
        _, x, y, w, h = btn
        self.send("draw %d,%d,%d,%d,%d" % (x, y, x + w - 1, y + h - 1, WHITE))
        self.send("draw %d,%d,%d,%d,%d" % (x + 1, y + 1, x + w - 2, y + h - 2, WHITE))

    def restore(self, btn, pic):
        _, x, y, w, h = btn
        self.send("picq %d,%d,%d,%d,%d" % (x, y, w, h, pic))

    def page_info(self, page=None):
        return {
            "main": (PIC_MAIN, MAIN_BUTTONS),
            "keypad": (PIC_KEYPAD, KEYPAD_BUTTONS),
            "dtmf": (PIC_DTMF, DTMF_BUTTONS),
            "system": (PIC_SYSTEM, SYSTEM_BUTTONS),
            "weather": (PIC_WEATHER, WEATHER_BUTTONS),
            "extras": (PIC_EXTRAS, EXTRAS_BUTTONS),
            "boot": (PIC_BOOT, []),
            "off": (PIC_BOOT, []),
        }[page or self.page]

    def show(self, page):
        self.page = page
        pic, buttons = self.page_info()
        self.send("pic 0,0,%d" % pic)
        self.last_clock = ""
        if page == "main":
            title = self.cfg.get("title") or ("NODE %s" % self.node)
            self.text(HEADER_TEXT["title"], title, FONT_BIG, WHITE, PIC_MAIN, align=0)
            self.draw_clock()
            self.draw_badges()
            self.draw_weather_header()
            self.lights = (None, None)
            self.draw_links()
            self.draw_lights()
            for b in buttons:
                self.draw_button(b[0])
        elif page == "keypad":
            self.draw_entry()
        elif page == "dtmf":
            self.draw_dtmf_entry()
        elif page == "system":
            self.draw_clock()
            self.draw_sysinfo()
            for b in buttons:
                self.draw_button(b[0])
        elif page == "weather":
            self.draw_weather_page()
        elif page == "extras":
            self.draw_clock()
            for b in buttons:
                self.draw_button(b[0])
            self.draw_extras_states()
            self.draw_extras_status()

    def flash(self, text, color, seconds=3):
        self.message = (text, color)
        self.message_until = time.time() + seconds

    def confirming(self, name):
        return time.time() < self.confirm.get(name, 0)

    # ---------------- main page ----------------
    def draw_clock(self):
        if self.page not in ("main", "system", "extras"):
            return
        fmt = "%H:%M" if self.cfg.get("clock_24h") else "%I:%M %p"
        now = time.strftime(fmt).lstrip("0")
        if now != self.last_clock:
            self.last_clock = now
            pic = {"main": PIC_MAIN, "system": PIC_SYSTEM}.get(self.page, PIC_EXTRAS)
            self.text(HEADER_TEXT["clock"], now, FONT_BIG, CYAN, pic, align=2)

    def badges_in_use(self):
        return any(self.status_path(k) for k, _, _ in BADGES)

    def draw_weather_header(self):
        txt = self.weather_header()
        if self.badges_in_use():
            box, font = HEADER_TEXT["weather"], FONT_SMALL
            if txt and text_px(txt) > box[2]:
                txt = txt.split("  ")[0]              # just the temperature
        else:
            box, font = HEADER_TEXT["weather_wide"], FONT_BIG
        self.text(box, txt, font, WHITE, PIC_MAIN)

    # ---------------- ReConn / GPS status badges ----------------
    def status_path(self, key):
        return ((self.cfg.get("status_files") or {}).get(key) or {}).get("path", "").strip()

    def read_status(self):
        """Trigger file present -> enabled or disabled, depending on the setting."""
        state = {}
        for key, _, _ in BADGES:
            info = (self.cfg.get("status_files") or {}).get(key) or {}
            path = (info.get("path") or "").strip()
            if not path:
                state[key] = None
                continue
            present = os.path.exists(path)
            on_when_present = str(info.get("present_means", "enabled")).lower().startswith("e")
            state[key] = present if on_when_present else not present
        return state

    def poll_status(self):
        state = self.read_status()
        if state == self.status:
            return
        for key, on in state.items():
            if self.status.get(key) != on and on is not None:
                log("%s is now %s" % (STATUS_NAMES[key], "ENABLED" if on else "DISABLED"))
        self.status = state
        if self.asleep:
            return
        if self.page == "main":
            self.draw_badges()
        elif self.page == "extras":
            self.draw_extras_states()

    def draw_badges(self):
        for key, label, box in BADGES:
            on = self.status.get(key)
            if on is None:
                self.text(box, "", FONT_SMALL, WHITE, PIC_MAIN)       # blank, not set up
            else:
                x, y, w, h = box
                self.send(u"xstr %d,%d,%d,%d,%d,%d,%d,1,1,1,%s" % (
                    x, y, w, h, FONT_SMALL, WHITE, BADGE_ON if on else BADGE_OFF,
                    self.quote(label)))

    def draw_extras_states(self):
        for i, key in enumerate(EXTRAS_STATE_KEYS):
            on = self.status.get(key)
            if on is None:
                txt, color = ("", GRAY)
            else:
                txt, color = ("ENABLED", GREEN) if on else ("DISABLED", RED)
            self.text(extras_state_box(i), txt, FONT_SMALL, color, PIC_EXTRAS, align=0)

    def links_parts(self):
        return [n + {"R": " (mon)", "C": " (...)"}.get(self.links[n], "")
                for n in sorted(self.links, key=lambda v: int(v))]

    def draw_links(self):
        """Linked-nodes line. Scrolls sideways when the list is wider than the box."""
        box = MAIN_TEXT["links"]
        count = ""
        if self.links:
            count = "%d node%s" % (len(self.links), "" if len(self.links) == 1 else "s")
        self.text(MAIN_TEXT["count"], count, FONT_SMALL, GRAY, PIC_MAIN, align=0)
        self.ticker = ""
        if self.message and time.time() < self.message_until:
            txt, color = self.message
        elif self.links is None:
            txt, color = ("Asterisk not responding", RED)
        elif not self.links:
            txt, color = ("Not linked", GRAY)
        else:
            txt, color = ("  ".join(self.links_parts()), GREEN)
            if self.cfg.get("scroll_links", True) and text_px(txt) > box[2]:
                self.ticker = txt + "      |      "
                self.ticker_pos %= len(self.ticker)
                self.draw_ticker()
                return
            if text_px(txt) > box[2]:          # scrolling turned off: show what fits
                parts, shown = self.links_parts(), []
                for part in parts:
                    trial = "  ".join(shown + [part]) + "  +99 more"
                    if text_px(trial) > box[2]:
                        break
                    shown.append(part)
                txt = "  ".join(shown) + "  +%d more" % (len(parts) - len(shown))
        self.text(box, txt, FONT_SMALL, color, PIC_MAIN, align=0)

    def draw_ticker(self):
        box = MAIN_TEXT["links"]
        loop = self.ticker * 2
        window, i = "", self.ticker_pos
        while text_px(window) < box[2] + 30 and len(window) < len(self.ticker):
            window += loop[i]
            i += 1
        self.text(box, window, FONT_SMALL, GREEN, PIC_MAIN, align=0)

    def draw_lights(self):
        """Big RX and TX lights: RX = your radio is receiving, TX = your node is transmitting."""
        air = self.air or {}
        state = (bool(air.get("rx")), bool(air.get("tx")))
        if state == self.lights:
            return
        old, self.lights = self.lights, state
        for i, (box, lit, color, ink, label) in enumerate([
                (RX_LIGHT, state[0], GREEN, rgb565(8, 40, 18), "RX"),
                (TX_LIGHT, state[1], RED, WHITE, "TX")]):
            if old[i] == lit:
                continue
            if lit:
                ix, iy, iw, ih = light_inner(box)
                self.send(u"xstr %d,%d,%d,%d,%d,%d,%d,1,1,1,%s" % (
                    ix, iy, iw, ih, FONT_BIG, ink, color, self.quote(label)))
            else:
                self.restore((label,) + box, PIC_MAIN)

    def draw_button(self, name):
        pic, buttons = self.page_info()
        btn = self.find(name, buttons)
        if not btn:
            return
        _, x, y, w, h = btn
        if self.page == "main":
            self.draw_main_button(btn)
        elif self.page == "system":
            self.restore(btn, pic)
            small, big = two_line_boxes(x, y, w, h)
            if self.confirming(name):
                self.text(small, "CONFIRM", FONT_SMALL, GRAY, pic)
                self.text(big, "TAP AGAIN", FONT_BIG, AMBER, pic)
            elif name == "restart":
                self.text(small, "RESTART", FONT_SMALL, GRAY, pic)
                self.text(big, "ASTERISK", FONT_BIG, WHITE, pic)
            elif name == "reboot":
                self.text(center_box(x, y, w, h, BIG_PX + 8), "REBOOT", FONT_BIG, WHITE, pic)
            elif name == "shutdown":
                self.text(center_box(x, y, w, h, BIG_PX + 8), "SHUTDOWN", FONT_BIG, WHITE, pic)
            # "back" is part of the picture
        elif self.page == "extras":
            self.restore(btn, pic)
            if name in EXTRAS_NAMES:
                code = self.extras_code(name)
                self.text(extras_code_box(btn), code or "not set", FONT_SMALL,
                          GRAY if code else DIM, pic)
        else:
            self.restore(btn, pic)

    def draw_main_button(self, btn):
        name, x, y, w, h = btn
        if name.startswith("fav"):
            fav = self.cfg["favorites"][int(name[3:])]
            self.restore(btn, PIC_MAIN)
            if not fav["node"]:
                self.text(center_box(x, y, w, h, SMALL_PX + 4), "- empty -", FONT_SMALL,
                          GRAY, PIC_MAIN)
                return
            label_box, node_box, bar = fav_boxes(x, y, w, h)
            self.text(label_box, fav["label"] or fav["node"], FONT_BIG, WHITE, PIC_MAIN)
            self.text(node_box, fav["node"], FONT_SMALL, GRAY, PIC_MAIN)
            state = (self.links or {}).get(fav["node"])
            if fav["node"] in (self.air or {}).get("keyed", []):
                self.send("fill %d,%d,%d,%d,%d" % (bar + (RED,)))
            elif state:
                color = {"T": GREEN, "R": AMBER}.get(state, CYAN)
                self.send("fill %d,%d,%d,%d,%d" % (bar + (color,)))
        elif name == "mode":
            self.restore(btn, PIC_MAIN)
            small, big = two_line_boxes(x, y, w, h)
            self.text(small, "MODE", FONT_SMALL, GRAY, PIC_MAIN)
            if self.monitor_mode:
                self.text(big, "MON", FONT_BIG, AMBER, PIC_MAIN)
            else:
                self.text(big, "TX", FONT_BIG, GREEN, PIC_MAIN)
        elif name == "discall":
            self.restore(btn, PIC_MAIN)
            small, big = two_line_boxes(x, y, w, h)
            if self.confirming(name):
                self.text(small, "CONFIRM", FONT_SMALL, GRAY, PIC_MAIN)
                self.text(big, "AGAIN", FONT_BIG, AMBER, PIC_MAIN)
            else:
                self.text(small, "DISC", FONT_SMALL, GRAY, PIC_MAIN)
                self.text(big, "ALL", FONT_BIG, WHITE, PIC_MAIN)
        elif name in ("keypad", "dtmf", "system", "extras"):
            self.restore(btn, PIC_MAIN)
        # "wx" is the invisible header area - nothing to draw

    # ---------------- keypad / dtmf ----------------
    def draw_entry(self, note=None):
        if note:
            self.text(ENTRY_TEXT, note[0], FONT_BIG, note[1], PIC_KEYPAD, align=0)
        elif self.entry:
            self.text(ENTRY_TEXT, "Node  " + self.entry, FONT_BIG, WHITE, PIC_KEYPAD, align=0)
        else:
            self.text(ENTRY_TEXT, "Enter node number", FONT_BIG, GRAY, PIC_KEYPAD, align=0)

    def draw_dtmf_entry(self):
        if self.dtmf:
            self.text(ENTRY_TEXT, "DTMF  " + self.dtmf, FONT_BIG, WHITE, PIC_DTMF, align=0)
        elif self.dtmf_note:
            self.text(ENTRY_TEXT, self.dtmf_note[0], FONT_BIG, self.dtmf_note[1], PIC_DTMF,
                      align=0)
        else:
            self.text(ENTRY_TEXT, "Enter DTMF command", FONT_BIG, GRAY, PIC_DTMF, align=0)

    # ---------------- system page ----------------
    def draw_sysinfo(self):
        units = self.cfg["units"]
        c = cpu_temp_c()
        if c is None:
            temp, color = "--", GRAY
        else:
            f = c * 9 / 5 + 32
            if units == "F":
                temp = u"%.1f%sF" % (f, DEG)
            else:
                temp = u"%.1f%sC" % (c, DEG)
            color = GREEN if c < 60 else AMBER if c < 75 else RED
        if not self.asterisk_ok:
            ast, acolor = "Not responding", RED
        else:
            n = len(self.links or {})
            ast, acolor = ("Running  -  %d link%s" % (n, "" if n == 1 else "s")), GREEN
        values = [(temp, color), (uptime_text(), WHITE), (ip_address(), WHITE),
                  (load_text(), WHITE), (ast, acolor)]
        for i, (txt, col) in enumerate(values):
            self.text(system_value_box(i), txt, FONT_BIG, col, PIC_SYSTEM, align=0)

    # ---------------- boot screen ----------------
    def boot_status(self, txt, color):
        x, y, w, h = BOOT_STATUS
        # solid background so it covers the "waiting" text baked into the picture
        self.send(u"xstr %d,%d,%d,%d,%d,%d,%d,1,1,1,%s" % (
            x, y, w, h, FONT_SMALL, color, BOOT_PANEL_BG, self.quote(txt)))

    def show_boot(self, status, color, node_line=None):
        self.page = "boot"
        self.send("pic 0,0,%d" % PIC_BOOT)
        self.boot_status(status, color)
        if node_line is None:
            title = self.cfg.get("title")
            node_line = ("%s  -  Node %s" % (title, self.node)) if title else ("Node %s" % self.node)
        self.text(BOOT_NODE, node_line, FONT_BIG, WHITE, PIC_BOOT)
        self.text(BOOT_VERSION, "v" + VERSION, FONT_SMALL, DIM, PIC_BOOT, align=2)

    def full_message(self, line1, line2=""):
        self.show_boot(line1, AMBER, node_line=line2)

    def system_command(self, key, default):
        cmd = self.cfg.get(key) or default
        log("SYSTEM:", cmd)
        if self.test_mode:
            return
        try:
            subprocess.Popen(cmd, shell=True)
        except Exception as e:
            log("System command failed:", e)

    # ---------------- weather page ----------------
    def draw_weather_page(self):
        w = self.wx
        pic = PIC_WEATHER
        u = self.cfg["units"]
        if not self.cfg.get("weather_zip") or self.cfg.get("weather_source") == "off":
            self.text(WX_TEXT["cond"], "Weather not set up", FONT_BIG, GRAY, pic)
            self.text(WX_TEXT["feels"], "Add your ZIP with install.sh", FONT_SMALL, GRAY, pic)
            return
        if not w or not w["ok"]:
            self.text(WX_TEXT["cond"], "Waiting for data...", FONT_BIG, GRAY, pic)
            return
        self.text(WX_TEXT["place"], w["place"], FONT_BIG, WHITE, pic, align=2)
        self.text(WX_TEXT["temp"], u"%s%s%s" % (w["temp"], DEG, u), FONT_HUGE, WHITE, pic)
        self.text(WX_TEXT["cond"], w["cond"], FONT_BIG, CYAN, pic)
        if w["feels"]:
            self.text(WX_TEXT["feels"], u"Feels like %s%s" % (w["feels"], DEG), FONT_SMALL,
                      GRAY, pic)
        if w["humid"]:
            self.text(WX_TEXT["humid"], "Humidity %s%%" % w["humid"], FONT_SMALL, GRAY, pic)
        if w["wind"]:
            self.text(WX_TEXT["wind"], "Wind %s" % w["wind"], FONT_SMALL, GRAY, pic)
        self.text(WX_TEXT["src"], "Current: " + w["source"].split(" +")[0], FONT_SMALL, DIM,
                  pic)
        fmt = "%H:%M" if self.cfg.get("clock_24h") else "%I:%M %p"
        self.text(WX_TEXT["updated"], "Updated " + time.strftime(fmt, time.localtime(
            w["time"])).lstrip("0"), FONT_SMALL, GRAY, pic, align=0)
        if not w["days"]:
            day, _, cond = wx_day_boxes(0)
            self.text(cond, "Forecast unavailable", FONT_SMALL, GRAY, pic, align=0)
        for i, d in enumerate(w["days"][:3]):
            day_box, hilo_box, cond_box = wx_day_boxes(i)
            self.text(day_box, d["name"], FONT_BIG, WHITE, pic, align=0)
            self.text(hilo_box, u"%s%s / %s%s" % (d["hi"], DEG, d["lo"], DEG), FONT_BIG,
                      AMBER, pic, align=2)
            cond = d["cond"]
            if d.get("pop"):
                cond += "  -  %s%% precip" % d["pop"]
            self.text(cond_box, cond, FONT_SMALL, GRAY, pic, align=0)

    # ---------------- touch handling ----------------
    def hit(self, buttons, tx, ty):
        for b in buttons:
            _, x, y, w, h = b
            if x <= tx < x + w and y <= ty < y + h:
                return b
        return None

    def on_touch(self, tx, ty):
        page = self.page
        _, buttons = self.page_info()
        btn = self.hit(buttons, tx, ty)
        if not btn:
            return
        if btn[0] not in ("wx", "badges"):    # invisible header areas
            self.highlight(btn)
        getattr(self, "act_" + page)(btn[0])
        if self.page == page:
            self.draw_button(btn[0])

    def act_main(self, name):
        if name.startswith("fav"):
            remote = self.cfg["favorites"][int(name[3:])]["node"]
            if not remote:
                return
            if remote in (self.links or {}):
                self.ilink(1, remote)
                self.flash("Disconnecting %s..." % remote, AMBER)
            else:
                self.ilink(2 if self.monitor_mode else 3, remote)
                self.flash("%s %s..." % ("Monitoring" if self.monitor_mode else "Connecting",
                                         remote), CYAN)
            self.draw_links()
        elif name == "mode":
            self.monitor_mode = not self.monitor_mode
        elif name == "keypad":
            self.entry = ""
            self.show("keypad")
        elif name == "dtmf":
            self.dtmf, self.dtmf_note = "", None
            self.show("dtmf")
        elif name == "system":
            self.confirm.clear()
            self.next_sys = time.time() + 3
            self.show("system")
        elif name == "wx":
            self.show("weather")
        elif name == "badges":
            if self.badges_in_use():
                self.extras_note = None
                self.show("extras")
            else:
                self.show("weather")       # no badges: this part of the header is weather
        elif name == "extras":
            self.extras_note = None
            self.show("extras")
        elif name == "discall":
            if self.confirming(name):
                self.confirm.pop(name, None)
                self.ilink(6, None)
                self.flash("Disconnecting all links...", AMBER)
                self.draw_links()
            else:
                self.confirm[name] = time.time() + 3

    def act_keypad(self, name):
        if name[0] == "k" and name[1:].isdigit():
            if len(self.entry) < MAX_NODE_DIGITS:
                self.entry += name[1:]
            self.draw_entry()
        elif name == "clr":
            self.entry = ""
            self.draw_entry()
        elif name == "del":
            self.entry = self.entry[:-1]
            self.draw_entry()
        elif name == "back":
            self.show("main")
        elif name in ("connect", "monitor", "disc"):
            remote = clean_node(self.entry)
            if len(remote) < 3:
                self.draw_entry(("Enter a node number first", RED))
                return
            func = {"connect": 3, "monitor": 2, "disc": 1}[name]
            verb = {"connect": "Connecting", "monitor": "Monitoring",
                    "disc": "Disconnecting"}[name]
            self.ilink(func, remote)
            self.flash("%s %s..." % (verb, remote), CYAN if func != 1 else AMBER)
            self.entry = ""
            self.show("main")

    def act_dtmf(self, name):
        if name in DTMF_KEYS:
            if len(self.dtmf) < MAX_DTMF:
                self.dtmf += DTMF_KEYS[name]
            self.dtmf_note = None
            self.draw_dtmf_entry()
        elif name == "del":
            self.dtmf = self.dtmf[:-1]
            self.draw_dtmf_entry()
        elif name == "clr":
            self.dtmf, self.dtmf_note = "", None
            self.draw_dtmf_entry()
        elif name == "back":
            self.show("main")
        elif name == "send":
            digits = clean_dtmf(self.dtmf)
            if not digits:
                self.dtmf_note = ("Enter a command first", RED)
            elif self.send_dtmf(digits):
                self.dtmf_note = ("Sent  " + digits, GREEN)
            else:
                self.dtmf_note = ("Asterisk not responding", RED)
            self.dtmf = ""
            self.draw_dtmf_entry()

    def act_system(self, name):
        if name == "back":
            self.confirm.clear()
            self.show("main")
            return
        if not self.confirming(name):
            self.confirm.clear()
            for b in SYSTEM_BUTTONS:
                if b[0] != name:
                    self.draw_button(b[0])
            self.confirm[name] = time.time() + 3
            return
        self.confirm.clear()
        if name == "restart":
            default = "/usr/local/sbin/astres.sh" if os.path.exists(
                "/usr/local/sbin/astres.sh") else "systemctl restart asterisk"
            self.system_command("restart_asterisk_cmd", default)
            self.flash("Restarting Asterisk...", AMBER, 8)
            self.links = {}
            self.next_poll = time.time() + 8
            self.show("main")
        elif name == "reboot":
            self.full_message("Rebooting node...", "Back in about a minute")
            self.system_command("reboot_cmd", "systemctl reboot")
            self.page = "off"
        elif name == "shutdown":
            self.full_message("Shutting down...",
                              "Unplug 30 s after the green light stops")
            self.system_command("shutdown_cmd", "systemctl poweroff")
            self.page = "off"

    def act_weather(self, name):
        if name == "back":
            self.show("main")
        elif name == "refresh":
            self.next_wx = 0
            self.start_weather()

    # ---------------- extras page ----------------
    def extras_code(self, name):
        return clean_dtmf((self.cfg.get("extras_dtmf") or {}).get(name, ""))

    def draw_extras_status(self):
        x, y, w, h = EXTRAS_STATUS_TEXT
        if self.extras_note:
            txt, color = self.extras_note
        else:
            txt, color = ("Tap a button to send its DTMF command", GRAY)
        self.text(EXTRAS_STATUS_TEXT, txt, FONT_SMALL, color, PIC_EXTRAS, align=0)

    def act_extras(self, name):
        if name == "back":
            self.show("main")
            return
        what = EXTRAS_NAMES.get(name)
        if not what:
            return
        code = self.extras_code(name)
        if not code:
            self.extras_note = ("%s: no DTMF code set (run install.sh)" % what, AMBER)
        elif self.send_dtmf(code):
            self.extras_note = ("Sent %s  (%s)" % (what, code), GREEN)
            self.next_status = time.time() + 1      # check the trigger file again soon
        else:
            self.extras_note = ("Asterisk not responding", RED)
        self.draw_extras_status()

    def act_off(self, name):
        pass

    def act_boot(self, name):
        pass

    # ---------------- incoming bytes ----------------
    def feed(self, data):
        self.rx.extend(data)
        while self.rx:
            first = self.rx[0]
            if first in (0x67, 0x68):           # touch X/Y: 67 xh xl yh yl ev ff ff ff
                if len(self.rx) < 9:
                    return
                pkt = self.rx[:9]
                del self.rx[:9]
                if pkt[6:9] != bytearray(TERM):
                    self.resync()
                    continue
                x = (pkt[1] << 8) | pkt[2]
                y = (pkt[3] << 8) | pkt[4]
                if pkt[5] != 1 or self.page == "off":
                    continue
                if self.asleep or first == 0x68:
                    self.wake("touch")           # waking touch never presses a button
                else:
                    self.last_touch = time.time()
                    self.on_touch(x, y)
                continue
            end = self.rx.find(TERM)
            if end < 0:
                if len(self.rx) > 64:
                    del self.rx[:]
                return
            pkt = bytes(self.rx[:end])
            del self.rx[:end + 3]
            self.on_message(bytearray(pkt))

    def resync(self):
        end = self.rx.find(TERM)
        if end >= 0:
            del self.rx[:end + 3]
        else:
            del self.rx[:]

    def on_message(self, pkt):
        if not pkt:
            return
        code = pkt[0]
        if code == 0x88 or pkt == bytearray(b"\x00\x00\x00"):
            log("Screen (re)started - initializing")
            self.init_screen()
        elif code == 0x86:
            self.asleep = True
        elif code == 0x87:
            self.wake("screen")

    # ---------------- screen saver ----------------
    # The screen saver only turns the backlight off (dim=0). The Nextion stays
    # awake, so every touch still reaches the Pi and nothing depends on the
    # screen's own sleep/wake messages.
    def go_sleep(self):
        log("Screen saver on (no touch for %d s)" % int(self.cfg.get("sleep_seconds", 0)))
        self.asleep = True
        self.confirm.clear()
        self.send("dim=0")

    def wake(self, why):
        self.last_touch = time.time()
        if not self.asleep:
            return
        log("Screen saver off (%s)" % why)
        self.asleep = False
        self.send("sleep=0")              # in case the screen slept on its own
        self.show("main")
        self.send("dim=%d" % int(self.cfg.get("brightness", 100)))

    # ---------------- main loop ----------------
    def init_screen(self):
        self.send("")                     # clear any partial command on the screen
        self.send("bkcmd=0")
        self.send("sleep=0")
        self.send("dim=%d" % int(self.cfg.get("brightness", 100)))
        self.send("sendxy=1")
        self.send("thup=1")               # a touch wakes the screen from sleep
        self.send("thsp=0")               # the Pi runs the idle timer, not the screen
        self.send("ussp=0")
        self.asleep = False
        self.last_touch = time.time()
        secs = float(self.cfg.get("boot_seconds", 4))
        if secs > 0:
            self.boot_until = time.time() + secs
            self.boot_shown = None
            self.show_boot("Starting the panel...", CYAN)
        else:
            self.show("main")

    def tick_boot(self, now):
        if self.asterisk_ok:
            msg = ("Node %s ready" % self.node, GREEN)
        else:
            msg = ("Waiting for Asterisk...", AMBER)
        if msg != self.boot_shown:
            self.boot_shown = msg
            self.boot_status(*msg)
        if now >= self.boot_until:
            self.last_touch = now
            self.show("main")

    def tick(self):
        now = time.time()
        if now >= self.next_status:
            self.next_status = now + float(self.cfg.get("status_poll_seconds", 2))
            self.poll_status()
        if now >= self.next_astdb:
            self.next_astdb = now + 3600
            self.load_astdb()
        if now >= self.next_poll:
            self.next_poll = now + float(self.cfg.get("poll_seconds", 3))
            self.poll_links()
        if now >= self.next_keyed:
            self.next_keyed = now + float(self.cfg.get("keyed_poll_seconds", 1))
            self.poll_keyed()
        if now >= self.next_wx:
            self.next_wx = now + 60 * float(self.cfg.get("weather_minutes", 15))
            self.start_weather()
        if self.wx_new is not None:
            new, self.wx_new = self.wx_new, None
            if new["ok"] or not self.wx:
                self.wx = new
            if not new["ok"] or (self.cfg.get("forecast") and not new["days"]):
                self.next_wx = min(self.next_wx, now + 120)    # retry soon, e.g. at boot
            if not self.asleep:
                if self.page == "main":
                    self.draw_weather_header()
                elif self.page == "weather":
                    self.show("weather")
        if self.asleep or self.page == "off":
            return
        if self.page == "boot":
            self.tick_boot(now)
            return
        idle = int(self.cfg.get("sleep_seconds", 0))
        if idle > 0 and now - self.last_touch >= idle:
            self.go_sleep()
            return
        self.draw_clock()
        for name, until in list(self.confirm.items()):
            if now >= until:
                del self.confirm[name]
                self.draw_button(name)
        if (self.page == "main" and self.ticker and not self.message
                and now >= self.next_scroll):
            self.next_scroll = now + float(self.cfg.get("scroll_step_seconds", 0.4))
            self.ticker_pos = (self.ticker_pos + 2) % len(self.ticker)
            self.draw_ticker()
        if self.page == "main" and self.message and now >= self.message_until:
            self.message = None
            self.draw_links()
        if self.page == "system" and now >= self.next_sys:
            self.next_sys = now + 3
            self.draw_sysinfo()

    def run(self):
        self.open_serial()
        self.load_astdb()
        self.poll_links()
        self.poll_keyed()
        self.init_screen()
        while True:
            r, _, _ = select.select([self.fd], [], [], 0.2)
            if r:
                data = os.read(self.fd, 256)
                if not data:
                    raise IOError("serial port closed")
                self.feed(bytearray(data))
            self.tick()


def main():
    args = sys.argv[1:]
    if "--version" in args:
        print(VERSION)
        return
    cfg_path = CONFIG_PATH
    if "--config" in args:
        cfg_path = args[args.index("--config") + 1]
    if "--geocode" in args:
        res = geocode(args[args.index("--geocode") + 1])
        print("%s|%s|%s" % res if res else "NOTFOUND")
        return
    cfg = load_config(cfg_path)
    if "--port" in args:
        cfg["serial_port"] = args[args.index("--port") + 1]
    test_mode = "--test" in args
    if "--weather-test" in args:
        w = build_weather(cfg, test_mode)
        print(json.dumps(w, indent=2))
        return
    log("Nextion node panel v%s starting - node" % VERSION, cfg["node"],
        "(test mode)" if test_mode else "")
    panel = Panel(cfg, test_mode=test_mode)
    try:
        panel.run()
    except KeyboardInterrupt:
        log("Stopped")


if __name__ == "__main__":
    main()

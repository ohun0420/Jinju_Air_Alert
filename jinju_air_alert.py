# -*- coding: utf-8 -*-
"""
진주 대기질 경보 프로그램 (Jinju Air Alert)

공공데이터 기반 진주시 대기오염 탐구의 후속 활동으로 제작한
취약계층(노약자·호흡기 질환자) 대상 3단계 대기오염 경보 프로그램.

- 데이터: 에어코리아 시도별 실시간 측정정보 API (공공데이터포털)
- 대상 측정소: 진주시 대안동, 상대동, 상봉동, 정촌면
- 경보 기준: 환경부 예보 등급을 민감계층 기준으로 보수적으로 조정
  (탐구 보고서에서 확인한 겨울·봄 미세먼지 / 여름 오존의 이원적 구조 반영)
- 외부 라이브러리 없이 표준 라이브러리만 사용 (배포 안정성)
"""

import json
import os
import sys
import threading
import urllib.parse
import urllib.request
from datetime import datetime

import tkinter as tk
from tkinter import messagebox

APP_TITLE = "진주 대기질 경보"
STATIONS = ["대안동", "상대동", "상봉동", "정촌면"]
API_URL = "http://apis.data.go.kr/B552584/ArpltnInforInqireSvc/getCtprvnRltmMesureDnsty"
REFRESH_MS = 60 * 60 * 1000  # 1시간마다 자동 갱신

# 3단계 경보 기준 (민감계층 보수 기준)
# level 0 안심 / 1 주의 / 2 외출 자제
THRESHOLDS = {
    "pm25": (15, 35),    # ㎍/㎥  (국내 연평균 기준 15를 '주의' 하한으로 사용)
    "pm10": (30, 80),    # ㎍/㎥
    "o3":   (0.030, 0.090),  # ppm
}

LEVELS = [
    {"name": "안심",      "color": "#2E7D32", "fg": "white",
     "msg": "대기 상태가 좋습니다. 평소처럼 활동하셔도 됩니다."},
    {"name": "주의",      "color": "#F9A825", "fg": "black",
     "msg": "민감하신 분은 장시간 야외 활동을 줄이고,\n외출 시 보건용 마스크(KF80 이상)를 착용하세요."},
    {"name": "외출 자제", "color": "#C62828", "fg": "white",
     "msg": "노약자·호흡기 질환자는 외출을 자제하세요.\n창문을 닫고, 부득이한 외출 시 KF94 마스크를 착용하세요."},
]

DEMO_DATA = {
    "대안동": {"pm25": 14.0, "pm10": 24.0, "o3": 0.031, "time": "데모 데이터"},
    "상대동": {"pm25": 17.0, "pm10": 28.0, "o3": 0.029, "time": "데모 데이터"},
    "상봉동": {"pm25": 13.0, "pm10": 23.0, "o3": 0.033, "time": "데모 데이터"},
    "정촌면": {"pm25": 14.0, "pm10": 26.0, "o3": 0.026, "time": "데모 데이터"},
}


# ---------- 유틸 ----------

def base_dir():
    """exe로 패키징된 경우 exe 위치, 아니면 스크립트 위치."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def config_path():
    return os.path.join(base_dir(), "config.json")


def load_key():
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            return json.load(f).get("service_key", "").strip()
    except (OSError, json.JSONDecodeError):
        return ""


def save_key(key):
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump({"service_key": key.strip()}, f, ensure_ascii=False, indent=2)


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None  # API가 '-' 또는 '통신장애'를 줄 수 있음


# ---------- 데이터 수집 ----------

def fetch_data(service_key):
    """에어코리아에서 경남 실시간 자료를 받아 진주 4개 측정소만 추림."""
    params = {
        "serviceKey": service_key,
        "returnType": "json",
        "numOfRows": "200",
        "pageNo": "1",
        "sidoName": "경남",
        "ver": "1.0",
    }
    url = API_URL + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=15) as r:
        raw = r.read().decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 인증키 오류 시 XML이 반환됨
        raise ValueError("인증키가 올바르지 않습니다. 공공데이터포털의 '일반 인증키(Decoding)'를 사용하세요.")

    items = data.get("response", {}).get("body", {}).get("items", []) or []
    out = {}
    for it in items:
        name = it.get("stationName")
        if name in STATIONS:
            out[name] = {
                "pm25": to_float(it.get("pm25Value")),
                "pm10": to_float(it.get("pm10Value")),
                "o3":   to_float(it.get("o3Value")),
                "time": it.get("dataTime", ""),
            }
    if not out:
        raise ValueError("측정소 자료를 찾지 못했습니다. 잠시 후 다시 시도하세요.")
    return out


# ---------- 경보 판정 ----------

def pollutant_level(key, value):
    if value is None:
        return 0
    low, high = THRESHOLDS[key]
    if value > high:
        return 2
    if value > low:
        return 1
    return 0


def assess(data):
    """측정소별 자료를 종합해 (경보단계, 원인 목록, 시각) 반환."""
    worst = 0
    reasons = []
    latest = ""
    for st, d in data.items():
        for key, label, unit in (("pm25", "초미세먼지", "㎍/㎥"),
                                 ("pm10", "미세먼지", "㎍/㎥"),
                                 ("o3", "오존", "ppm")):
            lv = pollutant_level(key, d.get(key))
            if lv > worst:
                worst = lv
                reasons = []
            if lv == worst and lv > 0:
                v = d[key]
                txt = f"{st} {label} {v:.3f}{unit}" if key == "o3" else f"{st} {label} {v:.0f}{unit}"
                reasons.append(txt)
        latest = d.get("time") or latest
    return worst, reasons[:3], latest


def season_tip():
    m = datetime.now().month
    if m in (11, 12, 1, 2, 3, 4):
        return "지금은 겨울·봄철입니다. 대기 정체로 미세먼지가 쌓이기 쉬운 계절이니 아침 환기는 짧게 하세요."
    return "지금은 여름철입니다. 햇빛이 강한 오후 2~5시에는 오존이 높아지니 이 시간대 야외 활동을 줄이세요."


# ---------- GUI ----------

class App:
    def __init__(self, root):
        self.root = root
        self.last_level = -1  # 경보 악화 시에만 팝업을 띄우기 위한 기억값
        root.title(APP_TITLE)
        root.geometry("660x720")
        root.configure(bg="white")

        f_big = ("맑은 고딕", 46, "bold")
        f_mid = ("맑은 고딕", 15)
        f_sm = ("맑은 고딕", 12)

        # 상태 표시부 (신호등)
        self.status_frame = tk.Frame(root, bg="#9E9E9E", height=150)
        self.status_frame.pack(fill="x", padx=16, pady=(16, 8))
        self.status_frame.pack_propagate(False)
        self.status_label = tk.Label(self.status_frame, text="확인 중…",
                                     font=f_big, bg="#9E9E9E", fg="white")
        self.status_label.pack(expand=True)

        # 행동 요령
        self.msg_label = tk.Label(root, text="", font=f_mid, bg="white",
                                  justify="center", wraplength=560)
        self.msg_label.pack(pady=(4, 2))
        self.tip_label = tk.Label(root, text=season_tip(), font=f_sm, bg="white",
                                  fg="#555555", justify="center", wraplength=560)
        self.tip_label.pack(pady=(0, 8))

        # 측정소별 표
        table = tk.Frame(root, bg="white")
        table.pack(pady=4)
        headers = ["측정소", "초미세먼지", "미세먼지", "오존"]
        for j, h in enumerate(headers):
            tk.Label(table, text=h, font=("맑은 고딕", 13, "bold"), bg="#EEF2F8",
                     width=12, pady=6, relief="ridge", bd=1).grid(row=0, column=j, sticky="nsew")
        self.cells = {}
        for i, st in enumerate(STATIONS, start=1):
            tk.Label(table, text=st, font=f_sm, bg="white", width=12, pady=6,
                     relief="ridge", bd=1).grid(row=i, column=0)
            for j, key in enumerate(("pm25", "pm10", "o3"), start=1):
                lb = tk.Label(table, text="-", font=f_sm, bg="white", width=12,
                              pady=6, relief="ridge", bd=1)
                lb.grid(row=i, column=j)
                self.cells[(st, key)] = lb

        self.time_label = tk.Label(root, text="", font=f_sm, bg="white", fg="#777777")
        self.time_label.pack(pady=(6, 2))

        # 조작부
        ctrl = tk.Frame(root, bg="white")
        ctrl.pack(pady=8)
        tk.Button(ctrl, text="지금 새로고침", font=f_mid, command=self.refresh,
                  width=14).grid(row=0, column=0, padx=6)

        keyf = tk.Frame(root, bg="white")
        keyf.pack(pady=(4, 12))
        tk.Label(keyf, text="공공데이터포털 인증키:", font=f_sm, bg="white").grid(row=0, column=0, padx=4)
        self.key_entry = tk.Entry(keyf, font=f_sm, width=34)
        self.key_entry.grid(row=0, column=1, padx=4)
        self.key_entry.insert(0, load_key())
        tk.Button(keyf, text="저장", font=f_sm,
                  command=self.on_save_key).grid(row=0, column=2, padx=4)

        self.demo_label = tk.Label(root, text="", font=f_sm, bg="white", fg="#C62828")
        self.demo_label.pack()

        self.refresh()
        root.after(REFRESH_MS, self.auto_refresh)

    # ----- 동작 -----

    def on_save_key(self):
        save_key(self.key_entry.get())
        messagebox.showinfo(APP_TITLE, "인증키를 저장했습니다.")
        self.refresh()

    def auto_refresh(self):
        self.refresh()
        self.root.after(REFRESH_MS, self.auto_refresh)

    def refresh(self):
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self):
        key = self.key_entry.get().strip() or load_key()
        demo = False
        try:
            if key:
                data = fetch_data(key)
            else:
                data, demo = DEMO_DATA, True
        except Exception as e:  # 네트워크·인증 오류 → 화면에 표시하고 데모로 대체하지 않음
            self.root.after(0, lambda: self._show_error(str(e)))
            return
        self.root.after(0, lambda: self._render(data, demo))

    def _show_error(self, msg):
        self.demo_label.config(text=f"오류: {msg}")

    def _render(self, data, demo):
        level, reasons, t = assess(data)
        info = LEVELS[level]
        self.status_frame.config(bg=info["color"])
        self.status_label.config(text=info["name"], bg=info["color"], fg=info["fg"])
        extra = ("  ·  ".join(reasons)) if reasons else ""
        self.msg_label.config(text=(extra + "\n" if extra else "") + info["msg"])
        for st in STATIONS:
            d = data.get(st, {})
            for key in ("pm25", "pm10", "o3"):
                v = d.get(key)
                if v is None:
                    txt = "-"
                elif key == "o3":
                    txt = f"{v:.3f}"
                else:
                    txt = f"{v:.0f}"
                lv = pollutant_level(key, v)
                self.cells[(st, key)].config(
                    text=txt, bg=("#FFFFFF", "#FFF8E1", "#FFEBEE")[lv])
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.time_label.config(text=f"측정 시각 {t or '-'}  /  확인 시각 {now}")
        self.demo_label.config(
            text="인증키가 없어 데모 데이터를 표시 중입니다. 아래에 인증키를 저장하세요." if demo else "")
        # 알림 팝업: 첫 확인은 화면 표시로 충분하므로 제외하고,
        # 이후 자동 갱신에서 경보가 이전보다 나빠졌을 때만 띄움
        if self.last_level >= 0 and level > self.last_level:
            messagebox.showwarning(APP_TITLE, f"대기질 경보: {info['name']}\n\n{info['msg']}")
        self.last_level = level


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

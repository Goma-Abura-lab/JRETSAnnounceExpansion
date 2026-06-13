import os
import time
import json
import re
import threading
import queue
import difflib
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFileDialog, QStackedWidget, QMessageBox,
    QListWidget, QFrame, QSplitter, QTextEdit
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QFont, QColor, QIcon, QPixmap

import cv2
import numpy as np
import pytesseract
import keyboard
import mss

import sys

# ==========================================
# 定数・設定値
# ==========================================

if getattr(sys, 'frozen', False):
    # PyInstallerでパッケージ化されている場合
    SCRIPT_DIR = sys._MEIPASS
    EXE_DIR = os.path.dirname(sys.executable)
else:
    # 通常のスクリプト実行時
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    EXE_DIR = SCRIPT_DIR

# 内部アセット（同梱するPNGなど）
LOGO_PATH = os.path.join(SCRIPT_DIR, "JRETSAnnounceExpansion.png")
ICON_PATH = os.path.join(SCRIPT_DIR, "JRETSAnnounceExpansion.ico")

# 外部ファイル（exeと同じ場所に置く必要があるTesseractやJSON）
TESSERACT_CMD = os.path.join(EXE_DIR, 'Tesseract-OCR', 'tesseract.exe')
pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD


# ==========================================
# 監視エンジン（バックグラウンドスレッド）
# ==========================================

DEFAULT_CONFIG = {
    "ROI_STATION_NAME": {"x": 90.8, "y": 9.5,  "w": 7.6, "h": 3.7},
    "ROI_DISTANCE":     {"x": 91.9, "y": 23.4, "w": 2.5, "h": 4.3},
    "ROI_SPEED":        {"x": 93.5, "y": 13.3, "w": 3.0, "h": 3.4},
    "ROI_POSITION":     {"x": 92.5, "y": 23.9, "w": 5.7, "h": 3.6},
    "GREEN_PIXEL_RATIO_THRESHOLD": 0.05,
    "STATION_MATCH_THRESHOLD": 0.4
}

class TrainAutoAnnouncer:
    """監視ロジック本体。GUIから別スレッドで実行される。"""

    def __init__(self, settings_file: str, log_queue: queue.Queue, on_event_triggered=None, config=None):
        self.settings_file = settings_file
        self.log_queue = log_queue
        self.settings: list = []
        self._stop_event = threading.Event()
        
        self.config = DEFAULT_CONFIG.copy()
        if config:
            self.config.update(config)
            
        self.on_event_triggered = on_event_triggered

        self.last_state_str = "不明"
        self.current_event_idx = 0
        self.last_dist_int = None # 前回の確定距離（数値）
        self.prev_event_idx = -1
        self.last_trigger_time = 0 # 最後にイベントがトリガーされた時刻
        self.last_triggered_station = "" # 最後にトリガーされた駅名

    def stop(self):
        self._stop_event.set()

    def skip_event(self):
        """手動で次のイベントへ進める"""
        if self.current_event_idx < len(self.settings):
            self.current_event_idx += 1
            self._log(f"[-操作-] イベントをスキップしました (次: {self.current_event_idx + 1})")

    def back_event(self):
        """手動で前のイベントへ戻す"""
        if self.current_event_idx > 0:
            self.current_event_idx -= 1
            self._log(f"[-操作-] イベントを1つ戻しました (現在: {self.current_event_idx + 1})")

    def _log(self, message: str):
        """GUIのキューにメッセージを送る（スレッドセーフ）"""
        self.log_queue.put(message)
        print(message)

    def load_settings(self) -> bool:
        """JSONを読み込み、成功すればTrue、失敗すればFalse。"""
        try:
            with open(self.settings_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, list):
                self._log(f"[{datetime.now().strftime('%H:%M:%S')}] エラー: JSONファイルが配列(リスト)形式ではありません。")
                return False
            self.settings = data
            return True
        except Exception as e:
            self._log(f"[{datetime.now().strftime('%H:%M:%S')}] 設定ファイルの読み込みに失敗しました: {e}")
            return False

    def get_pixel_roi(self, monitor, roi_pct):
        width = monitor["width"]
        height = monitor["height"]
        x = int(monitor["left"] + width * (roi_pct["x"] / 100))
        y = int(monitor["top"] + height * (roi_pct["y"] / 100))
        w = int(width * (roi_pct["w"] / 100))
        h = int(height * (roi_pct["h"] / 100))
        return {"top": y, "left": x, "width": w, "height": h}

    def capture_roi(self, sct, roi_dict):
        sct_img = sct.grab(roi_dict)
        return np.array(sct_img)[:, :, :3]

    def preprocess_for_ocr(self, img):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
        return thresh

    def is_station_match(self, ocr_text: str, target: str, threshold=0.4) -> bool:
        if not target:
            # ターゲット駅名が空の場合は、駅名チェックをスキップ（距離のみで判定）
            return True
        if not ocr_text:
            return False
        clean_ocr = re.sub(r'[^\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]', '', ocr_text)
        clean_target = re.sub(r'[^\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]', '', target)
        if not clean_ocr or not clean_target:
            return False
        ratio = difflib.SequenceMatcher(None, clean_ocr, clean_target).ratio()
        return ratio >= threshold

    def get_speed(self, sct, monitor):
        """speed計から数値（0.0形式）をOCRで取得する"""
        roi_px = self.get_pixel_roi(monitor, self.config["ROI_SPEED"])
        img = self.capture_roi(sct, roi_px)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        scaled = cv2.resize(gray, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
        _, thresh = cv2.threshold(scaled, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
        padded = cv2.copyMakeBorder(thresh, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255)
        text = pytesseract.image_to_string(padded, lang='eng',
                                           config='--psm 7 -c tessedit_char_whitelist=0123456789.').strip()
        return re.sub(r'[^0-9.]', '', text)

    def is_position_green(self, sct, monitor):
        """停車位置数値が緑色かどうかを判定する"""
        roi_px = self.get_pixel_roi(monitor, self.config["ROI_POSITION"])
        img = self.capture_roi(sct, roi_px)
        
        # 全ピクセル数を計算
        total_pixels = img.shape[0] * img.shape[1]
        if total_pixels == 0:
            return False
            
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        # 緑色範囲 (H:40-90, S:80以上, V:80以上)
        green_mask = cv2.inRange(hsv, np.array([40, 80, 80]), np.array([90, 255, 255]))
        
        green_pixels = cv2.countNonZero(green_mask)
        ratio = green_pixels / total_pixels
        
        return ratio >= self.config["GREEN_PIXEL_RATIO_THRESHOLD"]

    def get_station_name(self, sct, monitor):
        roi_px = self.get_pixel_roi(monitor, self.config["ROI_STATION_NAME"])
        img = self.capture_roi(sct, roi_px)
        thresh = self.preprocess_for_ocr(img)
        text = pytesseract.image_to_string(thresh, lang='jpn', config='--psm 7').strip()
        return re.sub(r'[^\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]', '', text)

    def get_distance(self, sct, monitor):
        roi_px = self.get_pixel_roi(monitor, self.config["ROI_DISTANCE"])
        img = self.capture_roi(sct, roi_px)
        thresh = self.preprocess_for_ocr(img)
        text = pytesseract.image_to_string(thresh, lang='eng', config='--psm 7 -c tessedit_char_whitelist=0123456789').strip()
        digits = re.sub(r'\D', '', text)
        return digits[:2] if len(digits) >= 2 else digits

    def _update_screen_state(self, sct, monitor):
        station = self.get_station_name(sct, monitor)
        ocr_dist = self.get_distance(sct, monitor)
        speed = self.get_speed(sct, monitor)
        pos_green = self.is_position_green(sct, monitor)
        return station, ocr_dist, speed, pos_green

    def _guess_distance(self, ocr_dist):
        if self.prev_event_idx != self.current_event_idx:
            self.last_dist_int = None
            self.prev_event_idx = self.current_event_idx

        dist = ocr_dist
        if ocr_dist.isdigit():
            curr_val = int(ocr_dist)
            if self.last_dist_int is not None:
                expected = self.last_dist_int - 1
                if curr_val != expected and expected >= 0:
                    s_ocr = str(curr_val)
                    s_exp = str(expected)
                    if s_ocr in s_exp or s_exp.endswith(s_ocr):
                        self._log(f"[補正] 距離 {ocr_dist} -> {expected} (前回 {self.last_dist_int} からの推測)")
                        curr_val = expected
                        dist = str(curr_val)
            self.last_dist_int = curr_val
        else:
            self.last_dist_int = None
        return dist

    def _evaluate_conditions(self, station, dist, speed, pos_green, current_event):
        if not current_event:
            return

        event_type = current_event.get("type")
        event_title = current_event.get("title", f"イベント {self.current_event_idx + 1}")
        now = time.time()
        cooldown_remaining = 60 - (now - self.last_trigger_time)

        if event_type == "distance":
            target_station = current_event.get("station", "")
            target_dist = current_event.get("trigger_distance", "")
            arrive_key = current_event.get("arrive_key", "0")

            if self.is_station_match(station, target_station) and dist == target_dist:
                is_same_station = (target_station != "" and target_station == self.last_triggered_station)
                if cooldown_remaining <= 0 or is_same_station:
                    if self.on_event_triggered:
                        self.on_event_triggered(event_title, arrive_key)
                    self._log(f"[進行] 条件達成: {event_title} -> キー '{arrive_key}' 送信")
                    self.current_event_idx += 1
                    self.last_trigger_time = now
                    self.last_triggered_station = target_station
                else:
                    if int(now) % 5 == 0:
                        self._log(f"[待機] 距離条件一致: {event_title} (クールダウン中: 残り {int(cooldown_remaining)}秒)")

        elif event_type == "door":
            door_key = current_event.get("door_key", "0")
            speed_zero = (speed == "0.0" or speed == "0" or speed == "00")
            if speed_zero and pos_green:
                if self.on_event_triggered:
                    self.on_event_triggered(event_title, door_key)
                self._log(f"[進行] 条件達成: {event_title} (速度={speed}, 位置登=緑) -> キー '{door_key}' 送信")
                self.current_event_idx += 1

    def run(self):
        self._log("=" * 60)
        self._log("自動放送OCRエミュレーター 開始 (シーケンシャル実行版)")
        self._log("=" * 60)

        if not self.load_settings():
            self._log("設定ファイルを正しく読み込めませんでした。終了します。")
            return

        with mss.mss() as sct:
            monitor = sct.monitors[1]

            while not self._stop_event.is_set():
                start_time = time.time()
                try:
                    # 毎ループ設定を再読み込み（動的更新対応）
                    try:
                        with open(self.settings_file, 'r', encoding='utf-8') as f:
                            new_settings = json.load(f)
                        if isinstance(new_settings, list):
                            self.settings = new_settings
                    except Exception:
                        pass

                    station, ocr_dist, speed, pos_green = self._update_screen_state(sct, monitor)
                    dist = self._guess_distance(ocr_dist)

                    if self.current_event_idx < len(self.settings):
                        current_event = self.settings[self.current_event_idx]
                        event_title = current_event.get("title", f"イベント {self.current_event_idx + 1}")
                    else:
                        current_event = None
                        event_title = "全イベント完了（待機中）"

                    self.last_state_str = f"{speed}km/h|{'\u7dd1' if pos_green else '\u9ed2'}"

                    timestamp = datetime.now().strftime('%H:%M:%S')
                    station_pad = (station or "未認識").ljust(8, '　')
                    dist_pad = (dist or "--").ljust(4)
                    self._log(
                        f"[{timestamp}] 次駅: {station_pad} | 距離: {dist_pad} | "
                        f"速度: {speed or '--'}km | 位置登: {'\u7dd1' if pos_green else '\u9ed2'} | 待機: {event_title}"
                    )

                    self._evaluate_conditions(station, dist, speed, pos_green, current_event)

                except Exception as e:
                    self._log(f"[エラー] {e}")

                elapsed = time.time() - start_time
                remaining = max(1.0 - elapsed, 0)
                self._stop_event.wait(timeout=remaining)


# ==========================================
# GUI (PyQt6)
# ==========================================

class MainApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("JRETSAnnounceExpansion")
        self.setWindowIcon(QIcon(ICON_PATH))
        self.setMinimumSize(1000, 700)


        # ── スタイルシート (お手本ベースのブルーテーマ) ──
        self.setStyleSheet("""
            QMainWindow { background-color: #f8fafc; }
            QWidget { font-family: 'Yu Gothic UI', 'Segoe UI', sans-serif; color: #0f172a; }
            QSplitter::handle { background: #e2e8f0; width: 1px; }
            QSplitter::handle:hover { background: #2563eb; }
            
            #HeaderBar { background-color: #ffffff; border-bottom: 1px solid #e2e8f0; }
            QLabel#HeaderTitle { font-size: 18px; font-weight: 800; color: #1e293b; }
            
            QPushButton { border-radius: 8px; padding: 8px 16px; font-weight: 600; font-size: 14px; border: none; }
            QPushButton#BtnPrimary { background-color: #2563eb; color: white; }
            QPushButton#BtnPrimary:hover { background-color: #1d4ed8; }
            QPushButton#BtnPrimary:disabled { background-color: #94a3b8; }
            
            QPushButton#BtnOutline { background-color: #ffffff; border: 1px solid #cbd5e1; color: #475569; }
            QPushButton#BtnOutline:hover { background-color: #f8fafc; border: 1px solid #94a3b8; color: #0f172a; }
            QPushButton#BtnOutline:disabled { color: #cbd5e1; border: 1px solid #f1f5f9; }
            
            QPushButton#BtnDanger { background-color: #fee2e2; color: #ef4444; }
            QPushButton#BtnDanger:hover { background-color: #fecaca; }
            
            #SidebarHeader { padding: 16px 20px; font-weight: 800; font-size: 13px; border-bottom: 1px solid #e2e8f0; background-color: #f8fafc; color: #64748b; letter-spacing: 1px; }
            
            #SidebarList { background-color: #f8fafc; border: none; outline: none; }
            #SidebarList::item { background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; margin: 3px 10px; padding: 10px 14px; font-size: 14px; }
            #SidebarList::item:hover { border: 1px solid #94a3b8; }
            #SidebarList::item:selected { border: 2px solid #2563eb; background-color: #eff6ff; color: #1e40af; font-weight: 700; }
            
            #StatusPanel { background-color: #ffffff; border-left: 1px solid #e2e8f0; }
            QLabel#StatusBig { font-size: 36px; font-weight: 800; color: #0f172a; }
            QLabel#StatusSub { font-size: 18px; font-weight: 700; color: #64748b; }
            QLabel#StatusLabel { font-size: 13px; font-weight: 600; color: #94a3b8; text-transform: uppercase; }
            
            QTextEdit#LogView { background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; font-family: 'Consolas', monospace; font-size: 12px; color: #334155; }
        """)

        self.settings_file = ""
        self.announcer: TrainAutoAnnouncer = None
        self.announcer_thread: threading.Thread = None
        self.log_queue = queue.Queue()
        self.current_idx = 0  # 選択中のインデックスを管理
        self.settings_data = [] # プレビュー用の設定データ

        self._setup_ui()
        
        # ログ監視タイマー (100ms周期)
        self.timer = QTimer()
        self.timer.timeout.connect(self._poll_log_queue)
        self.timer.start(100)

    def _setup_ui(self):
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        # ── 1. ウェルカム画面 (起動画面) ──
        self.page_welcome = QWidget()
        wel_lyt = QVBoxLayout(self.page_welcome)
        wel_lyt.setContentsMargins(40, 40, 40, 40)
        wel_lyt.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # ロゴ画像
        self.lbl_logo_placeholder = QLabel()
        self.lbl_logo_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        if os.path.exists(LOGO_PATH):
            pix = QPixmap(LOGO_PATH)

            self.lbl_logo_placeholder.setPixmap(pix)
            self.lbl_logo_placeholder.setScaledContents(True)
            w_sel = 400
            h_sel = int(w_sel * pix.height() / pix.width())
            self.lbl_logo_placeholder.setFixedSize(w_sel, h_sel)
        else:
            self.lbl_logo_placeholder.setFixedSize(400, 150)
            self.lbl_logo_placeholder.setStyleSheet("background-color: #eff6ff; border: 2px dashed #bfdbfe; border-radius: 20px;")
            self.lbl_logo_placeholder.setText("JRETS\nAnnounce\nExpansion")

        wel_lyt.addWidget(self.lbl_logo_placeholder, 0, Qt.AlignmentFlag.AlignCenter)
        wel_lyt.addSpacing(40)

        wel_msg = QLabel("運行データのJSONファイルを読み込んでください。")
        wel_msg.setStyleSheet("font-size: 20px; font-weight: 700; color: #64748b;")
        wel_lyt.addWidget(wel_msg, 0, Qt.AlignmentFlag.AlignCenter)
        wel_lyt.addSpacing(30)

        self.btn_select_file = QPushButton("JSONデータを選択")
        self.btn_select_file.setObjectName("BtnPrimary")
        self.btn_select_file.setFixedSize(300, 60)
        self.btn_select_file.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_select_file.clicked.connect(self._browse_file)
        wel_lyt.addWidget(self.btn_select_file, 0, Qt.AlignmentFlag.AlignCenter)
        
        self.stack.addWidget(self.page_welcome)

        # ── 2. メイン画面 ──
        self.page_main = QWidget()
        main_lyt = QVBoxLayout(self.page_main)
        main_lyt.setContentsMargins(0, 0, 0, 0)
        main_lyt.setSpacing(0)

        # -- Header --
        header = QFrame()
        header.setObjectName("HeaderBar")
        header.setFixedHeight(70)
        header_lyt = QHBoxLayout(header)
        header_lyt.setContentsMargins(24, 0, 24, 0)

        self.lbl_header_logo = QLabel()
        if os.path.exists(LOGO_PATH):
            pix = QPixmap(LOGO_PATH)

            ratio = self.devicePixelRatioF()
            scaled = pix.scaledToHeight(int(40 * ratio), Qt.TransformationMode.SmoothTransformation)
            scaled.setDevicePixelRatio(ratio)
            self.lbl_header_logo.setPixmap(scaled)
        else:
            self.lbl_header_logo.setFixedSize(40, 40)
            self.lbl_header_logo.setText("JRETS")
            self.lbl_header_logo.setStyleSheet("color: #bfdbfe; font-weight: 800;")
        header_lyt.addWidget(self.lbl_header_logo)


        header_title = QLabel("JRETSAnnounceExpansion")
        header_title.setObjectName("HeaderTitle")
        header_lyt.addWidget(header_title)
        header_lyt.addStretch()


        self.btn_start = QPushButton("▶ 監視開始")
        self.btn_start.setObjectName("BtnPrimary")
        self.btn_start.clicked.connect(self._start_monitoring)
        header_lyt.addWidget(self.btn_start)

        self.btn_stop = QPushButton("■ 停止")
        self.btn_stop.setObjectName("BtnDanger")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_monitoring)
        header_lyt.addSpacing(8)
        header_lyt.addWidget(self.btn_stop)

        self.btn_reload = QPushButton("📁 データ変更")
        self.btn_reload.setObjectName("BtnOutline")
        self.btn_reload.clicked.connect(self._browse_file)
        header_lyt.addSpacing(8)
        header_lyt.addWidget(self.btn_reload)

        main_lyt.addWidget(header)

        # -- Splitter --
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        main_lyt.addWidget(self.splitter, 1)

        # Left: Event List
        left_widget = QFrame()
        left_lyt = QVBoxLayout(left_widget)
        left_lyt.setContentsMargins(0, 0, 0, 0)
        left_lyt.setSpacing(0)
        
        left_header = QLabel("イベントリスト")
        left_header.setObjectName("SidebarHeader")
        left_lyt.addWidget(left_header)
        
        self.event_list = QListWidget()
        self.event_list.setObjectName("SidebarList")
        self.event_list.setFocusPolicy(Qt.FocusPolicy.StrongFocus) # フォーカスを有効化
        self.event_list.itemClicked.connect(self._on_event_clicked) # クリックでジャンプ
        left_lyt.addWidget(self.event_list)
        
        self.splitter.addWidget(left_widget)

        # Center: Log
        center_widget = QFrame()
        center_lyt = QVBoxLayout(center_widget)
        center_lyt.setContentsMargins(16, 16, 16, 16)
        
        log_label = QLabel("実行ログ")
        log_label.setStyleSheet("font-weight: 800; color: #64748b; font-size: 12px; margin-bottom: 4px;")
        center_lyt.addWidget(log_label)
        
        self.log_view = QTextEdit()
        self.log_view.setObjectName("LogView")
        self.log_view.setReadOnly(True)
        center_lyt.addWidget(self.log_view)
        
        self.splitter.addWidget(center_widget)

        # Right: Status Panel
        right_widget = QFrame()
        right_widget.setObjectName("StatusPanel")
        right_lyt = QVBoxLayout(right_widget)
        right_lyt.setContentsMargins(24, 32, 24, 24)
        right_lyt.setSpacing(20)

        # Status Cards
        def create_status_card(label):
            cont = QVBoxLayout()
            cont.setSpacing(4)
            lbl = QLabel(label)
            lbl.setObjectName("StatusLabel")
            val = QLabel("--")
            val.setObjectName("StatusBig")
            cont.addWidget(lbl)
            cont.addWidget(val)
            return cont, val

        # Speed Card
        lyt_speed, self.lbl_val_speed = create_status_card("Current Speed")
        right_lyt.addLayout(lyt_speed)
        
        # Position Card
        lyt_pos, self.lbl_val_pos = create_status_card("Stop Position")
        right_lyt.addLayout(lyt_pos)

        # Next Event Card
        lyt_next, self.lbl_val_next = create_status_card("Next Event")
        self.lbl_val_next.setObjectName("StatusSub")
        right_lyt.addLayout(lyt_next)

        right_lyt.addStretch()

        # Control Buttons
        ctrl_lyt = QHBoxLayout()
        ctrl_lyt.setSpacing(10)
        
        self.btn_back = QPushButton("⏮ 戻る")
        self.btn_back.setObjectName("BtnOutline")
        self.btn_back.clicked.connect(self._back_event)
        
        self.btn_skip = QPushButton("⏭ スキップ")
        self.btn_skip.setObjectName("BtnOutline")
        self.btn_skip.clicked.connect(self._skip_event)

        ctrl_lyt.addWidget(self.btn_back)
        ctrl_lyt.addWidget(self.btn_skip)
        right_lyt.addLayout(ctrl_lyt)

        self.btn_reset = QPushButton("↺ 最初からリセット")
        self.btn_reset.setObjectName("BtnOutline")
        self.btn_reset.clicked.connect(self._reset_index)
        right_lyt.addWidget(self.btn_reset)

        self.splitter.addWidget(right_widget)
        self.splitter.setSizes([250, 450, 300])

        self.stack.addWidget(self.page_main)

    def _browse_file(self):
        # EXE化した際は、一時フォルダ（SCRIPT_DIR）ではなく、EXE本体があるフォルダ（EXE_DIR）を初期表示する
        file_path, _ = QFileDialog.getOpenFileName(
            self, "設定ファイル (JSON) を選択", EXE_DIR, "JSON Files (*.json);;All Files (*)"
        )
        if file_path:
            self.settings_file = file_path
            self._load_and_preview()
            self.stack.setCurrentIndex(1)

    def _load_and_preview(self):
        try:
            with open(self.settings_file, 'r', encoding='utf-8') as f:
                self.settings_data = json.load(f)
            self.event_list.clear()
            for i, ev in enumerate(self.settings_data):
                ev_type = ev.get("type", "?")
                title = ev.get("title", f"イベント {i+1}")
                icon = "📏" if ev_type == "distance" else "🚪"
                self.event_list.addItem(f"{i+1:02d}. {icon} {title}")
            self.current_idx = 0
            self.event_list.setCurrentRow(0)
            self._update_status_display()
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"ファイルの読み込みに失敗しました:\n{e}")

    def _on_announcer_triggered(self, event_title, key):
        """TrainAutoAnnouncerからイベントトリガーを受け取り、キーボード送信を行う"""
        try:
            keyboard.send(key)
        except Exception as e:
            self._append_log(f"[エラー] キー送信に失敗しました: {e}")

    def _start_monitoring(self):
        if not self.settings_file:
            return
        
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_reload.setEnabled(False)
        
        self.announcer = TrainAutoAnnouncer(
            settings_file=self.settings_file, 
            log_queue=self.log_queue,
            on_event_triggered=self._on_announcer_triggered
        )
        self.announcer.current_event_idx = self.current_idx # 現在のUI位置からスタート
        self.announcer_thread = threading.Thread(target=self.announcer.run, daemon=True)
        self.announcer_thread.start()
        
        self._append_log(f"---- 監視開始 (開始位置: {self.current_idx + 1}) ----")

    def _stop_monitoring(self):
        if self.announcer:
            self.announcer.stop()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_reload.setEnabled(True)
        self._append_log("---- 監視停止 ----")

    def _skip_event(self):
        if self.announcer:
            self.announcer.skip_event()
        else:
            if self.current_idx < len(self.settings_data):
                self.current_idx += 1
                self._append_log(f"[-操作-] イベントをスキップしました (現在: {self.current_idx + 1})")
                self._update_status_display()

    def _back_event(self):
        if self.announcer:
            self.announcer.back_event()
        else:
            if self.current_idx > 0:
                self.current_idx -= 1
                self._append_log(f"[-操作-] イベントを1つ戻しました (現在: {self.current_idx + 1})")
                self._update_status_display()

    def _reset_index(self):
        self.current_idx = 0
        if self.announcer:
            self.announcer.current_event_idx = 0
        self._append_log("---- イベント位置をリセットしました ----")
        self._update_status_display()

    def _on_event_clicked(self, item):
        """リスト項目クリック時にそのイベントへジャンプ"""
        row = self.event_list.row(item)
        self.current_idx = row
        if self.announcer:
            self.announcer.current_event_idx = row
        self._append_log(f"[-操作-] 位置を「{item.text()}」に変更しました")
        self._update_status_display()

    def _update_status_display(self):
        """UIのインデックスとステータス表示を更新"""
        if 0 <= self.current_idx < self.event_list.count():
            self.event_list.setCurrentRow(self.current_idx)
        
        if 0 <= self.current_idx < len(self.settings_data):
            self.lbl_val_next.setText(self.settings_data[self.current_idx].get("title", f"Event {self.current_idx+1}"))
        else:
            self.lbl_val_next.setText("All Completed")

    def _poll_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self._append_log(msg)
                
                # インデックス同期
                # インデックス同期
                if self.announcer:
                    self.current_idx = self.announcer.current_event_idx
                    self._update_status_display()
                    
                    # ステータス表示の更新
                    state = self.announcer.last_state_str # speed|color
                    if "|" in state:
                        sp, cl = state.split("|")
                        self.lbl_val_speed.setText(f"{sp}")
                        self.lbl_val_pos.setText(f"{cl}")
                        self.lbl_val_pos.setStyleSheet(f"color: {'#16a34a' if cl == '\u7dd1' else '#ef4444'};")
                        
        except queue.Empty:
            pass

    def _append_log(self, message):
        self.log_view.append(message)
        # スクロールを末尾へ
        self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())

if __name__ == "__main__":
    import sys
    # 高DPI設定 (QApplicationのインスタンス化前に呼ぶ必要がある)
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    
    app = QApplication(sys.argv)
    
    window = MainApp()
    window.showMaximized()
    sys.exit(app.exec())

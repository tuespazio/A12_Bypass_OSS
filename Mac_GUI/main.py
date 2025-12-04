import sys
import os
import platform
import time
import shutil
import traceback
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QLabel, QPushButton, QProgressBar,
    QVBoxLayout, QHBoxLayout, QWidget, QMessageBox, QFrame, QTextEdit
)
from PyQt6.QtGui import QPixmap, QFont, QLinearGradient, QPainter, QColor
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer


def setup_app_environment():
    """Configure environment for .app bundle"""
    try:
        if getattr(sys, 'frozen', False):
            bundle_path = Path(sys.executable).parent.parent.parent
            resources_path = bundle_path / 'Contents' / 'Resources'

            if str(resources_path) not in sys.path:
                sys.path.insert(0, str(resources_path))

            bin_dir = resources_path / 'bin'
            if bin_dir.exists():
                new_path = str(bin_dir) + ':' + os.environ.get('PATH', '')
                os.environ['PATH'] = new_path
                print(f"🔧 Added to PATH: {bin_dir}")

            os.chdir(resources_path)
            print(f"🔧 Working directory: {os.getcwd()}")
            return True
    except Exception as e:
        print(f"⚠️ Environment setup failed: {e}")
    return False


def setup_logging():
    """Setup debug logging"""
    try:
        log_file = Path.home() / "Desktop" / "codex_app.log"
        with open(log_file, 'w') as f:
            f.write(f"=== Codex A12+ Log ===\n")
            f.write(f"Time: {time.ctime()}\n")
            f.write(f"Python: {sys.version}\n")
            f.write(f"Working dir: {os.getcwd()}\n")
            f.write(f"Frozen: {getattr(sys, 'frozen', False)}\n")
            f.write(f"PATH: {os.environ.get('PATH', '')}\n")
    except Exception as e:
        print(f"⚠️ Logging setup failed: {e}")


# Setup environment before imports
setup_app_environment()
setup_logging()

# Import core logic
try:
    from activator import BypassAutomation
    print("✅ activator imported successfully")
except Exception as e:
    print(f"❌ Failed to import activator: {e}")
    traceback.print_exc()
    try:
        app = QApplication(sys.argv)
        QMessageBox.critical(
            None,
            "❌ Import Error",
            f"Failed to import activator.py:\n{str(e)}\n\n"
            f"Current dir: {os.getcwd()}\n"
            f"Python path: {sys.path}"
        )
        sys.exit(1)
    except:
        sys.exit(1)


class WorkerThread(QThread):
    log = pyqtSignal(str)
    finished = pyqtSignal(bool, str)
    device_info = pyqtSignal(str, str, str)
    progress = pyqtSignal(int)

    def run(self):
        try:
            self.log.emit("🔍 Verifying system requirements...")
            automation = BypassAutomation(auto_confirm_guid=True)
            automation.verify_dependencies()

            self.log.emit("📱 Detecting device...")
            automation.detect_device()
            udid = automation.device_info.get('UniqueDeviceID', 'Unknown')
            ios = automation.device_info.get('ProductVersion', 'Unknown')
            product = automation.device_info.get('ProductType', 'Unknown')
            self.device_info.emit(udid, ios, product)

            self.log.emit("🔑 Auto-detecting SystemGroup GUID (up to 5 attempts)...")
            guid = automation.get_guid_auto()
            if not guid:
                self.log.emit("⚠ GUID detection failed — using fallback GUID")
                guid = "00000000-0000-0000-0000-000000000000"
            automation.guid = guid
            self.log.emit(f"✅ Using GUID: {guid}")

            self.log.emit("🌐 Requesting payload URLs from server...")
            prd = automation.device_info['ProductType']
            sn = automation.device_info['SerialNumber']
            stage1_url, stage2_url, stage3_url = automation.get_all_urls_from_server(prd, guid, sn)
            if not all([stage1_url, stage2_url, stage3_url]):
                raise Exception("Server did not return all required URLs")

            self.progress.emit(10)
            self.log.emit("📥 Pre-loading Stage 1...")
            automation.preload_stage("stage1", stage1_url)
            self.progress.emit(20)
            self.log.emit("📥 Pre-loading Stage 2...")
            automation.preload_stage("stage2", stage2_url)
            self.progress.emit(30)

            self.log.emit("💾 Downloading final payload (Stage 3)...")
            local_db = "downloads.28.sqlitedb"
            if os.path.exists(local_db):
                os.remove(local_db)
            if not automation._curl_download(stage3_url, local_db):
                raise Exception("Final payload download failed")

            self.progress.emit(40)
            self.log.emit("🔍 Validating payload database...")
            import sqlite3
            conn = sqlite3.connect(local_db)
            try:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM asset")
                count = cur.fetchone()[0]
                if count == 0:
                    raise Exception("Empty asset table — invalid payload")
                self.log.emit(f"✅ Database valid: {count} assets found")
            finally:
                conn.close()
            self.progress.emit(50)

            self.log.emit("📤 Uploading payload to /Downloads/ via AFC...")
            target = "/Downloads/downloads.28.sqlitedb"
            if automation.afc_mode == "ifuse":
                automation.mount_afc()
                fpath = automation.mount_point + target
                os.makedirs(os.path.dirname(fpath), exist_ok=True)
                if os.path.exists(fpath):
                    os.remove(fpath)
                shutil.copy(local_db, fpath)
                self.log.emit("✅ Uploaded via ifuse")
            else:
                automation._run_cmd(["pymobiledevice3", "afc", "rm", target])
                code, _, err = automation._run_cmd(["pymobiledevice3", "afc", "push", local_db, target])
                if code != 0:
                    raise Exception(f"AFC upload failed: {err}")
                self.log.emit("✅ Uploaded via pymobiledevice3")
            self.progress.emit(65)

            # Cleanup WAL/SHM
            for wal_file in ["/Downloads/downloads.28.sqlitedb-wal", "/Downloads/downloads.28.sqlitedb-shm"]:
                if automation.afc_mode == "ifuse":
                    fpath = automation.mount_point + wal_file
                    if os.path.exists(fpath):
                        try:
                            os.remove(fpath)
                        except:
                            pass
                else:
                    automation._run_cmd(["pymobiledevice3", "afc", "rm", wal_file])
            self.log.emit("🧹 Cleaned up WAL/SHM files")

            # === STAGE 1: FIRST REBOOT + COPY TO /Books/ ===
            self.log.emit("🔄 Stage 1: Rebooting device...")
            if not automation.reboot_device():
                self.log.emit("⚠ First reboot failed — proceeding anyway")

            self.log.emit("⏳ Waiting for iTunesMetadata.plist (max 25s)...")
            if not automation.wait_for_file("/iTunes_Control/iTunes/iTunesMetadata.plist", timeout=25):
                raise Exception("iTunesMetadata.plist not found after reboot")

            self.log.emit("➡ Copying plist to /Books/iTunesMetadata.plist...")
            if not automation.afc_copy(
                "/iTunes_Control/iTunes/iTunesMetadata.plist",
                "/Books/iTunesMetadata.plist"
            ):
                raise Exception("Failed to copy plist to /Books/")
            self.log.emit("✅ Copied to /Books/")
            self.progress.emit(75)

            # === STAGE 2: SECOND REBOOT + COPY BACK ===
            self.log.emit("🔄 Stage 2: Rebooting again to trigger bookassetd...")
            if not automation.reboot_device():
                self.log.emit("⚠ Second reboot failed — proceeding anyway")

            self.log.emit("⏳ Waiting 15s for bookassetd processing...")
            time.sleep(15)

            self.log.emit("⬅ Copying plist back to /iTunes_Control/...")
            if not automation.afc_copy(
                "/Books/iTunesMetadata.plist",
                "/iTunes_Control/iTunes/iTunesMetadata.plist"
            ):
                self.log.emit("⚠ Warning: copy-back failed — activation may still work")

            self.log.emit("⏸ Waiting 20s for MobileActivation sync...")
            time.sleep(20)
            self.progress.emit(90)

            # === FINAL REBOOT ===
            self.log.emit("🔄 Final reboot to commit activation state...")
            if automation.reboot_device():
                self.log.emit("✅ Device rebooted — activation should complete shortly")
            else:
                self.log.emit("⚠ Final reboot failed — activation may still complete in background")

            self.progress.emit(100)
            self.finished.emit(True, "🎉 Activation process completed successfully!\n"
                                    "Device should activate within 1–2 minutes.")

        except Exception as e:
            error_msg = f"❌ {str(e)}"
            print(f"💥 Worker error: {error_msg}")
            traceback.print_exc()
            self.log.emit(error_msg)
            self.finished.emit(False, error_msg)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Codex A12+ — Activation Tool")
        self.setFixedSize(820, 560)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # === iPhone image panel ===
        left = QLabel()
        left.setFixedSize(320, 560)
        left.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Try multiple paths for image
        image_paths = [
            "assets/iphone.png",
            "iphone.png",
            "../Resources/assets/iphone.png",
            str(Path.home() / "Desktop" / "iphone.png")
        ]
        pixmap = None
        for path in image_paths:
            if os.path.exists(path):
                p = QPixmap(path)
                if not p.isNull():
                    pixmap = p
                    print(f"✅ Loaded image: {path}")
                    break

        if pixmap and not pixmap.isNull():
            left.setPixmap(pixmap.scaled(
                280, 560,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            ))
        else:
            left.setText("📱\nCodex A12+\nActivation GUI")
            left.setStyleSheet("color: #e0e0e0; font-size: 16px; font-weight: bold;")
        layout.addWidget(left)

        # === Info & Control Panel ===
        right = QWidget()
        right.setFixedWidth(500)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(40, 40, 40, 30)
        right_layout.setSpacing(14)
        layout.addWidget(right)

        title = QLabel("Codex A12+")
        title.setFont(QFont("SF Pro Display", 28, QFont.Weight.Bold))
        title.setStyleSheet("color: white;")
        right_layout.addWidget(title)

        subtitle = QLabel("Professional iOS Activation Bypass (A12+)")
        subtitle.setFont(QFont("SF Pro Display", 13))
        subtitle.setStyleSheet("color: #aaa;")
        right_layout.addWidget(subtitle)

        # Device info labels
        self.udid_label = QLabel("📱 UDID: —")
        self.ios_label = QLabel("🌐 iOS Version: —")
        self.device_label = QLabel("📱 Device Model: —")
        for lbl in [self.udid_label, self.ios_label, self.device_label]:
            lbl.setFont(QFont("SF Pro", 12))
            lbl.setStyleSheet("color: #ddd;")
            right_layout.addWidget(lbl)

        # Compatibility badge
        compat = QLabel("✅ Compatible: A12, A13, A14, A15, A16, A17 devices")
        compat.setFont(QFont("SF Pro", 11, QFont.Weight.Bold))
        compat.setStyleSheet("color: #00d188;")
        right_layout.addWidget(compat)

        # Log output
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Menlo", 11))
        self.log_view.setStyleSheet("""
            QTextEdit {
                background: #1e293b;
                color: #e2e8f0;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 8px;
            }
        """)
        self.log_view.setFixedHeight(120)
        right_layout.addWidget(self.log_view)

        # Progress & status
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.setStyleSheet("""
            QProgressBar {
                border: none;
                background: #334155;
                border-radius: 3px;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #4ade80, stop:1 #38bdf8);
                border-radius: 3px;
            }
        """)
        right_layout.addWidget(self.progress)

        self.status_label = QLabel("🔌 Connect device and press Start")
        self.status_label.setFont(QFont("SF Pro", 10))
        self.status_label.setStyleSheet("color: #94a3b8;")
        right_layout.addWidget(self.status_label)

        # Start button
        self.start_btn = QPushButton("🚀 Start Full Activation")
        self.start_btn.setFont(QFont("SF Pro", 15, QFont.Weight.Bold))
        self.start_btn.setFixedHeight(52)
        self.start_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #0ea5e9, stop:1 #0284c7);
                color: white;
                border: none;
                border-radius: 12px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #0284c7, stop:1 #0369a1);
            }
            QPushButton:disabled {
                background: #334155;
                color: #64748b;
            }
        """)
        self.start_btn.clicked.connect(self.start_activation)
        right_layout.addWidget(self.start_btn)

        # Settings/about button
        self.about_btn = QPushButton("ℹ️ About")
        self.about_btn.setFixedSize(90, 32)
        self.about_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #94a3b8;
                border: 1px solid #334155;
                border-radius: 6px;
                font-size: 12px;
            }
            QPushButton:hover {
                color: white;
                border-color: #4ade80;
            }
        """)
        self.about_btn.clicked.connect(self.show_about)
        right_layout.addWidget(self.about_btn, alignment=Qt.AlignmentFlag.AlignRight)

        # Auto-refresh timer
        self.check_timer = QTimer()
        self.check_timer.timeout.connect(self.check_device)
        self.check_timer.start(5000)
        QTimer.singleShot(1000, self.check_device)

        self.worker = None
        print("✅ MainWindow initialized")

    def paintEvent(self, event):
        painter = QPainter(self)
        grad = QLinearGradient(0, 0, self.width(), self.height())
        grad.setColorAt(0.0, QColor("#0f172a"))  # slate-900
        grad.setColorAt(0.5, QColor("#1e293b"))  # slate-800
        grad.setColorAt(1.0, QColor("#334155"))  # slate-700
        painter.fillRect(self.rect(), grad)

    def log(self, msg: str):
        self.log_view.append(msg)
        self.log_view.verticalScrollBar().setValue(
            self.log_view.verticalScrollBar().maximum()
        )

    def check_device(self):
        try:
            auto = BypassAutomation(auto_confirm_guid=True)

            # Test ideviceinfo
            code, _, err = auto._run_cmd(["ideviceinfo", "--version"])
            if code != 0:
                self.status_label.setText("❌ ideviceinfo not available")
                self.start_btn.setEnabled(False)
                self.log("❌ ideviceinfo not found or failed")
                return

            # List devices
            code, out, err = auto._run_cmd(["idevice_id", "-l"])
            if code != 0 or not out.strip():
                self.status_label.setText("🔌 No device detected — connect & trust")
                self.start_btn.setEnabled(False)
                self.log("⏳ Waiting for USB device...")
                return

            udid = out.strip().split('\n')[0]
            code, out, _ = auto._run_cmd(["ideviceinfo", "-u", udid])
            if code == 0:
                info = {}
                for line in out.splitlines():
                    if ": " in line:
                        k, v = line.split(": ", 1)
                        info[k.strip()] = v.strip()

                udid_short = info.get('UniqueDeviceID',)[:35] + "..."
                self.udid_label.setText(f"📱 UDID: {udid_short}")
                self.ios_label.setText(f"🌐 iOS: {info.get('ProductVersion', '?')}")
                self.device_label.setText(f"📱 Device: {info.get('ProductType', '?')}")
                self.status_label.setText("✅ Device ready — click Start to begin")
                self.start_btn.setEnabled(True)
                self.log("✅ Device detected and ready")
            else:
                self.status_label.setText("❌ Failed to read device info")
                self.start_btn.setEnabled(False)
                self.log("❌ ideviceinfo command failed")

        except Exception as e:
            self.udid_label.setText("📱 UDID: —")
            self.ios_label.setText("🌐 iOS Version: —")
            self.device_label.setText("📱 Device Model: —")
            self.status_label.setText("❌ Error during device check")
            self.start_btn.setEnabled(False)
            self.log(f"❌ Device check error: {e}")

    def start_activation(self):
        if self.worker and self.worker.isRunning():
            return

        self.log_view.clear()
        self.start_btn.setEnabled(False)
        self.start_btn.setText("⚡ Running Activation...")
        self.progress.setValue(0)
        self.status_label.setText("⚙️ Initializing bypass engine...")

        self.worker = WorkerThread()
        self.worker.log.connect(self.log)
        self.worker.device_info.connect(self.update_device_info)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.finished.connect(self.on_finished)
        self.worker.start()

    def update_device_info(self, udid, ios, product):
        self.udid_label.setText(f"📱 UDID: {udid[:12]}...")
        self.ios_label.setText(f"🌐 iOS: {ios}")
        self.device_label.setText(f"📱 Device: {product}")

    def on_finished(self, success, msg):
        self.start_btn.setEnabled(True)
        self.start_btn.setText("🚀 Start Full Activation")
        if success:
            QMessageBox.information(
                self, "✅ Success",
                "Activation process completed successfully!\n\n"
            )
            self.status_label.setText("✅ Done — activation in progress")
            self.log("🎉 Activation completed successfully")
        else:
            QMessageBox.critical(
                self, "❌ Activation Failed",
                f"Error: {msg}\n\n"
                "Troubleshooting:\n"
                "• Ensure device is trusted and unlocked\n"
                "• Close iTunes/Finder\n"
                "• Use high-quality USB cable\n"
                "• Device must be in normal (not DFU/recovery) mode\n"
                "• Check Desktop/codex_app.log for details"
            )
            self.status_label.setText("❌ Activation failed")
            self.log("❌ Activation failed — see error above")

    def show_about(self):
        QMessageBox.about(
            self, "ℹ️ About Codex A12+",
            "<h3>Codex A12+ — iOS Activation Bypass Tool</h3>"
            "<p><b>Version:</b> 2.1 (GUI — Full Auto)</p>"
            "<p><b>Features:</b></p>"
            "<ul>"
            "<li>✅ Full auto GUID detection (no manual input)</li>"
            "<li>✅ Supports ifuse & pymobiledevice3 backends</li>"
            "<li>✅ A12–A17 device support</li>"
            "<li>✅ Built-in SSL bypass (<code>-k</code>)</li>"
            "</ul>"
            f"<p><b>Bundle Mode:</b> {'Yes' if getattr(sys, 'frozen', False) else 'No'}</p>"
            f"<p><b>Working Dir:</b> {os.getcwd()}</p>"
            "<p><i>For research and educational purposes only.</i></p>"
            "<p>© Codex Team — Developed by Rustam Asadov (Rust505)</p>"
        )


def main():
    try:
        print("🚀 Starting Codex A12+ GUI...")
        Path("assets").mkdir(exist_ok=True)

        app = QApplication(sys.argv)
        if platform.system() == "Darwin":
            app.setStyle("macos")
            app.setFont(QFont("SF Pro Display", 13))

        window = MainWindow()
        window.show()
        print("✅ Application started")
        return app.exec()

    except Exception as e:
        print(f"💥 Fatal startup error: {e}")
        traceback.print_exc()
        try:
            app = QApplication(sys.argv)
            QMessageBox.critical(
                None,
                "❌ Startup Error",
                f"Codex A12+ failed to start:\n{str(e)}\n\n"
                "Check Desktop/codex_app.log for diagnostics."
            )
        except:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
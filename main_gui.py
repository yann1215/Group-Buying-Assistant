from __future__ import annotations

# 必须尽量放在入口文件最前面，
# 并且放在 PySide6、ChatService 等重量级模块导入之前。
if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()


import html
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from threading import Event

from PySide6.QtWidgets import QInputDialog, QLineEdit

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent, QKeyEvent, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)


def get_runtime_dir() -> Path:
    """
    源码运行时：项目根目录。
    PyInstaller 打包后：exe 所在目录。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


# 让“./orders/xxx.xlsx”等相对路径始终相对于项目根目录或 exe 所在目录。
RUNTIME_DIR = get_runtime_dir()
os.chdir(RUNTIME_DIR)

from app.core.chat_service import ChatService
from app.database.db import init_db
from app.database.repositories import create_session


@dataclass
class KeyInputRequest:
    prompt: str
    value: str = ""
    finished: Event = field(default_factory=Event)


class GuiKeyInputBridge(QObject):
    """
    把工作线程中的同步 key 输入请求，
    转换成 GUI 主线程中的 QInputDialog。
    """

    request_input = Signal(object)

    def __init__(self) -> None:
        super().__init__()

        # QueuedConnection 保证弹窗方法在 GUI 主线程执行
        self.request_input.connect(
            self._show_key_dialog,
            Qt.QueuedConnection,
        )

    def __call__(self, prompt: str) -> str:
        """
        使当前对象能够像函数一样调用：

        key = bridge("微信数据库 key：")
        """
        request = KeyInputRequest(prompt=prompt)

        # 从工作线程向 GUI 主线程发送弹窗请求
        self.request_input.emit(request)

        # 工作线程在这里等待，不会阻塞 GUI 主线程
        request.finished.wait()

        return request.value

    @Slot(object)
    def _show_key_dialog(self, request: KeyInputRequest) -> None:
        try:
            text, accepted = QInputDialog.getText(
                None,
                "输入微信数据库 Key",
                (
                    f"{request.prompt}\n\n"
                    "请输入64位十六进制数据库 Key。\n"
                    "也可以输入 auto 重新自动识别。"
                ),
                QLineEdit.Normal,
            )

            if accepted:
                request.value = text.strip()
            else:
                # 用户点击取消时，模拟输入 quit
                request.value = "quit"

        finally:
            # 无论正常输入还是异常，都必须解除工作线程等待
            request.finished.set()


class ChatInput(QPlainTextEdit):
    """Enter 发送，Shift+Enter 换行。"""

    submitted = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if not (event.modifiers() & Qt.ShiftModifier):
                event.accept()
                self.submitted.emit()
                return
        super().keyPressEvent(event)


class WorkerSignals(QObject):
    result = Signal(str)
    error = Signal(str)
    finished = Signal()


class SendMessageWorker(QRunnable):
    """在线程池中调用后端，避免 GUI 在解密或计算时卡死。"""

    def __init__(
        self,
        chat_service: ChatService,
        session_id: int,
        user_text: str,
    ) -> None:
        super().__init__()
        self.chat_service = chat_service
        self.session_id = session_id
        self.user_text = user_text
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            reply = self.chat_service.send_message(
                self.session_id,
                self.user_text,
            )
            self.signals.result.emit(str(reply))
        except Exception as exc:
            self._write_error_log(exc)
            self.signals.error.emit(f"{type(exc).__name__}: {exc}")
        finally:
            self.signals.finished.emit()

    @staticmethod
    def _write_error_log(exc: Exception) -> None:
        log_dir = RUNTIME_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "gui_error.log"

        with log_path.open("a", encoding="utf-8") as file:
            file.write("\n" + "=" * 80 + "\n")
            file.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "\n")
            file.write(f"{type(exc).__name__}: {exc}\n")
            file.write(traceback.format_exc())


class ChatWindow(QMainWindow):
    def __init__(
        self,
        chat_service: ChatService,
        session_id: int,
    ) -> None:
        super().__init__()
        self.chat_service = chat_service
        self.session_id = session_id
        self.thread_pool = QThreadPool.globalInstance()
        self.is_processing = False

        # 必须保存 worker 引用，防止任务结束前被 Python 回收
        self.current_worker: SendMessageWorker | None = None

        self.setWindowTitle("拼团辅助机器人")
        self.resize(860, 680)
        self.setMinimumSize(680, 500)

        self._build_ui()
        self._apply_style()

        self.append_message(
            "assistant",
            (
                "已启动。你可以直接输入命令，例如：\n"
                "群聊名称：XXX\n"
                "订单文件：订单1.xlsx\n"
                "查成员\n"
                "算均摊\n"
                "算大货"
            ),
        )
        self.input_box.setFocus()

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)

        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(18, 18, 18, 18)
        main_layout.setSpacing(12)

        self.chat_view = QTextBrowser()
        self.chat_view.setObjectName("chatView")
        self.chat_view.setReadOnly(True)
        self.chat_view.setOpenExternalLinks(False)
        self.chat_view.document().setDocumentMargin(14)
        main_layout.addWidget(self.chat_view, 1)

        input_frame = QFrame()
        input_frame.setObjectName("inputFrame")

        input_layout = QHBoxLayout(input_frame)
        input_layout.setContentsMargins(10, 10, 10, 10)
        input_layout.setSpacing(10)

        self.input_box = ChatInput()
        self.input_box.setObjectName("inputBox")
        self.input_box.setPlaceholderText(
            "输入消息；Enter 发送，Shift+Enter 换行"
        )
        self.input_box.setMinimumHeight(72)
        self.input_box.setMaximumHeight(125)
        self.input_box.submitted.connect(self.send_current_message)
        input_layout.addWidget(self.input_box, 1)

        self.send_button = QPushButton("发送")
        self.send_button.setObjectName("sendButton")
        self.send_button.setMinimumSize(88, 72)
        self.send_button.clicked.connect(self.send_current_message)
        input_layout.addWidget(self.send_button)

        main_layout.addWidget(input_frame)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background-color: #EEF1EF;
                color: #26322C;
                font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
                font-size: 14px;
            }

            QTextBrowser#chatView {
                background-color: #F8FAF9;
                border: 1px solid #CDD5D0;
                border-radius: 12px;
                padding: 4px;
                selection-background-color: #8FC9A8;
            }

            QFrame#inputFrame {
                background-color: #FFFFFF;
                border: 1px solid #CDD5D0;
                border-radius: 12px;
            }

            QPlainTextEdit#inputBox {
                background-color: #FFFFFF;
                border: none;
                padding: 8px;
                color: #26322C;
                selection-background-color: #8FC9A8;
            }

            QPushButton#sendButton {
                background-color: #2F8F5B;
                color: #FFFFFF;
                border: none;
                border-radius: 9px;
                font-weight: 600;
                padding: 10px 18px;
            }

            QPushButton#sendButton:hover {
                background-color: #287C4F;
            }

            QPushButton#sendButton:pressed {
                background-color: #216842;
            }

            QPushButton#sendButton:disabled {
                background-color: #AAB6AF;
                color: #F2F4F3;
            }
            """
        )

    def send_current_message(self) -> None:
        if self.is_processing:
            return

        user_text = self.input_box.toPlainText().strip()
        if not user_text:
            return

        self.append_message("user", user_text)
        self.input_box.clear()
        self.set_processing(True)

        self.current_worker = SendMessageWorker(
            chat_service=self.chat_service,
            session_id=self.session_id,
            user_text=user_text,
        )

        self.current_worker.signals.result.connect(self.handle_reply)
        self.current_worker.signals.error.connect(self.handle_error)
        self.current_worker.signals.finished.connect(
            self.handle_worker_finished
        )

        self.thread_pool.start(self.current_worker)

    @Slot(str)
    def handle_reply(self, reply: str) -> None:
        try:
            self.append_message("assistant", reply)
        finally:
            self.set_processing(False)

    @Slot(str)
    def handle_error(self, error_text: str) -> None:
        try:
            self.append_message(
                "error",
                (
                    f"出错：{error_text}\n"
                    f"详细错误已写入："
                    f"{RUNTIME_DIR / 'logs' / 'gui_error.log'}"
                ),
            )
        finally:
            self.set_processing(False)

    @Slot()
    def handle_worker_finished(self) -> None:
        """
        后台任务彻底结束后，恢复输入框和发送按钮。
        """
        self.set_processing(False)
        self.current_worker = None

    def set_processing(self, processing: bool) -> None:
        self.is_processing = processing
        self.input_box.setEnabled(not processing)
        self.send_button.setEnabled(not processing)
        self.send_button.setText("处理中…" if processing else "发送")

        if not processing:
            self.input_box.setFocus()

    def append_message(self, role: str, text: str) -> None:
        safe_text = html.escape(text).replace("\n", "<br>")

        if role == "user":
            align = "right"
            title = "你"
            background = "#DDF1E5"
            border = "#A9D4BA"
            text_color = "#20362A"
        elif role == "error":
            align = "left"
            title = "错误"
            background = "#ECEFED"
            border = "#B5BDB8"
            text_color = "#5B2F2F"
        else:
            align = "left"
            title = "小助手"
            background = "#FFFFFF"
            border = "#D5DCD8"
            text_color = "#26322C"

        message_html = f"""
        <table width="100%" cellspacing="0" cellpadding="0">
          <tr>
            <td align="{align}">
              <table cellspacing="0" cellpadding="9"
                     style="background-color:{background};
                            border:1px solid {border};">
                <tr>
                  <td>
                    <span style="font-size:11px; color:#6F7B74;">{title}</span>
                    <br>
                    <span style="font-size:14px; color:{text_color};">{safe_text}</span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
        <div style="height:8px;"></div>
        """

        self.chat_view.moveCursor(QTextCursor.End)
        self.chat_view.insertHtml(message_html)
        self.chat_view.moveCursor(QTextCursor.End)
        self.chat_view.ensureCursorVisible()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.is_processing:
            answer = QMessageBox.question(
                self,
                "任务仍在运行",
                "当前任务尚未结束，确定要关闭程序吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("拼团辅助机器人")
    app.setStyle("Fusion")

    try:
        init_db()
        session_title = datetime.now().strftime("GUI 会话 %Y-%m-%d %H:%M:%S")
        session_id = create_session(session_title)

        key_input_bridge = GuiKeyInputBridge()

        chat_service = ChatService(
            key_input_func=key_input_bridge,
        )
    except Exception as exc:
        QMessageBox.critical(
            None,
            "启动失败",
            f"{type(exc).__name__}: {exc}",
        )
        return 1

    window = ChatWindow(
        chat_service=chat_service,
        session_id=session_id,
    )
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

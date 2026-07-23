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
    QListWidget,
    QListWidgetItem,
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
        self._updating_session_list = False

        # 必须保存 worker 引用，防止任务结束前被 Python 回收
        self.current_worker: SendMessageWorker | None = None

        self.setWindowTitle("拼团辅助机器人")
        self.resize(1080, 700)
        self.setMinimumSize(820, 520)

        self._build_ui()
        self._apply_style()
        self.load_session(session_id)
        self.input_box.setFocus()

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)

        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setMinimumWidth(190)
        sidebar.setMaximumWidth(260)

        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(12, 16, 12, 16)
        sidebar_layout.setSpacing(10)

        self.new_session_button = QPushButton("＋ 新对话")
        self.new_session_button.setObjectName("newSessionButton")
        self.new_session_button.clicked.connect(self.create_new_session)
        sidebar_layout.addWidget(self.new_session_button)

        self.session_list = QListWidget()
        self.session_list.setObjectName("sessionList")
        self.session_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAlwaysOff
        )
        self.session_list.currentItemChanged.connect(
            self.handle_session_selected
        )
        sidebar_layout.addWidget(self.session_list, 1)

        self.delete_session_button = QPushButton("删除当前对话")
        self.delete_session_button.setObjectName("deleteSessionButton")
        self.delete_session_button.clicked.connect(
            self.delete_current_session
        )
        sidebar_layout.addWidget(self.delete_session_button)

        root_layout.addWidget(sidebar)

        chat_panel = QWidget()
        chat_panel.setObjectName("chatPanel")
        main_layout = QVBoxLayout(chat_panel)
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
            "输入消息，例如：订单：订单1；Enter 发送，Shift+Enter 换行"
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
        root_layout.addWidget(chat_panel, 1)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background-color: #EEF1EF;
                color: #26322C;
                font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
                font-size: 14px;
            }

            QFrame#sidebar {
                background-color: #E2E8E4;
                border-right: 1px solid #C8D1CB;
            }

            QListWidget#sessionList {
                background-color: transparent;
                border: none;
                outline: none;
                padding: 2px;
            }

            QListWidget#sessionList::item {
                color: #34433B;
                border-radius: 8px;
                padding: 10px 9px;
                margin: 2px 0;
            }

            QListWidget#sessionList::item:hover {
                background-color: #D3DED7;
            }

            QListWidget#sessionList::item:selected {
                background-color: #BFD8C9;
                color: #1E4B33;
                font-weight: 600;
            }

            QPushButton#newSessionButton {
                background-color: #2F8F5B;
                color: #FFFFFF;
                border: none;
                border-radius: 8px;
                font-weight: 600;
                padding: 10px;
            }

            QPushButton#newSessionButton:hover {
                background-color: #287C4F;
            }

            QPushButton#deleteSessionButton {
                background-color: transparent;
                color: #6B3D3D;
                border: 1px solid #C9B5B5;
                border-radius: 8px;
                padding: 8px;
            }

            QPushButton#deleteSessionButton:hover {
                background-color: #E8DADA;
            }

            QPushButton#newSessionButton:disabled,
            QPushButton#deleteSessionButton:disabled {
                background-color: #C3CCC6;
                color: #7B867F;
                border-color: #C3CCC6;
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

    def refresh_session_list(
        self,
        selected_session_id: int | None = None,
    ) -> None:
        sessions = self.chat_service.list_conversations()

        self._updating_session_list = True
        self.session_list.blockSignals(True)
        try:
            self.session_list.clear()
            selected_item: QListWidgetItem | None = None

            for session in sessions:
                title = str(session.get("title") or "新对话")
                item = QListWidgetItem(title)
                item.setData(
                    Qt.ItemDataRole.UserRole,
                    int(session["id"]),
                )
                item.setToolTip(
                    f"群聊：{session.get('group_name') or '未设置'}\n"
                    f"更新时间：{session.get('updated_at') or ''}"
                )
                self.session_list.addItem(item)

                if int(session["id"]) == selected_session_id:
                    selected_item = item

            if selected_item is not None:
                self.session_list.setCurrentItem(selected_item)
            elif self.session_list.count() > 0:
                self.session_list.setCurrentRow(0)
        finally:
            self.session_list.blockSignals(False)
            self._updating_session_list = False

    def load_session(self, session_id: int) -> None:
        messages = self.chat_service.load_conversation(session_id)
        self.session_id = session_id
        self.chat_view.clear()

        if messages:
            for message in messages:
                self.append_message(
                    str(message.get("role") or "assistant"),
                    str(message.get("content") or ""),
                )
        else:
            self.append_message("assistant", self._welcome_text())

        self.refresh_session_list(session_id)
        self.input_box.setFocus()

    @Slot(object, object)
    def handle_session_selected(
        self,
        current: QListWidgetItem | None,
        previous: QListWidgetItem | None,
    ) -> None:
        if self._updating_session_list or current is None:
            return

        selected_id = current.data(Qt.ItemDataRole.UserRole)
        if selected_id is None or int(selected_id) == self.session_id:
            return

        if self.is_processing:
            self.refresh_session_list(self.session_id)
            return

        try:
            self.load_session(int(selected_id))
        except Exception as exc:
            QMessageBox.critical(
                self,
                "切换对话失败",
                f"{type(exc).__name__}: {exc}",
            )
            self.refresh_session_list(self.session_id)

    @Slot()
    def create_new_session(self) -> None:
        if self.is_processing:
            return

        try:
            session_id = self.chat_service.create_conversation()
            self.load_session(session_id)
        except Exception as exc:
            QMessageBox.critical(
                self,
                "新建对话失败",
                f"{type(exc).__name__}: {exc}",
            )

    @Slot()
    def delete_current_session(self) -> None:
        if self.is_processing:
            return

        answer = QMessageBox.question(
            self,
            "删除对话",
            "确定删除当前对话及其聊天记录吗？\n"
            "已经生成的 Excel、CSV 等文件不会被删除。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        try:
            self.chat_service.delete_conversation(self.session_id)
            sessions = self.chat_service.list_conversations()

            if sessions:
                next_session_id = int(sessions[0]["id"])
            else:
                next_session_id = (
                    self.chat_service.create_conversation()
                )

            self.load_session(next_session_id)
        except Exception as exc:
            QMessageBox.critical(
                self,
                "删除对话失败",
                f"{type(exc).__name__}: {exc}",
            )

    @staticmethod
    def _welcome_text() -> str:
        return (
            "已启动。请先录入必填信息：\n"
            "  群聊名称 XXX\n"
            "  订单 XXX\n"
            "  车主 XXX\n\n"
            
            "同时支持选填信息：\n"
            "  工具人 XXX\n"
            "  供稿人 XXX\n"
            "  画师 XXX\n"
            "  章稿画师 XXX\n"
            "  （车主等特殊成员默认不参与均摊计算）\n\n"
            
            "信息录入完成后，支持以下指令：\n"
            "  查成员\n"
            "  算均摊\n"
            "  算大货\n\n"
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
            self.refresh_session_list(self.session_id)
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
            self.refresh_session_list(self.session_id)
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
        self.session_list.setEnabled(not processing)
        self.new_session_button.setEnabled(not processing)
        self.delete_session_button.setEnabled(not processing)
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
            <td align="{align}" style="padding: 4px 0;">
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

        key_input_bridge = GuiKeyInputBridge()

        chat_service = ChatService(
            key_input_func=key_input_bridge,
        )

        sessions = chat_service.list_conversations()
        if sessions:
            session_id = int(sessions[0]["id"])
        else:
            session_id = chat_service.create_conversation()
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
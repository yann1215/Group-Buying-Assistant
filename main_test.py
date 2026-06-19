# main_test.py

from app.database.db import init_db
from app.database.repositories import create_session, get_messages
from app.core.chat_service import ChatService


def main():
    init_db()

    session_id = create_session("测试会话")
    chat_service = ChatService()

    username = "Yann"
    ai_nickname = "Assistant"

    print(f"当前 session_id = {session_id}")
    print("输入 exit / quit / q 退出。")
    print("")
    print("你可以直接在对话中设置上下文，例如：")
    print("  群聊名称：XXX")
    print(r"  订单文件：D:\orders\当前订单.xlsx")
    print(r"  输出目录：D:\orders\output")
    print("")
    print("也可以一句话输入：")
    print(r"  群聊名称：XXX，订单文件：.\orders\当前订单.xlsx，输出目录：.\orders\output")
    print("")
    print("均摊示例：")
    print("  算均摊")
    print("  拉通人头，金额120")
    print("  金额120，按人头拉通")
    print("  按个数拉通，金额100")
    print("  按人头独立")
    print("")

    while True:
        user_text = input(f"\n{username}: ").strip()

        if user_text.lower() in ("exit", "quit", "q"):
            break

        if not user_text:
            continue

        try:
            reply = chat_service.send_message(session_id, user_text)
            print(f"\n{ai_nickname}: ")
            print(reply)
        except Exception as e:
            print(f"\n出错：{e}")

    print("\n本次会话记录：")
    for msg in get_messages(session_id):
        print(f"[{msg['role']}] {msg['content']}")


if __name__ == "__main__":
    main()
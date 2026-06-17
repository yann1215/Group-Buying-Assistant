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

    group_name = input("请输入待处理群聊名称：").strip()
    order_file = input("请输入当前订单文件路径：").strip()
    output_dir = input("请输入输出目录，可留空：").strip()

    chat_service.set_working_context(
        session_id=session_id,
        group_name=group_name,
        order_input=order_file,
        order_output_dir=output_dir or None,
    )

    print(f"当前 session_id = {session_id}")
    print("输入 exit 退出。")

    print("示例：")
    print("  核对成员")
    print("  计算均摊，金额 120，按人头摊")
    print("  计算均摊，金额 120，按个数摊")

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
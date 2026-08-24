cd /d D:\1_PychamProjects\Group-Buying-Assistant

git submodule update --init --recursive

.\.venv\Scripts\activate

python -m pip install -r requirements.txt

python main_gui.py

rmdir /s /q build
rmdir /s /q dist

python -m PyInstaller --noconfirm --clean group_buying_assistant.spec

if not exist "dist\GroupBuyingAssistant\orders" mkdir "dist\GroupBuyingAssistant\orders"
if not exist "dist\GroupBuyingAssistant\orders\output" mkdir "dist\GroupBuyingAssistant\orders\output"
if not exist "dist\GroupBuyingAssistant\temp" mkdir "dist\GroupBuyingAssistant\temp"
if not exist "dist\GroupBuyingAssistant\logs" mkdir "dist\GroupBuyingAssistant\logs"
if not exist "dist\GroupBuyingAssistant\data" mkdir "dist\GroupBuyingAssistant\data"

dist\GroupBuyingAssistant\GroupBuyingAssistant.exe
@echo off
rem 中英翻譯 - Windows exe 建置腳本
rem 用法：在專案根目錄執行  build_exe.bat
cd /d %~dp0

if not exist .venv\Scripts\python.exe (
    echo [錯誤] 找不到 .venv，請先建立虛擬環境並安裝 agent\requirements.txt
    pause
    exit /b 1
)

call .venv\Scripts\activate
pip install pyinstaller==6.22.3

pyinstaller --noconfirm --clean agent\app.spec

if exist dist\FormosalabTranslator\FormosalabTranslator.exe (
    echo.
    echo [完成] 輸出在 dist\FormosalabTranslator\
    echo 執行方式：dist\FormosalabTranslator\FormosalabTranslator.exe
    echo 注意：整個 FormosalabTranslator 資料夾都要一起帶走，不能只複製 exe
) else (
    echo [錯誤] 建置失敗，請把上面的錯誤訊息回報給開發協助
)
pause

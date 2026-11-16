# Hybrid Translation Agent (Phase 1)

en ↔ zh-TW 混合式翻譯 Agent，線上主線為 LLM API（預設 Groq，可切換 Mistral）。本目錄獨立於 `libretranslate/`，不修改上游程式碼。

## 安裝（Windows 原生 Python 3.11）

```bat
cd LibreTranslate
python -m venv .venv
.venv\Scripts\activate
pip install -r agent/requirements.txt
copy agent\.env.example .env
```

編輯 `.env`，填入 `LLM_API_KEY`。

## API Key 取得

- **Groq（免費，預設）**：https://console.groq.com → API Keys → Create API Key
- **Gemini（備援引擎，免費）**：https://aistudio.google.com → Get API key（填入 `.env` 的 `LLM_API_KEY_GEMINI`）
- **Mistral（備用）**：https://console.mistral.ai → API Keys（需付費方案才能啟用）

切換供應商：改 `.env` 的 `LLM_PROVIDER`（`groq` 或 `mistral`）與 `LLM_MODEL`，程式碼不用動。

## 啟動

```bat
python -m agent.main
```

服務會跑在 `http://127.0.0.1:8000`，API 文件在 `http://127.0.0.1:8000/docs`。

## 使用

```bash
curl -X POST http://127.0.0.1:8000/translate \
  -H "Content-Type: application/json" \
  -d '{"text": "Please restart the server after updating the config.", "target_lang": "zh-TW"}'
```

回應：

```json
{
  "translated_text": "更新設定檔後請重新啟動伺服器。",
  "source": "cloud",
  "model": "openai/gpt-oss-120b",
  "source_lang": "en",
  "target_lang": "zh-TW",
  "elapsed_ms": 1234
}
```

## 雙引擎自動備援（Phase 2b）

在 `.env` 填入 `LLM_API_KEY_GEMINI`（Gemini 免費金鎖，取得方式見上）即可啟用：

- 主要引擎（Groq）失敗時（429 額度用完、5xx、斷網等），自動改用備援引擎（Gemini），結果標示 `cloud_backup`（綠色標籤）
- 備援也失敗時，才降到離線 Argos（`offline_fallback`）
- 備援引擎的結果同樣寫入快取，之後相同句子直接從快取回應，不消耗任何額度
- 未填 `LLM_API_KEY_GEMINI` 時功能自動停用，行為與原本完全相同

可用 `LLM_BACKUP_PROVIDER` / `LLM_BACKUP_MODEL` / `LLM_BACKUP_BASE_URL` 換掉備援引擎（預設 `gemini` / `gemini-2.5-flash`，走 Gemini 的 OpenAI 相容端點）。

## 語音翻譯（Phase 2a）

首頁下方「語音翻譯」區塊有兩種用法：

1. **🎤 按住說話**：按住按鈕說話，放開後自動辨識並翻譯（瀏覽器錄音，webm 格式）
2. **上傳語音檔並翻譯**：選擇語音檔（mp3 / wav / m4a / webm / ogg / flac / mp4 / opus，上限 25MB）後上傳，適合拿 Teams 錄音或任何音訊檔測試

語言對沿用頁面上方的語言選擇器（預設自動偵測 → 繁體中文）。流程：**Whisper 語音轉文字 → 現有翻譯管線（快取 → Groq → Gemini 備援 → 離線）**。

API：

```bat
rem 只辨識不翻譯（測試用）
curl -X POST http://127.0.0.1:8000/transcribe -F "file=@meeting.mp3"

rem 辨識 + 翻譯
curl -X POST http://127.0.0.1:8000/translate/voice -F "file=@meeting.mp3" -F "source_lang=en" -F "target_lang=zh-TW"
```

注意：

- 語音辨識使用 Groq Whisper（`whisper-large-v3-turbo`），與文字翻譯共用 `LLM_API_KEY`（也可用 `STT_API_KEY` 指定另一把金鎖）
- Groq 免費方案 Whisper 額度：每次請求**最低計費 10 秒**音訊、每日 8 小時音訊——短句建議累積後再送（後續即時字幕模式會自動處理）
- 設 `VOICE_ENABLED=0` 可停用語音功能

## 即時字幕模式（Phase 2c）

開會或看影片時，把**電腦播出的英文聲音**即時翻成中文字幕：

1. 點首頁語音區塊的「📺 即時字幕（擷取電腦聲音）」
2. 桌面模式會跳出一個**永遠在最上層的半透明小視窗**，把它拖到 Teams 畫面角落
3. 播放英文聲音（Teams 對方說話、YT 影片），偵測到說話時每 4–8 秒自動出一段字幕
4. 按「停止並關閉」結束

原理：Windows WASAPI loopback 直接讀取音效卡正在播的聲音（**戴耳機也擷得到**，不用外放、不用裝虛擬音效卡）→ 能量式 VAD 過濾靜音 → 累積 4–8 秒段落（Whisper 每次請求最低計費 10 秒，短段同價）→ Whisper 轉文字 → 現有翻譯管線。

注意：

- 擷取的是**整台電腦的系統聲音**——翻譯進行中請只開一個音源（例如只開 Teams），否則多個聲音會混在一起
- 你的麥克風（自己說的話）不會被擷取；需要翻譯自己說的話請用「🎤 按住說話」
- 僅支援 Windows（PyAudioWPatch）；瀏覽器網頁模式也能用，但字幕會開在新分頁而非浮動視窗
- 設 `LIVE_ENABLED=0` 可停用；`LIVE_MIN_SEGMENT` / `LIVE_MAX_SEGMENT` 可調段落長度（預設 4 / 8 秒）
- 字幕都沒出現時（通常是系統音量較小、語音被靜音偵測誤判）：調低 `.env` 的 `LIVE_VAD_MULTIPLIER`（預設 2.0，可試 1.5），或設 `LIVE_VAD=0` 完全關閉靜音偵測（每 8 秒固定送一段，較耗額度但保證有字幕）
- 終端機 log 的 `level X / threshold Y` 可看出目前音量與門檻的相對關係（level 長期低於 threshold = 門檻太高）

## 離線模式（Phase 2b）

首次使用前，先下載離線翻譯模型（約 300MB，只需一次，需連網）：

```bat
python -m agent.download_models
```

下載後即具備完整斷網能力（離線翻譯全程不上網：Argos 的神經斷句已被替換為本地標點規則斷句，不會有任何隱形下載）：

- 有網路：Groq/Mistral LLM 翻譯（`cloud`）
- API 失敗或斷網：自動切換 Argos 離線 NMT + OpenCC 簡轉繁（台灣用語）+ 術語表校正（`offline_fallback`，紅色標籤）
- 曾翻過的句子：SQLite 快取直接回應（`cache`，綠色標籤）

離線結果不寫入快取，避免離線品質污染之後的線上結果。

## 打包成 exe（Phase 4）

在 Windows 上於專案根目錄執行：

```bat
build_exe.bat
```

- 腳本會自動安裝 PyInstaller 並依 `agent\app.spec` 建置
- 輸出：`dist\FormosalabTranslator\FormosalabTranslator.exe`（onedir 模式，**整個資料夾都要一起帶走**）
- exe 會從 **exe 同層目錄**讀取 `.env`（LLM_API_KEY 等），快取資料庫存在 exe 旁的 `data\cache.db`
- 首次使用仍需連網下載離線翻譯模型（`python -m agent.download_models`）與 PDF 版面模型/字型
- 備援：若 WebView2 視窗有問題，可執行 exe 時設 `AGENT_HEADLESS=1` 改用純伺服器模式（用瀏覽器開啟主控台顯示的網址）

## 桌面模式（Phase 2c）

用原生視窗執行（不用開瀏覽器）：

```bat
python -m agent.desktop
```

- 啟動時自動挑選空閒的 port，不會跟其他程式衝突
- 視窗關閉 = 程式結束
- PDF 翻譯完成後會跳出**「另存新檔」對話框**，選擇儲存位置即可（網頁模式則是下載連結）
- Windows 使用內建的 WebView2（Edge）引擎；若視窗一片空白，請安裝 [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)
- 純網頁模式仍然可用：`python -m agent.main`

## PDF 翻譯（Phase 3）

開啟首頁下方的「PDF 翻譯」區塊：

1. 選擇 PDF 檔案（目前僅支援**文字型 PDF**，掃描/圖片型不支援）
2. 頁數欄位可留空（全部頁），或輸入 `2`、`2-3`、`1,3-4` 等指定頁數
3. 選擇輸出模式：**原文/譯文對照**（灰色原文 + 黑色譯文）或**純譯文（全文翻譯）**
4. 點「上傳並翻譯」，完成後點連結下載 `translated.pdf`

也可以直接呼叫 API：

```bat
curl -X POST http://127.0.0.1:8000/translate/pdf -F "file=@doc.pdf" -F "pages=2-3" -F "mode=compare"
```

PDF 每個段落都會經過與文字翻譯相同的管線（快取 → LLM → 離線備援），段落多時可能需要數分鐘。

### 格式保持模式（mode=format）

選「格式保持（原地翻譯）」時，改用 [PDFMathTranslate](https://github.com/PDFMathTranslate/PDFMathTranslate) 引擎：在原始版面位置直接以譯文覆蓋原文，版面、圖片、表格位置盡量不變。

注意：

- **首次使用需要連網**下載版面辨識模型（約 40MB）與中文字型（約 20MB），之後離線可跑版面分析，但翻譯仍走快取 → LLM → 離線備援管線
- Windows 需安裝 [Microsoft Visual C++ Redistributable](https://aka.ms/vs/17/release/vc_redist.x64.exe)（onnxruntime 依賴）
- 個別段落翻譯失敗時會保留原文，不會中斷整份文件
- **適合純文字文件**：向量圖/表格內的文字會被版面模型判定為圖形保留區而不翻譯（寧可不翻，也不破壞數學式或特殊符號）
- **自動驗證降級**：翻譯完成後逐頁比對圖片數、向量圖形數與文字存在性，若某頁內容流失（圖片消失、圖形殘缺），該頁自動改用原文/譯文對照模式輸出，確保不會交付破損的 PDF

## 測試

```bat
pytest tests/agent -v -o addopts=""
```

## Phase 1 內容

- FastAPI 服務（`/translate`、`/health`）
- LLM 整合（`llm_client.py`，OpenAI 相容格式，Groq / Mistral 可切換）：短文 8 秒 timeout、長文依 token 動態 timeout、429/5xx 自動重試兩次、其他 4xx 快速失敗
- 台灣繁中 Prompt 模板 v1（`prompt.py`）
- 程式碼保護器（`code_protector.py`）：fenced block、inline code、HTML 標籤先抽出為佔位符，譯後還原並驗證完整性
- `source` 欄位標示結果來源（Phase 1 僅 `cloud` / `passthrough`，Phase 2 加入 `cache` / `offline_fallback`）

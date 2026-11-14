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

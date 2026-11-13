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

# Hybrid Translation Agent (Phase 1)

en ↔ zh-TW 混合式翻譯 Agent，線上主線為 Mistral API。本目錄獨立於 `libretranslate/`，不修改上游程式碼。

## 安裝（Windows 原生 Python 3.11）

```bat
cd LibreTranslate
python -m venv .venv
.venv\Scripts\activate
pip install -r agent/requirements.txt
copy agent\.env.example .env
```

編輯 `.env`，填入 `MISTRAL_API_KEY`。

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
  "model": "mistral-large-latest",
  "source_lang": "en",
  "target_lang": "zh-TW",
  "elapsed_ms": 1234
}
```

## 測試

```bat
pytest tests/agent -v
```

## Phase 1 內容

- FastAPI 服務（`/translate`、`/health`）
- Mistral `mistral-large-latest` 整合：短文 8 秒 timeout、長文依 token 動態 timeout、429/5xx 自動重試兩次、其他 4xx 快速失敗
- 台灣繁中 Prompt 模板 v1（`prompt.py`）
- 程式碼保護器（`code_protector.py`）：fenced block、inline code、HTML 標籤先抽出為佔位符，譯後還原並驗證完整性
- `source` 欄位標示結果來源（Phase 1 僅 `cloud` / `passthrough`，Phase 2 加入 `cache` / `offline_fallback`）

# Agentic Agent - Next-Generation Capabilities Todo List

## 1. 多智能體協作與非同步委派 (Multi-Agent Orchestration)
- [x] 實作 Manager/Planner Agent：負責將複雜的開發需求拆解成多步驟的 Markdown 執行計畫。
- [x] 實作 Coder Worker Agent：專門接收子任務並進行程式碼撰寫與修改。
- [x] 實作 Reviewer Agent：負責在 Coder 完成後，審查程式碼品質、安全性與是否符合需求。
- [x] 開發非同步委派 (Delegate) 機制，允許主 Agent 產生並管理多個 Sub-agent 行程。

## 2. 瀏覽器與視覺感知能力 (Browser & Visual Perception)
- [x] 整合 Playwright (無頭瀏覽器)，賦予 Agent 自動連線、點擊、輸入與捲動網頁的自動化能力。
- [x] 開發截圖與視覺分析工具 (Vision)：將網頁截圖或圖表送交 VLM (如 GPT-4o/LLaVA) 進行分析，讓 Agent 具備「看」的能力。
- [x] 實作視覺 UI 測試迴圈：Agent 可自主修改前端程式碼後，重新整理瀏覽器並「視覺確認」版面是否正確。

## 3. 沙盒隔離與容器化執行 (Docker/Sandbox Execution)
- [x] 導入 Docker API 整合：允許 Agent 建立臨時的 Container 環境。
- [x] 將 `run_command` 等執行工具的後端改為在隔離的 Docker Container 內執行，避免影響本機系統。
- [x] 在容器內配置獨立的檔案系統與依賴項，確保每次測試環境的乾淨與一致性。

## 4. 深度網路研究與文件學習 (Deep Web Research & RAG On-the-fly)
- [x] 開發 Web Research Sub-agent：具備自動使用 DuckDuckGo 或 Google Search 的能力。
- [x] 實作網頁爬蟲與 Markdown 轉換：抓取最新 API 官方文件並過濾出純文字內容。
- [x] 即時動態 RAG 機制：當 Agent 遭遇未知套件錯誤或過期語法時，自動上網學習並將新知識放入短期記憶以供後續修復使用。

## 5. 專案級重構與依賴管理 (Project Bootstrapping & Dependency Management)
- [x] 實作環境管理工具 (Environment Manager)：賦予 Agent 建立虛擬環境 (venv)、安裝與解析套件 (pip/npm) 的完整能力。
- [x] 實作專案鷹架 (Scaffolding) 工具：根據使用者的需求架構，自動產出整個微服務專案的資料夾樹狀結構與基礎配置檔。
- [x] 賦予 Agent 自主解析並更新 `requirements.txt` 或 `package.json` 等依賴檔案的能力，並自動解決版本衝突問題。

## 6. Model Context Protocol (MCP) 整合
- [ ] **實作 MCP Manager (生命週期管理)**：開發 `mcp_manager.py` 來啟動、監控並安全關閉獨立的 MCP Server 行程 (支援 stdio / SSE 雙向通訊機制)。
- [ ] **實作 MCP Client (通訊與橋接)**：開發 Client 端與 MCP Server 進行 JSON-RPC 2.0 握手，並能夠讀取 Server 端宣告的 `tools`、`resources` 與 `prompts`。
- [ ] **動態 Tool Schema 轉換**：將 MCP Server 回傳的工具定義，動態轉換並註冊為 LLM (OpenAI 格式) 可直接呼叫的 Function Calling Schema。
- [ ] **配置檔設計 (MCP Config)**：建立 `mcp_config.json`，讓使用者可以輕鬆配置與掛載第三方的 MCP Servers (如 GitHub, SQLite, FileSystem, Slack 等)。
- [ ] **實作 Tool Call 路由分發**：當 Agent 呼叫 MCP 工具時，系統能自動將 LLM 產生的參數封裝成 MCP `callTool` 請求，轉發給對應的 MCP Server，並將執行結果送回給 Agent。

# Agentic Agent - Next-Generation Capabilities Todo List

## 1. 多智能體協作與非同步委派 (Multi-Agent Orchestration)
- [ ] 實作 Manager/Planner Agent：負責將複雜的開發需求拆解成多步驟的 Markdown 執行計畫。
- [ ] 實作 Coder Worker Agent：專門接收子任務並進行程式碼撰寫與修改。
- [ ] 實作 Reviewer Agent：負責在 Coder 完成後，審查程式碼品質、安全性與是否符合需求。
- [ ] 開發非同步委派 (Delegate) 機制，允許主 Agent 產生並管理多個 Sub-agent 行程。

## 2. 瀏覽器與視覺感知能力 (Browser & Visual Perception)
- [ ] 整合 Playwright (無頭瀏覽器)，賦予 Agent 自動連線、點擊、輸入與捲動網頁的自動化能力。
- [ ] 開發截圖與視覺分析工具 (Vision)：將網頁截圖或圖表送交 VLM (如 GPT-4o/LLaVA) 進行分析，讓 Agent 具備「看」的能力。
- [ ] 實作視覺 UI 測試迴圈：Agent 可自主修改前端程式碼後，重新整理瀏覽器並「視覺確認」版面是否正確。

## 3. 沙盒隔離與容器化執行 (Docker/Sandbox Execution)
- [ ] 導入 Docker API 整合：允許 Agent 建立臨時的 Container 環境。
- [ ] 將 `run_command` 等執行工具的後端改為在隔離的 Docker Container 內執行，避免影響本機系統。
- [ ] 在容器內配置獨立的檔案系統與依賴項，確保每次測試環境的乾淨與一致性。

## 4. 深度網路研究與文件學習 (Deep Web Research & RAG On-the-fly)
- [ ] 開發 Web Research Sub-agent：具備自動使用 DuckDuckGo 或 Google Search 的能力。
- [ ] 實作網頁爬蟲與 Markdown 轉換：抓取最新 API 官方文件並過濾出純文字內容。
- [ ] 即時動態 RAG 機制：當 Agent 遭遇未知套件錯誤或過期語法時，自動上網學習並將新知識放入短期記憶以供後續修復使用。

## 5. 專案級重構與依賴管理 (Project Bootstrapping & Dependency Management)
- [ ] 實作環境管理工具 (Environment Manager)：賦予 Agent 建立虛擬環境 (venv)、安裝與解析套件 (pip/npm) 的完整能力。
- [ ] 實作專案鷹架 (Scaffolding) 工具：根據使用者的需求架構，自動產出整個微服務專案的資料夾樹狀結構與基礎配置檔。
- [ ] 賦予 Agent 自主解析並更新 `requirements.txt` 或 `package.json` 等依賴檔案的能力，並自動解決版本衝突問題。

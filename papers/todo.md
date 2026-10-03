# Agentic Agent - Future Features Todo List

## 1. 深度推理、規劃與自我修正 (Plan, Execute & Reflect)
- [ ] 實作 ReAct (Reason + Act) 或 Plan-and-Solve 循環架構。
- [ ] 使 Agent 能夠自動拆解任務（建立子任務清單）、依序執行。
- [ ] 新增自我反思 (Self-Reflection) 與自動重試修正機制，遇到工具執行錯誤時能在背景重試，直到達成目標。

## 2. 沙盒程式碼執行環境 (Code Interpreter Sandbox)
- [ ] 導入隔離的 Jupyter Kernel 或 Docker 沙盒環境。
- [ ] 使 Agent 能安全地執行 Python 腳本（用於數據分析、圖表繪製或複雜邏輯計算），並能查看結果與自動除錯。

## 3. 多代理人協作架構 (Multi-Agent Orchestration)
- [ ] 導入 Multi-Agent 機制（參考 CrewAI 或 AutoGen）。
- [ ] 實作任務委派功能，根據任務性質動態生成 Sub-Agents（例如：Researcher, Coder, Reviewer）來進行非同步協作。

## 4. 真正的人機協作與中斷點機制 (Human-in-the-Loop, HITL)
- [ ] 實作任務中斷 (Suspend) 與恢復 (Resume) 的狀態機機制。
- [ ] 在長效任務中遇到高風險操作（或需詢問使用者資訊時），能非同步暫停任務，透過通訊軟體等待使用者回覆後無縫接續執行。

## 5. 聯網檢索與視覺感知 (Web Browsing & Vision)
- [ ] 整合 Headless Browser (如 Playwright) 來處理 JavaScript 動態渲染的網頁爬取。
- [ ] 串接專業的搜尋引擎 API (如 SerpAPI / Tavily) 強化 RAG 能力。
- [ ] 實作多模態視覺理解功能，支援解析使用者上傳的截圖或圖片。

## 6. 動態自建工具能力 (Self-Evolution / Tool Creation)
- [ ] 開發讓 Agent 自動撰寫新 Python Skill 腳本的能力。
- [ ] 實作動態載入與熱更新 (Hot Reload) 註冊新工具的機制，使系統能在不重啟的情況下自我進化。

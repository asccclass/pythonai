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
- [x] **實作 MCP Manager (生命週期管理)**：開發 `mcp_manager.py` 來啟動、監控並安全關閉獨立的 MCP Server 行程 (支援 stdio / SSE 雙向通訊機制)。
- [x] **實作 MCP Client (通訊與橋接)**：開發 Client 端與 MCP Server 進行 JSON-RPC 2.0 握手，並能夠讀取 Server 端宣告的 `tools`、`resources` 與 `prompts`。
- [x] **動態 Tool Schema 轉換**：將 MCP Server 回傳的工具定義，動態轉換並註冊為 LLM (OpenAI 格式) 可直接呼叫的 Function Calling Schema。
- [x] **配置檔設計 (MCP Config)**：建立 `mcp_config.json`，讓使用者可以輕鬆配置與掛載第三方的 MCP Servers (如 GitHub, SQLite, FileSystem, Slack 等)。
- [x] **實作 Tool Call 路由分發**：當 Agent 呼叫 MCP 工具時，系統能自動將 LLM 產生的參數封裝成 MCP `callTool` 請求，轉發給對應的 MCP Server，並將執行結果送回給 Agent。

## 7. 任務終態驗證與自癒迴圈 (Task Success Verification & Self-Correction)
- [x] **實作任務驗收評估器 (Task Evaluator)**：新增 `task_evaluator.py`，支援「規則型斷言 (Rule-based Assertions)」與「獨立 LLM-as-a-Judge 驗收」雙軌機制，輸出結構化評估結果 `EvaluationResult(success: bool, score: float, feedback: str, criteria_met: dict)`。
- [x] **多維度自動化驗收斷言 (Multi-dimensional Task Assertions)**：
  - **程式開發任務**：強制串接 `run_test_suite`，必須滿足 Exit Code == 0 且無未捕獲異常。
  - **檔案與工作區狀態**：透過 `git_status` 與 `git_diff` 驗證目標檔案是否確實產生、非空且語法正確。
  - **副作用確認**：針對資料庫、外部請求或指令操作提供前置/後置狀態比對。
- [x] **重構主推論迴圈導入自省修正 (Critic-Correction Loop)**：
  - 修改 `server.py` 的 `run_agent`：當 LLM 停止發出 `tool_calls` 時，不直接結束回合，而是觸發 Task Evaluator。
  - 若驗證失敗且在容許修正次數內（如 `eval_retries < 2`），自動將具體的錯誤與缺失反饋包裝為觀察結果回傳給模型，驅動模型進行自主修正。
- [x] **強化記憶體狀態機與資料庫審計 (Episode State Machine)**：
  - 改造 `agent_runtime.py` 與 `memory.py` 的 `finish_episode`，將完成狀態精細化為 `verified_completed`、`rejected`、`needs_review`、`failed`。
  - 只有通過驗收的 Episode 才允許沉澱為 Procedural Memory 範例，避免將錯誤操作經驗固化為記憶。
- [x] **單元測試與驗收流程驗證 (Test Suite & Verification)**：建立 `tests/test_task_evaluator.py`，完整涵蓋測試通過、驗證未過觸發修復、達到修正上限轉交人工 (HITL) 等情境。

## 8. 合規 Hook Pipeline 與 Middleware 化 ReAct 生命週期
- [ ] **定義通用 Hook 抽象介面**：新增 `HookContext`、`HookResult`、`HookDecision` 與 `HookManager`，讓 Hook 能以一致格式讀取與修改 `messages`、模型請求、模型回覆、工具呼叫、工具參數、工具結果、`memory`、`episode_id` 與審計 metadata。
- [ ] **支援短路控制 (Short-circuiting)**：Hook 執行結果需能明確表示 `continue`、`block`、`return_early`、`return_tool_error`、`append_feedback` 等狀態；前置安全檢查失敗時，必須能立即中止模型呼叫或工具執行，並將錯誤或回饋以標準格式送回使用者或 LLM。
- [ ] **支援上下文變異 (Context Mutation)**：Hook 必須能改寫模型輸入 `messages`、工具名稱、工具參數、工具結果與最終回覆；所有變異需留下審計紀錄，避免無法追蹤是哪個 Hook 改變了上下文。
- [ ] **建立四個核心生命週期事件**：
  - `PreModelCall`：模型推論前執行，負責 Prompt 注入、記憶注入、Token/敏感詞審查、安全攔截與工作記憶壓縮。
  - `PostModelCall`：模型推論後執行，負責檢查最終回覆、清洗敏感資訊、審查或改寫 tool call。
  - `PreToolExecute`：工具執行前執行，負責權限校驗、危險指令攔截、參數正規化、路徑與環境限制。
  - `PostToolExecute`：工具執行後執行，負責結果清洗、linter/測試驗收、程序記憶紀錄、失敗回饋注入下一輪模型上下文。
- [ ] **將既有功能 Hook 化**：
  - 將 `LayaGuard.assess()` 包裝為 `PreModelCall` Hook。
  - 將 memory retrieval、skill guidance 與 working memory compaction 包裝為 `PreModelCall` context mutation hooks。
  - 將 `check_tool_permission()`、`run_command()` 的 command guard、workspace path guard 包裝為 `PreToolExecute` hooks。
  - 將 tool result logging、procedure result recording 與 `TaskEvaluator` critic feedback 包裝為 `PostToolExecute` 或 `PostModelCall` hooks。
- [ ] **讓 ReAct 核心迴圈只依賴 Hook 介面**：重構 `server.run_agent()` 與 `agent_runtime.run_agent_turn()`，移除硬編碼的 guard、context injection、tool audit、task evaluation 呼叫，改由 `HookManager.dispatch(lifecycle, context)` 負責執行，讓核心 loop 只處理「模型呼叫、工具呼叫、訊息迴圈」三件事。
- [ ] **設計 Hook 註冊與排序機制**：支援內建 hooks 與專案自訂 hooks；可設定優先順序、啟用/停用、fail-open/fail-closed 策略，以及依工具名稱、模型階段或任務型態套用不同 Hook。
- [ ] **補齊審計與可觀測性**：每次 Hook 執行需記錄 hook name、lifecycle、decision、mutation summary、耗時、錯誤與 blocked reason；寫入 episode events，並提供 observability 查詢。
- [ ] **補齊測試與驗收案例**：
  - PreModelCall 可 return early，且不會呼叫 LLM。
  - PostModelCall 可阻斷或改寫 tool call。
  - PreToolExecute 可阻斷危險工具，並把標準 tool error 回傳給 LLM。
  - PostToolExecute 可將驗收失敗回饋 append 到下一輪 `messages`。
  - 多個 Hook 依優先順序執行，短路後後續 Hook 不再執行。
  - Hook 對 context 的修改會被審計並可在 episode events 中追蹤。


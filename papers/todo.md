# Agentic Agent - Advanced Coding Capabilities Todo List

## 1. 專案級的程式碼語意解析與精準編輯 (AST & LSP Integration)
- [x] 整合 `tree-sitter` 來解析 Python/JS 等多語言的抽象語法樹 (AST)。
- [x] 實作精準的程式碼編輯工具，允許 Agent 針對特定的 Class 或 Function 進行外科手術式的片段替換，而非覆寫整份檔案。
- [x] 串接 Language Server Protocol (LSP)，賦予 Agent「跳到定義 (Go to definition)」與「尋找參考 (Find references)」的程式碼導覽能力。
- [x] 內建 Linter (如 flake8, pylint) 檢查，在 Agent 修改後立刻於背景自動偵測語法與縮排錯誤。

## 2. 全域程式碼索引與 RAG 檢索 (Codebase Indexing & Vector Search)
- [x] 建立專案層級的 File Tree 與 Metadata 快取機制。
- [x] 實作 Codebase RAG：自動掃描專案目錄，將所有 Function/Class 結構與 docstrings 轉換為向量 (Embeddings) 並存入本機資料庫。
- [x] 開發 `search_codebase` 工具，讓 Agent 能用語意搜尋找出關聯檔案 (例如: "尋找與登入驗證相關的路由與模型")。
- [x] 實作自動 Context 壓縮與片段截取機制，避免檢索到的大量檔案撐爆 LLM 的 Context Window。

## 3. 自動化測試回饋迴圈 (Test-Driven Loop & CI Integration)
- [x] 實作原生的測試驅動 (TDD) 開發狀態機。
- [x] 開發 `run_test_suite` 工具，允許 Agent 在沙盒內安全地自動執行 `pytest` 或自訂測試腳本。
- [x] 建立自動化除錯迴圈：精確擷取 `stderr` 的 Traceback 錯誤，並自動作為下一次 Code Revision 的輸入內容。
- [x] 設定自我修正次數上限 (Max Retries)，連續修復失敗時自動觸發 HITL，將控制權與錯誤報告交還給人類。

## 4. 深度 Git 版本控制整合 (Deep Git Integration)
- [x] 開發專屬結構化的 Git 操作工具群 (如 `git_status`, `git_diff_parser`, `git_commit`)。
- [x] 實作安全編輯存檔點 (Checkpoint) 機制：每次 Agent 進行大規模重構前，自動建立分支或 Commit，確保隨時可無痛 Rollback。
- [x] 賦予 Agent 解析大量 Diff 的能力，並實作自動撰寫標準化、高品質 Commit Messages 與 PR 說明的流程。

## 5. IDE 即時互動介面 (Editor Integration)
- [ ] 開發專屬的 VSCode Extension 或整合現有編輯器 API，跳脫純 CLI 與 Telegram 的文字對話限制。
- [ ] 實作 Inline Chat 功能：允許使用者在編輯器內選取特定程式碼區塊，直接呼叫 Agent 進行原地重構與解釋。
- [ ] 實作預測性程式碼補全 (Streaming Ghost Text) 的本機 API 伺服器，達成類似 GitHub Copilot 的無縫開發體驗。

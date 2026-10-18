# 需求與驗收

1. 接收 Harness² 任務：提供原生 `BenchmarkAdapter.execute()` 與 JSON CLI。
2. 執行既有 PythonAI ReAct Agent：沿用工具、hooks、權限、核准及 budget。
3. 保存過程：逐筆 JSONL 事件、完整程序輸出、對話、例外 traceback、最終文字及檔案。
4. 回傳結果：原生執行目錄、標準 trajectory、文字／二進位產出 manifest 與執行狀態。
5. 評估分離：外部 judge 可讀取結果；答案及 rubric 不進入 agent prompt。
6. 隔離與可追蹤：每次執行獨立 workspace/state；已終止結果可重讀，中斷不自動重跑。
7. 驗收：單元、程序整合與上游介面契約測試通過；修改範圍 diff check 通過並建立 commit。

本版不修改 Harness² 上游 CLI、不啟動 Web/Telegram 服務、不自動執行付費 benchmark，
不新增持續記憶及未映射的 skills/subagents/plugins/hooks surface。

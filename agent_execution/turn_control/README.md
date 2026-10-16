# Agent 無進展迴圈修正

2026-10-16。實際紀錄：tenant memory episode 4 同 URL fetch_url 重抓 15 次；episode 9 在 Windows 直接執行 mkdir 多次失敗，後續測試／驗收修正用盡迭代；edit_file 的 old_str not found 曾被錯誤視為成功。

依序待辦：

- [ ] T01：加入可 checkpoint 恢復的重複呼叫／同類失敗追蹤，提示改策略，阻止重複副作用；最後一輪保留收尾，不單純調高 15。
- [ ] T02：fetch_url 回傳可讀主文／GitHub README、標示來源、截斷、HTTP 失敗；保留原始抓取相容選項。
- [ ] T03：提供跨平台批次建目錄工具，沿用 workspace/lifecycle/permission 保護；Windows mkdir 明確引導，edit_file 失敗明確結構化。
- [ ] T04：驗收允許正常空設定檔；明確修復的失敗不永久毒化結果，但權限拒絕／不確定副作用不會被掩蓋。
- [ ] T05：新增循環、最後一輪／恢復、HTML／GitHub、批次目錄與驗收回歸；真實 URL 抓取、完整 pytest、diff check，僅提交本次路徑。

不修改既有使用者 llm-wiki 輸出、其他未提交工作，不重送正式使用者任務。輪數、request/token/time budget 仍為硬限制；沒有證據時不得聲稱任務完成。

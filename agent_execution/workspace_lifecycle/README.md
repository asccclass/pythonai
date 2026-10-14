# Workspace lifecycle

目錄歸屬保存於 TaskStore 的 `wl_*` version 1 tables。所有權使用 source／identity／project；conversation 非空時跨裝置保持同一 session，原有空 conversation 保留 transport session 範圍。沒有以 token 字串作所有權。

新程式目錄以 `project_scaffold` 或 `write_file` 建立時登記；來源任務明確包含暫存、臨時、試作、temporary 或 scratch 時為 temporary，其餘 retained。既有目錄維持 unmanaged。跨 session 寫入已登記目錄前必須由人類入口 attach；未知目錄的接管需 admin。

清理從預覽開始，綁定 directory ID、generation、檔案身份與內容摘要。共用、retained、未知和正在執行任務的目錄不能直接清理。封存沒有時間到期；僅使用者刪除最後相關 session 才永久清理 temporary 內容。

Windows 操作使用原生 pinned handle，移動透過 `NtSetInformationFile` 的已鎖定目的目錄 handle，不依字串路徑推測資料位置。子檔案鎖在原子 rename 前關閉，來源物件／父鏈仍保持鎖定，移動後重新鎖定全部內容並驗證同一 manifest；不一致停在 blocked。永久刪除只操作已 pin 的物件；partial purge 只接受原批准 manifest 的剩餘子集，新增檔案保留。連結、reparse、hard link、檔案占用、跨 volume 及不支援平台拒絕破壞操作。

`/workspace preview <directory-id> archive|restore` 產生預覽，`/workspace request <plan-id>` 請求批准，真實使用者以 `/workspace confirm <plan-id>` 確認；一般 `/approve`＋`/workspace execute <plan-id> <approval-id>` 也可用。blocked 操作以 `/workspace resume <plan-id>` 使用原 frozen 清單續跑。模型不能呼叫人類 dispatcher。`AGENT_WORKSPACE_CLEANUP=0` 停用新增破壞操作，但仍可處理已有意圖。

這是應用管理流程，無法限制已獲准執行任意 host 程式的 admin 或外部編輯器的所有副作用。首次接管未知目錄需明確人工操作，不能依名稱或時間猜測。

設計與里程碑：`papers/workspace_cleanup_plan.md`、`papers/todo.md`。

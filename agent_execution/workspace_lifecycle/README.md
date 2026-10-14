# Workspace lifecycle

目錄歸屬保存於 TaskStore 的 `wl_*` version 1 tables。所有權使用 source／identity／project；conversation 非空時跨裝置保持同一 session，原有空 conversation 保留 transport session 範圍。沒有以 token 字串作所有權。

新程式目錄以 `project_scaffold` 或 `write_file` 建立時登記；來源任務明確包含暫存、臨時、試作、temporary 或 scratch 時為 temporary，其餘 retained。既有目錄維持 unmanaged。跨 session 寫入已登記目錄前必須由人類入口 attach；未知目錄的接管需 admin。

清理從預覽開始，綁定 directory ID、generation、檔案身份與內容摘要。共用、retained、未知和正在執行任務的目錄不能直接清理。封存沒有時間到期；僅使用者刪除最後相關 session 才永久清理 temporary 內容。

M1 尚未提供破壞操作。後續 Windows 操作將使用不允許其他程序 rename／write 的原生 handle，對連結、reparse、hard link、不支援檔案及跨 volume 拒絕執行。不使用 shell 遞迴刪除；不支援原生保障的平台將拒絕破壞操作，清冊與預覽仍可使用。

這是應用管理流程，無法限制已獲准執行任意 host 程式的 admin 或外部編輯器的所有副作用。首次接管未知目錄需明確人工操作，不能依名稱或時間猜測。

設計與里程碑：`papers/workspace_cleanup_plan.md`、`papers/todo.md`。

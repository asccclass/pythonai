# Agent 無進展迴圈修正

## 搜尋重複呼叫與 HTTP 429（2026-10-24）

`search_web` 目前呼叫英文 Wikipedia Search API，並非一般搜尋引擎；結果不能當成已查到即時匯率。
原先搜尋錯誤字串被誤判為成功，且搜尋未納入成功讀取重用，造成相同關鍵字反覆連網。
現在相同參數的成功搜尋（含空結果）在同一輪重用；搜尋 HTTP／網路錯誤保留為失敗，附來源、
HTTP 狀態與服務提供的 `Retry-After`。單次 HTTP 呼叫有 15 秒 timeout，不在工具內重試。
收到 429 後，本輪即使更換關鍵字或從 checkpoint 恢復也不再查同一搜尋來源；仍可讀取其他公開來源。
若最終驗收未通過，不再由 critic 自動開啟搜尋修正回圈；新使用者輪次可以重新嘗試。
這些限制避免放大限流，不能保證解除上游本身的配額或流量限制。

## 共用執行步驟（Temporal TP09，2026-10-17）

`steps.py` 提供 `model_step`、`tool_step`、`evaluation_step`，由 `server.run_agent`
呼叫；SQLite runner 與 Temporal 唯讀 Activity 都使用這條路徑，不各自維護一套工具執行。
每次呼叫只做一次 I/O 嘗試，不在步驟內重試、重設預算或建立 Temporal Client。

- 模型步驟沿用 budget wrapper、trace、串流與取消；沒有工具時不送空 tools。失敗請求的保守計帳保持有效。
- 工具步驟透過既有 policy-aware dispatcher 執行，傳遞當前身分、workspace、取消及 hooks-managed context；不把 raw result 轉成另一種協定。
- 驗收步驟實際擷取 post-state，只傳入 `evaluation_start` 後的當輪訊息，目標仍由 runner 的持久化 task goal 決定。

呼叫方仍負責 PRE/POST hooks、checkpoint、重複操作防護、retry、驗收修正回圈與收尾。
這些函式只能在一般 runner／Activity 執行，不可在 deterministic Workflow 裡執行。
`tests/test_steps.py` 驗證失敗、取消、權限、context 還原、預算持久化與 SQLite 實際讀檔；
Temporal real-service 測試另驗證同一組步驟經 Worker 進入、實際讀檔與驗收。
後續 TP10 已在 Temporal Activity scope 接上 ledger 與 JSON artifact 引用，
每個共用步驟接受穩定 `operation_id`；legacy scope 不啟用 ledger，不改變 raw tool error 行為。
TP11 新增可選逐步 Workflow；M0 Activity 的崩潰／重試限制維持不變。

## 持久化步驟邊界（Temporal TP11，2026-10-17）

`continuation.py` 的 `StepController` 在 model、每個 tool 與 evaluation 邊界控制暫停，
以 `step_ready` checkpoint 保存下一步，再以內部 `TaskYield` 交回 Workflow。
`durable_invoke` 核對實際保存的邊界後才接受暫停；每次暫停保持 pending，不增加舊有
崩潰恢復次數。一般 SQLite runner 未啟用 controller，仍走原來的完整迴圈。

恢復時只將最新 assistant 後的 tool results 視為本輪配對，保留原 iteration、provider
attempt、驗收起點與進度。最後一輪已發出的模型結果／驗收仍可完成，但不能多開模型輪次。
模型 PRE/POST hooks、工具 policy 與驗收共用原實作；PRE hooks 前先保存 `step_preparing`，
未確認的準備階段停止為 uncertain；完成 hooks 確認後才保存可恢復邊界。
POST_MODEL hook 要求改策略時，先補齊被取代的 tool-call 回覆再交回下一個模型步驟。

新增測試覆蓋逐工具暫停、重複 tool-call ID、最後一輪、prepared provider request 重用、
hook 改策略與未驗證 TaskYield 拒絕；真實 Temporal Service 的恢復與 replay 測試見
`temporal_backend/tests/test_step_integration.py`。真實 provider 本輪探測逾時，尚未完成驗收。

## 執行環境感知（2026-10-16）

問題：工具底層已有部分平台判斷，對話提示詞卻未向模型提供實際執行環境，容易生成 Unix 命令於 Windows 執行，或把後端主機與容器視為相同環境。

依序待辦與驗收：

- [x] E01：CLI 與共用 Web／Telegram runtime 啟動時偵測 OS、版本、架構、Python runtime 與 PATH 可用命令名稱；不啟動 shell、不讀取憑證，不推測套件版本。
- [x] E02：系統提示附上環境；每次 Agent 執行（含恢復、排程、一般／串流路徑）更新環境區塊與當前 workspace，取代 checkpoint 舊資訊，保留原則與驗收目標索引。
- [x] E03：區分主機工具與 Linux run_command 容器；使用 sandbox 定義的同一映像名稱；不將主機 PATH 當作容器可用命令，不允許環境提示繞過權限或切換回主機執行。
- [x] E04：提示優先使用可攜檔案工具；run_command 前置攔截常見不相容命令，回傳替代方式與無副作用證據；已安裝的跨平台命令仍可使用。不是任意命令的完整相容性分析，其他命令仍以實際執行結果為準。
- [x] E05：平台矩陣、checkpoint／串流、前置攔截測試；完整 pytest、compileall、diff check；僅提交此功能路徑。

主機資訊啟動後快取；修改 PATH 或切換實體主機需重啟。執行模式與 workspace 每次進入 Agent 時重新讀取。Linux／macOS／容器分流透過模擬測試驗證，不能代表這些平台的實機或 Docker 部署驗證。

驗收紀錄：完整 pytest **802 passed、8 skipped、95 subtests passed**，609.02 秒；本機紀錄 `temp/environment-final-pytest.log`（不入 Git）。環境專項 **20 passed**；環境與驗收邊界回歸 **42 passed、6 subtests passed**。compileall／diff check 通過。真實 provider 回答後端作業系統為 Windows 11，另一次任務以一次 create_directory 建立兩個目錄，實際檢查均存在，沒有使用 run_command；紀錄 `temp/environment-live-provider.log`。既有 Starlette/AnyIO deprecation warning 不影響測試結果。服務需重啟以載入此次改動。

2026-10-16。實際紀錄：tenant memory episode 4 同 URL fetch_url 重抓 15 次；episode 9 在 Windows 直接執行 mkdir 多次失敗，後續測試／驗收修正用盡迭代；edit_file 的 old_str not found 曾被錯誤視為成功。

依序待辦：

- [x] T01：加入可 checkpoint 恢復的重複呼叫／同類失敗追蹤，提示改策略，阻止重複副作用；最後一輪保留收尾，不單純調高 15。權限／參數 hook 拒絕也會追蹤；成功的重複讀取引用既有結果，不重新連網。`AGENT_MAX_ITERATIONS` 預設上限 32，實際依剩餘 request 預算再縮小，預留 judge 額度；顯式參數與 checkpoint 上限保留，硬預算不提升。
- [x] T02：fetch_url 回傳可讀主文／GitHub README、標示來源、截斷、HTTP 失敗；保留 `raw=true` 原始抓取選項（仍檢查 HTTP 失敗）。GitHub 根 URL 使用官方 readme API，不猜分支。另修正上下文裁剪：最新完整 tool call/result 組必須保留，過長文字以標示截短，原始執行證據不變；系統、目標、使用者請求、approval 不截短，真的放不下明確報錯。
- [x] T03：提供跨平台批次建目錄工具，沿用 workspace/lifecycle/permission 保護並登錄 builtin manifest；Windows mkdir 明確引導，維持 CompletedProcess 回傳相容；edit_file 失敗明確結構化，write_file 回傳已寫入路徑。
- [x] T04：驗收允許正常空 requirements.txt／__init__.py／.gitkeep。只有可證明未產生副作用的 mkdir／edit 前置拒絕，加上後續成功工具紀錄與實際檔案內容／目錄相符，才視為已修復；權限拒絕、執行失敗與不確定副作用仍阻止驗收。
- [x] T05：新增循環、最後一輪／恢復、HTML／GitHub、批次目錄與驗收回歸；真實 URL 抓取、完整 pytest、diff check，僅提交本次路徑。

不修改既有使用者 llm-wiki 輸出、其他未提交工作，不重送正式使用者任務。輪數、request/token/time budget 仍為硬限制；沒有證據時不得聲稱任務完成。

## 驗收紀錄

- 完整 pytest：**778 passed、8 skipped、95 subtests passed**，656.09 秒。紀錄：本機 `temp/turn-control-final-pytest.log`（不入 Git）。
- 完整測試後補強並行 tool-call/result 順序及 provider 空工具欄位相容性；相關 CLI／tools／hooks／evaluator／budget／durable task／streaming 回歸 **188 passed、1 skipped、10 subtests passed**，153.39 秒，紀錄 `temp/turn-control-followup-pytest.log`。最終 turn_control 模組 **25 passed**，含後補的串流空工具測試；compileall／diff check 通過。既有 Starlette/AnyIO deprecation warning 不影響結果。
- 真實 URL：HarnessRouter repo 取得 21,797 字元 README，HTTP 成功、含 UHP 說明、沒有 HTML 外殼。
- 真實設定 provider：一般 repo 分析僅呼叫 fetch_url 一次便完成；另透過完整 41 個工具 schema、正式 request/token 預算與上下文治理的 AgentRuntime/TaskStore 流程，再驗證一次，仍只 fetch 一次，任務狀態 completed，回覆含 UHP 與使用情境。
- 真實模型於隔離暫存 workspace 建立 wiki：一次 create_directory、三次 write_file、一次 list_files；七個必要目錄與三個文件全部存在，TaskEvaluator 通過。無 Python scaffold 或 shell mkdir；原有 workspace/llm-wiki 未更動。
- 真實 provider 拒絕 `tools=[]`（HTTP 400）；現在無工具時省略欄位，已在一般與串流收尾請求實測成功，並覆蓋有／無 task budget 的請求邊界測試。
- 重複成功讀取重用已保存的原始內容與 source tool-call ID，最新觀察即使經過裁剪仍有內容；提示附在工具回覆內，不插入尚未回覆完整的平行呼叫之間。原始工具證據保留於 checkpoint，不以提示文字偽造新讀取或測試成功。
- 真實 Telegram／桌面端重連未額外實測；服務需重新啟動以載入 Python 程式與 builtin manifest。測試跳過項不代表已通過外部裝置／Docker 部署驗證。

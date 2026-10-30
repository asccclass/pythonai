# ContactAI MCP 串接

需求：PythonAI 透過 HTTPS MCP 存取 ContactAI，會員身分由 MemAuth 驗證。
沿用既有 `httpx` 相依套件及專案 venv，無新增依賴。

流程：PythonAI → ContactAI `/api/auth/memauth` → MemAuth
`https://auth.justdrink.com.tw/apilogin`、`/verifytoken` → ContactAI 工作階段
→ `https://contacts.justdrink.com.tw/mcp`。
MemAuth 不是 OAuth/OIDC 授權碼提供者，勿設定成既有 `oauth.issuer`。
不共用 JWT 簽名密鑰，不直接讀取任何正式資料庫。

根目錄 `mcp_config.json` 已新增 `contactai`，使用 MCP `2025-06-18`。
提供 `mcp_contactai_search_contacts`、`mcp_contactai_get_contact`、
`mcp_contactai_create_contact`；實際工具以遠端 discovery 為準。
健康檢查使用 `tools/list`，因目前 ContactAI 不支援 `ping`。

## 設定與啟動

在啟動 PythonAI 的同一個 PowerShell 設定已驗證 Email 的會員帳密。
密碼透過遮蔽輸入，不要放進 shell 指令歷程、Git 或 `mcp_config.json`：

```powershell
$env:CONTACTAI_EMAIL = Read-Host 'MemAuth Email'
$contactaiSecret = Read-Host 'MemAuth Password' -AsSecureString
$env:CONTACTAI_PASSWORD = [System.Net.NetworkCredential]::new('', $contactaiSecret).Password
Remove-Item Env:CONTACTAI_MEMAUTH_TOKEN -ErrorAction SilentlyContinue
# 在此 shell 啟動平常使用的 PythonAI 入口，使其繼承環境變數。
```

也可只設定 `$env:CONTACTAI_MEMAUTH_TOKEN = Read-Host 'MemAuth Access Token'`。
這是 MemAuth Access Token，會優先於帳密，再交換成 ContactAI Token；
不是直接把 MemAuth Token 傳給 `/mcp`。
已有 ContactAI Access Token 者可改用外層 `token_env`，但必須移除 `memauth`，
且該模式不提供自動刷新。

認證資料缺少時，啟動會略過 contactai，其他 MCP 仍可使用。
補入環境變數後需重啟 PythonAI 或明確呼叫 MCP manager reconnect。
程式不會自動載入 `.env`。

認證與 discovery 的唯讀連線檢查（不搜尋或建立聯絡人）：

```powershell
.\venv\Scripts\python.exe -m agent_execution.extensions.contactai --prompt
# 已設定環境變數時可省略 --prompt。
```

此命令只輸出 ready、工具名稱或錯誤類型。檢查結束會嘗試撤銷該次
ContactAI Refresh Token，無法把互動式檢查登入傳遞給另一個 PythonAI 程序。
實際使用仍須讓 PythonAI 程序繼承上述環境變數。

## 工作階段與權限

Token 僅存在程序記憶體，Access Token 到期前 30 秒使用 ContactAI
`/api/auth/refresh` 刷新；不儲存密碼或 Token 到檔案。
若刷新或認證失敗，必須明確重新連線；不重送任何 `tools/call`，
包含新增聯絡人時遇到的 401、逾時或斷線。
關閉客戶端時嘗試 `/api/auth/logout`，服務不可用時無法保證撤銷成功。
Access Token 在到期前仍可能有效。

預設 `memauth.identity=local`，僅允許本機執行身分使用這組憑證。
其他 identity 的呼叫會在連線前遭拒。指定外部身分需由部署端建立專用客戶端，
在同一可信執行身分下初始化與呼叫，並將 identity 設為該完整身分；
目前全域 manager 的啟動執行緒使用 local，勿只更改 identity 設定。
不支援多會員自助登入或共用帳號映射。
聯絡人的存取範圍仍由 ContactAI 依登入會員強制限制。

ContactAI 後端需設定 `AuthBaseURL=https://auth.justdrink.com.tw`。
`memauth.origin` 會送到 ContactAI 並在其 `AuthOrigin` 空白時轉送到 MemAuth；
若後端設定了 `AuthOrigin`，以後端值為準。
來源必須符合會員註冊來源；401 也可能表示帳密錯誤或 Email 尚未驗證。
502 表示上游認證服務失敗或回應無效。

HTTPS 與 loopback HTTP 以外網址不接受，憑證不跟隨重新導向，
認證回應上限 64 KiB／10 秒；MCP 回應沿用 1 MiB 上限。
正式環境是否已部署目前 contactai 的 API，需使用有效測試會員驗收。

測試（模擬 HTTP 端點，不使用正式帳號及資料）：

```powershell
.\venv\Scripts\python.exe -m pytest agent_execution/extensions/contactai/test_contactai.py agent_execution/tests/test_extensions.py tests/test_mcp_manager.py
```

全專案測試另包含既有 PDF OCR 整合案例，需要 Tesseract 的 `eng` 模型；
若機器僅安裝 `chi_tra`，可在測試 shell 設定
`$env:AGENT_TESSERACT_LANGUAGE = 'chi_tra'`，再執行測試。
此設定只選擇已安裝的 OCR 模型，不影響 MCP 認證。

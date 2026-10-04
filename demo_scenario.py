import base

print('1. 網路搜尋 (Web Search)...')
search_result = base.TOOLS['search_web'](query='Anthropic Claude 3.5 Sonnet feature updates', max_results=2)
print(f'-> 搜尋完成。擷取前幾筆資料...\n{search_result[:150]}...')

print('\n2. 任務委派給專家 Sub-Agent (Multi-Agent Orchestration)...')
delegate_result = base.TOOLS['delegate_task'](
    agent_role='AI News Analyst', 
    task_description=f'Please briefly summarize the key updates in 50 words based on these search results:\n{search_result[:500]}'
)
print('-> Sub-Agent 回覆：\n' + delegate_result)

print('\n3. 沙盒程式碼執行 (Code Interpreter using run_command)...')
sandbox_result = base.TOOLS['run_command'](command=['python', '-c', 'print("Hello from the Sandbox! Drill Completed Successfully!")'])
print(f'-> 執行結果：\nstdout: {sandbox_result.stdout.strip()}\nstderr: {sandbox_result.stderr.strip()}')

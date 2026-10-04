def delegate_task(agent_role: str, task_description: str) -> str:
    try:
        from server import run_agent, build_system_prompt
        import logging
        
        base_prompt = build_system_prompt()
        system_prompt = f"""{base_prompt}\n\n=================================\nMULTI-AGENT DIRECTIVE:\nYou are now acting as a Sub-Agent. Your role is: {agent_role}.\nYour specific task is: {task_description}\nFocus ONLY on completing this task. When you are done, summarize the results and stop calling tools.\n================================="""
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Please complete the following task:\n{task_description}"}
        ]
        
        print(f"\n[Orchestrator] Spawning Sub-Agent '{agent_role}'...")
        
        result = run_agent(messages, max_retries=3)
        
        print(f"[Orchestrator] Sub-Agent '{agent_role}' finished task.")
        
        return f"Sub-Agent '{agent_role}' returned:\n{result}"
    except Exception as e:
        return f"Delegation failed: {e}"

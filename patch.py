import sys

def patch_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()
        
    start_idx = -1
    end_idx = -1
    for i, line in enumerate(lines):
        if line.startswith('    def process_next(self) -> AgentCommand | None:'):
            start_idx = i
        if start_idx != -1 and line.startswith('        return self.store.command_by_id(command.command_id)'):
            end_idx = i
            break
            
    if start_idx == -1 or end_idx == -1:
        print("Could not find process_next method bounds")
        sys.exit(1)
        
    new_code = """    def _send_reply(self, command: AgentCommand, text: str, is_error: bool = False) -> None:
        adapter = self.adapters[command.platform]
        adapter.send_message(
            OutboundMessage(
                platform=command.platform,
                conversation_id=command.conversation_id,
                text=text,
                reply_to_message_id=str(command.source_message_id) if command.source_message_id is not None else None,
            )
        )
        self.store.record_outbound_message(
            command.platform,
            command.conversation_id,
            text,
            platform_message_id=f"{command.command_id}:{'error' if is_error else 'outbound'}",
        )

    def process_next(self) -> AgentCommand | None:
        pending = self.store.pending_commands(limit=1)
        if not pending:
            return None
        command = pending[0]
        self.store.mark_command_running(command.command_id)

        text = command.text.strip()
        if text.startswith("/status"):
            jobs = self.store.query_jobs_status(limit=5)
            lines = ["📋 Recent Jobs:"]
            for j in jobs:
                status_icon = "⏳" if j.status == "pending" else "🏃" if j.status == "running" else "✅" if j.status == "completed" else "❌"
                lines.append(f"{status_icon} [{j.status}] {j.command_id[:8]}: {j.text[:30]}")
            result_text = "\n".join(lines)
            self.store.complete_command(command.command_id, result_text)
            self._send_reply(command, result_text)
            return self.store.command_by_id(command.command_id)
            
        elif text.startswith("/cancel"):
            parts = text.split()
            if len(parts) > 1:
                target_id = parts[1]
                resolved_id = self.store.resolve_command_id(target_id)
                if resolved_id:
                    if self.store.cancel_command(resolved_id):
                        result_text = f"✅ Cancel requested for job {resolved_id[:8]}"
                    else:
                        result_text = f"❌ Could not cancel job {resolved_id[:8]} (may be already completed or cancelled)"
                else:
                    result_text = f"❌ Job {target_id} not found."
            else:
                result_text = "❌ Please specify a job id: /cancel <job_id>"
                
            self.store.complete_command(command.command_id, result_text)
            self._send_reply(command, result_text)
            return self.store.command_by_id(command.command_id)

        try:
            result_text = self.command_runner(command)
            self.store.complete_command(command.command_id, result_text)
            self._send_reply(command, result_text)
        except Exception as error:
            error_text = f"Command failed: {error}"
            self.store.fail_command(command.command_id, str(error))
            self._send_reply(command, error_text, is_error=True)
        return self.store.command_by_id(command.command_id)
"""
    
    with open(filepath, 'w', encoding='utf-8') as f:
        f.writelines(lines[:start_idx])
        f.write(new_code)
        f.writelines(lines[end_idx+1:])
    print("Patched successfully")

if __name__ == '__main__':
    patch_file('communication_worker.py')
class TaskSuspendedException(Exception):
    def __init__(self, question: str, tool_call_id: str, messages: list):
        self.question = question
        self.tool_call_id = tool_call_id
        self.messages = messages
        super().__init__(question)

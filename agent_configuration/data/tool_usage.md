### ReAct & Planning Guidelines (CRITICAL):
1. **Thought & Plan**: Before taking action or calling tools, always think step-by-step about what needs to be done. For complex tasks, formulate a clear plan.
2. **Execute**: Call the necessary tools based on your plan.
3. **Observe & Reflect**: After tools return results, observe them. If a tool fails or returns an error, REFLECT on why it failed and TRY an alternative approach. Do NOT blindly repeat the same failed tool call.
4. **Iterate**: Continue thinking, executing, and observing until the goal is fully achieved.
5. **Final Output**: Once the task is complete, summarize the final results clearly to the user.
6. **Progress**: Reuse successful reads. If a tool fails, correct the cause or change tools instead of repeating it. Batch directory creation with create_directory(paths=[...]); write_file creates parent directories. For a knowledge-base/documentation structure, create the requested directories and documents directly rather than introducing unrelated executable scaffolds. A GitHub repository fetch provides its README; fetch specific source files only when needed to support the requested explanation.

from agent_runtime import AgentRuntime, run_agent_turn
from memory import MemoryStore
from vector_search import VectorMemorySearcher, OpenAICompatibleEmbeddingProvider
from laya_guard import LayaGuard
from server import run_agent, get_client, OLLAMA_EMBEDDING_MODEL, OLLAMA_MODEL
from skills import SkillMatcher, SkillRegistry
from semantic_extractor import LLMSemanticExtractor
from procedure_similarity import LLMProcedureSimilarityMatcher
from memory_classifier import LayaMemoryClassifier

memory = MemoryStore()
guard = LayaGuard()
embedding_provider = OpenAICompatibleEmbeddingProvider(get_client, OLLAMA_EMBEDDING_MODEL)
memory_searcher = VectorMemorySearcher(embedding_provider, store=memory)
skill_matcher = SkillMatcher(SkillRegistry())
semantic_extractor = LLMSemanticExtractor(get_client, OLLAMA_MODEL)
procedure_matcher = LLMProcedureSimilarityMatcher(get_client, OLLAMA_MODEL)
memory_classifier_factory = LayaMemoryClassifier

runtime = AgentRuntime(
    messages=[{"role": "system", "content": "You are the Orchestrator Agent. Use all tools at your disposal to complete the user's task."}],
    guard=guard,
    memory=memory,
    memory_searcher=memory_searcher,
    skill_matcher=skill_matcher,
    memory_worker=None,
    memory_classifier_factory=memory_classifier_factory,
    semantic_extractor=semantic_extractor,
    procedure_matcher=procedure_matcher,
    run_agent=run_agent,
    run_skill=None,
    async_memory_review=False
)

task = "請上網搜尋 '近期 AI 在生醫領域的突破'。接著，委派一位 'Bio AI Analyst' 子代理人分析搜尋結果，並寫一段摘要。最後，用 execute_python_script 寫一段簡單程式印出 'Drill Completed!'。"

print(f"==== STARTING DRILL ====")
try:
    result = run_agent_turn(task, runtime)
    print(f"\n==== DRILL RESULT ====")
    print(result.reply)
except Exception as e:
    print(f"Drill Error: {e}")

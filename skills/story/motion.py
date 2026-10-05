from pathlib import Path
from collections import defaultdict
import itertools

import matplotlib.pyplot as plt
import networkx as nx
import pandas as pd
from snownlp import SnowNLP


DEFAULT_STORY_PATH = Path(__file__).resolve().with_name("story.txt")
DEFAULT_RELATION_GRAPH_OUTPUT_PATH = Path(__file__).resolve().with_name("relation_graph.png")
STORY_ENTITY_TYPES = {"PERSON", "GPE", "ORG", "LOC"}


def load_story_chapters(story_path: Path = DEFAULT_STORY_PATH) -> dict[str, str]:
    if not story_path.exists():
        return {}
    lines = [
        line.strip()
        for line in story_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return {f"第{index}章": text for index, text in enumerate(lines, start=1)}


def create_ckip_drivers(device: int = -1):
    from ckip_transformers.nlp import CkipNerChunker, CkipWordSegmenter
    ws_driver = CkipWordSegmenter(model="bert-base", device=device)
    ner_driver = CkipNerChunker(model="bert-base", device=device)
    return ws_driver, ner_driver


def calculate_sentiment_scores(texts: list[str]) -> list[float]:
    sentiment_scores = []
    for text in texts:
        sentiment = SnowNLP(text).sentiments
        sentiment_scores.append((sentiment - 0.5) * 2)
    return sentiment_scores


def token_spans(text: str, tokens: list[str]) -> list[tuple[str, int, int]]:
    spans = []
    cursor = 0
    for token in tokens:
        start = text.find(token, cursor)
        if start < 0:
            start = cursor
        end = start + len(token)
        spans.append((token, start, end))
        cursor = end
    return spans


def merge_tokens_with_entities(text: str, tokens: list[str], ner) -> list[str]:
    entities = sorted(
        (
            (entity.idx[0], entity.idx[1], entity.word)
            for entity in ner
            if entity.ner in STORY_ENTITY_TYPES
        ),
        key=lambda item: item[0],
    )
    if not entities:
        return tokens

    merged_tokens = []
    spans = token_spans(text, tokens)
    entity_index = 0
    token_index = 0
    while token_index < len(spans):
        token, start, end = spans[token_index]
        while entity_index < len(entities) and entities[entity_index][1] <= start:
            entity_index += 1

        if entity_index < len(entities):
            entity_start, entity_end, entity_word = entities[entity_index]
            if entity_start <= start and end <= entity_end:
                merged_tokens.append(entity_word)
                token_index += 1
                while token_index < len(spans) and spans[token_index][2] <= entity_end:
                    token_index += 1
                entity_index += 1
                continue

        merged_tokens.append(token)
        token_index += 1
    return merged_tokens


def print_story_elements(
    chapters: list[str],
    texts: list[str],
    story_ws: list[list[str]],
    story_ner,
) -> None:
    print("\n=== CKIP 敘事元素提取結果 ===")
    for chapter, text, ws, ner in zip(chapters, texts, story_ws, story_ner):
        display_ws = merge_tokens_with_entities(text, ws, ner)
        print(f"\n【{chapter}】")
        print(f"斷詞結果: {' / '.join(display_ws[:10])} ... (下略)")
        entities = [
            f"{entity.word}({entity.ner})"
            for entity in ner
            if entity.ner in STORY_ENTITY_TYPES
        ]
        print(f"關鍵角色與場景: {', '.join(set(entities)) if entities else '無'}")


def extract_chapter_characters(story_ner) -> list[list[str]]:
    chapter_characters = []
    for ner_result in story_ner:
        names = {entity.word for entity in ner_result if entity.ner == "PERSON"}
        chapter_characters.append(list(names))
    return chapter_characters


def build_edges_data(chapter_characters: list[list[str]]) -> list[tuple[str, str, int]]:
    edge_weights = defaultdict(int)
    for chars in chapter_characters:
        if len(chars) < 2:
            continue
        pairs = itertools.combinations(sorted(chars), 2)
        for u, v in pairs:
            edge_weights[(u, v)] += 1

    return [(u, v, weight) for (u, v), weight in sorted(edge_weights.items())]


def build_relation_graph(chapter_characters: list[list[str]]) -> nx.Graph:
    edges_data = build_edges_data(chapter_characters)
    graph = nx.Graph()
    for u, v, weight in edges_data:
        graph.add_edge(u, v, weight=weight)
    return graph


def analyze_character_centrality(graph: nx.Graph) -> pd.DataFrame:
    weighted_degree_dict = {
        node: sum(data.get("weight", 1) for _, _, data in graph.edges(node, data=True))
        for node in graph.nodes()
    }
    max_weighted_degree = max(weighted_degree_dict.values(), default=0)
    degree_dict = {
        node: (value / max_weighted_degree if max_weighted_degree else 0.0)
        for node, value in weighted_degree_dict.items()
    }

    distance_graph = graph.copy()
    for _, _, data in distance_graph.edges(data=True):
        weight = data.get("weight", 1)
        data["distance"] = 1 / weight if weight else 1

    betweenness_dict = nx.betweenness_centrality(distance_graph, normalized=True, weight="distance")
    eigenvector_dict = nx.eigenvector_centrality(graph, max_iter=1000, weight="weight")

    metrics_data = []
    for char in graph.nodes():
        metrics_data.append({
            "角色名稱": char,
            "加權度中心性 (互動廣度)": round(degree_dict[char], 3),
            "加權中介中心性 (情節橋樑)": round(betweenness_dict[char], 3),
            "加權特徵向量中心性 (影響力)": round(eigenvector_dict[char], 3),
        })

    df = pd.DataFrame(metrics_data)
    return df.sort_values(by="加權特徵向量中心性 (影響力)", ascending=False).reset_index(drop=True)


def build_sentiment_relation_graph(
    chapter_characters: list[list[str]],
    sentiment_scores: list[float],
) -> nx.Graph:
    graph = nx.Graph()
    for chars, sentiment_score in zip(chapter_characters, sentiment_scores):
        if len(chars) < 2:
            continue
        pairs = itertools.combinations(sorted(chars), 2)
        for u, v in pairs:
            if graph.has_edge(u, v):
                graph[u][v]["total_sentiment"] += sentiment_score
                graph[u][v]["interactions"] += 1
            else:
                graph.add_edge(u, v, total_sentiment=sentiment_score, interactions=1)
    return graph


def narrative_role_tag(avg_sentiment: float) -> str:
    if avg_sentiment > 0.2:
        return "正向角色 / 救贖者"
    if avg_sentiment < -0.2:
        return "負向角色 / 衝突源"
    return "中性角色 / 受害者"


def analyze_character_sentiment_traits(graph: nx.Graph) -> pd.DataFrame:
    character_narrative = []
    for node in graph.nodes():
        total_node_sentiment = 0.0
        total_interactions = 0
        for neighbor in graph.neighbors(node):
            edge_data = graph[node][neighbor]
            total_node_sentiment += edge_data["total_sentiment"]
            total_interactions += edge_data["interactions"]

        avg_sentiment = total_node_sentiment / total_interactions if total_interactions > 0 else 0.0
        character_narrative.append({
            "角色名稱": node,
            "總互動次數": total_interactions,
            "情感投射分數": round(avg_sentiment, 3),
            "敘事角色診斷": narrative_role_tag(avg_sentiment),
        })

    df = pd.DataFrame(character_narrative)
    return df.sort_values(by="情感投射分數", ascending=False).reset_index(drop=True)


def draw_relation_graph(graph: nx.Graph, output_path: Path = DEFAULT_RELATION_GRAPH_OUTPUT_PATH) -> Path:
    plt.rcParams["font.sans-serif"] = ["Microsoft JhengHei"]
    plt.rcParams["axes.unicode_minus"] = False
    figure = plt.figure(figsize=(7, 7))

    pos = nx.spring_layout(graph, k=0.8, seed=42)
    edges = graph.edges(data=True)
    weights = [data["weight"] * 3 for _, _, data in edges]

    nx.draw_networkx_nodes(graph, pos, node_size=1500, node_color="lightblue", alpha=0.9)
    nx.draw_networkx_labels(graph, pos, font_size=12, font_family="Microsoft JhengHei", font_weight="bold")
    nx.draw_networkx_edges(graph, pos, width=weights, edge_color="gray", alpha=0.6)

    edge_labels = nx.get_edge_attributes(graph, "weight")
    nx.draw_networkx_edge_labels(graph, pos, edge_labels=edge_labels, font_size=10, font_color="red")

    plt.title("基於 CKIP 提取之故事人物關係網絡圖", fontsize=14, fontweight="bold")
    plt.axis("off")
    figure.savefig(output_path, bbox_inches="tight", dpi=150)
    plt.close(figure)
    return output_path


def draw_sentiment_curve(chapters: list[str], sentiment_scores: list[float]) -> None:
    plt.rcParams["font.sans-serif"] = ["Microsoft JhengHei"]
    plt.rcParams["axes.unicode_minus"] = False

    plt.figure(figsize=(9, 4.5))
    plt.plot(chapters, sentiment_scores, marker="o", linestyle="-", color="darkcyan", linewidth=2.5)
    plt.axhline(0, color="red", linestyle="--", alpha=0.5)

    plt.title("基於 CKIP 文本預處理之故事情感起伏曲線", fontsize=14)
    plt.xlabel("故事進度 / 章節", fontsize=12)
    plt.ylabel("情感得分 (-1極度負面 ~ 1極度正面)", fontsize=12)
    plt.ylim(-1.1, 1.1)
    plt.grid(True, linestyle=":", alpha=0.6)
    plt.show()


def main() -> None:
    print("正在載入 CKIP 模型...")
    ws_driver, ner_driver = create_ckip_drivers(device=-1)

    story_chapters_zh = load_story_chapters()
    chapters = list(story_chapters_zh.keys())
    texts = list(story_chapters_zh.values())

    print("正在進行 CKIP 斷詞與實體辨識...")
    story_ws = ws_driver(texts)
    story_ner = ner_driver(texts)
    print_story_elements(chapters, texts, story_ws, story_ner)

    chapter_characters = extract_chapter_characters(story_ner)
    print("\n=== 各章節角色提取結果 ===")
    for chapter, chars in zip(chapters, chapter_characters):
        print(f"{chapter}出現角色: {chars}")

    sentiment_scores = calculate_sentiment_scores(texts)

    graph = build_relation_graph(chapter_characters)
    graph_path = draw_relation_graph(graph)
    print(f"\n人物關係網絡圖已輸出: {graph_path}")

    metrics_df = analyze_character_centrality(graph)
    print("\n=== 計算敘事學：人物中心性定量分析表 ===")
    print(metrics_df.to_string())

    sentiment_graph = build_sentiment_relation_graph(chapter_characters, sentiment_scores)
    sentiment_traits_df = analyze_character_sentiment_traits(sentiment_graph)
    print("\n=== 計算敘事學：角色情感特質定量分析表 ===")
    print(sentiment_traits_df.to_string(index=False))

    draw_sentiment_curve(chapters, sentiment_scores)


if __name__ == "__main__":
    main()

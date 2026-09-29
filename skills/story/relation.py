import matplotlib.pyplot as plt
import networkx as nx
import pandas as pd
from ckip_transformers.nlp import CkipNerChunker
from collections import defaultdict
import itertools
from pathlib import Path

from huggingface_hub import snapshot_download


CKIP_NER_REPO_ID = "ckiplab/albert-tiny-chinese-ner"
CKIP_MODEL_DIR = Path(__file__).resolve().parents[2] / "models" / "ckip" / "albert-tiny-chinese-ner"
DEFAULT_GRAPH_OUTPUT_PATH = Path(__file__).resolve().with_name("relation_graph.png")


def ensure_ckip_ner_model(model_dir: Path = CKIP_MODEL_DIR) -> Path:
    model_dir.mkdir(parents=True, exist_ok=True)
    if not (model_dir / "config.json").exists():
        snapshot_download(
            repo_id=CKIP_NER_REPO_ID,
            local_dir=str(model_dir),
            local_dir_use_symlinks=False,
        )
    return model_dir


def create_ner_driver(device: int = -1) -> CkipNerChunker:
    model_dir = ensure_ckip_ner_model()
    return CkipNerChunker(model_name=str(model_dir), device=device)


# 2. 準備包含更多角色互動的故事文本
story_chapters = [
    "很久很久以前，主角張小明和他的家人，在寧靜的平安村過著幸福快樂的日子。",
    "突然間，黑龍會摧毀了平安村，張小明奮力抵抗，卻與妹妹張小華在混亂中走散了。",
    "張小明獨自流浪，在寒冬中遇到了善良的旅人李大叔，李大叔給了他溫暖與活下去的希望。",
    "幾年後，張小明長大了。他在市集巧遇了李大叔，兩人決定一起出發尋找張小華的下落。",
    "在繁華的京城裡，李大叔透過老朋友王掌櫃的情報，終於幫張小明找到了失散多年的張小華。"
]


def extract_chapter_characters(chapters: list[str], ner_driver: CkipNerChunker) -> list[list[str]]:
    story_ner = ner_driver(chapters)
    chapter_characters = []
    for ner_result in story_ner:
        names = {entity.word for entity in ner_result if entity.ner == 'PERSON'}
        chapter_characters.append(list(names))
    return chapter_characters


def build_relation_graph(chapter_characters: list[list[str]]) -> nx.Graph:
    edges_data = build_edges_data(chapter_characters)
    graph = nx.Graph()
    for u, v, weight in edges_data:
        graph.add_edge(u, v, weight=weight)
    return graph


def build_edges_data(chapter_characters: list[list[str]]) -> list[tuple[str, str, int]]:
    edge_weights = defaultdict(int)
    for chars in chapter_characters:
        if len(chars) < 2:
            continue
        pairs = itertools.combinations(sorted(chars), 2)
        for u, v in pairs:
            edge_weights[(u, v)] += 1

    return [(u, v, weight) for (u, v), weight in sorted(edge_weights.items())]


def analyze_character_centrality(graph: nx.Graph) -> pd.DataFrame:
    degree_dict = nx.degree_centrality(graph)
    betweenness_dict = nx.betweenness_centrality(graph, normalized=True)
    eigenvector_dict = nx.eigenvector_centrality(graph, max_iter=1000)

    metrics_data = []
    for char in graph.nodes():
        metrics_data.append({
            "角色名稱": char,
            "度中心性 (社交廣度)": round(degree_dict[char], 3),
            "中介中心性 (情節橋樑)": round(betweenness_dict[char], 3),
            "特徵向量中心性 (影響力)": round(eigenvector_dict[char], 3),
        })

    df = pd.DataFrame(metrics_data)
    return df.sort_values(by="特徵向量中心性 (影響力)", ascending=False).reset_index(drop=True)


def draw_relation_graph(graph: nx.Graph, output_path: Path = DEFAULT_GRAPH_OUTPUT_PATH) -> Path:
    plt.rcParams['font.sans-serif'] = ['Microsoft JhengHei']
    plt.rcParams['axes.unicode_minus'] = False
    figure = plt.figure(figsize=(7, 7))

    pos = nx.spring_layout(graph, k=0.8, seed=42)
    edges = graph.edges(data=True)
    weights = [d['weight'] * 3 for u, v, d in edges]

    nx.draw_networkx_nodes(graph, pos, node_size=1500, node_color='lightblue', alpha=0.9)
    nx.draw_networkx_labels(graph, pos, font_size=12, font_family='Microsoft JhengHei', font_weight='bold')
    nx.draw_networkx_edges(graph, pos, width=weights, edge_color='gray', alpha=0.6)

    edge_labels = nx.get_edge_attributes(graph, 'weight')
    nx.draw_networkx_edge_labels(graph, pos, edge_labels=edge_labels, font_size=10, font_color='red')

    plt.title("基於 CKIP 提取之故事人物關係網絡圖", fontsize=14, fontweight='bold')
    plt.axis('off')
    figure.savefig(output_path, bbox_inches="tight", dpi=150)
    plt.close(figure)
    return output_path


def main() -> None:
    print("正在載入 CKIP 模型...")
    ner_driver = create_ner_driver(device=-1)

    print("正在提取故事中的角色...")
    chapter_characters = extract_chapter_characters(story_chapters, ner_driver)

    print("\n=== 各章節角色提取結果 ===")
    for i, chars in enumerate(chapter_characters, 1):
        print(f"第 {i} 章出現角色: {chars}")

    graph = build_relation_graph(chapter_characters)
    graph_path = draw_relation_graph(graph)
    print(f"\n人物關係網絡圖已輸出: {graph_path}")

    metrics_df = analyze_character_centrality(graph)
    print("\n=== 計算敘事學：人物中心性定量分析表 ===")
    print(metrics_df.to_string())


if __name__ == "__main__":
    main()

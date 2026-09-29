import networkx as nx
import pandas as pd

from relation import build_edges_data, create_ner_driver, extract_chapter_characters, story_chapters


print("正在載入 CKIP 模型並產生人物關係資料...")
ner_driver = create_ner_driver(device=-1)
chapter_characters = extract_chapter_characters(story_chapters, ner_driver)
edges_data = build_edges_data(chapter_characters)

# 1. 使用 relation.py 產生的人物關係資料建立圖物件
G = nx.Graph()
for u, v, w in edges_data:
    G.add_edge(u, v, weight=w)

# 2. 計算各項嚴格的數學中心性指標
# 注意：在計算關係密度時，將邊的「權重（互動次數）」納入考量會更精準
degree_dict = nx.degree_centrality(G)
betweenness_dict = nx.betweenness_centrality(G, normalized=True)
eigenvector_dict = nx.eigenvector_centrality(G, max_iter=1000)

# 3. 將數據整理成乾淨的表格 (DataFrame)
metrics_data = []
for char in G.nodes():
    metrics_data.append({
        "角色名稱": char,
        "度中心性 (社交廣度)": round(degree_dict[char], 3),
        "中介中心性 (情節橋樑)": round(betweenness_dict[char], 3),
        "特徵向量中心性 (影響力)": round(eigenvector_dict[char], 3)
    })

df = pd.DataFrame(metrics_data)
# 依照特徵向量中心性進行排名
df = df.sort_values(by="特徵向量中心性 (影響力)", ascending=False).reset_index(drop=True)

# 4. 輸出量化分析結果
print("=== 計算敘事學：人物中心性定量分析表 ===")
print(df.to_string())

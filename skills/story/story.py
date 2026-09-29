import os
import itertools
from collections import defaultdict
import matplotlib.pyplot as plt
import networkx as nx
import pandas as pd
from snownlp import SnowNLP
from ckip_transformers.nlp import CkipWordSegmenter, CkipNerChunker

class ComputationalNarratologyApp:
    """計算敘事學大篇幅小說分析核心專案包"""
    
    def __init__(self, use_gpu=False):
        device = 0 if use_gpu else -1
        print(" [系統初始化] 正在載入中研院 CKIP Transformer 模型 (預設使用輕量版 albert-tiny)...")
        # 為了平衡運行大篇幅文本的速度，預設使用 albert-tiny，若求極致精準可改為 bert-base
        self.ner_driver = CkipNerChunker(model="albert-tiny", device=device)
        
        # 設定 Matplotlib 中文字體與負號顯示
        plt.rcParams['font.sans-serif'] = ['Microsoft JhengHei', 'Arial Unicode MS', 'SimHei']
        plt.rcParams['axes.unicode_minus'] = False
        
    def load_and_split_novel(self, file_path, chunk_size=2000):
        """
        匯入外部長篇小說文本，並按照字數動態切分成等長的『敘事片段/時間軸』
        :param file_path: 小說 .txt 檔案路徑
        :param chunk_size: 每個片段的字數（模擬章節，預設2000字為一個節點）
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"找不到小說檔案：{file_path}，請檢查路徑。")
            
        print(f" [文字讀取] 正在讀取文本：{file_path}")
        with open(file_path, "r", encoding="utf-8") as f:
            full_text = f.read().replace("\n", "").replace("\r", "").strip()
            
        # 動態切分文本
        chunks = [full_text[i:i + chunk_size] for i in range(0, len(full_text), chunk_size)]
        print(f" 成功匯入！全書共 {len(full_text)} 字，已自動切分為 {len(chunks)} 個敘事片段。")
        return chunks

    def analyze_narrative(self, chunks):
        """
        執行計算敘事學核心管線：NLP 提取、情感計算、網路建模
        """
        print(" [核心計算] 開始進行文本批次處理（此步驟依文本大小需耗時數分鐘）...")
        
        # 1. 批次執行 CKIP 實體辨識
        ner_results = self.ner_driver(chunks)
        
        # 2. 提取每章節的角色與計算情節情感
        chapter_characters = []
        timeline_sentiments = []
        
        for text, ner_res in zip(chunks, ner_results):
            # 提取當前片段出現的 PERSON 實體（去重複）
            names = {entity.word for entity in ner_res if entity.ner == 'PERSON'}
            chapter_characters.append(list(names))
            
            # 計算當前片段情感分數，並將 0~1 轉換為 -1~1
            s = SnowNLP(text)
            adjusted_score = (s.sentiments - 0.5) * 2
            timeline_sentiments.append(adjusted_score)
            
        # 3. 建立角色網絡與計算互動情感
        G = nx.Graph()
        for i, chars in enumerate(chapter_characters):
            if len(chars) < 2:
                continue
            pairs = itertools.combinations(sorted(chars), 2)
            current_sentiment = timeline_sentiments[i]
            
            for u, v in pairs:
                if G.has_edge(u, v):
                    G[u][v]['weight'] += 1
                    G[u][v]['total_sentiment'] += current_sentiment
                else:
                    G.add_edge(u, v, weight=1, total_sentiment=current_sentiment)
                    
        return G, chapter_characters, timeline_sentiments

    def generate_centrality_report(self, G):
        """計算三大中心性指標，產出嚴格的主角數學排定表"""
        print("\n [數據產出] 正在計算角色網絡中心性指標...")
        if len(G.nodes) == 0:
            print("網絡中無有效角色互動，無法生成報表。")
            return None
            
        degree = nx.degree_centrality(G)
        betweenness = nx.betweenness_centrality(G, normalized=True)
        eigenvector = nx.eigenvector_centrality(G, max_iter=1000)
        
        metrics = []
        for node in G.nodes():
            metrics.append({
                "角色名稱": node,
                "度中心性 (社交度)": round(degree[node], 3),
                "中介中心性 (橋樑度)": round(betweenness[node], 3),
                "特徵向量中心性 (影響力)": round(eigenvector[node], 3)
            })
            
        df = pd.DataFrame(metrics).sort_values(by="特徵向量中心性 (影響力)", ascending=False).reset_index(drop=True)
        return df

    def plot_character_network(self, G):
        """視覺化社會網絡圖（邊的粗細代表互動頻率）"""
        if len(G.edges) == 0:
            return
        plt.figure(figsize=(8, 8))
        pos = nx.spring_layout(G, k=0.6, seed=42)
        weights = [d['weight'] * 1.5 for u, v, d in G.edges(data=True)]
        
        nx.draw_networkx_nodes(G, pos, node_size=1200, node_color='lavender', alpha=0.9)
        nx.draw_networkx_labels(G, pos, font_size=11, font_weight='bold')
        nx.draw_networkx_edges(G, pos, width=weights, edge_color='gray', alpha=0.5)
        
        plt.title("長篇小說角色社交關係拓撲圖", fontsize=14, fontweight='bold')
        plt.axis('off')
        plt.show()

    def plot_dynamic_trajectories(self, chapter_characters, timeline_sentiments, target_characters):
        """
        繪製多個指定角色的動態情感交叉起伏曲線（情感雙人舞）
        :param target_characters: 想要觀測的角色清單，例如 ['張小明', '獨孤狼']
        """
        print(f"\n [繪圖監測] 正在追蹤目標角色 {target_characters} 的動態生命軌跡...")
        time_axis = [f"P{i+1}" for i in range(len(timeline_sentiments))]
        
        plt.figure(figsize=(12, 5.5))
        
        for char in target_characters:
            char_scores = []
            for i, chars in enumerate(chapter_characters):
                # 如果該片段有出現該角色，記錄當下情節情感值；無則記錄為 0 (中性)
                if char in chars:
                    char_scores.append(timeline_sentiments[i])
                else:
                    char_scores.append(0.0)
                    
            plt.plot(time_axis, char_scores, marker='o', label=char, linewidth=2)
            
        plt.axhline(0, color='black', linestyle=':', alpha=0.3)
        plt.title("目標角色群『動態敘事交織／情感雙人舞』軌跡圖", fontsize=14, fontweight='bold')
        plt.xlabel("小說推進時間軸 (動態切片進度)", fontsize=12)
        plt.ylabel("情感投射得分 (-1.0 負面/受難 ➡️ 1.0 正面/高光)", fontsize=12)
        plt.grid(True, linestyle=':', alpha=0.4)
        plt.legend()
        plt.show()

# ==========================================
# 示範如何直接呼叫此專案包進行小說健檢
# ==========================================
if __name__ == "__main__":
    # 建立測試用的小說文字檔案（若您有自己的小說，可跳過這段建立步驟，直接指定路徑）
    test_file = "my_epic_novel.txt"
    with open(test_file, "w", encoding="utf-8") as f:
        f.write(
            "第一階段，張小明和林小婷在桃花源過著無憂無慮的幸福時光。然而好景不長，反派血魔天尊突然降臨，"
            "血魔天尊殘忍地摧毀了桃花源，林小婷重傷昏迷，張小明痛苦不堪，誓言復仇。第二階段，張小明背著林小婷"
            "四處尋醫，在極度絕望時遇到了世外高人白鶴真人。白鶴真人用仙丹救醒了林小婷，並傳授張小明神功，"
            "兩人心中重新充滿了希望。第三階段，決戰爆發，張小明與白鶴真人聯手圍攻血魔天尊，血魔天尊眼見大勢已去，"
            "臨死前居然大徹大悟，散盡全身修為化作甘霖修復了大地，壯烈走向消亡。最終，張小明與林小婷重建了家園。"
        )

    # 實例化專案應用程式
    app = ComputationalNarratologyApp(use_gpu=False)
    
    try:
        # 1. 匯入小說並切片（設定每50字切一片以利測試，實際長篇小說建議設 2000~3000 字）
        story_chunks = app.load_and_split_novel(test_file, chunk_size=50)
        
        # 2. 執行計算敘事學管線
        graph, chap_chars, time_sentiments = app.analyze_narrative(story_chunks)
        
        # 3. 產出數據報表並列印
        report_df = app.generate_centrality_report(graph)
        print(report_df)
        
        # 4. 畫出人物社交圖
        app.plot_character_network(graph)
        
        # 5. 畫出主角與反派的命運交織雙人舞
        app.plot_dynamic_trajectories(chap_chars, time_sentiments, target_characters=["張小明", "血魔天尊"])
        
    finally:
        # 清理測試用的檔案
        if os.path.exists(test_file):
            os.remove(test_file)

from pathlib import Path

import matplotlib.pyplot as plt
from ckip_transformers.nlp import CkipNerChunker, CkipWordSegmenter
from snownlp import SnowNLP


DEFAULT_STORY_PATH = Path(__file__).resolve().with_name("story.txt")


def load_story_chapters(story_path: Path = DEFAULT_STORY_PATH) -> dict[str, str]:
    lines = [
        line.strip()
        for line in story_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return {f"第{index}章": text for index, text in enumerate(lines, start=1)}


story_chapters_zh = load_story_chapters()


def create_ckip_drivers(device: int = -1) -> tuple[CkipWordSegmenter, CkipNerChunker]:
    ws_driver = CkipWordSegmenter(model="bert-base", device=device)
    ner_driver = CkipNerChunker(model="bert-base", device=device)
    return ws_driver, ner_driver


def calculate_sentiment_scores(texts: list[str]) -> list[float]:
    sentiment_scores = []
    for text in texts:
        sentiment = SnowNLP(text).sentiments
        sentiment_scores.append((sentiment - 0.5) * 2)
    return sentiment_scores


def print_story_elements(
    chapters: list[str],
    story_ws: list[list[str]],
    story_ner,
) -> None:
    print("\n=== CKIP 敘事元素提取結果 ===")
    for chapter, ws, ner in zip(chapters, story_ws, story_ner):
        print(f"\n【{chapter}】")
        print(f"斷詞結果: {' / '.join(ws[:10])} ... (下略)")
        entities = [
            f"{entity.word}({entity.ner})"
            for entity in ner
            if entity.ner in ["PERSON", "GPE", "ORG", "LOC"]
        ]
        print(f"關鍵角色與場景: {', '.join(set(entities)) if entities else '無'}")


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

    chapters = list(story_chapters_zh.keys())
    texts = list(story_chapters_zh.values())

    print("正在進行 CKIP 斷詞與實體辨識...")
    story_ws = ws_driver(texts)
    story_ner = ner_driver(texts)
    print_story_elements(chapters, story_ws, story_ner)

    sentiment_scores = calculate_sentiment_scores(texts)
    draw_sentiment_curve(chapters, sentiment_scores)


if __name__ == "__main__":
    main()

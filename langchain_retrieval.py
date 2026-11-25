"""Experience retrieval on LangChain's vector store interface.

Selected with ``"retriever": "langchain"`` in config.local.json. The corpus
preparation is the same as ``search_experience.ExperienceRetriever``
(sections, narrative-only text, 320-token windows); the difference is where
vectors live and how they are queried:

- ``E5Embeddings`` is a LangChain ``Embeddings`` that adds the ``query:`` /
  ``passage:`` prefixes multilingual-E5 was trained with.
- chunks are LangChain ``Document``s in an ``InMemoryVectorStore``; scores are
  cosine similarities, which equal the classic dot product because the vectors
  are normalised, so the ranking is the same.
- ``as_retriever()`` returns a LangChain ``BaseRetriever`` that yields one
  Document per source section, for use inside chains and graphs.
"""
import sys
from typing import List

from search_experience import ExperienceRetriever, rank_sections

try:
    from langchain_core.documents import Document
    from langchain_core.embeddings import Embeddings
    from langchain_core.retrievers import BaseRetriever
    from langchain_core.vectorstores import InMemoryVectorStore
except ImportError as exc:  # pragma: no cover - reported to the caller
    raise ImportError("检索器 langchain 需要 langchain-core；请运行 python -m pip install -r requirements.txt") from exc


class E5Embeddings(Embeddings):
    """A loaded SentenceTransformer behind LangChain's Embeddings interface."""

    def __init__(self, model):
        self.model = model

    def _encode(self, texts):
        return self.model.encode(texts, normalize_embeddings=True, convert_to_numpy=True,
                                 show_progress_bar=False, batch_size=16).tolist()

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return self._encode(["passage: " + text for text in texts])

    def embed_query(self, text: str) -> List[float]:
        return self._encode(["query: " + text.strip()])[0]


class LangChainExperienceRetriever(ExperienceRetriever):
    """ExperienceRetriever whose vectors sit in a LangChain vector store.

    Same interface (build, search, basic_profile_candidates, chunks), so the
    evidence matcher does not know which one it holds."""
    framework = "langchain"

    def build(self):
        if self.model is not None:
            return self
        from search_experience import prepare_search_chunks, read_sections, select_sections
        records = select_sections(read_sections(self.data_dir), self.all_sections)
        if not records:
            raise ValueError("没有可检索的内容；请添加 UTF-8 的 .md 或 .txt 项目文件。")
        print("正在加载本地模型并检索（LangChain 向量库）……", file=sys.stderr)
        self.model = self._load_model()
        self.chunks = prepare_search_chunks(records, self.model.tokenizer)
        if not self.chunks:
            raise ValueError("资料没有可编码的文本。")
        texts = [f"{c['project']} | {c['section']}\n{c['search_keywords']}\n{c['text']}" for c in self.chunks]
        for text in texts:
            if len(self.model.tokenizer.encode("passage: " + text)) > self.model.max_seq_length:
                raise ValueError("查询或项目标题过长，请缩短查询/标题后重试。")
        self.embeddings = E5Embeddings(self.model)
        self.store = InMemoryVectorStore(self.embeddings)
        self.store.add_documents([Document(page_content=text, metadata={"index": i}) for i, text in enumerate(texts)],
                                 ids=[str(i) for i in range(len(texts))])
        return self

    def search(self, query, top_k=5):
        if self.model is None:
            self.build()
        if len(self.model.tokenizer.encode("query: " + query.strip())) > self.model.max_seq_length:
            raise ValueError("查询或项目标题过长，请缩短查询/标题后重试。")
        # Every chunk is scored so a section is ranked by its best window, as before.
        scores = [0.0] * len(self.chunks)
        for document, score in self.store.similarity_search_with_score(query, k=len(self.chunks)):
            scores[document.metadata["index"]] = score
        return rank_sections(self.chunks, scores, top_k)

    def as_retriever(self, top_k=5):
        return SectionRetriever(source=self, top_k=top_k)


class SectionRetriever(BaseRetriever):
    """LangChain retriever over whole experience sections; metadata keeps the
    project, section, source file, line range, score and section id needed
    for citations."""
    source: object
    top_k: int = 5

    def _get_relevant_documents(self, query, *, run_manager=None):
        return [Document(page_content=hit["text"],
                         metadata={key: hit[key] for key in ("project", "section", "source", "line_start", "line_end",
                                                             "score", "section_id", "matched_text")})
                for hit in self.source.search(query, self.top_k)]

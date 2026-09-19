# -*- coding: utf-8 -*-
"""
PaperIDE · 语义布局（概念网坐标）

来自 P0 的 `layout_semantic.py`（主人 09-16 晚点名要的），这里通用化：**接受任意 graph.json 路径**，
好让每篇论文都能算自己的坐标。

思路：每个概念 → 文本向量（字符 n-gram + LSA）→ 加上图拓扑相似度（共同邻居 Jaccard + 直接相连加分）
→ 合成距离矩阵 → MDS 降到 2D → 写回 nodes[].pos。
文本那一半保证"同族概念抱团"，拓扑那一半保证"有连线的不会被拆散"。
"""
import io
import json
import sys

import numpy as np
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.manifold import MDS
from sklearn.metrics.pairwise import cosine_similarity

W, H = 1500, 1000
SEM_W, TOPO_W = 0.55, 0.45


def layout_view(view):
    nodes, edges = view.get('nodes', []), view.get('edges', [])
    if len(nodes) < 3:
        return False
    ids = [n['id'] for n in nodes]
    idx = {v: i for i, v in enumerate(ids)}
    N = len(ids)

    docs = []
    for n in nodes:
        docs.append(' '.join([n.get('label', ''), n.get('short', ''), n.get('zh', ''),
                              (n.get('display') or '').replace('\n', ' ')]))
    X = TfidfVectorizer(analyzer='char_wb', ngram_range=(2, 4)).fit_transform(docs)
    k = max(2, min(8, X.shape[0] - 1, X.shape[1] - 1))
    E = TruncatedSVD(n_components=k, random_state=0).fit_transform(X)
    E = E / (np.linalg.norm(E, axis=1, keepdims=True) + 1e-9)
    sem_sim = cosine_similarity(E)

    adj = {i: set() for i in range(N)}
    for e in edges:
        a, b = idx.get(e['from']), idx.get(e['to'])
        if a is None or b is None:
            continue
        adj[a].add(b)
        adj[b].add(a)
    topo = np.zeros((N, N))
    for i in range(N):
        for j in range(N):
            if i == j:
                topo[i, j] = 1.0
                continue
            u = len(adj[i] | adj[j])
            topo[i, j] = (len(adj[i] & adj[j]) / u) if u else 0.0
    for e in edges:
        a, b = idx.get(e['from']), idx.get(e['to'])
        if a is None or b is None:
            continue
        topo[a, b] = topo[b, a] = min(1.0, topo[a, b] + 0.5)

    sim = SEM_W * sem_sim + TOPO_W * topo
    np.fill_diagonal(sim, 1.0)
    D = 1.0 - sim
    np.fill_diagonal(D, 0.0)
    D = (D + D.T) / 2.0

    pos = MDS(n_components=2, dissimilarity='precomputed', random_state=0,
              n_init=8, max_iter=600, normalized_stress='auto').fit_transform(D)
    pos -= pos.mean(axis=0)
    span = np.abs(pos).max(axis=0)
    span[span == 0] = 1.0
    pos /= span
    pos[:, 0] = W / 2 + pos[:, 0] * (W / 2 - 150)
    pos[:, 1] = H / 2 + pos[:, 1] * (H / 2 - 140)
    for i, n in enumerate(nodes):
        n['pos'] = [round(float(pos[i, 0]), 1), round(float(pos[i, 1]), 1)]
    return True


def run(graph_path, save=True):
    d = json.load(io.open(graph_path, encoding='utf-8'))
    n = 0
    for s in d.get('sections', []):
        for v in s.get('views', []):
            if v.get('type', 'concept-map') == 'concept-map' and layout_view(v):
                n += 1
    if save and n:
        json.dump(d, io.open(graph_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return n


if __name__ == '__main__':
    p = sys.argv[1] if len(sys.argv) > 1 else None
    if not p:
        print(__doc__)
        sys.exit(0)
    print('算了 %d 张概念网 → %s' % (run(p), p))

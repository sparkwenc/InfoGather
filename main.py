from sources import InfoSources
from storage import InfoStorage
from filters import InfoFilters


conf = {
    "https://rss.arxiv.org/rss/math.AG": "arXiv:Algebraic Geometry",
    "https://rss.arxiv.org/rss/math.NT": "arXiv:Number Theory",
    "https://rss.arxiv.org/rss/math.DG": "arXiv:Differential Geometry",
    "https://rss.arxiv.org/rss/math.RT": "arXiv:Representation Theory",
    # #
    # "https://rss.arxiv.org/rss/math.SG": "arXiv:Symplectic Geometry",
    # "https://rss.arxiv.org/rss/math.AC": "arXiv:Commutative Algebra",
    # "https://rss.arxiv.org/rss/math.MP": "arXiv:Mathematical Physics",
    # "https://rss.arxiv.org/rss/math.AT": "arXiv:Algebraic Topology",
    # "https://rss.arxiv.org/rss/math.KT": "arXiv:K-theory",
    # "https://rss.arxiv.org/rss/math.GT": "arXiv:Geometric Topology",
    # #
    # "https://rss.arxiv.org/rss/math.CV": "arXiv:Complex Variables",
    # "https://rss.arxiv.org/rss/math.DS": "arXiv:Dynamical Systems",
    # "https://rss.arxiv.org/rss/math.GR": "arXiv:Group Theory",
    # "https://rss.arxiv.org/rss/math.CO": "arXiv:Combinatorics",
    # "https://rss.arxiv.org/rss/math.CA": "arXiv:ODE",
    # "https://rss.arxiv.org/rss/math.AP": "arXiv:PDE",
    # #
    # "https://rss.arxiv.org/rss/math.ST": "arXiv:Statistics",
}

sources = InfoSources(conf)
with InfoStorage("entries.db") as storage:
    entries = sources.normalized_feeds_arxiv()

    storage.insert_to_db(entries)
    storage.export_entries("feb23.md", InfoFilters.filter_ingestion)

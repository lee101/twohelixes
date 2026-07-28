"""Working out which column a question means, with no model call and no torch.

`route.py` decides the chart *form* from phrases it recognises, and then has to
decide which column goes on the axis. Literal matching handles "compare bounce
rate across sources" - `bounce_rate` is in the sentence. It cannot handle "does
customer happiness drop when tickets take longer", where the column is
`satisfaction` and the word never appears, so the gate abstains and a model is
asked something an embedding already knows.

This is that lookup. `pybed` - the same static int8 512-dimension table `gobed`
uses - embeds text with no transformer forward pass: a token id indexes a row,
the rows are mean-pooled, done. About **40 microseconds** on CPU against a
second for a gateway round trip, and its only dependency is numpy, which is
what makes it usable at all here: the server runs on the CPython 3.12
environment the AOT binary embeds, and torch is not in it.

**What this is not.** The first version of this file classified the question's
*intent* - line vs bar vs scatter - by nearest labelled phrasing. Measured, it
did not work: mean-pooled static vectors are dominated by topic words, so "what
should I look at?" scored 0.47 against the time-series prototypes while "is bmi
linked to progression" scored 0.16 against the correlation ones. There is no
threshold between those, and no amount of prototype writing creates one.
Intent stays lexical, where it is precise.

Matching a phrase to a column name is the opposite case, and the same
measurement says so: "customer happiness" reaches `satisfaction` at 0.27 with
the next candidate at 0.03. Two rules keep it honest:

* **Only candidates of the right role are scored.** Given the whole question,
  the category word wins over the measure ("each site" beats "power draw"), so
  when resolving a measure only measures are candidates.
* **A score and a margin, both.** Close is not an answer: "which channel brings
  the most revenue" scores `amount` and `net_amount` within 0.04 of each other,
  which is the data telling us a person should choose, not us.

Absent model, absent pybed, or a load failure: `available()` is False and this
is invisible. It is an accelerator, never a dependency.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from twohelixes import config

log = logging.getLogger("twohelixes.pipeline.semantic")

# Calibrated in `tests/test_semantic.py` against the questions this product
# ships and the ones it must refuse. The margin does most of the work: raising
# the floor alone admits confident nonsense on a one-candidate frame.
ACCEPT_SCORE = 0.13
ACCEPT_MARGIN = 0.06

_lock = threading.Lock()
_state: dict[str, Any] = {"tried": False, "model": None}
# Column names repeat across every question about a dataset, so their vectors
# are worth keeping. Bounded because a user's own columns are unbounded.
_vectors: dict[str, Any] = {}
MAX_CACHED_VECTORS = 4096


def model_dir() -> str:
    return config.get("TWOHELIXES_EMBED_MODEL") or str(
        config.REPO_ROOT / "models" / "embed"
    )


def enabled() -> bool:
    return config.get_bool("TWOHELIXES_SEMANTIC_ROUTE", default=True)


def _load() -> None:
    if _state["tried"]:
        return
    _state["tried"] = True

    if not enabled():
        return
    try:
        from pybed import EmbedModel
    except ImportError:
        log.info("pybed is not installed; semantic column matching stays off")
        return

    try:
        _state["model"] = EmbedModel.from_dir(model_dir())
    except Exception:  # noqa: BLE001 - a missing model is configuration, not a fault
        log.info("no embedding model in %s; semantic matching stays off", model_dir())
        return

    log.info("semantic column matching ready (%s)", model_dir())


def available() -> bool:
    with _lock:
        _load()
        return _state["model"] is not None


def warm() -> None:
    """Pay the 16 MB load at boot rather than on somebody's first question."""
    if available():
        embed("warm")


def embed(text: str) -> Any:
    """L2-normalised embedding of one string, or None if the model is absent."""
    if not available():
        return None

    import numpy as np

    cached = _vectors.get(text)
    if cached is not None:
        return cached

    # Column names are snake_case and the tokenizer is WordPiece over English:
    # `net_amount` tokenizes far better as "net amount".
    vector = _state["model"].embed(text.replace("_", " "))
    norm = float(np.linalg.norm(vector))
    unit = vector / norm if norm else vector

    if len(_vectors) < MAX_CACHED_VECTORS:
        _vectors[text] = unit
    return unit


def resolve(question: str, candidates: list[str]) -> tuple[str, float] | None:
    """Which candidate the question is about, or None if it is not clear.

    `candidates` must already be narrowed by role - measures when a measure is
    wanted, categories when a category is. Scoring both together lets the
    grouping word beat the measured word, which is how "what is the power draw
    at each site" resolves to `site`.
    """
    if not question or len(candidates) < 1 or not available():
        return None

    query = embed(question)
    if query is None:
        return None

    scored = sorted(
        ((float(query @ embed(str(name))), str(name)) for name in candidates),
        reverse=True,
    )
    top_score, top_name = scored[0]
    if top_score < ACCEPT_SCORE:
        return None

    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if top_score - runner_up < ACCEPT_MARGIN:
        return None

    return top_name, top_score

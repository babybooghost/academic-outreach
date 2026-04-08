"""
Cross-draft similarity detection for the Academic Outreach Email System.

Uses scikit-learn TF-IDF vectorization and cosine similarity to detect
drafts that are too similar to each other within a session, helping ensure
each outreach email is sufficiently unique.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import List, Optional, Tuple

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity as sk_cosine_similarity

from app.config import Config
from app.database import get_connection, get_drafts, update_draft
from app.models import Draft

logger: logging.Logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core similarity computation
# ---------------------------------------------------------------------------

def compute_similarity_matrix(drafts: list[Draft]) -> list[list[float]]:
    """Compute pairwise cosine similarity between all draft bodies.

    Uses scikit-learn TfidfVectorizer to vectorize draft bodies and
    cosine_similarity to produce the pairwise matrix.

    Parameters
    ----------
    drafts : list[Draft]
        Drafts whose bodies will be compared.

    Returns
    -------
    list[list[float]]
        N x N similarity matrix where N = len(drafts). Each value is in
        [0.0, 1.0]. Returns an empty list if fewer than 2 drafts are given.
    """
    if len(drafts) < 2:
        # Return identity-style matrix for 0 or 1 drafts
        return [[1.0]] * len(drafts) if drafts else []

    bodies: list[str] = [draft.body for draft in drafts]

    # Filter out empty bodies -- TF-IDF cannot process them meaningfully
    non_empty_count: int = sum(1 for b in bodies if b.strip())
    if non_empty_count < 2:
        logger.warning(
            "Fewer than 2 non-empty draft bodies; returning identity matrix"
        )
        n: int = len(drafts)
        return [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]

    try:
        vectorizer: TfidfVectorizer = TfidfVectorizer(
            stop_words="english",
            max_features=5000,
            ngram_range=(1, 2),
            min_df=1,
        )
        tfidf_matrix = vectorizer.fit_transform(bodies)
        similarity: np.ndarray = sk_cosine_similarity(tfidf_matrix)

        # Convert numpy matrix to plain Python list[list[float]]
        return [
            [float(similarity[i, j]) for j in range(similarity.shape[1])]
            for i in range(similarity.shape[0])
        ]

    except ValueError as exc:
        logger.error("TF-IDF vectorization failed: %s", exc)
        n = len(drafts)
        return [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]


# ---------------------------------------------------------------------------
# Similarity scoring and flagging
# ---------------------------------------------------------------------------

def update_similarity_scores(
    drafts: list[Draft],
    config: Config,
) -> list[Draft]:
    """Compute and assign similarity scores to each draft.

    For each draft, similarity_score is set to the maximum cosine similarity
    with any OTHER draft in the list. Drafts exceeding
    config.generation.similarity_threshold receive a warning.

    Parameters
    ----------
    drafts : list[Draft]
        Drafts to evaluate. Modified in place and also returned.
    config : Config
        Application config providing the similarity threshold.

    Returns
    -------
    list[Draft]
        The same draft objects with similarity_score updated and warnings
        appended as needed.
    """
    if len(drafts) < 2:
        for draft in drafts:
            draft.similarity_score = 0.0
        return drafts

    threshold: float = config.generation.similarity_threshold
    matrix: list[list[float]] = compute_similarity_matrix(drafts)

    for i, draft in enumerate(drafts):
        # Max similarity to any other draft (exclude self at diagonal)
        max_sim: float = 0.0
        similar_count: int = 0

        for j in range(len(drafts)):
            if i == j:
                continue
            sim: float = matrix[i][j]
            if sim > max_sim:
                max_sim = sim
            if sim > threshold:
                similar_count += 1

        draft.similarity_score = round(max_sim, 4)

        # Add warning if above threshold
        if similar_count > 0:
            warning_msg: str = f"Too similar to {similar_count} other draft"
            if similar_count > 1:
                warning_msg += "s"

            current_warnings: list[str] = draft.warnings_list
            # Avoid duplicate warnings
            existing_sim_warnings: list[str] = [
                w for w in current_warnings if w.startswith("Too similar to")
            ]
            for old_w in existing_sim_warnings:
                current_warnings.remove(old_w)
            current_warnings.append(warning_msg)
            draft.warnings_list = current_warnings

    logger.info(
        "Updated similarity scores for %d drafts (threshold=%.2f)",
        len(drafts),
        threshold,
    )

    return drafts


# ---------------------------------------------------------------------------
# Pair detection
# ---------------------------------------------------------------------------

def find_similar_pairs(
    drafts: list[Draft],
    threshold: float,
) -> list[tuple[int, int, float]]:
    """Find all pairs of drafts with similarity above the given threshold.

    Parameters
    ----------
    drafts : list[Draft]
        Drafts to compare.
    threshold : float
        Minimum similarity score to include a pair.

    Returns
    -------
    list[tuple[int, int, float]]
        List of (draft_id_1, draft_id_2, similarity_score) tuples for pairs
        above the threshold. Each pair appears only once (i < j ordering).
    """
    if len(drafts) < 2:
        return []

    matrix: list[list[float]] = compute_similarity_matrix(drafts)
    pairs: list[tuple[int, int, float]] = []

    for i in range(len(drafts)):
        for j in range(i + 1, len(drafts)):
            sim: float = matrix[i][j]
            if sim >= threshold:
                draft_id_1: int = drafts[i].id if drafts[i].id is not None else i
                draft_id_2: int = drafts[j].id if drafts[j].id is not None else j
                pairs.append((draft_id_1, draft_id_2, round(sim, 4)))

    logger.info(
        "Found %d similar pairs among %d drafts (threshold=%.2f)",
        len(pairs),
        len(drafts),
        threshold,
    )

    return pairs


# ---------------------------------------------------------------------------
# Session-level batch processing
# ---------------------------------------------------------------------------

def compute_session_similarity(
    db_path: str,
    session_id: int,
    config: Config,
) -> int:
    """Load all drafts for a session, compute similarity scores, and persist.

    Parameters
    ----------
    db_path : str
        Path to the SQLite database.
    session_id : int
        Session whose drafts will be processed.
    config : Config
        Application config providing the similarity threshold.

    Returns
    -------
    int
        Count of drafts flagged as too similar (above threshold).
    """
    conn: Optional[sqlite3.Connection] = None
    flagged_count: int = 0

    try:
        conn = get_connection(db_path)
        drafts: list[Draft] = get_drafts(conn, session_id=session_id)

        if len(drafts) < 2:
            logger.info(
                "Session %d has %d draft(s); skipping similarity analysis",
                session_id,
                len(drafts),
            )
            return 0

        threshold: float = config.generation.similarity_threshold
        updated_drafts: list[Draft] = update_similarity_scores(drafts, config)

        for draft in updated_drafts:
            update_draft(conn, draft)
            if (
                draft.similarity_score is not None
                and draft.similarity_score > threshold
            ):
                flagged_count += 1

        logger.info(
            "Session %d: %d/%d drafts flagged for high similarity (threshold=%.2f)",
            session_id,
            flagged_count,
            len(drafts),
            threshold,
        )

        return flagged_count

    except sqlite3.Error:
        logger.exception(
            "Database error during similarity computation for session_id=%d",
            session_id,
        )
        raise
    finally:
        if conn is not None:
            conn.close()

"""Fake document corpora. 3-5 docs each, two revisions A (old) and B (intended)."""

from __future__ import annotations

import hashlib
import uuid

from .vectors import content_sha256, fixture_vector


def stable_point_id(corpus: str, doc_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"aftertrace/{corpus}/{doc_id}"))


def build_corpus(corpus: str, docs: list[tuple[str, str, str]]) -> dict:
    """docs: list of (doc_id, text_a, text_b). Returns manifest-like dict."""
    chunks = []
    for doc_id, text_a, text_b in docs:
        chunks.append(
            {
                "doc_id": doc_id,
                "point_id": stable_point_id(corpus, doc_id),
                "text_a": text_a,
                "text_b": text_b,
                "sha_a": content_sha256(text_a),
                "sha_b": content_sha256(text_b),
                "vec_a": fixture_vector(f"{corpus}:{doc_id}:A:{text_a}"),
                "vec_b": fixture_vector(f"{corpus}:{doc_id}:B:{text_b}"),
            }
        )
    return {"corpus": corpus, "chunks": chunks}


def corpus_s1() -> dict:
    return build_corpus(
        "sdk-docs-s1",
        [
            (
                "authentication",
                "Auth v1: API key in query param.",
                "Auth v2: API key in api-key header.",
            ),
            (
                "billing",
                "Billing v1: monthly invoices.",
                "Billing v2: usage-based metered billing.",
            ),
            (
                "search",
                "Search v1: keyword only.",
                "Search v2: hybrid semantic+keyword with rerank.",
            ),
            ("webhooks", "Webhooks v1: no retries.", "Webhooks v2: signed retries with backoff."),
        ],
    )


def corpus_s2() -> dict:
    # NEW fake corpus, different domain/ids, same fault shape.
    return build_corpus(
        "payments-s2",
        [
            ("refunds", "Refunds v1: manual review.", "Refunds v2: instant auto-refund under $50."),
            ("payouts", "Payouts v1: weekly batch.", "Payouts v2: daily instant payouts."),
            (
                "disputes",
                "Disputes v1: email support.",
                "Disputes v2: in-dashboard evidence upload.",
            ),
            ("ledger", "Ledger v1: CSV export.", "Ledger v2: real-time ledger API."),
            ("kyc", "KYC v1: manual docs.", "KYC v2: automated verification."),
        ],
    )


def corpus_s3() -> dict:
    # Same surface symptom, different cause. Reuse S1 shape but distinct corpus.
    return build_corpus(
        "sdk-docs-s3",
        [
            (
                "authentication",
                "Auth v1: API key in query param.",
                "Auth v2: API key in api-key header.",
            ),
            (
                "billing",
                "Billing v1: monthly invoices.",
                "Billing v2: usage-based metered billing.",
            ),
            (
                "search",
                "Search v1: keyword only.",
                "Search v2: hybrid semantic+keyword with rerank.",
            ),
        ],
    )


def canary_for(corpus_data: dict, doc_id: str) -> dict:
    for c in corpus_data["chunks"]:
        if c["doc_id"] == doc_id:
            return {
                "query_id": f"canary-{corpus_data['corpus']}-{doc_id}",
                "query_vector": c["vec_b"],
                "expected_point_id": c["point_id"],
                "expected_revision": "B",
                "expected_sha": c["sha_b"],
            }
    raise KeyError(doc_id)


def short_hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:10]

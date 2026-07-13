"""Windmill entrypoint for HF-013 pre-promotion validation."""
import json
from typing import Optional

from f.hermes_flow.candidate_ops.promote import prepare_promotion


def main(
    candidate_id: str,
    catalogue_yaml: str,
    test_results_json: str,
    candidate_capability_metadata: Optional[dict] = None,
) -> dict:
    return prepare_promotion(
        candidate_id,
        catalogue_yaml,
        json.loads(test_results_json),
        candidate_capability_metadata=candidate_capability_metadata,
    )

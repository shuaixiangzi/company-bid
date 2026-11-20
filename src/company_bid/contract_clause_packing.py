"""Reserve room for a complete indexed clause when building new model requests."""
import json


POLICY_KEY = "contract_window_packing"
WHOLE_CLAUSES = "whole_clauses_v1"


def block_cost(block):
    return len(json.dumps({key: value for key, value in block.items()
                           if key not in {"bbox", "image_ref", "illustration_source_use"}}, ensure_ascii=False))


def clause_reservations(index, document, blocks, char_limit, block_limit):
    """Count actual source blocks, including intervening page furniture, in each range."""
    ordered_ids = [ref["block_id"] for unit in document["units"] for ref in unit.get("blocks", [])]
    positions = {identity: i for i, identity in enumerate(ordered_ids)}
    costs = [0]
    for identity in ordered_ids:
        costs.append(costs[-1] + block_cost(blocks[identity]))
    reservations = {}
    ranges = list(index["entries"])
    for section in index["sections"]:
        for chapter in section["chapters"]:
            ranges.extend(chapter["items"])
    while ranges:
        clause = ranges.pop()
        if not clause["complete"]:
            continue
        start_id = clause["start"]["block_id"]
        start = positions[start_id]
        end = positions[clause["end"]["block_id"]] + 1
        count, cost = end - start, costs[end] - costs[start]
        if 1 < count <= block_limit and cost <= char_limit:
            reservations[start_id] = max(reservations.get(start_id, (0, 0)), (count, cost))
        elif count > block_limit or cost > char_limit:
            ranges.extend(clause.get("items", []))
    return reservations

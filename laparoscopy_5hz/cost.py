"""Account for observed API usage, including failed requests and resumed runs."""
from .common import read_json, save_json, AnnotationError
import math


def safe_usage(value):
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items()
            if k in {"prompt_tokens", "completion_tokens", "total_tokens"}
            and isinstance(v, int) and not isinstance(v, bool) and v >= 0}


def summarize(records):
    totals = {k: sum(safe_usage(r.get("usage")).get(k, 0) for r in records)
              for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
    unknown = sum(not {"prompt_tokens", "completion_tokens"} <=
                  safe_usage(r.get("usage")).keys() for r in records)
    return {**totals, "requests": len(records), "unknown_usage_requests": unknown,
            "usage_complete": unknown == 0}


def batch_records(batch):
    if "attempt_records" in batch:
        return batch["attempt_records"]
    # Older versions retained only the successful response's usage.
    records = list(batch.get("prior_attempts", []))
    attempts = batch.get("attempts", len(records)+1)
    if isinstance(attempts, int) and attempts > len(records)+1:
        records += [{"usage": {}} for _ in range(attempts-len(records)-1)]
    return records + [{"usage": batch.get("usage", {})}]


def append_metadata(path, meta):
    batches = []
    if path.exists():
        old = read_json(path)
        batches = old.get("request_batches", [old])
    batches.append(meta)
    records = [r for b in batches for r in batch_records(b)]
    save_json(path, {"request_batches": batches, "usage": summarize(records)})


def export_usage(output, price_in=None, price_out=None):
    records = []
    for path in sorted((output/"call_metadata").glob("*.json")):
        meta = read_json(path)
        for batch in meta.get("request_batches", [meta]):
            records += batch_records(batch)
    report = summarize(records)
    # Cached windows with no historical metadata are unknown, never zero-cost.
    missing = sum(not (output/"call_metadata"/p.name).exists()
                  for p in (output/"windows").glob("*.json"))
    report["windows_without_usage"] = missing
    report["usage_complete"] &= missing == 0
    report["note"] = "历史累计已知用量，含失败重试；未知用量未假定为零。金额仅按显式单价估算，非账单。"
    report.update(priced(report, price_in, price_out))
    if report["estimated_usd"] is not None:
        report["estimated_usd_is_lower_bound"] = not report["usage_complete"]
    save_json(output/"usage.json", report)
    return report


def priced(tokens, price_in, price_out):
    if price_in is None and price_out is None:
        return {"estimated_usd": None}
    if (price_in is None or price_out is None or
            any(not math.isfinite(x) or x < 0 for x in (price_in, price_out))):
        raise AnnotationError("必须同时提供非负有限 --price-in 和 --price-out（美元/M token）。")
    return {"price_in_usd_per_million": price_in, "price_out_usd_per_million": price_out,
            "estimated_usd": (tokens["prompt_tokens"]*price_in
                              + tokens["completion_tokens"]*price_out)/1e6}

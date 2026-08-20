"""Live Lambda/schedule/invocation health for the Infra dashboard page.

Read-only AWS introspection (Lambda config, EventBridge rule state,
CloudWatch metrics) -- no Postgres, no UC/SAP calls. Kept as its own
module rather than folded into dashboard_queries.py (Postgres-only)
since this is a different kind of I/O entirely.

boto3 clients constructed inline and untested here, matching the
existing alerts.py/credentials.py pattern in this repo -- AWS-calling
code stays thin and is verified live, not mocked.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

SERVICE_PREFIX = "unicommerce-b2c"

# Mirrors serverless.yml's functions: block. Kept as a small static list
# (not discovered via list_functions) so schedule/debug classification
# doesn't depend on guessing from name patterns.
SCHEDULED_FUNCTIONS = [
    "unicommerceFetch",
    "unicommercePostInvoices",
    "unicommercePostCreditNotes",
    "unicommercePoGrn",
    "unicommerceRecon",
]
HTTP_FUNCTIONS = ["unicommerceDashboardApi"]
# No debug/manual functions currently deployed -- kept as an empty list
# (not removed) so _function_type's "debug" classification still applies
# if one's ever added back.
DEBUG_FUNCTIONS: list[str] = []


def _function_type(key: str) -> str:
    if key in SCHEDULED_FUNCTIONS:
        return "scheduled"
    if key in DEBUG_FUNCTIONS:
        return "debug"
    return "http"


def _sum_datapoints(datapoints: list[dict]) -> int:
    return int(sum(p.get("Sum") or 0 for p in datapoints))


def fetch_infra_status(stage: str) -> dict[str, Any]:
    import boto3

    lambda_client = boto3.client("lambda")
    events_client = boto3.client("events")
    cw = boto3.client("cloudwatch")

    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=24)

    rows: list[dict[str, Any]] = []
    for key in SCHEDULED_FUNCTIONS + HTTP_FUNCTIONS + DEBUG_FUNCTIONS:
        fn_name = f"{SERVICE_PREFIX}-{stage}-{key}"
        row: dict[str, Any] = {
            "name": key,
            "type": _function_type(key),
            "schedule": None,
            "enabled": None,
            "invocations24h": 0,
            "errors24h": 0,
            "error": None,
        }

        try:
            cfg = lambda_client.get_function_configuration(FunctionName=fn_name)
            fn_arn = cfg["FunctionArn"]
        except Exception as exc:  # noqa: BLE001 -- surfaced to the dashboard, not swallowed
            row["error"] = str(exc)
            rows.append(row)
            continue

        if row["type"] == "scheduled":
            try:
                rule_names = events_client.list_rule_names_by_target(TargetArn=fn_arn).get("RuleNames") or []
                if rule_names:
                    rule = events_client.describe_rule(Name=rule_names[0])
                    row["schedule"] = rule.get("ScheduleExpression")
                    row["enabled"] = rule.get("State") == "ENABLED"
            except Exception as exc:  # noqa: BLE001 -- non-fatal, row still useful without it
                logger.warning("infra_status: schedule lookup failed for %s: %s", fn_name, exc)

        for metric_name, target_key in (("Invocations", "invocations24h"), ("Errors", "errors24h")):
            try:
                resp = cw.get_metric_statistics(
                    Namespace="AWS/Lambda",
                    MetricName=metric_name,
                    Dimensions=[{"Name": "FunctionName", "Value": fn_name}],
                    StartTime=start,
                    EndTime=now,
                    Period=86400,
                    Statistics=["Sum"],
                )
                row[target_key] = _sum_datapoints(resp.get("Datapoints") or [])
            except Exception as exc:  # noqa: BLE001 -- non-fatal, row still useful without it
                logger.warning("infra_status: %s lookup failed for %s: %s", metric_name, fn_name, exc)

        rows.append(row)

    return {"functions": rows, "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}

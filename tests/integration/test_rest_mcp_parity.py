import asyncio
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp.server import MCPServer

from doctask.durable import DurableProjectDeliveryService
from doctask.interfaces.app import create_application

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
CORPUS = Path(__file__).parents[2] / "fixtures" / "corpus"


def _call(mcp: MCPServer, name: str, arguments: dict[str, object]) -> dict[str, object]:
    result = asyncio.run(mcp.call_tool(name, arguments))
    assert result.is_error is False
    assert result.structured_content is not None
    return result.structured_content


@pytest.mark.skipif(DATABASE_URL is None, reason="TEST_DATABASE_URL is not configured")
def test_rest_and_mcp_share_the_same_workflow_and_response_contracts() -> None:
    with DurableProjectDeliveryService.connect(DATABASE_URL or "") as service:
        service.reset_for_test()
    app, mcp = create_application(database_url=DATABASE_URL or "")

    with TestClient(app) as client:
        created = client.post("/v1/corpora", json={"name": "Transport parity"})
        assert created.status_code == 201
        corpus_id = created.json()["corpusId"]
        uploaded = client.post(
            f"/v1/corpora/{corpus_id}/sources",
            files={
                "source": (
                    "project-plan.docx",
                    (CORPUS / "project-plan.docx").read_bytes(),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        assert uploaded.status_code == 201
        started = client.post(f"/v1/corpora/{corpus_id}/runs")
        assert started.status_code == 202
        rest_run = started.json()

        mcp_run = _call(mcp, "get_run_status", {"run_id": rest_run["runId"]})
        assert mcp_run == rest_run

        decision = {
            "proposalId": rest_run["proposals"][0]["proposalId"],
            "isApproved": True,
            "reason": "Grounded fixture accepted",
        }
        completed = _call(
            mcp,
            "submit_decisions",
            {"run_id": rest_run["runId"], "decisions": [decision]},
        )
        assert completed["status"] == "COMPLETED"

        rest_register = client.get(f"/v1/corpora/{corpus_id}/register").json()
        mcp_register = _call(
            mcp, "get_project_register", {"corpus_id": corpus_id}
        )
        assert mcp_register == rest_register

        protocol_headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        initialized = client.post(
            "/mcp/",
            headers=protocol_headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "parity-test", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200
        protocol_headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]
        protocol_headers["MCP-Protocol-Version"] = "2025-11-25"
        acknowledged = client.post(
            "/mcp/",
            headers=protocol_headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert acknowledged.status_code == 202
        protocol_register = client.post(
            "/mcp/",
            headers=protocol_headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "get_project_register",
                    "arguments": {"corpus_id": corpus_id},
                },
            },
        )
        assert protocol_register.status_code == 200
        assert protocol_register.json()["result"]["structuredContent"] == rest_register

        record_id = rest_register["records"][0]["recordId"]
        rest_evidence = client.get(
            f"/v1/corpora/{corpus_id}/evidence/{record_id}"
        ).json()
        mcp_evidence = _call(
            mcp,
            "get_record_evidence",
            {"corpus_id": corpus_id, "record_id": record_id},
        )
        assert mcp_evidence == rest_evidence

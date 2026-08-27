import os
from pathlib import Path

from doctask.interfaces.app import create_application
from doctask.model_adapters import OpenAICompatibleClaimAdapter

database_url = os.environ.get("DATABASE_URL")
if not database_url:
    raise RuntimeError("DATABASE_URL is required")

rules_path_value = os.environ.get("RULES_PATH")
rules_path = Path(rules_path_value) if rules_path_value else None
allowed_hosts_value = os.environ.get("MCP_ALLOWED_HOSTS")
allowed_hosts = (
    [value.strip() for value in allowed_hosts_value.split(",") if value.strip()]
    if allowed_hosts_value
    else None
)
model_values = {
    "base_url": os.environ.get("MODEL_BASE_URL"),
    "api_key": os.environ.get("MODEL_API_KEY"),
    "model": os.environ.get("MODEL_NAME"),
}
configured_model_values = [value for value in model_values.values() if value]
if configured_model_values and len(configured_model_values) != len(model_values):
    raise RuntimeError(
        "MODEL_BASE_URL, MODEL_API_KEY, and MODEL_NAME must be configured together"
    )
model_adapter = (
    OpenAICompatibleClaimAdapter(
        base_url=str(model_values["base_url"]),
        api_key=str(model_values["api_key"]),
        model=str(model_values["model"]),
        input_cost_per_million=float(os.environ.get("MODEL_INPUT_COST_PER_MILLION", "0")),
        output_cost_per_million=float(os.environ.get("MODEL_OUTPUT_COST_PER_MILLION", "0")),
    )
    if configured_model_values
    else None
)
app, mcp = create_application(
    database_url=database_url,
    rules_path=rules_path,
    mcp_allowed_hosts=allowed_hosts,
    model_adapter=model_adapter,
)

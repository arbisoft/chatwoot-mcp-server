import re
from pathlib import Path

from tests.conftest import tool_names

README = Path(__file__).resolve().parents[1] / "README.md"


def documented_tools() -> set[str]:
    """Every backticked name in the first column of the README's tool tables."""
    names: set[str] = set()
    for line in README.read_text().splitlines():
        if line.startswith("| `"):
            first_cell = line.split("|")[1]
            names.update(re.findall(r"`([a-z_]+)`", first_cell))
    return names


async def test_the_readme_lists_exactly_the_tools_that_exist(make_client):
    registered = await tool_names(make_client(CHATWOOT_MCP_ENABLE_WRITES="true", CHATWOOT_MCP_ENABLE_ACTIONS="true"))
    assert documented_tools() == registered

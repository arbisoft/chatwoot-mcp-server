"""Help centre: ``search_help_articles``. Articles are written by the team and returned as they are, cut to the
configured length. Drafts and archived articles are included; their ``status`` says which."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from chatwoot_mcp.client import ChatwootError, to_tool_error
from chatwoot_mcp.client.models import Article
from chatwoot_mcp.shaping import compact
from chatwoot_mcp.tiers import Tier
from chatwoot_mcp.tools.context import READ, ToolContext, as_dict, limited

MODULE = "help_center"


def register(mcp: FastMCP, context: ToolContext) -> None:
    @mcp.tool(**context.tool("Search help articles", READ, module=MODULE, tier=Tier.AGENT))
    async def search_help_articles(
        query: Annotated[str, Field(min_length=1, max_length=200, description="Words to find in the articles.")],
        page: Annotated[int, Field(ge=1, description="Page number, starting at 1.")] = 1,
    ) -> dict[str, Any] | str:
        """Search the help-centre articles, to answer from what the company has written. 15 per page.

        Drafts and archived articles are included; check `status` before quoting one to a customer.
        """
        try:
            session = await context.session()
            data = as_dict(await session.get("/search/articles", {"q": query, "page": page}))
            found = Article.parse_rows(as_dict(data.get("payload")).get("articles"))
        except ChatwootError as error:
            return to_tool_error(error)
        items, cut = limited(found, context.max_items)
        out: dict[str, Any] = {
            "page": page,
            "articles": [
                compact(
                    {
                        **article.model_dump(include=set(Article.model_fields) - {"content"}),
                        "content": (article.content or "")[: context.max_chars],
                    }
                )
                for article in items
            ],
        }
        if cut:
            out["truncated"] = f"Only the first {context.max_items} items are shown."
        return out

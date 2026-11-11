"""Help centre: ``search_help_articles``. Articles are written by the team and returned as they are, cut to the
configured length. Drafts and archived articles are included; their ``status`` says which."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from chatwoot_mcp.client import ChatwootError, to_tool_error
from chatwoot_mcp.client.models import Article
from chatwoot_mcp.shaping import compact, truncate
from chatwoot_mcp.tiers import Tier
from chatwoot_mcp.tools.context import READ, Page, ToolContext, as_dict

MODULE = "help_center"


def register(mcp: FastMCP, context: ToolContext) -> None:
    @mcp.tool(**context.tool("Search help articles", READ, module=MODULE, tier=Tier.AGENT))
    async def search_help_articles(
        query: Annotated[str, Field(min_length=1, max_length=200, description="Words to find in the articles.")],
        page: Page = 1,
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

        def shaped(article: Article) -> dict[str, Any]:
            content = truncate(article.content or "", context.max_chars)
            return compact(
                {
                    **article.model_dump(include=set(Article.model_fields) - {"content"}),
                    "content": content.text,
                    "truncated": content.truncated or None,
                }
            )

        return {"page": page, "articles": [shaped(article) for article in found]}

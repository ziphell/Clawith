"""Resource discovery — search Smithery & ModelScope registries and import MCP servers."""

import uuid
import httpx
from sqlalchemy import select
from app.database import async_session
from app.models.tool import Tool, AgentTool


# ── Smithery Registry Search ────────────────────────────────────

SMITHERY_API_BASE = "https://registry.smithery.ai"
MODELSCOPE_API_BASE = "https://modelscope.cn"


async def _get_smithery_api_key() -> str:
    """Read Smithery API key from discover_resources or import_mcp_server tool config."""
    try:
        async with async_session() as db:
            for tool_name in ("discover_resources", "import_mcp_server"):
                r = await db.execute(select(Tool).where(Tool.name == tool_name))
                tool = r.scalar_one_or_none()
                if tool and tool.config and tool.config.get("smithery_api_key"):
                    return tool.config["smithery_api_key"]
    except Exception:
        pass
    return ""


async def _search_smithery_api(query: str, max_results: int, api_key: str) -> list[dict]:
    """Search Smithery registry, returns normalized results."""
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                f"{SMITHERY_API_BASE}/servers",
                params={"q": query, "pageSize": max_results},
                headers=headers,
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
        results = []
        for srv in data.get("servers", [])[:max_results]:
            results.append({
                "name": srv.get("qualifiedName", ""),
                "display_name": srv.get("displayName", ""),
                "description": srv.get("description", "")[:200],
                "remote": srv.get("remote", False),
                "verified": srv.get("verified", False),
                "use_count": srv.get("useCount", 0),
                "homepage": srv.get("homepage", ""),
                "source": "Smithery",
            })
        return results
    except Exception:
        return []


async def _get_modelscope_api_token() -> str:
    """Read ModelScope API token from discover_resources tool config."""
    try:
        async with async_session() as db:
            for tool_name in ("discover_resources", "import_mcp_server"):
                r = await db.execute(select(Tool).where(Tool.name == tool_name))
                tool = r.scalar_one_or_none()
                if tool and tool.config and tool.config.get("modelscope_api_token"):
                    return tool.config["modelscope_api_token"]
    except Exception:
        pass
    return ""


async def _search_modelscope_api(query: str, max_results: int) -> list[dict]:
    """Search ModelScope MCP Hub via official OpenAPI (no WAF issues)."""
    api_token = await _get_modelscope_api_token()
    if not api_token:
        return []  # Silently skip if no token configured

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_token}",
        "Cookie": f"m_session_id={api_token}",
        "User-Agent": "modelscope-mcp-server/1.0",
    }
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.put(
                f"{MODELSCOPE_API_BASE}/openapi/v1/mcp/servers",
                json={"page_size": max_results, "page_number": 1, "search": query, "filter": {}},
                headers=headers,
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            if not data.get("success"):
                return []

        servers_data = data.get("data", {}).get("mcp_server_list", [])
        if not servers_data:
            return []

        results = []
        for srv in servers_data[:max_results]:
            server_id = srv.get("id", "")
            results.append({
                "name": server_id,
                "display_name": srv.get("name", server_id),
                "description": srv.get("description", "")[:200],
                "remote": srv.get("is_hosted", False),
                "verified": True,
                "use_count": 0,
                "homepage": f"https://modelscope.cn/mcp/servers/{server_id}",
                "source": "ModelScope",
            })
        return results
    except Exception as e:
        print(f"[ResourceDiscovery] ModelScope search failed: {e}")
        return []


async def search_registries(query: str, max_results: int = 5) -> str:
    """Search both Smithery and ModelScope for MCP servers."""
    api_key = await _get_smithery_api_key()

    # Search both registries in parallel
    import asyncio
    smithery_task = _search_smithery_api(query, max_results, api_key)
    modelscope_task = _search_modelscope_api(query, max_results)
    smithery_results, modelscope_results = await asyncio.gather(smithery_task, modelscope_task)

    # Merge: Smithery first, then ModelScope (deduplicate by name)
    seen_names = set()
    all_results = []
    for r in smithery_results + modelscope_results:
        if r["name"] not in seen_names:
            seen_names.add(r["name"])
            all_results.append(r)

    if not all_results:
        return f'🔍 No MCP servers found for "{query}" on Smithery or ModelScope. Try different keywords.'

    results = []
    for i, srv in enumerate(all_results[:max_results], 1):
        verified = " ✅" if srv["verified"] else ""
        source_tag = f"[{srv['source']}]"
        if srv["remote"]:
            deploy_info = "🌐 Remote (no local install needed)"
        else:
            deploy_info = "💻 Local install required"
        use_info = f" · 👥 {srv['use_count']:,} users" if srv["use_count"] else ""
        hp = srv['homepage']

        results.append(
            f"**{i}. {srv['display_name']}**{verified} {source_tag}\n"
            f"   ID: `{srv['name']}`\n"
            f"   {srv['description']}\n"
            f"   {deploy_info}{use_info}\n"
            f"   {'🔗 ' + hp if hp else ''}"
        )

    header = f'🔍 Found {len(results)} MCP server(s) for "{query}":\n\n'
    footer = (
        "\n\n---\n"
        "💡 To import a remote server, use `import_mcp_server` with the server ID.\n"
        '   Example: import_mcp_server(server_id="gmail")'
    )
    return header + "\n\n".join(results) + footer


# Keep backward-compatible alias
async def search_smithery(query: str, max_results: int = 5) -> str:
    return await search_registries(query, max_results)


# ── Import MCP Server ───────────────────────────────────────────

async def import_mcp_from_smithery(
    server_id: str,
    agent_id: uuid.UUID,
    config: dict | None = None,
) -> str:
    """Import an MCP server from Smithery into the platform.

    Uses the Smithery Registry detail API to get tool definitions,
    and stores the deploymentUrl for runtime execution via Smithery Connect.
    """
    api_key = await _get_smithery_api_key()
    if not api_key:
        return (
            "❌ Smithery API key is required to import MCP servers. "
            "Please configure it in Enterprise Settings → Tools → Resource Discovery → Smithery API Key.\n"
            "Get your key at: https://smithery.ai/account/api-keys"
        )

    # Step 1: Search for server by ID
    headers = {"Accept": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                f"{SMITHERY_API_BASE}/servers",
                params={"q": server_id.lstrip("@"), "pageSize": 5},
                headers=headers,
            )
            if resp.status_code != 200:
                return f"❌ Server '{server_id}' not found on Smithery (HTTP {resp.status_code})"
            data = resp.json()
            servers = data.get("servers", [])
            server_info = None
            clean_id = server_id.lstrip("@")
            for s in servers:
                if s.get("qualifiedName") == clean_id or s.get("qualifiedName") == server_id:
                    server_info = s
                    break
            if not server_info and servers:
                server_info = servers[0]
            if not server_info:
                return f"❌ Server '{server_id}' not found on Smithery."
    except Exception as e:
        return f"❌ Failed to fetch server info: {str(e)[:200]}"

    display_name = server_info.get("displayName", server_id.split("/")[-1])
    description = server_info.get("description", "")
    qualified_name = server_info.get("qualifiedName", server_id.lstrip("@"))

    # Check if server supports remote hosting
    if not server_info.get("remote"):
        return (
            f"⚠️ **{display_name}** (`{qualified_name}`) does not support remote hosting via Smithery Connect.\n"
            f"This server requires local installation and cannot be imported automatically.\n"
            f"🔗 {server_info.get('homepage', '')}"
        )

    # Step 2: Get full server details including tools from registry API
    tools_discovered = []
    deployment_url = None
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            detail_resp = await client.get(
                f"{SMITHERY_API_BASE}/servers/{qualified_name}",
                headers=headers,
            )
            if detail_resp.status_code == 200:
                detail = detail_resp.json()
                deployment_url = detail.get("deploymentUrl")
                raw_tools = detail.get("tools", [])
                tools_discovered = [
                    {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "inputSchema": t.get("inputSchema", {}),
                    }
                    for t in raw_tools if t.get("name")
                ]
                print(f"[ResourceDiscovery] Got {len(tools_discovered)} tools from registry for {qualified_name}")
            else:
                print(f"[ResourceDiscovery] Could not fetch detail for {qualified_name}: HTTP {detail_resp.status_code}")
    except Exception as e:
        print(f"[ResourceDiscovery] Could not fetch server detail: {e}")

    # Step 3: Determine the MCP server URL for runtime execution
    # deploymentUrl is the actual MCP server (e.g. https://github.run.tools)
    # We store this as the base URL; at runtime, Smithery Connect creates a connection with it
    base_mcp_url = deployment_url or f"https://{qualified_name}.run.tools"

    async with async_session() as db:
        imported_tools = []

        # Helper: ensure AgentTool link exists and save config
        async def _ensure_agent_tool(tool_id: uuid.UUID):
            agent_check = await db.execute(
                select(AgentTool).where(
                    AgentTool.agent_id == agent_id,
                    AgentTool.tool_id == tool_id,
                )
            )
            at = agent_check.scalar_one_or_none()
            if at:
                if config:
                    at.config = {**(at.config or {}), **config}
            else:
                db.add(AgentTool(
                    agent_id=agent_id, tool_id=tool_id, enabled=True,
                    source="user_installed", installed_by_agent_id=agent_id,
                    config=config or {},
                ))

        # On re-import with config: update ALL existing tools for this server
        if config:
            existing_server_tools_r = await db.execute(
                select(Tool).where(Tool.mcp_server_name == display_name, Tool.type == "mcp")
            )
            for et in existing_server_tools_r.scalars().all():
                et.mcp_server_url = base_mcp_url
                await _ensure_agent_tool(et.id)

        if tools_discovered:
            # Clean up old generic entry if individual tools are now discovered
            generic_name = f"mcp_{server_id.replace('/', '_').replace('@', '')}"
            old_generic_r = await db.execute(select(Tool).where(Tool.name == generic_name))
            old_generic = old_generic_r.scalar_one_or_none()
            if old_generic:
                await db.execute(
                    AgentTool.__table__.delete().where(AgentTool.tool_id == old_generic.id)
                )
                await db.delete(old_generic)
                await db.flush()

            # Create one Tool record per MCP tool
            for mcp_tool in tools_discovered:
                tool_name = f"mcp_{server_id.replace('/', '_').replace('@', '')}_{mcp_tool['name']}"
                tool_display = f"{display_name}: {mcp_tool['name']}"

                existing_r = await db.execute(select(Tool).where(Tool.name == tool_name))
                existing_tool = existing_r.scalar_one_or_none()
                if existing_tool:
                    existing_tool.mcp_server_url = base_mcp_url
                    await _ensure_agent_tool(existing_tool.id)
                    if config:
                        imported_tools.append(f"🔄 {tool_display} (config updated)")
                    else:
                        imported_tools.append(f"⏭️ {tool_display} (already imported)")
                    continue

                tool = Tool(
                    name=tool_name,
                    display_name=tool_display,
                    description=mcp_tool.get("description", description)[:500],
                    type="mcp",
                    category="mcp",
                    icon="🔌",
                    parameters_schema=mcp_tool.get("inputSchema", {"type": "object", "properties": {}}),
                    mcp_server_url=base_mcp_url,
                    mcp_server_name=display_name,
                    mcp_tool_name=mcp_tool["name"],
                    enabled=True,
                    is_default=False,
                )
                db.add(tool)
                await db.flush()
                await _ensure_agent_tool(tool.id)
                imported_tools.append(f"✅ {tool_display}")
        else:
            # Fallback: create a single generic tool entry
            tool_name = f"mcp_{server_id.replace('/', '_').replace('@', '')}"
            tool_display = display_name

            existing_r = await db.execute(select(Tool).where(Tool.name == tool_name))
            existing_tool = existing_r.scalar_one_or_none()
            if existing_tool:
                existing_tool.mcp_server_url = base_mcp_url
                await _ensure_agent_tool(existing_tool.id)
                if config:
                    await db.commit()
                    return f"🔄 {tool_display} config updated. The tool is now ready to use."
                else:
                    return f"⏭️ {tool_display} is already imported."

            tool = Tool(
                name=tool_name,
                display_name=tool_display,
                description=description[:500] or f"MCP Server: {server_id}",
                type="mcp",
                category="mcp",
                icon="🔌",
                parameters_schema={"type": "object", "properties": {}},
                mcp_server_url=base_mcp_url,
                mcp_server_name=display_name,
                enabled=True,
                is_default=False,
            )
            db.add(tool)
            await db.flush()
            await _ensure_agent_tool(tool.id)
            imported_tools.append(f"✅ {tool_display} (tool list not available from registry — may need configuration)")

        await db.commit()

    result = f"🔌 Imported MCP server: **{display_name}** (`{server_id}`)\n\n"
    result += "\n".join(imported_tools)
    result += f"\n\n📡 MCP Server URL: `{base_mcp_url}`"
    result += "\n\n💡 The imported tools are now available for use."
    return result

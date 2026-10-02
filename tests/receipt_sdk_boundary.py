"""Installed SDK4 callback serialization, with an in-process RPC sink (no model)."""
from types import SimpleNamespace

async def sdk_tool_response(tools, name, arguments, call_id):
    from band.adapters.codex import CodexAdapter, CodexAdapterConfig
    replies = []
    async def respond(identifier, body):
        replies.append((identifier, body))
    sdk = CodexAdapter(config=CodexAdapterConfig(model='fixture'), emit=set())
    sdk._room_client('r')
    sdk._active_room.set('r')
    sdk._client = SimpleNamespace(respond=respond)
    tools.call_id = call_id
    event = SimpleNamespace(id=call_id, method='item/tool/call',
        params={'tool': name, 'arguments': arguments, 'callId': call_id})
    await sdk._handle_server_request(tools=tools, msg=None, room_id='r', event=event)
    assert len(replies) == 1
    return replies[0][1]

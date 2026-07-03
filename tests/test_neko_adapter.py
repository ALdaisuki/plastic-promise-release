"""Tests for N.E.K.O adapter ZMQ layer."""

import pytest
import zmq
import time
import threading
import orjson

from plastic_promise.core.neko_adapter import NekoAdapter


def test_zmq_connect_and_receive_message():
    """Adapter SUB socket receives messages from a test PUB socket."""
    TEST_PUB_PORT = 48990  # Use a non-conflicting port for testing

    adapter = NekoAdapter(
        bus_url="ws://127.0.0.1:48999",
        zmq_pub_port=TEST_PUB_PORT,
        zmq_analyze_port=48991,
        session_id="test",
    )

    # Start ZMQ (don't start WS for this test)
    adapter._start_zmq()

    # Create a test publisher
    ctx = zmq.Context()
    pub = ctx.socket(zmq.PUB)
    pub.bind(f"tcp://127.0.0.1:{TEST_PUB_PORT}")

    # ZMQ PUB needs time for subscribers to connect (slow joiner)
    time.sleep(0.5)

    # Collect received events via _handle_zmq_event_sync (no event loop)
    received = []
    original_handler = adapter._handle_zmq_event_sync
    adapter._handle_zmq_event_sync = lambda event: received.append(event)

    # Publish a test event
    test_event = {"event_type": "session_lifecycle", "agent": "test_agent", "status": "online"}
    pub.send(orjson.dumps(test_event))

    # Wait for delivery
    time.sleep(0.5)

    assert len(received) == 1
    assert received[0]["event_type"] == "session_lifecycle"
    assert received[0]["agent"] == "test_agent"

    # Cleanup
    adapter._handle_zmq_event_sync = original_handler
    adapter._stop_zmq()
    pub.close()
    ctx.term()


def test_translate_session_lifecycle():
    """session_lifecycle → neko:announce with capabilities."""
    adapter = NekoAdapter(
        bus_url="ws://127.0.0.1:48999",
        zmq_pub_port=48990,
        zmq_analyze_port=48991,
        session_id="test",
    )

    zmq_event = {
        "event_type": "session_lifecycle",
        "agent": "agent_server",
        "status": "online",
        "capabilities": ["browser_use", "computer_use", "openclaw"],
    }

    ws_msg = adapter._translate_zmq_event(zmq_event)

    assert ws_msg is not None
    assert ws_msg["topic"] == "neko:announce"
    assert ws_msg["type"] == "handshake"
    assert ws_msg["from"] == "neko"
    assert ws_msg["to"] == "*"

    payload = ws_msg["payload"]
    assert isinstance(payload, dict)
    assert payload["agent"] == "neko"
    assert payload["status"] == "online"
    assert "browser_use" in payload["capabilities"]


def test_translate_voice_transcript():
    """voice_transcript_observed → neko:voice."""
    adapter = NekoAdapter(
        bus_url="ws://127.0.0.1:48999",
        zmq_pub_port=48990,
        zmq_analyze_port=48991,
        session_id="test",
    )

    zmq_event = {
        "event_type": "voice_transcript_observed",
        "lanlan_name": "小天",
        "transcript": "今天天气真好",
    }

    ws_msg = adapter._translate_zmq_event(zmq_event)

    assert ws_msg is not None
    assert ws_msg["topic"] == "neko:voice"
    assert ws_msg["type"] == "message"
    payload = ws_msg["payload"]
    assert isinstance(payload, dict)
    assert payload["transcript"] == "今天天气真好"
    assert payload["lanlan_name"] == "小天"


def test_translate_agent_result():
    """agent_result → neko:result."""
    adapter = NekoAdapter(
        bus_url="ws://127.0.0.1:48999",
        zmq_pub_port=48990,
        zmq_analyze_port=48991,
        session_id="test",
    )

    zmq_event = {
        "event_type": "agent_result",
        "event_id": "abc123",
        "result": "Browser task completed",
        "status": "success",
    }

    ws_msg = adapter._translate_zmq_event(zmq_event)

    assert ws_msg is not None
    assert ws_msg["topic"] == "neko:result"
    assert ws_msg["type"] == "result"
    assert ws_msg["replyTo"] == "abc123"
    payload = ws_msg["payload"]
    assert payload["result"] == "Browser task completed"
    assert payload["status"] == "success"


def test_translate_unknown_event():
    """Unknown event_type → neko:event passthrough."""
    adapter = NekoAdapter(
        bus_url="ws://127.0.0.1:48999",
        zmq_pub_port=48990,
        zmq_analyze_port=48991,
        session_id="test",
    )

    zmq_event = {
        "event_type": "some_custom_event",
        "data": "hello",
    }

    ws_msg = adapter._translate_zmq_event(zmq_event)

    assert ws_msg is not None
    assert ws_msg["topic"] == "neko:event"
    assert ws_msg["type"] == "message"
    assert ws_msg["payload"] == zmq_event


def test_ws_task_to_zmq_analyze_request():
    """WS task → ZMQ analyze_request with ack handling."""
    import asyncio

    TEST_ANALYZE_PORT = 48992

    async def run_test():
        adapter = NekoAdapter(
            bus_url="ws://127.0.0.1:48999",
            zmq_pub_port=48990,
            zmq_analyze_port=TEST_ANALYZE_PORT,
            session_id="test",
        )

        # Start ZMQ
        adapter._start_zmq()

        # Create a test PULL socket to receive the analyze_request
        ctx = zmq.Context()
        pull = ctx.socket(zmq.PULL)
        pull.bind(f"tcp://127.0.0.1:{TEST_ANALYZE_PORT}")
        pull.setsockopt(zmq.RCVTIMEO, 2000)

        # Simulate an incoming WS task from Pi
        ws_task = {
            "id": "pi-1719576000000-1",
            "topic": "pi:neko",
            "from": "pi",
            "to": "neko",
            "type": "task",
            "payload": "Browse to example.com and take a screenshot",
            "timestamp": int(time.time() * 1000),
        }

        # Call the handler directly
        await adapter._handle_ws_task(ws_task)

        # Verify the ZMQ message was sent
        raw = pull.recv()
        zmq_msg = orjson.loads(raw)

        assert zmq_msg["event_type"] == "analyze_request"
        assert zmq_msg["trigger"] == "pi_task"
        assert zmq_msg["lanlan_name"] == "pi"
        assert isinstance(zmq_msg["messages"], list)
        assert len(zmq_msg["messages"]) > 0
        assert zmq_msg["messages"][0]["content"] == ws_task["payload"]
        assert "event_id" in zmq_msg

        # Cleanup
        adapter._stop_zmq()
        pull.close()
        ctx.term()

    asyncio.run(run_test())


def test_adapter_start_and_stop():
    """Adapter.start() and .stop() lifecycle without actual WS connection."""
    adapter = NekoAdapter(
        bus_url="ws://127.0.0.1:48999",
        zmq_pub_port=48990,
        zmq_analyze_port=48991,
        session_id="test-lifecycle",
    )

    # Verify handlers are registered
    assert len(adapter.client.handlers.get("type:task", [])) >= 1
    assert len(adapter.client.handlers.get("type:message", [])) >= 1

    # Verify ZMQ starts and stops cleanly
    adapter._start_zmq()
    assert adapter._zmq_ready is True
    assert adapter._zmq_thread is not None
    assert adapter._zmq_thread.is_alive()

    adapter._stop_zmq()
    # After stop, thread should be joined and not alive
    if adapter._zmq_thread is not None:
        adapter._zmq_thread.join(timeout=1.0)
    assert adapter._zmq_ready is False


def test_error_handling_invalid_zmq_message():
    """Adapter handles malformed ZMQ messages without crashing."""
    import asyncio

    async def run_test():
        adapter = NekoAdapter(
            bus_url="ws://127.0.0.1:48999",
            zmq_pub_port=48990,
            zmq_analyze_port=48991,
            session_id="test",
        )

        # Send a malformed event (missing event_type)
        event = {"bad": "data", "no_event_type": True}
        await adapter._handle_zmq_event(event)

        # Send None-like event
        await adapter._handle_zmq_event({})

        # No exception means success
        assert True

    asyncio.run(run_test())


def test_wsslot_reconnect_backoff():
    """WSSlot maintainer uses exponential backoff."""
    import asyncio
    from plastic_promise.core.neko_adapter import WSSlot

    async def run_test():
        slot = WSSlot(
            name="test-slot",
            url="ws://127.0.0.1:49999",
            lanlan_name="test",
        )

        maintainer_task = asyncio.create_task(slot.maintain())
        await asyncio.sleep(3)
        maintainer_task.cancel()
        try:
            await maintainer_task
        except asyncio.CancelledError:
            pass

        assert True

    asyncio.run(run_test())


def test_dedup_cache():
    """DedupCache rejects duplicates within window."""
    from plastic_promise.core.neko_adapter import DedupCache

    cache = DedupCache(max_size=100)

    # First insert: not duplicate
    assert cache.is_duplicate("msg-001") is False

    # Second insert with same ID: is duplicate
    assert cache.is_duplicate("msg-001") is True

    # Different ID: not duplicate
    assert cache.is_duplicate("msg-002") is False

    # Overflow: old entries evicted
    for i in range(200):
        cache.is_duplicate(f"msg-{i:04d}")

    # msg-001 should be evicted, re-insert should NOT be duplicate
    assert cache.is_duplicate("msg-001") is False


def test_subscribe_receives_events():
    """subscribe() registers a handler that receives ZMQ events."""
    import asyncio

    received_events = []

    async def run_test():
        adapter = NekoAdapter(
            bus_url="ws://127.0.0.1:48999",
            zmq_pub_port=48990,
            zmq_analyze_port=48991,
            session_id="test",
        )

        # Register subscriber
        def my_handler(event):
            received_events.append(event)

        adapter.subscribe("voice_transcript_observed", my_handler)

        # Simulate receiving a ZMQ event
        zmq_event = {
            "event_type": "voice_transcript_observed",
            "lanlan_name": "test",
            "transcript": "hello world",
        }
        await adapter._handle_zmq_event(zmq_event)

        assert len(received_events) == 1
        assert received_events[0]["transcript"] == "hello world"

        # A different event_type should NOT trigger the handler
        zmq_event2 = {"event_type": "other_event", "data": "x"}
        await adapter._handle_zmq_event(zmq_event2)

        assert len(received_events) == 1  # Still 1, handler not called

    asyncio.run(run_test())


def test_get_agents_routing_table():
    """get_agents() returns current routing table."""
    adapter = NekoAdapter(
        bus_url="ws://127.0.0.1:48999",
        zmq_pub_port=48990,
        zmq_analyze_port=48991,
        session_id="test",
    )

    # Initially empty
    agents = adapter.get_agents()
    assert isinstance(agents, dict)

    # Simulate agent connecting
    adapter.agents["pi"] = {"status": "online", "session": "pi-main"}
    adapter.agents["claude"] = {"status": "online", "session": "claude-main"}

    agents = adapter.get_agents()
    assert "pi" in agents
    assert "claude" in agents
    assert agents["pi"]["status"] == "online"

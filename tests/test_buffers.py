"""Unbounded-buffer regression: bot buffers must be bounded with
drop-oldest semantics and a dropped counter (dashboard-down protection)."""


def test_buffers_are_bounded_with_drop_counter():
    from bot_core.state import BoundedBuffer

    buf = BoundedBuffer(maxlen=5)
    for i in range(12):
        buf.append(i)
    assert len(buf) == 5
    assert buf.dropped == 7
    assert list(buf) == [7, 8, 9, 10, 11]  # oldest dropped


def test_all_bot_buffers_are_bounded():
    from bot_core import state

    for name in dir(state):
        obj = getattr(state, name)
        if isinstance(obj, state.BoundedBuffer):
            assert obj.maxlen is not None, f"{name} unbounded"

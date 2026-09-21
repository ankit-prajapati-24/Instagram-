from engine.pipeline import Stage


def test_clips_run_after_voice_because_they_need_measured_duration():
    """Clip count is a function of beat length, and beat length only exists
    once synthesis has written measured_seconds."""
    order = list(Stage.ORDER)
    assert order.index(Stage.VOICE) < order.index(Stage.CLIPS)


def test_the_images_stage_is_gone():
    assert not hasattr(Stage, "IMAGES")
    assert "images" not in Stage.ORDER


def test_produce_order_starts_with_voice():
    assert Stage.PRODUCE[0] == Stage.VOICE

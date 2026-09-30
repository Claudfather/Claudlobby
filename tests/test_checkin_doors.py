"""The hand-caller tg-post sender identity remains independently covered."""
from pathlib import Path
LIB = Path(__file__).resolve().parent.parent / "claudlobby/_runtime_scripts"

def test_tg_post_anchors_the_sender_on_bot_id_with_the_hand_caller_fallback():
    text = (LIB / "tg-post.sh").read_text()
    assert 'bot:$FLEET_NAME/${BOT_ID:-$BOT_NAME}' in text        # a session has BOT_ID; bot-sweep-cron.sh sets only BOT_NAME
    assert 'bot:$FLEET_NAME/$BOT_NAME"' not in text

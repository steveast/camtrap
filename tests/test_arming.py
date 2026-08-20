"""S1.5: when is the alarm live? (spec 3.8, the owner's open question)

Decided: armed when the screen is locked, after an exit delay, plus a manual `camtrap arm`.
Unlocking disarms and opens a grace window — only the owner knows the password, so an unlock is
the most reliable "this is me, not someone else's hands" this machine can produce.

Capture is always on; only the noise is conditional.
"""

import pytest

from camtrap.arming import Arming
from camtrap.player import Stage
from camtrap.state import MODE_PAUSED, write_mode


@pytest.fixture
def session():
    class Session:
        def __init__(self):
            self.locked = False
            self.calls = 0

        def locked_hint(self):
            self.calls += 1
            return self.locked

    return Session()


@pytest.fixture
def arming(cfg, session):
    return Arming(cfg, session=session)


def test_unlocked_session_is_not_armed(arming, session):
    session.locked = False
    allowed, reason = arming.gate(Stage.SIREN, now=0.0)
    assert not allowed and reason == "not_armed"


def test_lock_arms_only_after_the_exit_delay(arming, session):
    arming.poll(now=0.0)
    session.locked = True
    arming.poll(now=1.0)  # locked at t=1
    allowed, reason = arming.gate(Stage.SIREN, now=30.0)
    assert not allowed and reason == "exit_delay"
    arming.poll(now=61.5)
    allowed, _ = arming.gate(Stage.SIREN, now=61.5)
    assert allowed


def test_unlock_disarms_and_opens_a_grace_window(arming, session):
    session.locked = True
    arming.poll(now=0.0)
    arming.poll(now=61.0)
    assert arming.gate(Stage.SIREN, now=61.0)[0]
    session.locked = False
    arming.poll(now=62.0)
    allowed, reason = arming.gate(Stage.SIREN, now=62.0)
    assert not allowed and reason == "unlock_grace"
    # the window closes 300 s after the unlock, but the session is still unlocked => not armed
    allowed, reason = arming.gate(Stage.SIREN, now=400.0)
    assert not allowed and reason == "not_armed"


def test_relocking_after_grace_arms_again(arming, session):
    session.locked = True
    arming.poll(now=0.0)
    arming.poll(now=61.0)
    session.locked = False
    arming.poll(now=62.0)
    session.locked = True
    arming.poll(now=100.0)
    assert not arming.gate(Stage.SIREN, now=100.0)[0]  # exit delay again
    arming.poll(now=161.0)
    assert arming.gate(Stage.SIREN, now=161.0)[0]


def test_manual_arm_wins_over_an_unlocked_session(cfg, session):
    from camtrap import state

    state.write_manual_arm(cfg.root, now=0.0)
    arming = Arming(cfg, session=session)
    session.locked = False
    arming.poll(now=61.0)
    allowed, _ = arming.gate(Stage.SIREN, now=61.0)
    assert allowed


def test_manual_arm_still_honours_the_exit_delay(cfg, session):
    from camtrap import state

    state.write_manual_arm(cfg.root, now=10.0)
    arming = Arming(cfg, session=session)
    arming.poll(now=11.0)
    assert not arming.gate(Stage.SIREN, now=11.0)[0]
    arming.poll(now=71.0)
    assert arming.gate(Stage.SIREN, now=71.0)[0]


def test_paused_mode_beats_everything(arming, session, cfg):
    session.locked = True
    arming.poll(now=0.0)
    arming.poll(now=61.0)
    write_mode(cfg.root, MODE_PAUSED, now=61.0)
    allowed, reason = arming.gate(Stage.SIREN, now=62.0)
    assert not allowed and reason == "paused"


def test_always_mode_arms_without_a_lock(cfg, session):
    cfg.arming.mode = "always"
    arming = Arming(cfg, session=session)
    session.locked = False
    arming.poll(now=0.0)
    allowed, _ = arming.gate(Stage.SIREN, now=0.5)
    assert allowed


def test_manual_mode_ignores_the_lock(cfg, session):
    cfg.arming.mode = "manual"
    arming = Arming(cfg, session=session)
    session.locked = True
    arming.poll(now=0.0)
    arming.poll(now=61.0)
    assert not arming.gate(Stage.SIREN, now=61.0)[0]


def test_warmup_holds_both_stages_then_releases(arming, session):
    cfg = arming.cfg
    cfg.arming.mode = "always"
    arming.start(now=0.0)
    allowed, reason = arming.gate(Stage.WARNING, now=5.0)
    assert not allowed and reason == "warmup"
    allowed, reason = arming.gate(Stage.SIREN, now=5.0)
    assert not allowed and reason == "warmup"
    # warm-up is over at 20 s, but the exit delay still holds the alarm until 60 s: the two
    # windows overlap deliberately, and the longer one wins
    allowed, reason = arming.gate(Stage.SIREN, now=25.0)
    assert not allowed and reason == "exit_delay"
    assert arming.gate(Stage.SIREN, now=61.0)[0]


def test_warning_can_be_held_while_the_siren_is_allowed(cfg, session):
    """A stage-specific gate: sound_on_motion off still leaves tampering audible."""
    cfg.arming.mode = "always"
    cfg.sound.warn_langs = []
    arming = Arming(cfg, session=session)
    arming.start(now=-100.0)
    assert not arming.gate(Stage.WARNING, now=0.0)[0]
    assert arming.gate(Stage.SIREN, now=0.0)[0]

"""The Step-6 compile must default to capcut-purple-pill everywhere.

The UI default is capcut-purple-pill, but the backend used to default to
submagic-pro — so any compile path that didn't explicitly send a template (or a
stale client) fell back to the wrong style. Lock both backend defaults so they
stay in sync with the UI.
"""
from __future__ import annotations

import inspect

from character_swap import api, runner_compile


def test_compile_body_default_template_is_purple_pill():
    assert api.CompileVideosBody().template == "capcut-purple-pill"


def test_compile_runner_default_template_is_purple_pill():
    default = inspect.signature(
        runner_compile.compile_job_videos
    ).parameters["template"].default
    assert default == "capcut-purple-pill"

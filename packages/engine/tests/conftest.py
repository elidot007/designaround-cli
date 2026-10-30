import os

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("da-home")
    monkeypatch.setenv("DESIGNAROUND_HOME", str(home))
    yield home


@pytest.fixture(scope="session")
def built_box(tmp_path_factory):
    """One storage-box version shared by read-only tests (builds take seconds)."""

    os.environ.setdefault("DESIGNAROUND_HOME", str(tmp_path_factory.mktemp("shared-home")))
    from designaround_engine import cad, views
    from designaround_engine.templates import get_template_source

    out = tmp_path_factory.mktemp("box") / "v1"
    cad.build_program(get_template_source("storage_box"), {}, out)
    views.render_views(out, size=600)
    return out

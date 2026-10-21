"""Integration tests run inside QGIS's Python: scripts/qgis-python -m pytest tests/qgis"""
import os
import tempfile

import pytest

_PROFILE = tempfile.mkdtemp(prefix="qgissi-test-profile-")
os.environ.setdefault("QGISSI_PROFILE_DIR", _PROFILE)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qgis_app():
    from qgissi.headless import start_qgis
    return start_qgis(gui=True)


@pytest.fixture(scope="session")
def fixture_dir(qgis_app, tmp_path_factory):
    from qgissi.bench.fixtures import build_fixture_dir
    return build_fixture_dir(str(tmp_path_factory.mktemp("fixtures")))


@pytest.fixture()
def project(qgis_app, fixture_dir, tmp_path):
    import shutil

    from qgis.core import QgsProject

    from qgissi.bench.runner import load_fixture_project
    data = tmp_path / "data"
    shutil.copytree(fixture_dir, data)
    load_fixture_project(QgsProject.instance(), str(data))
    yield QgsProject.instance()
    QgsProject.instance().clear()


@pytest.fixture()
def ctx(project, tmp_path):
    from qgissi.tools.registry import ToolContext
    return ToolContext(project=project, output_dir=str(tmp_path / "out"), headless=True)


def call(ctx, tool_name, **args):
    from qgissi.tools import REGISTRY
    return REGISTRY.invoke(REGISTRY.get(tool_name), args, ctx)

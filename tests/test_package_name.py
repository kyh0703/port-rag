"""Public Python package name tests."""

import importlib


def test_rag_package_exposes_application_module():
    module = importlib.import_module("rag.main")

    assert module.create_app().title == "rag"

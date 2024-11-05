#!/usr/bin/env python3
from __future__ import annotations

from importlib.metadata import version as get_version

from packaging.version import parse

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.intersphinx",
    "sphinx_autodoc_typehints",
    "sphinx_rtd_theme",
    "sphinx_tabs.tabs",
    "enum_tools.autoenum",
]

templates_path = ["_templates"]
source_suffix = ".rst"
master_doc = "index"
project = "mqttproto"
author = "Alex Grönholm"
copyright = "2024, " + author

v = parse(get_version("mqttproto"))
version = v.base_version
release = v.public

language = "en"

exclude_patterns = ["_build"]
pygments_style = "sphinx"
autodoc_default_options = {"members": True}
todo_include_todos = False

html_theme = "sphinx_rtd_theme"
htmlhelp_basename = project + "doc"

intersphinx_mapping = {
    "python": ("https://docs.python.org/3/", None),
    "anyio": ("https://anyio.readthedocs.io/en/stable/", None),
}

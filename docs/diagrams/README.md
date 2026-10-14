# Architecture diagram

`platform.png` is generated from `platform.py` with the Python
[diagrams](https://diagrams.mingrammer.com/) library (version 0.25.1, Graphviz 14.1.4). Each box in the
source cites the ADR it comes from. The tools are kept out of the project's `pyproject.toml` and
`uv.lock`; they live in a toolbox container.

## Regenerate

One-time setup (Fedora Silverblue):

```bash
toolbox create -y ecp-diagrams
toolbox run -c ecp-diagrams sudo dnf install -y graphviz
toolbox run -c ecp-diagrams python3 -m venv ~/.local/share/ecp-diagrams-venv
toolbox run -c ecp-diagrams ~/.local/share/ecp-diagrams-venv/bin/pip install diagrams==0.25.1
```

Each time the source changes:

```bash
cd docs/diagrams
toolbox run -c ecp-diagrams ~/.local/share/ecp-diagrams-venv/bin/python -I platform.py
```

`-I` is needed: the script is named `platform.py`, and without it Python imports the script in place
of the standard-library `platform` module.
